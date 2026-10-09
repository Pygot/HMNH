# tests/test_requirements.py
from agent.models import (
    Category,
    Finding,
    Priority,
    Requirement,
    RequirementKind,
    RequirementResult,
    RequirementStatus,
    Tag,
)
from agent.requirements import (
    assess_requirements,
    requirement_ids,
    summarise,
)
from agent.errors import (
    ComplianceRefusal,
    LLMOutputError,
)
from agent.compliance import check_requirements
from agent.config import RequirementTuning
from tests.fakes import RuleLLM

import pytest

MARKER = "judge whether one person satisfies"
TUNING = RequirementTuning()
ENGLISH = Requirement(kind=RequirementKind.LANGUAGE, label="English", level="C1")
PYTHON = Requirement(kind=RequirementKind.SKILL, label="Python", minimum_years=5)
CZECH = Requirement(
    kind=RequirementKind.LANGUAGE, label="Czech", level="B2", priority=Priority.NICE
)
FINDINGS = [
    Finding(
        id="F1",
        statement="Lists English as full professional proficiency",
        category=Category.EDUCATION,
        tag=Tag.SUPPORTED,
        source_urls=["https://a.example/p", "https://b.example/p"],
        independent_sources=2,
    ),
    Finding(
        id="F2",
        statement="Python developer since 2023",
        category=Category.EMPLOYMENT,
        tag=Tag.SINGLE_SOURCE,
        source_urls=["https://b.example/p"],
        independent_sources=1,
        year=2023,
    ),
]


def verdict(identifier, status, ids=(), text="Because."):
    return {"id": identifier, "status": status, "justification": text, "finding_ids": list(ids)}


def result(requirement, status):
    return RequirementResult(
        id="R1", requirement=requirement, status=status, justification="reason"
    )


def test_identifiers_are_numbered_from_one():
    assert requirement_ids([ENGLISH, PYTHON, CZECH]) == ["R1", "R2", "R3"]
    assert requirement_ids([]) == []


def test_met_requirements_give_full_coverage():
    summary = summarise(
        [result(ENGLISH, RequirementStatus.MET), result(CZECH, RequirementStatus.MET)], TUNING
    )
    assert summary.coverage == 1.0 and summary.met == 2
    assert summary.must_missing == []


def test_must_requirements_weigh_more_than_nice_ones():
    summary = summarise(
        [result(ENGLISH, RequirementStatus.MET), result(CZECH, RequirementStatus.UNMET)], TUNING
    )
    assert summary.coverage == pytest.approx(0.667, abs=0.001)
    assert (summary.met, summary.unmet) == (1, 1)


def test_partial_credit_comes_from_the_configuration():
    results = [result(ENGLISH, RequirementStatus.PARTIAL)]
    assert summarise(results, TUNING).coverage == 0.5
    assert summarise(results, RequirementTuning(partial_credit=0.25)).coverage == 0.25
    heavy = RequirementTuning(must_weight=5, nice_weight=1)
    mixed = [result(ENGLISH, RequirementStatus.UNMET), result(CZECH, RequirementStatus.MET)]
    assert summarise(mixed, heavy).coverage == pytest.approx(0.167, abs=0.001)


def test_missing_must_requirements_are_listed_by_identifier():
    results = [
        RequirementResult(
            id="R1", requirement=ENGLISH, status=RequirementStatus.UNMET, justification="x"
        ),
        RequirementResult(
            id="R2", requirement=PYTHON, status=RequirementStatus.UNKNOWN, justification="x"
        ),
        RequirementResult(
            id="R3", requirement=CZECH, status=RequirementStatus.UNKNOWN, justification="x"
        ),
        RequirementResult(
            id="R4", requirement=PYTHON, status=RequirementStatus.PARTIAL, justification="x"
        ),
    ]
    summary = summarise(results, TUNING)
    assert summary.must_missing == ["R1", "R2"]
    assert (summary.unmet, summary.unknown, summary.partial) == (1, 2, 1)


def test_an_empty_list_has_zero_coverage():
    summary = summarise([], TUNING)
    assert summary.coverage == 0.0 and summary.results == []


async def test_without_findings_everything_is_unknown_and_the_model_is_not_asked():
    llm = RuleLLM({})
    summary = await assess_requirements(llm, [ENGLISH, CZECH], [], TUNING)
    assert llm.calls == []
    assert [item.status for item in summary.results] == [RequirementStatus.UNKNOWN] * 2
    assert summary.must_missing == ["R1"] and summary.coverage == 0.0
    assert "No findings" in summary.results[0].justification


async def test_verdicts_are_matched_to_requirements_and_their_sources():
    llm = RuleLLM(
        {
            MARKER: lambda user: {
                "results": [
                    verdict("R1", "met", ["F1"], "English is listed."),
                    verdict("R2", "partial", ["F2"], "About three years."),
                    verdict("R3", "unknown"),
                ]
            }
        }
    )
    summary = await assess_requirements(llm, [ENGLISH, PYTHON, CZECH], FINDINGS, TUNING)
    first, second, third = summary.results
    assert first.status is RequirementStatus.MET and first.finding_ids == ["F1"]
    assert first.source_urls == ["https://a.example/p", "https://b.example/p"]
    assert second.status is RequirementStatus.PARTIAL
    assert second.source_urls == ["https://b.example/p"]
    assert third.status is RequirementStatus.UNKNOWN and third.source_urls == []
    assert summary.coverage == pytest.approx(0.6, abs=0.001)
    assert summary.must_missing == []


