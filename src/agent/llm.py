# src/agent/llm.py
from agent.config import (
    Endpoints,
    HttpTuning,
    LLMConfig,
    LlmTuning,
)
from pydantic import (
    BaseModel,
    SecretStr,
    ValidationError,
)
from agent.errors import (
    ConfigError,
    LLMOutputError,
)
from typing import (
    Any,
    Protocol,
)
from collections.abc import Callable
from agent.http import api_json
from pathlib import Path

import tempfile
import asyncio
import shutil
import httpx
import json


class LLMClient(Protocol):
    """A client that sends one system and user prompt pair to a language model.

    Implementations hide the provider, so callers only see text in and text out.
    """

    async def complete(self, system: str, user: str, as_json: bool = True) -> str:
        """Send one prompt to the model and return its text reply.

        Args:
            system: System prompt that sets the model's behaviour.
            user: User prompt.
            as_json: Whether the reply should be a JSON object, where the provider supports it.

        Returns:
            The text of the model reply.
        """
        ...


class AnthropicClient:
    """An LLM client for the Anthropic Messages API."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: SecretStr,
        model: str,
        endpoints: Endpoints,
        tuning: LlmTuning,
        http: HttpTuning,
    ):
        """Initialize the client with its connection settings.

        Args:
            client: the shared HTTP client.
            api_key: the Anthropic API key.
            model: the model identifier to call.
            endpoints: the API endpoint settings, including base URL and API version.
            tuning: the LLM settings, including the output token limit.
            http: the HTTP settings, including the LLM request timeout.
        """
        self._client = client
        self._api_key = api_key
        self._model = model
        self._url = f"{endpoints.anthropic_api}/v1/messages"
        self._version = endpoints.anthropic_version
        self._max_tokens = tuning.max_output_tokens
        self._time_limit = http.llm_timeout_seconds

    async def complete(self, system: str, user: str, as_json: bool = True) -> str:
        """Send a prompt to the Anthropic Messages API and return the text reply.

        Args:
            system: the system prompt.
            user: the user prompt.
            as_json: ignored, because the API has no JSON mode.

        Returns:
            The text blocks of the reply joined together.

        Raises:
            LLMOutputError: when the reply hit the token limit or has no text.
        """
        del as_json
        data = await api_json(
            self._client,
            "Anthropic API",
            "POST",
            self._url,
            headers={
                "x-api-key": self._api_key.get_secret_value(),
                "anthropic-version": self._version,
            },
            json={
                "model": self._model,
                "max_tokens": self._max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            time_limit=self._time_limit,
        )
        if data.get("stop_reason") == "max_tokens":
            raise LLMOutputError("The model response was cut off at the output token limit.")
        blocks = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        if not blocks:
            raise LLMOutputError("The Anthropic API returned no text content.")
        return "".join(blocks)


class OpenAICompatibleClient:
    """An LLM client for chat completions APIs that follow the OpenAI format."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        api_key: SecretStr,
        model: str,
        http: HttpTuning,
        label: str = "LLM API",
        json_mode: bool = True,
        max_tokens: int | None = None,
    ):
        """Initialize the client with its connection settings.

        Args:
            client: the shared HTTP client.
            base_url: the API base URL without the chat completions path.
            api_key: the bearer token for the API.
            model: the model identifier to call.
            http: the HTTP settings, including the LLM request timeout.
            label: the provider name used in error messages.
            json_mode: whether the provider supports the JSON object response format.
            max_tokens: an output token limit to send, or None to omit it.
        """
        self._client = client
        self._base_url = base_url
        self._api_key = api_key
        self._model = model
        self._time_limit = http.llm_timeout_seconds
        self._label = label
        self._json_mode = json_mode
        self._max_tokens = max_tokens

    async def complete(self, system: str, user: str, as_json: bool = True) -> str:
        """Send a prompt to the chat completions endpoint and return the reply.

        Args:
            system: the system prompt.
            user: the user prompt.
            as_json: whether to request the JSON response format, if json mode is on.

        Returns:
            The content of the first choice.

        Raises:
            LLMOutputError: when the response shape is unexpected, was cut off by the
                token limit, or is empty.
        """
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if self._json_mode and as_json:
            body["response_format"] = {"type": "json_object"}
        if self._max_tokens is not None:
            body["max_tokens"] = self._max_tokens
        data = await api_json(
            self._client,
            self._label,
            "POST",
            f"{self._base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key.get_secret_value()}"},
            json=body,
            time_limit=self._time_limit,
        )
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise LLMOutputError(
                f"The {self._label} returned an unexpected response shape."
            ) from error
        if choice.get("finish_reason") == "length":
            raise LLMOutputError("The model response was cut off at the output token limit.")
        if not isinstance(content, str) or not content.strip():
            raise LLMOutputError(f"The {self._label} returned an empty message.")
        return content


