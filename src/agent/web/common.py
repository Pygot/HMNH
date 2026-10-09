# src/agent/web/common.py
from agent.accounts import (
    ADMIN_NEEDS,
    OWNER_ID,
    owner_principal,
    Principal,
)
from agent.errors import (
    AgentError,
    InvalidRequest,
    RateLimitedError,
    VoiceDisabled,
)
from agent.web.prefs import (
    BuddyMode,
    decode_preferences,
    encode_preferences,
    Preferences,
)
from agent.web.security import (
    ENROLL,
    FULL,
    Session,
    tokens_match,
)
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from agent.compliance import (
    NOTICE,
    STORAGE_NOTICE,
    VOICE_NOTICE,
)
from agent.web.context import (
    context_of,
    WebContext,
)
from collections.abc import (
    Awaitable,
    Callable,
)
from starlette.exceptions import HTTPException
from starlette.datastructures import FormData
from agent.web.settings import WebSettings
from starlette.requests import Request
from pathlib import Path
from typing import Any

import functools
import asyncio

WEB_DIR = Path(__file__).parent
LANDINGS = (
    ("stats.view", "/dashboard"),
    ("candidates.view", "/candidates"),
    ("routines.view", "/routines"),
    ("emails.view", "/emails"),
    ("finance.view", "/finance"),
    ("users.view", "/admin/users"),
)
NETWORK_LABELS = {
    "linkedin": "LinkedIn",
    "facebook": "Facebook",
    "instagram": "Instagram",
    "web": "Open web",
}
ADMIN_PERMISSIONS = frozenset(
    {"users.view", "roles.manage", "audit.view", "config.manage", "data.export", "data.import"}
)
EXPIRED_FORM = "The form expired; reload the page and try again."
Handler = Callable[[Request], Awaitable[Response]]


def full_admin(principal: Principal) -> bool:
    """Check whether a principal holds every admin permission.

    Args:
        principal: The principal to check.

    Returns:
        True when the principal's permissions include all of the required admin set.
    """
    return principal.permissions >= ADMIN_NEEDS


def cookie_name(web: WebSettings) -> str:
    """Return the name of the session cookie.

    The __Host- prefix is used when secure cookies are enabled.

    Args:
        web: Web server settings.

    Returns:
        The cookie name.
    """
    return "__Host-session" if web.cookie_secure else "session"


def prefs_cookie_name(web: WebSettings) -> str:
    """Return the name of the preferences cookie.

    The __Host- prefix is used when secure cookies are enabled.

    Args:
        web: Web server settings.

    Returns:
        The cookie name.
    """
    return "__Host-prefs" if web.cookie_secure else "prefs"


def current_session(request: Request) -> Session | None:
    """Return the session identified by the request's session cookie.

    Args:
        request: The incoming request.

    Returns:
        The session, or None when there is no cookie or the session is unknown.
    """
    ctx = context_of(request)
    return ctx.sessions.get(request.cookies.get(cookie_name(ctx.web)))


def attach_cookie(response: Response, web: WebSettings, session: Session) -> None:
    """Set the session cookie on a response.

    The cookie is HttpOnly, SameSite strict and scoped to the whole site.

    Args:
        response: The response to modify.
        web: Web server settings.
        session: The session whose id is stored.
    """
    response.set_cookie(
        cookie_name(web),
        session.id,
        httponly=True,
        secure=web.cookie_secure,
        samesite="strict",
        path="/",
    )


def read_preferences(request: Request) -> tuple[Preferences, bool]:
    """Read the preferences cookie of a request.

    Args:
        request: The incoming request.

    Returns:
        The preferences and a flag that is True when an unusable cookie was replaced by
        defaults.
    """
    web = context_of(request).web
    return decode_preferences(request.cookies.get(prefs_cookie_name(web)), web.prefs_max_bytes)


def save_preferences(response: Response, web: WebSettings, preferences: Preferences) -> None:
    """Store preferences in a long-lived cookie on a response.

    The cookie is HttpOnly, SameSite strict and scoped to the whole site.

    Args:
        response: The response to modify.
        web: Web server settings.
        preferences: The preferences to store.
    """
    response.set_cookie(
        prefs_cookie_name(web),
        encode_preferences(preferences),
        max_age=web.prefs_max_age_seconds,
        httponly=True,
        secure=web.cookie_secure,
        samesite="strict",
        path="/",
    )


