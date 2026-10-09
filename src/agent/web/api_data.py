# src/agent/web/api_data.py
from agent.web.api_core import (
    api,
    error_response,
    json_body,
    page_of,
    paged,
)
from agent.web.dashboard import (
    allowed,
    build_report,
    BY_NAME,
    visible,
    window_of,
)
from agent.web.charts import (
    plot_svg,
    RowPlot,
    rows_svg,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
)
from agent.web.context import (
    context_of,
    WebContext,
)
from starlette.responses import (
    JSONResponse,
    Response,
)
from agent.models import ResearchOptions
from agent.errors import InvalidRequest
from starlette.requests import Request
from agent.chat import load_profile
from typing import Any

import asyncio


# Request body for creating a candidate through the API.
class ApiCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=200)
    headline: str = Field(default="", max_length=300)
    location: str = Field(default="", max_length=300)
    employer: str = Field(default="", max_length=300)
    skills: list[str] = Field(default_factory=list, max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=50)
    status: str | None = Field(default=None, max_length=20)
    notes: str = Field(default="", max_length=50_000)
    links: list[str] = Field(default_factory=list, max_length=100)


# Request body for partially updating a candidate.
class ApiCandidatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)
    headline: str | None = Field(default=None, max_length=300)
    location: str | None = Field(default=None, max_length=300)
    employer: str | None = Field(default=None, max_length=300)
    skills: list[str] | None = Field(default=None, max_length=100)
    tags: list[str] | None = Field(default=None, max_length=50)
    status: str | None = Field(default=None, max_length=20)
    notes: str | None = Field(default=None, max_length=50_000)


# Request body for recording a hire for a candidate.
class ApiHire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role_title: str = Field(min_length=1, max_length=300)
    department: str = Field(default="", max_length=300)
    started: str = Field(default="", max_length=10)
    email: str = Field(default="", max_length=254)
    phone: str = Field(default="", max_length=40)


# Request body for adding one contact detail to a candidate.
class ApiContact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(max_length=12)
    value: str = Field(max_length=600)
    label: str = Field(default="", max_length=300)


# Request body for starting a recurring skill search routine.
class ApiRoutine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: str = Field(min_length=1, max_length=120)
    location: str = Field(min_length=1, max_length=120)
    name: str = Field(default="", max_length=200)
    min_rating: float | None = Field(default=None, ge=0, le=5)
    need_count: int = Field(default=1, ge=1, le=10)
    size: int | None = Field(default=None, ge=1, le=10)
    interval_hours: int | None = Field(default=None, ge=1)
    max_runs: int | None = Field(default=None, ge=1)
    notify_emails: list[str] = Field(default_factory=list, max_length=50)
    use_standing_requirements: bool = False
    lawful_purpose: bool


# Request body for adding one revenue or expense entry.
class ApiEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: str = Field(max_length=10)
    kind: str = Field(max_length=10)
    amount: str = Field(max_length=24)
    category: str = Field(default="", max_length=80)
    note: str = Field(default="", max_length=1000)


def request_models() -> list[type[BaseModel]]:
    """Return the request body models that the API documentation describes.

    Returns:
        The pydantic model classes used as API request bodies.
    """
    return [ApiCandidate, ApiCandidatePatch, ApiHire, ApiContact, ApiRoutine, ApiEntry]


def not_found(name: str = "item") -> Response:
    """Build a 404 JSON error response for a missing item.

    Args:
        name: Kind of item to name in the message.

    Returns:
        A JSON error response with status 404 and code not_found.
    """
    return error_response(404, "not_found", f"No such {name}.")


