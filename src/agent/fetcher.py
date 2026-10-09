# src/agent/fetcher.py
from agent.models import (
    Network,
    SourceDocument,
    utcnow,
)
from urllib.parse import (
    urljoin,
    urlsplit,
    urlunsplit,
)
from agent.http import (
    Throttle,
    TTLCache,
)
from agent.urls import (
    hostname,
    social_network,
)
from collections.abc import (
    Awaitable,
    Callable,
)
from urllib.robotparser import RobotFileParser
from agent.errors import SourceUnavailable
from agent.html_text import extract_text
from agent.config import FetchTuning
from dataclasses import dataclass
from datetime import datetime

import ipaddress
import asyncio
import codecs
import socket
import httpx
import re

CHARSET = re.compile(r"charset=([\w-]+)", re.IGNORECASE)
BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(network) for network in ("64:ff9b::/96", "2002::/16", "2001::/32")
)
Resolver = Callable[[str], Awaitable[list[str]]]


def public_address(raw: str) -> bool:
    """Check whether an IP address string is publicly routable.

    IPv4-mapped IPv6 addresses are checked as their IPv4 form. NAT64, 6to4 and Teredo
    ranges are rejected because they can wrap a private address.

    Args:
        raw: the address text.

    Returns:
        True for a valid global address outside the blocked ranges, False otherwise.
    """
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    blocked = any(ip in network for network in BLOCKED_NETWORKS if network.version == ip.version)
    return ip.is_global and not blocked


async def resolve_host(host: str) -> list[str]:
    """Resolve a host name to its IP addresses without blocking the event loop.

    Args:
        host: the host name to look up.

    Returns:
        The resolved addresses as text, for stream sockets.

    Raises:
        OSError: when the lookup fails.
    """
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


@dataclass(frozen=True)
class _Fetched:
    """A raw HTTP answer reduced to status, body, content type and final URL."""

    status: int
    body: bytes
    content_type: str
    url: str


def _decode(body: bytes, content_type: str, allowed: tuple[str, ...]) -> str:
    """Decode response bytes with a charset taken from an allow list.

    The charset comes from the Content-Type header. Missing, unknown or disallowed
    charsets fall back to UTF-8, and undecodable bytes are replaced.

    Args:
        body: the raw bytes.
        content_type: the Content-Type header, possibly with a charset parameter.
        allowed: codec names that may be used.

    Returns:
        The decoded text.
    """
    match = CHARSET.search(content_type)
    try:
        charset = codecs.lookup(match.group(1) if match else "utf-8").name
    except LookupError:
        charset = "utf-8"
    return body.decode(charset if charset in allowed else "utf-8", errors="replace")


