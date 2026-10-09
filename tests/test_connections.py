# tests/test_connections.py
from agent.connections import (
    ConnectionChecker,
    Connections,
    EDITABLE,
    SECRET_KEYS,
    SECTIONS,
)
from agent.envfile import (
    env_target,
    load_settings,
    overridden,
    write_values,
)
from agent.config import (
    ENV_FILE_VARIABLE,
    MAX_ENV_FILE_BYTES,
    Settings,
)
from tests.helpers import (
    json_response,
    mock_client,
)
from agent.errors import ConfigError
from pathlib import Path

import pytest
import os

KEYS = frozenset({"LLM_API_KEY", "SMTP_PASSWORD", "SMTP_HOST", "APIFY_TOKEN", "AGENT_CONTACT"})


@pytest.fixture
def target(tmp_path, monkeypatch):
    path = tmp_path / "settings.env"
    monkeypatch.setenv(ENV_FILE_VARIABLE, str(path))
    return path


def test_writing_creates_a_private_file_and_keeps_other_lines(tmp_path):
    path = tmp_path / "a.env"
    path.write_text(
        "# my notes\nAPIFY_TOKEN=old\nOTHER=1\nAPIFY_TOKEN=duplicate\n", encoding="utf-8"
    )
    write_values(path, {"APIFY_TOKEN": "new-token", "SMTP_HOST": "mail.example.com"}, KEYS)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines == ["# my notes", "APIFY_TOKEN=new-token", "OTHER=1", "SMTP_HOST=mail.example.com"]
    if os.name == "posix":
        assert path.stat().st_mode & 0o077 == 0
    assert list(tmp_path.glob(".*.tmp")) == []


def test_a_none_or_empty_value_removes_the_key(tmp_path):
    path = tmp_path / "a.env"
    path.write_text("APIFY_TOKEN=x\nSMTP_HOST=h\n", encoding="utf-8")
    write_values(path, {"APIFY_TOKEN": None, "SMTP_HOST": ""}, KEYS)
    assert path.read_text(encoding="utf-8") == ""


def test_windows_line_endings_are_kept(tmp_path):
    path = tmp_path / "a.env"
    path.write_bytes(b"APIFY_TOKEN=x\r\nOTHER=1\r\n")
    write_values(path, {"APIFY_TOKEN": "y"}, KEYS)
    assert path.read_bytes() == b"APIFY_TOKEN=y\r\nOTHER=1\r\n"


def test_awkward_values_survive_a_round_trip_through_the_settings_loader(target):
    password = 'p#ss "quoted" back\\slash $dollar'
    write_values(target, {"SMTP_PASSWORD": password, "SMTP_HOST": "mail.example.com"}, KEYS)
    settings = load_settings()
    assert settings.smtp_password.get_secret_value() == password
    assert settings.smtp_host == "mail.example.com"


def test_values_that_could_break_the_file_are_refused(tmp_path):
    path = tmp_path / "a.env"
    for bad in ("one\ntwo", "a\rb", "x${HOME}", "\x00", "y" * 600):
        with pytest.raises(ConfigError):
            write_values(path, {"APIFY_TOKEN": bad}, KEYS)
    assert not path.exists()
    with pytest.raises(ConfigError, match="cannot be changed"):
        write_values(path, {"WEB_ACCESS_TOKEN": "x"}, KEYS)
    with pytest.raises(ConfigError, match="cannot be changed"):
        write_values(path, {"lower": "x"}, KEYS | {"lower"})


def test_the_writer_refuses_links_directories_and_oversized_files(tmp_path):
    folder = tmp_path / "dir.env"
    folder.mkdir()
    with pytest.raises(ConfigError, match="regular file"):
        write_values(folder, {"APIFY_TOKEN": "x"}, KEYS)
    big = tmp_path / "big.env"
    big.write_text("A" * (MAX_ENV_FILE_BYTES + 1), encoding="utf-8")
    with pytest.raises(ConfigError, match="larger"):
        write_values(big, {"APIFY_TOKEN": "x"}, KEYS)
    real = tmp_path / "real.env"
    real.write_text("A=1\n", encoding="utf-8")
    link = tmp_path / "link.env"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("links cannot be created here")
    with pytest.raises(ConfigError, match="link"):
        write_values(link, {"APIFY_TOKEN": "x"}, KEYS)
    assert real.read_text(encoding="utf-8") == "A=1\n"


