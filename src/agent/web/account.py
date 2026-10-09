# src/agent/web/account.py
from agent.web.common import (
    audit,
    carry,
    protected_form,
    render,
    renew_session,
    requires,
    text,
)
from agent.totp import (
    check as check_totp,
    new_recovery_codes,
    new_secret,
    provisioning_uri,
    qr_svg,
)
from agent.web.context import (
    client_key,
    context_of,
    WebContext,
)
from agent.web.security import (
    ENROLL,
    Session,
)
from starlette.responses import (
    RedirectResponse,
    Response,
)
from starlette.exceptions import HTTPException
from starlette.requests import Request
from agent.permissions import GROUPS
from agent.errors import AgentError
from agent.accounts import User
from agent.mailer import SYSTEM

import asyncio
import logging
import time

logger = logging.getLogger(__name__)
VERIFY = "verify"
WRONG_CREDENTIAL = "That password is not correct."
WRONG_CODE = "That code was not accepted."
TOO_MANY = "Too many attempts; wait a few minutes."
NOT_AVAILABLE = "That is not available for this account."
NOTES = {
    "saved": "Saved.",
    "password": "Password changed. Other devices were signed out.",
    "email": "Email confirmed.",
    "sessions": "Signed out everywhere else.",
    "totp_off": "Two step sign in is off.",
    "revoked": "The key was revoked.",
}


def sign_in_account(request: Request) -> User:
    """Return the account of the signed-in user.

    Args:
        request: Current request with an authenticated principal.

    Returns:
        The stored user record.

    Raises:
        HTTPException: with status 404 when the account no longer exists.
    """
    ctx = context_of(request)
    user = ctx.accounts.user(request.state.principal.user_id)
    if user is None:
        raise HTTPException(404)
    return user


def throttle(ctx: WebContext, request: Request, user: User, label: str) -> None:
    """Limit how often a sensitive action can be tried.

    The attempt is counted against both the user and the client address, and
    it is refused when either of the two limits is used up.

    Args:
        ctx: Web context holding the account rate limiter.
        request: Current request, used to find the client address.
        user: Account performing the action.
        label: Name of the action, which keeps separate counters per action.

    Raises:
        HTTPException: with status 429 when there were too many attempts.
    """
    if not (
        ctx.account_limiter.allow(f"{label}:{user.id}")
        and ctx.account_limiter.allow(f"{label}:{client_key(request)}")
    ):
        raise HTTPException(429, TOO_MANY)


async def password_ok(ctx: WebContext, user: User, supplied: str) -> bool:
    """Check a password against the stored hash of a user.

    The check runs in a worker thread so the event loop is not blocked.

    Args:
        ctx: Web context holding the accounts and the hasher.
        user: Account to check.
        supplied: Password typed by the user.

    Returns:
        True when the password matches, or when the account has no password.
    """
    stored = ctx.accounts.secrets_of(user.id).password_hash
    if not stored:
        return True
    return await asyncio.to_thread(ctx.hasher.verify, supplied, stored)


def key_choices(request: Request) -> list[tuple[str, list[tuple[str, str]]]]:
    """Group the permissions that the user may give to an API key.

    Args:
        request: Current request carrying the principal and its permissions.

    Returns:
        Pairs of group name and a list of permission value and label, limited
        to permissions the principal holds. Empty groups are left out.
    """
    held = request.state.principal.permissions
    shown = []
    for group, entries in GROUPS.items():
        allowed = [(value, label) for value, label in entries if value in held]
        if allowed:
            shown.append((group, allowed))
    return shown


