# src/agent/web/auth.py
from agent.web.common import (
    attach_cookie,
    audit,
    client_address,
    cookie_name,
    current_session,
    EXPIRED_FORM,
    landing_path,
    protected_form,
    public,
    render,
    requires,
    text,
)
from agent.accounts import (
    INVITE,
    LOGIN,
    OWNER_ID,
    RESET,
    User,
)
from agent.web.security import (
    ENROLL,
    FULL,
    SECOND,
    Session,
    tokens_match,
)
from agent.web.context import (
    client_key,
    context_of,
    WebContext,
)
from starlette.responses import (
    RedirectResponse,
    Response,
)
from starlette.exceptions import HTTPException
from agent.totp import check as check_totp
from starlette.requests import Request
from agent.errors import AgentError
from agent.mailer import SYSTEM
from typing import Any

import asyncio
import hashlib
import logging
import time

logger = logging.getLogger(__name__)
LOGIN_FAILED = "The sign in details were not accepted."
CODE_FAILED = "That code was not accepted."
LINK_INVALID = "This link is not valid any more. Ask for a new one."
LINK_SENT = "If that address belongs to an account, a link is on its way."
TOO_MANY = "Too many attempts; wait a few minutes."
TOTP = "totp"
EMAIL = "email"
BACKGROUND: set[asyncio.Task[None]] = set()
LINK_TEXT = {
    INVITE: ("Welcome", "Create your password", True),
    RESET: ("Choose a new password", "Save the password", True),
    LOGIN: ("Sign in", "Continue", False),
}


def landing(
    request: Request,
    status: int = 200,
    errors: list[str] | None = None,
    notes: list[str] | None = None,
) -> Response:
    """Render the sign in page and open an anonymous session if needed.

    Args:
        request: the incoming request.
        status: HTTP status code of the response.
        errors: error messages to show on the page.
        notes: informational messages to show on the page.

    Returns:
        The rendered page, with a session cookie attached for a new session.

    Raises:
        HTTPException: 429 when the client address opened too many sessions.
    """
    ctx = context_of(request)
    session = current_session(request)
    fresh = session is None
    if session is None:
        if not ctx.session_limiter.allow(client_key(request)):
            raise HTTPException(429, "Too many sessions were opened from this address.")
        session = ctx.sessions.create(ip=client_address(request), agent=agent_of(request))
    request.state.session = session
    policy = ctx.policy.get()
    response = render(
        request,
        "landing.html",
        status,
        errors=errors or [],
        notes=notes or [],
        has_token=ctx.web.access_token is not None,
        needs_setup=ctx.accounts.needs_setup(),
        password_login=policy.allow_password_login and not ctx.accounts.needs_setup(),
        link_login=policy.allow_magic_links and ctx.mailer.configured(),
        can_reset=ctx.mailer.configured() and not ctx.accounts.needs_setup(),
    )
    if fresh:
        attach_cookie(response, ctx.web, session)
    return response


def login_keys(ctx: WebContext, request: Request, username: str) -> list[tuple[Any, str]]:
    """Return the rate limiter and key pairs that apply to a sign in attempt.

    Args:
        ctx: the web context holding the limiters.
        request: the incoming request, used for the client address.
        username: the submitted username, empty for token sign in.

    Returns:
        Pairs of limiter and key: one per address, plus per username and address
        and per username when a username was given.
    """
    address = client_key(request)
    keys: list[tuple[Any, str]] = [(ctx.login_limiter, address)]
    if username:
        name = username.lower()
        keys.append((ctx.user_login_limiter, f"{name}|{address}"))
        keys.append((ctx.user_ceiling_limiter, name))
    return keys


def throttled(ctx: WebContext, request: Request, username: str) -> bool:
    """Check whether any rate limiter currently blocks this sign in attempt.

    Args:
        ctx: the web context holding the limiters.
        request: the incoming request.
        username: the submitted username, empty for token sign in.

    Returns:
        True when at least one limiter is blocking.
    """
    return any(limiter.blocked(key) for limiter, key in login_keys(ctx, request, username))


