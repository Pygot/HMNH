# src/agent/web/admin.py
from agent.accounts import (
    ACTIVE,
    DISABLED,
    INVITE,
    INVITED,
    LOGIN,
    RESET,
    Role,
    STATUSES,
    User,
)
from agent.web.common import (
    audit,
    carry,
    full_admin,
    protected_form,
    render,
    renew_session,
    requires,
    text,
)
from agent.permissions import (
    GROUPS,
    KNOWN,
)
from agent.web.context import (
    context_of,
    WebContext,
)
from starlette.responses import (
    RedirectResponse,
    Response,
)
from starlette.exceptions import HTTPException
from starlette.requests import Request
from agent.errors import AgentError
from agent.web.format import stamp
from agent.mailer import SYSTEM

import logging
import csv
import io

logger = logging.getLogger(__name__)
LINK_PATHS = {INVITE: "invite", RESET: "reset", LOGIN: "l"}
LINK_LABELS = {
    INVITE: "set up the account",
    RESET: "choose a new password",
    LOGIN: "sign in once",
}
SECTIONS = (
    ("users.view", "/admin/users"),
    ("roles.manage", "/admin/roles"),
    ("audit.view", "/admin/audit"),
    ("config.manage", "/admin/connections"),
    ("data.export", "/admin/data"),
    ("data.import", "/admin/import"),
)
POLICY_FIELDS = (
    (
        "allow_password_login",
        "Password sign in",
        "People can sign in with a username and password.",
    ),
    ("allow_magic_links", "Sign in links by email", "People can ask for a one time link by email."),
    (
        "require_totp",
        "Require an authenticator",
        "Everyone must set up two step sign in before using the workspace.",
    ),
    ("email_code_login", "Email codes for everyone", "Sign in also asks for a code sent by email."),
    ("allow_api_keys", "API keys", "People with the permission can create keys for scripts."),
    ("candidates_enabled", "Candidate records", "Keep candidates, contacts and hiring history."),
    (
        "candidates_auto_record",
        "Remember researched people",
        "Every finished search adds or updates a candidate record with its rating.",
    ),
    (
        "messaging_enabled",
        "Messages and announcements",
        "People can write to each other, to roles and to groups, and post announcements.",
    ),
    (
        "messaging_e2ee",
        "Private end to end messages",
        "Allow conversations only the people in them can read, even on the server.",
    ),
    ("routines_enabled", "Routines", "Run saved searches on a schedule."),
    (
        "finance_enabled",
        "Revenue and finance",
        "Keep revenue and expense data and show its charts.",
    ),
    (
        "email_sending_enabled",
        "Sending email",
        "Allow sending email from the workspace. Needs an email connection.",
    ),
)
CSV_UNSAFE = ("=", "+", "-", "@", "\t", "\r")
SAVED = "Saved."
SIGN_IN_POLICIES = (
    "allow_password_login",
    "allow_magic_links",
    "require_totp",
    "email_code_login",
    "allow_api_keys",
)
SIGN_IN_ADMIN_ONLY = (
    "Only an administrator who can manage everything may change how people sign in."
)


def sections_for(request: Request) -> str:
    """Return the path of the first admin section the user may open.

    Args:
        request: current request with the principal in its state.

    Returns:
        The URL path of the first section whose permission the user holds.

    Raises:
        HTTPException: with status 404 when the user may open no admin section.
    """
    principal = request.state.principal
    for permission, path in SECTIONS:
        if principal.can(permission):
            return path
    raise HTTPException(404)


def covers(request: Request, permissions: frozenset[str] | list[str]) -> bool:
    """Check that the current user holds every given permission.

    Args:
        request: current request with the principal in its state.
        permissions: permission names to check.

    Returns:
        True when all of them are held, so nothing is granted beyond the user's own.
    """
    return set(permissions) <= request.state.principal.permissions