def account_view(
    request: Request,
    status: int = 200,
    *,
    errors: list[str] | None = None,
    notes: list[str] | None = None,
    new_key: str = "",
    section: str = "profile",
) -> Response:
    """Render the account page.

    The API key list is only filled when the user may own keys and the policy
    allows API keys.

    Args:
        request: Current request.
        status: HTTP status of the response.
        errors: Error messages to show.
        notes: Confirmation messages to show.
        new_key: Token of a key that was just created, shown only this once.
        section: Part of the page to open first.

    Returns:
        The rendered page.
    """
    ctx = context_of(request)
    principal = request.state.principal
    session: Session = request.state.session
    user = ctx.accounts.user(principal.user_id)
    policy = ctx.policy.get()
    keys_allowed = user is not None and principal.can("apikeys.own") and policy.allow_api_keys
    return render(
        request,
        "account.html",
        status,
        active="account",
        user=user,
        errors=errors or [],
        notes=notes or [],
        section=section,
        new_key=new_key,
        sessions=ctx.sessions.listing(principal.user_id, session.id),
        keys=ctx.accounts.keys(user.id) if user is not None and keys_allowed else [],
        keys_allowed=keys_allowed,
        key_choices=key_choices(request),
        key_days=[(str(days), f"{days} days") for days in ctx.settings.auth.api_key_day_choices],
        key_default=str(ctx.settings.auth.api_key_days),
        recovery_left=ctx.accounts.recovery_remaining(user.id) if user is not None else 0,
        can_email_codes=ctx.mailer.configured() and user is not None and bool(user.email),
        password_min=ctx.settings.auth.password_min_length,
    )


@requires()
async def account_page(request: Request) -> Response:
    """Show the account page, with a notice for a finished action.

    Args:
        request: Current request; its done query parameter picks the notice.

    Returns:
        The rendered account page.
    """
    note = NOTES.get(request.query_params.get("done", ""))
    return account_view(request, notes=[note] if note else [])


def done(key: str, anchor: str = "") -> Response:
    """Redirect to the account page after an action succeeded.

    Args:
        key: Key of the confirmation text to show.
        anchor: Page fragment to scroll to, such as #email.

    Returns:
        A 303 redirect to the account page.
    """
    return RedirectResponse(f"/account?done={key}{anchor}", status_code=303)


@requires()
async def save_profile(request: Request) -> Response:
    """Save the display name and email address of the signed-in user.

    Changing the email address requires the current password and turns off
    sign-in by email until the new address is confirmed. Attempts are throttled.

    Args:
        request: Current request with the submitted form.

    Returns:
        A redirect on success, otherwise the account page with status 400 and
        the errors.
    """
    ctx = context_of(request)
    user = sign_in_account(request)
    form = await protected_form(request, request.state.session)
    try:
        name = text(form, "display_name", ctx.settings.auth.max_username_chars)
        email = text(form, "email", ctx.settings.mail.max_address_chars)
        current = text(form, "current", ctx.settings.auth.password_max_length + 1)
    finally:
        await form.close()
    changed_email = email.lower() != (user.email or "").lower()
    throttle(ctx, request, user, "profile")
    if changed_email and not await password_ok(ctx, user, current):
        audit(request, "account.profile", outcome="denied")
        return account_view(request, 400, errors=[WRONG_CREDENTIAL])
    try:
        ctx.accounts.update_user(
            user.id,
            display_name=name,
            email=email or None,
            clear_email=not email,
        )
        if changed_email:
            ctx.accounts.set_email_login(user.id, False)
    except AgentError as error:
        return account_view(request, 400, errors=[str(error)])
    audit(request, "account.profile", detail={"email_changed": changed_email})
    return done("saved")


@requires()
async def change_password(request: Request) -> Response:
    """Change the password of the signed-in user.

    The current password must be correct, the two new entries must match and
    the password policy must be met. On success outstanding links of the user
    are revoked and the session is renewed. Attempts are throttled.

    Args:
        request: Current request with the submitted form.

    Returns:
        A redirect on success, otherwise the account page with status 400 and
        the errors.
    """
    ctx = context_of(request)
    user = sign_in_account(request)
    auth = ctx.settings.auth
    form = await protected_form(request, request.state.session)
    try:
        current = text(form, "current", auth.password_max_length + 1)
        first = text(form, "password", auth.password_max_length + 1)
        second = text(form, "confirm", auth.password_max_length + 1)
    finally:
        await form.close()
    throttle(ctx, request, user, "password")
    errors = []
    if not await password_ok(ctx, user, current):
        errors.append(WRONG_CREDENTIAL)
    if first != second:
        errors.append("The two new passwords are not the same.")
    errors.extend(ctx.hasher.problems(first, user.username, user.display_name))
    if errors:
        audit(request, "account.password", outcome="denied")
        return account_view(request, 400, errors=errors, section="password")
    encoded = await asyncio.to_thread(ctx.hasher.hash, first)
    ctx.accounts.set_password(user.id, encoded)
    ctx.accounts.revoke_links(user.id)
    audit(request, "account.password")
    renew_session(request, ctx, user.id)
    return carry(request, ctx, done("password", "#password"))


