# src/agent/web/talk.py
from agent.web.common import (
    audit,
    json_errors,
    protected_form,
    protected_header,
    render,
    requires,
    text,
)
from agent.messaging import (
    Conversation,
    MAX_ID,
    Message,
)
from agent.web.account import (
    password_ok,
    throttle,
    WRONG_CREDENTIAL,
)
from starlette.responses import (
    JSONResponse,
    RedirectResponse,
    Response,
)
from agent.errors import (
    InvalidRequest,
    RateLimitedError,
)
from agent.web.context import (
    context_of,
    WebContext,
)
from starlette.exceptions import HTTPException
from starlette.requests import Request
from agent.web.format import stamp
from typing import Any

import json

MODULE = "messaging_enabled"
BACK = ("/announcements", "/messages", "/")
SLOW_DOWN = "You are sending messages very fast; wait a moment."
E2EE_OFF = "Private conversations are switched off."
NOTES = {
    "created": "The conversation was created.",
    "posted": "The announcement was posted.",
    "removed": "Removed.",
    "saved": "Saved.",
}


def busy(request: Request, data: dict[str, Any]) -> Response | None:
    """Return a failure response when the user sends messages too fast.

    Args:
        request: the incoming request, used to identify the user and limiter.
        data: the parsed request body, used to pick the response format.

    Returns:
        None when the request may proceed, else a rate limit failure response.
    """
    ctx = context_of(request)
    if ctx.message_limiter.allow(request.state.principal.user_id):
        return None
    return failure(data, RateLimitedError(SLOW_DOWN), request)


def conversation_or_404(request: Request) -> Conversation:
    """Return the conversation from the request path if the user can see it.

    Args:
        request: the incoming request carrying the conversation id.

    Returns:
        The conversation the current user belongs to.

    Raises:
        HTTPException: 404 when it does not exist or the user is not a member.
    """
    ctx = context_of(request)
    found = ctx.messenger.conversation(
        request.state.principal.user_id, request.path_params["conversation_id"]
    )
    if found is None:
        raise HTTPException(404)
    return found


def message_json(ctx: WebContext, message: Message) -> dict[str, Any]:
    """Convert a message into the JSON shape sent to the browser.

    Args:
        ctx: the web context, used for the time format.
        message: the message to convert.

    Returns:
        A dictionary with the message fields and a formatted timestamp.
    """
    return {
        "id": message.id,
        "sender_id": message.sender_id,
        "sender_name": message.sender_name,
        "text": message.text,
        "cipher": message.cipher,
        "encrypted": message.encrypted,
        "version": message.version,
        "when": stamp(message.ts, ctx.web.time_format),
        "removed": message.removed,
        "mine": message.mine,
    }


async def body_of(request: Request) -> dict[str, Any]:
    """Parse the request body from JSON or from a protected form.

    JSON bodies need the anti forgery header and are size limited. Form bodies
    need the form token and have each field cut to 4000 characters. The result
    has a _json flag telling which format came in.

    Args:
        request: the incoming request.

    Returns:
        The body fields as a dictionary.

    Raises:
        InvalidRequest: when the JSON is too large, malformed or not an object.
    """
    ctx = context_of(request)
    if request.headers.get("content-type", "").startswith("application/json"):
        protected_header(request, request.state.session)
        raw = (await request.body()).decode("utf-8", errors="replace")
        # The cap is checked before parsing so an oversized body is never decoded as JSON.
        if len(raw) > ctx.settings.messages.max_cipher_chars * 4:
            raise InvalidRequest("That request is too large.")
        try:
            parsed = json.loads(raw)
        except (ValueError, RecursionError) as error:
            raise InvalidRequest("The request is not valid JSON.") from error
        if not isinstance(parsed, dict):
            raise InvalidRequest("The request must be an object.")
        parsed["_json"] = True
        return parsed
    form = await protected_form(request, request.state.session)
    try:
        values: dict[str, Any] = {key: text(form, key, 4000) for key in form}
        values["members"] = [item for item in form.getlist("members") if isinstance(item, str)]
    finally:
        await form.close()
    values["_json"] = False
    return values


def reply(data: dict[str, Any], target: str, payload: dict[str, Any] | None = None) -> Response:
    """Build the success response for either a JSON or a form request.

    Args:
        data: the parsed request body, used to pick the response format.
        target: the page to redirect to for form requests.
        payload: the JSON body for JSON requests, defaults to an ok flag.

    Returns:
        A JSON response, or a 303 redirect to the target.
    """
    if data.get("_json"):
        return JSONResponse(payload or {"ok": True})
    return RedirectResponse(target, status_code=303)


