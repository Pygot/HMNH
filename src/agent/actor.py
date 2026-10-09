# src/agent/actor.py
from agent.models import (
    Clarification,
    ResearchRequest,
    SkillSearchRequest,
)
from agent.envfile import load_settings
from agent.runtime import open_runtime
from agent.errors import ConfigError
from agent.config import Settings
from pydantic import SecretStr
from typing import Any

import asyncio

IDENTITY_FIELDS = {
    "name": "name",
    "aliases": "aliases",
    "location": "location",
    "employers": "employers",
    "roles": "roles",
    "skills": "skills",
    "links": "links",
}
OPTION_FIELDS = {
    "networks": "networks",
    "strictness": "strictness",
    "scepticism": "scepticism",
    "maxWebPages": "max_web_pages",
}
MODES = ("person", "skill_search")


def _present(values: dict[str, Any]) -> dict[str, Any]:
    """Drop entries that are None, empty strings or empty lists.

    Args:
        values: The mapping to clean.

    Returns:
        A new mapping without the empty entries.
    """
    return {key: value for key, value in values.items() if value not in (None, "", [])}


def person_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Translate an Actor input into the data of a research request.

    Args:
        payload: The Actor input with camelCase keys.

    Returns:
        A mapping that ``ResearchRequest.model_validate`` accepts.
    """
    return _present(
        {
            "goal": payload.get("goal", "hiring"),
            "identity": _present(
                {to: payload.get(source) for source, to in IDENTITY_FIELDS.items()}
            ),
            "focus": payload.get("focus"),
            "purpose_confirmed": payload.get("purposeConfirmed") is True,
            "requirements": payload.get("requirements"),
            "options": _present({to: payload.get(source) for source, to in OPTION_FIELDS.items()}),
        }
    )


def skill_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Translate an Actor input into the data of a skill search request.

    Args:
        payload: The Actor input with camelCase keys.

    Returns:
        A mapping that ``SkillSearchRequest.model_validate`` accepts.
    """
    return _present(
        {
            "goal": payload.get("goal", "hiring"),
            "skill": payload.get("skill"),
            "location": payload.get("location"),
            "limit": payload.get("limit"),
            "purpose_confirmed": payload.get("purposeConfirmed") is True,
        }
    )


def with_overrides(settings: Settings, payload: dict[str, Any]) -> Settings:
    """Apply the optional language model choices of the Actor input.

    Args:
        settings: Settings loaded from the environment.
        payload: The Actor input.

    Returns:
        The settings, with the provider, key and model replaced when given.
    """
    changes = _present(
        {
            "llm_provider": payload.get("llmProvider"),
            "llm_api_key": payload.get("llmApiKey"),
            "llm_model": payload.get("llmModel"),
        }
    )
    if "llm_api_key" in changes:
        changes["llm_api_key"] = SecretStr(changes["llm_api_key"])
    return settings.model_copy(update=changes) if changes else settings


async def run(payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Run one research from an Actor input.

    Args:
        payload: The Actor input with camelCase keys.
        settings: The application settings.

    Returns:
        A JSON ready mapping with a ``status`` of ``done`` or ``needs_input`` and the
        ``result``, which is a report or a set of clarification questions.

    Raises:
        ConfigError: When the mode is unknown or no research backend is configured.
    """
    mode = payload.get("mode", "person")
    if mode not in MODES:
        raise ConfigError(f"The mode must be one of: {', '.join(MODES)}.")
    async with open_runtime(settings) as runtime:
        if runtime.pipeline is None:
            raise ConfigError("No research backend is configured for this run.")
        if mode == "skill_search":
            request = SkillSearchRequest.model_validate(skill_request(payload))
            result = await runtime.pipeline.skill_search(request)
        else:
            research = ResearchRequest.model_validate(person_request(payload))
            result = await runtime.pipeline.research(research)
    status = "needs_input" if isinstance(result, Clarification) else "done"
    return {"mode": mode, "status": status, "result": result.model_dump(mode="json")}


async def main() -> None:
    """Run the Actor: read the input, research, and store the result."""
    from apify import Actor

    async with Actor:
        payload = await Actor.get_input() or {}
        settings = with_overrides(load_settings(), payload)
        try:
            outcome = await run(payload, settings)
        except (ConfigError, ValueError) as error:
            await Actor.fail(status_message=str(error)[:300])
            return
        await Actor.push_data(outcome)
        await Actor.set_value("OUTPUT", outcome)
        await Actor.set_status_message(
            "Research finished." if outcome["status"] == "done" else "More details are needed."
        )


if __name__ == "__main__":
    asyncio.run(main())