def record_failure(ctx: WebContext, request: Request, username: str) -> None:
    """Count a failed sign in attempt against every applicable limiter.

    Args:
        ctx: the web context holding the limiters.
        request: the incoming request.
        username: the submitted username, empty for token sign in.
    """
    for limiter, key in login_keys(ctx, request, username):
        limiter.allow(key)


def shown_name(username: str, user: User | None) -> str:
    """Return the name to record in the audit log for a sign in attempt.

    Args:
        username: the submitted username.
        user: the matching account, or None when none exists.

    Returns:
        The account username, or a short hash of the submitted name for unknown users.
    """
    if user is not None:
        return user.username
    # Unknown names are hashed so attacker supplied text never reaches the audit log.
    return "unknown-" + hashlib.sha256(username.encode("utf-8")).hexdigest()[:8]


def agent_of(request: Request) -> str:
    """Return the client user agent, cut to 160 characters.

    Args:
        request: the incoming request.

    Returns:
        The User-Agent header value, or an empty string.
    """
    return request.headers.get("user-agent", "")[:160]


def background(coroutine: object) -> None:
    """Run a coroutine as a tracked background task.

    The task is kept in a module level set until it finishes so it is not
    garbage collected while running.

    Args:
        coroutine: the coroutine to schedule.
    """
    task = asyncio.ensure_future(coroutine)
    BACKGROUND.add(task)
    task.add_done_callback(BACKGROUND.discard)


async def sign_in(
    request: Request, ctx: WebContext, session: Session, user: User | None, via: str
) -> Response:
    """Complete a sign in, or send the user on to the second factor step.

    An owner token sign in (no user) is finished at once. For an account, a TOTP
    or emailed code is required when the account or policy asks for it, and the
    session is rotated into the second factor stage.

    Args:
        request: the incoming request.
        ctx: the web context.
        session: the session the first factor was submitted on.
        user: the verified account, or None for the owner access token.
        via: label of the sign in method, recorded in the audit log.

    Returns:
        A redirect to the verification page or to the signed in destination.
    """
    if user is None:
        return await finish(request, ctx, session, OWNER_ID, 0, via)
    policy = ctx.policy.get()
    needs_totp = user.totp_enabled
    needs_email = (
        not needs_totp
        and (policy.email_code_login or user.email_code_login)
        and bool(user.email)
        and ctx.mailer.configured()
    )
    if needs_totp or needs_email:
        pending = TOTP if needs_totp else EMAIL
        rotated = ctx.sessions.rotate(
            session, user_id=user.id, version=user.token_version, stage=SECOND, pending=pending
        )
        if pending == EMAIL:
            await mail_code(ctx, user)
        response = RedirectResponse("/login/verify", status_code=303)
        attach_cookie(response, ctx.web, rotated)
        return response
    return await finish(request, ctx, session, user.id, user.token_version, via, user)


async def finish(
    request: Request,
    ctx: WebContext,
    session: Session,
    user_id: str,
    version: int,
    via: str,
    user: User | None = None,
) -> Response:
    """Finish a sign in by rotating the session and redirecting.

    The session becomes fully signed in, or enrolment only when policy requires
    TOTP and the account has none. The login is audited and the per user failure
    counter is reset.

    Args:
        request: the incoming request.
        ctx: the web context.
        session: the session to replace.
        user_id: id of the signed in principal.
        version: token version of the account, tied to the new session.
        via: label of the sign in method, recorded in the audit log.
        user: the account, or None for the owner access token.

    Returns:
        A redirect carrying the new session cookie.
    """
    policy = ctx.policy.get()
    enroll = user is not None and policy.require_totp and not user.totp_enabled
    stage = ENROLL if enroll else FULL
    rotated = ctx.sessions.rotate(session, user_id=user_id, version=version, stage=stage)
    if user is not None:
        ctx.accounts.note_login(user.id)
    request.state.session = rotated
    name = user.username if user else "owner"
    ctx.accounts.log(
        "login.ok",
        actor_name=name,
        ip=client_address(request),
        detail={"via": via},
    )
    if user is not None:
        ctx.user_login_limiter.reset(f"{user.username.lower()}|{client_key(request)}")
    if enroll:
        target = "/account/2fa"
    else:
        principal = ctx.accounts.principal_for(user.id) if user is not None else None
        target = "/" if principal is None or principal.can("chat.use") else landing_path(principal)
    response = RedirectResponse(target, status_code=303)
    attach_cookie(response, ctx.web, rotated)
    return response