def may_manage(request: Request, target: User, ctx: WebContext) -> bool:
    """Check that the current user may manage a given account.

    The user must hold every permission of the target's role.

    Args:
        request: current request with the principal in its state.
        target: account to be managed.
        ctx: web context giving access to accounts.

    Returns:
        True when the role exists and is covered, False otherwise.
    """
    role = ctx.accounts.role(target.role_id)
    return role is not None and covers(request, role.effective())


def link_url(ctx: WebContext, request: Request, purpose: str, token: str) -> str:
    """Build the one-time link URL for a token.

    The public origin from the settings is preferred over the request's own host.

    Args:
        ctx: web context with the web settings.
        request: current request, used when no public origin is set.
        purpose: link purpose, one of invite, reset or login.
        token: one-time token to put in the path.

    Returns:
        The absolute URL.
    """
    base = ctx.web.public_origin or f"{request.url.scheme}://{request.headers.get('host', '')}"
    return f"{base}/{LINK_PATHS[purpose]}/{token}"


def link_hours(ctx: WebContext, purpose: str) -> float:
    """Return how long a link of the given purpose stays valid.

    Args:
        ctx: web context with the auth settings.
        purpose: link purpose, one of invite, reset or login.

    Returns:
        The lifetime in hours.
    """
    auth = ctx.settings.auth
    return {
        INVITE: float(auth.invite_hours),
        RESET: float(auth.reset_hours),
        LOGIN: auth.login_link_minutes / 60,
    }[purpose]


def safe_cell(value: object) -> str:
    """Return a CSV cell neutralised against spreadsheet formula injection.

    Args:
        value: value to write into the cell.

    Returns:
        The text of the value, prefixed with an apostrophe if it starts with a
        character that spreadsheets treat as a formula.
    """
    cell = str(value)
    return f"'{cell}" if cell.startswith(CSV_UNSAFE) else cell


@requires()
async def admin_home(request: Request) -> Response:
    """Redirect to the first admin section the user can open.

    Args:
        request: current request.

    Returns:
        A 303 redirect response.
    """
    return RedirectResponse(sections_for(request), status_code=303)


def users_view(
    request: Request,
    status: int = 200,
    *,
    errors: list[str] | None = None,
    notes: list[str] | None = None,
    values: dict[str, str] | None = None,
) -> Response:
    """Render the user list page.

    The optional q query parameter filters by username, display name or email. Only
    roles whose permissions the viewer holds are offered for new accounts.

    Args:
        request: current request.
        status: HTTP status code of the response.
        errors: error messages to show.
        notes: confirmation messages to show.
        values: form values to show again after a failed submit.

    Returns:
        The rendered HTML response.
    """
    ctx = context_of(request)
    needle = request.query_params.get("q", "")[: ctx.web.max_field_length].strip().lower()
    users = [
        user
        for user in ctx.accounts.users()
        if not needle
        or needle in user.username.lower()
        or needle in user.display_name.lower()
        or needle in (user.email or "").lower()
    ]
    held = request.state.principal.permissions
    grantable = [role for role in ctx.accounts.roles() if set(role.effective()) <= held]
    return render(
        request,
        "admin_users.html",
        status,
        active="admin",
        section="users",
        users=users,
        role_options=[(role.id, role.name) for role in grantable],
        needle=needle,
        errors=errors or [],
        notes=notes or [],
        values=values or {},
        sizes=ctx.accounts.role_sizes(),
        can_mail=ctx.mailer.configured(),
    )


@requires("users.view")
async def users_page(request: Request) -> Response:
    """Show the user list, with a saved note after a redirect.

    Args:
        request: current request.

    Returns:
        The rendered HTML response.
    """
    notes = [SAVED] if request.query_params.get("done") else []
    return users_view(request, notes=notes)