async def mail_verification(ctx: WebContext, user: User) -> None:
    """Email a one-time confirmation code to the user.

    The code works once and expires after the configured number of minutes.

    Args:
        ctx: Web context holding the accounts, mailer and settings.
        user: Account whose address receives the code.

    Raises:
        AgentError: when the mail cannot be sent.
    """
    code = ctx.accounts.issue_code(user.id, VERIFY)
    body = (
        f"Your confirmation code is {code}.\n\nIt works once and for "
        f"{ctx.settings.auth.email_code_minutes} minutes."
    )
    await ctx.mailer.send(
        user.email or "",
        ctx.settings.mail.system_subjects["verify"],
        body,
        kind=SYSTEM,
        purpose="verify",
    )


@requires()
async def email_send_code(request: Request) -> Response:
    """Send a confirmation code to the email address of the user.

    Attempts are throttled.

    Args:
        request: Current request with the submitted form.

    Returns:
        The account page with a notice, status 400 when mail is not set up or
        the user has no address, or status 502 when sending fails.
    """
    ctx = context_of(request)
    user = sign_in_account(request)
    form = await protected_form(request, request.state.session)
    await form.close()
    throttle(ctx, request, user, "email")
    if not (ctx.mailer.configured() and user.email):
        return account_view(request, 400, errors=[NOT_AVAILABLE])
    try:
        await mail_verification(ctx, user)
    except AgentError as error:
        return account_view(request, 502, errors=[str(error)])
    return account_view(request, notes=["A confirmation code was sent."], section="email")


@requires()
async def email_confirm(request: Request) -> Response:
    """Confirm the email address with the code that was mailed.

    Attempts are throttled.

    Args:
        request: Current request with the submitted code.

    Returns:
        A redirect on success, otherwise the account page with status 400.
    """
    ctx = context_of(request)
    user = sign_in_account(request)
    form = await protected_form(request, request.state.session)
    try:
        code = text(form, "code", ctx.settings.auth.max_code_chars)
    finally:
        await form.close()
    throttle(ctx, request, user, "email")
    if not ctx.accounts.check_code(user.id, VERIFY, code):
        audit(request, "account.email_verify", outcome="denied")
        return account_view(request, 400, errors=[WRONG_CODE], section="email")
    ctx.accounts.mark_email_verified(user.id)
    audit(request, "account.email_verify")
    return done("email", "#email")


@requires()
async def email_login(request: Request) -> Response:
    """Turn sign-in by emailed code on or off for the user.

    Turning it on needs a confirmed address and a configured mailer.

    Args:
        request: Current request with the submitted choice.

    Returns:
        A redirect on success, otherwise the account page with status 400.
    """
    ctx = context_of(request)
    user = sign_in_account(request)
    form = await protected_form(request, request.state.session)
    try:
        wanted = text(form, "enabled", 4) == "yes"
    finally:
        await form.close()
    if wanted and not (user.email_verified and ctx.mailer.configured()):
        return account_view(request, 400, errors=[NOT_AVAILABLE], section="email")
    ctx.accounts.set_email_login(user.id, wanted)
    audit(request, "account.email_login", detail={"enabled": wanted})
    return done("saved", "#email")


