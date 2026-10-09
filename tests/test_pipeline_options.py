# tests/test_pipeline_options.py
from agent.models import (
    Clarification,
    Goal,
    Network,
    PersonReport,
    ResearchOptions,
    ResearchRequest,
    ScepticismMode,
    SkillSearchRequest,
    Strictness,
)
from tests.test_pipeline import (
    build,
    IDENTITY,
    LI_JAN,
    linkedin_collector,
    linkedin_doc,
    make_llm,
    raw,
    request,
    SITE,
)
from tests.fakes import (
    document,
    FakeSearch,
    hit,
    RuleLLM,
)
from agent.errors import InvalidRequest
from agent.pipeline import MatchRules
from agent.config import Settings

import pytest


def options_request(**options):
    return ResearchRequest(
        goal=Goal.HIRING,
        identity=IDENTITY,
        purpose_confirmed=True,
        options=ResearchOptions(**options),
    )


def test_match_rules_follow_defaults_presets_and_overrides():
    pipeline, _ = build()
    assert pipeline.match_rules(ResearchOptions()) == MatchRules(0.7, 0.15)
    strict = pipeline.match_rules(ResearchOptions(strictness=Strictness.STRICT))
    lenient = pipeline.match_rules(ResearchOptions(strictness=Strictness.LENIENT))
    assert strict == MatchRules(0.8, 0.2)
    assert lenient == MatchRules(0.6, 0.1)
    explicit = ResearchOptions(strictness=Strictness.STRICT, threshold=0.5, margin=0.05)
    assert pipeline.match_rules(explicit) == MatchRules(0.5, 0.05)
    assert pipeline.match_rules(ResearchOptions(margin=0.3)) == MatchRules(0.7, 0.3)


def test_match_rules_use_the_configured_defaults():
    settings = Settings(_env_file=None, scoring={"threshold": 0.9, "margin": 0.25})
    pipeline, _ = build(settings=settings)
    assert pipeline.match_rules(ResearchOptions()) == MatchRules(0.9, 0.25)


async def test_sources_switched_off_in_the_options_are_not_searched_and_are_reported():
    search = FakeSearch({"site:linkedin.com/in": [hit(LI_JAN, "Jan Novak", "Brno")]})
    collectors = linkedin_collector({LI_JAN: linkedin_doc()})
    pipeline, fetcher = build(search, collectors)
    report = await pipeline.research(options_request(networks=[Network.LINKEDIN]))
    assert [s.url for s in report.sources] == [LI_JAN]
    assert fetcher.calls == []
    assert all("portfolio" not in query for query in search.queries)
    assert any("open web was switched off" in item for item in report.limitations)
    assert any("facebook was switched off" in item for item in report.limitations)
    assert report.options.networks == [Network.LINKEDIN]


async def test_linkedin_can_be_switched_off():
    search = FakeSearch({})
    collectors = linkedin_collector({LI_JAN: linkedin_doc()})
    pipeline, _ = build(search, collectors)
    report = await pipeline.research(options_request(networks=[Network.WEB]))
    assert collectors[Network.LINKEDIN].calls == []
    assert all("site:linkedin.com/in" not in query for query in search.queries)
    assert any("linkedin was switched off" in item for item in report.limitations)


async def test_a_stricter_preset_turns_a_confident_match_into_a_question():
    search = FakeSearch({"site:linkedin.com/in": [hit(LI_JAN, "Jan Novak", "Brno")]})
    middling = linkedin_doc(text="Jan Novak. Python engineer at Globex.")
    pipeline, _ = build(search, linkedin_collector({LI_JAN: middling}))
    balanced = await pipeline.research(options_request(strictness=Strictness.BALANCED))
    assert isinstance(balanced, PersonReport)
    strict = await pipeline.research(options_request(strictness=Strictness.STRICT))
    assert isinstance(strict, Clarification)


async def test_the_threshold_override_beats_the_preset():
    search = FakeSearch({"site:linkedin.com/in": [hit(LI_JAN, "Jan Novak", "Brno")]})
    middling = linkedin_doc(text="Jan Novak. Python engineer at Globex.")
    pipeline, _ = build(search, linkedin_collector({LI_JAN: middling}))
    result = await pipeline.research(options_request(strictness=Strictness.LENIENT, threshold=0.99))
    assert isinstance(result, Clarification)


async def test_the_web_page_budget_comes_from_options_and_settings():
    many = [hit(f"https://site{n}.example/jan", "Jan", "") for n in range(8)]
    pages = {h.url: document(h.url, "Jan Novak Acme Brno python", title="Jan Novak") for h in many}
    search = FakeSearch({"portfolio": many})
    pipeline, fetcher = build(search, pages=pages)
    await pipeline.research(options_request(max_web_pages=2))
    assert len(fetcher.calls) == 2
    default, fetcher = build(search, pages=pages)
    await default.research(options_request())
    assert len(fetcher.calls) == 5
    custom = Settings(_env_file=None, discovery={"max_web_pages": 3})
    configured, fetcher = build(search, pages=pages, settings=custom)
    await configured.research(options_request())
    assert len(fetcher.calls) == 3