def voice_ready(ctx: WebContext) -> bool:
    """Check whether the voice services are available.

    Args:
        ctx: The web context.

    Returns:
        True when the research services are open and include voice.
    """
    return ctx.services is not None and ctx.services.voice is not None


def buddy_context(
    ctx: WebContext, preferences: Preferences, page: str, voice_enabled: bool
) -> dict[str, Any]:
    """Build the template values that configure the buddy widget.

    A voice buddy falls back to text mode when voice is not enabled.

    Args:
        ctx: The web context.
        preferences: The caller's preferences.
        page: Name of the active page.
        voice_enabled: Whether voice can be used by the caller.

    Returns:
        The buddy template values.
    """
    tuning = ctx.settings.buddy
    mode = preferences.buddy
    if mode is BuddyMode.VOICE and not voice_enabled:
        mode = BuddyMode.TEXT
    return {
        "nudges": mode is not BuddyMode.OFF,
        "mode": mode.value,
        "page": page,
        "name": tuning.name,
        "voice_available": voice_enabled,
        "focus_minutes": tuning.focus_minutes,
        "config": tuning.model_dump(mode="json"),
    }


def render(request: Request, name: str, status: int = 200, **values: Any) -> HTMLResponse:
    """Render a template with the shared page context.

    Adds the preferences, CSRF token, permissions, policy, notices, thread rail, unread
    counts and announcement banner. Sessions that have not finished sign-in get no
    permissions.

    Args:
        request: The incoming request.
        name: Template file name.
        status: HTTP status code of the response.
        **values: Extra template values, which override the defaults for active and
            thread.

    Returns:
        The rendered HTML response.
    """
    ctx = context_of(request)
    session: Session | None = getattr(request.state, "session", None) or current_session(request)
    principal = principal_of(request)
    preferences, reset = read_preferences(request)
    staged = session is not None and session.stage != FULL
    can = principal.permissions if principal and not staged else frozenset()
    values.setdefault("active", "")
    values.setdefault("thread", None)
    values.setdefault("prefs_reset", reset)
    values["prefs"] = preferences
    values["nonce"] = request.state.nonce
    values["csrf_token"] = session.csrf_token if session else ""
    values["authenticated"] = principal is not None
    values["principal"] = principal
    values["can"] = can
    values["policy"] = ctx.policy.get()
    values["admin_access"] = bool(can & ADMIN_PERMISSIONS)
    values["mail_ready"] = ctx.mailer.configured()
    values["notice"] = NOTICE
    values["storage_notice"] = STORAGE_NOTICE
    values["api_enabled"] = ctx.web.api_token is not None
    values["voice_enabled"] = voice_ready(ctx) and "voice.use" in can
    values["buddy"] = buddy_context(ctx, preferences, values["active"], values["voice_enabled"])
    values["voice_notice"] = VOICE_NOTICE
    values["record_seconds"] = ctx.settings.voice.max_record_seconds
    values["threads"] = (
        ctx.store.threads("research", ctx.settings.chat.rail_threads)
        if principal is not None and "chat.use" in can
        else []
    )
    talking = principal is not None and "messages.use" in can and values["policy"].messaging_enabled
    values["inbox"] = (
        ctx.messenger.unread(principal.user_id)
        if talking and principal is not None
        else {"messages": 0, "announcements": 0}
    )
    values["banner"] = (
        ctx.messenger.announcements(principal.user_id, True, ctx.settings.messages.banner_shown)
        if talking and principal is not None and ctx.settings.messages.banner_shown
        else []
    )
    template = ctx.templates.get_template(name)
    return HTMLResponse(template.render(**values), status_code=status)


def resolve_principal(
    ctx: WebContext, session: Session | None, stages: tuple[str, ...] = (FULL,)
) -> Principal | None:
    """Resolve the principal behind a session.

    The owner pseudo-account only resolves when a web access token is configured. Other
    users are loaded by id and must match the session's token version.

    Args:
        ctx: The web context.
        session: The session, or None.
        stages: Session stages that are accepted.

    Returns:
        The principal, or None when the session is missing, in another stage, or stale.
    """
    if session is None or session.stage not in stages or not session.user_id:
        return None
    if session.user_id == OWNER_ID:
        # The owner account only works while a web access token is configured.
        return owner_principal() if ctx.web.access_token is not None else None
    return ctx.accounts.principal_for(session.user_id, session.version)