@requires("users.manage")
async def user_create(request: Request) -> Response:
    """Create an invited user and issue an invite link.

    The chosen role must be one the current user fully holds. On success the one-time
    link is shown and may also be emailed.

    Args:
        request: current request with a CSRF-protected form.

    Returns:
        The new user's page, or the user list with errors and status 400.
    """
    ctx = context_of(request)
    auth = ctx.settings.auth
    form = await protected_form(request, request.state.session)
    try:
        values = {
            "username": text(form, "username", auth.max_username_chars),
            "display_name": text(form, "display_name", 80),
            "email": text(form, "email", ctx.settings.mail.max_address_chars),
            "role": text(form, "role", 40),
            "send": text(form, "send", 4),
        }
    finally:
        await form.close()
    role = ctx.accounts.role(values["role"])
    if role is None or not covers(request, role.effective()):
        audit(request, "user.create", outcome="denied", target=values["username"])
        return users_view(
            request, 400, errors=["Choose a role you are allowed to give."], values=values
        )
    try:
        user = ctx.accounts.create_user(
            values["username"],
            role.id,
            display_name=values["display_name"],
            email=values["email"] or None,
            status=INVITED,
        )
    except AgentError as error:
        return users_view(request, 400, errors=[str(error)], values=values)
    audit(request, "user.create", target=user.id, detail={"role": role.name})
    return await issue_link(request, ctx, user, INVITE, values["send"] == "yes")


async def issue_link(
    request: Request, ctx: WebContext, user: User, purpose: str, send: bool
) -> Response:
    """Create a one-time link for a user and show it to the administrator.

    The link can also be emailed when the user has an address and mail is configured.
    A failed email is reported as a note, not as an error. The action is audited.

    Args:
        request: current request.
        ctx: web context.
        user: account the link is for.
        purpose: link purpose, one of invite, reset or login.
        send: whether to email the link as well.

    Returns:
        The user's page with the link revealed once.
    """
    token = ctx.accounts.create_link(
        user.id, purpose, link_hours(ctx, purpose) * 3600, request.state.principal.name
    )
    url = link_url(ctx, request, purpose, token)
    notes = []
    if send and user.email and ctx.mailer.configured():
        subject = ctx.settings.mail.system_subjects["invite" if purpose == INVITE else "reset"]
        body = f"Open this link to {LINK_LABELS[purpose]}:\n\n{url}\n\nIt works once."
        try:
            await ctx.mailer.send(user.email, subject, body, kind=SYSTEM, purpose=purpose)
            notes.append(f"The link was also sent to {user.email}.")
        except AgentError as error:
            notes.append(f"The email could not be sent: {error}")
    audit(request, f"link.{purpose}", target=user.id)
    return user_view(request, user, reveal=url, notes=notes)


def user_view(
    request: Request,
    user: User,
    status: int = 200,
    *,
    errors: list[str] | None = None,
    notes: list[str] | None = None,
    reveal: str = "",
) -> Response:
    """Render the admin page for one user.

    Args:
        request: current request.
        user: account to show.
        status: HTTP status code of the response.
        errors: error messages to show.
        notes: confirmation messages to show.
        reveal: one-time link URL to display, if any.

    Returns:
        The rendered HTML response.
    """
    ctx = context_of(request)
    held = request.state.principal.permissions
    grantable = [role for role in ctx.accounts.roles() if set(role.effective()) <= held]
    return render(
        request,
        "admin_user.html",
        status,
        active="admin",
        section="users",
        target=user,
        role_options=[(role.id, role.name) for role in grantable],
        statuses=[
            (value, value.capitalize())
            for value in STATUSES
            if value != INVITED or user.status == INVITED
        ],
        editable=may_manage(request, user, ctx),
        itself=user.id == request.state.principal.user_id,
        sessions=ctx.sessions.of_user(user.id),
        keys=ctx.accounts.keys(user.id) if request.state.principal.can("apikeys.all") else [],
        history=ctx.accounts.audit(limit=10, actor=user.username),
        errors=errors or [],
        notes=notes or [],
        reveal=reveal,
        can_mail=ctx.mailer.configured() and bool(user.email),
    )