def failure(data: dict[str, Any], error: Exception, request: Request) -> Response:
    """Build the error response for either a JSON or a form request.

    Args:
        data: the parsed request body, used to pick the response format.
        error: the error to show.
        request: the incoming request, used to render the page again.

    Returns:
        A 422 JSON response, or the messages page with a 400 status and the error.
    """
    if data.get("_json"):
        return JSONResponse({"error": str(error)}, status_code=422)
    return messages_view(request, 400, errors=[str(error)])


def messages_view(
    request: Request,
    status: int = 200,
    *,
    errors: list[str] | None = None,
) -> Response:
    """Render the messages page with the selected conversation.

    When no conversation is asked for, the first one that is not a role channel
    is opened. Showing a conversation marks its latest message as read.

    Args:
        request: the incoming request.
        status: HTTP status code of the response.
        errors: error messages to show on the page.

    Returns:
        The rendered messages page.
    """
    ctx = context_of(request)
    principal = request.state.principal
    policy = ctx.policy.get()
    conversations = ctx.messenger.conversations(principal.user_id)
    wanted = request.query_params.get("c", "")[:40]
    current = next((item for item in conversations if item.id == wanted), None)
    if current is None and conversations and not wanted:
        current = next((item for item in conversations if item.kind != "role"), conversations[0])
    feed = ctx.messenger.feed(principal.user_id, current.id) if current else []
    if current and feed:
        ctx.messenger.mark_read(principal.user_id, current.id, feed[-1].id)
    keys = ctx.messenger.keys_of(principal.user_id)
    members = ctx.messenger.members(principal.user_id, current.id) if current else []
    note = NOTES.get(request.query_params.get("done", ""))
    return render(
        request,
        "messages.html",
        status,
        active="messages",
        conversations=conversations,
        current=current,
        feed=[message_json(ctx, item) for item in feed or []],
        members=members,
        people=ctx.messenger.people(principal.user_id),
        keys=keys,
        e2ee=policy.messaging_e2ee,
        errors=errors or [],
        notes=[note] if note else [],
        settings=ctx.settings.messages,
        me=principal.user_id,
        names={item.user_id: item.name for item in members},
        can_group="messages.groups" in principal.permissions,
        moderator="messages.moderate" in principal.permissions,
        unread=ctx.messenger.unread(principal.user_id),
    )


@requires("messages.use", MODULE)
async def messages_page(request: Request) -> Response:
    """Show the messages page.

    Args:
        request: the incoming request.

    Returns:
        The rendered messages page.
    """
    return messages_view(request)


@requires("messages.use", MODULE)
@json_errors
async def feed_json(request: Request) -> Response:
    """Return the messages of a conversation newer than a given id.

    The messages returned are marked as read for the current user.

    Args:
        request: the incoming request, with an optional after query parameter.

    Returns:
        JSON with the messages and the current key version.

    Raises:
        HTTPException: 404 when the conversation is not available.
    """
    ctx = context_of(request)
    user = request.state.principal.user_id
    conversation = conversation_or_404(request)
    raw = request.query_params.get("after", "")
    after = min(int(raw), MAX_ID) if raw.isascii() and raw.isdigit() else 0
    feed = ctx.messenger.feed(user, conversation.id, after)
    if feed is None:
        raise HTTPException(404)
    if feed:
        ctx.messenger.mark_read(user, conversation.id, feed[-1].id)
    return JSONResponse(
        {
            "messages": [message_json(ctx, item) for item in feed],
            "key_version": conversation.key_version,
        }
    )


@requires("messages.use", MODULE)
@json_errors
async def send_json(request: Request) -> Response:
    """Post a message to a conversation from a JSON request.

    Args:
        request: the incoming request with text or cipher text and a key version.

    Returns:
        JSON with the stored message.

    Raises:
        RateLimitedError: when the user sends too fast.
        InvalidRequest: when the body is not a valid JSON object.
    """
    ctx = context_of(request)
    principal = request.state.principal
    protected_header(request, request.state.session)
    conversation = conversation_or_404(request)
    if not ctx.message_limiter.allow(principal.user_id):
        raise RateLimitedError(SLOW_DOWN)
    try:
        body = json.loads((await request.body()).decode("utf-8", errors="replace"))
    except (ValueError, RecursionError) as error:
        raise InvalidRequest("The request is not valid JSON.") from error
    if not isinstance(body, dict):
        raise InvalidRequest("The request must be an object.")
    version = body.get("version", 1)
    sent = ctx.messenger.send(
        principal.user_id,
        conversation.id,
        text=str(body.get("text", "")),
        cipher=str(body.get("cipher", "")),
        version=version if isinstance(version, int) else 0,
    )
    return JSONResponse({"message": message_json(ctx, sent)})


