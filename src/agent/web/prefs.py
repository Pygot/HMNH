# src/agent/web/prefs.py
from agent.models import (
    Goal,
    Network,
    ResearchOptions,
    ScepticismMode,
    Strictness,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    ValidationError,
)
from enum import StrEnum

import base64
import json


class Theme(StrEnum):
    """Colour theme choices for the web interface."""

    LIGHT = "light"
    DARK = "dark"
    SYSTEM = "system"


class Density(StrEnum):
    """Layout density choices for the web interface."""

    COMFORTABLE = "comfortable"
    COMPACT = "compact"


class LiveView(StrEnum):
    """Choices for how a running search is shown live."""

    CHAT = "chat"
    GRAPH = "graph"


class BuddyMode(StrEnum):
    """Choices for how the buddy assistant talks to the user."""

    OFF = "off"
    TEXT = "text"
    VOICE = "voice"


class Preferences(BaseModel):
    """A browser's interface and research preferences, stored in a cookie.

    The cookie is controlled by the client, so every value is validated when it is
    read back. Unknown fields are ignored.
    """

    model_config = ConfigDict(extra="ignore")

    theme: Theme = Theme.SYSTEM
    density: Density = Density.COMFORTABLE
    goal: Goal = Goal.HIRING
    networks: list[Network] = Field(default_factory=lambda: list(Network))
    strictness: Strictness = Strictness.BALANCED
    threshold: float | None = Field(default=None, ge=0, le=1)
    margin: float | None = Field(default=None, ge=0, le=1)
    max_web_pages: int | None = Field(default=None, ge=0, le=20)
    scepticism: ScepticismMode = ScepticismMode.STANDARD
    skill_limit: int = Field(default=5, ge=1, le=10)
    live_view: LiveView = LiveView.CHAT
    buddy: BuddyMode = BuddyMode.TEXT
    show_api: bool = False
    backdrop: bool = True

    @field_validator("networks")
    @classmethod
    def _at_least_one_source(cls, networks: list[Network]) -> list[Network]:
        """Check that at least one source network is chosen.

        Args:
            networks: The selected networks.

        Returns:
            The networks without duplicates, in their original order.

        Raises:
            ValueError: when the list is empty.
        """
        if not networks:
            raise ValueError("choose at least one source")
        return list(dict.fromkeys(networks))

    def options(self) -> ResearchOptions:
        """Build the research options described by these preferences.

        Returns:
            The research options for a new search.
        """
        return ResearchOptions(
            networks=self.networks,
            strictness=self.strictness,
            threshold=self.threshold,
            margin=self.margin,
            max_web_pages=self.max_web_pages,
            scepticism=self.scepticism,
        )


def encode_preferences(preferences: Preferences) -> str:
    """Encode preferences as URL-safe base64 text without padding.

    Args:
        preferences: The preferences to encode.

    Returns:
        The encoded JSON, suitable for a cookie value.
    """
    raw = preferences.model_dump_json().encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_preferences(value: str | None, max_bytes: int) -> tuple[Preferences, bool]:
    """Decode preferences from a cookie value, falling back to defaults.

    Args:
        value: The cookie value, or None.
        max_bytes: Longest encoded value that is accepted.

    Returns:
        The preferences and a reset flag. The flag is False for a missing or valid value
        and True when the value was too long or could not be decoded or validated.
    """
    if not value:
        return Preferences(), False
    if len(value) > max_bytes:
        return Preferences(), True
    try:
        padded = value + "=" * (-len(value) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return Preferences.model_validate(data), False
    except ValueError, ValidationError, TypeError:
        return Preferences(), True
