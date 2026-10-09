# tests/test_rating.py
from agent.models import (
    Category,
    Claim,
    Confidence,
    Criterion,
    CriterionRating,
    Finding,
    Goal,
    Network,
    Rating,
    Scepticism,
    ScepticismLevel,
    ScepticismMode,
    Tag,
)
from agent.rating import (
    half_up,
    NO_TARGET,
    overall_score,
    rank_candidates,
    rate,
    rate_consistency,
    rate_recency,
    Scored,
)
from agent.config import (
    RatingTuning,
    ScepticismTuning,
)
from agent.errors import LLMOutputError
from tests.fakes import RuleLLM

import pytest

RATING = RatingTuning()
SCEPTIC = ScepticismTuning()
MODE = ScepticismMode.STANDARD
SITE = "https://jan.dev"
GITHUB = "https://github.com/jan"
LINKEDIN = "https://www.linkedin.com/in/jan"


def finding(number, tag=Tag.SINGLE_SOURCE, year=None, urls=(SITE,), conflict=False):
    return Finding(
        id=f"F{number}",
        statement=f"Statement {number}",
        category=Category.EMPLOYMENT,
        tag=tag,
        source_urls=list(urls),
        independent_sources=len(urls),
        conflict=conflict,
        year=year,
    )


def criteria(skill=0, evidence=0, recency=0, consistency=0):
    scores = {
        Criterion.SKILL_FIT: skill,
        Criterion.EVIDENCE_OF_WORK: evidence,
        Criterion.RECENCY: recency,
        Criterion.CONSISTENCY: consistency,
    }
    return [
        CriterionRating(criterion=name, score=score, justification="because")
        for name, score in scores.items()
    ]


@pytest.mark.parametrize("goal", list(Goal))
def test_weights_sum_to_one_and_cover_every_criterion(goal):
    assert set(RATING.weights[goal.value]) == {c.value for c in Criterion}
    assert sum(RATING.weights[goal.value].values()) == 100


def test_half_up_rounding():
    assert [half_up(x) for x in (0.4, 0.5, 1.5, 2.5, 3.49, 4.5)] == [0, 1, 2, 3, 3, 5]


def test_overall_is_the_weighted_mean():
    assert overall_score(Goal.HIRING, criteria(5, 5, 5, 5), RATING) == 5.0
    assert overall_score(Goal.HIRING, criteria(), RATING) == 0.0
    assert overall_score(Goal.HIRING, criteria(4, 3, 2, 1), RATING) == 2.8
    assert overall_score(Goal.SALES, criteria(4, 3, 2, 1), RATING) == 2.7
    assert overall_score(Goal.DUE_DILIGENCE, criteria(4, 3, 2, 1), RATING) == 2.4
    assert overall_score(Goal.HIRING, criteria(3, 2, 0, 0), RATING) == 1.7


@pytest.mark.parametrize(
    ("latest", "score"),
    [(2026, 5), (2025, 4), (2024, 3), (2023, 2), (2022, 1), (2021, 1), (2020, 0), (2000, 0)],
)
def test_recency_by_age_of_the_latest_dated_finding(latest, score):
    result = rate_recency([finding(1, year=2010), finding(2, year=latest)], 2026, RATING)
    assert result.score == score
    assert result.criterion is Criterion.RECENCY


def test_recency_future_year_counts_as_current_and_names_the_sources():
    result = rate_recency(
        [finding(1, year=2027, urls=(GITHUB,)), finding(2, year=2020)], 2026, RATING
    )
    assert result.score == 5
    assert result.source_urls == [GITHUB]


def test_recency_without_dates_is_zero_with_explanation():
    result = rate_recency([finding(1)], 2026, RATING)
    assert result.score == 0
    assert "No finding carries a year" in result.justification
    assert rate_recency([], 2026, RATING).score == 0


def test_consistency_needs_two_independent_sources():
    result = rate_consistency(
        [finding(1, urls=(SITE,)), finding(2, urls=("https://jan.dev/x",))], RATING
    )
    assert result.score == 0
    assert "cannot be assessed" in result.justification
    assert rate_consistency([], RATING).score == 0


def test_consistency_without_overlap_is_neutral():
    result = rate_consistency([finding(1, urls=(SITE,)), finding(2, urls=(GITHUB,))], RATING)
    assert result.score == 2
    assert "nothing to compare" in result.justification


def test_consistency_reflects_corroboration_against_conflicts():
    supported = [
        finding(1, Tag.SUPPORTED, urls=(SITE, GITHUB)),
        finding(2, Tag.SUPPORTED, urls=(SITE, GITHUB)),
    ]
    conflicting = finding(3, Tag.UNCERTAIN, urls=(SITE, LINKEDIN), conflict=True)
    assert rate_consistency(supported, RATING).score == 5
    mixed = rate_consistency([*supported, conflicting], RATING)
    assert mixed.score == 3
    assert "2 corroborated and 1 conflicting" in mixed.justification
    assert LINKEDIN in mixed.source_urls
    assert rate_consistency([conflicting], RATING).score == 0