def candidate_detail(ctx: WebContext, request: Request, identifier: str) -> dict[str, Any] | None:
    """Collect one candidate with links, hires and history for the API.

    Contact details are included only when the caller holds the contacts.view
    permission, otherwise that key is None.

    Args:
        ctx: Shared web context.
        request: The incoming request, carrying the authenticated principal.
        identifier: Candidate id to look up.

    Returns:
        A JSON-ready dict, or None when the candidate does not exist.
    """
    principal = request.state.principal
    found = ctx.candidates.get(identifier)
    if found is None:
        return None
    return {
        "candidate": found.model_dump(mode="json"),
        "links": [item.model_dump(mode="json") for item in ctx.candidates.links(identifier)],
        "contacts": (
            [item.model_dump(mode="json") for item in ctx.candidates.contacts(identifier)]
            if principal.can("contacts.view")
            else None
        ),
        "hires": [item.model_dump(mode="json") for item in ctx.candidates.hires(identifier)],
        "events": [item.model_dump(mode="json") for item in ctx.candidates.events(identifier)],
    }


@api("GET", "/me", "Who this credential belongs to and what it may do.", "api.use", tag="Account")
async def me(request: Request) -> Response:
    """Return the identity and permissions of the calling credential.

    Args:
        request: The incoming request, carrying the authenticated principal.

    Returns:
        A JSON response with user, role, key id and sorted permissions.
    """
    principal = request.state.principal
    return JSONResponse(
        {
            "user_id": principal.user_id,
            "name": principal.name,
            "role": principal.role,
            "source": principal.source,
            "key_id": principal.key_id,
            "permissions": sorted(principal.permissions),
        }
    )


@api("GET", "/stats", "The statistics categories you may read.", "stats.view", tag="Statistics")
async def stats_index(request: Request) -> Response:
    """List the statistics categories the caller may read.

    Args:
        request: The incoming request.

    Returns:
        A JSON response with name, label, window and link for each category.
    """
    ctx = context_of(request)
    topics = visible(request.state.principal, ctx.policy.get())
    return JSONResponse(
        {
            "categories": [
                {
                    "name": topic.name,
                    "label": topic.label,
                    "window": topic.window,
                    "href": f"/api/v1/stats/{topic.name}",
                }
                for topic in topics
            ]
        }
    )


def topic_or_none(ctx: WebContext, request: Request) -> Any:
    """Find the statistics topic named in the path if the caller may see it.

    Args:
        ctx: Shared web context.
        request: The incoming request whose path holds the category name.

    Returns:
        The matching topic, or None when it is unknown or not permitted.
    """
    topic = BY_NAME.get(request.path_params["category"])
    if topic is None or not allowed(topic, request.state.principal, ctx.policy.get()):
        return None
    return topic


@api(
    "GET",
    "/stats/{category}",
    "Numbers and chart descriptions for one category. Add ?window= for days or months.",
    "stats.view",
    tag="Statistics",
)
async def stats_category(request: Request) -> Response:
    """Return facts, data and chart links for one statistics category.

    The optional window query parameter is applied only to topics that support one.

    Args:
        request: The incoming request.

    Returns:
        A JSON report, or a 404 error when the category is unknown or hidden.
    """
    ctx = context_of(request)
    topic = topic_or_none(ctx, request)
    if topic is None:
        return not_found("category")
    report = build_report(ctx, topic.name, window_of(request) if topic.window else None)
    return JSONResponse(
        {
            "category": topic.name,
            "label": topic.label,
            "facts": [{"label": label, "value": value} for label, value in report.facts],
            "data": report.data,
            "charts": [
                {
                    "index": index,
                    "title": chart.title,
                    "kind": chart.kind,
                    "svg": f"/api/v1/stats/{topic.name}/charts/{index}.svg",
                }
                for index, chart in enumerate(report.charts)
            ],
        }
    )


@api(
    "GET",
    "/stats/{category}/charts/{index}.svg",
    "One chart of a category as an SVG image.",
    "stats.view",
    tag="Statistics",
)
async def stats_chart(request: Request) -> Response:
    """Render one chart of a statistics category as an SVG image.

    Args:
        request: The incoming request whose path holds category and chart index.

    Returns:
        An image/svg+xml response, or a 404 error when the category or the chart
        index does not exist.
    """
    ctx = context_of(request)
    topic = topic_or_none(ctx, request)
    index = request.path_params["index"]
    if topic is None or not index.isdigit():
        return not_found("chart")
    report = build_report(ctx, topic.name, window_of(request) if topic.window else None)
    if int(index) >= len(report.charts):
        return not_found("chart")
    plot = report.charts[int(index)].plot
    tuning = ctx.settings.charts
    body = rows_svg(plot, tuning) if isinstance(plot, RowPlot) else plot_svg(plot, tuning)
    return Response(body, media_type="image/svg+xml")


