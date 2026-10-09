# tests/test_disambiguation.py
from agent.disambiguation import (
    Ambiguous,
    build_question,
    Confirmed,
    decide,
    NoCandidates,
    normalize,
    rank,
)
from agent.models import (
    Identity,
    Network,
)
from agent.config import ScoringTuning
from agent import disambiguation

import pytest

SCORING = ScoringTuning()
THRESHOLD = 0.7
MARGIN = 0.15
JAN = Identity(name="Jan Novák", employers=["Acme"], location="Brno", skills=["python", "rust"])


def score_text(identity, url, title, text, tuning=SCORING):
    return disambiguation.score_text(identity, url, title, text, tuning)


def candidate(identity, url, title, snippet="", text="", tuning=SCORING):
    return disambiguation.make_candidate(identity, url, title, snippet, text, tuning)


def test_normalize_strips_diacritics_case_and_punctuation():
    assert normalize("  Jan   NOVÁK-Svoboda, Ph.D. ") == "jan novak svoboda ph d"
    assert normalize("Žluťoučký kůň") == "zlutoucky kun"
    assert normalize("Müller Straße") == "muller strasse"


def test_full_match_scores_one():
    score = score_text(
        JAN,
        "https://www.linkedin.com/in/jan-novak-12ab34",
        "Jan Novak - CTO - Acme | LinkedIn",
        "Brno, Czechia. Works with Python and Rust.",
    )
    assert (score.name, score.employer, score.location, score.skills) == (1.0, 1.0, 1.0, 1.0)
    assert score.total == 1.0


def test_mismatched_dimensions_reduce_the_total_by_weight():
    score = score_text(
        JAN, "https://www.linkedin.com/in/jan-novak", "Jan Novak - Teacher", "Prague"
    )
    assert (score.name, score.employer, score.location, score.skills) == (1.0, 0.0, 0.0, 0.0)
    assert score.total == pytest.approx(0.45)


def test_initials_on_either_side_match():
    wanted_initial = score_text(Identity(name="J Novak"), "https://x.example/a", "Jan Novak", "")
    candidate_initial = score_text(
        Identity(name="Jan Novak"), "https://x.example/a", "J. Novak", ""
    )
    assert wanted_initial.name == candidate_initial.name == 1.0


def test_a_candidate_without_any_name_scores_zero():
    score = score_text(Identity(name="Jan Novak"), "https://x.example/a", "", "unrelated words")
    assert score.name == 0.0


def test_dimensions_missing_from_the_identity_are_ignored():
    score = score_text(Identity(name="Jan Novak"), "https://x.example/a", "Jan Novak", "anything")
    assert (score.employer, score.location, score.skills) == (None, None, None)
    assert score.total == 1.0


def test_partial_skill_overlap_is_proportional():
    identity = Identity(name="Jan Novak", skills=["python", "rust", "go"])
    score = score_text(identity, "https://x.example/a", "Jan Novak", "python and rust")
    assert score.skills == pytest.approx(2 / 3)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Jan Novak - CTO", 1.0),
        ("Novák Jan | Acme", 1.0),
        ("J. Novak - Engineer", 1.0),
        ("Jan Novaak - Engineer", 1.0),
        ("Jan Svoboda - Engineer", 0.5),
        ("Petr Svoboda - Engineer", 0.0),
    ],
)
def test_name_matching_variants(title, expected):
    score = score_text(Identity(name="Jan Novak"), "https://x.example/a", title, "")
    assert score.name == pytest.approx(expected)


def test_nickname_matches_through_the_profile_slug_and_aliases():
    identity = Identity(name="Jan Novak", aliases=["pygot"])
    by_slug = score_text(identity, "https://www.instagram.com/pygot/", "Instagram", "")
    assert by_slug.name == 1.0
    by_text = score_text(identity, "https://example.com", "Blog", "Posts by Pygot about code")
    assert by_text.name == 1.0


def test_linkedin_id_suffix_is_not_part_of_the_name():
    score = score_text(
        Identity(name="Jan Novak"), "https://www.linkedin.com/in/jan-novak-1a2b3c4", "LinkedIn", ""
    )
    assert score.name == 1.0


