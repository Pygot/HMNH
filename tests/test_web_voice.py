# tests/test_web_voice.py
from tests.web_helpers import (
    API_TOKEN,
    bearer,
    Browser,
    build_app,
    clarification,
    FakeVoice,
    open_client,
    ORIGIN,
    say,
    start,
    wait_for,
)
from agent.errors import (
    InvalidRequest,
    RateLimitedError,
    UpstreamError,
)

import pytest

AUDIO = b"fake webm recording"
MP3 = b"ID3 fake mp3 audio"


def transcribe(client, token, body=AUDIO, kind="audio/webm", origin=ORIGIN, headers=None):
    sent = {"content-type": kind, "x-csrf-token": token, **(headers or {})}
    if origin is not None:
        sent["origin"] = origin
    return client.post("/voice/transcribe", content=body, headers=sent)


def finished_job(browser, token):
    location = start(browser, token)
    wait_for(browser, location)
    return location


def assistant_message(browser, token, text="Jan Novak"):
    reply = say(browser, token, text).json()
    return next(item for item in reply["messages"] if item["role"] == "assistant")


def test_voice_is_invisible_and_unreachable_without_a_key(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        home = client.get("/").text
        settings = client.get("/settings").text
        location = finished_job(browser, token)
        job = client.get(location).text
        message = assistant_message(browser, token)
        recorded = transcribe(client, token)
        spoken = client.get(f"{location}/speech")
        said = client.get(f"/chat/messages/{message['id']}/speech")
        line = client.get("/buddy/speech/greeting")
    for page in (home, job):
        for marker in ("data-mic", "data-tie-mic", "data-voice-toggle", "data-speak"):
            assert marker not in page
        assert "ElevenLabs" not in page and 'data-voice=""' in page
    assert "Off, an administrator can add a voice key under Connections" in settings
    assert recorded.status_code == spoken.status_code == said.status_code == line.status_code == 404
    assert recorded.json()["error"].startswith("Voice is disabled")


def test_voice_controls_appear_when_a_key_is_configured(tmp_path):
    app, _ = build_app(tmp_path, voice=FakeVoice())
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        home = client.get("/").text
        settings = client.get("/settings").text
        job = client.get(finished_job(browser, token)).text
    for marker in ("data-mic", "data-tie-mic", "data-voice-toggle"):
        assert marker in home
    assert 'data-voice="on"' in home and 'data-seconds="30"' in home
    assert "Voice features send your recording" in home and "ElevenLabs" in home
    assert "On, ElevenLabs" in settings and "Voice features send your recording" in settings
    assert 'data-speak="/jobs/' in job


def test_a_reply_in_the_chat_is_spoken_from_the_stored_message(tmp_path):
    voice = FakeVoice()
    app, _ = build_app(tmp_path, voice=voice)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        message = assistant_message(browser, token)
        user = say(browser, token, "Jan").json()["messages"][0]
        response = client.get(f"/chat/messages/{message['id']}/speech")
        refused = client.get(f"/chat/messages/{user['id']}/speech")
        missing = client.get("/chat/messages/999999/speech")
        broken = client.get("/chat/messages/abc/speech")
    assert response.status_code == 200 and response.content == MP3
    assert voice.spoken == [message["text"]]
    assert refused.status_code == missing.status_code == broken.status_code == 404


def test_the_buddy_speaks_only_its_own_fixed_lines(tmp_path):
    voice = FakeVoice()
    app, _ = build_app(tmp_path, voice=voice)
    with open_client(app) as client:
        browser = Browser(client)
        browser.logged_in_token()
        greeting = client.get("/buddy/speech/greeting")
        unknown = client.get("/buddy/speech/anything-else")
    assert greeting.status_code == 200 and voice.spoken[0].startswith("Hi, I am Tie")
    assert unknown.status_code == 404 and len(voice.spoken) == 1


def test_a_recording_is_transcribed_through_the_server(tmp_path):
    voice = FakeVoice()
    app, _ = build_app(tmp_path, voice=voice)
    with open_client(app) as client:
        token = Browser(client).logged_in_token()
        response = transcribe(client, token, kind="audio/webm;codecs=opus")
    assert response.status_code == 200
    assert response.json() == {"text": "Jan Novak from Brno"}
    assert voice.heard == [(AUDIO, "audio/webm;codecs=opus")]
    assert response.headers["cache-control"] == "no-store"


def test_transcription_is_protected_like_every_other_action(tmp_path):
    voice = FakeVoice()
    app, _ = build_app(tmp_path, voice=voice)
    with open_client(app) as client:
        anonymous = client.post("/voice/transcribe", content=AUDIO, headers={"origin": ORIGIN})
        token = Browser(client).logged_in_token()
        assert anonymous.status_code == 303
        for headers in ({}, {"x-csrf-token": "forged"}, {"x-csrf-token": token[:-1]}):
            sent = {"origin": ORIGIN, "content-type": "audio/webm", **headers}
            assert client.post("/voice/transcribe", content=AUDIO, headers=sent).status_code == 403
        assert transcribe(client, token, origin="https://evil.example").status_code == 403
        cross = transcribe(client, token, origin=None, headers={"sec-fetch-site": "cross-site"})
        assert cross.status_code == 403
        assert client.get("/voice/transcribe").status_code == 405
    assert voice.heard == []


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (InvalidRequest("No speech was detected in the recording."), 422),
        (UpstreamError("ElevenLabs transcription timed out."), 502),
        (RateLimitedError("ElevenLabs rate limit reached (HTTP 429)."), 429),
    ],
)
def test_voice_failures_become_json_errors(tmp_path, error, status):
    voice = FakeVoice()
    voice.error = error
    app, _ = build_app(tmp_path, voice=voice)
    with open_client(app) as client:
        token = Browser(client).logged_in_token()
        response = transcribe(client, token)
    assert response.status_code == status
    assert response.json() == {"error": str(error)}


