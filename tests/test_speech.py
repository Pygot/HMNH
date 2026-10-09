# tests/test_speech.py
from tests.fakes import (
    person_report,
    sceptical_report,
    skill_report,
)
from agent.speech import (
    describe_clarification,
    describe_report,
)
from tests.web_helpers import clarification

SPOKEN_FLAGS = 2


def test_a_clean_report_is_summarised_without_warnings():
    text = describe_report(person_report(), SPOKEN_FLAGS)
    assert text.startswith("Report on Jan Novak.")
    assert "Overall rating 3.4 out of 5, rated against Python engineer." in text
    assert "1 findings from 1 sources, 0 supported by two or more sources." in text
    assert "Scepticism" not in text and "reduced" not in text


def test_a_sceptical_report_says_why_the_rating_was_reduced():
    text = describe_report(sceptical_report(), SPOKEN_FLAGS)
    assert "It was reduced from 4.4 because of scepticism." in text
    assert "Scepticism is high. Roles overlap." in text


def test_the_number_of_spoken_warnings_is_configurable():
    assert "Roles overlap." in describe_report(sceptical_report(), 1)
    assert "Roles overlap." not in describe_report(sceptical_report(), 0)


def test_a_skill_search_names_the_best_candidate():
    text = describe_report(skill_report(), SPOKEN_FLAGS)
    assert text == (
        "Skill search for Rust in Brno. 1 candidates were assessed. "
        "The best match is Jan Novak with a rating of 3.4 out of 5."
    )


def test_an_empty_skill_search_is_reported():
    empty = skill_report().model_copy(update={"candidates": []})
    assert describe_report(empty, SPOKEN_FLAGS).endswith("No candidates could be assessed.")


def test_a_clarification_reads_every_option_and_how_to_answer():
    text = describe_clarification(clarification())
    assert text.startswith("Which one?")
    assert "Option 1: Jan <b>A</b>. Brno" in text
    assert "Option 2: Jan B. Prague" in text
    assert text.endswith("Say the option number, say none to skip, or say narrow to add details.")
