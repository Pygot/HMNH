# src/agent/web/emails.py
from agent.drafts import (
    build_context,
    Draft,
    DraftCategory,
    DraftPurpose,
    EmailTemplate,
    render_draft,
    sample_context,
    TemplateStore,
    validate_template,
)
from agent.web.common import (
    json_errors,
    protected_form,
    protected_header,
    render,
    requires,
    text,
)
from agent.web.jobs import (
    Job,
    JobState,
    WEB_OWNER,
)
from starlette.responses import (
    JSONResponse,
    RedirectResponse,
    Response,
)
from agent.chat import (
    load_profile,
    Profile,
)
from agent.models import (
    PersonReport,
    SkillSearchReport,
)
from agent.web.context import (
    context_of,
    WebContext,
)
from starlette.exceptions import HTTPException
from agent.permissions import Permission
from agent.errors import InvalidRequest
from starlette.requests import Request
from agent.config import EmailTuning
from typing import Any

import json
import re

MAX_NAME = 120
PLACEHOLDERS = {
    DraftCategory.SINGLE: (
        "candidate.name",
        "candidate.first_name",
        "candidate.last_name",
        "candidate.highlight",
        "candidate.location",
        "company.name",
        "company.category",
        "company.summary",
        "company.products",
        "company.website",
        "role.title",
        "sender.name",
        "sender.company",
        "today",
    ),
    DraftCategory.BATCH: (
        "candidate.name",
        "candidate.first_name",
        "candidate.highlight",
        "company.name",
        "company.category",
        "company.products",
        "role.title",
        "sender.name",
        "sender.company",
        "index",
        "total",
        "today",
    ),
    DraftCategory.TEAM: (
        "candidate.name",
        "company.name",
        "role.title",
        "report.overall",
        "report.level",
        "report.flags",
        "report.findings_count",
        "report.supported_count",
        "report.sources_count",
        "report.coverage",
        "report.requirement_lines",
        "report.top_findings",
        "report.candidates",
        "sender.name",
        "sender.company",
    ),
}
NEW_TEMPLATE = {
    "template_id": "",
    "name": "",
    "category": DraftCategory.SINGLE.value,
    "purpose": DraftPurpose.OUTREACH.value,
    "subject": "",
    "body": "",
}
DEFAULT_TEMPLATES = {
    "person": (("outreach-single", False), ("report-team", True)),
    "skill": (("outreach-batch", False), ("shortlist-team", True)),
}


def store_of(ctx: WebContext) -> TemplateStore:
    """Return the email template store for the current settings.

    Args:
        ctx: The web context.

    Returns:
        A template store configured from the email settings.
    """
    return TemplateStore(ctx.settings.email)


def slug(name: str, taken: set[str]) -> str:
    """Build a unique template id from a template name.

    The id is lowercase, hyphenated, at most 34 characters before any numeric suffix and
    at least 3 characters long.

    Args:
        name: The display name of the template.
        taken: Ids that are already in use.

    Returns:
        An id that is not in the taken set.
    """
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:34] or "template"
    base = base if len(base) >= 3 else f"{base}-draft"
    candidate, number = base, 2
    while candidate in taken:
        candidate = f"{base}-{number}"
        number += 1
    return candidate


def report_of(job: Job | None) -> PersonReport | SkillSearchReport | None:
    """Return the report of a job only when the job is done.

    Args:
        job: The job, or None.

    Returns:
        The finished report, or None.
    """
    return job.report if job is not None and job.state is JobState.DONE else None


def role_title(report: PersonReport | SkillSearchReport | None) -> str | None:
    """Return the role title to use in drafts for a report.

    Args:
        report: The finished report, or None.

    Returns:
        The focus or the joined roles for a person report, the skill for a skill search
        report, or None when there is no usable title.
    """
    if isinstance(report, PersonReport):
        return report.focus or ", ".join(report.identity.roles) or None
    return report.skill if isinstance(report, SkillSearchReport) else None


def candidate_names(
    category: DraftCategory,
    report: PersonReport | SkillSearchReport | None,
    typed: list[str],
    tuning: EmailTuning,
) -> list[str]:
    """Choose the candidate names a set of drafts is written for.

    Typed names win, then names from the report, then a generic placeholder.

    Args:
        category: The template category; only batch templates use several names.
        report: The finished report, or None.
        typed: Names typed by the user.
        tuning: Email limits, including the maximum batch size.

    Returns:
        The names to render drafts for.
    """
    if typed:
        return typed[: tuning.max_batch] if category is DraftCategory.BATCH else typed[:1]
    if isinstance(report, PersonReport):
        return [report.identity.name]
    if isinstance(report, SkillSearchReport):
        names = [item.name for item in report.candidates]
        return names[: tuning.max_batch] if category is DraftCategory.BATCH else names[:1]
    return ["Candidate"]


