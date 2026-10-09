# tests/test_export_live.py
from agent.models import (
    CompanyProfile,
    PersonReport,
    Priority,
    Requirement,
    RequirementKind,
    RequirementResult,
    RequirementStatus,
    SkillSearchReport,
)
from tests.fakes import (
    company_profile,
    hiring_report,
    requirements_assessment,
    skill_report,
)
from agent.export import (
    render_json,
    render_markdown,
)
from agent.config import RequirementTuning
from agent.requirements import summarise

import json


def test_the_company_section_lists_each_fact_with_its_origin():
    text = render_markdown(hiring_report())
    assert "## Hiring company" in text
    assert "Acme - <https://acme.example>" in text
    assert "- category: Payments software - <https://acme.example>" in text
    assert "- provided: We build payment software for shops - provided by the requester" in text


def test_a_company_without_facts_says_so():
    report = hiring_report().model_copy(
        update={"company": CompanyProfile(name="Acme", website=None)}
    )
    text = render_markdown(report)
    assert "No public facts could be confirmed." in text
    assert "Acme\n" in text


def test_reports_without_a_company_or_requirements_have_neither_section():
    text = render_markdown(
        hiring_report().model_copy(update={"company": None, "requirements": None})
    )
    assert "## Hiring company" not in text and "## Requirements" not in text


def test_the_requirements_section_has_the_coverage_and_a_row_per_requirement():
    text = render_markdown(hiring_report())
    assert "## Requirements" in text
    assert "Coverage: **60%** (met 1, partial 1, unmet 0, unknown 1)." in text
    assert "| R1 | English (C1) | must | met |" in text
    assert "| R2 | Python 5+ years | must | partial |" in text
    assert "| R3 | Czech (B2) | nice | unknown |" in text
    assert "Must-have requirements without evidence" not in text


def test_missing_must_have_requirements_are_called_out():
    results = [
        RequirementResult(
            id="R1",
            requirement=Requirement(
                kind=RequirementKind.CERTIFICATION, label="AWS", priority=Priority.MUST
            ),
            status=RequirementStatus.UNKNOWN,
            justification="No finding mentions it.",
        )
    ]
    assessment = summarise(results, RequirementTuning())
    text = render_markdown(hiring_report().model_copy(update={"requirements": assessment}))
    assert "Must-have requirements without evidence: R1." in text


def test_each_skill_candidate_shows_its_requirement_coverage():
    report = skill_report()
    candidate = report.candidates[0].model_copy(update={"requirements": requirements_assessment()})
    report = report.model_copy(update={"candidates": [candidate], "company": company_profile()})
    text = render_markdown(report)
    assert "Requirement coverage: 60%" in text
    assert "## Hiring company" in text


def test_json_round_trips_the_company_and_requirements():
    report = hiring_report()
    data = json.loads(render_json(report))
    assert data["company"]["name"] == "Acme"
    assert data["requirements"]["coverage"] == 0.6
    assert PersonReport.model_validate_json(render_json(report)) == report
    shortlist = skill_report().model_copy(update={"company": company_profile()})
    assert SkillSearchReport.model_validate_json(render_json(shortlist)) == shortlist


def test_hostile_company_text_is_escaped_in_the_markdown():
    profile = CompanyProfile(name="[Acme](https://evil.example)|x", website=None)
    text = render_markdown(hiring_report().model_copy(update={"company": profile}))
    assert "[Acme](https://evil.example)" not in text