def principal_of(request: Request) -> Principal | None:
    """Return the request's principal, resolving and caching it when needed.

    Args:
        request: The incoming request.

    Returns:
        The principal, or None when the caller is not signed in.
    """
    try:
        return request.state.principal
    except AttributeError:
        principal = resolve_principal(context_of(request), current_session(request))
        request.state.principal = principal
        return principal


def renew_session(request: Request, ctx: WebContext, user_id: str) -> None:
    """Rotate the request's session into a fully signed-in session for a user.

    The new session carries the user's current token version, and every other session of
    that user is destroyed. Nothing happens if the user no longer exists.

    Args:
        request: The incoming request, whose state holds the current session.
        ctx: The web context.
        user_id: Id of the user to sign in.
    """
    session: Session = request.state.session
    fresh = ctx.accounts.user(user_id)
    if fresh is None:
        return
    rotated = ctx.sessions.rotate(
        session, user_id=fresh.id, version=fresh.token_version, stage=FULL
    )
    ctx.sessions.destroy_user(fresh.id, keep=rotated.id)
    request.state.session = rotated


def carry(request: Request, ctx: WebContext, response: Response) -> Response:
    """Attach the request's session cookie to a response.

    Args:
        request: The incoming request whose state holds the session.
        ctx: The web context.
        response: The response to modify.

    Returns:
        The same response.
    """
    attach_cookie(response, ctx.web, request.state.session)
    return response


def client_address(request: Request) -> str:
    """Return the IP address of the client.

    Args:
        request: The incoming request.

    Returns:
        The host of the connecting peer, or unknown when it is not available.
    """
    return request.client.host if request.client else "unknown"


def audit(
    request: Request,
    action: str,
    *,
    target: str = "",
    outcome: str = "ok",
    detail: dict[str, Any] | None = None,
    actor_name: str = "",
) -> None:
    """Write an audit log entry for the current request.

    The actor is the request's principal and the client address is recorded.

    Args:
        request: The incoming request.
        action: Dotted name of the action, such as access.denied.
        target: What the action applied to.
        outcome: Result of the action.
        detail: Extra structured details.
        actor_name: Name to record when there is no principal yet.
    """
    context_of(request).accounts.log(
        action,
        actor=principal_of(request),
        actor_name=actor_name,
        target=target,
        outcome=outcome,
        ip=client_address(request),
        detail=detail,
    )


def needs_enrolment(ctx: WebContext, principal: Principal) -> bool:
    """Check whether a principal must enrol in two-factor sign-in first.

    This applies to signed-in account users, not the owner or API callers, when policy
    requires TOTP and the account has none.

    Args:
        ctx: The web context.
        principal: The principal to check.

    Returns:
        True when enrolment is required.
    """
    return (
        ctx.policy.get().require_totp
        and principal.source == "session"
        and principal.user_id != OWNER_ID
        and not principal.totp
    )


def landing_path(principal: Principal) -> str:
    """Return the first page a principal is allowed to open.

    Args:
        principal: The signed-in principal.

    Returns:
        The path of the first permitted landing page, or the account page.
    """
    for permission, path in LANDINGS:
        if principal.can(permission):
            return path
    return "/account"


