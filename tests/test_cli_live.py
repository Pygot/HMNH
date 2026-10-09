# tests/test_cli_live.py
from tests.fakes import (
    hiring_report,
    requirements_assessment,
    skill_report,
)
from tests.test_cli import (
    BASE,
    FakePipeline,
    FakeRuntime,
)
from agent.models import (
    Priority,
    RequirementKind,
)
from contextlib import asynccontextmanager
from typer.testing import CliRunner
from agent.cli import app
from agent import cli

import pytest

runner = CliRunner()
CONFIRM = "--confirm-lawful-purpose"
SKILL = ["skill-search", "--goal", "hiring", "--skill", "Rust", "--location", "Brno"]


@pytest.fixture
def install(monkeypatch):
    def apply(pipeline):
        @asynccontextmanager
        async def fake_open(settings):
            yield FakeRuntime(pipeline, None)

        monkeypatch.setattr(cli, "open_runtime", fake_open)
        return pipeline

    return apply


def test_the_company_and_requirements_reach_the_research_request(install, tmp_path):
    pipeline = install(FakePipeline([hiring_report()]))
    args = [*BASE, CONFIRM, "--output-dir", str(tmp_path)]
    args += ["--company", "Acme", "--company-website", "https://acme.example"]
    args += ["--company-about", "We build payment software for shops"]
    args += ["--require", "language:English:C1", "--require", "skill:Python::5:nice"]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    request = pipeline.requests[0]
    assert request.company.name == "Acme" and request.company.website == "https://acme.example"
    assert request.company.description == "We build payment software for shops"
    english, python = request.requirements
    assert (english.kind, english.label, english.level) == (
        RequirementKind.LANGUAGE,
        "English",
        "C1",
    )
    assert english.minimum_years is None and english.priority is Priority.MUST
    assert (python.kind, python.label, python.minimum_years) == (RequirementKind.SKILL, "Python", 5)
    assert python.level is None and python.priority is Priority.NICE


def test_the_requirement_outcome_is_printed(install, tmp_path):
    install(FakePipeline([hiring_report()]))
    result = runner.invoke(app, [*BASE, CONFIRM, "--output-dir", str(tmp_path)])
    assert "Requirements: 60% covered (met 1, partial 1, unmet 0, unknown 1)" in result.output
    assert "Must have without evidence" not in result.output


def test_missing_must_have_requirements_are_printed(install, tmp_path):
    assessment = requirements_assessment().model_copy(update={"must_missing": ["R2"]})
    install(FakePipeline([hiring_report().model_copy(update={"requirements": assessment})]))
    result = runner.invoke(app, [*BASE, CONFIRM, "--output-dir", str(tmp_path)])
    assert "Must have without evidence: R2" in result.output


def test_without_requirements_nothing_about_them_is_printed(install, tmp_path):
    pipeline = install(FakePipeline([hiring_report().model_copy(update={"requirements": None})]))
    result = runner.invoke(app, [*BASE, CONFIRM, "--output-dir", str(tmp_path)])
    assert "Requirements:" not in result.output
    request = pipeline.requests[0]
    assert request.company is None and request.requirements == []


@pytest.mark.parametrize(
    "spec",
    [
        "English",
        "language:",
        ":English",
        "hobby:Chess",
        "skill:Python:::vital",
        "skill:Python::many",
        "skill:Python::99",
    ],
)
def test_invalid_requirements_are_rejected_before_anything_runs(install, spec, tmp_path):
    pipeline = install(FakePipeline([hiring_report()]))
    result = runner.invoke(app, [*BASE, CONFIRM, "--output-dir", str(tmp_path), "--require", spec])
    assert result.exit_code != 0
    assert pipeline.requests == []


def test_biased_requirements_are_refused_with_the_compliance_exit_code(install, tmp_path):
    class Refusing(FakePipeline):
        async def research(self, request, progress):
            from agent.compliance import check_research_request

            check_research_request(request)
            return await super().research(request, progress)

    pipeline = install(Refusing([hiring_report()]))
    args = [*BASE, CONFIRM, "--output-dir", str(tmp_path), "--require", "custom:Native speaker"]
    result = runner.invoke(app, args)
    assert result.exit_code == 3
    assert "protected or sensitive" in result.output
    assert pipeline.requests == []


def test_company_details_need_a_company_name(install, tmp_path):
    pipeline = install(FakePipeline([hiring_report()]))
    for extra in (["--company-website", "https://acme.example"], ["--company-about", "x"]):
        result = runner.invoke(app, [*BASE, CONFIRM, "--output-dir", str(tmp_path), *extra])
        assert result.exit_code != 0 and "need --company" in result.output
    assert pipeline.requests == []


def test_an_invalid_company_website_is_an_error(install, tmp_path):
    pipeline = install(FakePipeline([hiring_report()]))
    args = [*BASE, CONFIRM, "--output-dir", str(tmp_path), "--company", "Acme"]
    result = runner.invoke(app, [*args, "--company-website", "ftp://acme.example"])
    assert result.exit_code == 2 and pipeline.requests == []


def test_skill_search_takes_the_company_and_requirements(install, tmp_path):
    candidate = (
        skill_report().candidates[0].model_copy(update={"requirements": requirements_assessment()})
    )
    report = skill_report().model_copy(update={"candidates": [candidate]})
    pipeline = install(FakePipeline(skill_result=report))
    args = [*SKILL, CONFIRM, "--output-dir", str(tmp_path), "--company", "Acme"]
    args += ["--require", "language:English:C1"]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    request = pipeline.requests[0]
    assert request.company.name == "Acme"
    assert [item.label for item in request.requirements] == ["English"]
    assert "   Requirements: 60% covered" in result.output


def test_the_help_explains_the_requirement_format(install):
    result = runner.invoke(app, ["research", "--help"])
    assert "--require" in result.output and "--company-about" in result.output
    assert "Candidate" in result.output
