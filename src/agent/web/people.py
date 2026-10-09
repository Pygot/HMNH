# src/agent/web/people.py
from agent.web.common import (
    audit,
    items,
    number,
    protected_form,
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
from agent.candidates import (
    Candidate,
    HIRE,
)
from agent.web.context import (
    context_of,
    WebContext,
)
from starlette.exceptions import HTTPException
from starlette.datastructures import FormData
from starlette.requests import Request
from agent.errors import AgentError
from agent.web.format import stamp

import logging
import time

logger = logging.getLogger(__name__)
MODULE = "candidates_enabled"
SAVED = "Saved."
NOTES = {
    "saved": SAVED,
    "added": "The candidate was added.",
    "contact": "The contact detail was saved.",
    "removed": "Removed.",
    "hired": "The hire was recorded and the contact details were saved.",
}


def candidate_or_404(request: Request) -> Candidate:
    """Return the candidate named by the request path.

    Args:
        request: The request, whose path has a candidate_id parameter.

    Returns:
        The candidate.

    Raises:
        HTTPException: with status 404 when the candidate does not exist.
    """
    found = context_of(request).candidates.get(request.path_params["candidate_id"])
    if found is None:
        raise HTTPException(404)
    return found


def done(candidate_id: str, key: str, anchor: str = "") -> Response:
    """Build the redirect to a candidate page that shows a status notice.

    Args:
        candidate_id: The candidate to show.
        key: The key of the notice to display, one of the known notice keys.
        anchor: Optional page fragment such as #links, including the hash.

    Returns:
        A 303 redirect response.
    """
    return RedirectResponse(f"/candidates/{candidate_id}?done={key}{anchor}", status_code=303)


def record_research(ctx: WebContext, job: Job) -> None:
    """Save a finished research job's report as candidates when allowed.

    Nothing happens unless the job is done, has a report, and the policy enables both
    candidates and automatic recording. Recording failures are logged by error type
    only and never raised.

    Args:
        ctx: The web application context.
        job: The research job that just finished.
    """
    if job.report is None or job.state is not JobState.DONE:
        return
    policy = ctx.policy.get()
    if not (policy.candidates_enabled and policy.candidates_auto_record):
        return
    try:
        ctx.candidates.record(job.report, job.id)
    except AgentError as error:
        logger.warning("a candidate could not be recorded: %s", type(error).__name__)


@requires("candidates.view", MODULE)
async def candidates_page(request: Request) -> Response:
    """Show the filtered and paged list of candidates.

    Unknown status and sort values are ignored, the search text is length limited, and
    the page number falls back to 1 unless it is a sensible positive integer.

    Args:
        request: The request, with optional q, status, sort, hired, min, and page
            query parameters.

    Returns:
        The rendered candidates page.
    """
    ctx = context_of(request)
    tuning = ctx.settings.candidates
    params = request.query_params
    limit = ctx.web.max_field_length
    query = params.get("q", "")[:limit].strip()
    status = params.get("status", "")
    status = status if status in tuning.statuses else ""
    sort = params.get("sort", "updated")
    sort = sort if sort in ("updated", "name", "rating", "created") else "updated"
    hired = {"yes": True, "no": False}.get(params.get("hired", ""))
    errors: list[str] = []
    minimum = number(params.get("min", "")[:6], float, "Minimum rating", errors)
    raw_page = params.get("page", "1")
    page_number = int(raw_page) if raw_page.isdigit() and 0 < int(raw_page) < 100_000 else 1
    found = ctx.candidates.page(
        query=query,
        status=status,
        minimum=minimum,
        hired=hired,
        sort=sort,
        offset=(page_number - 1) * tuning.page_size,
    )
    return render(
        request,
        "candidates.html",
        active="candidates",
        found=found,
        page_number=page_number,
        pages=max(1, -(-found.total // found.size)),
        filters={
            "q": query,
            "status": status,
            "sort": sort,
            "hired": params.get("hired", ""),
            "min": params.get("min", "")[:6],
        },
        statuses=[("", "Any status"), *((value, value.capitalize()) for value in tuning.statuses)],
        sorts=[
            ("updated", "Recently active"),
            ("name", "Name"),
            ("rating", "Best rated"),
            ("created", "Newest"),
        ],
        hired_options=[("", "Hired or not"), ("yes", "Hired before"), ("no", "Never hired")],
        errors=errors,
        counts=ctx.candidates.stats(),
    )


@requires("candidates.edit", MODULE)
async def candidate_new(request: Request) -> Response:
    """Show the form for adding a candidate.

    Args:
        request: The request.

    Returns:
        The rendered empty form.
    """
    ctx = context_of(request)
    return render(
        request,
        "candidate_new.html",
        active="candidates",
        statuses=[(value, value.capitalize()) for value in ctx.settings.candidates.statuses],
        errors=[],
        values={},
    )


def candidate_fields(ctx: WebContext, form: FormData) -> dict[str, str]:
    """Read the candidate fields from a submitted form.

    Each value is stripped and cut to the configured length for its field.

    Args:
        ctx: The web application context.
        form: The submitted form.

    Returns:
        A mapping of field name to cleaned text.
    """
    tuning = ctx.settings.candidates
    return {
        "name": text(form, "name", tuning.max_name_chars),
        "headline": text(form, "headline", tuning.max_text_chars),
        "location": text(form, "location", tuning.max_text_chars),
        "employer": text(form, "employer", tuning.max_text_chars),
        "skills": text(form, "skills", tuning.max_text_chars * 3),
        "tags": text(form, "tags", tuning.max_text_chars),
        "status": text(form, "status", 20),
        "notes": text(form, "notes", tuning.max_notes_chars),
    }


@requires("candidates.edit", MODULE)
async def candidate_create(request: Request) -> Response:
    """Create a candidate from the submitted form.

    The form is checked for a valid CSRF token. On a validation error the form is
    shown again with status 400. A successful creation is written to the audit log.

    Args:
        request: The form post request.

    Returns:
        A redirect to the new candidate, or the form with an error.
    """
    ctx = context_of(request)
    form = await protected_form(request, request.state.session)
    try:
        values = candidate_fields(ctx, form)
        link = text(form, "link", ctx.settings.candidates.max_text_chars * 3)
    finally:
        await form.close()
    try:
        created = ctx.candidates.create(
            values["name"],
            headline=values["headline"],
            location=values["location"],
            employer=values["employer"],
            skills=items(values["skills"]),
            tags=items(values["tags"]),
            status=values["status"] or None,
            notes=values["notes"],
            links=[link] if link else [],
            actor=request.state.principal.name,
        )
    except AgentError as error:
        return render(
            request,
            "candidate_new.html",
            400,
            active="candidates",
            statuses=[(value, value.capitalize()) for value in ctx.settings.candidates.statuses],
            errors=[str(error)],
            values=values,
        )
    audit(request, "candidate.create", target=created.id)
    return done(created.id, "added")


def detail_view(
    request: Request,
    candidate: Candidate,
    status: int = 200,
    errors: list[str] | None = None,
) -> Response:
    """Render the detail page of a candidate.

    Contact details are only loaded when the signed-in user may view contacts.

    Args:
        request: The request.
        candidate: The candidate to show.
        status: The HTTP status of the response.
        errors: Error messages to show on the page.

    Returns:
        The rendered detail page.
    """
    ctx = context_of(request)
    principal = request.state.principal
    tuning = ctx.settings.candidates
    note = NOTES.get(request.query_params.get("done", ""))
    return render(
        request,
        "candidate.html",
        status,
        active="candidates",
        candidate=candidate,
        links=ctx.candidates.links(candidate.id),
        contacts=ctx.candidates.contacts(candidate.id) if principal.can("contacts.view") else [],
        hires=ctx.candidates.hires(candidate.id),
        events=ctx.candidates.events(candidate.id),
        statuses=[(value, value.capitalize()) for value in tuning.statuses],
        outcomes=[(value, value.capitalize()) for value in tuning.hire_outcomes],
        kinds=[(value, value.capitalize()) for value in tuning.contact_kinds],
        grades=[
            ("", "No rating"),
            *((str(n), str(n)) for n in range(tuning.rating_low, tuning.rating_high + 1)),
        ],
        errors=errors or [],
        notes=[note] if note else [],
        grade_range=(tuning.rating_low, tuning.rating_high),
        mail_ready=ctx.mailer.configured() and ctx.policy.get().email_sending_enabled,
    )


@requires("candidates.view", MODULE)
async def candidate_page(request: Request) -> Response:
    """Show the detail page of one candidate.

    Args:
        request: The request.

    Returns:
        The rendered detail page.

    Raises:
        HTTPException: with status 404 when the candidate does not exist.
    """
    return detail_view(request, candidate_or_404(request))


@requires("candidates.edit", MODULE)
async def candidate_update(request: Request) -> Response:
    """Update a candidate from the submitted form.

    On a validation error the detail page is shown again with status 400. A
    successful update is written to the audit log.

    Args:
        request: The form post request.

    Returns:
        A redirect to the candidate, or the detail page with an error.
    """
    ctx = context_of(request)
    candidate = candidate_or_404(request)
    form = await protected_form(request, request.state.session)
    try:
        values = candidate_fields(ctx, form)
    finally:
        await form.close()
    try:
        ctx.candidates.update(
            candidate.id,
            name=values["name"],
            headline=values["headline"],
            location=values["location"],
            employer=values["employer"],
            skills=items(values["skills"]),
            tags=items(values["tags"]),
            status=values["status"] or None,
            notes=values["notes"],
            actor=request.state.principal.name,
        )
    except AgentError as error:
        return detail_view(request, candidate, 400, [str(error)])
    audit(request, "candidate.update", target=candidate.id)
    return done(candidate.id, "saved")


@requires("candidates.edit", MODULE)
async def candidate_note(request: Request) -> Response:
    """Add a note to a candidate's timeline from the submitted form.

    An empty note is ignored.

    Args:
        request: The form post request.

    Returns:
        A redirect to the timeline of the candidate.
    """
    ctx = context_of(request)
    candidate = candidate_or_404(request)
    form = await protected_form(request, request.state.session)
    try:
        summary = text(form, "summary", ctx.settings.candidates.max_text_chars)
    finally:
        await form.close()
    if summary:
        ctx.candidates.note(candidate.id, summary, request.state.principal.name)
    return done(candidate.id, "saved", "#timeline")


@requires("candidates.edit", MODULE)
async def link_add(request: Request) -> Response:
    """Add a link to a candidate from the submitted form.

    Args:
        request: The form post request.

    Returns:
        A redirect to the links section, or the detail page with an error.
    """
    ctx = context_of(request)
    candidate = candidate_or_404(request)
    form = await protected_form(request, request.state.session)
    try:
        url = text(form, "url", ctx.settings.candidates.max_text_chars * 3)
        label = text(form, "label", ctx.settings.candidates.max_text_chars)
    finally:
        await form.close()
    try:
        ctx.candidates.add_link(candidate.id, url, label)
    except AgentError as error:
        return detail_view(request, candidate, 400, [str(error)])
    return done(candidate.id, "saved", "#links")


@requires("candidates.edit", MODULE)
async def link_remove(request: Request) -> Response:
    """Remove a link from a candidate using the submitted link key.

    Args:
        request: The form post request.

    Returns:
        A redirect to the links section.
    """
    ctx = context_of(request)
    candidate = candidate_or_404(request)
    form = await protected_form(request, request.state.session)
    try:
        key = text(form, "key", ctx.settings.candidates.max_text_chars * 3)
    finally:
        await form.close()
    ctx.candidates.remove_link(candidate.id, key)
    return done(candidate.id, "removed", "#links")


@requires("contacts.edit", MODULE)
async def contact_add(request: Request) -> Response:
    """Add a contact detail to a candidate from the submitted form.

    The action is written to the audit log with the contact kind but not its value.

    Args:
        request: The form post request.

    Returns:
        A redirect to the contacts section, or the detail page with an error.
    """
    ctx = context_of(request)
    candidate = candidate_or_404(request)
    form = await protected_form(request, request.state.session)
    try:
        kind = text(form, "kind", 12)
        value = text(form, "value", ctx.settings.candidates.max_text_chars * 2)
        label = text(form, "label", ctx.settings.candidates.max_text_chars)
    finally:
        await form.close()
    try:
        ctx.candidates.add_contact(
            candidate.id, kind, value, label=label, actor=request.state.principal.name
        )
    except AgentError as error:
        return detail_view(request, candidate, 400, [str(error)])
    audit(request, "contact.add", target=candidate.id, detail={"kind": kind})
    return done(candidate.id, "contact", "#contacts")


@requires("contacts.edit", MODULE)
async def contact_remove(request: Request) -> Response:
    """Remove a contact detail from a candidate.

    The removal is written to the audit log.

    Args:
        request: The form post request, with a contact_id path parameter.

    Returns:
        A redirect to the contacts section.
    """
    ctx = context_of(request)
    candidate = candidate_or_404(request)
    form = await protected_form(request, request.state.session)
    await form.close()
    ctx.candidates.remove_contact(
        candidate.id, request.path_params["contact_id"], request.state.principal.name
    )
    audit(request, "contact.remove", target=candidate.id)
    return done(candidate.id, "removed", "#contacts")


@requires("hires.manage", MODULE)
async def hire_create(request: Request) -> Response:
    """Record a hire for a candidate from the submitted form.

    Email and phone are saved as contact details only when the user may edit contacts.
    A linked job is kept only when it exists for the web owner. The hire is written to
    the audit log.

    Args:
        request: The form post request.

    Returns:
        A redirect to the hires section, or the detail page with an error.
    """
    ctx = context_of(request)
    candidate = candidate_or_404(request)
    tuning = ctx.settings.candidates
    form = await protected_form(request, request.state.session)
    try:
        title = text(form, "role_title", tuning.max_text_chars)
        department = text(form, "department", tuning.max_text_chars)
        started = text(form, "started", 12)
        email = text(form, "email", ctx.settings.mail.max_address_chars)
        phone = text(form, "phone", 40)
        job_id = text(form, "job_id", 40)
    finally:
        await form.close()
    # The submitted job id is only trusted when that job exists for the web owner, so a forged id is
    # replaced by an empty one.
    known = ctx.jobs.get(job_id, WEB_OWNER) if job_id else None
    try:
        ctx.candidates.hire(
            candidate.id,
            title,
            department=department,
            started=started,
            contacts=(
                [("email", email), ("phone", phone)]
                # Contact details typed into the hire form are dropped unless the user may edit
                # contacts.
                if request.state.principal.can("contacts.edit")
                else []
            ),
            job_id=known.id if known else "",
            actor=request.state.principal.name,
        )
    except AgentError as error:
        return detail_view(request, candidate, 400, [str(error)])
    audit(request, "hire.create", target=candidate.id, detail={"source": HIRE})
    return done(candidate.id, "hired", "#hires")


@requires("hires.manage", MODULE)
async def hire_update(request: Request) -> Response:
    """Update a hire from the submitted form.

    An empty rating clears the rating and an empty outcome keeps the current one. A
    rating that is not a whole number gives an error with status 400.

    Args:
        request: The form post request, with a hire_id path parameter.

    Returns:
        A redirect to the hires section, or the detail page with an error.
    """
    ctx = context_of(request)
    candidate = candidate_or_404(request)
    tuning = ctx.settings.candidates
    form = await protected_form(request, request.state.session)
    try:
        values = {
            "role_title": text(form, "role_title", tuning.max_text_chars),
            "department": text(form, "department", tuning.max_text_chars),
            "started": text(form, "started", 12),
            "ended": text(form, "ended", 12),
            "outcome": text(form, "outcome", 12),
            "review": text(form, "review", tuning.max_notes_chars),
            "rating": text(form, "rating", 3),
        }
    finally:
        await form.close()
    errors: list[str] = []
    grade = number(values["rating"], int, "The rating", errors) if values["rating"] else None
    outcome = values["outcome"] or None
    if errors:
        return detail_view(request, candidate, 400, errors)
    try:
        ctx.candidates.update_hire(
            candidate.id,
            request.path_params["hire_id"],
            role_title=values["role_title"],
            department=values["department"],
            started=values["started"],
            ended=values["ended"],
            outcome=outcome,
            rating=grade,
            clear_rating=not values["rating"],
            review=values["review"],
            actor=request.state.principal.name,
        )
    except AgentError as error:
        return detail_view(request, candidate, 400, [str(error)])
    audit(request, "hire.update", target=candidate.id)
    return done(candidate.id, "saved", "#hires")


@requires("hires.manage", MODULE)
async def hire_delete(request: Request) -> Response:
    """Delete a hire record from a candidate.

    The deletion is written to the audit log.

    Args:
        request: The form post request, with a hire_id path parameter.

    Returns:
        A redirect to the hires section.
    """
    ctx = context_of(request)
    candidate = candidate_or_404(request)
    form = await protected_form(request, request.state.session)
    await form.close()
    ctx.candidates.delete_hire(candidate.id, request.path_params["hire_id"])
    audit(request, "hire.delete", target=candidate.id)
    return done(candidate.id, "removed", "#hires")


@requires("data.delete", MODULE)
async def candidate_delete(request: Request) -> Response:
    """Delete a candidate.

    The deletion is written to the audit log together with the candidate's name.

    Args:
        request: The form post request.

    Returns:
        A redirect to the candidate list.
    """
    ctx = context_of(request)
    candidate = candidate_or_404(request)
    form = await protected_form(request, request.state.session)
    await form.close()
    ctx.candidates.delete(candidate.id)
    audit(request, "candidate.delete", target=candidate.id, detail={"name": candidate.name})
    return RedirectResponse("/candidates", status_code=303)


@requires("data.export", MODULE)
async def candidate_export(request: Request) -> Response:
    """Download one candidate with links, hires, and events as a JSON file.

    Contact details are included only when the user may view contacts. The export is
    written to the audit log.

    Args:
        request: The request.

    Returns:
        A JSON response sent as an attachment.

    Raises:
        HTTPException: with status 404 when the candidate does not exist or the user
            may not view candidates.
    """
    ctx = context_of(request)
    candidate = candidate_or_404(request)
    principal = request.state.principal
    # Export permission alone is not enough; users who may not view candidates get a 404 so the
    # candidate's existence is not revealed.
    if not principal.can("candidates.view"):
        raise HTTPException(404)
    payload = {
        "candidate": candidate.model_dump(mode="json"),
        "links": [link.model_dump() for link in ctx.candidates.links(candidate.id)],
        "contacts": [
            item.model_dump(mode="json")
            for item in (
                ctx.candidates.contacts(candidate.id) if principal.can("contacts.view") else []
            )
        ],
        "hires": [item.model_dump(mode="json") for item in ctx.candidates.hires(candidate.id)],
        "events": [item.model_dump(mode="json") for item in ctx.candidates.events(candidate.id)],
        "exported": stamp(time.time(), ctx.web.time_format),
    }
    audit(request, "candidate.export", target=candidate.id)
    return JSONResponse(
        payload,
        headers={"Content-Disposition": f'attachment; filename="candidate-{candidate.id}.json"'},
    )


@requires("candidates.edit", MODULE)
async def job_candidate(request: Request) -> Response:
    """Save the people in a finished job's report as candidates.

    The form field hire set to yes sends the user to the hires section when exactly one
    candidate was saved.

    Args:
        request: The form post request, with a job_id path parameter.

    Returns:
        A redirect to the first saved candidate.

    Raises:
        HTTPException: with status 404 when the job is unknown, has no report, the user
            may not view reports, or no candidate could be saved.
    """
    ctx = context_of(request)
    job = ctx.jobs.get(request.path_params["job_id"], WEB_OWNER)
    if job is None or job.report is None or not request.state.principal.can("reports.view"):
        raise HTTPException(404)
    form = await protected_form(request, request.state.session)
    try:
        hire = text(form, "hire", 4) == "yes"
    finally:
        await form.close()
    found = ctx.candidates.record(job.report, job.id, request.state.principal.name)
    if not found:
        raise HTTPException(404)
    audit(request, "candidate.from_report", target=found[0].id)
    return done(found[0].id, "added", "#hires" if hire and len(found) == 1 else "")
