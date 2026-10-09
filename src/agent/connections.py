# src/agent/connections.py
from agent.envfile import (
    env_source,
    env_target,
    load_settings,
    overridden,
    write_values,
)
from agent.errors import (
    AgentError,
    ConfigError,
)
from agent.forms import (
    Field,
    Section,
)
from agent.http import (
    build_client,
    user_agent,
)
from pydantic import (
    BaseModel,
    ValidationError,
)
from contextlib import AbstractAsyncContextManager
from agent.text import describe_errors
from agent.search import BraveSearch
from agent.voice import VoiceService
from collections.abc import Callable
from agent.apify import ApifyRunner
from agent.config import Settings
from agent.http import TTLCache
from agent.http import Throttle
from agent.llm import build_llm

import threading
import httpx

ClientFactory = Callable[[Settings], AbstractAsyncContextManager[httpx.AsyncClient]]
APIFY_HINT = (
    " The Apify AI gateway may only accept calls made from inside the Apify platform, "
    "so add a key from Anthropic, OpenAI or another provider if this keeps failing."
)


SECTIONS = (
    Section(
        "llm",
        "Language model",
        "Reads pages and writes the reports. Use your own key, a local Ollama, or just Apify.",
        (
            Field(
                "LLM_PROVIDER",
                "Provider",
                "Leave on automatic to decide from the other values.",
                choices=(
                    ("", "Automatic"),
                    ("anthropic", "Anthropic"),
                    ("openai", "OpenAI or compatible"),
                    ("ollama", "Ollama on your machine"),
                    ("apify", "Apify AI (uses your Apify token)"),
                    ("claude", "Claude login (the claude command on this computer)"),
                ),
            ),
            Field(
                "LLM_API_KEY",
                "API key",
                "From your Anthropic, OpenAI or compatible account.",
                secret=True,
            ),
            Field(
                "LLM_MODEL",
                "Model",
                "Optional, a sensible default is used.",
                placeholder="claude-sonnet-5-5",
            ),
            Field(
                "LLM_BASE_URL",
                "Service address",
                "Only for an OpenAI compatible service.",
                placeholder="https://api.example.com/v1",
            ),
            Field(
                "LLM_OLLAMA_SCHEME",
                "Ollama scheme",
                choices=(("", "Default"), ("http", "http"), ("https", "https")),
            ),
            Field("LLM_OLLAMA_HOST", "Ollama host", placeholder="localhost"),
            Field("LLM_OLLAMA_PORT", "Ollama port", placeholder="11434"),
        ),
    ),
    Section(
        "apify",
        "Apify",
        "Reads social profiles, can run the search and can host the language model.",
        (
            Field(
                "APIFY_TOKEN",
                "Apify token",
                "From your Apify console, under Integrations.",
                secret=True,
            ),
        ),
    ),
    Section(
        "search",
        "Web search",
        "Finds the public pages about a person.",
        (
            Field(
                "SEARCH_PROVIDER",
                "Provider",
                "Leave on automatic to use Brave when a key exists, otherwise Apify.",
                choices=(
                    ("", "Automatic"),
                    ("brave", "Brave Search"),
                    ("apify", "Google through Apify"),
                ),
            ),
            Field("BRAVE_API_KEY", "Brave Search key", secret=True),
        ),
    ),
    Section(
        "voice",
        "Voice",
        "Lets Tie and the chat speak and listen.",
        (
            Field("ELEVENLABS_API_KEY", "ElevenLabs key", secret=True),
            Field("ELEVENLABS_VOICE_ID", "Voice id", "Optional."),
        ),
    ),
    Section(
        "mail",
        "Email",
        "Sends sign in codes, invitations and the messages you approve.",
        (
            Field("SMTP_HOST", "Server", placeholder="smtp.example.com"),
            Field("SMTP_PORT", "Port", placeholder="587"),
            Field(
                "SMTP_SECURITY",
                "Security",
                choices=(("starttls", "STARTTLS"), ("ssl", "SSL"), ("none", "None")),
            ),
            Field("SMTP_USERNAME", "Username"),
            Field("SMTP_PASSWORD", "Password", secret=True),
            Field("SMTP_FROM", "Sender address", placeholder="hiring@example.com"),
            Field("SMTP_FROM_NAME", "Sender name", placeholder="Acme hiring"),
        ),
    ),
    Section(
        "agent",
        "Identity",
        "Websites see this contact when the agent reads their public pages.",
        (
            Field(
                "AGENT_CONTACT",
                "Contact",
                "An email address or a page about you.",
                placeholder="hiring@example.com",
            ),
        ),
    ),
)
BY_NAME = {section.name: section for section in SECTIONS}
EDITABLE = frozenset(field.key for section in SECTIONS for field in section.fields)
SECRET_KEYS = frozenset(
    field.key for section in SECTIONS for field in section.fields if field.secret
)
TARGETS = ("llm", "apify", "search", "voice")
BOUND_SECRETS = {
    "LLM_API_KEY": ("LLM_PROVIDER", "LLM_BASE_URL"),
    "SMTP_PASSWORD": ("SMTP_HOST", "SMTP_PORT", "SMTP_SECURITY", "SMTP_USERNAME"),
}


