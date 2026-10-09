# tests/test_voice.py
from agent.errors import (
    AuthenticationError,
    InvalidRequest,
    RateLimitedError,
    UpstreamError,
)
from tests.helpers import (
    body_of,
    json_response,
    make_voice,
    mock_client,
)
from agent.config import VoiceTuning
from agent.voice import clip

import pytest
import httpx

VOICE = "21m00Tcm4TlvDq8ikWAM"
MP3 = b"ID3 fake mp3 bytes"


def speech_handler(calls):
    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=MP3, headers={"content-type": "audio/mpeg"})

    return handler


def test_clip_collapses_whitespace_and_cuts_at_a_word():
    assert clip("  hello \n  world  ", 50) == "hello world"
    assert clip("alpha beta gamma", 12) == "alpha beta"
    assert clip("abcdefghij", 4) == "abcd"
    assert clip("   ", 10) == ""


async def test_speech_request_shape():
    calls = []
    async with mock_client(speech_handler(calls)) as client:
        audio = await make_voice(client).speak("Report on Jan Novak.")
    assert audio == MP3
    request = calls[0]
    assert request.method == "POST"
    assert request.url.path == f"/v1/text-to-speech/{VOICE}"
    assert request.url.host == "api.elevenlabs.io"
    assert request.url.params["output_format"] == "mp3_44100_64"
    assert request.headers["xi-api-key"] == "eleven-key"
    assert request.headers["accept"] == "audio/mpeg"
    assert "eleven-key" not in str(request.url)
    assert body_of(request) == {
        "text": "Report on Jan Novak.",
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }


async def test_speech_is_cached_per_text_and_voice():
    calls = []
    async with mock_client(speech_handler(calls)) as client:
        voice = make_voice(client)
        await voice.speak("Same words")
        await voice.speak("Same   words")
        assert len(calls) == 1
        await voice.speak("Other words")
        assert len(calls) == 2
        await make_voice(client, voice="AnotherVoice123", cache=None).speak("Same words")
        assert len(calls) == 3
        assert calls[2].url.path.endswith("/AnotherVoice123")


async def test_long_text_is_clipped_to_the_configured_limit():
    calls = []
    tuning = VoiceTuning(max_speech_chars=60)
    async with mock_client(speech_handler(calls)) as client:
        await make_voice(client, tuning=tuning).speak("word " * 100)
    assert len(body_of(calls[0])["text"]) <= 60


async def test_the_voice_and_models_come_from_the_configuration():
    calls = []
    tuning = VoiceTuning(tts_model="eleven_flash_v2_5", output_format="mp3_22050_32", stability=0.2)
    async with mock_client(speech_handler(calls)) as client:
        await make_voice(client, tuning=tuning).speak("hello")
    assert calls[0].url.params["output_format"] == "mp3_22050_32"
    assert body_of(calls[0])["model_id"] == "eleven_flash_v2_5"
    assert body_of(calls[0])["voice_settings"]["stability"] == 0.2


async def test_there_must_be_something_to_say():
    async with mock_client(speech_handler([])) as client:
        with pytest.raises(InvalidRequest, match="nothing to say"):
            await make_voice(client).speak("   ")


@pytest.mark.parametrize("content", [b"", b"x" * 20_000])
async def test_unexpected_audio_responses_are_rejected(content):
    tuning = VoiceTuning(max_response_bytes=10_000)
    async with mock_client(lambda request: httpx.Response(200, content=content)) as client:
        with pytest.raises(UpstreamError, match="unexpected audio"):
            await make_voice(client, tuning=tuning).speak("hello")