async def mail_code(ctx: WebContext, user: User) -> None:
    """Email a one-time sign in code to the user.

    A mail failure is logged and swallowed so the caller still shows the page.

    Args:
        ctx: the web context.
        user: the account that receives the code.
    """
    code = ctx.accounts.issue_code(user.id, LOGIN)
    subject = ctx.settings.mail.system_subjects["login_code"]
    body = (
        f"Your sign in code is {code}.\n\nIt works once and for "
        f"{ctx.settings.auth.email_code_minutes} minutes. If you did not try to sign in, "
        "ignore this message and consider changing your password."
    )
    try:
        await ctx.mailer.send(user.email or "", subject, body, kind=SYSTEM, purpose="login_code")
    except AgentError as error:
        logger.warning("the sign in code could not be sent: %s", type(error).__name__)


@public
async def login(request: Request) -> Response:
    """Handle the sign in form for password or access token sign in.

    Args:
        request: the incoming request.

    Returns:
        A redirect on success, or the sign in page with an error.

    Raises:
        HTTPException: 403 when the session expired, 429 when throttled.
    """
    ctx = context_of(request)
    session = current_session(request)
    if session is None:
        raise HTTPException(403, EXPIRED_FORM)
    request.state.session = session
    form = await protected_form(request, session)
    try:
        username = text(form, "username", ctx.settings.auth.max_username_chars)
        password = text(form, "password", ctx.settings.auth.password_max_length + 1)
        token = text(form, "token", ctx.web.max_field_length)
    finally:
        await form.close()
    if throttled(ctx, request, username):
        raise HTTPException(429, TOO_MANY)
    if token and not username:
        return await token_login(request, ctx, session, token)
    return await password_login(request, ctx, session, username, password)


async def token_login(request: Request, ctx: WebContext, session: Session, token: str) -> Response:
    """Sign in with the shared access token.

    Args:
        request: the incoming request.
        ctx: the web context.
        session: the current session.
        token: the submitted token.

    Returns:
        The result of sign in, or the sign in page with a 401 on a wrong token.
    """
    expected = ctx.web.access_token
    accepted = expected is not None and tokens_match(expected.get_secret_value(), token)
    if not accepted:
        record_failure(ctx, request, "")
        ctx.accounts.log(
            "login.fail", actor_name="token", outcome="denied", ip=client_address(request)
        )
        return landing(request, 401, [LOGIN_FAILED])
    return await sign_in(request, ctx, session, None, "token")


async def password_login(
    request: Request, ctx: WebContext, session: Session, username: str, password: str
) -> Response:
    """Sign in with a username and password.

    The password hash is verified even for unknown users, off the event loop.
    A hash made with outdated parameters is upgraded after a successful check.

    Args:
        request: the incoming request.
        ctx: the web context.
        session: the current session.
        username: the submitted username.
        password: the submitted password.

    Returns:
        The result of sign in, or the sign in page with a 401 and a generic error.
    """
    policy = ctx.policy.get()
    user = ctx.accounts.user_named(username) if username else None
    stored = ctx.accounts.secrets_of(user.id).password_hash if user else None
    correct = await asyncio.to_thread(ctx.hasher.verify, password, stored)
    allowed = policy.allow_password_login and user is not None and user.status == "active"
    if not (correct and allowed and user is not None):
        record_failure(ctx, request, username)
        ctx.accounts.log(
            "login.fail",
            actor_name=shown_name(username, user),
            outcome="denied",
            ip=client_address(request),
        )
        return landing(request, 401, [LOGIN_FAILED])
    if stored and ctx.hasher.needs_rehash(stored):
        upgraded = await asyncio.to_thread(ctx.hasher.hash, password)
        ctx.accounts.upgrade_hash(user.id, upgraded, stored)
    return await sign_in(request, ctx, session, user, "password")