def make_drafts(
    template: EmailTemplate,
    ctx: WebContext,
    profile: Profile,
    job: Job | None,
    typed: list[str],
) -> list[Draft]:
    """Render drafts from a template, one per candidate.

    Drafts are text only and nothing is sent. Team templates produce a single draft.

    Args:
        template: The template to render.
        ctx: The web context.
        profile: Sender and company names set by the user.
        job: The finished job to take report data from, or None.
        typed: Candidate names typed by the user.

    Returns:
        The rendered drafts.
    """
    tuning = ctx.settings.email
    report = report_of(job)
    company = report.company if report is not None else None
    names = candidate_names(template.category, report, typed, tuning)
    if template.category is DraftCategory.TEAM:
        names = names[:1]
    drafts = []
    for position, name in enumerate(names, 1):
        context = build_context(
            template.category,
            name=name,
            role_title=role_title(report),
            company=company,
            company_name=profile.company_name,
            sender_name=profile.sender_name,
            tuning=tuning,
            report=report,
            index=position,
            total=len(names),
        )
        drafts.append(render_draft(template, context, tuning, recipient=name))
    return drafts


def job_drafts(
    ctx: WebContext, profile: Profile, job: Job
) -> list[tuple[EmailTemplate, list[Draft]]]:
    """Render the default templates for a finished job.

    Args:
        ctx: The web context.
        profile: Sender and company names set by the user.
        job: The job to render drafts for.

    Returns:
        Pairs of template and drafts, empty when the job has no finished report.
    """
    report = report_of(job)
    if report is None:
        return []
    store = store_of(ctx)
    kind = "person" if isinstance(report, PersonReport) else "skill"
    shown = []
    for identifier, _ in DEFAULT_TEMPLATES[kind]:
        template = store.get(identifier)
        if template is not None:
            shown.append((template, make_drafts(template, ctx, profile, job, [])))
    return shown


def _names(raw: Any) -> list[str]:
    """Clean a list of candidate names taken from untrusted input.

    Args:
        raw: The decoded JSON value expected to be a list of strings.

    Returns:
        Non-empty names with whitespace collapsed, each cut to the maximum length. The
        result is empty when the value is not a list.
    """
    if not isinstance(raw, list):
        return []
    return [
        " ".join(item.split())[:MAX_NAME] for item in raw if isinstance(item, str) and item.strip()
    ]


def _draft_template(body: dict[str, Any]) -> EmailTemplate:
    """Build an unsaved template for previewing from a request body.

    Args:
        body: The decoded JSON body with category, purpose, subject and body.

    Returns:
        A temporary template that is never stored.

    Raises:
        InvalidRequest: when the category or purpose is not valid.
    """
    try:
        return EmailTemplate(
            id="preview-draft",
            name="Preview",
            category=DraftCategory(body.get("category", "")),
            purpose=DraftPurpose(body.get("purpose", "")),
            subject=str(body.get("subject", "")),
            body=str(body.get("body", "")),
        )
    except ValueError as error:
        raise InvalidRequest("Choose a valid category and purpose.") from error


def editor_values(template: EmailTemplate | None) -> dict[str, str]:
    """Return the form values for the template editor.

    Built-in templates cannot be edited, so they are offered as a copy without an id.

    Args:
        template: The template being edited, or None for a new one.

    Returns:
        A dictionary of field names to form values.
    """
    if template is None:
        return dict(NEW_TEMPLATE)
    return {
        "template_id": "" if template.builtin else template.id,
        "name": f"{template.name} (copy)" if template.builtin else template.name,
        "category": template.category.value,
        "purpose": template.purpose.value,
        "subject": template.subject,
        "body": template.body,
    }


def example_subject(template: EmailTemplate, tuning: EmailTuning) -> str:
    """Render the subject of a template with sample data.

    Args:
        template: The template to render.
        tuning: Email limits.

    Returns:
        The example subject, or an empty string when the template cannot be rendered.
    """
    try:
        return render_draft(template, sample_context(template.category, tuning), tuning).subject
    except InvalidRequest:
        return ""


def emails_context(
    request: Request, values: dict[str, str], errors: list[str], saved: bool
) -> dict[str, Any]:
    """Build the template context for the emails page.

    Templates can be filtered by the category query parameter. Finished searches are
    only offered when the user may view reports.

    Args:
        request: The incoming request.
        values: Current values of the editor form.
        errors: Validation messages to show.
        saved: Whether a template was just saved.

    Returns:
        The variables passed to the emails page template.
    """
    ctx = context_of(request)
    templates = store_of(ctx).all()
    category = request.query_params.get("category", "")
    finished = [
        job
        for job in ctx.jobs.for_owner(WEB_OWNER)
        if job.state is JobState.DONE and request.state.principal.can("reports.view")
    ]
    return {
        "active": "emails",
        "templates": [
            item for item in templates if not category or item.category.value == category
        ],
        "all_templates": templates,
        "examples": {item.id: example_subject(item, ctx.settings.email) for item in templates},
        "categories": list(DraftCategory),
        "purpose_options": [(item.value, item.value.capitalize()) for item in DraftPurpose],
        "category": category,
        "values": values,
        "editing": bool(values["template_id"]),
        "job_options": [("", "No search selected")] + [(job.id, job.title) for job in finished],
        "selected_job": request.query_params.get("job", ""),
        "saved": saved,
        "errors": errors,
        "max_batch": ctx.settings.email.max_batch,
    }


