# tests/test_runtime.py
from agent.config import (
    LogTuning,
    Settings,
)
from agent.errors import (
    ConfigError,
    UpstreamError,
)
from agent.logs import (
    configure_logging,
    RedactingFilter,
)
from agent.runtime import (
    open_runtime,
    Runtime,
)
from agent.web import server as web_server
from typer.testing import CliRunner
from agent.apify import ApifyUsage
from agent.models import Network
from agent.cli import app

import logging
import uvicorn
import pytest


STRONG = "Ab3dEf6hIj9kLm2nOp5qRs8t"


def settings(**values):
    return Settings(_env_file=None, **values)


async def test_runtime_is_wired_from_the_settings():
    config = settings(llm_api_key="sk-ant-x", brave_api_key="b", apify_token="a")
    async with open_runtime(config) as runtime:
        assert runtime.pipeline.unavailable_networks == []
        assert runtime.apify is not None
        assert runtime.settings is config


async def test_social_networks_are_unavailable_without_an_apify_token():
    config = settings(llm_api_key="sk-ant-x", brave_api_key="b")
    async with open_runtime(config) as runtime:
        assert runtime.pipeline.unavailable_networks == [
            Network.LINKEDIN,
            Network.FACEBOOK,
            Network.INSTAGRAM,
        ]
        assert runtime.apify is None


async def test_a_missing_linkedin_actor_leaves_only_linkedin_unavailable():
    config = settings(llm_api_key="sk-ant-x", apify_token="a", apify_linkedin_actor=None)
    async with open_runtime(config) as runtime:
        assert runtime.pipeline.unavailable_networks == [Network.LINKEDIN]


async def test_the_search_provider_choice_is_respected():
    brave = settings(llm_api_key="sk-ant-x", brave_api_key="b", apify_token="a")
    async with open_runtime(brave):
        pass
    apify = settings(
        llm_api_key="sk-ant-x", brave_api_key="b", apify_token="a", search_provider="apify"
    )
    async with open_runtime(apify):
        pass


async def test_missing_configuration_fails_before_anything_starts():
    with pytest.raises(ConfigError, match="No LLM configured"):
        async with open_runtime(settings(brave_api_key="b")):
            pass
    with pytest.raises(ConfigError, match="No search provider"):
        async with open_runtime(settings(llm_api_key="sk-ant-x")):
            pass


class FakeApify:
    def __init__(self, usage=None, error=None):
        self._usage = usage
        self._error = error

    async def usage(self):
        if self._error:
            raise self._error
        return self._usage


async def runtime_with(apify, **values):
    config = settings(llm_api_key="sk-ant-x", brave_api_key="b", **values)
    async with open_runtime(config) as real:
        return Runtime(pipeline=real.pipeline, settings=config, apify=apify)


async def test_status_describes_providers_and_unavailable_networks():
    runtime = await runtime_with(None)
    status = await runtime.status()
    assert status.llm_provider == "anthropic"
    assert status.llm_model == "claude-sonnet-5-5"
    assert status.search_provider == "brave"
    assert status.unavailable_networks == ["linkedin", "facebook", "instagram"]
    assert status.apify_remaining_usd is None and status.apify_error is None


async def test_status_includes_the_apify_credit():
    usage = ApifyUsage(monthly_usage_usd=5.0, max_monthly_usage_usd=105.0, max_concurrent_jobs=5)
    status = await (await runtime_with(FakeApify(usage=usage))).status()
    assert status.apify_monthly_usage_usd == 5.0
    assert status.apify_remaining_usd == 100.0


async def test_status_reports_apify_failures_instead_of_hiding_them():
    failing = FakeApify(error=UpstreamError("Apify account timed out."))
    status = await (await runtime_with(failing)).status()
    assert status.apify_error == "Apify account timed out."
    assert status.apify_remaining_usd is None


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers, root.level = handlers, level


def test_secrets_are_redacted_from_log_output(capsys, restore_logging):
    configure_logging(["super-secret-key", "abc"], LogTuning())
    logging.getLogger("agent.test").error("failed with %s for %s", "super-secret-key", "abc")
    captured = capsys.readouterr().err
    assert "super-secret-key" not in captured
    assert "failed with *** for abc" in captured


def test_the_redaction_threshold_comes_from_the_configuration(capsys, restore_logging):
    configure_logging(["abc"], LogTuning(min_secret_length=3))
    logging.getLogger("agent.test").error("value abc")
    assert "value ***" in capsys.readouterr().err


def test_redaction_filter_replaces_every_occurrence():
    record = logging.LogRecord(
        "x", logging.ERROR, __file__, 1, "%s and %s", ("tokenvalue",) * 2, None
    )
    assert RedactingFilter(["tokenvalue"], 4).filter(record)
    assert record.getMessage() == "*** and ***"


