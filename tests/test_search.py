# tests/test_search.py
from tests.helpers import (
    body_of,
    json_response,
    make_brave,
    make_google,
    make_runner,
    mock_client,
)
from agent.errors import (
    AuthenticationError,
    UpstreamError,
)
from agent.http import Throttle

import pytest
import httpx

BRAVE_PAYLOAD = {
    "web": {
        "results": [
            {
                "url": "https://jane.dev",
                "title": "Jane <strong>Doe</strong> &amp; Co",
                "description": "Engineer at <b>Acme</b>",
            },
            {"url": "javascript:alert(1)", "title": "bad", "description": ""},
            {"url": "https://example.com/a", "title": "A"},
        ]
    }
}


class CountingThrottle(Throttle):
    def __init__(self):
        super().__init__(0)
        self.waits = 0

    async def wait(self):
        self.waits += 1


async def test_brave_request_and_parsing():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url.copy_with(query=None))
        seen["token"] = request.headers["x-subscription-token"]
        seen["params"] = dict(request.url.params)
        return json_response(BRAVE_PAYLOAD)

    throttle = CountingThrottle()
    async with mock_client(handler) as client:
        search = make_brave(client, throttle, "brave-key")
        hits = await search.search("jane doe", 50)
    assert seen["url"] == "https://api.search.brave.com/res/v1/web/search"
    assert seen["token"] == "brave-key"
    assert seen["params"] == {"q": "jane doe", "count": "20"}
    assert [hit.url for hit in hits] == ["https://jane.dev", "https://example.com/a"]
    assert hits[0].title == "Jane Doe & Co"
    assert hits[0].snippet == "Engineer at Acme"
    assert throttle.waits == 1


async def test_brave_results_are_cached():
    calls = []

    def handler(request):
        calls.append(request)
        return json_response(BRAVE_PAYLOAD)

    throttle = CountingThrottle()
    async with mock_client(handler) as client:
        search = make_brave(client, throttle, "k")
        first = await search.search("q", 5)
        second = await search.search("q", 5)
    assert first == second
    assert len(calls) == 1
    assert throttle.waits == 1


async def test_brave_without_web_section_means_no_results():
    async with mock_client(lambda request: json_response({"query": {}})) as client:
        search = make_brave(client, CountingThrottle(), "k")
        assert await search.search("q", 5) == []


async def test_brave_rejects_oversized_queries_and_bad_shapes():
    async with mock_client(
        lambda request: json_response({"web": {"results": [{"title": "x"}]}})
    ) as c:
        search = make_brave(c, CountingThrottle(), "k")
        with pytest.raises(ValueError, match="exceeds"):
            await search.search("x" * 401, 5)
        with pytest.raises(UpstreamError, match="unexpected response shape"):
            await search.search("q", 5)


async def test_brave_authentication_failure_is_loud():
    async with mock_client(lambda request: httpx.Response(401)) as client:
        search = make_brave(client, CountingThrottle(), "k")
        with pytest.raises(AuthenticationError, match="Brave Search API"):
            await search.search("q", 5)


async def test_apify_google_search_parses_organic_results():
    seen = {}

    def handler(request):
        seen["body"] = body_of(request)
        return json_response(
            [
                {
                    "organicResults": [
                        {"url": "https://a.example", "title": "A", "description": "da"},
                        {"url": "https://b.example", "title": "B", "description": "db"},
                    ]
                },
                {"organicResults": [{"url": "https://c.example", "title": "C"}]},
                {"searchQuery": {}},
            ]
        )

    async with mock_client(handler) as client:
        runner = make_runner(client, "tok")
        hits = await make_google(runner).search("q", 2)
    assert seen["body"] == {"queries": "q", "maxPagesPerQuery": 1}
    assert [hit.url for hit in hits] == ["https://a.example", "https://b.example"]


async def test_apify_google_search_rejects_bad_shapes():
    async with mock_client(
        lambda request: json_response([{"organicResults": [{"title": "x"}]}])
    ) as c:
        runner = make_runner(c, "tok")
        with pytest.raises(UpstreamError, match="unexpected response shape"):
            await make_google(runner).search("q", 5)


async def test_queries_are_limited_by_the_provider_word_and_character_caps():
    long_words = " ".join(f"word{n}" for n in range(33))
    async with mock_client(lambda request: json_response([])) as client:
        google = make_google(make_runner(client, "tok"))
        with pytest.raises(ValueError, match="32 words"):
            await google.search(long_words, 5)
        brave = make_brave(client, CountingThrottle())
        with pytest.raises(ValueError, match="400 characters"):
            await brave.search("x" * 401, 5)
        with pytest.raises(ValueError, match="50 words"):
            await brave.search(" ".join(["w"] * 51), 5)