@requires("messages.use", MODULE)
@json_errors
async def read_json(request: Request) -> Response:
    """Mark a conversation as read up to a message id.

    Args:
        request: the incoming request with an upto field in a JSON body.

    Returns:
        An empty 204 response.

    Raises:
        InvalidRequest: when the body is not valid.
    """
    ctx = context_of(request)
    protected_header(request, request.state.session)
    conversation = conversation_or_404(request)
    try:
        upto = int(json.loads((await request.body()).decode("utf-8")).get("upto", 0))
    except (ValueError, AttributeError, TypeError, RecursionError) as error:
        raise InvalidRequest("The request is not valid.") from error
    ctx.messenger.mark_read(request.state.principal.user_id, conversation.id, upto)
    return Response(status_code=204)


@requires("messages.use", MODULE)
@json_errors
async def unread_json(request: Request) -> Response:
    """Return the unread message counts of the current user.

    Args:
        request: the incoming request.

    Returns:
        JSON with the unread counts.
    """
    ctx = context_of(request)
    return JSONResponse(ctx.messenger.unread(request.state.principal.user_id))


@requires("messages.use", MODULE)
@json_errors
async def keys_get(request: Request) -> Response:
    """Return the stored end to end encryption keys of the current user.

    Args:
        request: the incoming request.

    Returns:
        JSON with the key record, or null when none was saved.
    """
    ctx = context_of(request)
    keys = ctx.messenger.keys_of(request.state.principal.user_id)
    return JSONResponse({"keys": keys.model_dump() if keys else None})


@requires("messages.use", MODULE)
@json_errors
async def keys_save(request: Request) -> Response:
    """Save the encryption key pair of the current user.

    Replacing existing keys needs the account password and is throttled. The
    action is audited without key material.

    Args:
        request: the incoming request with the public key and the wrapped private key.

    Returns:
        JSON with the saved keys.

    Raises:
        InvalidRequest: when private conversations are off or the password is wrong.
        HTTPException: 404 when the account no longer exists.
    """
    ctx = context_of(request)
    principal = request.state.principal
    data = await body_of(request)
    if not ctx.policy.get().messaging_e2ee:
        raise InvalidRequest(E2EE_OFF)
    replace = bool(data.get("replace"))
    user = ctx.accounts.user(principal.user_id)
    if user is None:
        raise HTTPException(404)
    throttle(ctx, request, user, "message-keys")
    if replace and not await password_ok(ctx, user, str(data.get("password", ""))[:1000]):
        raise InvalidRequest(WRONG_CREDENTIAL)
    iterations = data.get("iterations", 0)
    saved = ctx.messenger.save_keys(
        principal.user_id,
        str(data.get("public_key", "")),
        str(data.get("wrapped_private", "")),
        str(data.get("salt", "")),
        iterations if isinstance(iterations, int) else 0,
        replace=replace,
    )
    audit(request, "messages.keys", detail={"replace": replace})
    return JSONResponse({"keys": saved.model_dump()})


@requires("messages.use", MODULE)
@json_errors
async def key_of(request: Request) -> Response:
    """Return the public key of another user.

    Args:
        request: the incoming request with the user id in its path.

    Returns:
        JSON with the public key.

    Raises:
        HTTPException: 404 when the user has no key.
    """
    found = context_of(request).messenger.public_key_of(request.path_params["user_id"])
    if found is None:
        raise HTTPException(404)
    return JSONResponse(found)


@requires("messages.use", MODULE)
@json_errors
async def conversation_keys(request: Request) -> Response:
    """Return the current user's wrapped keys for a conversation.

    Args:
        request: the incoming request with the conversation id in its path.

    Returns:
        JSON with the wrapped keys and the current key version.
    """
    ctx = context_of(request)
    conversation = conversation_or_404(request)
    wrapped = ctx.messenger.wrapped_keys(request.state.principal.user_id, conversation.id)
    return JSONResponse({"keys": wrapped or [], "version": conversation.key_version})


