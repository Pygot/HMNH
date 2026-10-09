# src/agent/web/voice.py
from agent.errors import (
    InvalidRequest,
    RateLimitedError,
    VoiceDisabled,
)
from agent.speech import (
    describe_clarification,
    describe_report,
)
from agent.web.jobs import (
    Job,
    JobState,
)
from agent.web.context import WebContext
from starlette.requests import Request
from agent.voice import VoiceService

VOICE_LIMITED = "Too many voice requests; wait a few minutes."


def voice_of(ctx: WebContext) -> VoiceService:
    """Return the voice service of the web context.

    Args:
        ctx: The web context holding the services.

    Returns:
        The configured VoiceService.

    Raises:
        VoiceDisabled: when no voice service is configured.
    """
    voice = ctx.services.voice if ctx.services is not None else None
    if voice is None:
        raise VoiceDisabled("Voice is disabled; set ELEVENLABS_API_KEY.")
    return voice


def _admit(ctx: WebContext, owner: str) -> None:
    """Count a voice request against the owner's rate limit.

    Args:
        ctx: The web context holding the voice rate limiter.
        owner: The identifier of the user making the request.

    Raises:
        RateLimitedError: when the owner has used up the allowed voice requests.
    """
    if not ctx.voice_limiter.allow(owner):
        raise RateLimitedError(VOICE_LIMITED)


async def transcript(ctx: WebContext, request: Request, owner: str) -> str:
    """Transcribe an uploaded recording from the request body.

    Args:
        ctx: The web context.
        request: The request whose streamed body is the recording.
        owner: The identifier of the user, used for rate limiting.

    Returns:
        The transcript of the recording.

    Raises:
        VoiceDisabled: when voice is not configured.
        RateLimitedError: when the owner is over the voice request limit.
        InvalidRequest: when the recording is too large or not usable.
    """
    voice = voice_of(ctx)
    _admit(ctx, owner)
    limit = ctx.settings.voice.max_audio_bytes
    audio = bytearray()
    async for chunk in request.stream():
        audio.extend(chunk)
        # The size is checked while streaming so an oversized upload is cut off without buffering it
        # all.
        if len(audio) > limit:
            raise InvalidRequest(f"The recording may be at most {limit} bytes.")
    return await voice.transcribe(bytes(audio), request.headers.get("content-type", ""))


def spoken_text(ctx: WebContext, job: Job) -> str:
    """Create the text to read aloud for a job.

    Args:
        ctx: The web context holding the voice settings.
        job: The research job to describe.

    Returns:
        A spoken summary of the report or of the pending question for a finished or
        waiting job, the error for a failed job, or a progress line otherwise.
    """
    if job.state is JobState.DONE and job.report is not None:
        return describe_report(job.report, ctx.settings.voice.spoken_flags)
    if job.state is JobState.NEEDS_INPUT and job.clarification is not None:
        return describe_clarification(job.clarification)
    if job.state is JobState.FAILED:
        return f"The search stopped. {job.error}"
    return f"The search is still running. {job.stage}."


async def speak_text(ctx: WebContext, line: str, owner: str) -> bytes:
    """Convert a line of text to speech.

    Args:
        ctx: The web context.
        line: The text to speak.
        owner: The identifier of the user, used for rate limiting.

    Returns:
        The audio bytes.

    Raises:
        VoiceDisabled: when voice is not configured.
        RateLimitedError: when the owner is over the voice request limit.
    """
    voice = voice_of(ctx)
    _admit(ctx, owner)
    return await voice.speak(line)


async def speech(ctx: WebContext, job: Job, owner: str) -> bytes:
    """Convert the current state of a job to speech.

    Args:
        ctx: The web context.
        job: The research job to describe aloud.
        owner: The identifier of the user, used for rate limiting.

    Returns:
        The audio bytes.

    Raises:
        VoiceDisabled: when voice is not configured.
        RateLimitedError: when the owner is over the voice request limit.
    """
    voice = voice_of(ctx)
    _admit(ctx, owner)
    return await voice.speak(spoken_text(ctx, job))