def user_or_404(request: Request) -> User:
    """Return the user named by the user_id path parameter.

    Args:
        request: current request.

    Returns:
        The matching user.

    Raises:
        HTTPException: with status 404 when the user does not exist.
    """
    user = context_of(request).accounts.user(request.path_params["user_id"])
    if user is None:
        raise HTTPException(404)
    return user


@requires("users.view")
async def user_page(request: Request) -> Response:
    """Show the detail page of one user.

    Args:
        request: current request.

    Returns:
        The rendered HTML response.
    """
    return user_view(request, user_or_404(request))


async def manage_target(request: Request) -> tuple[WebContext, User]:
    """Load the user from the path and check the caller may manage them.

    A refused attempt is audited.

    Args:
        request: current request.

    Returns:
        The web context and the target user.

    Raises:
        HTTPException: with status 404 when the user is missing or out of reach.
    """
    ctx = context_of(request)
    user = user_or_404(request)
    if not may_manage(request, user, ctx):
        # Answer 404 instead of 403 so accounts outside the caller's reach are not revealed.
        audit(request, "user.manage", outcome="denied", target=user.id)
        raise HTTPException(404)
    return ctx, user


@requires("users.manage")
async def user_update(request: Request) -> Response:
    """Update a user's name, email, role and status.

    People cannot change their own role or status, and the new role must be fully
    held by the caller. The user is signed out everywhere if the account is no longer
    active or the role changed.

    Args:
        request: current request with a CSRF-protected form.

    Returns:
        The user's page, with a saved note or with errors and status 400.
    """
    ctx, user = await manage_target(request)
    form = await protected_form(request, request.state.session)
    try:
        name = text(form, "display_name", 80)
        email = text(form, "email", ctx.settings.mail.max_address_chars)
        role_id = text(form, "role", 40)
        state = text(form, "status", 12)
    finally:
        await form.close()
    itself = user.id == request.state.principal.user_id
    if itself and (role_id != user.role_id or state != user.status):
        return user_view(request, user, 400, errors=["You cannot change your own role or status."])
    role = ctx.accounts.role(role_id)
    if role is None or not covers(request, role.effective()):
        audit(request, "user.update", outcome="denied", target=user.id)
        return user_view(request, user, 400, errors=["Choose a role you are allowed to give."])
    try:
        updated = ctx.accounts.update_user(
            user.id,
            display_name=name,
            email=email or None,
            clear_email=not email,
            role_id=role.id,
            # Only active and disabled can be set from the form. Any other value leaves the status
            # unchanged.
            status=state if state in (ACTIVE, DISABLED) else None,
        )
    except AgentError as error:
        return user_view(request, user, 400, errors=[str(error)])
    if updated.status != ACTIVE or updated.role_id != user.role_id:
        ctx.sessions.destroy_user(user.id)
    audit(
        request,
        "user.update",
        target=user.id,
        detail={"role": role.name, "status": updated.status},
    )
    return user_view(request, updated, notes=[SAVED])


@requires("users.manage")
async def user_link(request: Request) -> Response:
    """Issue a new invite, reset or sign-in link for a user.

    Args:
        request: current request with a CSRF-protected form.

    Returns:
        The user's page with the link revealed once.

    Raises:
        HTTPException: with status 400 for an unknown purpose or a disabled user.
    """
    ctx, user = await manage_target(request)
    form = await protected_form(request, request.state.session)
    try:
        purpose = text(form, "purpose", 12)
        send = text(form, "send", 4) == "yes"
    finally:
        await form.close()
    if purpose not in LINK_PATHS or user.status == "disabled":
        raise HTTPException(400)
    return await issue_link(request, ctx, user, purpose, send)


@requires("apikeys.all")
async def user_key_revoke(request: Request) -> Response:
    """Revoke one API key of a user.

    Args:
        request: current request with a CSRF-protected form.

    Returns:
        The user's page with a confirmation note.

    Raises:
        HTTPException: with status 404 when the user or key does not exist.
    """
    ctx = context_of(request)
    user = user_or_404(request)
    form = await protected_form(request, request.state.session)
    await form.close()
    key_id = request.path_params["key_id"]
    if not ctx.accounts.revoke_key(key_id, user.id):
        raise HTTPException(404)
    audit(request, "apikey.revoke", target=key_id, detail={"owner": user.username})
    return user_view(request, user, notes=["The key was revoked."])