@requires("messages.use", MODULE)
@json_errors
async def peers_json(request: Request) -> Response:
    """Return the public keys of the other members of a conversation.

    Args:
        request: the incoming request with the conversation id in its path.

    Returns:
        JSON with the peer keys.

    Raises:
        HTTPException: 404 when the conversation is not available.
    """
    conversation = conversation_or_404(request)
    found = context_of(request).messenger.peer_keys(
        request.state.principal.user_id, conversation.id
    )
    if found is None:
        raise HTTPException(404)
    return JSONResponse({"peers": found})


@requires("messages.use", MODULE)
@json_errors
async def rotate_json(request: Request) -> Response:
    """Rotate the conversation key and store the new wrapped keys.

    Args:
        request: the incoming request with the new wraps in its body.

    Returns:
        JSON with the new key version.

    Raises:
        RateLimitedError: when the user acts too fast.
    """
    ctx = context_of(request)
    principal = request.state.principal
    conversation = conversation_or_404(request)
    data = await body_of(request)
    if not ctx.message_limiter.allow(principal.user_id):
        raise RateLimitedError(SLOW_DOWN)
    wraps = data.get("wraps")
    version = ctx.messenger.rotate_key(
        principal.user_id, conversation.id, wraps if isinstance(wraps, dict) else None
    )
    audit(request, "messages.rotate", target=conversation.id)
    return JSONResponse({"version": version})


def encrypted_allowed(ctx: WebContext, data: dict[str, Any]) -> bool:
    """Check whether a request asks for a private (encrypted) conversation.

    Args:
        ctx: the web context, used for the policy.
        data: the parsed request body.

    Returns:
        True when encryption was asked for and is allowed.

    Raises:
        InvalidRequest: when encryption is asked for but switched off by policy.
    """
    wants = str(data.get("encrypted", "")).lower() in {"1", "true", "yes", "on"}
    if wants and not ctx.policy.get().messaging_e2ee:
        raise InvalidRequest(E2EE_OFF)
    return wants


@requires("messages.use", MODULE)
async def direct_create(request: Request) -> Response:
    """Start or open a direct conversation with another user.

    Args:
        request: the incoming request with the other user and optional key wraps.

    Returns:
        A JSON or redirect response pointing to the conversation.
    """
    ctx = context_of(request)
    principal = request.state.principal
    data = await body_of(request)
    if slow := busy(request, data):
        return slow
    try:
        created = ctx.messenger.direct(
            principal.user_id,
            str(data.get("other", ""))[:40],
            encrypted_allowed(ctx, data),
            data.get("wraps") if isinstance(data.get("wraps"), dict) else None,
            str(data.get("id", ""))[:40],
        )
    except InvalidRequest as error:
        return failure(data, error, request)
    audit(request, "messages.direct", target=created.id)
    return reply(data, f"/messages?c={created.id}", {"id": created.id})


@requires("messages.groups", MODULE)
async def group_create(request: Request) -> Response:
    """Create a group conversation.

    Args:
        request: the incoming request with a title, members and optional key wraps.

    Returns:
        A JSON or redirect response pointing to the new group.
    """
    ctx = context_of(request)
    principal = request.state.principal
    data = await body_of(request)
    if slow := busy(request, data):
        return slow
    members = data.get("members", [])
    try:
        created = ctx.messenger.create_group(
            principal.user_id,
            str(data.get("title", "")),
            [str(item) for item in members] if isinstance(members, list) else [],
            encrypted_allowed(ctx, data),
            data.get("wraps") if isinstance(data.get("wraps"), dict) else None,
            str(data.get("id", ""))[:40],
        )
    except InvalidRequest as error:
        return failure(data, error, request)
    audit(request, "messages.group", target=created.id)
    return reply(data, f"/messages?c={created.id}&done=created", {"id": created.id})


@requires("messages.use", MODULE)
async def role_channel(request: Request) -> Response:
    """Open the channel that belongs to the role of the current user.

    Args:
        request: the incoming request.

    Returns:
        A redirect to the channel.

    Raises:
        HTTPException: 404 when the role has no channel.
    """
    ctx = context_of(request)
    form = await protected_form(request, request.state.session)
    await form.close()
    person = ctx.accounts.user(request.state.principal.user_id)
    identifier = ctx.messenger.role_channel(person.role_id) if person else None
    if identifier is None:
        raise HTTPException(404)
    return RedirectResponse(f"/messages?c={identifier}", status_code=303)