@api(
    "GET",
    "/candidates",
    "List candidates. Filters: q, status, min, hired (yes or no), sort, page, size.",
    "candidates.view",
    module="candidates_enabled",
    tag="Candidates",
)
async def candidates_list(request: Request) -> Response:
    """List candidates with optional filters and paging.

    An unparsable min value is ignored and a status outside the configured
    statuses is treated as no status filter. The search text is cut to 200
    characters.

    Args:
        request: The incoming request carrying the filter query parameters.

    Returns:
        A JSON page of candidates.
    """
    ctx = context_of(request)
    params = request.query_params
    page, size = page_of(request)
    status = params.get("status", "")
    try:
        minimum = float(params["min"]) if "min" in params else None
    except ValueError:
        minimum = None
    found = ctx.candidates.page(
        query=params.get("q", "")[:200],
        status=status if status in ctx.settings.candidates.statuses else "",
        minimum=minimum,
        hired={"yes": True, "no": False}.get(params.get("hired", "")),
        sort=params.get("sort", "updated"),
        offset=(page - 1) * size,
        size=size,
    )
    items = [item.model_dump(mode="json") for item in found.items]
    return JSONResponse(paged(items, found.total, page, size, "/candidates"))


@api(
    "POST",
    "/candidates",
    "Add a candidate.",
    "candidates.edit",
    body="ApiCandidate",
    module="candidates_enabled",
    tag="Candidates",
)
async def candidates_create(request: Request) -> Response:
    """Create a candidate from the validated request body.

    Args:
        request: The incoming request with a JSON body matching ApiCandidate.

    Returns:
        The created candidate as JSON with status 201.
    """
    ctx = context_of(request)
    body = ApiCandidate.model_validate(await json_body(request))
    created = ctx.candidates.create(
        body.name,
        headline=body.headline,
        location=body.location,
        employer=body.employer,
        skills=body.skills,
        tags=body.tags,
        status=body.status,
        notes=body.notes,
        links=body.links,
        actor=request.state.principal.name,
    )
    return JSONResponse(created.model_dump(mode="json"), status_code=201)


@api(
    "GET",
    "/candidates/{candidate_id}",
    "One candidate with links, hires, history and, if allowed, contacts.",
    "candidates.view",
    module="candidates_enabled",
    tag="Candidates",
)
async def candidates_read(request: Request) -> Response:
    """Return one candidate with its links, hires and history.

    Args:
        request: The incoming request whose path holds the candidate id.

    Returns:
        The candidate detail as JSON, or a 404 error when it does not exist.
    """
    ctx = context_of(request)
    detail = candidate_detail(ctx, request, request.path_params["candidate_id"])
    return not_found("candidate") if detail is None else JSONResponse(detail)


@api(
    "PATCH",
    "/candidates/{candidate_id}",
    "Change a candidate. Only the fields you send change.",
    "candidates.edit",
    body="ApiCandidatePatch",
    module="candidates_enabled",
    tag="Candidates",
)
async def candidates_patch(request: Request) -> Response:
    """Update only the candidate fields that the client sent.

    Args:
        request: The incoming request with a JSON body matching ApiCandidatePatch.

    Returns:
        The updated candidate as JSON, or a 404 error when it does not exist.
    """
    ctx = context_of(request)
    identifier = request.path_params["candidate_id"]
    if ctx.candidates.get(identifier) is None:
        return not_found("candidate")
    body = ApiCandidatePatch.model_validate(await json_body(request))
    changes = {key: getattr(body, key) for key in body.model_fields_set}
    updated = ctx.candidates.update(identifier, actor=request.state.principal.name, **changes)
    return JSONResponse(updated.model_dump(mode="json"))


