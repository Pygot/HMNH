# tests/test_buddy.py
from tests.web_helpers import (
    Browser,
    build_app,
    FakeVoice,
    open_client,
    ORIGIN,
    ScriptedResearcher,
    start,
    wait_for,
)
from agent.web.prefs import (
    BuddyMode,
    decode_preferences,
    encode_preferences,
    Preferences,
)
from agent.config import (
    BUDDY_LINES,
    BuddyTuning,
    Settings,
)
from agent.errors import (
    AgentError,
    UpstreamError,
)
from agent.web.common import prefs_cookie_name
from agent.web.app import STATIC_DIR
from pydantic import ValidationError

import xml.etree.ElementTree as ET
import pytest
import json
import re

JS = (STATIC_DIR / "buddy.js").read_text(encoding="utf-8")
STYLES = (STATIC_DIR / "buddy.css").read_text(encoding="utf-8")
TIE = (STATIC_DIR / "tie.svg").read_text(encoding="utf-8")
LOGO = (STATIC_DIR / "logo.svg").read_text(encoding="utf-8")
MOODS = {"idle", "working", "happy", "worried", "sleepy", "surprised"}
SETTINGS_FORM = {
    "theme": "system",
    "density": "comfortable",
    "live_view": "chat",
    "goal": "hiring",
    "source": ["web"],
    "skill_limit": "5",
    "strictness": "balanced",
    "scepticism": "standard",
}
PAGES = ("/", "/emails", "/settings")


def config_of(page):
    return json.loads(re.search(r"data-config='([^']*)'", page).group(1))


def choose(client, token, mode, origin=ORIGIN):
    headers = {"origin": origin, "x-csrf-token": token}
    return client.post("/buddy", json={"mode": mode}, headers=headers)


def talk(client, token, payload, headers=None):
    sent = {"origin": ORIGIN, "x-csrf-token": token, **(headers or {})}
    return client.post("/tie/send", json=payload, headers=sent)


def test_the_default_lines_are_complete_and_safe():
    tuning = BuddyTuning()
    assert tuning.name == "Tie"
    assert set(BUDDY_LINES) <= set(tuning.lines)
    for key in tuning.voice_lines:
        assert "{" not in tuning.lines[key]
    assert Settings(_env_file=None).buddy == tuning


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"lines": {"greeting": "Hi"}}, "missing"),
        ({"lines": {**BuddyTuning().lines, "progress": "Hi {nobody}"}}, "unknown placeholder"),
        ({"lines": {**BuddyTuning().lines, "done": "{count} done"}}, "cannot have placeholders"),
        ({"voice_lines": ("greeting", "ghost")}, "do not exist"),
        ({"lines": {**BuddyTuning().lines, "idle": "  "}}, "missing"),
        ({"focus_minutes": 0}, "greater than or equal to 1"),
        ({"max_lines_per_minute": 0}, "greater than or equal to 1"),
    ],
)
def test_invalid_buddy_settings_are_rejected(change, message):
    with pytest.raises(ValidationError, match=message):
        BuddyTuning(**change)


def test_the_lines_can_be_replaced_from_the_configuration():
    lines = {**BuddyTuning().lines, "greeting": "Good day."}
    assert BuddyTuning(lines=lines, name="Knot").lines["greeting"] == "Good day."


def test_the_buddy_is_on_in_text_mode_by_default_and_survives_the_cookie():
    assert Preferences().buddy is BuddyMode.TEXT
    prefs = Preferences(buddy=BuddyMode.VOICE)
    assert decode_preferences(encode_preferences(prefs), 2048) == (prefs, False)
    with pytest.raises(ValidationError):
        Preferences(buddy="loud")


