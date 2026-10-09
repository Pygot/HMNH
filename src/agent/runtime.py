# src/agent/runtime.py
from agent.http import (
    build_client,
    Throttle,
    TTLCache,
    user_agent,
)
from agent.config import (
    LLMConfig,
    SearchConfig,
    Settings,
)
from agent.search import (
    ApifyGoogleSearch,
    BraveSearch,
    SearchProvider,
)
from agent.llm import (
    build_llm,
    StructuredLLM,
)
from agent.collectors import build_collectors
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from agent.errors import UpstreamError
from agent.discovery import Discovery
from agent.fetcher import PageFetcher
from agent.voice import VoiceService
from agent.apify import ApifyRunner
from agent.pipeline import Pipeline
from agent.chat import ChatEngine
from dataclasses import dataclass
from pydantic import BaseModel


class ServiceStatus(BaseModel):
    """A snapshot of the connected providers and the Apify budget."""

    llm_provider: str
    llm_model: str | None
    search_provider: str
    problems: list[str] = []
    unavailable_networks: list[str]
    apify_monthly_usage_usd: float | None = None
    apify_remaining_usd: float | None = None
    apify_error: str | None = None
    voice_enabled: bool = False


@dataclass(frozen=True)
class Runtime:
    """The connected services of one process, each None when it is not configured.

    The instance is immutable. The services share one HTTP client that is closed when
    the runtime context ends.
    """

    pipeline: Pipeline | None
    settings: Settings
    apify: ApifyRunner | None
    voice: VoiceService | None = None
    chat: ChatEngine | None = None

    async def status(self) -> ServiceStatus:
        """Report which services are connected and how much Apify budget is left.

        The Apify usage is fetched live. A failure of that call is reported in the apify_error
        field instead of being raised.

        Returns:
            The status with provider names, problems, unavailable networks and Apify usage.
        """
        llm = self.settings.llm_config_or_none()
        search = self.settings.search_config_or_none()
        problems = []
        if llm is None:
            problems.append("No language model is connected.")
        if search is None:
            problems.append("No search provider is connected.")
        status = ServiceStatus(
            llm_provider=llm.provider if llm else "none",
            llm_model=llm.model if llm else None,
            search_provider=search.provider if search else "none",
            problems=problems,
            unavailable_networks=(
                [network.value for network in self.pipeline.unavailable_networks]
                if self.pipeline
                else []
            ),
            voice_enabled=self.voice is not None,
        )
        if self.apify is None:
            return status
        try:
            usage = await self.apify.usage()
        except UpstreamError as error:
            return status.model_copy(update={"apify_error": str(error)})
        return status.model_copy(
            update={
                "apify_monthly_usage_usd": usage.monthly_usage_usd,
                "apify_remaining_usd": usage.remaining_usd,
            }
        )


@asynccontextmanager
async def open_runtime(settings: Settings, require_research: bool = True) -> AsyncIterator[Runtime]:
    """Build all services from the settings and keep them open while in use.

    The pipeline exists only when both a language model and a search provider are
    connected. The shared HTTP client is closed when the context exits.

    Args:
        settings: The application settings.
        require_research: When True, a missing language model or search provider is an
            error. When False, the runtime is built without whatever is missing.

    Returns:
        An async context manager that provides the runtime with the pipeline, Apify
        runner, voice service and chat engine.

    Raises:
        NotConfigured: when require_research is True and a provider is not set up.
        ConfigError: when the provider settings are inconsistent.
    """
    if require_research:
        llm_config: LLMConfig | None = settings.llm_config()
        search_config: SearchConfig | None = settings.search_config()
    else:
        llm_config = settings.llm_config_or_none()
        search_config = settings.search_config_or_none()
    ttl = settings.http.cache_ttl_seconds
    entries = settings.http.cache_max_entries
    agent = user_agent(settings.http.user_agent_name, settings.agent_contact)
    async with build_client(agent, settings.http) as client:

        def apify_runner(token) -> ApifyRunner:
            return ApifyRunner(
                client,
                token,
                TTLCache(ttl, entries),
                settings.endpoints,
                settings.http,
                settings.apify_max_charge_usd,
            )

        runner = apify_runner(settings.apify_token) if settings.apify_token is not None else None
        llm: StructuredLLM | None = None
        pipeline: Pipeline | None = None
        if llm_config is not None:
            llm = StructuredLLM(
                build_llm(
                    llm_config, client, settings.endpoints, settings.llm_tuning, settings.http
                ),
                settings.llm_tuning,
            )
        if llm is not None and search_config is not None:
            provider: SearchProvider
            if search_config.provider == "brave":
                provider = BraveSearch(
                    client,
                    search_config.secret,
                    TTLCache(ttl, entries),
                    Throttle(settings.http.brave_interval_seconds),
                    settings.endpoints,
                    settings.discovery,
                    settings.http,
                )
            else:
                provider = ApifyGoogleSearch(
                    apify_runner(search_config.secret),
                    settings.apify_google_actor,
                    settings.discovery,
                    settings.apify_max_pages_per_query,
                )
            # Without an Apify token the pipeline still runs, but it has no network collectors.
            collectors = build_collectors(settings, runner) if runner is not None else {}
            pipeline = Pipeline(
                llm=llm,
                discovery=Discovery(provider, settings.discovery),
                collectors=collectors,
                fetcher=PageFetcher(
                    client, TTLCache(ttl, entries), settings.fetch, settings.http.user_agent_name
                ),
                settings=settings,
            )
        voice = (
            VoiceService(
                client,
                settings.elevenlabs_api_key,
                settings.voice_id(),
                settings.endpoints,
                settings.voice,
                TTLCache(settings.voice.cache_ttl_seconds, settings.voice.cache_entries),
            )
            if settings.elevenlabs_api_key is not None
            else None
        )
        yield Runtime(
            pipeline=pipeline,
            settings=settings,
            apify=runner,
            voice=voice,
            chat=ChatEngine(llm, settings.chat) if llm is not None else None,
        )
