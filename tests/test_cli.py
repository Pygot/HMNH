# tests/test_cli.py
from agent.models import (
    Clarification,
    ClarificationOption,
    ClarificationQuestion,
    Identity,
    Network,
)
from agent.cli import (
    app,
    EXIT_ERROR,
    EXIT_NEEDS_INPUT,
    EXIT_REFUSED,
)
from tests.fakes import (
    person_report,
    skill_report,
)
from agent.errors import ComplianceRefusal
from contextlib import asynccontextmanager
from agent.export import export_report
from tests.documents import make_pdf
from typer.testing import CliRunner
from agent import cli

import pytest

runner = CliRunner()
LI_A = "https://www.linkedin.com/in/jan-a"
LI_B = "https://www.linkedin.com/in/jan-b"
BASE = ["research", "--goal", "hiring", "--name", "Jan Novak"]


def clarification():
    options = [
        ClarificationOption(url=LI_A, title="Jan A", snippet="", score=0.6, evidence="name 100%"),
        ClarificationOption(url=LI_B, title="Jan B", snippet="", score=0.55, evidence="name 90%"),
    ]
    question = ClarificationQuestion(network=Network.LINKEDIN, text="Which one?", options=options)
    return Clarification(questions=[question])


class FakeRuntime:
    def __init__(self, pipeline, status):
        self.pipeline = pipeline
        self._status = status

    async def status(self):
        return self._status


class FakePipeline:
    def __init__(self, results=(), skill_result=None):
        self.unavailable_networks = [Network.LINKEDIN]
        self.results = list(results)
        self.skill_result = skill_result
        self.requests = []
        self.cv_calls = []

    async def research(self, request, progress):
        self.requests.append(request)
        progress("stage")
        return self.results.pop(0)

    async def skill_search(self, request, progress):
        self.requests.append(request)
        return self.skill_result

    async def identity_from_cv(self, data, filename):
        self.cv_calls.append((len(data), filename))
        return Identity(name="Jan Novak", links=["https://jan.dev"])


@pytest.fixture
def install(monkeypatch):
    def apply(pipeline, status=None):
        @asynccontextmanager
        async def fake_open(settings):
            yield FakeRuntime(pipeline, status)

        monkeypatch.setattr(cli, "open_runtime", fake_open)
        return pipeline

    return apply


def test_help_lists_the_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("research", "skill-search", "delete-outputs", "serve"):
        assert command in result.output
    for variable in ("LLM_API_KEY", "LLM_OLLAMA_PORT", "BRAVE_API_KEY", "WEB_ACCESS_TOKEN"):
        assert variable in result.output


def test_non_interactive_run_without_confirmation_is_refused(install):
    pipeline = install(FakePipeline())
    result = runner.invoke(app, [*BASE, "--no-interactive"])
    assert result.exit_code == EXIT_REFUSED
    assert "--confirm-lawful-purpose" in result.output
    assert "GDPR" in result.output
    assert pipeline.requests == []


def test_declining_the_prompt_is_a_refusal(install):
    install(FakePipeline([person_report()]))
    result = runner.invoke(app, BASE, input="n\n")
    assert result.exit_code == EXIT_REFUSED
    assert "lawful purpose" in result.output


def test_research_exports_and_summarises(install, tmp_path):
    pipeline = install(FakePipeline([person_report()]))
    args = [*BASE, "--city", "Brno", "--employer", "Acme", "--skill", "python"]
    args += ["--confirm-lawful-purpose", "--output-dir", str(tmp_path), "--focus", "Backend"]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "Overall rating: 3.4 / 5" in result.output
    assert "single-source 1" in result.output
    assert "warning: social profiles will not be read for linkedin" in result.output
    files = sorted(path.suffix for path in tmp_path.iterdir())
    assert files == [".json", ".md"]
    request = pipeline.requests[0]
    assert request.purpose_confirmed is True
    assert request.focus == "Backend"
    assert request.identity.employers == ["Acme"]


def test_interactive_confirmation_is_accepted(install, tmp_path):
    install(FakePipeline([person_report()]))
    result = runner.invoke(app, [*BASE, "--output-dir", str(tmp_path)], input="y\n")
    assert result.exit_code == 0, result.output


def test_name_and_cv_are_mutually_exclusive(tmp_path):
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(make_pdf(["Jan Novak"]))
    both = runner.invoke(app, [*BASE, "--cv", str(cv), "--confirm-lawful-purpose"])
    assert both.exit_code == 2
    assert "either --name or --cv" in both.output
    neither = runner.invoke(app, ["research", "--goal", "hiring", "--confirm-lawful-purpose"])
    assert neither.exit_code == 2
    with_options = runner.invoke(
        app, ["research", "--goal", "hiring", "--cv", str(cv), "--city", "Brno"]
    )
    assert with_options.exit_code == 2


def test_cv_input_extracts_the_identity(install, tmp_path):
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(make_pdf(["Jan Novak"]))
    pipeline = install(FakePipeline([person_report()]))
    args = ["research", "--goal", "sales", "--cv", str(cv), "--confirm-lawful-purpose"]
    result = runner.invoke(app, [*args, "--output-dir", str(tmp_path / "out")])
    assert result.exit_code == 0, result.output
    assert pipeline.cv_calls == [(cv.stat().st_size, "cv.pdf")]
    assert pipeline.requests[0].identity.links == ["https://jan.dev"]
    assert "CV identity" in result.output