@api(
    "DELETE",
    "/candidates/{candidate_id}",
    "Erase a candidate with everything recorded about them.",
    "data.delete",
    module="candidates_enabled",
    tag="Candidates",
)
async def candidates_delete(request: Request) -> Response:
    """Erase a candidate and everything recorded about them.

    The deletion is written to the audit log with the calling principal.

    Args:
        request: The incoming request whose path holds the candidate id.

    Returns:
        An empty 204 response, or a 404 error when the candidate does not exist.
    """
    ctx = context_of(request)
    identifier = request.path_params["candidate_id"]
    if not ctx.candidates.delete(identifier):
        return not_found("candidate")
    ctx.accounts.log(
        "candidate.delete", actor=request.state.principal, target=identifier, detail={"via": "api"}
    )
    return Response(status_code=204)


@api(
    "POST",
    "/candidates/{candidate_id}/hires",
    "Record a hire. An email address and phone number are saved as contact details.",
    "hires.manage",
    body="ApiHire",
    module="candidates_enabled",
    tag="Candidates",
)
async def hires_create(request: Request) -> Response:
    """Record a hire for an existing candidate.

    The email address and phone number are saved as contact details only when
    the caller also holds the contacts.edit permission.

    Args:
        request: The incoming request with a JSON body matching ApiHire.

    Returns:
        The created hire as JSON with status 201, or a 404 error when the
        candidate does not exist.
    """
    ctx = context_of(request)
    identifier = request.path_params["candidate_id"]
    if ctx.candidates.get(identifier) is None:
        return not_found("candidate")
    body = ApiHire.model_validate(await json_body(request))
    hire = ctx.candidates.hire(
        identifier,
        body.role_title,
        department=body.department,
        started=body.started,
        contacts=(
            [("email", body.email), ("phone", body.phone)]
            # Contact details are dropped silently without contacts.edit so a hire cannot be used to
            # bypass that permission.
            if request.state.principal.can("contacts.edit")
            else []
        ),
        actor=request.state.principal.name,
    )
    return JSONResponse(hire.model_dump(mode="json"), status_code=201)


@api(
    "POST",
    "/candidates/{candidate_id}/contacts",
    "Add a contact detail to a candidate.",
    "contacts.edit",
    body="ApiContact",
    module="candidates_enabled",
    tag="Candidates",
)
async def contacts_create(request: Request) -> Response:
    """Add a contact detail to an existing candidate.

    Args:
        request: The incoming request with a JSON body matching ApiContact.

    Returns:
        A JSON object with added set to whether a new detail was stored, with
        status 201 if added and 200 otherwise, or a 404 error for an unknown
        candidate.
    """
    ctx = context_of(request)
    identifier = request.path_params["candidate_id"]
    if ctx.candidates.get(identifier) is None:
        return not_found("candidate")
    body = ApiContact.model_validate(await json_body(request))
    added = ctx.candidates.add_contact(
        identifier, body.kind, body.value, label=body.label, actor=request.state.principal.name
    )
    return JSONResponse({"added": added}, status_code=201 if added else 200)


@api(
    "GET", "/routines", "List routines.", "routines.view", module="routines_enabled", tag="Routines"
)
async def routines_list(request: Request) -> Response:
    """List all routines.

    Args:
        request: The incoming request.

    Returns:
        A JSON response holding every routine.
    """
    items = [item.model_dump(mode="json") for item in context_of(request).routines.all()]
    return JSONResponse({"routines": items})


@api(
    "POST",
    "/routines",
    "Start a routine that repeats a skill search until someone perfect is found.",
    "routines.manage",
    body="ApiRoutine",
    module="routines_enabled",
    tag="Routines",
)
async def routines_create(request: Request) -> Response:
    """Start a routine that repeats a skill search.

    The standing requirements from the saved profile are attached only when
    use_standing_requirements is set. The lawful purpose flag is passed on as
    the purpose confirmation.

    Args:
        request: The incoming request with a JSON body matching ApiRoutine.

    Returns:
        The created routine as JSON with status 201.
    """
    ctx = context_of(request)
    body = ApiRoutine.model_validate(await json_body(request))
    profile = load_profile(ctx.store)
    created = ctx.routines.create(
        body.name,
        body.skill,
        body.location,
        size=body.size,
        requirements=profile.requirements if body.use_standing_requirements else [],
        options=ResearchOptions(),
        min_rating=body.min_rating,
        need_count=body.need_count,
        interval_hours=body.interval_hours,
        max_runs=body.max_runs,
        notify_emails=body.notify_emails,
        purpose_confirmed=body.lawful_purpose,
        company=profile.company(),
        actor=request.state.principal.name,
    )
    return JSONResponse(created.model_dump(mode="json"), status_code=201)


