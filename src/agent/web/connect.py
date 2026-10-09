# src/agent/web/connect.py
from agent.web.common import (
    audit,
    full_admin,
    protected_form,
    render,
    requires,
    text,
)
from agent.connections import (
    BOUND_SECRETS,
    BY_NAME,
    Check,
    SECTIONS,
    TARGETS,
)
from agent.envfile import (
    env_target,
    restore,
    snapshot,
)
from agent.web.account import (
    password_ok,
    throttle,
    WRONG_CREDENTIAL,
)
from agent.web.context import (
    context_of,
    service_status,
    WebContext,
)
from starlette.exceptions import HTTPException
from starlette.datastructures import FormData
from starlette.responses import Response
from starlette.requests import Request
from agent.errors import AgentError
from agent.mailer import SYSTEM

import logging
import os

logger = logging.getLogger(__name__)
FIELD_LIMIT = 400
SAVED = "Saved and applied."
MAIL_ADMIN_ONLY = (
    "Only an administrator who can manage everything may change the email server, because "
    "it delivers sign in links and codes."
)


def sections_view(ctx: WebContext) -> list[dict[str, object]]:
    """Build the data for each connection section shown on the settings page.

    Secret values are never included, only whether one is saved. A field is
    locked when the server environment already sets it.

    Args:
        ctx: the web context.

    Returns:
        One dictionary per section with its fields and whether it can be tested.
    """
    present = ctx.connections.present(ctx.settings)
    shown = []
    for section in SECTIONS:
        fields = []
        for field in section.fields:
            fields.append(
                {
                    "field": field,
                    "value": "" if field.secret else present.get(field.key, ""),
                    "saved": field.key in present,
                    "locked": bool(os.environ.get(field.key)),
                }
            )
        shown.append({"section": section, "fields": fields, "testable": section.name in TARGETS})
    return shown


async def connections_view(
    request: Request,
    status: int = 200,
    *,
    errors: list[str] | None = None,
    notes: list[str] | None = None,
    checks: list[Check] | None = None,
    opened: str = "",
) -> Response:
    """Render the connections settings page.

    Args:
        request: the incoming request.
        status: HTTP status code of the response.
        errors: error messages to show on the page.
        notes: informational messages to show on the page.
        checks: results of connection checks to show.
        opened: the name of the section to show expanded.

    Returns:
        The rendered page with the current service status.
    """
    ctx = context_of(request)
    service, problem = await service_status(ctx)
    return render(
        request,
        "admin_connections.html",
        status,
        active="admin",
        section="connections",
        sections=sections_view(ctx),
        service=service,
        service_problem=problem,
        problems=service.problems if service else [],
        errors=errors or [],
        notes=notes or [],
        checks=checks or [],
        opened=opened,
        welcome=request.query_params.get("setup") == "1",
        can_mail=ctx.mailer.configured(),
        own_email=ctx.accounts.user(request.state.principal.user_id),
    )


@requires("config.manage")
async def connections_page(request: Request) -> Response:
    """Show the connections settings page.

    Args:
        request: the incoming request.

    Returns:
        The rendered connections page.
    """
    return await connections_view(request)


def collect(form: FormData, section_name: str) -> dict[str, str | None]:
    """Read the submitted values of one connection section.

    A secret field changes only when a new value is typed or the clear box is
    ticked, so leaving it empty keeps the saved secret. Other fields are set to
    None when emptied.

    Args:
        form: the submitted form.
        section_name: the name of the section being saved.

    Returns:
        A map of setting key to new value, with None meaning remove the value.
    """
    changes: dict[str, str | None] = {}
    for field in BY_NAME[section_name].fields:
        raw = text(form, field.key, FIELD_LIMIT)
        if field.secret:
            if text(form, f"clear_{field.key}", 4) == "yes":
                changes[field.key] = None
            elif raw:
                changes[field.key] = raw
        else:
            changes[field.key] = raw or None
    return changes


async def run_check(ctx: WebContext, target: str) -> Check:
    """Run the live check for one connection target.

    Args:
        ctx: the web context.
        target: the name of the connection to check.

    Returns:
        The result of the check.
    """
    return await ctx.checker.check(target, ctx.settings)