def test_oversized_recordings_are_refused_while_streaming(tmp_path):
    voice = FakeVoice()
    app, _ = build_app(tmp_path, voice=voice)
    limit = app.state.ctx.settings.voice.max_audio_bytes
    with open_client(app) as client:
        token = Browser(client).logged_in_token()
        response = transcribe(client, token, body=b"a" * (limit + 1))
    assert response.status_code == 422
    assert f"at most {limit} bytes" in response.json()["error"]
    assert voice.heard == []


def test_voice_requests_are_rate_limited_per_session(tmp_path):
    voice = FakeVoice()
    app, _ = build_app(tmp_path, voice=voice, voice_requests=2)
    with open_client(app) as client:
        token = Browser(client).logged_in_token()
        statuses = [transcribe(client, token).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]
    assert len(voice.heard) == 2


def test_a_finished_report_is_spoken_from_server_side_text(tmp_path):
    voice = FakeVoice()
    app, _ = build_app(tmp_path, voice=voice)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = finished_job(browser, token)
        response = client.get(f"{location}/speech")
        again = client.get(f"{location}/speech")
    assert response.status_code == 200 and again.status_code == 200
    assert response.content == MP3 and response.headers["content-type"] == "audio/mpeg"
    assert response.headers["cache-control"] == "no-store"
    assert voice.spoken[0].startswith("Report on Jan Novak. Overall rating 3.4 out of 5")


def test_a_question_a_failure_and_a_running_job_are_spoken_too(tmp_path):
    voice = FakeVoice()
    app, stub = build_app(tmp_path, voice=voice, max_jobs_per_session=3)
    stub.results = [clarification(), RuntimeError("secret detail")]
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        asking = finished_job(browser, token)
        failing = finished_job(browser, token)
        gate = stub.hold()
        running = start(browser, token)
        for location in (asking, failing, running):
            assert client.get(f"{location}/speech").status_code == 200
        gate.set()
        wait_for(browser, running)
    assert voice.spoken[0].startswith("Which one?") and "Option 1: Jan <b>A</b>" in voice.spoken[0]
    assert voice.spoken[1].startswith("The search stopped. An unexpected error occurred")
    assert "secret detail" not in voice.spoken[1]
    assert voice.spoken[2].startswith("The search is still running.")


def test_speech_needs_a_session_and_never_reaches_api_jobs(tmp_path):
    voice = FakeVoice()
    app, _ = build_app(tmp_path, voice=voice, api_token=API_TOKEN)
    with open_client(app) as owner:
        mine = Browser(owner)
        token = mine.logged_in_token()
        location = finished_job(mine, token)
        started = owner.post(
            "/api/v1/research",
            json={"goal": "hiring", "name": "Eva Svobodova", "lawful_purpose": True},
            headers=bearer(),
        ).json()
        assert owner.get(f"{location}/speech").status_code == 200
        assert owner.get(f"/jobs/{started['id']}/speech").status_code == 404
    with open_client(app) as stranger:
        assert stranger.get(f"{location}/speech").status_code == 303
        assert stranger.get("/chat/messages/1/speech").status_code == 303
    assert len(voice.spoken) == 1