@api(
    "GET",
    "/routines/{routine_id}",
    "One routine with its run history.",
    "routines.view",
    module="routines_enabled",
    tag="Routines",
)
async def routines_read(request: Request) -> Response:
    """Return one routine together with its run history.

    Args:
        request: The incoming request whose path holds the routine id.

    Returns:
        The routine and its runs as JSON, or a 404 error when it does not exist.
    """
    ctx = context_of(request)
    found = ctx.routines.get(request.path_params["routine_id"])
    if found is None:
        return not_found("routine")
    return JSONResponse(
        {
            "routine": found.model_dump(mode="json"),
            "runs": [item.model_dump(mode="json") for item in ctx.routines.runs(found.id)],
        }
    )


def routine_action(action: str) -> Any:
    """Build a handler that pauses, resumes or queues a routine.

    Args:
        action: One of pause, resume or run.

    Returns:
        An async request handler for that action.
    """

    async def run(request: Request) -> Response:
        """Apply the chosen action to the routine named in the path.

        Args:
            request: The incoming request whose path holds the routine id.

        Returns:
            The updated routine as JSON, or a 404 error when it does not exist.

        Raises:
            InvalidRequest: when running a routine that is not active.
        """
        ctx = context_of(request)
        identifier = request.path_params["routine_id"]
        found = ctx.routines.get(identifier)
        if found is None:
            return not_found("routine")
        if action == "pause":
            result = ctx.routines.set_status(identifier, "paused")
        elif action == "resume":
            result = ctx.routines.set_status(identifier, "active")
        else:
            if found.status != "active":
                raise InvalidRequest("Resume the routine first.")
            ctx.routines.queue(identifier)
            result = ctx.routines.get(identifier) or found
        return JSONResponse(result.model_dump(mode="json"))

    return run


@api(
    "POST",
    "/routines/{routine_id}/pause",
    "Pause a routine.",
    "routines.manage",
    module="routines_enabled",
    tag="Routines",
)
async def routines_pause(request: Request) -> Response:
    """Pause a routine.

    Args:
        request: The incoming request whose path holds the routine id.

    Returns:
        The updated routine as JSON.
    """
    return await routine_action("pause")(request)


@api(
    "POST",
    "/routines/{routine_id}/resume",
    "Resume a routine.",
    "routines.manage",
    module="routines_enabled",
    tag="Routines",
)
async def routines_resume(request: Request) -> Response:
    """Resume a paused routine.

    Args:
        request: The incoming request whose path holds the routine id.

    Returns:
        The updated routine as JSON.
    """
    return await routine_action("resume")(request)


@api(
    "POST",
    "/routines/{routine_id}/run",
    "Run a routine at the next opportunity.",
    "routines.manage",
    module="routines_enabled",
    tag="Routines",
)
async def routines_run(request: Request) -> Response:
    """Queue an active routine to run at the next opportunity.

    Args:
        request: The incoming request whose path holds the routine id.

    Returns:
        The routine as JSON.
    """
    return await routine_action("run")(request)


@api(
    "DELETE",
    "/routines/{routine_id}",
    "Remove a routine and its history.",
    "routines.manage",
    module="routines_enabled",
    tag="Routines",
)
async def routines_delete(request: Request) -> Response:
    """Delete a routine and its history.

    Args:
        request: The incoming request whose path holds the routine id.

    Returns:
        An empty 204 response, or a 404 error when the routine does not exist.
    """
    if not context_of(request).routines.delete(request.path_params["routine_id"]):
        return not_found("routine")
    return Response(status_code=204)