@public
async def verify_page(request: Request) -> Response:
    """Show the second factor page for a session waiting for a code.

    Args:
        request: the incoming request.

    Returns:
        The verification page, or a redirect home when no code is pending.
    """
    session = current_session(request)
    if session is None or session.stage != SECOND:
        return RedirectResponse("/", status_code=303)
    request.state.session = session
    return render(request, "verify.html", pending=session.pending, errors=[])


@public
async def verify(request: Request) -> Response:
    """Check the second factor code or resend an emailed code.

    Args:
        request: the incoming request.

    Returns:
        A redirect after a valid code, or the verification page with an error.

    Raises:
        HTTPException: 403 when there is no pending second factor, 429 when the
            session or account made too many attempts.
    """
    ctx = context_of(request)
    session = current_session(request)
    if session is None or session.stage != SECOND or session.user_id is None:
        raise HTTPException(403, EXPIRED_FORM)
    request.state.session = session
    form = await protected_form(request, session)
    try:
        code = text(form, "code", ctx.settings.auth.max_code_chars)
        resend = text(form, "resend", 4)
    finally:
        await form.close()
    user = ctx.accounts.user(session.user_id)
    if user is None or user.status != "active" or user.token_version != session.version:
        ctx.sessions.destroy(session.id)
        return RedirectResponse("/", status_code=303)
    if resend and session.pending == EMAIL:
        if ctx.second_factor_limiter.allow(f"resend:{user.id}"):
            await mail_code(ctx, user)
        return render(
            request,
            "verify.html",
            pending=session.pending,
            errors=[],
            notes=["A new code was sent."],
        )
    if not (
        ctx.second_factor_limiter.allow(session.id) and ctx.second_factor_limiter.allow(user.id)
    ):
        ctx.sessions.destroy(session.id)
        raise HTTPException(429, TOO_MANY)
    if not await accept_code(ctx, user, session.pending, code):
        audit(request, "login.second_factor", outcome="denied", actor_name=user.username)
        return render(request, "verify.html", 401, pending=session.pending, errors=[CODE_FAILED])
    ctx.second_factor_limiter.reset(session.id)
    ctx.second_factor_limiter.reset(user.id)
    return await finish(request, ctx, session, user.id, user.token_version, session.pending, user)


async def accept_code(ctx: WebContext, user: User, pending: str, code: str) -> bool:
    """Check a submitted second factor code.

    An emailed code is checked against the issued code. Otherwise a TOTP code is
    checked with replay protection, and a recovery code is accepted as fallback.

    Args:
        ctx: the web context.
        user: the account being verified.
        pending: the kind of code awaited, email or totp.
        code: the submitted code.

    Returns:
        True when the code was accepted and consumed.
    """
    if pending == EMAIL:
        return ctx.accounts.check_code(user.id, LOGIN, code)
    sealed = ctx.accounts.secrets_of(user.id)
    secret = ctx.vault.decrypt(sealed.totp_secret)
    if secret is not None:
        counter = check_totp(secret, code, time.time(), ctx.settings.auth, sealed.totp_counter)
        if counter is not None and ctx.accounts.use_counter(user.id, counter):
            return True
    return ctx.accounts.use_recovery_code(user.id, code)


async def mail_link(ctx: WebContext, user: User, purpose: str, label: str, hours: float) -> None:
    """Email a single use sign in or password reset link.

    Args:
        ctx: the web context.
        user: the account that receives the link.
        purpose: the link kind, reset or login.
        label: short action text used in the message body.
        hours: lifetime of the link in hours.
    """
    token = ctx.accounts.create_link(user.id, purpose, hours * 3600, "self")
    base = ctx.web.public_origin or ctx.base_url
    path = {RESET: "reset", LOGIN: "l"}[purpose]
    subject = ctx.settings.mail.system_subjects["reset" if purpose == RESET else "login_link"]
    body = (
        f"Open this link to {label}:\n\n{base}/{path}/{token}\n\nIt works once and expires "
        f"in {int(hours * 60)} minutes. If you did not ask for it, ignore this message."
    )
    await ctx.mailer.send(user.email or "", subject, body, kind=SYSTEM, purpose=purpose)


