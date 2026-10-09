# tests/test_envfile.py
from agent.envfile import (
    env_source,
    env_warnings,
    load_settings,
    write_values,
)
from agent.config import (
    ENV_FILE_VARIABLE,
    MAX_ENV_FILE_BYTES,
    Settings,
)
from agent.web.settings import (
    load_web_settings,
    WebSettings,
)
from agent.errors import ConfigError
from pydantic import ValidationError
from pathlib import Path

import pytest
import os

ROOT = Path(__file__).resolve().parent.parent
SECRET = "sk-ant-secret-value-from-the-file"


STRONG = "Ab3dEf6hIj9kLm2nOp5qRs8t"
STRONG_API = "Zy7xWv4uTs1rQp0oNm9lKj6i"


class FakeStat:
    def __init__(self, mode):
        self.st_mode = mode


class FakePath:
    def __init__(self, mode):
        self._mode = mode

    def stat(self):
        return FakeStat(self._mode)

    def __str__(self):
        return "private.env"


def write_env(tmp_path, monkeypatch, text, name="test.env"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv(ENV_FILE_VARIABLE, str(path))
    return path


def test_no_file_means_no_source(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_FILE_VARIABLE, str(tmp_path / "missing.env"))
    assert env_source() is None
    monkeypatch.delenv(ENV_FILE_VARIABLE)
    monkeypatch.chdir(tmp_path)
    assert env_source() is None


def test_the_default_file_is_dot_env_in_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_FILE_VARIABLE)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("LLM_MODEL=from-default\n", encoding="utf-8")
    assert env_source() == Path(".env")
    assert load_settings().llm_model == "from-default"


def test_values_are_loaded_from_the_file(tmp_path, monkeypatch):
    write_env(
        tmp_path,
        monkeypatch,
        f"LLM_API_KEY={SECRET}\nAPIFY_TOKEN='quoted token'\nELEVENLABS_API_KEY=voice-key\n"
        "ELEVENLABS_VOICE_ID=AbCdEfGhIjKl\nSCORING__THRESHOLD=0.9\nIGNORED_VARIABLE=1\n",
    )
    settings = load_settings()
    assert settings.llm_api_key.get_secret_value() == SECRET
    assert settings.apify_token.get_secret_value() == "quoted token"
    assert settings.elevenlabs_api_key.get_secret_value() == "voice-key"
    assert settings.voice_id() == "AbCdEfGhIjKl"
    assert settings.scoring.threshold == 0.9


def test_real_environment_variables_win_over_the_file(tmp_path, monkeypatch):
    write_env(tmp_path, monkeypatch, "LLM_MODEL=from-file\nOUTPUT_DIR=from-file\n")
    monkeypatch.setenv("LLM_MODEL", "from-environment")
    settings = load_settings()
    assert settings.llm_model == "from-environment"
    assert settings.output_dir == Path("from-file")


def test_blank_values_in_the_file_are_ignored(tmp_path, monkeypatch):
    write_env(tmp_path, monkeypatch, "LLM_API_KEY=\nBRAVE_API_KEY=\nLLM_MODEL=\n")
    settings = load_settings()
    assert settings.llm_api_key is None and settings.brave_api_key is None
    assert settings.llm_model is None


def test_the_file_is_read_into_memory_only(tmp_path, monkeypatch):
    write_env(tmp_path, monkeypatch, f"LLM_API_KEY={SECRET}\nWEB_ACCESS_TOKEN={STRONG}\n")
    load_settings()
    load_web_settings()
    assert "LLM_API_KEY" not in os.environ and "WEB_ACCESS_TOKEN" not in os.environ
    assert SECRET not in "".join(os.environ.values())


def test_secrets_never_appear_in_repr_or_dumps(tmp_path, monkeypatch):
    write_env(tmp_path, monkeypatch, f"LLM_API_KEY={SECRET}\nELEVENLABS_API_KEY=voice-key\n")
    settings = load_settings()
    assert SECRET not in repr(settings) and "voice-key" not in repr(settings)
    assert SECRET not in str(settings.model_dump()) and "voice-key" not in str(
        settings.model_dump()
    )
    assert settings.secret_values() == [SECRET, "voice-key"]


