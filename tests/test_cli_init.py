# tests/test_cli_init.py
from agent.config import ENV_FILE_VARIABLE
from typer.testing import CliRunner
from agent.cli import app

SECRET = "sk-ant-typed-in-the-wizard"


def run(input_text, monkeypatch, tmp_path):
    path = tmp_path / "wizard.env"
    monkeypatch.setenv(ENV_FILE_VARIABLE, str(path))
    result = CliRunner().invoke(app, ["init"], input=input_text)
    return result, path


def test_the_wizard_saves_what_was_typed_and_never_prints_a_secret(monkeypatch, tmp_path):
    answers = ["y", "", SECRET, "", "", "", "", ""] + ["n"] * 5
    result, path = run("\n".join(answers) + "\n", monkeypatch, tmp_path)
    assert result.exit_code == 0, result.output
    assert f"LLM_API_KEY={SECRET}" in path.read_text(encoding="utf-8")
    assert SECRET not in result.output
    assert "Saved 1 settings" in result.output and "agent serve" in result.output


def test_skipping_every_section_saves_nothing(monkeypatch, tmp_path):
    result, path = run("n\n" * 6, monkeypatch, tmp_path)
    assert result.exit_code == 0 and "Nothing to save" in result.output
    assert not path.exists()


def test_the_wizard_stops_on_values_that_cannot_work_together(monkeypatch, tmp_path):
    answers = ["y", "", "sk-key", "", "", "", "localhost", ""] + ["n"] * 5
    result, path = run("\n".join(answers) + "\n", monkeypatch, tmp_path)
    assert result.exit_code == 2 and "Both" in result.output
    assert not path.exists()


def test_a_secret_already_saved_is_kept_when_the_answer_is_empty(monkeypatch, tmp_path):
    path = tmp_path / "wizard.env"
    path.write_text("APIFY_TOKEN=keep-me\n", encoding="utf-8")
    monkeypatch.setenv(ENV_FILE_VARIABLE, str(path))
    answers = ["n", "y", "", "n", "n", "n", "n"]
    result = CliRunner().invoke(app, ["init"], input="\n".join(answers) + "\n")
    assert result.exit_code == 0 and "Nothing to save" in result.output
    assert path.read_text(encoding="utf-8") == "APIFY_TOKEN=keep-me\n"
