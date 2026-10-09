# tests/web_helpers.py
from agent.models import (
    Clarification,
    ClarificationOption,
    ClarificationQuestion,
    Identity,
    Network,
)
from tests.fakes import (
    hiring_report,
    person_report,
    replay,
    RuleLLM,
    skill_report,
)
from starlette.testclient import TestClient
from agent.web.settings import WebSettings
from contextlib import asynccontextmanager
from agent.runtime import ServiceStatus
from agent.events import as_reporter
from agent.web.app import create_app
from agent.chat import ChatEngine
from agent.config import Settings

import threading
import asyncio
import time
import re

TOKEN = "correct-horse-battery-staple-token"
API_TOKEN = "api-token-for-the-test-suite-only"
LI_A = "https://www.linkedin.com/in/jan-a"
LI_B = "https://www.linkedin.com/in/jan-b"
ORIGIN = "https://localhost"
PASSWORD = "tangerine-orbit-lantern-42"
CSRF_FIELD = re.compile(r'data-csrf="([^"]+)"')


def clarification():
    options = [
        ClarificationOption(
            url=LI_A,
            title="Jan <b>A</b>",
            snippet="Brno",
            score=0.6,
            evidence="name 100%, location 100%",
        ),
        ClarificationOption(
            url=LI_B, title="Jan B", snippet="Prague", score=0.55, evidence="name 100%, location 0%"
        ),
    ]
    question = ClarificationQuestion(network=Network.LINKEDIN, text="Which one?", options=options)
    return Clarification(questions=[question])


def bearer(token=API_TOKEN):
    return {"authorization": f"Bearer {token}"}


class StubResearcher:
    def __init__(self):
        self.unavailable_networks = [Network.LINKEDIN]
        self.results = []
        self.skill_result = skill_report()
        self.calls = []
        self.gate = None
        self.status_calls = 0
        self.status_error = None
        self.status_value = ServiceStatus(
            llm_provider="anthropic",
            llm_model="claude-sonnet-5-5",
            search_provider="apify",
            unavailable_networks=["linkedin"],
            apify_monthly_usage_usd=2.0,
            apify_remaining_usd=103.0,
        )

    async def identity_from_cv(self, data, filename):
        self.calls.append(("cv", len(data), filename))
        return Identity(name="Cv Person")

    async def research(self, request, progress):
        self.calls.append(("research", request))
        progress("Searching for public profiles")
        if self.gate is not None:
            await asyncio.to_thread(self.gate.wait, 10)
        result = self.results.pop(0) if self.results else person_report()
        if isinstance(result, Exception):
            raise result
        return result

    async def skill_search(self, request, progress):
        self.calls.append(("skill", request))
        progress("Searching for candidates")
        return self.skill_result

    def hold(self):
        self.gate = threading.Event()
        return self.gate


class ScriptedResearcher(StubResearcher):
    def __init__(self, report=None):
        super().__init__()
        self.report = report or hiring_report()

    async def research(self, request, progress):
        self.calls.append(("research", request))
        if self.gate is not None:
            await asyncio.to_thread(self.gate.wait, 10)
        result = self.results.pop(0) if self.results else self.report
        if isinstance(result, Exception):
            raise result
        if hasattr(result, "findings"):
            replay(result, as_reporter(progress))
        return result


class FakeVoice:
    def __init__(self):
        self.spoken = []
        self.heard = []
        self.error = None

    async def speak(self, text):
        self.spoken.append(text)
        if self.error is not None:
            raise self.error
        return b"ID3 fake mp3 audio"

    async def transcribe(self, audio, content_type):
        self.heard.append((audio, content_type))
        if self.error is not None:
            raise self.error
        return "Jan Novak from Brno"