def judgement(score, finding_ids, text="Solid evidence."):
    return {"score": score, "justification": text, "finding_ids": finding_ids}


def llm_returning(skill_fit, evidence):
    return RuleLLM(
        {"rate one person": lambda user: {"skill_fit": skill_fit, "evidence_of_work": evidence}}
    )


async def test_rate_combines_model_and_deterministic_criteria():
    findings = [
        finding(1, Tag.SUPPORTED, year=2025, urls=(SITE, GITHUB)),
        finding(2, Tag.SINGLE_SOURCE, year=2019, urls=(LINKEDIN,)),
    ]
    llm = llm_returning(judgement(4, ["F1"]), judgement(3, ["F1", "F2"]))
    rating = await rate(llm, Goal.HIRING, "Python engineer", findings, 2026, RATING, SCEPTIC, MODE)
    assert rating.target == "Python engineer"
    by_name = {item.criterion: item for item in rating.criteria}
    assert by_name[Criterion.SKILL_FIT].score == 4
    assert by_name[Criterion.SKILL_FIT].source_urls == [SITE, GITHUB]
    assert by_name[Criterion.EVIDENCE_OF_WORK].source_urls == [SITE, GITHUB, LINKEDIN]
    assert by_name[Criterion.RECENCY].score == 4
    assert by_name[Criterion.CONSISTENCY].score == 5
    assert rating.overall == overall_score(Goal.HIRING, rating.criteria, RATING)
    prompt = llm.calls[0][1]
    assert "Target: Python engineer" in prompt
    assert "F1 [employment] [supported] (2025) Statement 1" in prompt
    assert "F2 [employment] [single-source] (2019)" in prompt


async def test_rate_without_findings_makes_no_model_call_and_scores_zero():
    llm = RuleLLM({})
    rating = await rate(llm, Goal.SALES, None, [], 2026, RATING, SCEPTIC, MODE)
    assert llm.calls == []
    assert rating.overall == 0.0
    assert rating.target == NO_TARGET
    assert all(item.score == 0 for item in rating.criteria)
    assert "No findings were available" in rating.criteria[0].justification


async def test_rate_rejects_unknown_finding_ids_and_uncited_scores():
    findings = [finding(1)]
    unknown = llm_returning(judgement(4, ["F9"]), judgement(0, []))
    with pytest.raises(LLMOutputError, match="unknown finding ids"):
        await rate(unknown, Goal.HIRING, None, findings, 2026, RATING, SCEPTIC, MODE)
    uncited = llm_returning(judgement(3, []), judgement(0, []))
    with pytest.raises(LLMOutputError, match="cites no finding"):
        await rate(uncited, Goal.HIRING, None, findings, 2026, RATING, SCEPTIC, MODE)
    blank = llm_returning(judgement(0, [], text="  "), judgement(0, []))
    with pytest.raises(LLMOutputError, match="no justification"):
        await rate(blank, Goal.HIRING, None, findings, 2026, RATING, SCEPTIC, MODE)


async def test_rate_rejects_out_of_range_scores():
    llm = llm_returning(judgement(6, ["F1"]), judgement(0, []))
    with pytest.raises(LLMOutputError):
        await rate(llm, Goal.HIRING, None, [finding(1)], 2026, RATING, SCEPTIC, MODE)


async def test_justifications_are_collapsed_to_one_bounded_line():
    long_text = "word\nnext " + "x" * 500
    llm = llm_returning(judgement(2, ["F1"], long_text), judgement(0, []))
    rating = await rate(llm, Goal.HIRING, None, [finding(1)], 2026, RATING, SCEPTIC, MODE)
    text = rating.criteria[0].justification
    assert "\n" not in text
    assert len(text) == 300 and text.endswith("...")


def scored(name, overall_scores, url=None):
    raw = overall_score(Goal.HIRING, criteria(*overall_scores), RATING)
    rating = Rating(
        target="rust",
        criteria=criteria(*overall_scores),
        raw_overall=raw,
        overall=raw,
        scepticism=Scepticism(mode=MODE, level=ScepticismLevel.NONE),
    )
    claim = Claim(
        statement="Writes Rust",
        category=Category.SKILL,
        confidence=Confidence.HIGH,
        source_url=url or f"https://github.com/{name.lower()}",
    )
    return Scored(name, claim.source_url, Network.WEB, [claim], rating)


def test_candidates_are_ranked_and_the_best_is_marked():
    ranked = rank_candidates(
        [scored("Low", (1, 1, 1, 0)), scored("High", (5, 4, 4, 0)), scored("Mid", (3, 3, 3, 0))]
    )
    assert [c.name for c in ranked] == ["High", "Mid", "Low"]
    assert [c.rank for c in ranked] == [1, 2, 3]
    assert [c.best for c in ranked] == [True, False, False]