@requires("messages.use", MODULE)
async def group_members(request: Request) -> Response:
    """Add members to a group conversation.

    Args:
        request: the incoming request with the member ids and optional key wraps.

    Returns:
        A JSON or redirect response, or a failure response when not allowed.
    """
    ctx = context_of(request)
    principal = request.state.principal
    conversation = conversation_or_404(request)
    data = await body_of(request)
    if slow := busy(request, data):
        return slow
    members = data.get("members", [])
    try:
        ctx.messenger.add_members(
            principal.user_id,
            conversation.id,
            [str(item) for item in members] if isinstance(members, list) else [],
            moderator="messages.moderate" in principal.permissions,
            wraps=data.get("wraps") if isinstance(data.get("wraps"), dict) else None,
        )
    except InvalidRequest as error:
        return failure(data, error, request)
    audit(request, "messages.members", target=conversation.id)
    return reply(data, f"/messages?c={conversation.id}&done=saved")


@requires("messages.use", MODULE)
async def group_leave(request: Request) -> Response:
    """Leave a group conversation or remove another member.

    Args:
        request: the incoming request, with an optional user to remove.

    Returns:
        A JSON or redirect response, or a failure response when not allowed.
    """
    ctx = context_of(request)
    principal = request.state.principal
    conversation = conversation_or_404(request)
    data = await body_of(request)
    if slow := busy(request, data):
        return slow
    target = str(data.get("user", principal.user_id))[:40] or principal.user_id
    try:
        ctx.messenger.remove_member(
            principal.user_id,
            conversation.id,
            target,
            moderator="messages.moderate" in principal.permissions,
            wraps=data.get("wraps") if isinstance(data.get("wraps"), dict) else None,
        )
    except InvalidRequest as error:
        return failure(data, error, request)
    audit(request, "messages.remove_member", target=conversation.id)
    leaving = target == principal.user_id
    return reply(data, "/messages" if leaving else f"/messages?c={conversation.id}&done=saved")


@requires("messages.use", MODULE)
async def group_rename(request: Request) -> Response:
    """Rename a group conversation.

    Args:
        request: the incoming request with the new title.

    Returns:
        A JSON or redirect response, or a failure response when not allowed.
    """
    ctx = context_of(request)
    principal = request.state.principal
    conversation = conversation_or_404(request)
    data = await body_of(request)
    if slow := busy(request, data):
        return slow
    try:
        ctx.messenger.rename_group(
            principal.user_id,
            conversation.id,
            str(data.get("title", "")),
            moderator="messages.moderate" in principal.permissions,
        )
    except InvalidRequest as error:
        return failure(data, error, request)
    return reply(data, f"/messages?c={conversation.id}&done=saved")


@requires("messages.use", MODULE)
async def group_delete(request: Request) -> Response:
    """Delete a group conversation.

    Args:
        request: the incoming request.

    Returns:
        A JSON or redirect response, or a failure response when not allowed.
    """
    ctx = context_of(request)
    principal = request.state.principal
    conversation = conversation_or_404(request)
    data = await body_of(request)
    try:
        ctx.messenger.delete_group(
            principal.user_id,
            conversation.id,
            moderator="messages.moderate" in principal.permissions,
        )
    except InvalidRequest as error:
        return failure(data, error, request)
    audit(request, "messages.group_delete", target=conversation.id)
    return reply(data, "/messages?done=removed")


@requires("messages.use", MODULE)
@json_errors
async def message_remove(request: Request) -> Response:
    """Remove a message, as its author or as a moderator.

    Args:
        request: the incoming request with the message id in its path.

    Returns:
        An empty 204 response.

    Raises:
        HTTPException: 404 when the id is invalid or nothing was removed.
    """
    ctx = context_of(request)
    principal = request.state.principal
    protected_header(request, request.state.session)
    raw = request.path_params["message_id"]
    if not (raw.isascii() and raw.isdigit()) or int(raw) > MAX_ID:
        raise HTTPException(404)
    removed = ctx.messenger.remove_message(
        principal.user_id, int(raw), "messages.moderate" in principal.permissions
    )
    if not removed:
        raise HTTPException(404)
    audit(request, "messages.remove", target=raw)
    return Response(status_code=204)