async def send_link(request: Request, purpose: str) -> Response:
    """Handle a request for an emailed sign in or reset link.

    The answer is always the same message, and the lookup and mailing run in the
    background, so the response does not reveal whether the address has an account.

    Args:
        request: the incoming request.
        purpose: the link kind, reset or login.

    Returns:
        The sign in page with a neutral notice.

    Raises:
        HTTPException: 403 when the session expired, 429 when rate limited.
    """
    ctx = context_of(request)
    session = current_session(request)
    if session is None:
        raise HTTPException(403, EXPIRED_FORM)
    request.state.session = session
    form = await protected_form(request, session)
    try:
        email = text(form, "email", ctx.settings.mail.max_address_chars)
    finally:
        await form.close()
    if not ctx.link_limiter.allow(client_key(request)):
        raise HTTPException(429, TOO_MANY)
    policy = ctx.policy.get()
    enabled = ctx.mailer.configured() and (policy.allow_magic_links or purpose == RESET)

    async def work() -> None:
        """Mail the link to the matching account unless one was sent recently."""
        user = ctx.accounts.user_with_email(email) if email else None
        if user is None:
            return
        auth = ctx.settings.auth
        if ctx.accounts.recent_link(user.id, purpose, auth.link_cooldown_seconds):
            return
        hours = auth.reset_hours if purpose == RESET else auth.login_link_minutes / 60
        label = "choose a new password" if purpose == RESET else "sign in"
        try:
            await mail_link(ctx, user, purpose, label, hours)
        except AgentError as error:
            logger.warning("a %s link could not be sent: %s", purpose, type(error).__name__)

    if enabled:
        # The lookup runs after the response so timing does not reveal whether the address exists.
        background(work())
    return landing(request, notes=[LINK_SENT])


@public
async def forgot(request: Request) -> Response:
    """Handle the forgotten password form.

    Args:
        request: the incoming request.

    Returns:
        The sign in page with a neutral notice.
    """
    return await send_link(request, RESET)


@public
async def link_request(request: Request) -> Response:
    """Handle the request for an emailed sign in link.

    Args:
        request: the incoming request.

    Returns:
        The sign in page with a neutral notice.
    """
    return await send_link(request, LOGIN)


def link_session(request: Request, ctx: WebContext) -> tuple[Session, bool]:
    """Return the current session, or open an anonymous one for a link page.

    Args:
        request: the incoming request.
        ctx: the web context.

    Returns:
        The session and whether it was newly created.

    Raises:
        HTTPException: 429 when the client address opened too many sessions.
    """
    session = current_session(request)
    fresh = session is None
    if session is None:
        if not ctx.session_limiter.allow(client_key(request)):
            raise HTTPException(429, "Too many sessions were opened from this address.")
        session = ctx.sessions.create(ip=client_address(request), agent=agent_of(request))
    request.state.session = session
    return session, fresh


async def show_link(request: Request, purpose: str) -> Response:
    """Show the page behind an emailed link without using the link up.

    Args:
        request: the incoming request, with the token in its path.
        purpose: the link kind, invite, reset or login.

    Returns:
        The link page, with a 404 status when the link is not valid.

    Raises:
        HTTPException: 429 when rate limited.
    """
    ctx = context_of(request)
    if not ctx.link_limiter.allow(client_key(request)):
        raise HTTPException(429, TOO_MANY)
    title, action, password = LINK_TEXT[purpose]
    token = request.path_params["token"]
    info = ctx.accounts.peek_link(token, purpose)
    session, fresh = link_session(request, ctx)
    user = ctx.accounts.user(info.user_id) if info else None
    valid = user is not None and user.status != "disabled"
    response = render(
        request,
        "link.html",
        200 if valid else 404,
        valid=valid,
        title=title,
        action=action,
        password=password,
        token=token,
        target=user,
        post_to=request.url.path,
        errors=[] if valid else [LINK_INVALID],
    )
    if fresh:
        attach_cookie(response, ctx.web, session)
    return response