def test_logger_levels_and_noisy_loggers_follow_the_configuration(restore_logging):
    configure_logging([], LogTuning(level=logging.WARNING, noisy_loggers=("chatty.lib",)))
    assert logging.getLogger().level == logging.WARNING
    assert logging.getLogger("chatty.lib").level == logging.WARNING
    configure_logging([], LogTuning())
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
    assert logging.getLogger().level == logging.INFO


def test_server_starts_with_hardened_uvicorn_options(monkeypatch):
    monkeypatch.setenv("WEB_ACCESS_TOKEN", STRONG)
    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda application, **kwargs: captured.update(kwargs))
    web_server.run_server(settings(llm_api_key="sk-ant-x", brave_api_key="b"), None, None)
    assert captured == {
        "host": "127.0.0.1",
        "port": 8000,
        "server_header": False,
        "proxy_headers": False,
        "forwarded_allow_ips": None,
        "access_log": False,
        "log_config": None,
    }
    web_server.run_server(settings(llm_api_key="sk-ant-x", brave_api_key="b"), "localhost", 9001)
    assert (captured["host"], captured["port"]) == ("localhost", 9001)


def test_a_trusted_proxy_list_turns_on_forwarded_headers_only_for_those_addresses(monkeypatch):
    monkeypatch.setenv("WEB_ACCESS_TOKEN", STRONG)
    monkeypatch.setenv("WEB_TRUSTED_PROXIES", "10.0.0.2, 10.0.0.3")
    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda application, **kwargs: captured.update(kwargs))
    web_server.run_server(settings(llm_api_key="sk-ant-x", brave_api_key="b"), None, None)
    assert captured["proxy_headers"] is True
    assert captured["forwarded_allow_ips"] == "10.0.0.2,10.0.0.3"


def test_the_listen_address_and_port_come_from_the_web_settings(monkeypatch):
    monkeypatch.setenv("WEB_ACCESS_TOKEN", STRONG)
    monkeypatch.setenv("WEB_HOST", "localhost")
    monkeypatch.setenv("WEB_PORT", "9300")
    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda application, **kwargs: captured.update(kwargs))
    web_server.run_server(settings(llm_api_key="sk-ant-x", brave_api_key="b"), None, None)
    assert (captured["host"], captured["port"]) == ("localhost", 9300)


def test_server_refuses_to_leave_loopback_without_secure_cookies(monkeypatch):
    monkeypatch.setenv("WEB_ACCESS_TOKEN", STRONG)
    monkeypatch.setattr(uvicorn, "run", lambda application, **kwargs: None)
    config = settings(llm_api_key="sk-ant-x", brave_api_key="b")
    monkeypatch.setenv("WEB_COOKIE_SECURE", "false")
    with pytest.raises(ConfigError, match="loopback"):
        web_server.run_server(config, "10.0.0.5", None)
    monkeypatch.setenv("WEB_COOKIE_SECURE", "true")
    web_server.run_server(config, "10.0.0.5", None)


def test_server_starts_without_any_credentials(monkeypatch):
    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda application, **kwargs: captured.update(kwargs))
    web_server.run_server(settings(), None, None)
    assert captured["host"] == "127.0.0.1"


def test_serve_command_reports_configuration_errors(monkeypatch):
    monkeypatch.setattr(uvicorn, "run", lambda application, **kwargs: None)
    monkeypatch.setenv("WEB_COOKIE_SECURE", "false")
    result = CliRunner().invoke(app, ["serve", "--host", "10.0.0.5"])
    assert result.exit_code == 2
    assert "error:" in result.output


def test_serve_command_passes_its_options_through(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        web_server, "run_server", lambda config, host, port: seen.update(host=host, port=port)
    )
    result = CliRunner().invoke(app, ["serve", "--host", "localhost", "--port", "9100"])
    assert result.exit_code == 0, result.output
    assert seen == {"host": "localhost", "port": 9100}


async def test_voice_is_enabled_only_when_a_key_is_configured():
    plain = settings(llm_api_key="sk-ant-x", brave_api_key="b")
    async with open_runtime(plain) as runtime:
        assert runtime.voice is None
        assert (await runtime.status()).voice_enabled is False
    spoken = settings(
        llm_api_key="sk-ant-x",
        brave_api_key="b",
        elevenlabs_api_key="e",
        elevenlabs_voice_id="AbCdEfGhIjKl",
    )
    async with open_runtime(spoken) as runtime:
        assert runtime.voice is not None
        assert (await runtime.status()).voice_enabled is True


def test_the_voice_key_is_redacted_from_log_output(capsys, restore_logging):
    loaded = settings(elevenlabs_api_key="eleven-secret-key", llm_api_key="llm-secret-key")
    configure_logging(loaded.secret_values(), LogTuning())
    logging.getLogger("agent.test").error("failed with %s", "eleven-secret-key")
    assert "eleven-secret-key" not in capsys.readouterr().err
