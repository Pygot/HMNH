# tests/test_apify.py
from tests.helpers import (
    body_of,
    json_response,
    make_runner,
    mock_client,
)
from agent.errors import (
    AuthenticationError,
    UpstreamError,
)

import pytest
import httpx


async def test_runner_request_shape_and_caching():
    calls = []

    def handler(request):
        calls.append(request)
        return json_response([{"fullName": "Jane"}])

    async with mock_client(handler) as client:
        runner = make_runner(client, "apify-token")
        first = await runner.run("owner/actor-name", {"b": 1, "a": 2})
        second = await runner.run("owner/actor-name", {"a": 2, "b": 1})
    assert first == second == [{"fullName": "Jane"}]
    assert len(calls) == 1
    request = calls[0]
    assert request.url.path == "/v2/acts/owner~actor-name/run-sync-get-dataset-items"
    assert request.headers["authorization"] == "Bearer apify-token"
    assert "apify-token" not in str(request.url)
    assert body_of(request) == {"b": 1, "a": 2}
    assert request.url.params["format"] == "json"
    assert request.url.params["maxTotalChargeUsd"] == "0.5"
    assert request.url.params["timeout"] == "280"


async def test_runner_does_not_share_cache_between_inputs():
    calls = []

    def handler(request):
        calls.append(body_of(request))
        return json_response([])

    async with mock_client(handler) as client:
        runner = make_runner(client, "t")
        await runner.run("o/a", {"x": 1})
        await runner.run("o/a", {"x": 2})
    assert calls == [{"x": 1}, {"x": 2}]


async def test_runner_error_mapping():
    async with mock_client(lambda request: httpx.Response(401)) as client:
        runner = make_runner(client, "t")
        with pytest.raises(AuthenticationError, match="Apify actor o/a"):
            await runner.run("o/a", {})
    async with mock_client(lambda request: httpx.Response(402)) as client:
        runner = make_runner(client, "t")
        with pytest.raises(UpstreamError, match="credits"):
            await runner.run("o/a", {})


@pytest.mark.parametrize("payload", [{"items": []}, ["text"], [1, 2]])
async def test_runner_rejects_unexpected_shapes(payload):
    async with mock_client(lambda request: json_response(payload)) as client:
        runner = make_runner(client, "t")
        with pytest.raises(UpstreamError, match="unexpected response shape"):
            await runner.run("o/a", {})


async def test_runner_sends_the_configured_cost_cap():
    seen = {}

    def handler(request):
        seen["cap"] = request.url.params["maxTotalChargeUsd"]
        return json_response([])

    async with mock_client(handler) as client:
        await make_runner(client, "t", max_charge=2.5).run("o/a", {})
    assert seen["cap"] == "2.5"


async def test_usage_reads_the_account_limits():
    payload = {
        "data": {
            "current": {"monthlyUsageUsd": 1.25},
            "limits": {"maxMonthlyUsageUsd": 105, "maxConcurrentActorJobs": 5},
        }
    }
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["path"] = request.url.path
        return json_response(payload)

    async with mock_client(handler) as client:
        usage = await make_runner(client, "secret-token").usage()
    assert seen == {"auth": "Bearer secret-token", "path": "/v2/users/me/limits"}
    assert usage.monthly_usage_usd == 1.25
    assert usage.max_concurrent_jobs == 5
    assert usage.remaining_usd == 103.75


async def test_usage_rejects_unexpected_shapes_and_errors():
    async with mock_client(lambda request: json_response({"data": {}})) as client:
        with pytest.raises(UpstreamError, match="unexpected response shape"):
            await make_runner(client).usage()
    async with mock_client(lambda request: httpx.Response(401)) as client:
        with pytest.raises(AuthenticationError, match="Apify account"):
            await make_runner(client).usage()
