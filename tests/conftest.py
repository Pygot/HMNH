# tests/conftest.py
import pytest
import socket
import httpx
import os

ENVIRONMENT_PREFIXES = (
    "BRAVE_",
    "APIFY_",
    "SEARCH_PROVIDER",
    "LLM_",
    "AGENT_CONTACT",
    "OUTPUT_DIR",
    "MATCH_",
    "CV_MAX_BYTES",
    "CACHE_TTL_SECONDS",
    "WEB_",
    "ELEVENLABS_",
    "VOICE__",
    "EMAIL__",
    "COMPANY__",
    "EVENTS__",
    "REQUIREMENTS__",
    "BUDDY__",
    "STORE__",
    "AUTH__",
    "MAIL__",
    "SMTP_",
    "CHAT__",
    "AGENT_ENV_FILE",
)


def isolate_environment(patch, root):
    for name in list(os.environ):
        if name.startswith(ENVIRONMENT_PREFIXES):
            patch.delenv(name)
    patch.setenv("AGENT_ENV_FILE", str(root / "no-such.env"))
    patch.setenv("STORE__PATH", str(root / "data" / "agent.db"))
    patch.setenv("AUTH__KEY_FILE", str(root / "data" / "secret.key"))
    patch.setenv("AUTH__SCRYPT_N", "1024")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch, tmp_path):
    isolate_environment(monkeypatch, tmp_path)


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    async def blocked_async(self, request):
        raise AssertionError("network access attempted in a test")

    def blocked_sync(self, request):
        raise AssertionError("network access attempted in a test")

    def blocked_lookup(*args, **kwargs):
        raise AssertionError("DNS lookup attempted in a test")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked_sync)
    monkeypatch.setattr(socket, "getaddrinfo", blocked_lookup)
