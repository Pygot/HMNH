# src/agent/web/outbox.py
from agent.web.common import (
    audit,
    protected_form,
    render,
    requires,
    text,
)
from agent.errors import (
    AgentError,
    InvalidRequest,
    RateLimitedError,
)
from agent.mailer import (
    OUTREACH,
    Suppression,
)
from agent.web.context import (
    context_of,
    WebContext,
)
from agent.web.emails import (
    make_drafts,
    store_of,
)
from starlette.responses import (
    RedirectResponse,
    Response,
)
from starlette.requests import Request
from agent.models import PersonReport
from agent.web.jobs import WEB_OWNER
from agent.chat import load_profile

import logging

logger = logging.getLogger(__name__)
MODULE = "email_sending_enabled"
NOT_SENT = "The message was not sent."
TOO_MANY = "You are sending too fast; wait a little."
NOT_CONFIRMED = "Tick the box to confirm that you reviewed the message."
UNSET = "Email is not connected yet. An administrator can do that under Connections."
NOTES = {"added": "The address will not be contacted.", "removed": "The address was released."}


def ready(ctx: WebContext) -> bool:
    """Check whether outgoing email is configured.

    Args:
        ctx: Web application context holding the mailer.

    Returns:
        True when the mailer has the settings it needs to send.
    """
    return ctx.mailer.configured()


def suggest_address(request: Request, ctx: WebContext, job: object) -> tuple[str, str]:
    """Suggest a recipient and candidate for the person described by a report.

    Nothing is suggested unless the job holds a person report and the user may view
    contacts. The candidate id is returned even when the candidate has no email.

    Args:
        request: Incoming request, used for the signed in principal.
        ctx: Web application context.
        job: Research job that may hold a person report.

    Returns:
        A tuple of the first saved email address and the matching candidate id, with
        empty strings for whatever is not available.
    """
    principal = request.state.principal
    report = getattr(job, "report", None)
    if not (isinstance(report, PersonReport) and principal.can("contacts.view")):
        return "", ""
    match = ctx.candidates.find_match(report.identity)
    if match is None:
        return "", ""
    for contact in ctx.candidates.contacts(match.id):
        if contact.kind == "email":
            return contact.value, match.id
    return "", match.id


def compose_view(
    request: Request,
    status: int = 200,
    *,
    values: dict[str, str] | None = None,
    errors: list[str] | None = None,
) -> Response:
    """Render the compose email page.

    Args:
        request: Incoming request.
        status: HTTP status code of the response.
        values: Field values to prefill the form with.
        errors: Error messages to show above the form.

    Returns:
        The rendered page.
    """
    ctx = context_of(request)
    return render(
        request,
        "compose.html",
        status,
        active="emails",
        values=values or {},
        errors=errors or [],
        ready=ready(ctx),
        footer=ctx.settings.mail.outreach_footer,
        sender=ctx.settings.smtp_from or "",
    )


@requires("emails.send", MODULE)
async def compose_page(request: Request) -> Response:
    """Show the compose form, optionally prefilled from a job report and a template.

    The job and template come from the job, template and index query parameters.
    Prefilling needs the report viewing permission. The route requires the email
    sending permission and the email sending module to be enabled.

    Args:
        request: Incoming GET request.

    Returns:
        The compose page.
    """
    ctx = context_of(request)
    params = request.query_params
    limit = ctx.web.max_field_length
    job_id = params.get("job", "")[:limit]
    template_id = params.get("template", "")[:limit]
    index = int(params["index"]) if params.get("index", "").isdigit() else 0
    values = {"to": "", "subject": "", "body": "", "job_id": "", "template": "", "candidate_id": ""}
    job = (
        ctx.jobs.get(job_id, WEB_OWNER)
        if job_id and request.state.principal.can("reports.view")
        else None
    )
    template = store_of(ctx).get(template_id) if template_id else None
    if job is not None and template is not None and job.report is not None:
        drafts = make_drafts(template, ctx, load_profile(ctx.store), job, [])
        if 0 <= index < len(drafts):
            address, candidate_id = suggest_address(request, ctx, job)
            values.update(
                to=address,
                subject=drafts[index].subject,
                body=drafts[index].body,
                job_id=job.id,
                template=template.id,
                candidate_id=candidate_id,
            )
    return compose_view(request, values=values)


