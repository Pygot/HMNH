# tests/test_llm.py
from agent.llm import (
    AnthropicClient,
    build_llm,
    ClaudeCodeClient,
    extract_json,
    OllamaClient,
    OpenAICompatibleClient,
    StructuredLLM,
)
from agent.config import (
    Endpoints,
    HttpTuning,
    LLMConfig,
    LlmTuning,
)
from tests.helpers import (
    body_of,
    json_response,
    mock_client,
)
from agent.errors import (
    ConfigError,
    LLMOutputError,
)
from pydantic import (
    BaseModel,
    SecretStr,
)

import asyncio
import pytest
import httpx


async def test_anthropic_request_and_response():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["key"] = request.headers["x-api-key"]
        seen["version"] = request.headers["anthropic-version"]
        seen["body"] = body_of(request)
        return json_response(
            {"content": [{"type": "text", "text": "hello"}], "stop_reason": "end_turn"}
        )

    async with mock_client(handler) as client:
        llm = AnthropicClient(
            client, SecretStr("sk-ant-x"), "model-a", Endpoints(), LlmTuning(), HttpTuning()
        )
        assert await llm.complete("sys", "usr") == "hello"
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert (seen["key"], seen["version"]) == ("sk-ant-x", "2023-06-01")
    assert seen["body"]["system"] == "sys"
    assert seen["body"]["messages"] == [{"role": "user", "content": "usr"}]
    assert seen["body"]["model"] == "model-a"


async def test_anthropic_truncation_and_empty_output_are_errors():
    async with mock_client(
        lambda r: json_response({"content": [], "stop_reason": "max_tokens"})
    ) as c:
        with pytest.raises(LLMOutputError, match="cut off"):
            await AnthropicClient(
                c, SecretStr("k"), "m", Endpoints(), LlmTuning(), HttpTuning()
            ).complete("s", "u")
    async with mock_client(
        lambda r: json_response({"content": [], "stop_reason": "end_turn"})
    ) as c:
        with pytest.raises(LLMOutputError, match="no text"):
            await AnthropicClient(
                c, SecretStr("k"), "m", Endpoints(), LlmTuning(), HttpTuning()
            ).complete("s", "u")


