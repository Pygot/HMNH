# tests/test_fetcher.py
from agent.fetcher import (
    PageFetcher,
    resolve_host,
)
from agent.errors import SourceUnavailable
from agent.html_text import extract_text
from tests.helpers import mock_client
from agent.config import FetchTuning
from agent.models import Network
from agent.http import TTLCache

import asyncio
import codecs
import pytest
import socket
import httpx
import time

PAGE = (
    "<html><head><title> Jane Doe - Portfolio </title>"
    '<meta name="description" content="Engineer and speaker">'
    "<script>var secret = 1;</script><style>p{}</style></head>"
    "<body><nav>menu</nav><h1>Jane Doe</h1><p>Built <b>things</b> at Acme.</p>"
    "<noscript>enable js</noscript><ul><li>Talk one</li><li>Talk two</li></ul></body></html>"
)

ROBOTS_OPEN = "User-agent: *\nAllow: /\n"


async def public(host):
    return ["93.184.216.34"]


def site(pages, robots=ROBOTS_OPEN, robots_status=200):
    requests = []

    def handler(request):
        requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(robots_status, text=robots)
        response = pages.get(request.url.path)
        return response if response is not None else httpx.Response(404)

    return handler, requests


def fetcher(client, resolver=public, **tuning):
    config = FetchTuning(min_interval_seconds=0, **tuning)
    return PageFetcher(client, TTLCache(60), config, "person-research-agent", resolver=resolver)


def test_extract_text_removes_scripts_and_keeps_structure():
    title, text = extract_text(PAGE, 1000)
    assert title == "Jane Doe - Portfolio"
    assert text.splitlines()[0] == "Engineer and speaker"
    assert "Built things at Acme." in text
    assert "secret" not in text
    assert "menu" not in text
    assert "enable js" not in text
    assert "Talk one" in text.splitlines()


def test_extract_text_is_bounded():
    assert len(extract_text("<p>" + "word " * 1000 + "</p>", 50)[1]) == 50


async def test_fetch_returns_a_web_document_with_honest_user_agent():
    agents = []

    def handler(request):
        agents.append(request.headers["user-agent"])
        return httpx.Response(
            200,
            text=ROBOTS_OPEN if request.url.path == "/robots.txt" else PAGE,
            headers={"content-type": "text/html; charset=utf-8"},
        )

    async with mock_client(handler) as client:
        document = await fetcher(client).fetch("https://jane.dev/about")
    assert document.network is Network.WEB
    assert document.title == "Jane Doe - Portfolio"
    assert "Built things at Acme." in document.text
    assert all(agent.startswith("test-agent/1.0") for agent in agents)


async def test_robots_disallow_blocks_the_page_and_the_page_is_not_requested():
    handler, requests = site(
        {"/about": httpx.Response(200, html=PAGE)}, robots="User-agent: *\nDisallow: /\n"
    )
    async with mock_client(handler) as client:
        with pytest.raises(SourceUnavailable, match=r"robots.txt .* disallows"):
            await fetcher(client).fetch("https://jane.dev/about")
    assert requests == ["https://jane.dev/robots.txt"]


async def test_robots_rules_for_our_agent_are_honoured():
    robots = "User-agent: person-research-agent\nDisallow: /private\n\nUser-agent: *\nAllow: /\n"
    handler, _ = site(
        {"/private": httpx.Response(200, html=PAGE), "/open": httpx.Response(200, html=PAGE)},
        robots=robots,
    )
    async with mock_client(handler) as client:
        instance = fetcher(client)
        assert (await instance.fetch("https://jane.dev/open")).title
        with pytest.raises(SourceUnavailable, match="disallows"):
            await instance.fetch("https://jane.dev/private")


@pytest.mark.parametrize(
    ("status", "allowed"), [(404, True), (410, True), (401, False), (403, False), (500, False)]
)
async def test_robots_status_handling(status, allowed):
    handler, _ = site({"/a": httpx.Response(200, html=PAGE)}, robots="", robots_status=status)
    async with mock_client(handler) as client:
        if allowed:
            assert (await fetcher(client).fetch("https://jane.dev/a")).text
        else:
            with pytest.raises(SourceUnavailable, match=r"robots.txt"):
                await fetcher(client).fetch("https://jane.dev/a")


