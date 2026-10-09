# src/agent/web/chat.py
from agent.web.common import (
    json_errors,
    principal_of,
    protected_form,
    protected_header,
    read_preferences,
    render,
    requires,
    text,
)
from agent.chat import (
    Defaults,
    load_profile,
    Profile,
)
from agent.errors import (
    InvalidRequest,
    RateLimitedError,
)
from agent.store import (
    Message,
    Thread,
)
from agent.web.context import (
    context_of,
    WebContext,
)
from agent.web.conversation import (
    Conversation,
    RESEARCH_KIND,
)
from agent.web.jobs import (
    CVUpload,
    WEB_OWNER,
)
from starlette.datastructures import (
    FormData,
    UploadFile,
)
from starlette.responses import (
    JSONResponse,
    Response,
)
from starlette.exceptions import HTTPException
from agent.permissions import Permission
from agent.web.prefs import Preferences
from agent.web.voice import speak_text
from starlette.requests import Request
from typing import Any

import json

THREAD_ID_CHARS = 64
TIE_MESSAGES = 40


def base_defaults(preferences: Preferences, profile: Profile) -> Defaults:
    """Build the chat defaults from user preferences and the saved profile.

    Args:
        preferences: The user's research preferences.
        profile: The saved company profile and standing requirements.

    Returns:
        A Defaults object with goal, options, skill limit, company and requirements.
    """
    return Defaults(
        goal=preferences.goal,
        options=preferences.options(),
        limit=preferences.skill_limit,
        company=profile.company(),
        requirements=list(profile.requirements),
    )


def message_json(message: Message) -> dict[str, Any]:
    """Convert a stored chat message to a JSON-ready dict.

    Args:
        message: The stored message.

    Returns:
        A dict with id, role, kind, text, data and creation time.
    """
    return {
        "id": message.id,
        "role": message.role,
        "kind": message.kind,
        "text": message.text,
        "data": message.data,
        "created": message.created,
    }


def thread_json(thread: Thread) -> dict[str, Any]:
    """Convert a chat thread to a JSON-ready dict.

    Args:
        thread: The stored thread.

    Returns:
        A dict with id, title and phase, where phase defaults to idle.
    """
    return {"id": thread.id, "title": thread.title, "phase": thread.state.get("phase", "idle")}


def conversation_of(ctx: WebContext) -> Conversation:
    """Return the conversation service of the web context.

    Args:
        ctx: Shared web context.

    Returns:
        The conversation service.

    Raises:
        InvalidRequest: when no language model is connected.
    """
    if ctx.conversation is None:
        raise InvalidRequest(
            "The chat needs a language model. An administrator can connect one under Connections."
        )
    return ctx.conversation


def admit(ctx: WebContext, owner: str) -> None:
    """Apply the chat message rate limit to an owner.

    Args:
        ctx: Shared web context.
        owner: Key to rate limit, normally the session id.

    Raises:
        RateLimitedError: when the owner sent too many messages recently.
    """
    if not ctx.chat_limiter.allow(owner):
        raise RateLimitedError("Too many messages; wait a few minutes.")


def research_thread(ctx: WebContext, identifier: str) -> Thread:
    """Load a research chat thread by id.

    Args:
        ctx: Shared web context.
        identifier: Thread id from the request.

    Returns:
        The thread.

    Raises:
        HTTPException: with status 404 when the id is empty, unknown or belongs to
            another kind of thread.
    """
    thread = ctx.store.thread(identifier) if identifier else None
    if thread is None or thread.kind != RESEARCH_KIND:
        raise HTTPException(404)
    return thread


@requires(Permission.CHAT_USE)
async def chat_page(request: Request) -> Response:
    """Render the chat page, optionally opened on an existing thread.

    Args:
        request: The incoming request, with an optional thread id in the path.

    Returns:
        The rendered chat page with the thread and its messages as initial data.
    """
    ctx = context_of(request)
    identifier = request.path_params.get("thread_id", "")
    thread = research_thread(ctx, identifier) if identifier else None
    found = ctx.store.messages(thread.id) if thread else []
    initial = {
        "thread": thread_json(thread) if thread else None,
        "messages": [message_json(item) for item in found],
    }
    return render(
        request,
        "chat.html",
        active="chat",
        thread=thread,
        initial=initial,
        welcome=ctx.settings.chat.lines["welcome"],
        suggestions=ctx.settings.chat.suggestions,
        max_chars=ctx.settings.chat.max_user_chars,
    )


async def read_cv(form: FormData, ctx: WebContext) -> CVUpload | None:
    """Read an uploaded CV from a submitted form.

    At most one byte more than the size limit is read, so an oversized upload is
    rejected without loading all of it. The file name is cut to 200 characters.

    Args:
        form: The parsed multipart form.
        ctx: Shared web context holding the size limit.

    Returns:
        The upload, or None when no file was sent.

    Raises:
        InvalidRequest: when the file is larger than the configured limit.
    """
    upload = form.get("cv")
    if not isinstance(upload, UploadFile) or not upload.filename:
        return None
    limit = ctx.settings.cv.max_bytes
    # Reading one extra byte detects an oversized file without loading all of it.
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise InvalidRequest(f"The CV may be at most {limit // 1024} KiB.")
    return CVUpload(data=data, filename=upload.filename[:200])