async def test_provider_errors_are_mapped_and_never_leak_the_key():
    async with mock_client(lambda request: httpx.Response(401)) as client:
        with pytest.raises(AuthenticationError, match="ElevenLabs speech") as caught:
            await make_voice(client).speak("hello")
    assert "eleven-key" not in str(caught.value)
    async with mock_client(lambda request: httpx.Response(429, headers={"retry-after": "3"})) as c:
        with pytest.raises(RateLimitedError, match="Retry after 3s"):
            await make_voice(c).speak("hello")
    detail = {"detail": {"status": "quota_exceeded", "message": "You have no characters left."}}
    async with mock_client(lambda request: json_response(detail, 400)) as client:
        with pytest.raises(UpstreamError, match="no characters left"):
            await make_voice(client).speak("hello")
    async with mock_client(lambda request: json_response({"detail": "Bad voice"}, 422)) as client:
        with pytest.raises(UpstreamError, match="Bad voice"):
            await make_voice(client).speak("hello")


async def test_transcription_request_shape():
    calls = []

    def handler(request):
        calls.append(request)
        return json_response({"text": " Jan   Novak from Brno. ", "language_code": "eng"})

    async with mock_client(handler) as client:
        text = await make_voice(client).transcribe(b"RIFFaudio", "audio/webm;codecs=opus")
    assert text == "Jan Novak from Brno."
    request = calls[0]
    assert request.url.path == "/v1/speech-to-text"
    assert request.headers["xi-api-key"] == "eleven-key"
    assert request.headers["content-type"].startswith("multipart/form-data")
    body = request.content
    assert b'name="model_id"' in body and b"scribe_v1" in body
    assert b'filename="recording.webm"' in body
    assert b"Content-Type: audio/webm" in body and b"RIFFaudio" in body
    assert b"language_code" not in body


async def test_the_language_can_be_pinned():
    calls = []

    def handler(request):
        calls.append(request)
        return json_response({"text": "ahoj"})

    tuning = VoiceTuning(language_code="ces")
    async with mock_client(handler) as client:
        await make_voice(client, tuning=tuning).transcribe(b"a", "audio/ogg")
    assert b"language_code" in calls[0].content and b"ces" in calls[0].content
    assert b'filename="recording.ogg"' in calls[0].content


async def test_transcripts_are_clipped():
    tuning = VoiceTuning(max_transcript_chars=30)
    async with mock_client(lambda request: json_response({"text": "word " * 50})) as client:
        text = await make_voice(client, tuning=tuning).transcribe(b"a", "audio/mp4")
    assert len(text) <= 30


@pytest.mark.parametrize(
    ("audio", "kind", "message"),
    [
        (b"data", "text/plain", "Unsupported audio type"),
        (b"data", "", "Unsupported audio type"),
        (b"", "audio/webm", "recording is empty"),
        (b"x" * 11_000, "audio/webm", "at most 10000 bytes"),
    ],
)
async def test_invalid_recordings_are_rejected_before_any_request(audio, kind, message):
    calls = []
    tuning = VoiceTuning(max_audio_bytes=10_000)
    async with mock_client(speech_handler(calls)) as client:
        with pytest.raises(InvalidRequest, match=message):
            await make_voice(client, tuning=tuning).transcribe(audio, kind)
    assert calls == []


@pytest.mark.parametrize("body", [{"words": []}, {"text": 5}, [1, 2]])
async def test_unexpected_transcription_shapes_fail_loudly(body):
    async with mock_client(lambda request: json_response(body)) as client:
        with pytest.raises(UpstreamError, match="unexpected response shape"):
            await make_voice(client).transcribe(b"a", "audio/webm")


async def test_silence_is_reported():
    async with mock_client(lambda request: json_response({"text": "  "})) as client:
        with pytest.raises(InvalidRequest, match="No speech was detected"):
            await make_voice(client).transcribe(b"a", "audio/webm")


async def test_timeouts_become_upstream_errors():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    async with mock_client(handler) as client:
        with pytest.raises(UpstreamError, match="timed out"):
            await make_voice(client).speak("hello")


async def test_a_missing_plan_explains_which_voice_cannot_be_used():
    detail = {
        "detail": {
            "code": "paid_plan_required",
            "message": "Free users cannot use library voices via the API.",
        }
    }
    async with mock_client(lambda request: json_response(detail, 402)) as client:
        with pytest.raises(UpstreamError, match=r"HTTP 402\): Free users cannot use library"):
            await make_voice(client).speak("hello")