def test_tie_is_in_the_rail_of_every_signed_in_page_and_never_on_the_landing_page(tmp_path):
    app, _ = build_app(tmp_path, ScriptedResearcher())
    with open_client(app) as client:
        landing = client.get("/").text
        browser = Browser(client)
        token = browser.logged_in_token()
        pages = [client.get(path).text for path in PAGES]
        location = start(browser, token)
        wait_for(browser, location)
        pages.append(client.get(location).text)
    assert "data-buddy" not in landing and "buddy.js" not in landing and "Tie" not in landing
    for page in pages:
        assert "data-buddy" in page and 'data-mode="text"' in page
        assert re.search(
            r'<script src="/static/buddy\.[0-9a-f]{10}\.js" nonce="[^"]+" defer>', page
        )
        assert re.search(r'<link rel="stylesheet" href="/static/buddy\.[0-9a-f]{10}\.css">', page)
        assert '<svg class="tie" aria-hidden="true"' in page and "data-tie-open" in page
        assert 'data-tiepanel hidden role="dialog"' in page and "Focus 25 min" in page
        assert "Ready to talk" in page and "data-nudge hidden" in page


def test_the_rail_hands_its_configuration_to_the_script(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        Browser(client).logged_in_token()
        page = client.get("/").text
    config = config_of(page)
    tuning = BuddyTuning()
    assert config["name"] == "Tie" and config["lines"] == tuning.lines
    assert config["voice_lines"] == list(tuning.voice_lines)
    assert config["focus_minutes"] == 25 and config["break_minutes"] == 5
    assert 'data-page="chat"' in page and "Focus 25 min" in page


def test_the_page_names_follow_the_current_page(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        Browser(client).logged_in_token()
        assert 'data-page="emails"' in client.get("/emails").text
        assert 'data-page="settings"' in client.get("/settings").text


def test_the_focus_length_and_the_name_come_from_the_settings(tmp_path):
    app, _ = build_app(tmp_path)
    settings = app.state.ctx.settings
    app.state.ctx.settings = settings.model_copy(
        update={"buddy": BuddyTuning(name="Knot", focus_minutes=40)}
    )
    with open_client(app) as client:
        Browser(client).logged_in_token()
        page = client.get("/").text
    assert "Focus 40 min" in page and 'aria-label="Talk to Knot"' in page
    assert 'aria-label="Conversation with Knot"' in page


def test_turning_the_nudges_off_keeps_tie_in_the_rail_so_he_can_always_be_found(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        assert choose(client, token, "off").status_code == 204
        page = client.get("/").text
        assert choose(client, token, "text").status_code == 204
        back = client.get("/").text
    assert 'data-mode="off"' in page and "data-tie-open" in page and "data-tiepanel" in page
    assert "Quiet, tap to talk" in page and "Nudges are off" in page
    assert 'data-mode="text"' in back and "Ready to talk" in back and "Nudges are on" in back


def test_the_voice_button_needs_a_voice_key(tmp_path):
    plain, _ = build_app(tmp_path / "plain")
    spoken, _ = build_app(tmp_path / "spoken", voice=FakeVoice())
    with open_client(plain) as client:
        Browser(client).logged_in_token()
        without = client.get("/").text
    with open_client(spoken) as client:
        Browser(client).logged_in_token()
        with_voice = client.get("/").text
    assert "data-voice-toggle" not in without and "data-tie-mic" not in without
    assert "data-voice-toggle" in with_voice and "data-tie-mic" in with_voice
    assert 'aria-pressed="false"' in with_voice


def test_voice_mode_is_remembered_and_falls_back_to_text_without_a_key(tmp_path):
    spoken, _ = build_app(tmp_path / "spoken", voice=FakeVoice())
    with open_client(spoken) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        assert choose(client, token, "voice").status_code == 204
        page = client.get("/").text
        cookie = client.cookies.get(prefs_cookie_name(spoken.state.ctx.web))
    assert 'data-mode="voice"' in page and 'aria-pressed="true"' in page
    plain, _ = build_app(tmp_path / "plain")
    with open_client(plain) as client:
        Browser(client).logged_in_token()
        client.cookies.set(prefs_cookie_name(plain.state.ctx.web), cookie)
        fallback = client.get("/").text
    assert 'data-mode="text"' in fallback and 'data-mode="voice"' not in fallback


def test_the_choice_is_validated_and_protected(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        good = {"origin": ORIGIN, "x-csrf-token": token}
        assert choose(client, token, "voice").status_code == 400
        assert choose(client, token, "loud").status_code == 400
        assert client.post("/buddy", json={"other": 1}, headers=good).status_code == 400
        assert client.post("/buddy", content=b"nope", headers=good).status_code == 400
        assert client.post("/buddy", content=b"[1]", headers=good).status_code == 400
        assert (
            client.post("/buddy", json={"mode": "off"}, headers={"origin": ORIGIN}).status_code
            == 403
        )
        assert choose(client, token, "off", origin="https://evil.example").status_code == 403


def test_the_choice_needs_a_login(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        response = client.post("/buddy", json={"mode": "off"}, headers={"origin": ORIGIN})
    assert response.status_code == 303 and response.headers["location"] == "/login"


def test_the_settings_page_offers_the_modes(tmp_path):
    plain, _ = build_app(tmp_path / "plain")
    spoken, _ = build_app(tmp_path / "spoken", voice=FakeVoice())
    with open_client(plain) as client:
        Browser(client).logged_in_token()
        without = client.get("/settings").text
    with open_client(spoken) as client:
        Browser(client).logged_in_token()
        with_voice = client.get("/settings").text
    for page in (without, with_voice):
        assert "<legend>Nudges and voice</legend>" in page
        assert 'name="buddy" value="off"' in page and 'name="buddy" value="text" checked' in page
        assert "at the bottom of the left rail" in page
    assert 'name="buddy" value="voice"' not in without
    assert 'name="buddy" value="voice"' in with_voice
    assert "Voice needs ELEVENLABS_API_KEY" in without


def test_the_mode_can_be_chosen_in_the_settings(tmp_path):
    app, _ = build_app(tmp_path, voice=FakeVoice())
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        saved = browser.post("/settings", {**SETTINGS_FORM, "buddy": "off"}, token=token)
        off = client.get("/").text
        browser.post("/settings", {**SETTINGS_FORM, "buddy": "voice"}, token=token)
        voice = client.get("/").text
        browser.post("/settings", SETTINGS_FORM, token=token)
        default = client.get("/").text
    assert saved.status_code == 303
    assert 'data-mode="off"' in off and "data-tie-open" in off
    assert 'data-mode="voice"' in voice
    assert 'data-mode="text"' in default


def test_voice_cannot_be_saved_without_a_key_and_bad_values_are_errors(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        voice = browser.post("/settings", {**SETTINGS_FORM, "buddy": "voice"}, token=token)
        loud = browser.post("/settings", {**SETTINGS_FORM, "buddy": "loud"}, token=token)
    assert voice.status_code == 400 and "can only use text" in voice.text
    assert loud.status_code == 400 and "Buddy is not a valid choice" in loud.text


def test_lines_are_spoken_from_the_server_text_only(tmp_path):
    voice = FakeVoice()
    app, _ = build_app(tmp_path, voice=voice)
    with open_client(app) as client:
        Browser(client).logged_in_token()
        done = client.get("/buddy/speech/done")
        injected = client.get("/buddy/speech/done?text=buy+me+a+coffee")
    tuning = BuddyTuning()
    assert done.status_code == 200 and done.headers["content-type"] == "audio/mpeg"
    assert done.content == b"ID3 fake mp3 audio"
    assert voice.spoken == [tuning.lines["done"], tuning.lines["done"]]
    assert injected.status_code == 200


@pytest.mark.parametrize("line", ["progress", "detour", "away_back", "nonsense"])
def test_only_fixed_lines_without_placeholders_can_be_spoken(tmp_path, line):
    voice = FakeVoice()
    app, _ = build_app(tmp_path, voice=voice)
    with open_client(app) as client:
        Browser(client).logged_in_token()
        response = client.get(f"/buddy/speech/{line}")
    assert response.status_code == 404 and voice.spoken == []


def test_speech_is_unreachable_without_a_voice_key_or_a_login(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        anonymous = client.get("/buddy/speech/done")
        Browser(client).logged_in_token()
        signed = client.get("/buddy/speech/done")
    assert anonymous.status_code == 303 and anonymous.headers["location"] == "/login"
    assert signed.status_code == 404 and signed.json()["error"].startswith("Voice is disabled")


def test_speech_is_rate_limited_and_failures_are_reported(tmp_path):
    app, _ = build_app(tmp_path, voice=FakeVoice(), voice_requests=2)
    with open_client(app) as client:
        Browser(client).logged_in_token()
        statuses = [client.get("/buddy/speech/done").status_code for _ in range(3)]
    assert statuses == [200, 200, 429]
    failing = FakeVoice()
    failing.error = UpstreamError("The voice service failed.")
    other, _ = build_app(tmp_path / "other", voice=failing)
    with open_client(other) as client:
        Browser(client).logged_in_token()
        assert client.get("/buddy/speech/done").status_code == 502


def test_the_conversation_starts_with_a_greeting_and_keeps_what_was_said(tmp_path):
    app, stub = build_app(tmp_path)
    stub.script.replies = ["Ask what they shipped alone."]
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        before = client.get("/tie/messages").json()
        reply = talk(client, token, {"text": "How do I interview a developer?", "page": "chat"})
        after = client.get("/tie/messages").json()
    assert before["messages"] == [] and before["hello"].startswith("Hi, I am Tie")
    assert [item["role"] for item in reply.json()["messages"]] == ["user", "assistant"]
    assert reply.json()["messages"][1]["text"] == "Ask what they shipped alone."
    assert [item["text"] for item in after["messages"]] == [
        "How do I interview a developer?",
        "Ask what they shipped alone.",
    ]


def test_the_conversation_can_be_read_in_context_and_cleared(tmp_path):
    app, stub = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = start(browser, token)
        wait_for(browser, location)
        job_id = location.removeprefix("/jobs/")
        ok = talk(client, token, {"text": "What stands out?", "job": job_id, "page": "job"})
        unknown = talk(client, token, {"text": "And now?", "job": "not-a-job"})
        cleared = client.post("/tie/clear", headers={"origin": ORIGIN, "x-csrf-token": token})
        empty = client.get("/tie/messages").json()
    prompt = next(user for system, user in stub.script.llm.calls if "friendly necktie" in system)
    assert "Overall rating 3.4" in prompt and "job page" in prompt
    assert ok.status_code == unknown.status_code == 200
    assert cleared.status_code == 204 and empty["messages"] == []


def test_the_conversation_is_validated_protected_and_rate_limited(tmp_path):
    app, _ = build_app(tmp_path, chat_requests=3)
    with open_client(app) as client:
        anonymous = client.post("/tie/send", json={"text": "Hi"}, headers={"origin": ORIGIN})
        browser = Browser(client)
        token = browser.logged_in_token()
        forged = talk(client, "forged", {"text": "Hi"})
        cross = talk(client, token, {"text": "Hi"}, {"origin": "https://evil.example"})
        broken = client.post(
            "/tie/send",
            content=b"nope",
            headers={"origin": ORIGIN, "x-csrf-token": token},
        )
        statuses = [
            talk(client, token, payload).status_code
            for payload in ({"other": 1}, {"text": "  "}, {"text": "x" * 5000}, {"text": 7})
        ]
        accepted = [talk(client, token, {"text": "Hi"}).status_code for _ in range(4)]
    assert anonymous.status_code == 303
    assert forged.status_code == cross.status_code == 403
    assert broken.status_code == 422 and statuses == [422, 422, 422, 422]
    assert accepted == [200, 200, 200, 429]


def test_harmful_questions_and_model_failures_become_notices_not_errors(tmp_path):
    app, stub = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        refused = talk(client, token, {"text": "help me stalk my ex-girlfriend"}).json()
        stub.script.replies = []
        original = stub.script._reply

        def failing(user):
            raise AgentError("The model is down.")

        stub.script.llm.rules["friendly necktie"] = failing
        broken = talk(client, token, {"text": "Hello there"}).json()
        stub.script.llm.rules["friendly necktie"] = original
    assert refused["messages"][-1]["kind"] == "notice"
    assert refused["messages"][-1]["text"].startswith("Refused:")
    assert broken["messages"][-1]["kind"] == "notice"
    assert "could not reach the language model" in broken["messages"][-1]["text"]


def test_the_script_only_uses_lines_and_moods_that_exist():
    terminal = dict(
        re.findall(r"(\w+): \"(\w+)\"", re.search(r"TERMINAL = \{(.*?)\}", JS).group(1))
    )
    used = set(re.findall(r'say\("(\w+)"', JS)) | set(terminal)
    assert used <= set(BuddyTuning().lines)
    for key in BUDDY_LINES:
        assert key in used or key == "progress", key
    assert re.search(r'say\("progress"', JS)
    moods = set(re.findall(r'(?:setMood\(|mood: )"(\w+)"', JS)) | set(terminal.values())
    moods |= set(re.findall(r'return "(\w+)";', JS.split("const baseMood")[1].split("};")[0]))
    assert moods <= MOODS
    for mood in MOODS - {"idle"}:
        assert f'.tie[data-mood="{mood}"]' in STYLES, mood
    assert '.tie[data-mood="idle"].is-wiggle' in STYLES


def test_every_script_setting_exists_in_the_configuration():
    keys = set(re.findall(r"config\.(\w+)", JS))
    assert keys <= set(BuddyTuning.model_fields)


def test_the_script_talks_to_the_server_only_through_its_own_routes():
    routes = set(re.findall(r'(?:request|postJson|play)\(\s*[`"](/[^`"$]*)', JS))
    assert routes == {
        "/tie/messages",
        "/tie/send",
        "/tie/clear",
        "/buddy",
        "/buddy/speech/",
        "/chat/messages/",
    }
    assert "innerHTML" not in JS and "sessionStorage" in JS and "localStorage" not in JS


def test_tie_is_an_animated_svg_with_its_own_idle_life():
    root = ET.fromstring(TIE)
    namespace = "{http://www.w3.org/2000/svg}"
    animations = [
        element.get("attributeName")
        for element in root.iter()
        if element.tag in {f"{namespace}animate", f"{namespace}animateTransform"}
    ]
    assert animations.count("transform") >= 4 and animations.count("ry") >= 4
    assert all(
        element.get("repeatCount") == "indefinite"
        for element in root.iter()
        if "animate" in element.tag
    )
    durations = {element.get("dur") for element in root.iter() if "animate" in element.tag}
    assert len(durations) >= 4
    assert root.get("viewBox") == "0 0 120 200" and root.get("data-mood") == "idle"


def test_tie_has_a_clean_standalone_state_and_no_external_references():
    root = ET.fromstring(TIE)
    ids = re.findall(r'\sid="([^"]+)"', TIE)
    assert len(ids) == len(set(ids)) == 1 and f"url(#{ids[0]})" in TIE
    hidden = [
        element.get("class")
        for element in root.iter()
        if element.get("opacity") == "0" and element.get("class")
    ]
    assert len(hidden) == 8
    assert not re.search(r"(?:href|src)\s*=", TIE) and "<script" not in TIE and "<style" not in TIE
    assert TIE.splitlines()[0] == "<!-- src/agent/web/static/tie.svg -->" and TIE.count("<!--") == 1


def test_tie_and_the_logo_use_only_black_white_and_grey():
    colours = set(re.findall(r"#[0-9a-fA-F]{6}\b", TIE + LOGO))
    assert colours
    for colour in colours:
        red, green, blue = colour[1:3], colour[3:5], colour[5:7]
        assert red == green == blue, colour


def test_tie_stays_out_of_the_way_of_print_and_motion_preferences():
    assert "@media print" in STYLES and "max-width: 860px" in STYLES
    shared = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in shared and "reduced" in JS and "pauseAnimations" in JS
    assert "visibilitychange" in JS and "agent:event" in JS