def announcements_view(
    request: Request, status: int = 200, errors: list[str] | None = None
) -> Response:
    """Render the announcements page.

    Args:
        request: the incoming request.
        status: HTTP status code of the response.
        errors: error messages to show on the page.

    Returns:
        The rendered page with the visible announcements and audience choices.
    """
    ctx = context_of(request)
    principal = request.state.principal
    note = NOTES.get(request.query_params.get("done", ""))
    return render(
        request,
        "announcements.html",
        status,
        active="announcements",
        items=ctx.messenger.announcements(principal.user_id),
        audiences=[
            ("all", "Everyone"),
            *((f"role:{role.id}", f"Role: {role.name}") for role in ctx.accounts.roles()),
            *(
                (f"group:{item.id}", f"Group: {item.title}")
                for item in ctx.messenger.conversations(principal.user_id)
                if item.kind == "group"
            ),
        ],
        expiry=[
            (str(days), "Until removed" if not days else f"{days} day{'s' if days != 1 else ''}")
            for days in ctx.settings.messages.expiry_choices
        ],
        can_post="messages.announce" in principal.permissions,
        moderator="messages.moderate" in principal.permissions,
        me=principal.user_id,
        errors=errors or [],
        notes=[note] if note else [],
        settings=ctx.settings.messages,
    )


@requires("messages.use", MODULE)
async def announcements_page(request: Request) -> Response:
    """Show the announcements page.

    Args:
        request: the incoming request.

    Returns:
        The rendered announcements page.
    """
    return announcements_view(request)


@requires("messages.announce", MODULE)
async def announcement_create(request: Request) -> Response:
    """Post an announcement from the form.

    Args:
        request: the incoming request with title, body, audience and expiry.

    Returns:
        A redirect after posting, or the page with an error status.
    """
    ctx = context_of(request)
    principal = request.state.principal
    tuning = ctx.settings.messages
    form = await protected_form(request, request.state.session)
    try:
        title = text(form, "title", tuning.announcement_title_chars + 10)
        body = form.get("body")
        choice = text(form, "audience", 60)
        raw_days = text(form, "days", 4)
        pinned = text(form, "pinned", 4) == "yes"
    finally:
        await form.close()
    if not ctx.message_limiter.allow(principal.user_id):
        return announcements_view(request, 429, [SLOW_DOWN])
    audience, _, target = choice.partition(":")
    try:
        posted = ctx.messenger.announce(
            principal.user_id,
            title,
            body if isinstance(body, str) else "",
            audience,
            target,
            int(raw_days) if raw_days.isdigit() else -1,
            pinned,
            "messages.moderate" in principal.permissions,
        )
    except InvalidRequest as error:
        return announcements_view(request, 400, [str(error)])
    audit(request, "announcement.post", target=posted.id, detail={"audience": posted.audience})
    return RedirectResponse("/announcements?done=posted", status_code=303)


@requires("messages.use", MODULE)
async def announcement_read(request: Request) -> Response:
    """Mark an announcement as read and go back to a known page.

    Args:
        request: the incoming request with the announcement id in its path.

    Returns:
        A redirect to the requested page, limited to a short list of local pages.
    """
    ctx = context_of(request)
    form = await protected_form(request, request.state.session)
    try:
        landing = text(form, "next", 20)
    finally:
        await form.close()
    # Only known local pages are allowed as the next target, which prevents an open redirect.
    landing = landing if landing in BACK else "/announcements"
    ctx.messenger.read_announcement(
        request.state.principal.user_id, request.path_params["announcement_id"]
    )
    return RedirectResponse(landing, status_code=303)


@requires("messages.use", MODULE)
async def announcement_delete(request: Request) -> Response:
    """Delete an announcement, as its author or as a moderator.

    Args:
        request: the incoming request with the announcement id in its path.

    Returns:
        A redirect to the announcements page.

    Raises:
        HTTPException: 404 when nothing was removed.
    """
    ctx = context_of(request)
    principal = request.state.principal
    form = await protected_form(request, request.state.session)
    await form.close()
    removed = ctx.messenger.delete_announcement(
        principal.user_id,
        request.path_params["announcement_id"],
        "messages.moderate" in principal.permissions,
    )
    if not removed:
        raise HTTPException(404)
    audit(request, "announcement.delete", target=request.path_params["announcement_id"])
    return RedirectResponse("/announcements?done=removed", status_code=303)
