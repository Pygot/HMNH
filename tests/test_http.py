# tests/test_http.py
from agent.errors import (
    AuthenticationError,
    ForbiddenHostError,
    RateLimitedError,
    UpstreamError,
)
from agent.http import (
    api_json,
    Throttle,
    TTLCache,
    user_agent,
)
from tests.helpers import (
    json_response,
    mock_client,
)

import pytest
import httpx


def test_user_agent_identifies_the_tool_honestly():
    assert "person-research-agent/" in user_agent("person-research-agent", None)
    assert "automated" in user_agent("person-research-agent", None)
    assert "contact: ops@example.com" in user_agent("person-research-agent", "ops@example.com")
    assert user_agent("custom-bot", None).startswith("custom-bot/")


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/in/jane",
        "https://cz.linkedin.com/in/jane",
        "https://www.facebook.com/jane",
        "https://m.facebook.com/jane",
        "https://www.instagram.com/jane/",
        "https://lnkd.in/x",
    ],
)
async def test_social_networks_are_never_requested_directly(url):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200)

    async with mock_client(handler) as client:
        with pytest.raises(ForbiddenHostError):
            await client.get(url)
    assert calls == []


async def test_other_hosts_are_allowed():
    async with mock_client(lambda request: httpx.Response(200, text="ok")) as client:
        assert (await client.get("https://example.com")).text == "ok"


def test_cache_expires_and_evicts():
    now = [0.0]
    cache = TTLCache[str](10, max_entries=2, clock=lambda: now[0])
    cache.put("a", "1")
    assert cache.get("a") == "1"
    now[0] = 10.0
    assert cache.get("a") is None
    cache.put("a", "1")
    cache.put("b", "2")
    cache.put("c", "3")
    assert cache.get("a") is None
    assert (cache.get("b"), cache.get("c")) == ("2", "3")


def test_cache_with_zero_ttl_stores_nothing():
    cache = TTLCache[str](0)
    cache.put("a", "1")
    assert cache.get("a") is None


async def test_throttle_spaces_requests():
    now = [0.0]
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    throttle = Throttle(2.0, clock=lambda: now[0], sleep=sleep)
    await throttle.wait()
    await throttle.wait()
    now[0] += 5
    await throttle.wait()
    await throttle.wait()
    assert slept == [2.0, 2.0]


@pytest.mark.parametrize(
    ("status", "error", "text"),
    [
        (401, AuthenticationError, "rejected the credentials"),
        (403, AuthenticationError, "rejected the credentials"),
        (402, UpstreamError, "exhausted credits"),
        (500, UpstreamError, "HTTP 500"),
    ],
)
async def test_error_statuses_map_to_clear_errors(status, error, text):
    async with mock_client(lambda request: httpx.Response(status)) as client:
        with pytest.raises(error, match=text):
            await api_json(client, "Svc", "GET", "https://api.example.com/x", time_limit=5)


async def test_rate_limit_reports_retry_after():
    async with mock_client(lambda r: httpx.Response(429, headers={"Retry-After": "7"})) as client:
        with pytest.raises(RateLimitedError, match="Retry after 7s") as info:
            await api_json(client, "Svc", "GET", "https://api.example.com/x", time_limit=5)
    assert info.value.retry_after == 7.0


async def test_rate_limit_without_header():
    async with mock_client(lambda request: httpx.Response(429)) as client:
        with pytest.raises(RateLimitedError) as info:
            await api_json(client, "Svc", "GET", "https://api.example.com/x", time_limit=5)
    assert info.value.retry_after is None


async def test_invalid_json_and_transport_failures_are_reported():
    async with mock_client(lambda request: httpx.Response(200, text="not json")) as client:
        with pytest.raises(UpstreamError, match="invalid JSON"):
            await api_json(client, "Svc", "GET", "https://api.example.com/x", time_limit=5)

    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    async with mock_client(timeout) as client:
        with pytest.raises(UpstreamError, match="timed out"):
            await api_json(client, "Svc", "GET", "https://api.example.com/x", time_limit=5)

    def refused(request):
        raise httpx.ConnectError("no route", request=request)

    async with mock_client(refused) as client:
        with pytest.raises(UpstreamError, match="ConnectError"):
            await api_json(client, "Svc", "GET", "https://api.example.com/x", time_limit=5)


async def test_api_json_returns_parsed_body():
    async with mock_client(lambda request: json_response({"ok": True})) as client:
        assert await api_json(client, "Svc", "GET", "https://api.example.com/x", time_limit=5) == {
            "ok": True
        }


async def test_error_bodies_with_a_message_are_surfaced_briefly():
    body = {"error": {"type": "bad", "message": "Maximum cost per run is too low"}}
    async with mock_client(lambda request: json_response(body, status=400)) as client:
        with pytest.raises(UpstreamError, match="HTTP 400: Maximum cost per run is too low"):
            await api_json(client, "Svc", "POST", "https://api.example.com/x", time_limit=5)