def test_non_interactive_ambiguity_exits_with_the_question(install, tmp_path):
    install(FakePipeline([clarification()]))
    args = [*BASE, "--confirm-lawful-purpose", "--no-interactive", "--output-dir", str(tmp_path)]
    result = runner.invoke(app, args)
    assert result.exit_code == EXIT_NEEDS_INPUT
    assert "clarification needed (linkedin)" in result.output
    assert LI_A in result.output and LI_B in result.output
    assert not tmp_path.exists() or not list(tmp_path.iterdir())


def test_interactive_choice_adds_the_profile_and_reruns(install, tmp_path):
    pipeline = install(FakePipeline([clarification(), person_report()]))
    args = [*BASE, "--confirm-lawful-purpose", "--output-dir", str(tmp_path)]
    result = runner.invoke(app, args, input="2\n")
    assert result.exit_code == 0, result.output
    assert LI_B in pipeline.requests[1].identity.links
    assert LI_A not in pipeline.requests[1].identity.links


def test_interactive_rejection_skips_the_network(install, tmp_path):
    pipeline = install(FakePipeline([clarification(), person_report()]))
    args = [*BASE, "--confirm-lawful-purpose", "--output-dir", str(tmp_path)]
    result = runner.invoke(app, args, input="0\n")
    assert result.exit_code == 0, result.output
    assert pipeline.requests[1].skipped_networks == [Network.LINKEDIN]


def test_interactive_details_narrow_the_identity_and_invalid_input_reprompts(install, tmp_path):
    pipeline = install(FakePipeline([clarification(), person_report()]))
    args = [*BASE, "--confirm-lawful-purpose", "--output-dir", str(tmp_path)]
    scripted = "9\nd\n\n\nd\nMain Street 5\nAcme\nd\nBrno\nAcme\n"
    result = runner.invoke(app, args, input=scripted)
    assert result.exit_code == 0, result.output
    assert "Please enter a listed number" in result.output
    assert "invalid input" in result.output
    identity = pipeline.requests[1].identity
    assert identity.location == "Brno"
    assert identity.employers == ["Acme"]


def test_skill_search_prints_the_ranking(install, tmp_path):
    install(FakePipeline(skill_result=skill_report()))
    args = ["skill-search", "--goal", "hiring", "--skill", "Rust", "--location", "Brno"]
    args += ["--confirm-lawful-purpose", "--output-dir", str(tmp_path), "--limit", "2"]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "1. Jan Novak  3.4 / 5  https://github.com/jan  <- best" in result.output
    assert len(list(tmp_path.iterdir())) == 2


def test_skill_search_requires_confirmation_and_valid_limit(install):
    install(FakePipeline(skill_result=skill_report()))
    base = ["skill-search", "--goal", "hiring", "--skill", "Rust", "--location", "Brno"]
    refused = runner.invoke(app, [*base, "--no-interactive"])
    assert refused.exit_code == EXIT_REFUSED
    assert runner.invoke(app, [*base, "--limit", "99"]).exit_code == 2


def test_compliance_refusals_from_the_pipeline_exit_with_the_refusal_code(monkeypatch):
    class Refusing:
        async def __aenter__(self):
            raise ComplianceRefusal("Refused: nope")

        async def __aexit__(self, *exc_info):
            return None

    monkeypatch.setattr(cli, "open_runtime", lambda settings: Refusing())
    result = runner.invoke(app, [*BASE, "--confirm-lawful-purpose"])
    assert result.exit_code == EXIT_REFUSED
    assert "Refused: nope" in result.output


def test_configuration_errors_fail_loudly_with_a_clear_message():
    result = runner.invoke(app, [*BASE, "--confirm-lawful-purpose"])
    assert result.exit_code == EXIT_ERROR
    assert "No LLM configured" in result.output


def test_invalid_identity_values_are_explained():
    result = runner.invoke(app, [*BASE, "--city", "Main Street 5", "--confirm-lawful-purpose"])
    assert result.exit_code == EXIT_ERROR
    assert "location" in result.output


def test_delete_outputs_removes_reports_after_confirmation(tmp_path):
    export_report(person_report(), tmp_path)
    declined = runner.invoke(app, ["delete-outputs", "--output-dir", str(tmp_path)], input="n\n")
    assert declined.exit_code == 1
    assert len(list(tmp_path.iterdir())) == 2
    accepted = runner.invoke(app, ["delete-outputs", "--output-dir", str(tmp_path)], input="y\n")
    assert accepted.exit_code == 0
    assert "Deleted 2 file(s)" in accepted.output
    assert list(tmp_path.iterdir()) == []


def test_delete_outputs_with_yes_and_default_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "configured"))
    export_report(person_report(), tmp_path / "configured")
    result = runner.invoke(app, ["delete-outputs", "--yes"])
    assert result.exit_code == 0
    assert "Deleted 2 file(s)" in result.output