def requires(
    permission: str | None = None, module: str | None = None, enrolling: bool = False
) -> Callable[[Handler], Handler]:
    """Create a decorator that guards a handler by session, permission and module.

    Signed-out callers are redirected to the login page. Sessions still enrolling in
    two-factor sign-in are redirected to the enrolment page unless enrolling is True.
    A missing permission or a disabled module gives a 404, not a 403.

    Args:
        permission: Permission the caller needs, or None for any signed-in user.
        module: Name of a policy flag that must be enabled.
        enrolling: Whether the handler may run during two-factor enrolment.

    Returns:
        The decorator.
    """

    def decorate(handler: Handler) -> Handler:
        """Wrap a handler with the access checks.

        Args:
            handler: The request handler to protect.

        Returns:
            The wrapped handler, tagged with the permission and module it requires.
        """

        @functools.wraps(handler)
        async def wrapper(request: Request) -> Response:
            """Check the session, permission and module, then call the handler.

            On success the session, principal and required permission are stored on the request
            state. A denied permission is written to the audit log.

            Args:
                request: The incoming request.

            Returns:
                The handler's response, or a redirect.

            Raises:
                HTTPException: 404 when the permission is missing or the module is disabled.
            """
            ctx = context_of(request)
            session = current_session(request)
            if session is None or session.user_id is None:
                return RedirectResponse("/login", status_code=303)
            if session.stage == ENROLL:
                if not enrolling:
                    return RedirectResponse("/account/2fa", status_code=303)
                request.state.session = session
                request.state.principal = resolve_principal(ctx, session, (ENROLL,))
                if request.state.principal is None:
                    ctx.sessions.destroy(session.id)
                    return RedirectResponse("/login", status_code=303)
                return await handler(request)
            if session.stage != FULL:
                return RedirectResponse("/login", status_code=303)
            principal = resolve_principal(ctx, session)
            if principal is None:
                ctx.sessions.destroy(session.id)
                return RedirectResponse("/login", status_code=303)
            request.state.session = session
            request.state.principal = principal
            request.state.required = permission
            if needs_enrolment(ctx, principal) and not enrolling:
                return RedirectResponse("/account/2fa", status_code=303)
            if permission is not None and not principal.can(permission):
                # Denied access is answered with a 404 so protected pages are not revealed.
                audit(request, "access.denied", target=request.url.path[:100], outcome="denied")
                raise HTTPException(404)
            if module is not None and not getattr(ctx.policy.get(), module):
                raise HTTPException(404)
            return await handler(request)

        wrapper.permission = permission
        wrapper.module = module
        return wrapper

    return decorate


def public(handler: Handler) -> Handler:
    """Mark a handler as reachable without signing in.

    Args:
        handler: The request handler.

    Returns:
        The same handler, tagged as public with no required permission.
    """
    handler.permission = None
    handler.public = True
    return handler


def json_errors(handler: Handler) -> Handler:
    """Wrap a handler so that agent errors become JSON error responses.

    Voice disabled gives 404, rate limiting 429, invalid requests 422, and any other
    agent error 502. The body has an error field with the message.

    Args:
        handler: The request handler.

    Returns:
        The wrapped handler.
    """

    @functools.wraps(handler)
    async def wrapper(request: Request) -> Response:
        """Call the handler and convert agent errors into JSON responses.

        Args:
            request: The incoming request.

        Returns:
            The handler's response, or a JSON error response.
        """
        try:
            return await handler(request)
        except VoiceDisabled as error:
            return JSONResponse({"error": str(error)}, status_code=404)
        except RateLimitedError as error:
            return JSONResponse({"error": str(error)}, status_code=429)
        except InvalidRequest as error:
            return JSONResponse({"error": str(error)}, status_code=422)
        except AgentError as error:
            return JSONResponse({"error": str(error)}, status_code=502)

    return wrapper


def check_origin(request: Request, web: WebSettings) -> None:
    """Reject requests that do not come from the site itself.

    A request with an Origin header must match the public origin, or the request's own
    scheme and host. Without one, the Sec-Fetch-Site header must say same-origin or none.

    Args:
        request: The incoming request.
        web: Web server settings.

    Raises:
        HTTPException: 403 when the request is cross-origin or cross-site.
    """
    origin = request.headers.get("origin")
    if origin is not None and origin != "null":
        expected = web.public_origin or f"{request.url.scheme}://{request.headers.get('host', '')}"
        if origin != expected:
            raise HTTPException(403, "Cross-origin request rejected.")
    # Without a usable Origin header, Fetch Metadata decides. A missing header counts as same-
    # origin.
    elif request.headers.get("sec-fetch-site", "same-origin") not in {"same-origin", "none"}:
        raise HTTPException(403, "Cross-site request rejected.")