def test_ranking_ties_break_on_skill_fit_then_evidence_then_name():
    skill_led = scored("Zed", (3, 0, 0, 0))
    evidence_led = scored("Amy", (0, 3, 1, 0))
    assert skill_led.rating.overall == evidence_led.rating.overall
    assert [c.name for c in rank_candidates([evidence_led, skill_led])] == ["Zed", "Amy"]

    more_evidence = scored("Zed", (3, 2, 0, 0))
    less_evidence = scored("Amy", (3, 0, 4, 0))
    assert more_evidence.rating.overall == less_evidence.rating.overall
    assert [c.name for c in rank_candidates([less_evidence, more_evidence])] == ["Zed", "Amy"]

    same_b = scored("Bob", (3, 3, 3, 0))
    same_a = scored("Al", (3, 3, 3, 0))
    assert [c.name for c in rank_candidates([same_b, same_a])] == ["Al", "Bob"]


def test_ranking_nothing_gives_nothing():
    assert rank_candidates([]) == []


def test_rating_weights_and_recency_table_are_configurable():
    custom = RatingTuning(
        weights={
            "hiring": {"skill_fit": 100, "evidence_of_work": 0, "recency": 0, "consistency": 0},
            "sales": {"skill_fit": 25, "evidence_of_work": 25, "recency": 25, "consistency": 25},
            "due_diligence": {
                "skill_fit": 25,
                "evidence_of_work": 25,
                "recency": 25,
                "consistency": 25,
            },
        },
        recency_by_age={0: 5, 1: 5, 2: 5},
        neutral_consistency=4,
        max_justification_chars=40,
    )
    assert overall_score(Goal.HIRING, criteria(4, 5, 5, 5), custom) == 4.0
    assert rate_recency([finding(1, year=2025)], 2026, custom).score == 5
    assert rate_recency([finding(1, year=2020)], 2026, custom).score == 0
    unrelated = [finding(1, urls=(SITE,)), finding(2, urls=(GITHUB,))]
    assert rate_consistency(unrelated, custom).score == 4


async def test_justification_limit_is_configurable():
    llm = llm_returning(judgement(2, ["F1"], "x" * 100), judgement(0, []))
    custom = RatingTuning(max_justification_chars=40)
    rating = await rate(llm, Goal.HIRING, None, [finding(1)], 2026, custom, SCEPTIC, MODE)
    assert len(rating.criteria[0].justification) == 40


def hyped_findings():
    return [finding(number, year=2025) for number in range(1, 6)]


async def test_a_single_source_profile_with_many_claims_is_capped():
    llm = llm_returning(judgement(5, ["F1"]), judgement(5, ["F2"]))
    rating = await rate(llm, Goal.HIRING, "Python", hyped_findings(), 2026, RATING, SCEPTIC, MODE)
    assert rating.raw_overall == 3.9
    assert rating.overall == 3.5
    assert rating.scepticism.level is ScepticismLevel.MEDIUM
    assert rating.scepticism.flags[0].code == "thin_sources"
    assert rating.scepticism.guidance


async def test_a_too_good_multi_source_profile_is_capped_hard():
    urls = [(SITE,), (GITHUB,)]
    findings = [finding(number, year=2025, urls=urls[number % 2]) for number in range(1, 7)]
    llm = llm_returning(judgement(5, ["F1"]), judgement(5, ["F2"]))
    rating = await rate(llm, Goal.HIRING, "Python", findings, 2026, RATING, SCEPTIC, MODE)
    assert rating.raw_overall == 4.3
    assert rating.overall == 2.5
    assert rating.scepticism.level is ScepticismLevel.HIGH
    assert rating.scepticism.flags[0].code == "uncorroborated_excellence"


async def test_strict_scepticism_caps_harder_than_standard():
    findings = [finding(1, Tag.SUPPORTED, year=2025, urls=(SITE, GITHUB)), finding(2, year=2025)]
    llm = llm_returning(judgement(5, ["F1"]), judgement(5, ["F1"]))
    standard = await rate(llm, Goal.HIRING, None, findings, 2026, RATING, SCEPTIC, MODE)
    llm = llm_returning(judgement(5, ["F1"]), judgement(5, ["F1"]))
    strict = await rate(
        llm, Goal.HIRING, None, findings, 2026, RATING, SCEPTIC, ScepticismMode.STRICT
    )
    assert strict.overall <= standard.overall
    assert strict.scepticism.mode is ScepticismMode.STRICT


async def test_candidates_without_corroboration_are_not_penalised_for_it():
    llm = llm_returning(judgement(5, ["F1"]), judgement(5, ["F2"]))
    rating = await rate(
        llm,
        Goal.HIRING,
        "Rust",
        hyped_findings(),
        2026,
        RATING,
        SCEPTIC,
        MODE,
        corroboration_expected=False,
    )
    assert rating.scepticism.level is ScepticismLevel.NONE
    assert rating.overall == rating.raw_overall


async def test_the_rating_prompt_asks_the_model_to_be_sceptical():
    llm = llm_returning(judgement(2, ["F1"]), judgement(0, []))
    await rate(llm, Goal.HIRING, None, [finding(1)], 2026, RATING, SCEPTIC, MODE)
    assert "sceptical" in llm.calls[0][0]
    assert "too good to be true" in llm.calls[0][0]
