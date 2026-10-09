# tests/test_cli_options.py
from agent.models import (
    Network,
    Scepticism,
    ScepticismLevel,
    ScepticismMode,
    Strictness,
)
from tests.fakes import (
    person_report,
    sceptical_report,
    skill_report,
)
from tests.test_cli import (
    BASE,
    FakePipeline,
    FakeRuntime,
)
from agent.cli import (
    app,
    EXIT_ERROR,
)
from contextlib import asynccontextmanager
from agent.runtime import ServiceStatus
from typer.testing import CliRunner
from agent import cli

import pytest

runner = CliRunner()
CONFIRM = "--confirm-lawful-purpose"


@pytest.fixture
def install(monkeypatch):
    def apply(pipeline, status=None):
        @asynccontextmanager
        async def fake_open(settings):
            yield FakeRuntime(pipeline, status)

        monkeypatch.setattr(cli, "open_runtime", fake_open)
        return pipeline

    return apply


def test_research_options_reach_the_request(install, tmp_path):
    pipeline = install(FakePipeline([person_report()]))
    args = [*BASE, CONFIRM, "--output-dir", str(tmp_path)]
    args += ["--source", "linkedin", "--source", "web", "--strictness", "strict"]
    args += ["--threshold", "0.75", "--margin", "0.2", "--max-web-pages", "3"]
    args += ["--scepticism", "strict"]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    options = pipeline.requests[0].options
    assert options.networks == [Network.LINKEDIN, Network.WEB]
    assert options.strictness is Strictness.STRICT
    assert (options.threshold, options.margin, options.max_web_pages) == (0.75, 0.2, 3)
    assert options.scepticism is ScepticismMode.STRICT


def test_default_options_use_every_source_and_standard_scepticism(install, tmp_path):
    pipeline = install(FakePipeline([person_report()]))
    result = runner.invoke(app, [*BASE, CONFIRM, "--output-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    options = pipeline.requests[0].options
    assert options.networks == list(Network)
    assert options.scepticism is ScepticismMode.STANDARD
    assert options.strictness is None and options.threshold is None


@pytest.mark.parametrize(
    "extra",
    [
        ["--threshold", "1.5"],
        ["--margin", "-0.1"],
        ["--max-web-pages", "99"],
        ["--strictness", "extreme"],
        ["--scepticism", "off"],
        ["--source", "myspace"],
    ],
)
def test_invalid_option_values_are_rejected_before_anything_runs(install, extra):
    pipeline = install(FakePipeline([person_report()]))
    result = runner.invoke(app, [*BASE, CONFIRM, *extra])
    assert result.exit_code == 2
    assert pipeline.requests == []


def test_the_scepticism_outcome_is_printed(install, tmp_path):
    install(FakePipeline([sceptical_report()]))
    result = runner.invoke(app, [*BASE, CONFIRM, "--output-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Scepticism: high, rating 4.4 reduced to 2.5" in result.output
    assert "  - Roles overlap." in result.output


def test_a_clean_run_prints_no_scepticism_line(install, tmp_path):
    install(FakePipeline([person_report()]))
    result = runner.invoke(app, [*BASE, CONFIRM, "--output-dir", str(tmp_path)])
    assert "Scepticism:" not in result.output


def test_skill_search_options_reach_the_request(install, tmp_path):
    pipeline = install(FakePipeline(skill_result=skill_report()))
    args = ["skill-search", "--goal", "hiring", "--skill", "Rust", "--location", "Brno"]
    args += [CONFIRM, "--output-dir", str(tmp_path), "--source", "web", "--scepticism", "strict"]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    options = pipeline.requests[0].options
    assert options.networks == [Network.WEB]
    assert options.scepticism is ScepticismMode.STRICT


def test_skill_search_marks_candidates_that_need_caution(install, tmp_path):
    report = skill_report()
    candidate = report.candidates[0]
    caution = Scepticism(mode=ScepticismMode.STANDARD, level=ScepticismLevel.MEDIUM)
    rating = candidate.rating.model_copy(update={"scepticism": caution})
    flagged = report.model_copy(
        update={"candidates": [candidate.model_copy(update={"rating": rating})]}
    )
    install(FakePipeline(skill_result=flagged))
    args = ["skill-search", "--goal", "hiring", "--skill", "Rust", "--location", "Brno"]
    result = runner.invoke(app, [*args, CONFIRM, "--output-dir", str(tmp_path)])
    assert "scepticism medium" in result.output


def status_report(**overrides):
    base = {
        "llm_provider": "anthropic",
        "llm_model": "claude-sonnet-5-5",
        "search_provider": "apify",
        "unavailable_networks": ["facebook"],
        "apify_monthly_usage_usd": 1.5,
        "apify_remaining_usd": 103.5,
    }
    return ServiceStatus(**{**base, **overrides})


def test_status_reports_providers_and_apify_credit(install):
    install(FakePipeline(), status_report())
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "LLM:     anthropic (claude-sonnet-5-5)" in result.output
    assert "Search:  apify" in result.output
    assert "Social profiles not readable: facebook" in result.output
    assert "1.50 USD used this month, 103.50 USD remaining" in result.output


def test_status_shows_apify_problems_and_missing_models(install):
    install(
        FakePipeline(),
        status_report(
            llm_model=None,
            unavailable_networks=[],
            apify_remaining_usd=None,
            apify_monthly_usage_usd=None,
            apify_error="Apify account rejected the credentials (HTTP 401).",
        ),
    )
    result = runner.invoke(app, ["status"])
    assert "(auto)" in result.output
    assert "not readable: none" in result.output
    assert "Apify:   Apify account rejected the credentials (HTTP 401)." in result.output


def test_status_without_apify_omits_the_credit_line(install):
    install(
        FakePipeline(),
        status_report(apify_remaining_usd=None, apify_monthly_usage_usd=None),
    )
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "Apify:" not in result.output


def test_status_fails_loudly_without_configuration():
    result = runner.invoke(app, ["status"])
    assert result.exit_code == EXIT_ERROR
    assert "No LLM configured" in result.output


def test_status_reports_whether_voice_is_enabled(install):
    install(FakePipeline(), status_report(voice_enabled=True))
    assert "Voice:   enabled" in runner.invoke(app, ["status"]).output
    install(FakePipeline(), status_report())
    assert "Voice:   disabled" in runner.invoke(app, ["status"]).output


def test_a_readable_env_file_is_reported_before_anything_else(monkeypatch):
    message = "private.env can be read by other users; run: chmod 600 private.env"
    monkeypatch.setattr(cli, "env_warnings", lambda path: [message])
    result = runner.invoke(app, ["status"])
    assert f"warning: {message}" in result.output