async def accept_link(request: Request, purpose: str) -> Response:
    """Redeem an emailed link and sign the user in.

    For invite and reset links the new password must match its confirmation and
    pass the password rules. The link is consumed only after the checks pass.

    Args:
        request: the incoming request, with the token in its path.
        purpose: the link kind, invite, reset or login.

    Returns:
        A sign in result, or the link page with errors.

    Raises:
        HTTPException: 403 when the session expired, 429 when rate limited.
    """
    ctx = context_of(request)
    session = current_session(request)
    if session is None:
        raise HTTPException(403, EXPIRED_FORM)
    request.state.session = session
    if not ctx.link_limiter.allow(client_key(request)):
        raise HTTPException(429, TOO_MANY)
    title, action, password = LINK_TEXT[purpose]
    token = request.path_params["token"]
    form = await protected_form(request, session)
    try:
        first = text(form, "password", ctx.settings.auth.password_max_length + 1)
        second = text(form, "confirm", ctx.settings.auth.password_max_length + 1)
    finally:
        await form.close()
    info = ctx.accounts.peek_link(token, purpose)
    user = ctx.accounts.user(info.user_id) if info else None
    usable = user is not None and user.status != "disabled"
    errors: list[str] = []
    if not usable or user is None:
        errors.append(LINK_INVALID)
    elif password:
        if first != second:
            errors.append("The two passwords are not the same.")
        errors.extend(ctx.hasher.problems(first, user.username, user.display_name))
    if errors or user is None:
        return render(
            request,
            "link.html",
            400,
            valid=usable,
            title=title,
            action=action,
            password=password,
            token=token,
            target=user,
            post_to=request.url.path,
            errors=errors,
        )
    redeemed = ctx.accounts.redeem_link(token, purpose)
    if redeemed is None or redeemed.user_id != user.id:
        return landing(request, 400, [LINK_INVALID])
    if password:
        encoded = await asyncio.to_thread(ctx.hasher.hash, first)
        ctx.accounts.set_password(user.id, encoded)
    ctx.accounts.log(f"link.{purpose}", actor_name=user.username, ip=client_address(request))
    fresh = ctx.accounts.user(user.id)
    return await sign_in(request, ctx, session, fresh or user, f"link-{purpose}")


@public
async def invite_page(request: Request) -> Response:
    """Show the page for accepting an invitation.

    Args:
        request: the incoming request.

    Returns:
        The invitation page.
    """
    return await show_link(request, INVITE)


@public
async def invite_accept(request: Request) -> Response:
    """Accept an invitation by choosing a password.

    Args:
        request: the incoming request.

    Returns:
        A sign in result, or the page with errors.
    """
    return await accept_link(request, INVITE)


@public
async def reset_page(request: Request) -> Response:
    """Show the page for choosing a new password.

    Args:
        request: the incoming request.

    Returns:
        The reset page.
    """
    return await show_link(request, RESET)


@public
async def reset_accept(request: Request) -> Response:
    """Save a new password from a reset link.

    Args:
        request: the incoming request.

    Returns:
        A sign in result, or the page with errors.
    """
    return await accept_link(request, RESET)


@public
async def signin_page(request: Request) -> Response:
    """Show the page for signing in with an emailed link.

    Args:
        request: the incoming request.

    Returns:
        The link sign in page.
    """
    return await show_link(request, LOGIN)


@public
async def signin_accept(request: Request) -> Response:
    """Sign in with an emailed link.

    Args:
        request: the incoming request.

    Returns:
        A sign in result, or the page with errors.
    """
    return await accept_link(request, LOGIN)