@requires("users.manage")
async def user_sessions(request: Request) -> Response:
    """Sign a user out everywhere.

    The account's token version is bumped and all its web sessions are destroyed.

    Args:
        request: current request with a CSRF-protected form.

    Returns:
        The user's page with a confirmation note.
    """
    ctx, user = await manage_target(request)
    form = await protected_form(request, request.state.session)
    await form.close()
    ctx.accounts.bump_version(user.id)
    ctx.sessions.destroy_user(user.id)
    audit(request, "user.sessions_revoked", target=user.id)
    return user_view(request, user, notes=["Signed out everywhere."])


@requires("users.manage")
async def user_two_factor(request: Request) -> Response:
    """Remove a user's two step sign in and sign them out everywhere.

    Args:
        request: current request with a CSRF-protected form.

    Returns:
        The user's page with a confirmation note.
    """
    ctx, user = await manage_target(request)
    form = await protected_form(request, request.state.session)
    await form.close()
    ctx.accounts.disable_totp(user.id)
    ctx.sessions.destroy_user(user.id)
    audit(request, "user.totp_removed", target=user.id)
    fresh = ctx.accounts.user(user.id) or user
    return user_view(request, fresh, notes=["Two step sign in was removed."])


@requires("users.manage")
async def user_delete(request: Request) -> Response:
    """Delete a user account and destroy its sessions.

    People cannot remove their own account.

    Args:
        request: current request with a CSRF-protected form.

    Returns:
        A redirect to the user list, or the user's page with an error and status 400.
    """
    ctx, user = await manage_target(request)
    form = await protected_form(request, request.state.session)
    await form.close()
    if user.id == request.state.principal.user_id:
        return user_view(request, user, 400, errors=["You cannot remove your own account."])
    try:
        ctx.accounts.delete_user(user.id)
    except AgentError as error:
        return user_view(request, user, 400, errors=[str(error)])
    ctx.sessions.destroy_user(user.id)
    audit(request, "user.delete", target=user.id, detail={"username": user.username})
    return RedirectResponse("/admin/users?done=1", status_code=303)


def roles_view(
    request: Request,
    status: int = 200,
    *,
    errors: list[str] | None = None,
    notes: list[str] | None = None,
    editing: Role | None = None,
) -> Response:
    """Render the roles page.

    Args:
        request: current request.
        status: HTTP status code of the response.
        errors: error messages to show.
        notes: confirmation messages to show.
        editing: role loaded into the editor, if any.

    Returns:
        The rendered HTML response.
    """
    ctx = context_of(request)
    held = request.state.principal.permissions
    return render(
        request,
        "admin_roles.html",
        status,
        active="admin",
        section="roles",
        roles=ctx.accounts.roles(),
        sizes=ctx.accounts.role_sizes(),
        groups=GROUPS,
        held=held,
        editing=editing,
        errors=errors or [],
        notes=notes or [],
    )


@requires("roles.manage")
async def roles_page(request: Request) -> Response:
    """Show the roles page, optionally with a role loaded for editing.

    Args:
        request: current request; the edit query parameter selects the role.

    Returns:
        The rendered HTML response.
    """
    ctx = context_of(request)
    editing = ctx.accounts.role(request.query_params.get("edit", "")[:40])
    notes = [SAVED] if request.query_params.get("done") else []
    return roles_view(request, editing=editing, notes=notes)


