# tests/test_actor.py
from agent.actor import (
    person_request,
    run,
    skill_request,
    with_overrides,
)
from agent.models import (
    Clarification,
    ClarificationOption,
    ClarificationQuestion,
    Network,
)
from contextlib import asynccontextmanager
from agent.errors import ConfigError
from agent.config import Settings
from types import SimpleNamespace
from pathlib import Path

import pytest
import json

ROOT = Path(__file__).resolve().parent.parent
ACTOR = ROOT / ".actor"


class FakePipeline:
    def __init__(self, result):
        self.result = result
        self.seen = None

    async def research(self, request):
        self.seen = request
        return self.result

    async def skill_search(self, request):
        self.seen = request
        return self.result


def fake_runtime(monkeypatch, pipeline):
    @asynccontextmanager
    async def opened(settings):
        yield SimpleNamespace(pipeline=pipeline)

    monkeypatch.setattr("agent.actor.open_runtime", opened)


def clarification():
    option = ClarificationOption(
        url="https://example.com/in/a", title="A", snippet="s", score=0.5, evidence="e"
    )
    question = ClarificationQuestion(network=Network.LINKEDIN, text="Which one?", options=[option])
    return Clarification(questions=[question])


def test_the_actor_files_are_consistent():
    definition = json.loads((ACTOR / "actor.json").read_text(encoding="utf-8"))
    schema = json.loads((ACTOR / "input_schema.json").read_text(encoding="utf-8"))
    assert definition["actorSpecification"] == 1
    assert (ACTOR / definition["dockerfile"]).is_file()
    assert (ACTOR / definition["readme"]).is_file()
    assert (ACTOR / definition["input"]).is_file()
    assert schema["schemaVersion"] == 1
    assert set(schema["required"]) <= set(schema["properties"])
    assert schema["properties"]["llmApiKey"]["isSecret"] is True
    for name in ("mode", "purposeConfirmed", "name", "skill", "requirements"):
        assert name in schema["properties"]


def test_a_person_input_becomes_a_research_request():
    request = person_request(
        {
            "name": "Ada Lovelace",
            "location": "London",
            "skills": ["math"],
            "links": [],
            "purposeConfirmed": True,
            "strictness": "strict",
            "maxWebPages": 3,
        }
    )
    assert request["identity"] == {"name": "Ada Lovelace", "location": "London", "skills": ["math"]}
    assert request["purpose_confirmed"] is True
    assert request["options"] == {"strictness": "strict", "max_web_pages": 3}
    assert request["goal"] == "hiring"


def test_the_purpose_must_be_exactly_true():
    assert person_request({"name": "A", "purposeConfirmed": "yes"})["purpose_confirmed"] is False
    assert skill_request({"skill": "x", "location": "y"})["purpose_confirmed"] is False


def test_a_skill_search_input_drops_empty_values():
    request = skill_request(
        {"skill": "Python", "location": "Brno", "limit": 3, "purposeConfirmed": True, "focus": ""}
    )
    assert request == {
        "goal": "hiring",
        "skill": "Python",
        "location": "Brno",
        "limit": 3,
        "purpose_confirmed": True,
    }


def test_the_model_overrides_are_applied_as_a_secret():
    settings = Settings(_env_file=None)
    changed = with_overrides(
        settings, {"llmProvider": "openai", "llmApiKey": "sk-x", "llmModel": "m"}
    )
    assert changed.llm_provider == "openai"
    assert changed.llm_api_key.get_secret_value() == "sk-x"
    assert changed.llm_model == "m"
    assert with_overrides(settings, {}) is settings


async def test_run_reports_clarification_questions_as_needing_input(monkeypatch):
    pipeline = FakePipeline(clarification())
    fake_runtime(monkeypatch, pipeline)
    outcome = await run(
        {"name": "Ada Lovelace", "purposeConfirmed": True}, Settings(_env_file=None)
    )
    assert outcome["status"] == "needs_input"
    assert outcome["mode"] == "person"
    assert outcome["result"]["questions"][0]["text"] == "Which one?"
    assert pipeline.seen.identity.name == "Ada Lovelace"
    json.dumps(outcome)


async def test_run_rejects_an_unknown_mode_and_a_missing_backend(monkeypatch):
    with pytest.raises(ConfigError, match="mode"):
        await run({"mode": "other"}, Settings(_env_file=None))
    fake_runtime(monkeypatch, None)
    with pytest.raises(ConfigError, match="backend"):
        await run({"name": "A", "purposeConfirmed": True}, Settings(_env_file=None))


async def test_run_rejects_input_that_is_not_a_valid_request(monkeypatch):
    fake_runtime(monkeypatch, FakePipeline(None))
    with pytest.raises(ValueError):
        await run({"mode": "person", "purposeConfirmed": True}, Settings(_env_file=None))