@requires()
async def sessions_revoke(request: Request) -> Response:
    """Sign the user out of other sessions.

    With a session handle only that session is ended. Without one every other
    session is ended and all API keys of the user are revoked. The current
    session always stays.

    Args:
        request: Current request with the submitted form.

    Returns:
        A redirect to the account page.
    """
    ctx = context_of(request)
    session: Session = request.state.session
    form = await protected_form(request, session)
    try:
        handle = text(form, "handle", 40)
    finally:
        await form.close()
    user_id = request.state.principal.user_id
    if handle:
        count = ctx.sessions.destroy_handle(user_id, handle, keep=session.id)
    else:
        count = ctx.sessions.destroy_user(user_id, keep=session.id)
        # Signing out everywhere also revokes the user's API keys, so a leaked key stops working
        # too.
        ctx.accounts.revoke_user_keys(user_id)
    audit(request, "account.sessions_revoked", detail={"count": count})
    return done("sessions", "#sessions")


def two_factor_view(
    request: Request,
    status: int = 200,
    *,
    errors: list[str] | None = None,
    codes: list[str] | None = None,
) -> Response:
    """Render the two step sign-in page.

    While two step sign-in is off, a pending secret is created and stored
    encrypted if none exists yet, and its key and QR code are shown.

    Args:
        request: Current request.
        status: HTTP status of the response.
        errors: Error messages to show.
        codes: Recovery codes to show, displayed only when freshly made.

    Returns:
        The rendered page.
    """
    ctx = context_of(request)
    user = sign_in_account(request)
    auth = ctx.settings.auth
    secret = None
    if not user.totp_enabled:
        sealed = ctx.accounts.secrets_of(user.id).totp_secret
        secret = ctx.vault.decrypt(sealed)
        if secret is None:
            secret = new_secret(auth)
            ctx.accounts.begin_totp(user.id, ctx.vault.encrypt(secret))
    uri = provisioning_uri(secret, user.username, auth) if secret else ""
    return render(
        request,
        "twofa.html",
        status,
        active="account",
        user=user,
        totp_key=secret or "",
        qr=qr_svg(uri, auth.qr_scale, auth.qr_border) if uri else "",
        codes=codes or [],
        errors=errors or [],
        required=ctx.policy.get().require_totp,
        recovery_left=ctx.accounts.recovery_remaining(user.id),
        enrolling=request.state.session.stage == ENROLL,
    )


@requires(enrolling=True)
async def two_factor_page(request: Request) -> Response:
    """Show the two step sign-in page.

    Args:
        request: Current request, which may be in the enrollment stage.

    Returns:
        The rendered page.
    """
    return two_factor_view(request)


@requires(enrolling=True)
async def two_factor_enable(request: Request) -> Response:
    """Turn on two step sign-in after checking a code from the app.

    The code is checked against the pending secret. On success new recovery
    codes are shown once and the session is renewed. Attempts are throttled.

    Args:
        request: Current request with the submitted code.

    Returns:
        The page with the recovery codes, or the page with status 400 when the
        code is wrong or two step sign-in is already on.
    """
    ctx = context_of(request)
    user = sign_in_account(request)
    auth = ctx.settings.auth
    form = await protected_form(request, request.state.session)
    try:
        code = text(form, "code", auth.max_code_chars)
    finally:
        await form.close()
    throttle(ctx, request, user, "totp")
    secret = ctx.vault.decrypt(ctx.accounts.secrets_of(user.id).totp_secret)
    counter = check_totp(secret, code, time.time(), auth, None) if secret else None
    codes = new_recovery_codes(auth)
    if (
        user.totp_enabled
        or counter is None
        or not ctx.accounts.enable_totp(user.id, codes, counter)
    ):
        audit(request, "account.totp_enable", outcome="denied")
        return two_factor_view(request, 400, errors=[WRONG_CODE])
    audit(request, "account.totp_enable")
    renew_session(request, ctx, user.id)
    return carry(request, ctx, two_factor_view(request, codes=codes))


@requires(enrolling=True)
async def two_factor_recovery(request: Request) -> Response:
    """Replace the recovery codes after confirming the password.

    Attempts are throttled.

    Args:
        request: Current request with the submitted password.

    Returns:
        The page with the new codes, or the page with status 400.
    """
    ctx = context_of(request)
    user = sign_in_account(request)
    auth = ctx.settings.auth
    form = await protected_form(request, request.state.session)
    try:
        current = text(form, "current", auth.password_max_length + 1)
    finally:
        await form.close()
    throttle(ctx, request, user, "totp")
    if not await password_ok(ctx, user, current):
        audit(request, "account.recovery", outcome="denied")
        return two_factor_view(request, 400, errors=[WRONG_CREDENTIAL])
    codes = new_recovery_codes(auth)
    if not ctx.accounts.replace_recovery_codes(user.id, codes):
        return two_factor_view(request, 400, errors=[NOT_AVAILABLE])
    audit(request, "account.recovery")
    return two_factor_view(request, codes=codes)