async def test_robots_is_fetched_once_per_host_and_pages_are_cached():
    handler, requests = site(
        {"/a": httpx.Response(200, html=PAGE), "/b": httpx.Response(200, html=PAGE)}
    )
    async with mock_client(handler) as client:
        instance = fetcher(client)
        await instance.fetch("https://jane.dev/a")
        await instance.fetch("https://jane.dev/a")
        await instance.fetch("https://jane.dev/b")
    assert requests.count("https://jane.dev/robots.txt") == 1
    assert requests.count("https://jane.dev/a") == 1


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "192.168.0.5",
        "169.254.169.254",
        "::1",
        "::ffff:127.0.0.1",
        "fd00::1",
        "64:ff9b::7f00:1",
        "2002:7f00:1::1",
    ],
)
async def test_non_public_addresses_are_refused_before_any_request(address):
    handler, requests = site({"/": httpx.Response(200, html=PAGE)})

    async def resolver(host):
        return [address]

    async with mock_client(handler) as client:
        with pytest.raises(SourceUnavailable, match="non-public"):
            await fetcher(client, resolver).fetch("https://internal.example/")
    assert requests == []


async def test_the_default_resolver_returns_the_addresses_of_the_host(monkeypatch):
    def lookup(*args, **kwargs):
        return [(2, 1, 6, "", ("93.184.216.34", 0)), (10, 1, 6, "", ("2606:2800:220::1", 0, 0, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    assert await resolve_host("jan.dev") == ["93.184.216.34", "2606:2800:220::1"]


async def test_unresolvable_hosts_are_reported():
    async def failing(host):
        raise OSError("nxdomain")

    async def empty(host):
        return []

    handler, _ = site({})
    async with mock_client(handler) as client:
        with pytest.raises(SourceUnavailable, match="could not be resolved"):
            await fetcher(client, failing).fetch("https://nope.example/")
        with pytest.raises(SourceUnavailable, match="could not be resolved"):
            await fetcher(client, empty).fetch("https://nope.example/")


@pytest.mark.parametrize(
    "url",
    [
        "ftp://jane.dev/a",
        "https://jane.dev:8443/a",
        "http://jane.dev:22/a",
        "https://www.linkedin.com/in/jane",
        "https://www.instagram.com/jane/",
        "https://facebook.com/jane",
    ],
)
async def test_scheme_port_and_social_hosts_are_refused(url):
    handler, requests = site({})
    async with mock_client(handler) as client:
        with pytest.raises(SourceUnavailable):
            await fetcher(client).fetch(url)
    assert requests == []


async def test_redirects_are_followed_and_rechecked_at_every_hop():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_OPEN)
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return httpx.Response(200, html=PAGE)

    async with mock_client(handler) as client:
        document = await fetcher(client).fetch("https://jane.dev/old")
    assert document.url == "https://jane.dev/new"

    def to_social(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_OPEN)
        return httpx.Response(302, headers={"location": "https://www.linkedin.com/in/jane"})

    async with mock_client(to_social) as client:
        with pytest.raises(SourceUnavailable, match="social network"):
            await fetcher(client).fetch("https://jane.dev/go")


async def test_redirect_to_a_private_address_is_refused():
    async def resolver(host):
        return ["10.0.0.1"] if host == "internal.example" else ["93.184.216.34"]

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_OPEN)
        return httpx.Response(302, headers={"location": "https://internal.example/admin"})

    async with mock_client(handler) as client:
        with pytest.raises(SourceUnavailable, match="non-public"):
            await fetcher(client, resolver).fetch("https://jane.dev/go")


async def test_redirect_loops_and_missing_targets_fail():
    def loop(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_OPEN)
        return httpx.Response(302, headers={"location": "/again"})

    def no_target(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_OPEN)
        return httpx.Response(302)

    async with mock_client(loop) as client:
        with pytest.raises(SourceUnavailable, match="too many times"):
            await fetcher(client).fetch("https://jane.dev/start")
    async with mock_client(no_target) as client:
        with pytest.raises(SourceUnavailable, match="did not name a target"):
            await fetcher(client).fetch("https://jane.dev/start")


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(404), "HTTP 404"),
        (
            httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"}),
            "unsupported content type",
        ),
        (httpx.Response(200, html="<html><script>x()</script></html>"), "no readable text"),
        (
            httpx.Response(200, content=b"x" * 2_000_000, headers={"content-type": "text/html"}),
            "oversized",
        ),
    ],
)
async def test_unusable_pages_are_reported(response, message):
    handler, _ = site({"/a": response})
    async with mock_client(handler) as client:
        with pytest.raises(SourceUnavailable, match=message):
            await fetcher(client).fetch("https://jane.dev/a")


async def test_streamed_bodies_without_content_length_are_capped():
    class Endless(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                yield b"x" * 100_000

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_OPEN)
        return httpx.Response(200, stream=Endless(), headers={"content-type": "text/html"})

    async with mock_client(handler) as client:
        with pytest.raises(SourceUnavailable, match="oversized"):
            await fetcher(client).fetch("https://jane.dev/a")