@requires(Permission.CHAT_USE)
@json_errors
async def chat_send(request: Request) -> Response:
    """Handle a chat message, with an optional CV, and return the reply.

    The form is checked for a valid CSRF token, the per-session rate limit is
    applied, and a new thread is created when no thread id is sent. Research is
    started only if the user holds the research permission.

    Args:
        request: The incoming form post.

    Returns:
        A JSON response with the thread, the new messages and any job id.

    Raises:
        InvalidRequest: when both the message and the CV are missing.
    """
    ctx = context_of(request)
    session = request.state.session
    conversation = conversation_of(ctx)
    form = await protected_form(request, session)
    try:
        message = text(form, "text", ctx.settings.chat.max_user_chars + 1)
        identifier = text(form, "thread", THREAD_ID_CHARS)
        cv = await read_cv(form, ctx)
    finally:
        await form.close()
    if not message and cv is None:
        raise InvalidRequest("Write a message first.")
    admit(ctx, session.id)
    thread = research_thread(ctx, identifier) if identifier else conversation.new_thread()
    preferences, _ = read_preferences(request)
    profile = load_profile(ctx.store)
    principal = principal_of(request)
    reply = await conversation.send(
        thread,
        message,
        cv,
        base_defaults(preferences, profile),
        profile,
        principal is not None and principal.can(Permission.RESEARCH_RUN),
    )
    return JSONResponse(
        {
            "thread": thread_json(reply.thread),
            "messages": [message_json(item) for item in reply.messages],
            "job_id": reply.job_id,
        }
    )


@requires(Permission.CHAT_USE)
@json_errors
async def chat_messages(request: Request) -> Response:
    """Return the messages of a thread newer than a given message id.

    Args:
        request: The incoming request with the thread id in the path and an
            optional after query parameter.

    Returns:
        A JSON response with the thread and its newer messages.
    """
    ctx = context_of(request)
    thread = research_thread(ctx, request.path_params["thread_id"])
    raw = request.query_params.get("after", "")
    after = int(raw) if raw.isdigit() else 0
    found = ctx.store.messages(thread.id, after)
    return JSONResponse(
        {"thread": thread_json(thread), "messages": [message_json(item) for item in found]}
    )


@requires(Permission.CHAT_USE)
@json_errors
async def chat_delete(request: Request) -> Response:
    """Delete a research chat thread with all its messages.

    The request must carry a valid CSRF header.

    Args:
        request: The incoming request with the thread id in the path.

    Returns:
        An empty 204 response.
    """
    ctx = protected_header(request, request.state.session)
    thread = research_thread(ctx, request.path_params["thread_id"])
    ctx.store.delete_thread(thread.id)
    return Response(status_code=204)


@requires(Permission.VOICE_USE)
@json_errors
async def chat_speech(request: Request) -> Response:
    """Turn an assistant message into spoken audio.

    Args:
        request: The incoming request with the message id in the path.

    Returns:
        An audio/mpeg response, or a 404 error when the message is unknown or was
        not written by the assistant.
    """
    ctx = context_of(request)
    raw = request.path_params["message_id"]
    message = ctx.store.message(int(raw)) if raw.isdigit() else None
    if message is None or message.role != "assistant":
        raise HTTPException(404)
    audio = await speak_text(ctx, message.text, request.state.session.id)
    return Response(audio, media_type="audio/mpeg")


@requires(Permission.TIE_USE)
@json_errors
async def tie_messages(request: Request) -> Response:
    """Return the recent messages of the Tie assistant thread.

    Args:
        request: The incoming request.

    Returns:
        A JSON response with the latest messages and the configured greeting.
    """
    ctx = context_of(request)
    thread = conversation_of(ctx).tie_thread()
    found = ctx.store.recent_messages(thread.id, TIE_MESSAGES)
    greeting = ctx.settings.chat.lines["tie_hello"]
    return JSONResponse({"messages": [message_json(item) for item in found], "hello": greeting})


@requires(Permission.TIE_USE)
@json_errors
async def tie_send(request: Request) -> Response:
    """Send a message to the Tie assistant and return the new messages.

    The body is JSON with a text field and optional job and page fields. The job
    is looked up only among jobs owned by the web owner, and the page name is cut
    to 40 characters. The request must carry a valid CSRF header.

    Args:
        request: The incoming request.

    Returns:
        A JSON response with the new messages.

    Raises:
        InvalidRequest: when the body is not valid JSON, the text is missing or
            empty, or the text exceeds the configured length.
    """
    session = request.state.session
    ctx = protected_header(request, session)
    conversation = conversation_of(ctx)
    try:
        body = json.loads((await request.body()).decode("utf-8"))
    except ValueError as error:
        raise InvalidRequest("The message is not valid JSON.") from error
    if not isinstance(body, dict) or not isinstance(body.get("text"), str):
        raise InvalidRequest("Write a message first.")
    message = body["text"].strip()
    if not message:
        raise InvalidRequest("Write a message first.")
    if len(message) > ctx.settings.chat.max_user_chars:
        raise InvalidRequest("That message is too long.")
    admit(ctx, session.id)
    job_id = body.get("job")
    # Looking the job up with the web owner stops a user from attaching jobs created through the
    # API.
    job = ctx.jobs.get(job_id, WEB_OWNER) if isinstance(job_id, str) and job_id else None
    page = body.get("page") if isinstance(body.get("page"), str) else ""
    found = await conversation.tie(message, load_profile(ctx.store), job, page[:40])
    return JSONResponse({"messages": [message_json(item) for item in found]})


@requires(Permission.TIE_USE)
@json_errors
async def tie_clear(request: Request) -> Response:
    """Delete the Tie assistant thread and its messages.

    The request must carry a valid CSRF header.

    Args:
        request: The incoming request.

    Returns:
        An empty 204 response.
    """
    ctx = protected_header(request, request.state.session)
    ctx.store.delete_thread(conversation_of(ctx).tie_thread().id)
    return Response(status_code=204)
