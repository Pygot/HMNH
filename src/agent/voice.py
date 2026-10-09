# src/agent/voice.py
from agent.http import (
    api_json,
    api_request,
    TTLCache,
)
from agent.config import (
    Endpoints,
    VoiceTuning,
)
from agent.errors import (
    InvalidRequest,
    UpstreamError,
)
from pydantic import SecretStr

import hashlib
import httpx


def clip(text: str, limit: int) -> str:
    """Collapse whitespace and shorten text to a limit at a word boundary.

    Args:
        text: The text to clip.
        limit: The maximum number of characters.

    Returns:
        The cleaned text, cut at the last whole word that fits within the limit.
    """
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    head = cleaned[:limit]
    return head.rsplit(" ", 1)[0] if " " in head else head


class VoiceService:
    """A client for ElevenLabs text-to-speech and speech-to-text.

    The API key is held as a secret and only sent in the xi-api-key header. Generated
    audio is cached in memory by the voice, model, format and text.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        key: SecretStr,
        voice_id: str,
        endpoints: Endpoints,
        tuning: VoiceTuning,
        cache: TTLCache[bytes],
    ):
        """Initialize the service.

        Args:
            client: The shared asynchronous HTTP client.
            key: The ElevenLabs API key.
            voice_id: The id of the voice used for speech.
            endpoints: The configured service endpoints.
            tuning: The voice settings for models, limits and timeouts.
            cache: The cache that holds generated audio.
        """
        self._client = client
        self._key = key
        self._voice_id = voice_id
        self._base = endpoints.elevenlabs_api
        self._tuning = tuning
        self._cache = cache

    def _auth(self) -> dict[str, str]:
        """Return the authentication headers for ElevenLabs requests.

        Returns:
            A header map holding the API key.
        """
        return {"xi-api-key": self._key.get_secret_value()}

    async def ping(self) -> None:
        """Check that ElevenLabs is reachable and accepts the API key.

        The check lists the available models.

        Raises:
            AuthenticationError: when the key is rejected.
            UpstreamError: when the service fails or times out.
        """
        await api_json(
            self._client,
            "ElevenLabs",
            "GET",
            f"{self._base}/models",
            headers=self._auth(),
            time_limit=self._tuning.timeout_seconds,
        )

    async def speak(self, text: str) -> bytes:
        """Convert text to spoken audio.

        The text is clipped to the configured length. Identical requests are served from
        the cache.

        Args:
            text: The text to speak.

        Returns:
            The audio bytes in the configured output format.

        Raises:
            InvalidRequest: when the text is empty.
            UpstreamError: when the service fails or returns unusable audio.
        """
        spoken = clip(text, self._tuning.max_speech_chars)
        if not spoken:
            raise InvalidRequest("There is nothing to say.")
        tuning = self._tuning
        # The cache key covers everything that changes the audio and is hashed to keep the text out
        # of it.
        recipe = f"{self._voice_id}|{tuning.tts_model}|{tuning.output_format}|{spoken}"
        digest = hashlib.sha256(recipe.encode()).hexdigest()
        cached = self._cache.get(digest)
        if cached is not None:
            return cached
        response = await api_request(
            self._client,
            "ElevenLabs speech",
            "POST",
            f"{self._base}/text-to-speech/{self._voice_id}",
            headers={**self._auth(), "Accept": "audio/mpeg"},
            params={"output_format": tuning.output_format},
            json={
                "text": spoken,
                "model_id": tuning.tts_model,
                "voice_settings": {
                    "stability": tuning.stability,
                    "similarity_boost": tuning.similarity_boost,
                },
            },
            time_limit=tuning.timeout_seconds,
        )
        audio = response.content
        # Empty or oversized audio is refused so a bad response is never cached or returned.
        if not audio or len(audio) > tuning.max_response_bytes:
            raise UpstreamError("ElevenLabs speech returned an unexpected audio response.")
        self._cache.put(digest, audio)
        return audio

    async def transcribe(self, audio: bytes, content_type: str) -> str:
        """Convert a recording to text.

        Args:
            audio: The raw recording bytes.
            content_type: The media type of the recording; parameters are ignored.

        Returns:
            The transcript, clipped to the configured length.

        Raises:
            InvalidRequest: when the audio type is unsupported, the recording is empty or too
                large, or no speech was detected.
            UpstreamError: when the service fails or returns an unexpected response.
        """
        tuning = self._tuning
        kind = content_type.split(";")[0].strip().lower()
        extension = tuning.audio_types.get(kind)
        if extension is None:
            raise InvalidRequest(
                f"Unsupported audio type; send one of {', '.join(sorted(tuning.audio_types))}."
            )
        if not audio:
            raise InvalidRequest("The recording is empty.")
        if len(audio) > tuning.max_audio_bytes:
            raise InvalidRequest(f"The recording may be at most {tuning.max_audio_bytes} bytes.")
        form = {"model_id": tuning.stt_model}
        if tuning.language_code:
            form["language_code"] = tuning.language_code
        body = await api_json(
            self._client,
            "ElevenLabs transcription",
            "POST",
            f"{self._base}/speech-to-text",
            headers=self._auth(),
            data=form,
            files={"file": (f"recording.{extension}", audio, kind)},
            time_limit=tuning.timeout_seconds,
        )
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str):
            raise UpstreamError("ElevenLabs transcription returned an unexpected response shape.")
        transcript = clip(text, tuning.max_transcript_chars)
        if not transcript:
            raise InvalidRequest("No speech was detected in the recording.")
        return transcript