async def test_a_zero_page_budget_skips_the_web_search_entirely():
    search = FakeSearch({"portfolio": [hit(SITE, "Jan", "")]})
    pipeline, fetcher = build(search, pages={SITE: document(SITE, "Jan Novak")})
    report = await pipeline.research(options_request(max_web_pages=0))
    assert fetcher.calls == []
    assert all("portfolio" not in query for query in search.queries)
    assert report.sources == []


async def test_candidates_per_network_is_configurable():
    hits = [hit(f"https://www.linkedin.com/in/jan-novak-{n}", "Jan Novak", "") for n in range(5)]
    docs = {h.url: linkedin_doc(h.url) for h in hits}
    collectors = linkedin_collector(docs)
    settings = Settings(_env_file=None, discovery={"candidates_per_network": 2})
    pipeline, _ = build(FakeSearch({"site:linkedin.com/in": hits}), collectors, settings=settings)
    result = await pipeline.research(request())
    assert isinstance(result, Clarification)
    assert len(collectors[Network.LINKEDIN].calls) == 2


async def test_the_scepticism_mode_reaches_the_rating_and_the_report():
    claims = {SITE: [raw(f"Fact {n}", "skill") for n in range(3)]}
    page = document(SITE, "Jan Novak works at Acme in Brno python.", title="Jan Novak")
    search = FakeSearch({"portfolio": [hit(SITE, "", "")]})
    pipeline, _ = build(search, pages={SITE: page}, llm=make_llm(claims))
    standard = await pipeline.research(options_request())
    strict = await pipeline.research(options_request(scepticism=ScepticismMode.STRICT))
    assert standard.rating.scepticism.mode is ScepticismMode.STANDARD
    assert strict.rating.scepticism.mode is ScepticismMode.STRICT
    assert strict.options.scepticism is ScepticismMode.STRICT


async def test_a_too_good_profile_is_capped_end_to_end():
    claims = {
        LI_JAN: [raw(f"Led project number {n}", "project") for n in range(3)],
        SITE: [raw(f"Led project number {n}", "project") for n in range(3, 6)],
    }
    collectors = linkedin_collector({LI_JAN: linkedin_doc()})
    page = document(SITE, "Jan Novak Acme Brno python", title="Jan Novak")
    search = FakeSearch(
        {
            "site:linkedin.com/in": [hit(LI_JAN, "Jan Novak", "Brno")],
            "portfolio": [hit(SITE, "", "")],
        }
    )
    pipeline, _ = build(search, collectors, {SITE: page}, make_llm(claims, fit=5))
    report = await pipeline.research(request())
    assert report.rating.raw_overall > report.rating.overall
    assert report.rating.scepticism.level.value in {"medium", "high"}
    assert report.rating.scepticism.guidance


async def test_skill_search_uses_only_the_chosen_sources_and_applies_scepticism():
    gh = "https://github.com/ada"
    search = FakeSearch(
        {
            "site:linkedin.com/in": [hit(LI_JAN, "Jan", "")],
            "site:github.com": [hit(gh, "ada", "")],
        }
    )
    collectors = linkedin_collector({LI_JAN: linkedin_doc()})
    pages = {gh: document(gh, "ada rust")}

    def summarize(user):
        return {"subject_name": "Ada Lovelace", "claims": [raw("Writes Rust", "skill")]}

    def rate(user):
        verdict = {"score": 4, "justification": "Rust work.", "finding_ids": ["F1"]}
        return {"skill_fit": verdict, "evidence_of_work": verdict}

    llm = RuleLLM({"extract professional facts": summarize, "rate one person": rate})
    pipeline, _ = build(search, collectors, pages, llm)
    skill_request = SkillSearchRequest(
        goal=Goal.HIRING,
        skill="Rust",
        location="Brno",
        purpose_confirmed=True,
        options=ResearchOptions(networks=[Network.WEB], scepticism=ScepticismMode.STRICT),
    )
    report = await pipeline.skill_search(skill_request)
    assert [c.profile_url for c in report.candidates] == [gh]
    assert report.candidates[0].rating.scepticism.mode is ScepticismMode.STRICT
    assert all("linkedin" not in query for query in search.queries)
    assert report.options.scepticism is ScepticismMode.STRICT


async def test_skill_search_without_a_usable_source_is_an_invalid_request():
    pipeline, _ = build()
    skill_request = SkillSearchRequest(
        goal=Goal.HIRING,
        skill="Rust",
        location="Brno",
        purpose_confirmed=True,
        options=ResearchOptions(networks=[Network.FACEBOOK, Network.INSTAGRAM]),
    )
    with pytest.raises(InvalidRequest, match="LinkedIn or the open web"):
        await pipeline.skill_search(skill_request)