async def test_unknown_charsets_fall_back_to_utf8():
    page = httpx.Response(
        200,
        content="Jan Novak, inženýr".encode(),
        headers={"content-type": "text/plain; charset=x-made-up"},
    )
    handler, _ = site({"/a": page})
    async with mock_client(handler) as client:
        document = await fetcher(client).fetch("https://jan.dev/a")
    assert document.text == "Jan Novak, inženýr"


async def test_plain_text_and_network_errors():
    handler, _ = site(
        {"/a": httpx.Response(200, text="Plain bio text", headers={"content-type": "text/plain"})}
    )
    async with mock_client(handler) as client:
        document = await fetcher(client).fetch("https://jane.dev/a")
    assert document.text == "Plain bio text"
    assert document.title == "jane.dev"

    def refused(request):
        raise httpx.ConnectError("down", request=request)

    async with mock_client(refused) as client:
        with pytest.raises(SourceUnavailable, match="could not be reached"):
            await fetcher(client).fetch("https://jane.dev/a")


async def test_the_robots_agent_name_comes_from_the_configuration():
    robots = "User-agent: custom-bot\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
    handler, _ = site({"/a": httpx.Response(200, html=PAGE)}, robots=robots)
    async with mock_client(handler) as client:
        blocked = PageFetcher(
            client, TTLCache(60), FetchTuning(min_interval_seconds=0), "custom-bot", resolver=public
        )
        with pytest.raises(SourceUnavailable, match="disallows"):
            await blocked.fetch("https://jane.dev/a")
        allowed = fetcher(client)
        assert (await allowed.fetch("https://jane.dev/a")).title


async def test_size_text_redirect_and_port_limits_are_configurable():
    handler, _ = site(
        {"/big": httpx.Response(200, content=b"x" * 5000, headers={"content-type": "text/html"})}
    )
    async with mock_client(handler) as client:
        with pytest.raises(SourceUnavailable, match="oversized"):
            await fetcher(client, max_page_bytes=1024).fetch("https://jane.dev/big")
    handler, _ = site({"/a": httpx.Response(200, html=PAGE)})
    async with mock_client(handler) as client:
        short = await fetcher(client, max_text_chars=1000).fetch("https://jane.dev/a")
        assert short.text
    handler, _ = site({"/a": httpx.Response(200, html=PAGE)})
    async with mock_client(handler) as client:
        with pytest.raises(SourceUnavailable, match="non-standard port"):
            await fetcher(client).fetch("https://jane.dev:8443/a")
        allowed = fetcher(client, allowed_ports=(443, 8443))
        assert (await allowed.fetch("https://jane.dev:8443/a")).text


async def test_the_page_type_allow_list_is_configurable():
    pdf = httpx.Response(200, content=b"plain", headers={"content-type": "application/custom"})
    handler, _ = site({"/a": pdf})
    async with mock_client(handler) as client:
        with pytest.raises(SourceUnavailable, match="unsupported content type"):
            await fetcher(client).fetch("https://jane.dev/a")


async def test_dangerous_charsets_are_never_used_to_decode():
    body = ("a-" * 400_000).encode()
    page = httpx.Response(
        200, content=body, headers={"content-type": "text/plain; charset=punycode"}
    )
    handler, _ = site({"/a": page})
    async with mock_client(handler) as client:
        started = time.monotonic()
        document = await fetcher(client).fetch("https://jan.dev/a")
    assert time.monotonic() - started < 2
    assert document.text.startswith("a-a-")


def test_every_allowed_charset_is_a_real_canonical_codec_name():
    for name in FetchTuning().charsets:
        assert codecs.lookup(name).name == name


async def test_a_slow_server_is_cut_off_by_the_total_deadline():
    class Drip(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.05)
                yield b"x"

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_OPEN)
        return httpx.Response(200, stream=Drip(), headers={"content-type": "text/html"})

    async with mock_client(handler) as client:
        with pytest.raises(SourceUnavailable, match="took too long"):
            await fetcher(client, total_seconds=1.0, max_page_bytes=10_000_000).fetch(
                "https://jan.dev/slow"
            )


async def test_invalid_ports_are_reported_instead_of_crashing():
    handler, requests = site({})
    async with mock_client(handler) as client:
        with pytest.raises(SourceUnavailable, match="not a valid address"):
            await fetcher(client).fetch("https://jan.dev:99999999/a")
    assert requests == []


async def test_a_connection_that_lands_on_a_private_address_is_dropped():
    class Stream:
        def get_extra_info(self, name):
            return ("10.0.0.5", 443)

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_OPEN)
        return httpx.Response(
            200,
            text=PAGE,
            headers={"content-type": "text/html"},
            extensions={"network_stream": Stream()},
        )

    async with mock_client(handler) as client:
        with pytest.raises(SourceUnavailable, match="non-public"):
            await fetcher(client).fetch("https://rebind.example/a")