class PageFetcher:
    """A polite, guarded fetcher that turns web pages into source documents.

    Every hop is checked for scheme, port, social hosts, public DNS answers and the
    address actually connected to. Redirects are followed manually, robots.txt is
    obeyed, requests to one host are spaced, and size and total time are capped.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        cache: TTLCache[SourceDocument],
        tuning: FetchTuning,
        agent_name: str,
        *,
        resolver: Resolver = resolve_host,
        now: Callable[[], datetime] = utcnow,
    ):
        """Initialize the fetcher.

        Args:
            client: the HTTP client used for all requests.
            cache: shared cache of fetched documents, keyed by requested URL.
            tuning: size, time, port and charset limits.
            agent_name: product token matched against the robots.txt rules.
            resolver: coroutine that returns the IP addresses of a host, replaceable in tests.
            now: function returning the current time, replaceable in tests.
        """
        self._client = client
        self._cache = cache
        self._tuning = tuning
        self._agent_name = agent_name
        self._resolver = resolver
        self._now = now
        self._throttles: dict[str, Throttle] = {}
        self._robots: TTLCache[RobotFileParser] = TTLCache(tuning.robots_ttl_seconds)

    async def fetch(self, url: str) -> SourceDocument:
        """Fetch a web page and return it as a source document.

        Args:
            url: the page address.

        Returns:
            A document with the title and readable text. It is cached under the requested
            URL, and its own URL is the final one after redirects.

        Raises:
            SourceUnavailable: when the target is not allowed, robots.txt forbids it, the
                server fails or is too slow, the body is too big, the content type is
                unsupported or no readable text is found.
        """
        cached = self._cache.get(url)
        if cached is not None:
            return cached
        result = await self._request(url, self._tuning.max_page_bytes, enforce_robots=True)
        if result.status >= 400:
            raise SourceUnavailable(f"{hostname(url)} answered HTTP {result.status}.")
        content_type = result.content_type.split(";")[0].strip().lower()
        if content_type not in self._tuning.page_types:
            raise SourceUnavailable(f"{hostname(url)} served an unsupported content type.")
        text = await asyncio.to_thread(
            _decode, result.body, result.content_type, self._tuning.charsets
        )
        limit = self._tuning.max_text_chars
        if content_type == "text/plain":
            title, body = "", text[:limit]
        else:
            title, body = extract_text(text, limit)
        if not body.strip():
            raise SourceUnavailable(f"{hostname(url)} returned no readable text.")
        document = SourceDocument(
            url=result.url,
            network=Network.WEB,
            title=title or hostname(result.url),
            text=body,
            retrieved_at=self._now(),
        )
        self._cache.put(url, document)
        return document

    async def _request(self, url: str, max_bytes: int, *, enforce_robots: bool) -> _Fetched:
        """Fetch a URL within the overall time limit.

        Args:
            url: the address to request.
            max_bytes: largest body accepted, in bytes.
            enforce_robots: whether robots.txt must allow the request.

        Returns:
            The final response.

        Raises:
            SourceUnavailable: when the time limit is exceeded or the fetch fails.
        """
        try:
            async with asyncio.timeout(self._tuning.total_seconds):
                return await self._follow(url, max_bytes, enforce_robots=enforce_robots)
        except TimeoutError as error:
            raise SourceUnavailable(f"{hostname(url)} took too long to answer.") from error

    async def _follow(self, url: str, max_bytes: int, *, enforce_robots: bool) -> _Fetched:
        """Request a URL and follow its redirects, checking every hop.

        The body is read in chunks and abandoned as soon as it exceeds the size limit.

        Args:
            url: the address to start from.
            max_bytes: largest body accepted, in bytes.
            enforce_robots: whether robots.txt must allow each hop.

        Returns:
            The first non-redirect response with its body and final URL.

        Raises:
            SourceUnavailable: when a hop is not allowed, a redirect has no target, there
                are too many redirects, the body is too big or the host cannot be reached.
        """
        current = url
        for _ in range(self._tuning.max_redirects + 1):
            await self._check_target(current)
            if enforce_robots:
                await self._check_robots(current)
            await self._throttle(hostname(current)).wait()
            try:
                async with self._client.stream(
                    "GET", current, headers={"Accept": "text/html,text/plain;q=0.9"}
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise SourceUnavailable("A redirect did not name a target.")
                        current = urljoin(current, location)
                        continue
                    self._check_peer(response, current)
                    declared = response.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > max_bytes:
                        raise SourceUnavailable(f"{hostname(current)} served an oversized body.")
                    body = bytearray()
                    # The body is capped while streaming, so a missing or false Content-Length
                    # cannot exhaust memory.
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise SourceUnavailable(
                                f"{hostname(current)} served an oversized body."
                            )
                    return _Fetched(
                        response.status_code,
                        bytes(body),
                        response.headers.get("content-type", ""),
                        current,
                    )
            except httpx.HTTPError as error:
                raise SourceUnavailable(
                    f"{hostname(current)} could not be reached ({type(error).__name__})."
                ) from error
        raise SourceUnavailable(f"{hostname(url)} redirected too many times.")

    def _check_peer(self, response: httpx.Response, url: str) -> None:
        """Reject a response that came from a non-public address.

        The check is skipped when the transport does not expose the peer address.

        Args:
            response: the received response.
            url: the requested address, used in the error message.

        Raises:
            SourceUnavailable: when the connected address is not public.
        """
        stream = response.extensions.get("network_stream")
        # This checks the address actually connected to, which catches DNS answers that changed
        # after the pre-check.
        peer = stream.get_extra_info("server_addr") if stream is not None else None
        if peer and not public_address(str(peer[0])):
            raise SourceUnavailable(f"{hostname(url)} connected to a non-public address.")

    def _throttle(self, host: str) -> Throttle:
        """Return the throttle for a host, creating it on first use.

        Args:
            host: the host name.

        Returns:
            The throttle that spaces requests to that host.
        """
        if host not in self._throttles:
            self._throttles[host] = Throttle(self._tuning.min_interval_seconds)
        return self._throttles[host]

    async def _check_target(self, url: str) -> None:
        """Check that a URL is safe to request.

        Every address the host resolves to must be public.

        Args:
            url: the address to check.

        Raises:
            SourceUnavailable: when the scheme is not http or https, the port is invalid or
                not allowed, the host is a social network, the name does not resolve or an
                address is not public.
        """
        parts = urlsplit(url)
        host = hostname(url)
        if parts.scheme not in {"http", "https"} or not host:
            raise SourceUnavailable("Only http(s) URLs can be fetched.")
        try:
            port = parts.port
        except ValueError as error:
            raise SourceUnavailable(f"{host} is not a valid address.") from error
        if port is not None and port not in self._tuning.allowed_ports:
            raise SourceUnavailable(f"{host} uses a non-standard port.")
        if social_network(host) is not None:
            raise SourceUnavailable(
                f"{host} is a social network and is only read through the configured actors."
            )
        try:
            addresses = await self._resolver(host)
        except OSError as error:
            raise SourceUnavailable(f"{host} could not be resolved.") from error
        if not addresses:
            raise SourceUnavailable(f"{host} could not be resolved.")
        if not all(public_address(address) for address in addresses):
            raise SourceUnavailable(f"{host} resolves to a non-public address.")

    async def _check_robots(self, url: str) -> None:
        """Check the robots.txt rules of a URL's origin, loading and caching them.

        Args:
            url: the address that is about to be requested.

        Raises:
            SourceUnavailable: when the rules forbid this page or cannot be loaded.
        """
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        rules = self._robots.get(origin)
        if rules is None:
            rules = await self._load_robots(origin)
            self._robots.put(origin, rules)
        if not rules.can_fetch(self._agent_name, url):
            raise SourceUnavailable(f"robots.txt of {hostname(url)} disallows fetching this page.")

    async def _load_robots(self, origin: str) -> RobotFileParser:
        """Download and parse the robots.txt file of an origin.

        A missing file (a 4xx answer other than 401 or 403) allows everything.

        Args:
            origin: the scheme and host, for example https://example.com.

        Returns:
            The parsed rules.

        Raises:
            SourceUnavailable: when the file cannot be fetched or answers 401, 403 or
                any status that is not a success or a 4xx.
        """
        robots_url = urlunsplit((*urlsplit(origin)[:2], "/robots.txt", "", ""))
        result = await self._request(
            robots_url, self._tuning.max_robots_bytes, enforce_robots=False
        )
        rules = RobotFileParser()
        if result.status == 200:
            decoded = await asyncio.to_thread(
                _decode, result.body, result.content_type, self._tuning.charsets
            )
            rules.parse(decoded.splitlines())
        # Only a plain missing file allows everything. Access errors and server errors fail closed.
        elif result.status not in {401, 403} and 400 <= result.status < 500:
            rules.allow_all = True
        else:
            raise SourceUnavailable(
                f"robots.txt of {hostname(origin)} is unavailable (HTTP {result.status})."
            )
        rules.modified()
        return rules