class OllamaClient:
    """An LLM client for a local Ollama server.

    The model name is looked up from the server when it is not configured.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        model: str | None,
        tuning: LlmTuning,
        http: HttpTuning,
    ):
        """Initialize the client with its connection settings.

        Args:
            client: the shared HTTP client.
            base_url: the Ollama server base URL.
            model: the model name, or None to use the only installed model.
            tuning: the LLM settings, including the context window size.
            http: the HTTP settings, including the LLM request timeout.
        """
        self._client = client
        self._base_url = base_url
        self._model = model
        self._context = tuning.ollama_context
        self._time_limit = http.llm_timeout_seconds

    async def _resolve_model(self) -> str:
        """Return the model name, detecting it from the server when not set.

        The detected name is cached for later calls.

        Returns:
            The model name to use.

        Raises:
            ConfigError: when no model is configured and the number of installed
                models is not exactly one.
        """
        if self._model is not None:
            return self._model
        data = await api_json(
            self._client,
            "Ollama",
            "GET",
            f"{self._base_url}/api/tags",
            time_limit=self._time_limit,
        )
        names = sorted(item["name"] for item in data.get("models", []) if "name" in item)
        if len(names) != 1:
            installed = ", ".join(names) if names else "none"
            raise ConfigError(
                f"Set LLM_MODEL to one of the installed Ollama models (installed: {installed})."
            )
        self._model = names[0]
        return self._model

    async def complete(self, system: str, user: str, as_json: bool = True) -> str:
        """Send a prompt to the Ollama chat endpoint and return the reply.

        Args:
            system: the system prompt.
            user: the user prompt.
            as_json: whether to ask Ollama to constrain the reply to JSON.

        Returns:
            The message content of the reply.

        Raises:
            LLMOutputError: when the reply was cut off by the token limit or is empty.
        """
        model = await self._resolve_model()
        body: dict[str, Any] = {
            "model": model,
            "stream": False,
            "options": {"num_ctx": self._context},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if as_json:
            body["format"] = "json"
        data = await api_json(
            self._client,
            "Ollama",
            "POST",
            f"{self._base_url}/api/chat",
            json=body,
            time_limit=self._time_limit,
        )
        if data.get("done_reason") == "length":
            raise LLMOutputError("The model response was cut off at the output token limit.")
        content = data.get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise LLMOutputError("Ollama returned an empty message.")
        return content


class ClaudeCodeClient:
    """Language model access through the signed-in Claude command line tool."""

    def __init__(self, model: str | None, tuning: LlmTuning, http: HttpTuning):
        """Initialize the client.

        Args:
            model: Optional model alias or name passed to the command.
            tuning: Language model tuning, which names the command to run.
            http: HTTP tuning, whose language model timeout also limits the command.
        """
        self._model = model
        self._command = tuning.claude_command
        self._system_file = tuning.claude_system_file
        self._time_limit = http.llm_timeout_seconds

    async def complete(self, system: str, user: str, as_json: bool = True) -> str:
        """Run one non-interactive Claude prompt and return its text.

        Args:
            system: System prompt, passed through a file so its length is not limited.
            user: User prompt, passed on standard input.
            as_json: Ignored, the JSON shape is requested in the prompt itself.

        Returns:
            The text the command printed.

        Raises:
            ConfigError: When the command is missing or not signed in.
            LLMOutputError: When the command fails, times out or prints nothing.
        """
        del as_json
        executable = shutil.which(self._command)
        if executable is None:
            raise ConfigError(
                f"The {self._command} command was not found. Install Claude Code and run "
                f"{self._command} login, or choose another language model provider."
            )
        with tempfile.TemporaryDirectory() as folder:
            system_path = Path(folder) / self._system_file
            system_path.write_text(system, encoding="utf-8")
            arguments = [
                "-p",
                "--system-prompt-file",
                str(system_path),
                "--tools",
                "",
                "--disable-slash-commands",
                "--no-session-persistence",
                "--output-format",
                "text",
            ]
            if self._model:
                arguments += ["--model", self._model]
            process = await asyncio.create_subprocess_exec(
                executable,
                *arguments,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=folder,
            )
            try:
                out, err = await asyncio.wait_for(
                    process.communicate(user.encode("utf-8")), self._time_limit
                )
            except TimeoutError as error:
                process.kill()
                await process.wait()
                raise LLMOutputError("The Claude command timed out.") from error
        text = out.decode("utf-8", errors="replace").strip()
        if process.returncode != 0 or not text:
            detail = " ".join(err.decode("utf-8", errors="replace").split())[:300]
            raise LLMOutputError(
                f"The Claude command failed ({detail or 'no output'}). "
                f"Run {self._command} login if you are not signed in."
            )
        return text


def build_llm(
    config: LLMConfig,
    client: httpx.AsyncClient,
    endpoints: Endpoints,
    tuning: LlmTuning,
    http: HttpTuning,
) -> LLMClient:
    """Create the language model client that matches a configuration.

    Args:
        config: The resolved language model configuration.
        client: Shared HTTP client used by the network based providers.
        endpoints: Service addresses and API versions.
        tuning: Language model tuning.
        http: HTTP tuning.

    Returns:
        A client with a ``complete`` coroutine.

    Raises:
        ConfigError: When the configuration lacks a key or model.
    """
    if config.provider == "claude":
        return ClaudeCodeClient(config.model, tuning, http)
    if config.provider == "ollama":
        return OllamaClient(client, config.base_url, config.model, tuning, http)
    if config.api_key is None or config.model is None:
        raise ConfigError("The LLM configuration is incomplete.")
    if config.provider == "anthropic":
        return AnthropicClient(client, config.api_key, config.model, endpoints, tuning, http)
    if config.provider == "apify":
        return OpenAICompatibleClient(
            client,
            config.base_url,
            config.api_key,
            config.model,
            http,
            label="Apify AI",
            json_mode=False,
            max_tokens=min(tuning.max_output_tokens, tuning.apify_ai_output_tokens),
        )
    return OpenAICompatibleClient(client, config.base_url, config.api_key, config.model, http)


def extract_json(raw: str) -> Any:
    """Parse the outermost JSON object found in model output.

    Args:
        raw: the raw model text, possibly with prose or code fences around the JSON.

    Returns:
        The parsed JSON value.

    Raises:
        ValueError: when no JSON object is found or the text is not valid JSON.
    """
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in the model output")
    return json.loads(raw[start : end + 1])


class StructuredLLM:
    """A wrapper that turns model replies into validated pydantic objects.

    A reply that fails parsing or validation is retried with the error shown to
    the model, up to the configured number of attempts.
    """

    def __init__(self, client: LLMClient, tuning: LlmTuning):
        """Initialize the wrapper.

        Args:
            client: the LLM client to call.
            tuning: the LLM settings, including the attempt count.
        """
        self._client = client
        self._tuning = tuning

    async def text(self, system: str, user: str) -> str:
        """Ask the model for a free text reply.

        Args:
            system: the system prompt.
            user: the user prompt.

        Returns:
            The reply text, without asking for JSON.
        """
        return await self._client.complete(system, user, as_json=False)

    async def ask[T: BaseModel](
        self,
        system: str,
        user: str,
        model: type[T],
        check: Callable[[T], None] | None = None,
    ) -> T:
        """Ask the model for an object that matches a pydantic model.

        The JSON schema of the model is appended to the system prompt. When a reply
        is rejected, the next prompt includes a cut down copy of the error.

        Args:
            system: the system prompt.
            user: the user prompt.
            model: the pydantic class the reply must validate against.
            check: an optional extra check that raises ValueError to reject a result.

        Returns:
            The validated object.

        Raises:
            LLMOutputError: when no valid reply arrived within the allowed attempts.
        """
        schema = json.dumps(model.model_json_schema(), separators=(",", ":"))
        full_system = (
            f"{system}\n\nRespond with a single JSON object that matches this schema:\n{schema}"
        )
        prompt = user
        last_error = ""
        for _ in range(self._tuning.attempts):
            raw = await self._client.complete(full_system, prompt)
            try:
                result = model.model_validate(extract_json(raw))
                if check is not None:
                    check(result)
                return result
            except (ValueError, ValidationError) as error:
                # The error is shortened so a long validation dump does not bloat the retry prompt.
                last_error = str(error)[: self._tuning.error_excerpt_chars]
                prompt = (
                    f"{user}\n\nYour previous answer was rejected: {last_error}\n"
                    "Answer again with only the corrected JSON object."
                )
        raise LLMOutputError(
            f"The model did not return valid JSON after {self._tuning.attempts} attempts: "
            f"{last_error}"
        )