def test_rejected_values_are_not_echoed_in_errors(tmp_path, monkeypatch):
    write_env(tmp_path, monkeypatch, "ELEVENLABS_VOICE_ID=not-valid-because-of-dashes-xyz\n")
    with pytest.raises(ValidationError) as caught:
        load_settings()
    assert "not-valid-because-of-dashes-xyz" not in str(caught.value)
    assert "elevenlabs_voice_id" in str(caught.value)


def test_web_settings_load_from_the_same_file(tmp_path, monkeypatch):
    write_env(
        tmp_path,
        monkeypatch,
        f"WEB_ACCESS_TOKEN={STRONG}\nWEB_API_TOKEN={STRONG_API}\nWEB_PORT=9123\n",
    )
    web = load_web_settings()
    assert web.access_token.get_secret_value() == STRONG
    assert web.api_token.get_secret_value() == STRONG_API and web.port == 9123
    assert STRONG not in repr(web)


def test_web_settings_errors_hide_the_rejected_token(tmp_path, monkeypatch):
    write_env(tmp_path, monkeypatch, "WEB_ACCESS_TOKEN=short-secret\n")
    with pytest.raises(ConfigError) as caught:
        load_web_settings()
    assert "short-secret" not in str(caught.value) and "short-secret" not in repr(
        caught.value.__cause__
    )
    with pytest.raises(ValidationError) as direct:
        WebSettings(_env_file=None, access_token="short-secret")
    assert "short-secret" not in str(direct.value)


def test_directories_and_huge_files_are_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_FILE_VARIABLE, str(tmp_path))
    with pytest.raises(ConfigError, match="not a regular file"):
        env_source()
    write_env(tmp_path, monkeypatch, "A=" + "x" * MAX_ENV_FILE_BYTES)
    with pytest.raises(ConfigError, match="larger than"):
        env_source()


def test_a_file_readable_by_others_triggers_a_warning(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    for mode in (0o644, 0o640, 0o604, 0o666):
        warnings = env_warnings(FakePath(mode))
        assert warnings == ["private.env can be read by other users; run: chmod 600 private.env"]
    for mode in (0o600, 0o400, 0o700):
        assert env_warnings(FakePath(mode)) == []


def test_there_is_nothing_to_warn_about_without_a_file_or_on_windows(monkeypatch):
    assert env_warnings(None) == []
    monkeypatch.setattr(os, "name", "nt")
    assert env_warnings(FakePath(0o666)) == []


def example_variables():
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    return [line.partition("=")[0] for line in lines[1:] if line.strip()]


def test_every_variable_in_the_example_is_a_real_setting():
    names = example_variables()
    assert "ELEVENLABS_API_KEY" in names and "WEB_ACCESS_TOKEN" in names
    settings_fields = set(Settings.model_fields)
    web_fields = set(WebSettings.model_fields)
    for name in names:
        if name.startswith("WEB_"):
            assert name.removeprefix("WEB_").lower() in web_fields, name
        else:
            assert name.split("__")[0].lower() in settings_fields, name


def test_the_example_holds_no_secrets_and_loads_cleanly(monkeypatch):
    monkeypatch.setenv(ENV_FILE_VARIABLE, str(ROOT / ".env.example"))
    settings = load_settings()
    assert settings.secret_values() == []
    assert settings.elevenlabs_api_key is None and settings.apify_token is None
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    for line in lines[1:]:
        assert line.partition("=")[2] in {"", "true", "localhost,127.0.0.1"}, line


def test_real_env_files_are_ignored_by_git_but_the_example_is_not():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignored and ".env.*" in ignored and "!.env.example" in ignored


def test_unicode_line_separators_cannot_smuggle_new_keys(tmp_path):
    path = tmp_path / ".env"
    for sneaky in ("a\u2028WEB_ACCESS_TOKEN=x", "a\u2029B=c", "a\x85B=c", "a\u200bb", "a\ufeffb"):
        with pytest.raises(ConfigError, match="cannot be stored"):
            write_values(path, {"LLM_MODEL": sneaky}, frozenset({"LLM_MODEL"}))
    path.write_text('KEEP=1\nNOTE="x\u2028SNEAK=1"\n', encoding="utf-8")
    write_values(path, {"LLM_MODEL": "ok"}, frozenset({"LLM_MODEL"}))
    text = path.read_text(encoding="utf-8")
    assert 'NOTE="x\u2028SNEAK=1"' in text and "LLM_MODEL=ok" in text
    assert [line.split("=")[0] for line in text.split("\n") if line] == [
        "KEEP",
        "NOTE",
        "LLM_MODEL",
    ]
