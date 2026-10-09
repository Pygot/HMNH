# src/agent/apify.py
from pydantic import (
    BaseModel,
    SecretStr,
    ValidationError,
)
from agent.config import (
    Endpoints,
    HttpTuning,
)
from agent.http import (
    api_json,
    TTLCache,
)
from agent.errors import UpstreamError
from typing import Any

import hashlib
import httpx
import json


class ApifyUsage(BaseModel):
    """The monthly usage and limits of an Apify account, in US dollars and jobs."""

    monthly_usage_usd: float
    max_monthly_usage_usd: float
    max_concurrent_jobs: int

    @property
    def remaining_usd(self) -> float:
        """Return the monthly budget still available in US dollars, never below zero."""
        return max(0.0, self.max_monthly_usage_usd - self.monthly_usage_usd)


class ApifyRunner:
    """A client that runs Apify actors synchronously and caches their results.

    Every run is capped by the configured maximum charge in US dollars, and the token
    is sent as a bearer header.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        token: SecretStr,
        cache: TTLCache[list[dict[str, Any]]],
        endpoints: Endpoints,
        http: HttpTuning,
        max_charge_usd: float,
    ):
        """Initialize the runner.

        Args:
            client: the HTTP client used for requests.
            token: the Apify API token.
            cache: shared cache of actor results.
            endpoints: supplies the Apify API base URL.
            http: timeout settings, including the actor wait time.
            max_charge_usd: cost cap, in US dollars, sent with every actor run.
        """
        self._client = client
        self._token = token
        self._cache = cache
        self._base = endpoints.apify_api
        self._http = http
        self._max_charge = max_charge_usd

    def _auth(self) -> dict[str, str]:
        """Build the authorization header that carries the API token.

        Returns:
            A mapping with the bearer Authorization header.
        """
        return {"Authorization": f"Bearer {self._token.get_secret_value()}"}

    async def run(self, actor_id: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        # Sorted keys make payloads that differ only in key order share one cache entry.
        """Run an actor and return the items it produced.

        Results are cached by actor id and a hash of the payload, so an identical run
        within the cache lifetime costs nothing.

        Args:
            actor_id: the actor in username/actor-name form.
            payload: the input passed to the actor.

        Returns:
            The dataset items, each a dictionary.

        Raises:
            UpstreamError: when the call fails or the answer is not a list of objects.
            AuthenticationError: when Apify rejects the token.
            RateLimitedError: when Apify reports a rate limit.
        """
        # Sorted keys make payloads that differ only in key order share one cache entry.
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        key = f"apify:{actor_id}:{digest}"
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        data = await api_json(
            self._client,
            f"Apify actor {actor_id}",
            "POST",
            # The API path needs the actor id written with a tilde instead of a slash.
            f"{self._base}/acts/{actor_id.replace('/', '~')}/run-sync-get-dataset-items",
            headers=self._auth(),
            params={
                "format": "json",
                "clean": "true",
                "timeout": self._http.apify_wait_seconds,
                "maxTotalChargeUsd": self._max_charge,
            },
            json=payload,
            time_limit=self._http.apify_timeout_seconds,
        )
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise UpstreamError(f"Apify actor {actor_id} returned an unexpected response shape.")
        self._cache.put(key, data)
        return data

    async def usage(self) -> ApifyUsage:
        """Fetch the account's monthly usage and limits.

        Returns:
            The current usage, the monthly limit and the concurrent job limit.

        Raises:
            UpstreamError: when the call fails or the answer has an unexpected shape.
            AuthenticationError: when Apify rejects the token.
        """
        data = await api_json(
            self._client,
            "Apify account",
            "GET",
            f"{self._base}/users/me/limits",
            headers=self._auth(),
            time_limit=self._http.timeout_seconds,
        )
        try:
            return ApifyUsage(
                monthly_usage_usd=data["data"]["current"]["monthlyUsageUsd"],
                max_monthly_usage_usd=data["data"]["limits"]["maxMonthlyUsageUsd"],
                max_concurrent_jobs=data["data"]["limits"]["maxConcurrentActorJobs"],
            )
        except (KeyError, TypeError, ValidationError) as error:
            raise UpstreamError("Apify account returned an unexpected response shape.") from error