@requires("roles.manage")
async def role_save(request: Request) -> Response:
    """Create or update a role.

    The caller can only give permissions they hold themselves, and cannot change a
    role that holds permissions they lack. If the caller's own role changed, their
    session is renewed.

    Args:
        request: current request with a CSRF-protected form.

    Returns:
        A redirect to the roles page, or the roles page with errors and status 400.

    Raises:
        HTTPException: with status 404 for an unknown role or one out of reach.
    """
    ctx = context_of(request)
    form = await protected_form(request, request.state.session)
    try:
        identifier = text(form, "id", 40)
        name = text(form, "name", 40)
        description = text(form, "description", 300)
        chosen = sorted({value for value in form.getlist("permission") if isinstance(value, str)})
    finally:
        await form.close()
    existing = ctx.accounts.role(identifier) if identifier else None
    if identifier and existing is None:
        raise HTTPException(404)
    if existing is not None and not covers(request, existing.effective()):
        audit(request, "role.save", outcome="denied", target=existing.id)
        raise HTTPException(404)
    if any(item not in KNOWN for item in chosen) or not covers(request, chosen):
        audit(request, "role.save", outcome="denied", target=identifier)
        return roles_view(
            request,
            400,
            errors=["You can only give permissions that you hold yourself."],
            editing=existing,
        )
    try:
        saved = ctx.accounts.save_role(name, description, chosen, existing.id if existing else None)
    except AgentError as error:
        return roles_view(request, 400, errors=[str(error)], editing=existing)
    audit(request, "role.save", target=saved.id, detail={"name": saved.name, "count": len(chosen)})
    me = ctx.accounts.user(request.state.principal.user_id)
    if me is not None and me.role_id == saved.id:
        renew_session(request, ctx, me.id)
    return carry(request, ctx, RedirectResponse("/admin/roles?done=1", status_code=303))


@requires("roles.manage")
async def role_delete(request: Request) -> Response:
    """Delete a role.

    Args:
        request: current request with a CSRF-protected form.

    Returns:
        A redirect to the roles page, or the roles page with an error and status 400.

    Raises:
        HTTPException: with status 404 for an unknown role or one out of reach.
    """
    ctx = context_of(request)
    form = await protected_form(request, request.state.session)
    await form.close()
    role = ctx.accounts.role(request.path_params["role_id"])
    if role is None or not covers(request, role.effective()):
        raise HTTPException(404)
    try:
        ctx.accounts.delete_role(role.id)
    except AgentError as error:
        return roles_view(request, 400, errors=[str(error)])
    audit(request, "role.delete", target=role.id, detail={"name": role.name})
    return RedirectResponse("/admin/roles?done=1", status_code=303)


def audit_filters(request: Request) -> dict[str, str]:
    """Return the audit log filters from the query string.

    Values are trimmed and cut to the maximum field length.

    Args:
        request: current request.

    Returns:
        A mapping with the action, actor and outcome filters.
    """
    limit = context_of(request).web.max_field_length
    return {
        key: request.query_params.get(key, "")[:limit].strip()
        for key in ("action", "actor", "outcome")
    }


def audit_before(request: Request) -> int | None:
    """Parse the before cursor of the audit log from the query string.

    Args:
        request: current request.

    Returns:
        The entry id as an integer, or None when it is missing or not a short ASCII
        number.
    """
    raw = request.query_params.get("before", "")
    return int(raw) if raw.isascii() and raw.isdigit() and len(raw) < 16 else None


@requires("audit.view")
async def audit_page(request: Request) -> Response:
    """Show one page of the audit log with filters and a next-page cursor.

    Args:
        request: current request.

    Returns:
        The rendered HTML response.
    """
    ctx = context_of(request)
    filters = audit_filters(request)
    size = ctx.settings.auth.audit_page_size
    entries = ctx.accounts.audit(limit=size + 1, before=audit_before(request), **filters)
    more = len(entries) > size
    entries = entries[:size]
    return render(
        request,
        "admin_audit.html",
        active="admin",
        section="audit",
        entries=entries,
        filters=filters,
        next_before=entries[-1].id if more and entries else None,
        can_export="data.export" in request.state.principal.permissions,
    )