@requires(enrolling=True)
async def logout(request: Request) -> Response:
    """Sign the user out and clear the session cookie.

    Args:
        request: the incoming request.

    Returns:
        A redirect home that deletes the cookie and asks the browser to clear its
        cache.
    """
    ctx = context_of(request)
    session = request.state.session
    form = await protected_form(request, session)
    await form.close()
    audit(request, "logout")
    ctx.sessions.destroy(session.id)
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(
        cookie_name(ctx.web),
        path="/",
        secure=ctx.web.cookie_secure,
        httponly=True,
        samesite="strict",
    )
    response.headers["Clear-Site-Data"] = '"cache"'
    return response


def setup_allowed(ctx: WebContext, code: str) -> bool:
    """Check the first run setup code.

    Args:
        ctx: the web context holding the expected code.
        code: the submitted code.

    Returns:
        True when a setup code exists and matches in constant time.
    """
    return ctx.setup_code is not None and tokens_match(ctx.setup_code, code)


@public
async def setup_page(request: Request) -> Response:
    """Show the first run setup page.

    Args:
        request: the incoming request.

    Returns:
        The setup page, or a redirect home when setup is already done.
    """
    ctx = context_of(request)
    if not ctx.accounts.needs_setup():
        return RedirectResponse("/", status_code=303)
    session, fresh = link_session(request, ctx)
    code = request.query_params.get("code", "")[: ctx.settings.auth.max_code_chars]
    response = render(
        request,
        "setup.html",
        200,
        valid=setup_allowed(ctx, code),
        code=code,
        errors=[],
        values={},
    )
    if fresh:
        attach_cookie(response, ctx.web, session)
    return response


@public
async def setup_create(request: Request) -> Response:
    """Create the first administrator account from the setup form.

    The setup code is cleared after the account is created and the user is
    signed in.

    Args:
        request: the incoming request.

    Returns:
        A redirect to the connections page, or the setup page with errors.

    Raises:
        HTTPException: 403 when the session expired or the setup code is wrong,
            429 when rate limited.
    """
    ctx = context_of(request)
    session = current_session(request)
    if session is None:
        raise HTTPException(403, EXPIRED_FORM)
    request.state.session = session
    if not ctx.accounts.needs_setup():
        return RedirectResponse("/", status_code=303)
    if not ctx.link_limiter.allow(client_key(request)):
        raise HTTPException(429, TOO_MANY)
    auth = ctx.settings.auth
    form = await protected_form(request, session)
    try:
        values = {
            "code": text(form, "code", auth.max_code_chars),
            "username": text(form, "username", auth.max_username_chars),
            "display_name": text(form, "display_name", 80),
            "email": text(form, "email", ctx.settings.mail.max_address_chars),
        }
        first = text(form, "password", auth.password_max_length + 1)
        second = text(form, "confirm", auth.password_max_length + 1)
    finally:
        await form.close()
    if not setup_allowed(ctx, values["code"]):
        raise HTTPException(403, "The setup code was not accepted.")
    errors = []
    if first != second:
        errors.append("The two passwords are not the same.")
    errors.extend(ctx.hasher.problems(first, values["username"], values["display_name"]))
    try:
        username, email = ctx.accounts.validate_identity(values["username"], values["email"])
    except AgentError as error:
        errors.append(str(error))
    if errors:
        return render(
            request,
            "setup.html",
            400,
            valid=True,
            code=values["code"],
            errors=errors,
            values=values,
        )
    role = ctx.accounts.role_named(auth.first_admin_role)
    encoded = await asyncio.to_thread(ctx.hasher.hash, first)
    try:
        user = ctx.accounts.create_user(
            username,
            role.id if role else "",
            display_name=values["display_name"],
            email=email,
            password_hash=encoded,
            only_if_empty=True,
        )
    except AgentError as error:
        return render(
            request,
            "setup.html",
            400,
            valid=True,
            code=values["code"],
            errors=[str(error)],
            values=values,
        )
    ctx.setup_code = None
    ctx.accounts.log("setup.complete", actor_name=user.username, ip=client_address(request))
    response = await finish(request, ctx, session, user.id, user.token_version, "setup", user)
    # Override the redirect chosen by finish so a new admin lands on the connections page.
    response.headers["location"] = "/admin/connections?setup=1"
    return response