@requires("emails.send", MODULE)
async def mail_send(request: Request) -> Response:
    """Send the composed outreach email after the sender confirms it.

    The form is protected against cross site requests. The sender must tick the
    confirmation box, is rate limited per user, and the configured outreach footer is
    appended to the body. A sent message is audited and noted on the candidate when
    the sender may edit candidates. Failures re-render the form with a 400, 429 or
    502 status.

    Args:
        request: Incoming POST request with the compose form.

    Returns:
        A redirect to the mail log or the emails page on success, otherwise the
        compose page with error messages.
    """
    ctx = context_of(request)
    tuning = ctx.settings.mail
    form = await protected_form(request, request.state.session)
    try:
        values = {
            "to": text(form, "to", tuning.max_address_chars),
            "subject": text(form, "subject", tuning.max_subject_chars + 50),
            "body": form.get("body") if isinstance(form.get("body"), str) else "",
            "job_id": text(form, "job_id", 40),
            "template": text(form, "template", 40),
            "candidate_id": text(form, "candidate_id", 40),
            "confirm": text(form, "confirm", 4),
        }
    finally:
        await form.close()
    values["body"] = str(values["body"]).strip()[: tuning.max_body_chars + 50]
    principal = request.state.principal
    if not ready(ctx):
        return compose_view(request, 400, values=values, errors=[UNSET])
    if values["confirm"] != "yes":
        return compose_view(request, 400, values=values, errors=[NOT_CONFIRMED])
    if not ctx.mail_limiter.allow(principal.user_id):
        return compose_view(request, 429, values=values, errors=[TOO_MANY])
    # The footer comes from settings, not from the form, so a sender cannot remove it.
    body = f"{values['body']}\n\n{tuning.outreach_footer}"
    # The candidate id from the form is only trusted when the sender may view candidates.
    candidate = (
        ctx.candidates.get(values["candidate_id"])
        if values["candidate_id"] and principal.can("candidates.view")
        else None
    )
    try:
        entry = await ctx.mailer.send(
            values["to"],
            values["subject"],
            body,
            kind=OUTREACH,
            purpose="outreach",
            actor=principal,
            template=values["template"],
            job_id=values["job_id"],
            candidate_id=candidate.id if candidate else "",
        )
    except RateLimitedError as error:
        return compose_view(request, 429, values=values, errors=[f"{NOT_SENT} {error}"])
    except AgentError as error:
        audit(request, "mail.send", outcome="failed", target=values["to"][:100])
        return compose_view(request, 502, values=values, errors=[f"{NOT_SENT} {error}"])
    audit(request, "mail.send", target=entry.to_address, detail={"mail": entry.id})
    if candidate is not None and principal.can("candidates.edit"):
        ctx.candidates.note(candidate.id, f"Emailed: {entry.subject}", principal.name, entry.job_id)
    target = f"/emails/log?sent={entry.id}" if principal.can("emails.log") else "/emails?mailed=1"
    return RedirectResponse(target, status_code=303)


def log_view(request: Request, status: int = 200, errors: list[str] | None = None) -> Response:
    """Render the sent mail log together with the do not contact list.

    The before query parameter pages through older entries, and the sent and done
    parameters add confirmation notes.

    Args:
        request: Incoming request.
        status: HTTP status code of the response.
        errors: Error messages to show on the page.

    Returns:
        The rendered mail log page.
    """
    ctx = context_of(request)
    raw = request.query_params.get("before", "")
    before = int(raw) if raw.isascii() and raw.isdigit() and len(raw) < 16 else None
    size = ctx.settings.mail.log_page_size
    # One extra row is fetched to learn whether another page exists.
    entries = ctx.mailer.entries(before, size + 1)
    more = len(entries) > size
    entries = entries[:size]
    sent = request.query_params.get("sent", "")
    note = NOTES.get(request.query_params.get("done", ""))
    suppressions: list[Suppression] = ctx.mailer.suppressions()
    return render(
        request,
        "mail_log.html",
        status,
        active="emails",
        entries=entries,
        next_before=entries[-1].id if more and entries else None,
        notes=(["The message was sent."] if sent.isdigit() else []) + ([note] if note else []),
        suppressions=suppressions,
        ready=ready(ctx),
        sending=ctx.policy.get().email_sending_enabled,
        errors=errors or [],
    )


@requires("emails.log")
async def mail_log(request: Request) -> Response:
    """Show the mail log page.

    Args:
        request: Incoming GET request.

    Returns:
        The mail log page.
    """
    return log_view(request)


@requires("emails.send")
async def suppress_add(request: Request) -> Response:
    """Add an address to the do not contact list.

    Args:
        request: Incoming POST request with the address and an optional reason.

    Returns:
        A redirect to the mail log on success, or the log page with status 400 when the
        address is rejected.
    """
    ctx = context_of(request)
    form = await protected_form(request, request.state.session)
    try:
        address = text(form, "address", ctx.settings.mail.max_address_chars)
        reason = text(form, "reason", 200)
    finally:
        await form.close()
    try:
        ctx.mailer.suppress(address, reason, request.state.principal.name)
    except InvalidRequest as error:
        return log_view(request, 400, [str(error)])
    audit(request, "mail.suppress", target=address[:100])
    return RedirectResponse("/emails/log?done=added#blocked", status_code=303)


@requires("emails.send")
async def suppress_remove(request: Request) -> Response:
    """Remove an address from the do not contact list.

    The removal is audited only when the address was actually on the list.

    Args:
        request: Incoming POST request with the address.

    Returns:
        A redirect to the mail log.
    """
    ctx = context_of(request)
    form = await protected_form(request, request.state.session)
    try:
        address = text(form, "address", ctx.settings.mail.max_address_chars)
    finally:
        await form.close()
    if ctx.mailer.unsuppress(address):
        audit(request, "mail.unsuppress", target=address[:100])
    return RedirectResponse("/emails/log?done=removed#blocked", status_code=303)