@requires(enrolling=True)
async def two_factor_disable(request: Request) -> Response:
    """Turn off two step sign-in after confirming the password.

    Refused while the administrator policy requires two step sign-in. On
    success the session is renewed. Attempts are throttled.

    Args:
        request: Current request with the submitted password.

    Returns:
        A redirect on success, otherwise the page with status 400.
    """
    ctx = context_of(request)
    user = sign_in_account(request)
    form = await protected_form(request, request.state.session)
    try:
        current = text(form, "current", ctx.settings.auth.password_max_length + 1)
    finally:
        await form.close()
    throttle(ctx, request, user, "totp")
    if ctx.policy.get().require_totp:
        return two_factor_view(
            request, 400, errors=["Your administrator requires two step sign in."]
        )
    if not await password_ok(ctx, user, current):
        audit(request, "account.totp_disable", outcome="denied")
        return two_factor_view(request, 400, errors=[WRONG_CREDENTIAL])
    ctx.accounts.disable_totp(user.id)
    audit(request, "account.totp_disable")
    renew_session(request, ctx, user.id)
    return carry(request, ctx, done("totp_off", "#security"))


@requires("apikeys.own")
async def key_create(request: Request) -> Response:
    """Create an API key for the signed-in user.

    Needs the current password. The key can only carry permissions the user
    holds and a lifetime from the offered choices. The token is shown once.
    Attempts are throttled.

    Args:
        request: Current request with the submitted form.

    Returns:
        The account page showing the new token, or the page with status 400.

    Raises:
        HTTPException: with status 404 when the policy does not allow API keys.
    """
    ctx = context_of(request)
    user = sign_in_account(request)
    form = await protected_form(request, request.state.session)
    try:
        name = text(form, "name", ctx.settings.auth.max_username_chars)
        raw_days = text(form, "days", 6)
        current = text(form, "current", ctx.settings.auth.password_max_length + 1)
        chosen = [value for value in form.getlist("permission") if isinstance(value, str)]
    finally:
        await form.close()
    throttle(ctx, request, user, "key")
    held = request.state.principal.permissions
    days = int(raw_days) if raw_days.isascii() and raw_days.isdigit() else None
    if not await password_ok(ctx, user, current):
        audit(request, "apikey.create", outcome="denied")
        return account_view(request, 400, errors=[WRONG_CREDENTIAL], section="keys")
    if not ctx.policy.get().allow_api_keys:
        raise HTTPException(404)
    if days is not None and days not in ctx.settings.auth.api_key_day_choices:
        return account_view(
            request, 400, errors=["Choose one of the offered lifetimes."], section="keys"
        )
    # A key can never carry a permission that its owner does not hold.
    if any(item not in held for item in chosen):
        return account_view(request, 400, errors=[NOT_AVAILABLE], section="keys")
    try:
        key, token = ctx.accounts.create_key(user.id, name, chosen or None, days)
    except AgentError as error:
        return account_view(request, 400, errors=[str(error)], section="keys")
    audit(request, "apikey.create", target=key.id)
    return account_view(request, new_key=token, section="keys")


@requires("apikeys.own")
async def key_revoke(request: Request) -> Response:
    """Revoke one API key of the signed-in user.

    Only keys owned by the user are affected.

    Args:
        request: Current request; the key_id path parameter picks the key.

    Returns:
        A redirect to the account page.
    """
    ctx = context_of(request)
    form = await protected_form(request, request.state.session)
    await form.close()
    key_id = request.path_params["key_id"]
    if ctx.accounts.revoke_key(key_id, request.state.principal.user_id):
        audit(request, "apikey.revoke", target=key_id)
    return done("revoked", "#keys")
