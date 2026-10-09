# tests/helpers.py
from agent.config import (
    DiscoveryTuning,
    Endpoints,
    HttpTuning,
    VoiceTuning,
)
from agent.search import (
    ApifyGoogleSearch,
    BraveSearch,
)
from agent.voice import VoiceService
from collections.abc import Callable
from agent.apify import ApifyRunner
from agent.http import build_client
from agent.http import TTLCache
from pydantic import SecretStr

import httpx
import json


def mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return build_client("test-agent/1.0", HttpTuning(), httpx.MockTransport(handler))


def json_response(data, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=data, headers=headers)


def body_of(request: httpx.Request):
    return json.loads(request.content)


def make_runner(client, token="t", max_charge=0.5):
    return ApifyRunner(
        client, SecretStr(token), TTLCache(60), Endpoints(), HttpTuning(), max_charge
    )


def make_brave(client, throttle, key="k", cache=None):
    return BraveSearch(
        client,
        SecretStr(key),
        cache or TTLCache(60),
        throttle,
        Endpoints(),
        DiscoveryTuning(),
        HttpTuning(),
    )


def make_google(runner):
    return ApifyGoogleSearch(runner, "apify/google-search-scraper", DiscoveryTuning(), 1)


def make_voice(client, key="eleven-key", voice="21m00Tcm4TlvDq8ikWAM", tuning=None, cache=None):
    return VoiceService(
        client,
        SecretStr(key),
        voice,
        Endpoints(),
        tuning or VoiceTuning(),
        cache or TTLCache(60),
    )