def protected_header(request: Request, session: Session) -> WebContext:
    """Verify origin and CSRF header token for a JSON request.

    Args:
        request: The incoming request.
        session: The caller's session.

    Returns:
        The web context.

    Raises:
        HTTPException: 403 when the origin is wrong or the X-CSRF-Token header does not
            match the session.
    """
    ctx = context_of(request)
    check_origin(request, ctx.web)
    if not tokens_match(session.csrf_token, request.headers.get("x-csrf-token", "")):
        raise HTTPException(403, EXPIRED_FORM)
    return ctx


def revalidate(request: Request, ctx: WebContext, session: Session) -> None:
    """Re-check the request's principal against the current account state.

    Owner and non-session principals are skipped. If the account is gone or its token
    version changed, the session is destroyed. Otherwise the principal on the request is
    replaced with the fresh one.

    Args:
        request: The incoming request.
        ctx: The web context.
        session: The caller's session.

    Raises:
        HTTPException: 403 when the account or session is no longer valid, 404 when the
            required permission was lost.
    """
    principal = getattr(request.state, "principal", None)
    if principal is None or principal.source != "session" or principal.user_id == OWNER_ID:
        return
    fresh = ctx.accounts.principal_for(principal.user_id, session.version)
    required = getattr(request.state, "required", None)
    if fresh is None:
        ctx.sessions.destroy(session.id)
        raise HTTPException(403, EXPIRED_FORM)
    if required is not None and not fresh.can(required):
        raise HTTPException(404)
    request.state.principal = fresh


async def protected_form(request: Request, session: Session) -> FormData:
    """Read a form after checking origin, CSRF token and current permissions.

    The read has a time limit and caps on fields, files and field size. The caller must
    close the returned form.

    Args:
        request: The incoming POST request.
        session: The caller's session.

    Returns:
        The parsed form data.

    Raises:
        HTTPException: 408 when the form arrives too slowly, 403 when the origin or CSRF
            token is wrong or the account is no longer valid, 404 when permission was lost.
    """
    ctx = context_of(request)
    web = ctx.web
    check_origin(request, web)
    try:
        async with asyncio.timeout(web.form_read_seconds):
            form = await request.form(
                max_files=1, max_fields=web.max_form_fields, max_part_size=web.max_field_length * 4
            )
    except TimeoutError as error:
        raise HTTPException(408, "The form took too long to arrive.") from error
    supplied = form.get("csrf_token")
    if not isinstance(supplied, str) or not tokens_match(session.csrf_token, supplied):
        await form.close()
        raise HTTPException(403, EXPIRED_FORM)
    try:
        revalidate(request, ctx, session)
    except HTTPException:
        await form.close()
        raise
    return form


def text(form: FormData, key: str, limit: int) -> str:
    """Return a stripped form field, cut to a maximum length.

    Args:
        form: The parsed form.
        key: Field name.
        limit: Maximum number of characters to keep.

    Returns:
        The trimmed text, or an empty string when the field is missing or not text.
    """
    value = form.get(key)
    return value.strip()[:limit] if isinstance(value, str) else ""


def items(raw: str) -> list[str]:
    """Split comma or line separated text into non-empty items.

    Args:
        raw: The text to split.

    Returns:
        The stripped items in order.
    """
    return [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]


def number(raw: str, cast_to: type, label: str, errors: list[str]) -> Any:
    """Convert text to a number, recording an error when it is not valid.

    Args:
        raw: The text to convert.
        cast_to: The numeric type, such as int or float.
        label: Field name used in the error message.
        errors: List that receives the error message.

    Returns:
        The converted number, or None when the text is empty or invalid.
    """
    if not raw:
        return None
    try:
        return cast_to(raw)
    except ValueError:
        errors.append(f"{label} must be a number.")
        return None


def choice(raw: str, kind: type, label: str, errors: list[str]) -> Any:
    """Convert text to an enum member, recording an error when it is not valid.

    Args:
        raw: The text to convert.
        kind: The enum type.
        label: Field name used in the error message.
        errors: List that receives the error message.

    Returns:
        The enum member, or None when the text is empty or invalid.
    """
    if not raw:
        return None
    try:
        return kind(raw)
    except ValueError:
        errors.append(f"{label} is not a valid choice.")
        return None