class ChatScript:
    def __init__(self):
        self.understood = {}
        self.answers = []
        self.replies = []
        self.llm = RuleLLM(
            {
                "fill in a JSON form": self._understand,
                "asked which one is right": self._answer,
                "assistant of a tool": self._reply,
                "friendly necktie": self._reply,
            }
        )

    def _understand(self, user):
        text = user.rsplit("Message to read:\n", 1)[1]
        if text in self.understood:
            return self.understood[text]
        if text.lower().startswith("find "):
            skill, _, place = text[5:].partition(" in ")
            return {"intent": "skill", "skill": skill, "location": place or "Brno"}
        name, _, city = text.partition(" in ")
        return {"intent": "research", "name": name, "city": city or None}

    def _answer(self, user):
        return self.answers.pop(0)

    def _reply(self, user):
        return self.replies.pop(0) if self.replies else "A reply."


class StubServices:
    def __init__(self, researcher, voice=None, chat=None):
        self.pipeline = researcher
        self.voice = voice
        self.chat = chat

    async def status(self):
        self.pipeline.status_calls += 1
        if self.pipeline.status_error is not None:
            raise self.pipeline.status_error
        return self.pipeline.status_value


def build_app(tmp_path, stub=None, voice=None, **overrides):
    stub = stub or StubResearcher()
    stub.script = ChatScript()
    settings = Settings(
        _env_file=None,
        output_dir=tmp_path / "out",
        email={"template_dir": tmp_path / "email_templates"},
    )
    web = WebSettings(_env_file=None, access_token=TOKEN, **overrides)

    @asynccontextmanager
    async def factory():
        yield StubServices(stub, voice, ChatEngine(stub.script.llm, settings.chat))

    return create_app(settings, web, factory), stub


class Browser:
    def __init__(self, client: TestClient):
        self.client = client

    def csrf(self, path="/"):
        response = self.client.get(path)
        match = CSRF_FIELD.search(response.text)
        assert match, response.text[:500]
        return match.group(1)

    def post(self, path, data=None, token=None, origin=ORIGIN, headers=None, **kwargs):
        fields = dict(data or {})
        if token is not None:
            fields["csrf_token"] = token
        merged = {"origin": origin, **(headers or {})}
        if origin is None:
            del merged["origin"]
        return self.client.post(path, data=fields, headers=merged, **kwargs)

    def login(self, token=TOKEN):
        form_token = self.csrf("/")
        return self.post("/login", {"token": token}, token=form_token)

    def sign_in(self, username, password=PASSWORD):
        form_token = self.csrf("/")
        return self.post("/login", {"username": username, "password": password}, token=form_token)

    def signed_in_token(self, username, password=PASSWORD, path="/account"):
        response = self.sign_in(username, password)
        assert response.status_code == 303, response.text
        return self.csrf(path)

    def logged_in_token(self, path="/"):
        assert self.login().status_code == 303
        return self.csrf(path)


def say(browser, token, text, thread="", **kwargs):
    fields = {"text": text, "thread": thread, "csrf_token": token}
    return browser.client.post("/chat/send", data=fields, headers={"origin": ORIGIN}, **kwargs)


def start(browser, token, text="Jan Novak in Brno"):
    first = say(browser, token, text)
    assert first.status_code == 200, first.text
    thread = first.json()["thread"]["id"]
    second = say(browser, token, "yes", thread)
    assert second.status_code == 200, second.text
    job_id = second.json()["job_id"]
    assert job_id, second.json()
    return f"/jobs/{job_id}"


def add_user(app, username, role="admin", password=PASSWORD, email=None, **fields):
    ctx = app.state.ctx
    chosen = ctx.accounts.role_named(role)
    assert chosen is not None, role
    return ctx.accounts.create_user(
        username,
        chosen.id,
        email=email,
        password_hash=ctx.hasher.hash(password) if password else None,
        **fields,
    )


def open_client(app, host="testclient"):
    return TestClient(
        app,
        base_url=ORIGIN,
        follow_redirects=False,
        raise_server_exceptions=False,
        client=(host, 50000),
    )


def wait_for(
    browser: Browser, location: str, states=("done", "failed", "needs_input"), timeout=5.0
):
    job_path = location.removeprefix(ORIGIN)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = browser.client.get(f"{job_path}/status").json()
        if payload["state"] in states:
            return payload
        time.sleep(0.02)
    raise AssertionError("job did not finish in time")