def test_decide_confirms_a_clear_winner():
    best = candidate(
        JAN, "https://www.linkedin.com/in/jan-novak", "Jan Novak - Acme", "Brno python"
    )
    other = candidate(
        JAN, "https://www.linkedin.com/in/jan-novak-2", "Jan Novak - Teacher", "Prague"
    )
    decision = decide([other, best], THRESHOLD, MARGIN)
    assert isinstance(decision, Confirmed)
    assert decision.candidate.url == best.url


def test_decide_asks_when_the_top_two_are_close():
    first = candidate(
        JAN, "https://www.linkedin.com/in/jan-novak", "Jan Novak - Acme", "Brno python"
    )
    second = candidate(
        JAN, "https://www.linkedin.com/in/jan-novak-2", "Jan Novak - Acme", "Brno python"
    )
    decision = decide([first, second], THRESHOLD, MARGIN)
    assert isinstance(decision, Ambiguous)
    assert len(decision.candidates) == 2


def test_decide_margin_boundary():
    first = candidate(JAN, "https://www.linkedin.com/in/a", "Jan Novak - Acme", "Brno python")
    second = candidate(JAN, "https://www.linkedin.com/in/b", "Jan Novak - Acme", "Brno")
    gap = first.score.total - second.score.total
    assert isinstance(decide([first, second], THRESHOLD, gap + 0.001), Ambiguous)
    assert isinstance(decide([first, second], THRESHOLD, gap - 0.001), Confirmed)


def test_decide_asks_when_the_only_candidate_is_below_the_threshold():
    weak = candidate(JAN, "https://www.linkedin.com/in/jan-novak", "Jan Novak - Teacher", "Prague")
    assert weak.score.total < THRESHOLD
    decision = decide([weak], THRESHOLD, MARGIN)
    assert isinstance(decision, Ambiguous)
    assert decision.candidates == [weak]


def test_decide_with_no_candidates():
    assert isinstance(decide([], THRESHOLD, MARGIN), NoCandidates)


def test_threshold_boundary_is_inclusive():
    exact = candidate(Identity(name="Jan Novak"), "https://x.example/a", "Jan Novak")
    assert exact.score.total == 1.0
    assert isinstance(decide([exact], 1.0, MARGIN), Confirmed)


def test_rank_is_deterministic_for_ties():
    a = candidate(JAN, "https://www.linkedin.com/in/a", "Jan Novak - Acme", "Brno python")
    b = candidate(JAN, "https://www.linkedin.com/in/b", "Jan Novak - Acme", "Brno python")
    assert [c.url for c in rank([b, a])] == [a.url, b.url]


def test_build_question_lists_at_most_three_options_best_first():
    options = [
        candidate(
            JAN, f"https://www.linkedin.com/in/jan-{n}", "Jan Novak", "Brno" if n == 3 else ""
        )
        for n in range(5)
    ]
    question = build_question(JAN, Network.LINKEDIN, options, SCORING)
    assert question.network is Network.LINKEDIN
    assert len(question.options) == 3
    assert question.options[0].url.endswith("jan-3")
    assert "Jan Novák" in question.text
    assert "linkedin" in question.text


def test_weights_are_configurable():
    identity = Identity(name="Jan Novak", employers=["Acme"])
    page = ("https://x.example/a", "Jan Novak", "no employer here")
    default = score_text(identity, *page)
    heavy_name = score_text(identity, *page, ScoringTuning(name_weight=0.9, employer_weight=0.1))
    assert heavy_name.total > default.total
    assert heavy_name.total == pytest.approx(0.9)


def test_fuzzy_matching_ratio_is_configurable():
    identity = Identity(name="Jan Novak")
    page = ("https://x.example/a", "Jan Novaak", "")
    assert score_text(identity, *page).name == 1.0
    assert score_text(identity, *page, ScoringTuning(fuzzy_token_ratio=1.0)).name == 0.5


def test_the_number_of_options_in_a_question_is_configurable():
    options = [
        candidate(JAN, f"https://www.linkedin.com/in/jan-{n}", "Jan Novak") for n in range(5)
    ]
    question = build_question(JAN, Network.LINKEDIN, options, ScoringTuning(max_options=2))
    assert len(question.options) == 2
    assert all("name 100%" in option.evidence for option in question.options)