@requires(Permission.EMAILS_VIEW)
async def emails_page(request: Request) -> Response:
    """Render the emails page with the template list and editor.

    Args:
        request: The incoming request; the edit query parameter selects a template.

    Returns:
        The HTML page.
    """
    store = store_of(context_of(request))
    template = store.get(request.query_params.get("edit", ""))
    saved = request.query_params.get("saved") == "1"
    context = emails_context(request, editor_values(template), [], saved)
    return render(
        request, "emails.html", mailed=request.query_params.get("mailed") == "1", **context
    )


@requires(Permission.EMAILS_VIEW)
@json_errors
async def emails_preview(request: Request) -> Response:
    """Render a live preview of an unsaved template as JSON.

    The request must carry the CSRF header of the session.

    Args:
        request: The incoming request with a JSON object body.

    Returns:
        A JSON response with the rendered drafts.

    Raises:
        InvalidRequest: when the body is not a JSON object or the template is invalid.
    """
    session = request.state.session
    ctx = protected_header(request, session)
    try:
        body = json.loads((await request.body()).decode("utf-8"))
    except ValueError as error:
        raise InvalidRequest("The preview request is not valid JSON.") from error
    if not isinstance(body, dict):
        raise InvalidRequest("The preview request must be an object.")
    template = _draft_template(body)
    validate_template(template, ctx.settings.email)
    job_id = body.get("job")
    allowed = request.state.principal.can("reports.view")
    job = (
        # A job is only looked up when the user may view reports, so a preview cannot leak report
        # data.
        ctx.jobs.get(job_id, WEB_OWNER) if allowed and isinstance(job_id, str) and job_id else None
    )
    drafts = make_drafts(template, ctx, load_profile(ctx.store), job, _names(body.get("names")))
    return JSONResponse({"drafts": [draft.model_dump() for draft in drafts]})


@requires(Permission.EMAILS_EDIT)
async def emails_save(request: Request) -> Response:
    """Save a template from the submitted editor form.

    Built-in templates are never overwritten; saving one creates a new template.

    Args:
        request: The incoming request with a CSRF protected form.

    Returns:
        A redirect to the editor on success, or the page with errors and status 400.
    """
    ctx = context_of(request)
    form = await protected_form(request, request.state.session)
    tuning = ctx.settings.email
    limits = {
        "template_id": 40,
        "name": MAX_NAME,
        "category": 20,
        "purpose": 20,
        "subject": tuning.max_subject_chars + 50,
        "body": tuning.max_body_chars + 50,
    }
    try:
        values = {key: text(form, key, limit) for key, limit in limits.items()}
    finally:
        await form.close()
    store = store_of(ctx)
    errors: list[str] = []
    saved = None
    try:
        existing = store.get(values["template_id"]) if values["template_id"] else None
        identifier = (
            existing.id
            # Built-in templates are never overwritten; saving one creates a new template with a
            # fresh id.
            if existing is not None and not existing.builtin
            else slug(values["name"], {item.id for item in store.all()})
        )
        saved = store.save(
            EmailTemplate(
                id=identifier,
                name=values["name"],
                category=DraftCategory(values["category"]),
                purpose=DraftPurpose(values["purpose"]),
                subject=values["subject"],
                body=values["body"],
            )
        )
    except InvalidRequest as error:
        errors.append(str(error))
    except ValueError:
        errors.append("Give the template a name and choose a category and a purpose.")
    if saved is None:
        context = emails_context(request, values, errors, False)
        return render(request, "emails.html", 400, mailed=False, **context)
    return RedirectResponse(f"/emails?edit={saved.id}&saved=1", status_code=303)


@requires(Permission.EMAILS_EDIT)
async def emails_delete(request: Request) -> Response:
    """Delete a custom email template.

    Args:
        request: The incoming request with a CSRF protected form.

    Returns:
        A redirect to the emails page.

    Raises:
        HTTPException: with status 404 when the template is unknown or built in.
    """
    ctx = context_of(request)
    form = await protected_form(request, request.state.session)
    await form.close()
    if not store_of(ctx).delete(request.path_params["template_id"]):
        raise HTTPException(404)
    return RedirectResponse("/emails", status_code=303)
