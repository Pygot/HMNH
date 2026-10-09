# tests/test_models.py
from agent.models import (
    Criterion,
    CriterionRating,
    Identity,
    Network,
    Rating,
    ResearchOptions,
    Scepticism,
    ScepticismLevel,
    ScepticismMode,
    SearchHit,
    Strictness,
)
from agent.text import (
    clean_text,
    describe_errors,
)
from pydantic import ValidationError

import pytest


def test_clean_text_collapses_whitespace_and_rejects_hidden_characters():
    assert clean_text("  Jan \n\t Novak ") == "Jan Novak"
    for bad in ("", "   ", "a\x00b", "a\u202eb", "a\u200bb", "x" * 121):
        with pytest.raises(ValueError):
            clean_text(bad)
    assert clean_text("x" * 300, 300) == "x" * 300


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "ftp://example.com/a",
        "https://",
        "https://exa mple.com",
        "https://example.com/\x01",
        "https://example.com/" + "a" * 600,
        "//example.com",
    ],
)
def test_urls_must_be_plain_http_urls(url):
    with pytest.raises(ValidationError):
        SearchHit(url=url, title="t", snippet="s")


def test_valid_urls_are_kept_verbatim_apart_from_edge_whitespace():
    assert SearchHit(url=" https://example.com/a?b=1 ", title="t", snippet="s").url == (
        "https://example.com/a?b=1"
    )


def test_identity_drops_blank_list_entries_and_rejects_non_lists():
    identity = Identity(name="Jan Novak", employers=["Acme", "  ", ""], skills=[])
    assert identity.employers == ["Acme"]
    with pytest.raises(ValidationError, match="must be a list"):
        Identity(name="Jan Novak", skills="python")


def test_identity_list_sizes_and_unknown_fields_are_limited():
    with pytest.raises(ValidationError):
        Identity(name="Jan Novak", skills=[f"skill{n}" for n in range(21)])
    with pytest.raises(ValidationError):
        Identity.model_validate({"name": "Jan Novak", "ssn": "123"})


def test_names_are_cleaned_inside_models():
    assert Identity(name="  Jan   Novak ").name == "Jan Novak"


def rating_for(criteria):
    return Rating(
        target="t",
        criteria=criteria,
        raw_overall=1.0,
        overall=1.0,
        scepticism=Scepticism(mode=ScepticismMode.STANDARD, level=ScepticismLevel.NONE),
    )


def test_a_rating_needs_exactly_one_entry_per_criterion():
    full = [
        CriterionRating(criterion=criterion, score=1, justification="ok") for criterion in Criterion
    ]
    assert rating_for(full).score_of(Criterion.RECENCY) == 1
    with pytest.raises(ValidationError, match="exactly one rating per criterion"):
        rating_for(full[:3])
    with pytest.raises(ValidationError, match="exactly one rating per criterion"):
        rating_for([*full[:3], full[0]])


def test_criterion_scores_are_bounded_and_justifications_single_line():
    with pytest.raises(ValidationError):
        CriterionRating(criterion=Criterion.RECENCY, score=6, justification="x")
    with pytest.raises(ValidationError):
        CriterionRating(criterion=Criterion.RECENCY, score=-1, justification="x")
    rating = CriterionRating(criterion=Criterion.RECENCY, score=1, justification="a\nb")
    assert rating.justification == "a b"


def test_describe_errors_names_the_field_and_hides_the_input():
    with pytest.raises(ValidationError) as info:
        Identity(name="Jan Novak", location="Main Street 5")
    message = describe_errors(info.value)
    assert message.startswith("location:")
    assert "Main Street" not in message


def test_research_options_default_to_every_source_and_standard_scepticism():
    options = ResearchOptions()
    assert options.networks == list(Network)
    assert options.scepticism is ScepticismMode.STANDARD
    assert options.threshold is None and options.strictness is None


def test_research_options_are_validated():
    with pytest.raises(ValidationError, match="at least one source"):
        ResearchOptions(networks=[])
    with pytest.raises(ValidationError):
        ResearchOptions(threshold=1.5)
    with pytest.raises(ValidationError):
        ResearchOptions(max_web_pages=99)
    with pytest.raises(ValidationError):
        ResearchOptions.model_validate({"unknown": 1})
    deduplicated = ResearchOptions(networks=[Network.WEB, Network.WEB, Network.LINKEDIN])
    assert deduplicated.networks == [Network.WEB, Network.LINKEDIN]
    assert ResearchOptions(strictness=Strictness.STRICT).strictness is Strictness.STRICT
