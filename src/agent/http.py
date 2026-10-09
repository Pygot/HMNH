# src/agent/http.py
from agent.errors import (
    AuthenticationError,
    ForbiddenHostError,
    RateLimitedError,
    UpstreamError,
)
from collections.abc import (
    Awaitable,
    Callable,
)
from agent.urls import social_network
from agent.config import HttpTuning
from agent import __version__
from typing import Any

import asyncio
import httpx
import time


def user_agent(name: str, contact: str | None) -> str:
    """Build the User-Agent header value for outgoing requests.

    Args:
        name: the product name placed first in the header.
        contact: optional contact text appended so site owners can reach the operator.

    Returns:
        The header text with the version, a robots.txt note and the contact if given.
    """
    base = f"{name}/{__version__} (automated; honours robots.txt"
    return f"{base}; contact: {contact})" if contact else f"{base})"


async def _reject_social_hosts(request: httpx.Request) -> None:
    """Refuse any request whose host is a social network.

    This runs as a request event hook, so social profiles can only be read through
    the configured Apify actors.

    Args:
        request: the outgoing request about to be sent.

    Raises:
        ForbiddenHostError: when the request targets a social network host.
    """
    host = request.url.host.lower().rstrip(".")
    if social_network(host) is not None:
        raise ForbiddenHostError(
            f"Direct requests to {host} are forbidden; social profiles are read only through "
            "the configured Apify actors."
        )