@api(
    "GET",
    "/finance/entries",
    "List finance entries. Filters: kind, category, month, page, size.",
    "finance.view",
    module="finance_enabled",
    tag="Finance",
)
async def finance_entries(request: Request) -> Response:
    """List finance entries with optional filters and paging.

    Amounts are shown in plain decimal form and the response names the currency.

    Args:
        request: The incoming request carrying kind, category and month filters.

    Returns:
        A JSON page of entries.
    """
    ctx = context_of(request)
    page, size = page_of(request)
    params = request.query_params
    found = ctx.finance.page(
        kind=params.get("kind", ""),
        category_id=params.get("category", "")[:40],
        month=params.get("month", ""),
        offset=(page - 1) * size,
        size=size,
    )
    items = [
        {**item.model_dump(mode="json"), "amount": ctx.finance.plain(item.amount)}
        for item in found.items
    ]
    return JSONResponse(
        {
            **paged(items, found.total, page, size, "/finance/entries"),
            "currency": ctx.finance.currency(),
        }
    )


@api(
    "POST",
    "/finance/entries",
    "Add a revenue or expense entry. The category is created if it does not exist.",
    "finance.edit",
    body="ApiEntry",
    module="finance_enabled",
    tag="Finance",
)
async def finance_create(request: Request) -> Response:
    """Add a revenue or expense entry.

    A named category is created first when it does not exist yet.

    Args:
        request: The incoming request with a JSON body matching ApiEntry.

    Returns:
        The created entry as JSON with status 201.
    """
    ctx = context_of(request)
    body = ApiEntry.model_validate(await json_body(request))
    category = ctx.finance.ensure_category(body.category, body.kind) if body.category else None
    entry = ctx.finance.add_entry(
        body.day,
        body.kind,
        body.amount,
        category_id=category,
        note=body.note,
        actor=request.state.principal.name,
    )
    return JSONResponse(
        {**entry.model_dump(mode="json"), "amount": ctx.finance.plain(entry.amount)},
        status_code=201,
    )


@api(
    "GET",
    "/finance/categories",
    "List finance categories.",
    "finance.view",
    module="finance_enabled",
    tag="Finance",
)
async def finance_categories(request: Request) -> Response:
    """List all finance categories.

    Args:
        request: The incoming request.

    Returns:
        A JSON response holding every category.
    """
    items = [item.model_dump(mode="json") for item in context_of(request).finance.categories()]
    return JSONResponse({"categories": items})


@api(
    "GET",
    "/announcements",
    "Announcements addressed to you.",
    "messages.use",
    module="messaging_enabled",
    tag="Messages",
)
async def announcements(request: Request) -> Response:
    """List the announcements addressed to the calling user.

    Args:
        request: The incoming request carrying the authenticated principal.

    Returns:
        A JSON response holding the announcements.
    """
    ctx = context_of(request)
    items = ctx.messenger.announcements(request.state.principal.user_id)
    return JSONResponse({"announcements": [item.model_dump(mode="json") for item in items]})


@api(
    "GET",
    "/audit",
    "The audit log. Filters: action, actor, outcome, before, size.",
    "audit.view",
    tag="Administration",
)
async def audit_log(request: Request) -> Response:
    """Return recent audit log entries, newest first.

    The before value is a numeric cursor and text filters are cut to a maximum
    length. The database read runs in a worker thread.

    Args:
        request: The incoming request carrying the filter query parameters.

    Returns:
        A JSON response holding the matching entries.
    """
    ctx = context_of(request)
    params = request.query_params
    raw = params.get("before", "")
    _, size = page_of(request)
    entries = await asyncio.to_thread(
        ctx.accounts.audit,
        limit=size,
        before=int(raw) if raw.isdigit() else None,
        action=params.get("action", "")[:60],
        actor=params.get("actor", "")[:80],
        outcome=params.get("outcome", "")[:20],
    )
    return JSONResponse({"entries": [item.model_dump(mode="json") for item in entries]})