def test_speech_failures_are_json_errors(tmp_path):
    voice = FakeVoice()
    voice.error = UpstreamError("ElevenLabs speech timed out.")
    app, _ = build_app(tmp_path, voice=voice)
    with open_client(app) as client:
        browser = Browser(client)
        location = finished_job(browser, browser.logged_in_token())
        response = client.get(f"{location}/speech")
    assert response.status_code == 502
    assert response.json() == {"error": "ElevenLabs speech timed out."}


def test_the_browser_may_use_the_microphone_and_play_blob_audio(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        response = client.get("/")
    assert "microphone=(self)" in response.headers["permissions-policy"]
    assert "camera=()" in response.headers["permissions-policy"]
    policy = response.headers["content-security-policy"]
    assert "media-src 'self' blob:" in policy and "connect-src 'self'" in policy
    assert "default-src 'none'" in policy


def api_client(tmp_path, voice=None, **overrides):
    app, stub = build_app(tmp_path, voice=voice, api_token=API_TOKEN, **overrides)
    return app, stub


def test_the_api_reports_that_voice_is_disabled(tmp_path):
    app, _ = api_client(tmp_path)
    with open_client(app) as client:
        recorded = client.post(
            "/api/v1/voice/transcribe",
            content=AUDIO,
            headers={**bearer(), "content-type": "audio/webm"},
        )
        spoken = client.get("/api/v1/jobs/anything/speech", headers=bearer())
    assert recorded.status_code == 404
    assert recorded.json()["error"]["code"] == "voice_disabled"
    assert spoken.status_code == 404 and spoken.json()["error"]["code"] == "not_found"


def test_the_api_transcribes_and_speaks(tmp_path):
    voice = FakeVoice()
    app, stub = api_client(tmp_path, voice)
    with open_client(app) as client:
        started = client.post(
            "/api/v1/research",
            json={"goal": "hiring", "name": "Jan Novak", "lawful_purpose": True},
            headers=bearer(),
        ).json()
        for _ in range(200):
            state = client.get(f"/api/v1/jobs/{started['id']}", headers=bearer()).json()["state"]
            if state == "done":
                break
        recorded = client.post(
            "/api/v1/voice/transcribe",
            content=AUDIO,
            headers={**bearer(), "content-type": "audio/ogg"},
        )
        spoken = client.get(f"/api/v1/jobs/{started['id']}/speech", headers=bearer())
        missing = client.get("/api/v1/jobs/unknown/speech", headers=bearer())
        unauthorised = client.get(f"/api/v1/jobs/{started['id']}/speech")
    assert recorded.status_code == 200 and recorded.json() == {"text": "Jan Novak from Brno"}
    assert voice.heard == [(AUDIO, "audio/ogg")]
    assert spoken.status_code == 200 and spoken.content == MP3
    assert spoken.headers["content-type"] == "audio/mpeg"
    assert missing.status_code == 404 and unauthorised.status_code == 401
    assert stub.calls


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (InvalidRequest("The recording is empty."), 422, "invalid_request"),
        (UpstreamError("ElevenLabs transcription timed out."), 502, "upstream_error"),
        (RateLimitedError("ElevenLabs rate limit reached (HTTP 429)."), 429, "rate_limited"),
    ],
)
def test_api_voice_errors_use_the_standard_error_shape(tmp_path, error, status, code):
    voice = FakeVoice()
    voice.error = error
    app, _ = api_client(tmp_path, voice)
    with open_client(app) as client:
        response = client.post(
            "/api/v1/voice/transcribe",
            content=AUDIO,
            headers={**bearer(), "content-type": "audio/webm"},
        )
    assert response.status_code == status
    assert response.json()["error"] == {"code": code, "message": str(error)}


def test_api_voice_requests_are_rate_limited(tmp_path):
    voice = FakeVoice()
    app, _ = api_client(tmp_path, voice, voice_requests=1)
    with open_client(app) as client:
        headers = {**bearer(), "content-type": "audio/webm"}
        first = client.post("/api/v1/voice/transcribe", content=AUDIO, headers=headers)
        second = client.post("/api/v1/voice/transcribe", content=AUDIO, headers=headers)
    assert (first.status_code, second.status_code) == (200, 429)
    assert second.json()["error"]["code"] == "rate_limited"


def test_the_api_documents_the_voice_endpoints(tmp_path):
    app, _ = api_client(tmp_path)
    with open_client(app) as client:
        document = client.get("/api/v1/openapi.json", headers=bearer()).json()
    upload = document["paths"]["/api/v1/voice/transcribe"]["post"]["requestBody"]
    assert upload["content"]["audio/*"]["schema"] == {"type": "string", "format": "binary"}
    assert "get" in document["paths"]["/api/v1/jobs/{job_id}/speech"]
