# tests/test_prefs.py
from agent.web.prefs import (
    BuddyMode,
    decode_preferences,
    Density,
    encode_preferences,
    LiveView,
    Preferences,
    Theme,
)
from agent.models import (
    Goal,
    Network,
    ResearchOptions,
    ScepticismMode,
    Strictness,
)
from pydantic import ValidationError

import base64
import pytest
import json

LIMIT = 2048


def encoded(payload):
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def test_defaults_are_system_theme_and_every_source():
    prefs = Preferences()
    assert prefs.theme is Theme.SYSTEM and prefs.density is Density.COMFORTABLE
    assert prefs.goal is Goal.HIRING and prefs.networks == list(Network)
    assert prefs.strictness is Strictness.BALANCED
    assert prefs.scepticism is ScepticismMode.STANDARD
    assert prefs.threshold is None and prefs.margin is None and prefs.max_web_pages is None
    assert prefs.skill_limit == 5


def test_preferences_survive_the_cookie_round_trip():
    prefs = Preferences(
        theme=Theme.DARK,
        density=Density.COMPACT,
        goal=Goal.SALES,
        networks=[Network.WEB, Network.LINKEDIN],
        strictness=Strictness.STRICT,
        threshold=0.8,
        margin=0.2,
        max_web_pages=4,
        scepticism=ScepticismMode.STRICT,
        skill_limit=7,
    )
    value = encode_preferences(prefs)
    assert "=" not in value and len(value) < LIMIT
    assert decode_preferences(value, LIMIT) == (prefs, False)


def test_an_absent_cookie_gives_the_defaults_without_a_reset_notice():
    assert decode_preferences(None, LIMIT) == (Preferences(), False)
    assert decode_preferences("", LIMIT) == (Preferences(), False)


@pytest.mark.parametrize(
    "value",
    [
        "!!!not-base64!!!",
        "A",
        base64.urlsafe_b64encode(b"not json").decode(),
        base64.urlsafe_b64encode(b"[1, 2]").decode(),
        encoded({"theme": "neon"}),
        encoded({"threshold": 5}),
        encoded({"networks": []}),
        encoded({"skill_limit": 99}),
        "éè",
    ],
)
def test_unreadable_cookies_fall_back_to_the_defaults_and_say_so(value):
    assert decode_preferences(value, LIMIT) == (Preferences(), True)


def test_oversized_cookies_are_not_even_decoded():
    huge = encode_preferences(Preferences())
    assert decode_preferences(huge, 10) == (Preferences(), True)
    assert decode_preferences(huge, LIMIT) == (Preferences(), False)


def test_partial_cookies_fill_in_the_defaults():
    prefs, reset = decode_preferences(encoded({"theme": "light"}), LIMIT)
    assert not reset and prefs.theme is Theme.LIGHT and prefs.goal is Goal.HIRING


def test_sources_are_deduplicated_and_cannot_be_empty():
    assert Preferences(networks=[Network.WEB, Network.WEB]).networks == [Network.WEB]
    with pytest.raises(ValidationError, match="at least one source"):
        Preferences(networks=[])


@pytest.mark.parametrize(
    "values",
    [
        {"threshold": -0.1},
        {"threshold": 1.1},
        {"margin": 2},
        {"max_web_pages": 21},
        {"max_web_pages": -1},
        {"skill_limit": 0},
        {"skill_limit": 11},
        {"theme": "neon"},
    ],
)
def test_invalid_values_are_rejected(values):
    with pytest.raises(ValidationError):
        Preferences(**values)


def test_preferences_become_research_options():
    prefs = Preferences(
        networks=[Network.WEB],
        strictness=Strictness.LENIENT,
        threshold=0.5,
        margin=0.05,
        max_web_pages=0,
        scepticism=ScepticismMode.STRICT,
    )
    assert prefs.options() == ResearchOptions(
        networks=[Network.WEB],
        strictness=Strictness.LENIENT,
        threshold=0.5,
        margin=0.05,
        max_web_pages=0,
        scepticism=ScepticismMode.STRICT,
    )


def test_the_live_view_defaults_to_chat_and_survives_the_cookie():
    assert Preferences().live_view is LiveView.CHAT
    prefs = Preferences(live_view=LiveView.GRAPH)
    assert decode_preferences(encode_preferences(prefs), LIMIT) == (prefs, False)


def test_the_buddy_and_the_developer_page_choices_survive_the_cookie():
    prefs = Preferences(buddy=BuddyMode.VOICE, show_api=True)
    value = encode_preferences(prefs)
    assert decode_preferences(value, LIMIT) == (prefs, False)
    assert len(value) < LIMIT
    assert Preferences().buddy is BuddyMode.TEXT and Preferences().show_api is False


def test_fields_of_older_cookies_are_ignored_instead_of_resetting_everything():
    old = encoded({"theme": "dark", "company_name": "Acme", "sender_name": "Eva", "unknown": 1})
    prefs, reset = decode_preferences(old, LIMIT)
    assert not reset and prefs == Preferences(theme=Theme.DARK)
    assert not hasattr(prefs, "company_name")


@pytest.mark.parametrize(
    "values",
    [
        {"live_view": "hologram"},
        {"buddy": "loud"},
        {"show_api": "maybe"},
        {"density": "huge"},
    ],
)
def test_invalid_view_values_are_rejected(values):
    with pytest.raises(ValidationError):
        Preferences(**values)