@api(
    "GET",
    "/users",
    "Accounts and their roles. No secrets are included.",
    "users.view",
    tag="Administration",
)
async def users_list(request: Request) -> Response:
    """List accounts and their roles without any secrets.

    Args:
        request: The incoming request.

    Returns:
        A JSON response with id, username, role, status, two-step flag and
        last login of each user.
    """
    users = context_of(request).accounts.users()
    return JSONResponse(
        {
            "users": [
                {
                    "id": user.id,
                    "username": user.username,
                    "name": user.label,
                    "email": user.email,
                    "role": user.role_name,
                    "status": user.status,
                    "two_step": user.totp_enabled,
                    "last_login": user.last_login,
                }
                for user in users
            ]
        }
    )


@api("GET", "/roles", "Roles and their permissions.", "roles.manage", tag="Administration")
async def roles_list(request: Request) -> Response:
    """List roles with their effective permissions.

    Args:
        request: The incoming request.

    Returns:
        A JSON response with each role, its sorted permissions and built-in flag.
    """
    roles = context_of(request).accounts.roles()
    return JSONResponse(
        {
            "roles": [
                {
                    "id": role.id,
                    "name": role.name,
                    "description": role.description,
                    "permissions": sorted(role.effective()),
                    "builtin": role.builtin,
                }
                for role in roles
            ]
        }
    )


def modules_ok(ctx: WebContext) -> Any:
    """Build a check telling whether a workspace module is switched on.

    Args:
        ctx: Shared web context whose current policy is read once.

    Returns:
        A function taking a policy flag name or None and returning True when the
        module is enabled or None.
    """
    policy = ctx.policy.get()
    return lambda module: module is None or bool(getattr(policy, module))


@api(
    "GET",
    "/export",
    "Download your data as one JSON bundle. Add ?datasets=a,b to choose parts.",
    "data.export",
    tag="Data",
)
async def export_bundle(request: Request) -> Response:
    """Export the caller's permitted datasets as one JSON bundle.

    Without the datasets parameter every dataset open to the caller is
    exported. Asking for a dataset the caller may not read is refused as a
    whole, and every export is audit logged.

    Args:
        request: The incoming request, optionally with a datasets list.

    Returns:
        The bundle as JSON, or a 403 error when a requested dataset is not open
        to the caller.
    """
    ctx = context_of(request)
    principal = request.state.principal
    allowed = [item.name for item in ctx.portability.available(principal.can, modules_ok(ctx))]
    asked = [name for name in request.query_params.get("datasets", "").split(",") if name]
    chosen = [name for name in asked if name in allowed] if asked else allowed
    if asked and len(chosen) != len(set(asked)):
        return error_response(403, "forbidden", "Some of those datasets are not open to you.")
    bundle = await asyncio.to_thread(ctx.portability.bundle, chosen)
    ctx.accounts.log("data.export", actor=principal, detail={"datasets": chosen, "via": "api"})
    return JSONResponse(bundle)


@api(
    "POST",
    "/import",
    "Import a bundle. Use ?dry_run=1 to see the effect first and ?strategy=update to overwrite.",
    "data.import",
    tag="Data",
)
async def import_bundle(request: Request) -> Response:
    """Import a JSON bundle, optionally as a dry run.

    The strategy query parameter controls conflicts and confirm_purpose
    acknowledges the lawful purpose. The import is audit logged and runs in a
    worker thread.

    Args:
        request: The incoming request whose body is the raw bundle.

    Returns:
        The import result as JSON, with status 200 on success and 422 when it
        failed.
    """
    ctx = context_of(request)
    principal = request.state.principal
    params = request.query_params
    raw = await request.body()
    records = ctx.portability.parse_json(raw)
    dry = params.get("dry_run", "") in {"1", "true", "yes"}
    result = await asyncio.to_thread(
        ctx.portability.apply,
        records,
        params.get("strategy", "skip"),
        principal.name,
        principal.can,
        dry,
        params.get("confirm_purpose", "") in {"1", "true", "yes"},
    )
    ctx.accounts.log(
        "data.import",
        actor=principal,
        outcome="ok" if result.ok else "failed",
        detail={"dry_run": dry, "via": "api"},
    )
    return JSONResponse(
        {**result.model_dump(), "ok": result.ok}, status_code=200 if result.ok else 422
    )