class Check(BaseModel):
    """The result of testing one external connection."""

    target: str
    ok: bool
    message: str


def attribute(key: str) -> str:
    """Return the settings attribute name for an environment variable key.

    Args:
        key: Environment variable name such as LLM_API_KEY.

    Returns:
        The lower case attribute name on the settings object.
    """
    return key.lower()


def default_client(settings: Settings) -> AbstractAsyncContextManager[httpx.AsyncClient]:
    """Open an HTTP client that identifies the agent and its contact address.

    Args:
        settings: Application settings giving the user agent name, contact and limits.

    Returns:
        An async context manager that yields the configured client.
    """
    return build_client(
        user_agent(settings.http.user_agent_name, settings.agent_contact), settings.http
    )


class ConnectionChecker:
    """Tests saved credentials by making one small call to each external service."""

    def __init__(self, open_client: ClientFactory = default_client):
        """Initialize the checker.

        Args:
            open_client: Factory that returns an async context manager yielding an HTTP
                client for the given settings; tests can replace it.
        """
        self._open = open_client

    async def check(self, target: str, settings: Settings) -> Check:
        """Test one connection and report the result instead of raising.

        Any agent error becomes a failed check carrying its message. When the language
        model runs through Apify, a hint about the Apify gateway is appended.

        Args:
            target: Which connection to test: llm, apify, search or voice.
            settings: Settings holding the credentials to test.

        Returns:
            The check with a success flag and a human readable message.
        """
        try:
            async with self._open(settings) as client:
                message = await {
                    "llm": self._llm,
                    "apify": self._apify,
                    "search": self._search,
                    "voice": self._voice,
                }[target](client, settings)
        except AgentError as error:
            config = settings.llm_config_or_none() if target == "llm" else None
            hint = APIFY_HINT if config is not None and config.provider == "apify" else ""
            return Check(target=target, ok=False, message=f"{error}{hint}")
        return Check(target=target, ok=True, message=message)

    async def _llm(self, client: httpx.AsyncClient, settings: Settings) -> str:
        """Send a tiny test prompt to the configured language model.

        Args:
            client: HTTP client to use.
            settings: Settings holding the model configuration.

        Returns:
            A message naming the provider and model.
        """
        config = settings.llm_config()
        model = build_llm(config, client, settings.endpoints, settings.llm_tuning, settings.http)
        await model.complete(settings.llm_tuning.ping_system, settings.llm_tuning.ping_user)
        return f"Connected to {config.provider}, model {config.model or 'default'}."

    async def _apify(self, client: httpx.AsyncClient, settings: Settings) -> str:
        """Check the Apify token by reading the account usage.

        Args:
            client: HTTP client to use.
            settings: Settings holding the Apify token and spending limit.

        Returns:
            A message with the remaining monthly allowance in USD.

        Raises:
            ConfigError: when no Apify token is saved.
        """
        if settings.apify_token is None:
            raise ConfigError("No Apify token is saved.")
        runner = ApifyRunner(
            client,
            settings.apify_token,
            TTLCache(1, 1),
            settings.endpoints,
            settings.http,
            settings.apify_max_charge_usd,
        )
        usage = await runner.usage()
        return f"Connected. {usage.remaining_usd:.2f} USD of this month's limit remain."

    async def _search(self, client: httpx.AsyncClient, settings: Settings) -> str:
        """Check the configured web search provider.

        Brave Search is tested with a single query. Any other provider is tested through
        the Apify check.

        Args:
            client: HTTP client to use.
            settings: Settings holding the search configuration.

        Returns:
            A message saying the provider answered.
        """
        config = settings.search_config()
        if config.provider == "brave":
            provider = BraveSearch(
                client,
                config.secret,
                TTLCache(1, 1),
                Throttle(settings.http.brave_interval_seconds),
                settings.endpoints,
                settings.discovery,
                settings.http,
            )
            await provider.search(settings.discovery.test_query, 1)
            return "Brave Search answered."
        return await self._apify(client, settings)

    async def _voice(self, client: httpx.AsyncClient, settings: Settings) -> str:
        """Check the ElevenLabs key with a ping to the voice service.

        Args:
            client: HTTP client to use.
            settings: Settings holding the ElevenLabs key and voice.

        Returns:
            A message saying the key was accepted.

        Raises:
            ConfigError: when no ElevenLabs key is saved.
        """
        if settings.elevenlabs_api_key is None:
            raise ConfigError("No ElevenLabs key is saved.")
        service = VoiceService(
            client,
            settings.elevenlabs_api_key,
            settings.voice_id(),
            settings.endpoints,
            settings.voice,
            TTLCache(1, 1),
        )
        await service.ping()
        return "ElevenLabs accepted the key."