@requires("audit.view")
async def audit_csv(request: Request) -> Response:
    """Export the filtered audit log as a CSV download.

    Only people who may export data get the file, others see a 404. Text cells are
    guarded against formula injection and the export is itself audited.

    Args:
        request: current request.

    Returns:
        A CSV attachment response.

    Raises:
        HTTPException: with status 404 when the user may not export data.
    """
    ctx = context_of(request)
    if not request.state.principal.can("data.export"):
        raise HTTPException(404)
    entries = ctx.accounts.audit(
        limit=ctx.settings.auth.audit_export_rows,
        before=audit_before(request),
        **audit_filters(request),
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "time", "actor", "action", "target", "outcome", "ip"])
    for entry in entries:
        writer.writerow(
            [
                entry.id,
                stamp(entry.ts, ctx.web.time_format),
                safe_cell(entry.actor),
                entry.action,
                safe_cell(entry.target),
                entry.outcome,
                entry.ip,
            ]
        )
    audit(request, "audit.export", detail={"rows": len(entries)})
    return Response(
        buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="audit.csv"'},
    )


def security_view(
    request: Request,
    status: int = 200,
    *,
    errors: list[str] | None = None,
    notes: list[str] | None = None,
) -> Response:
    """Render the sign-in and feature settings page.

    Args:
        request: current request.
        status: HTTP status code of the response.
        errors: error messages to show.
        notes: confirmation messages to show.

    Returns:
        The rendered HTML response.
    """
    ctx = context_of(request)
    current = ctx.policy.get()
    return render(
        request,
        "admin_security.html",
        status,
        active="admin",
        section="security",
        fields=[(name, label, hint, getattr(current, name)) for name, label, hint in POLICY_FIELDS],
        errors=errors or [],
        notes=notes or [],
        mail_ready=ctx.mailer.configured(),
    )


@requires("config.manage")
async def security_page(request: Request) -> Response:
    """Show the sign-in and feature settings page.

    Args:
        request: current request.

    Returns:
        The rendered HTML response.
    """
    notes = [SAVED] if request.query_params.get("done") else []
    return security_view(request, notes=notes)


@requires("config.manage")
async def security_save(request: Request) -> Response:
    """Save the workspace sign-in and feature switches.

    The change is refused if nobody could sign in afterwards, if email codes are
    wanted without an email connection, or if it alters sign-in rules and the caller
    is not a full administrator. Email sending is switched off without a connection.

    Args:
        request: current request with a CSRF-protected form.

    Returns:
        A redirect to the settings page, or the page with errors and status 400 or 403.
    """
    ctx = context_of(request)
    form = await protected_form(request, request.state.session)
    try:
        wanted = {name: text(form, name, 4) == "yes" for name, _, _ in POLICY_FIELDS}
    finally:
        await form.close()
    reachable = wanted["allow_password_login"] or (
        wanted["allow_magic_links"] and ctx.mailer.configured()
    )
    # Lockout guard: refuse a policy with no usable sign-in method unless an access token still
    # allows entry.
    if not reachable and ctx.web.access_token is None:
        return security_view(
            request,
            400,
            errors=["Keep password sign in on, or nobody could sign in."],
        )
    if wanted["email_sending_enabled"] and not ctx.mailer.configured():
        wanted["email_sending_enabled"] = False
    before = ctx.policy.get().model_dump()
    sign_in_changes = [name for name in SIGN_IN_POLICIES if before.get(name) != wanted[name]]
    if sign_in_changes and not full_admin(request.state.principal):
        audit(request, "policy.save", outcome="denied", detail={"changed": sign_in_changes})
        return security_view(request, 403, errors=[SIGN_IN_ADMIN_ONLY])
    if wanted["email_code_login"] and not ctx.mailer.configured():
        return security_view(
            request, 400, errors=["Set up email first, or nobody could receive the codes."]
        )
    ctx.policy.update(**wanted)
    changed = sorted(name for name, value in wanted.items() if before.get(name) != value)
    audit(request, "policy.save", detail={"changed": changed})
    return RedirectResponse("/admin/security?done=1", status_code=303)
