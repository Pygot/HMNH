# src/agent/search.py
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
)
from agent.config import (
    DiscoveryTuning,
    Endpoints,
    HttpTuning,
)
from agent.http import (
    api_json,
    Throttle,
    TTLCache,
)
from agent.errors import UpstreamError
from collections.abc import Iterable
from agent.apify import ApifyRunner
from agent.models import SearchHit
from typing import Protocol

import httpx
import html
import re

TAGS = re.compile(r"<[^>]+>")


class SearchProvider(Protocol):
    """A web search backend that returns result links for a text query.

    Implementations expose the longest query they accept.
    """

    max_query_words: int
    max_query_chars: int

    async def search(self, query: str, count: int) -> list[SearchHit]:
        """Run one web search.

        Args:
            query: The search text.
            count: Maximum number of hits to return.

        Returns:
            The hits in ranked order.
        """
        ...


def _plain(text: str) -> str:
    """Strip HTML tags and entities and collapse whitespace.

    Args:
        text: Text that may contain markup.

    Returns:
        Plain single-line text.
    """
    return " ".join(html.unescape(TAGS.sub("", text)).split())


def _check_query(provider: SearchProvider, query: str) -> None:
    """Check that a query fits the limits of a search provider.

    Args:
        provider: The provider whose limits apply.
        query: The search text.

    Raises:
        ValueError: When the query has too many characters or words.
    """
    if len(query) > provider.max_query_chars or len(query.split()) > provider.max_query_words:
        raise ValueError(
            f"search query exceeds {provider.max_query_chars} characters or "
            f"{provider.max_query_words} words"
        )


class _BraveResult(BaseModel):
    """One web result in a Brave Search response."""

    url: str
    title: str = ""
    description: str = ""


class _BraveWeb(BaseModel):
    """The web results section of a Brave Search response."""

    results: list[_BraveResult] = Field(default_factory=list)


class _BraveResponse(BaseModel):
    """The part of a Brave Search response that this client reads."""

    web: _BraveWeb | None = None


class BraveSearch:
    """A search provider backed by the Brave Search API.

    Results are cached by query and count, and requests are throttled.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: SecretStr,
        cache: TTLCache[list[SearchHit]],
        throttle: Throttle,
        endpoints: Endpoints,
        discovery: DiscoveryTuning,
        http: HttpTuning,
    ):
        """Initialize the Brave Search client.

        Args:
            client: The shared HTTP client.
            api_key: The Brave subscription token.
            cache: Cache of earlier results.
            throttle: Rate limiter applied before each request.
            endpoints: Configured service URLs.
            discovery: Query limits and the largest result count.
            http: HTTP settings such as the request timeout.
        """
        self._client = client
        self._api_key = api_key
        self._cache = cache
        self._throttle = throttle
        self._url = endpoints.brave_search
        self._max_count = discovery.brave_max_count
        self._time_limit = http.timeout_seconds
        self.max_query_chars = discovery.max_query_chars
        self.max_query_words = discovery.brave_max_query_words

    async def search(self, query: str, count: int) -> list[SearchHit]:
        """Search the web with Brave.

        The count is capped at the configured maximum, and a cached answer is
        returned when one exists.

        Args:
            query: The search text.
            count: The most results to return.

        Returns:
            The search hits, with markup removed from titles and snippets.

        Raises:
            ValueError: When the query is too long.
            UpstreamError: When the response does not have the expected shape.
        """
        _check_query(self, query)
        count = min(count, self._max_count)
        # The count is part of the key because it is sent to the API and changes the result list.
        key = f"brave:{count}:{query}"
        cached = self._cache.get(key)
        if cached is not None:
            return list(cached)
        await self._throttle.wait()
        data = await api_json(
            self._client,
            "Brave Search API",
            "GET",
            self._url,
            headers={
                "X-Subscription-Token": self._api_key.get_secret_value(),
                "Accept": "application/json",
            },
            params={"q": query, "count": count},
            time_limit=self._time_limit,
        )
        try:
            parsed = _BraveResponse.model_validate(data)
        except ValidationError as error:
            raise UpstreamError(
                "Brave Search API returned an unexpected response shape."
            ) from error
        results = parsed.web.results if parsed.web else []
        hits = _to_hits((r.url, r.title, r.description) for r in results)
        self._cache.put(key, hits)
        return list(hits)


class _Organic(BaseModel):
    """One organic result in a Google search actor response."""

    url: str
    title: str = ""
    description: str = ""


class _GoogleItem(BaseModel):
    """One result page returned by the Google search actor."""

    model_config = ConfigDict(populate_by_name=True)

    organic_results: list[_Organic] = Field(default_factory=list, alias="organicResults")


class ApifyGoogleSearch:
    """A search provider that runs a Google search actor on Apify."""

    def __init__(
        self, runner: ApifyRunner, actor_id: str, discovery: DiscoveryTuning, max_pages: int
    ):
        """Initialize the Google search client.

        Args:
            runner: The Apify actor runner.
            actor_id: The id of the Google search actor.
            discovery: Query limits.
            max_pages: The most result pages to fetch per query.
        """
        self._runner = runner
        self._actor_id = actor_id
        self._max_pages = max_pages
        self.max_query_chars = discovery.max_query_chars
        self.max_query_words = discovery.apify_max_query_words

    async def search(self, query: str, count: int) -> list[SearchHit]:
        """Search the web with the Google search actor.

        Args:
            query: The search text.
            count: The most results to return.

        Returns:
            The first count hits, with markup removed from titles and snippets.

        Raises:
            ValueError: When the query is too long.
            UpstreamError: When the actor output does not have the expected shape.
        """
        _check_query(self, query)
        items = await self._runner.run(
            self._actor_id, {"queries": query, "maxPagesPerQuery": self._max_pages}
        )
        try:
            pages = [_GoogleItem.model_validate(item) for item in items]
        except ValidationError as error:
            raise UpstreamError(
                "Google search actor returned an unexpected response shape."
            ) from error
        organic = [r for page in pages for r in page.organic_results]
        return _to_hits((r.url, r.title, r.description) for r in organic)[:count]


def _to_hits(rows: Iterable[tuple[str, str, str]]) -> list[SearchHit]:
    """Convert raw result rows into search hits.

    Rows that fail validation, such as ones with a bad URL, are skipped.

    Args:
        rows: Tuples of URL, title and snippet.

    Returns:
        The valid hits, in order.
    """
    hits = []
    for url, title, snippet in rows:
        try:
            hits.append(SearchHit(url=url, title=_plain(title), snippet=_plain(snippet)))
        except ValidationError:
            continue
    return hits