class Connections:
    """Reads, validates and saves the connection settings that can be edited online.

    Writes are serialized with a lock. Secret values are never reported back, only
    the fact that they are set.
    """

    def __init__(self):
        """Initialize the manager with the lock that serializes saves."""
        self._lock = threading.Lock()

    def present(self, settings: Settings) -> dict[str, str]:
        """Return the current values of the editable settings for display.

        Settings that are not set are left out, and secret ones are reported as the
        word set instead of their value.

        Args:
            settings: Current application settings.

        Returns:
            A mapping from environment key to display text.
        """
        values = {}
        for key in EDITABLE:
            value = getattr(settings, attribute(key), None)
            if value is None:
                continue
            # Secrets are reported only as set so their values never leave the server.
            values[key] = (
                "set"
                if key in SECRET_KEYS
                else str(value.get_secret_value() if hasattr(value, "get_secret_value") else value)
            )
        return values

    def validate(self, changes: dict[str, str | None]) -> Settings:
        """Check proposed changes by building the settings they would produce.

        Nothing is written. The result also has to yield a usable language model and
        search configuration when those are present.

        Args:
            changes: Mapping from environment key to the new value; an empty value or
                None clears the key.

        Returns:
            The settings that would be in effect after the change.

        Raises:
            ConfigError: when a key is not editable or the resulting settings are invalid.
        """
        unknown = sorted(set(changes) - EDITABLE)
        if unknown:
            raise ConfigError("Some settings cannot be changed from here.")
        wanted = {attribute(key): (value or None) for key, value in changes.items()}
        try:
            # Keyword values take priority over the env file, so this previews the result of the
            # change without writing anything.
            candidate = Settings(_env_file=env_source(), _env_file_encoding="utf-8", **wanted)
        except ValidationError as error:
            raise ConfigError(describe_errors(error)) from error
        candidate.llm_config_or_none()
        candidate.search_config_or_none()
        return candidate

    def apply(self, changes: dict[str, str | None]) -> tuple[Settings, list[str]]:
        """Validate changes and save them to the environment file.

        Args:
            changes: Mapping from environment key to the new value; an empty value or
                None clears the key.

        Returns:
            The reloaded settings and the changed keys that a process environment
            variable still overrides, whose new values will therefore not take effect.

        Raises:
            ConfigError: when a change is not allowed, invalid, or cannot be written.
        """
        with self._lock:
            # Validate first so an invalid change never reaches the env file.
            self.validate(changes)
            write_values(env_target(), changes, EDITABLE)
            return load_settings(), overridden(list(changes))