@requires("config.manage")
async def connections_save(request: Request) -> Response:
    """Save one connection section, apply it and test it.

    Outside the owner token, the account password is asked for again and
    throttled, and only a full administrator may change the mail section. A
    secret tied to a service address is removed when that address changes. If
    applying fails, the environment file is restored.

    Args:
        request: the incoming request with the section name in its path.

    Returns:
        The connections page with notes, check results or errors.

    Raises:
        HTTPException: 404 for an unknown section or account, 503 when the service
            host is not available to reload the settings.
    """
    ctx = context_of(request)
    name = request.path_params["section"]
    if name not in BY_NAME:
        raise HTTPException(404)
    form = await protected_form(request, request.state.session)
    try:
        changes = collect(form, name)
        recipient = text(form, "test_to", ctx.settings.mail.max_address_chars)
        confirm = text(form, "confirm_password", ctx.settings.auth.password_max_length + 1)
    finally:
        await form.close()
    principal = request.state.principal
    if name == "mail" and not full_admin(principal):
        audit(request, "connections.save", outcome="denied", detail={"section": name})
        return await connections_view(request, 403, errors=[MAIL_ADMIN_ONLY], opened=name)
    if principal.source != "owner":
        user = ctx.accounts.user(principal.user_id)
        if user is None:
            raise HTTPException(404)
        throttle(ctx, request, user, "connections")
        if not await password_ok(ctx, user, confirm):
            audit(request, "connections.save", outcome="denied", detail={"section": name})
            return await connections_view(request, 400, errors=[WRONG_CREDENTIAL], opened=name)
    if ctx.host is None:
        raise HTTPException(503)
    present = ctx.connections.present(ctx.settings)
    cleared = []
    for secret, endpoints in BOUND_SECRETS.items():
        if secret in changes:
            continue
        if any(
            key in changes and (changes[key] or "") != present.get(key, "") for key in endpoints
        ):
            # A saved secret must not be sent to a different address, so it is dropped when its
            # endpoint changes.
            changes[secret] = None
            cleared.append(secret)
    target = env_target()
    # Keep a copy of the env file so a failed reload can roll the change back.
    backup = snapshot(target)
    try:
        settings, shadowed = ctx.connections.apply(changes)
        await ctx.host.reload(settings)
    except AgentError as error:
        restore(target, backup)
        audit(request, "connections.save", outcome="denied", detail={"section": name})
        return await connections_view(request, 400, errors=[str(error)], opened=name)
    audit(request, "connections.save", detail={"section": name, "keys": sorted(changes)})
    notes = [SAVED]
    if cleared:
        notes.append(
            "The saved secret was removed because its service address changed. Enter it again "
            "if the new address needs one: " + ", ".join(cleared) + "."
        )
    if shadowed:
        notes.append(
            "These values are also set in the server environment and that one wins: "
            + ", ".join(shadowed)
            + "."
        )
    checks = [await run_check(ctx, name)] if name in TARGETS else []
    if name == "mail" and recipient and ctx.mailer.configured():
        notes.append(await mail_test(request, ctx, recipient))
    return await connections_view(request, notes=notes, checks=checks, opened=name)


async def mail_test(request: Request, ctx: WebContext, recipient: str) -> str:
    """Send a test email and describe the outcome.

    Args:
        request: the incoming request, used to record who sent the message.
        ctx: the web context.
        recipient: the address that receives the test message.

    Returns:
        A message saying the email was sent or why it failed.
    """
    try:
        await ctx.mailer.send(
            recipient,
            ctx.settings.mail.system_subjects["test"],
            ctx.settings.mail.test_body,
            kind=SYSTEM,
            purpose="test",
            actor=request.state.principal,
        )
    except AgentError as error:
        return f"The test message could not be sent: {error}"
    return f"A test message was sent to {recipient}."


@requires("config.manage")
async def connections_test(request: Request) -> Response:
    """Test one connection without saving anything.

    For mail, a test message is sent. Other targets are checked live.

    Args:
        request: the incoming request with the target and optional test address.

    Returns:
        The connections page with the test result.

    Raises:
        HTTPException: 404 when the target is unknown.
    """
    ctx = context_of(request)
    form = await protected_form(request, request.state.session)
    try:
        target = text(form, "target", 12)
        recipient = text(form, "test_to", ctx.settings.mail.max_address_chars)
    finally:
        await form.close()
    notes: list[str] = []
    checks: list[Check] = []
    if target == "mail":
        if not ctx.mailer.configured():
            return await connections_view(
                request, 400, errors=["Email is not set up yet."], opened="mail"
            )
        notes.append(await mail_test(request, ctx, recipient))
    elif target in TARGETS:
        checks.append(await run_check(ctx, target))
    else:
        raise HTTPException(404)
    audit(request, "connections.test", detail={"target": target})
    return await connections_view(request, notes=notes, checks=checks, opened=target)