def build_client(
    agent: str, tuning: HttpTuning, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    """Create the shared asynchronous HTTP client.

    The client does not follow redirects, ignores proxy settings from the environment
    and refuses requests to social network hosts.

    Args:
        agent: the User-Agent header value.
        tuning: timeout and connection limit settings.
        transport: optional transport that replaces the network, mainly for tests.

    Returns:
        The configured client. The caller must close it.
    """
    return httpx.AsyncClient(
        headers={"User-Agent": agent},
        timeout=httpx.Timeout(tuning.timeout_seconds),
        # Redirects stay off so callers can vet every hop themselves.
        follow_redirects=False,
        trust_env=False,
        event_hooks={"request": [_reject_social_hosts]},
        limits=httpx.Limits(
            max_connections=tuning.max_connections,
            max_keepalive_connections=tuning.max_keepalive_connections,
        ),
        transport=transport,
    )


class TTLCache[T]:
    """An in-memory cache whose entries expire after a fixed time.

    When the cache is full the oldest inserted entry is dropped. There is no locking,
    so use it from a single event loop.
    """

    def __init__(
        self,
        ttl_seconds: float,
        max_entries: int = 512,
        clock: Callable[[], float] = time.monotonic,
    ):
        """Initialize the cache.

        Args:
            ttl_seconds: lifetime of an entry in seconds. Zero or less disables storing.
            max_entries: largest number of entries kept at once.
            clock: function returning the current time in seconds, replaceable in tests.
        """
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._entries: dict[str, tuple[float, T]] = {}

    def get(self, key: str) -> T | None:
        """Return a cached value if it is still fresh.

        Args:
            key: the cache key.

        Returns:
            The stored value, or None when it is missing or has expired. An expired entry
            is removed.
        """
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._clock() >= expires_at:
            del self._entries[key]
            return None
        return value

    def clear(self) -> None:
        """Remove every entry from the cache."""
        self._entries.clear()

    def put(self, key: str, value: T) -> None:
        """Store a value, evicting the oldest entries when the cache is full.

        Storing an existing key replaces it and makes it the newest entry. Nothing is
        stored when the lifetime is zero or less.

        Args:
            key: the cache key.
            value: the value to keep.
        """
        if self._ttl <= 0:
            return
        self._entries.pop(key, None)
        # A dict keeps insertion order, so the first key is always the oldest entry.
        while len(self._entries) >= self._max_entries:
            del self._entries[next(iter(self._entries))]
        self._entries[key] = (self._clock() + self._ttl, value)


class Throttle:
    """A gate that keeps successive calls at least a minimum time apart.

    Each caller reserves the next free slot before sleeping, so concurrent callers
    are queued one after another instead of waking together.
    """

    def __init__(
        self,
        min_interval: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        """Initialize the throttle.

        Args:
            min_interval: smallest gap between two calls, in seconds.
            clock: function returning the current time in seconds, replaceable in tests.
            sleep: coroutine function that waits for the given seconds.
        """
        self._min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._ready_at = 0.0

    async def wait(self) -> None:
        """Wait until the next call slot is free, then reserve the slot after it."""
        now = self._clock()
        start = max(now, self._ready_at)
        self._ready_at = start + self._min_interval
        if start > now:
            await self._sleep(start - now)


def check_response(response: httpx.Response, service: str) -> None:
    """Raise a project error when a response is not successful.

    Args:
        response: the HTTP response to inspect.
        service: the service name used in the error message.

    Raises:
        AuthenticationError: when the status is 401 or 403.
        RateLimitedError: when the status is 429. It carries the Retry-After seconds.
        UpstreamError: when the status is 402 or any other failure.
    """
    if response.is_success:
        return
    status = response.status_code
    if status in {401, 403}:
        raise AuthenticationError(f"{service} rejected the credentials (HTTP {status}).")
    if status == 402:
        raise UpstreamError(
            f"{service} reports exhausted credits or a missing plan (HTTP 402){_problem(response)}."
        )
    if status == 429:
        retry_after = _retry_after(response)
        suffix = f" Retry after {retry_after:g}s." if retry_after is not None else ""
        raise RateLimitedError(f"{service} rate limit reached (HTTP 429).{suffix}", retry_after)
    raise UpstreamError(f"{service} returned HTTP {status}{_problem(response)}.")


def _retry_after(response: httpx.Response) -> float | None:
    """Read the Retry-After header as a number of seconds.

    Args:
        response: the HTTP response.

    Returns:
        The delay in seconds, or None when the header is absent or not a number.
    """
    try:
        return float(response.headers["Retry-After"])
    except KeyError, ValueError:
        return None


def _problem(response: httpx.Response) -> str:
    """Extract a short error detail from a JSON error body.

    Args:
        response: the failed HTTP response.

    Returns:
        A colon and the detail, with whitespace collapsed and cut to 200 characters,
        or an empty string when the body has no readable error message.
    """
    try:
        body = response.json()
        detail = body["error"]["message"] if "error" in body else body["detail"]
        if isinstance(detail, dict):
            detail = detail["message"]
    except ValueError, KeyError, TypeError:
        return ""
    return f": {' '.join(str(detail).split())[:200]}"


async def api_request(
    client: httpx.AsyncClient,
    service: str,
    method: str,
    url: str,
    *,
    time_limit: float,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json: Any = None,
    data: dict[str, str] | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
) -> httpx.Response:
    """Send an HTTP request and turn every failure into a project error.

    Args:
        client: the HTTP client to use.
        service: the service name used in error messages.
        method: the HTTP method.
        url: the full request URL.
        time_limit: timeout for this request, in seconds.
        headers: optional request headers.
        params: optional query parameters.
        json: optional body sent as JSON.
        data: optional form fields.
        files: optional multipart files mapped to (file name, bytes, content type).

    Returns:
        The successful response.

    Raises:
        UpstreamError: on a timeout, a transport failure or an error status.
        AuthenticationError: when the service rejects the credentials.
        RateLimitedError: when the service reports a rate limit.
    """
    try:
        response = await client.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json,
            data=data,
            files=files,
            timeout=time_limit,
        )
    except httpx.TimeoutException as error:
        raise UpstreamError(f"{service} timed out.") from error
    except httpx.HTTPError as error:
        raise UpstreamError(f"{service} request failed ({type(error).__name__}).") from error
    check_response(response, service)
    return response


async def api_json(
    client: httpx.AsyncClient,
    service: str,
    method: str,
    url: str,
    *,
    time_limit: float,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json: Any = None,
    data: dict[str, str] | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
) -> Any:
    """Send an HTTP request and return the decoded JSON body.

    Args:
        client: the HTTP client to use.
        service: the service name used in error messages.
        method: the HTTP method.
        url: the full request URL.
        time_limit: timeout for this request, in seconds.
        headers: optional request headers.
        params: optional query parameters.
        json: optional body sent as JSON.
        data: optional form fields.
        files: optional multipart files mapped to (file name, bytes, content type).

    Returns:
        The parsed JSON value.

    Raises:
        UpstreamError: when the request fails or the body is not valid JSON.
        AuthenticationError: when the service rejects the credentials.
        RateLimitedError: when the service reports a rate limit.
    """
    response = await api_request(
        client,
        service,
        method,
        url,
        time_limit=time_limit,
        headers=headers,
        params=params,
        json=json,
        data=data,
        files=files,
    )
    try:
        return response.json()
    except ValueError as error:
        raise UpstreamError(f"{service} returned invalid JSON.") from error