def test_overridden_reports_values_the_environment_wins_over(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "from-the-shell")
    assert overridden(["APIFY_TOKEN", "SMTP_HOST"]) == ["APIFY_TOKEN"]


def test_every_form_field_is_a_known_setting_and_secrets_are_flagged():
    for section in SECTIONS:
        for field in section.fields:
            assert field.key.lower() in Settings.model_fields, field.key
    assert {"LLM_API_KEY", "APIFY_TOKEN", "BRAVE_API_KEY", "SMTP_PASSWORD"} <= SECRET_KEYS
    assert "WEB_ACCESS_TOKEN" not in EDITABLE
    assert env_target() == Path(".env") or os.environ.get(ENV_FILE_VARIABLE)


def test_applying_validates_before_writing_anything(target):
    manager = Connections()
    with pytest.raises(ConfigError):
        manager.apply({"SMTP_PORT": "99999"})
    with pytest.raises(ConfigError, match="cannot be changed"):
        manager.apply({"WEB_PORT": "1"})
    with pytest.raises(ConfigError, match="Both"):
        manager.apply({"LLM_API_KEY": "sk-x", "LLM_OLLAMA_HOST": "localhost"})
    assert not target.exists()


def test_applying_saves_reloads_and_reports_what_is_present(target):
    manager = Connections()
    settings, shadowed = manager.apply({"APIFY_TOKEN": "tok", "AGENT_CONTACT": "me@example.com"})
    assert shadowed == []
    assert settings.apify_token.get_secret_value() == "tok"
    assert settings.llm_config().provider == "apify"
    present = manager.present(settings)
    assert present["APIFY_TOKEN"] == "set" and present["AGENT_CONTACT"] == "me@example.com"
    settings, _ = manager.apply({"APIFY_TOKEN": None})
    assert settings.apify_token is None
    assert settings.llm_config_or_none() is None


def test_applying_warns_when_the_environment_overrides_the_file(target, monkeypatch):
    monkeypatch.setenv("AGENT_CONTACT", "shell@example.com")
    settings, shadowed = Connections().apply({"AGENT_CONTACT": "file@example.com"})
    assert shadowed == ["AGENT_CONTACT"]
    assert settings.agent_contact == "shell@example.com"


def checker(handler):
    return ConnectionChecker(lambda settings: mock_client(handler))


async def test_the_language_model_check_reports_success_and_failure():
    settings = Settings(_env_file=None, llm_api_key="sk-ant-x")
    ok = await checker(
        lambda r: json_response(
            {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
        )
    ).check("llm", settings)
    assert ok.ok and "anthropic" in ok.message
    bad = await checker(lambda r: json_response({"error": "no"}, 401)).check("llm", settings)
    assert not bad.ok and "Apify AI gateway" not in bad.message


async def test_the_apify_ai_gateway_failure_explains_the_likely_reason():
    settings = Settings(_env_file=None, apify_token="tok")
    result = await checker(lambda r: json_response({"error": "forbidden"}, 403)).check(
        "llm", settings
    )
    assert not result.ok and "inside the Apify platform" in result.message


async def test_the_other_checks_read_real_endpoints():
    payload = {
        "data": {
            "current": {"monthlyUsageUsd": 1.0},
            "limits": {"maxMonthlyUsageUsd": 5, "maxConcurrentActorJobs": 2},
        }
    }
    settings = Settings(
        _env_file=None, apify_token="tok", brave_api_key="b", elevenlabs_api_key="e"
    )
    apify = await checker(lambda r: json_response(payload)).check("apify", settings)
    assert apify.ok and "4.00" in apify.message
    brave = await checker(
        lambda r: json_response({"web": {"results": [{"url": "https://a.example", "title": "t"}]}})
    ).check("search", settings)
    assert brave.ok and "Brave" in brave.message
    voice = await checker(lambda r: json_response([])).check("voice", settings)
    assert voice.ok
    missing = await checker(lambda r: json_response([])).check("voice", Settings(_env_file=None))
    assert not missing.ok and "No ElevenLabs key" in missing.message