async def test_openai_compatible_request_and_response():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = body_of(request)
        return json_response({"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})

    async with mock_client(handler) as client:
        llm = OpenAICompatibleClient(
            client, "https://router.example/v1", SecretStr("tok"), "m", HttpTuning()
        )
        assert await llm.complete("sys", "usr") == "{}"
    assert seen["url"] == "https://router.example/v1/chat/completions"
    assert seen["auth"] == "Bearer tok"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert [m["role"] for m in seen["body"]["messages"]] == ["system", "user"]


@pytest.mark.parametrize(
    ("payload", "text"),
    [
        ({"choices": [{"message": {"content": "x"}, "finish_reason": "length"}]}, "cut off"),
        ({"choices": []}, "unexpected response shape"),
        ({"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}, "empty"),
    ],
)
async def test_openai_compatible_bad_responses(payload, text):
    async with mock_client(lambda request: json_response(payload)) as client:
        llm = OpenAICompatibleClient(client, "https://x/v1", SecretStr("k"), "m", HttpTuning())
        with pytest.raises(LLMOutputError, match=text):
            await llm.complete("s", "u")


async def test_ollama_chat_request():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = body_of(request)
        return json_response({"message": {"content": "{}"}, "done_reason": "stop"})

    async with mock_client(handler) as client:
        assert (
            await OllamaClient(
                client, "http://localhost:11434", "llama", LlmTuning(), HttpTuning()
            ).complete("s", "u")
            == "{}"
        )
    assert seen["url"] == "http://localhost:11434/api/chat"
    assert seen["body"]["format"] == "json"
    assert seen["body"]["stream"] is False
    assert seen["body"]["options"]["num_ctx"] >= 8192


async def test_ollama_resolves_the_only_installed_model():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/api/tags":
            return json_response({"models": [{"name": "only:latest"}]})
        assert body_of(request)["model"] == "only:latest"
        return json_response({"message": {"content": "{}"}})

    async with mock_client(handler) as client:
        llm = OllamaClient(client, "http://localhost:11434", None, LlmTuning(), HttpTuning())
        await llm.complete("s", "u")
        await llm.complete("s", "u")
    assert calls.count("/api/tags") == 1


@pytest.mark.parametrize("models", [[], [{"name": "a"}, {"name": "b"}]])
async def test_ollama_requires_explicit_model_when_not_unique(models):
    async with mock_client(lambda request: json_response({"models": models})) as client:
        with pytest.raises(ConfigError, match="LLM_MODEL"):
            await OllamaClient(
                client, "http://localhost:11434", None, LlmTuning(), HttpTuning()
            ).complete("s", "u")


async def test_ollama_empty_message_is_an_error():
    payload = {"message": {"content": "  "}, "done_reason": "stop"}
    async with mock_client(lambda request: json_response(payload)) as client:
        with pytest.raises(LLMOutputError, match="empty message"):
            await OllamaClient(
                client, "http://localhost:11434", "m", LlmTuning(), HttpTuning()
            ).complete("s", "u")


async def test_ollama_truncation_is_an_error():
    payload = {"message": {"content": "x"}, "done_reason": "length"}
    async with mock_client(lambda request: json_response(payload)) as client:
        with pytest.raises(LLMOutputError, match="cut off"):
            await OllamaClient(
                client, "http://localhost:11434", "m", LlmTuning(), HttpTuning()
            ).complete("s", "u")


async def test_build_llm_selects_the_provider():
    async with mock_client(lambda request: httpx.Response(200)) as client:
        key = SecretStr("k")
        anthropic = LLMConfig(provider="anthropic", model="m", base_url="u", api_key=key)
        openai = LLMConfig(provider="openai", model="m", base_url="https://x/v1", api_key=key)
        ollama = LLMConfig(provider="ollama", model=None, base_url="http://h:1", api_key=None)
        assert isinstance(
            build_llm(anthropic, client, Endpoints(), LlmTuning(), HttpTuning()), AnthropicClient
        )
        assert isinstance(
            build_llm(openai, client, Endpoints(), LlmTuning(), HttpTuning()),
            OpenAICompatibleClient,
        )
        assert isinstance(
            build_llm(ollama, client, Endpoints(), LlmTuning(), HttpTuning()), OllamaClient
        )
        broken = LLMConfig(provider="openai", model=None, base_url="u", api_key=key)
        with pytest.raises(ConfigError):
            build_llm(broken, client, Endpoints(), LlmTuning(), HttpTuning())


def test_extract_json_handles_fences_and_noise():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"a": {"b": 2}} done') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        extract_json("no json here")
    with pytest.raises(ValueError):
        extract_json("{broken")


class Answer(BaseModel):
    value: int


class ScriptedLLM:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    async def complete(self, system, user, as_json=True):
        self.calls.append((system, user))
        return self.replies.pop(0)


async def test_ask_returns_validated_model_and_includes_schema():
    llm = ScriptedLLM('{"value": 3}')
    assert (await StructuredLLM(llm, LlmTuning()).ask("be brief", "question", Answer)).value == 3
    assert "be brief" in llm.calls[0][0]
    assert '"value"' in llm.calls[0][0]


async def test_ask_retries_once_with_the_rejection_reason():
    llm = ScriptedLLM('{"value": "x"}', '{"value": 4}')
    assert (await StructuredLLM(llm, LlmTuning()).ask("s", "u", Answer)).value == 4
    assert "rejected" in llm.calls[1][1]


async def test_ask_fails_loudly_after_two_bad_answers():
    llm = ScriptedLLM("nonsense", '{"value": "still wrong"}')
    with pytest.raises(LLMOutputError, match="2 attempts"):
        await StructuredLLM(llm, LlmTuning()).ask("s", "u", Answer)


async def test_ask_check_hook_triggers_a_retry():
    def check(answer):
        if answer.value < 5:
            raise ValueError("too small")

    llm = ScriptedLLM('{"value": 1}', '{"value": 9}')
    assert (await StructuredLLM(llm, LlmTuning()).ask("s", "u", Answer, check=check)).value == 9
    assert "too small" in llm.calls[1][1]


async def test_ask_honours_the_configured_attempt_count():
    llm = ScriptedLLM("nonsense", "nonsense", '{"value": 1}')
    one_attempt = StructuredLLM(llm, LlmTuning(attempts=1))
    with pytest.raises(LLMOutputError, match="1 attempts"):
        await one_attempt.ask("s", "u", Answer)
    assert len(llm.calls) == 1
    three = StructuredLLM(ScriptedLLM("x", "x", '{"value": 7}'), LlmTuning(attempts=3))
    assert (await three.ask("s", "u", Answer)).value == 7


async def test_error_excerpts_are_bounded_by_the_configuration():
    llm = ScriptedLLM("x" * 10, "y" * 10)
    tuning = LlmTuning(attempts=2, error_excerpt_chars=50)
    with pytest.raises(LLMOutputError) as info:
        await StructuredLLM(llm, tuning).ask("s", "u", Answer)
    assert len(str(info.value)) < 200


async def test_the_configured_output_budget_and_context_are_sent():
    seen = {}

    def handler(request):
        seen[request.url.path] = body_of(request)
        if request.url.path == "/v1/messages":
            return json_response({"content": [{"type": "text", "text": "{}"}]})
        return json_response({"message": {"content": "{}"}})

    async with mock_client(handler) as client:
        tuning = LlmTuning(max_output_tokens=1234, ollama_context=4096)
        anthropic = AnthropicClient(client, SecretStr("k"), "m", Endpoints(), tuning, HttpTuning())
        await anthropic.complete("s", "u")
        ollama = OllamaClient(client, "http://localhost:11434", "m", tuning, HttpTuning())
        await ollama.complete("s", "u")
    assert seen["/v1/messages"]["max_tokens"] == 1234
    assert seen["/api/chat"]["options"]["num_ctx"] == 4096


async def test_plain_conversation_never_asks_the_provider_for_json():
    seen = []

    def ollama(request):
        seen.append(body_of(request))
        return json_response({"message": {"content": "Hello there."}, "done_reason": "stop"})

    async with mock_client(ollama) as client:
        model = OllamaClient(client, "http://localhost:11434", "llama", LlmTuning(), HttpTuning())
        assert await StructuredLLM(model, LlmTuning()).text("sys", "usr") == "Hello there."
        await model.complete("sys", "usr")
    assert "format" not in seen[0] and seen[1]["format"] == "json"

    bodies = []

    def compatible(request):
        bodies.append(body_of(request))
        return json_response(
            {"choices": [{"message": {"content": "Hi."}, "finish_reason": "stop"}]}
        )

    async with mock_client(compatible) as client:
        model = OpenAICompatibleClient(
            client, "https://router.example/v1", SecretStr("tok"), "m", HttpTuning()
        )
        assert await StructuredLLM(model, LlmTuning()).text("sys", "usr") == "Hi."
        await model.complete("sys", "usr")
    assert "response_format" not in bodies[0]
    assert bodies[1]["response_format"] == {"type": "json_object"}


def test_a_model_that_wraps_its_chat_answer_in_json_is_unwrapped():
    from agent.chat import plain_reply

    assert plain_reply('{"assistant": "Hello! How can I help?"}') == "Hello! How can I help?"
    assert plain_reply('  {"reply": "  Fine.  "} ') == "Fine."
    assert plain_reply("Plain words stay.") == "Plain words stay."
    assert plain_reply('{"a": "x", "b": "y"}') == '{"a": "x", "b": "y"}'
    assert plain_reply("{not json}") == "{not json}"
    assert plain_reply("[1, 2]") == "[1, 2]"
    assert plain_reply('{"n": 3}') == '{"n": 3}'


class FakeProcess:
    def __init__(self, out=b"hello", err=b"", code=0, delay=0.0):
        self.out = out
        self.err = err
        self.returncode = code
        self.delay = delay
        self.killed = False
        self.stdin = None

    async def communicate(self, data):
        self.stdin = data
        await asyncio.sleep(self.delay)
        return self.out, self.err

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def fake_command(monkeypatch, process, found="/bin/claude"):
    seen = {}

    async def spawn(executable, *arguments, **options):
        seen["executable"] = executable
        seen["arguments"] = list(arguments)
        seen["cwd"] = options["cwd"]
        return process

    monkeypatch.setattr("agent.llm.shutil.which", lambda name: found)
    monkeypatch.setattr("agent.llm.asyncio.create_subprocess_exec", spawn)
    return seen


async def test_claude_login_runs_the_cli_without_tools_and_returns_its_text(monkeypatch):
    process = FakeProcess(out=b" an answer ")
    seen = fake_command(monkeypatch, process)
    client = ClaudeCodeClient("sonnet", LlmTuning(), HttpTuning())
    assert await client.complete("be brief", "question") == "an answer"
    assert process.stdin == b"question"
    assert seen["executable"] == "/bin/claude"
    arguments = seen["arguments"]
    assert arguments[0] == "-p"
    assert arguments[arguments.index("--tools") + 1] == ""
    assert arguments[arguments.index("--model") + 1] == "sonnet"
    assert "--no-session-persistence" in arguments
    assert "--bare" not in arguments
    assert "be brief" not in arguments


async def test_claude_login_omits_the_model_when_none_is_set(monkeypatch):
    seen = fake_command(monkeypatch, FakeProcess())
    await ClaudeCodeClient(None, LlmTuning(), HttpTuning()).complete("s", "u")
    assert "--model" not in seen["arguments"]


async def test_claude_login_reports_a_missing_command(monkeypatch):
    fake_command(monkeypatch, FakeProcess(), found=None)
    with pytest.raises(ConfigError, match="not found"):
        await ClaudeCodeClient(None, LlmTuning(), HttpTuning()).complete("s", "u")


async def test_claude_login_reports_a_failed_or_silent_command(monkeypatch):
    fake_command(monkeypatch, FakeProcess(out=b"", err=b"Not logged in", code=1))
    client = ClaudeCodeClient(None, LlmTuning(), HttpTuning())
    with pytest.raises(LLMOutputError, match="Not logged in"):
        await client.complete("s", "u")
    fake_command(monkeypatch, FakeProcess(out=b"  ", code=0))
    with pytest.raises(LLMOutputError):
        await client.complete("s", "u")


async def test_claude_login_kills_a_command_that_runs_too_long(monkeypatch):
    process = FakeProcess(delay=5)
    fake_command(monkeypatch, process)
    client = ClaudeCodeClient(None, LlmTuning(), HttpTuning(llm_timeout_seconds=0.05))
    with pytest.raises(LLMOutputError, match="timed out"):
        await client.complete("s", "u")
    assert process.killed


async def test_the_claude_provider_needs_no_key_and_is_built_by_build_llm():
    from agent.config import Settings

    config = Settings(llm_provider="claude", _env_file=None).llm_config()
    assert config.provider == "claude"
    assert config.api_key is None
    async with mock_client(lambda request: httpx.Response(200)) as client:
        built = build_llm(config, client, Endpoints(), LlmTuning(), HttpTuning())
    assert isinstance(built, ClaudeCodeClient)