async def test_the_prompt_lists_requirements_findings_and_company_text():
    llm = RuleLLM({MARKER: lambda user: {"results": [verdict("R1", "unknown")]}})
    await assess_requirements(llm, [ENGLISH], FINDINGS, TUNING, "Acme (Payments software)")
    user = llm.calls[0][1]
    assert "R1 [language, must] English (C1)" in user
    assert "F2 [employment] (2023) Python developer since 2023" in user
    assert "F1 [education] (undated)" in user
    assert "Hiring company (untrusted text): Acme (Payments software)" in user


async def test_the_company_line_is_left_out_when_there_is_no_company():
    llm = RuleLLM({MARKER: lambda user: {"results": [verdict("R1", "unknown")]}})
    await assess_requirements(llm, [ENGLISH], FINDINGS, TUNING)
    assert "Hiring company" not in llm.calls[0][1]


async def test_justifications_are_cleaned_and_clipped():
    text = "word   " * 100
    llm = RuleLLM({MARKER: lambda user: {"results": [verdict("R1", "met", ["F1"], text)]}})
    summary = await assess_requirements(llm, [ENGLISH], FINDINGS, TUNING)
    assert len(summary.results[0].justification) <= TUNING.max_justification_chars
    assert "  " not in summary.results[0].justification


async def test_an_empty_justification_gets_a_placeholder():
    llm = RuleLLM({MARKER: lambda user: {"results": [verdict("R1", "unknown", text="  ")]}})
    summary = await assess_requirements(llm, [ENGLISH], FINDINGS, TUNING)
    assert summary.results[0].justification == "No justification was given."


async def test_verdicts_without_evidence_are_rejected_and_retried():
    answers = iter(
        [
            {"results": [verdict("R1", "met")]},
            {"results": [verdict("R1", "met", ["F1"])]},
        ]
    )
    llm = RuleLLM({MARKER: lambda user: next(answers)})
    summary = await assess_requirements(llm, [ENGLISH], FINDINGS, TUNING)
    assert summary.results[0].status is RequirementStatus.MET
    assert len(llm.calls) == 2
    assert "cites no finding" in llm.calls[1][1]


@pytest.mark.parametrize(
    ("results", "message"),
    [
        ([verdict("R9", "unknown")], "unknown requirement id"),
        ([verdict("R1", "unknown"), verdict("R1", "unknown")], "answered twice"),
        ([verdict("R1", "met", ["F9"])], "unknown finding ids"),
        ([], "missing"),
    ],
)
async def test_invalid_verdict_sets_fail_after_the_retries(results, message):
    llm = RuleLLM({MARKER: lambda user: {"results": results}})
    with pytest.raises(LLMOutputError, match=message):
        await assess_requirements(llm, [ENGLISH], FINDINGS, TUNING)
    assert len(llm.calls) == 2


async def test_every_requirement_must_be_answered():
    llm = RuleLLM({MARKER: lambda user: {"results": [verdict("R1", "unknown")]}})
    with pytest.raises(LLMOutputError, match="R2"):
        await assess_requirements(llm, [ENGLISH, PYTHON], FINDINGS, TUNING)


def test_job_related_requirements_are_accepted():
    check_requirements([ENGLISH, PYTHON, CZECH])
    check_requirements(
        [
            Requirement(kind=RequirementKind.EDUCATION, label="Master degree in informatics"),
            Requirement(kind=RequirementKind.CERTIFICATION, label="AWS Solutions Architect"),
            Requirement(kind=RequirementKind.INDUSTRY, label="Fintech", minimum_years=2),
            Requirement(kind=RequirementKind.LOCATION, label="Brno"),
            Requirement(kind=RequirementKind.CUSTOM, label="Open source maintainer"),
        ]
    )


@pytest.mark.parametrize(
    "label",
    [
        "Native speaker of English",
        "Mother tongue Czech",
        "Under 30 years old",
        "Young team player",
        "Male candidate",
        "Married with kids",
        "Czech citizenship",
        "Belongs to a church",
        "Healthy, no medical history",
        "Voted for a party",
    ],
)
def test_protected_or_biased_requirements_are_refused(label):
    with pytest.raises(ComplianceRefusal, match="protected or sensitive"):
        check_requirements([ENGLISH, Requirement(kind=RequirementKind.CUSTOM, label=label)])


def test_the_level_is_checked_too():
    with pytest.raises(ComplianceRefusal):
        check_requirements(
            [Requirement(kind=RequirementKind.LANGUAGE, label="Czech", level="native speaker")]
        )


@pytest.mark.parametrize(
    "label",
    [
        "Message queue experience",
        "Management of engineering teams",
        "Image processing",
        "Sage accounting software",
        "Language: C1 English",
    ],
)
def test_ordinary_words_that_contain_biased_fragments_are_not_refused(label):
    check_requirements([Requirement(kind=RequirementKind.CUSTOM, label=label)])
