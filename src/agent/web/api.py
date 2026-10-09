# src/agent/web/api.py
from agent.models import (
    CityName,
    ClarificationAnswer,
    CompanyInput,
    Goal,
    Identity,
    PersonReport,
    Requirement,
    ResearchOptions,
    ShortText,
    SkillSearchReport,
    SkillSearchRequest,
)
from agent.web.api_core import (
    api,
    BASE,
    ENDPOINTS,
    error_response,
    json_body,
    JSON_TYPE,
    owner_of,
    ROUTES,
    visible,
)
from agent.web.context import (
    context_of,
    service_status,
    WebContext,
)
from agent.web.jobs import (
    CVUpload,
    Job,
    JobState,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
)
from agent.errors import (
    ComplianceRefusal,
    InvalidRequest,
)
from agent.web.emails import (
    make_drafts,
    store_of,
)
from agent.web.voice import (
    speech,
    transcript,
)
from starlette.responses import (
    JSONResponse,
    Response,
)
from pydantic.json_schema import models_json_schema
from agent.web.api_data import request_models
from starlette.requests import Request
from agent.accounts import Principal
from starlette.routing import Route
from agent.chat import Profile
from agent import __version__
from agent.text import LIMITS
from typing import Any

import binascii
import base64
import re

__all__ = ["BASE", "ENDPOINTS", "api_routes"]
OPERATION_ERRORS = {
    "401": "Unauthorized",
    "403": "This credential may not do that",
    "422": "The request is not valid",
    "429": "Too many requests",
}


# A base64 encoded CV file sent with an API research request.
class ApiCv(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=200)
    content_base64: str = Field(min_length=1)


# The request body for starting a research job on one person.
class ApiResearch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: Goal
    name: ShortText | None = None
    aliases: list[ShortText] = Field(default_factory=list)
    city: CityName | None = None
    employers: list[ShortText] = Field(default_factory=list)
    roles: list[ShortText] = Field(default_factory=list)
    skills: list[ShortText] = Field(default_factory=list)
    focus: ShortText | None = None
    cv: ApiCv | None = None
    company: CompanyInput | None = None
    requirements: list[Requirement] = Field(default_factory=list, max_length=LIMITS.max_list)
    lawful_purpose: bool
    options: ResearchOptions = Field(default_factory=ResearchOptions)


# The request body for starting a search for candidates by skill.
class ApiSkillSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: Goal
    skill: ShortText
    location: CityName
    limit: int = Field(default=5, ge=1, le=10)
    company: CompanyInput | None = None
    requirements: list[Requirement] = Field(default_factory=list, max_length=LIMITS.max_list)
    lawful_purpose: bool
    options: ResearchOptions = Field(default_factory=ResearchOptions)


# The request body for rendering email drafts from a template.
class ApiDrafts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template: str = Field(min_length=3, max_length=40)
    names: list[ShortText] = Field(default_factory=list, max_length=LIMITS.max_names)
    job: str | None = Field(default=None, max_length=80)
    sender_name: ShortText | None = None
    company_name: ShortText | None = None


# The request body for answering the clarification questions of a job.
class ApiAnswers(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answers: list[ClarificationAnswer] = Field(min_length=1)


def job_view(job: Job, *, full: bool) -> dict[str, Any]:
    """Build the JSON view of a job.

    Links to the report and to the answers endpoint are added depending on the state.

    Args:
        job: The job to describe.
        full: Whether to include the clarification and the full report.

    Returns:
        A JSON-ready dictionary describing the job.
    """
    view: dict[str, Any] = {
        "id": job.id,
        "kind": job.kind,
        "title": job.title,
        "state": job.state.value,
        "stage": job.stage,
        "task": job.task.value if job.task else None,
        "events": len(job.events),
        "error": job.error,
        "links": {"self": f"{BASE}/jobs/{job.id}"},
    }
    if job.state is JobState.DONE:
        view["links"]["report_md"] = f"{BASE}/jobs/{job.id}/report.md"
        view["links"]["report_json"] = f"{BASE}/jobs/{job.id}/report.json"
    if job.state is JobState.NEEDS_INPUT:
        view["links"]["answers"] = f"{BASE}/jobs/{job.id}/answers"
    if full:
        clarification = job.clarification.model_dump(mode="json") if job.clarification else None
        report = job.report.model_dump(mode="json") if job.report else None
        view.update({"clarification": clarification, "report": report})
    return view


def find(request: Request) -> Job | None:
    """Return the job named in the request path if the caller may see it.

    Args:
        request: The request, carrying the authenticated principal and the job id.

    Returns:
        The caller's job, or None when the caller lacks the reports.view permission or
        the job does not exist for them.
    """
    if not request.state.principal.can("reports.view"):
        return None
    return context_of(request).jobs.get(
        request.path_params["job_id"], owner_of(request.state.principal)
    )


def not_found() -> JSONResponse:
    """Return the 404 response used for an unknown job.

    Returns:
        A JSON error response with status 404.
    """
    return error_response(404, "not_found", "No such job.")


@api("GET", "/status", "Service status: providers and Apify credit.", "api.use")
async def status(request: Request) -> Response:
    """Handle GET /status by returning provider status and Apify credit.

    Args:
        request: The incoming request.

    Returns:
        The status report with the version, or a 503 error when it is unavailable.
    """
    report, problem = await service_status(context_of(request))
    if report is None:
        return error_response(503, "unavailable", problem or "Status is not available.")
    return JSONResponse({**report.model_dump(), "version": __version__})


@api("POST", "/research", "Start researching one person.", "research.run", body="ApiResearch")
async def start_research(request: Request) -> Response:
    """Handle POST /research by starting a research job on one person.

    The person is given either by identity fields or by an uploaded CV, never both.

    Args:
        request: The incoming request with an ApiResearch JSON body.

    Returns:
        A 202 response with the new job.

    Raises:
        ComplianceRefusal: when the lawful purpose is not confirmed.
        InvalidRequest: when name and cv are not exclusive, the CV is combined with
            identity fields, is not valid base64 or exceeds the size limit.
    """
    ctx = context_of(request)
    body = ApiResearch.model_validate(await json_body(request))
    if not body.lawful_purpose:
        raise ComplianceRefusal("A lawful purpose must be confirmed (lawful_purpose: true).")
    # This is true when both or neither are given, so exactly one source is required.
    if (body.name is None) == (body.cv is None):
        raise InvalidRequest("Provide exactly one of name or cv.")
    source: Identity | CVUpload
    if body.cv is not None:
        if any([body.aliases, body.city, body.employers, body.roles, body.skills]):
            raise InvalidRequest("cv cannot be combined with identity fields.")
        try:
            # Strict decoding rejects stray characters instead of silently skipping them.
            data = base64.b64decode(body.cv.content_base64, validate=True)
        except (binascii.Error, ValueError) as error:
            raise InvalidRequest("cv.content_base64 is not valid base64.") from error
        if len(data) > ctx.settings.cv.max_bytes:
            raise InvalidRequest(f"The CV may be at most {ctx.settings.cv.max_bytes} bytes.")
        source = CVUpload(data=data, filename=body.cv.filename)
    else:
        source = Identity(
            name=body.name or "",
            aliases=body.aliases,
            location=body.city,
            employers=body.employers,
            roles=body.roles,
            skills=body.skills,
        )
    job = ctx.jobs.start_research(
        owner_of(request.state.principal),
        body.goal,
        body.focus,
        source,
        body.options,
        body.company,
        body.requirements,
    )
    return JSONResponse(job_view(job, full=False), status_code=202)


@api(
    "POST",
    "/skill-search",
    "Find and rank candidates by skill.",
    "research.run",
    body="ApiSkillSearch",
)
async def start_skill_search(request: Request) -> Response:
    """Handle POST /skill-search by starting a search for candidates by skill.

    Args:
        request: The incoming request with an ApiSkillSearch JSON body.

    Returns:
        A 202 response with the new job.

    Raises:
        ComplianceRefusal: when the lawful purpose is not confirmed.
    """
    ctx = context_of(request)
    body = ApiSkillSearch.model_validate(await json_body(request))
    if not body.lawful_purpose:
        raise ComplianceRefusal("A lawful purpose must be confirmed (lawful_purpose: true).")
    search = SkillSearchRequest(
        goal=body.goal,
        skill=body.skill,
        location=body.location,
        limit=body.limit,
        purpose_confirmed=True,
        company=body.company,
        requirements=body.requirements,
        options=body.options,
    )
    job = ctx.jobs.start_skill_search(owner_of(request.state.principal), search)
    return JSONResponse(job_view(job, full=False), status_code=202)


@api("GET", "/jobs", "List your jobs.", "reports.view")
async def list_jobs(request: Request) -> Response:
    """Handle GET /jobs by listing the caller's jobs.

    Args:
        request: The incoming request.

    Returns:
        A JSON response with a summary of each job.
    """
    jobs = context_of(request).jobs.for_owner(owner_of(request.state.principal))
    return JSONResponse({"jobs": [job_view(job, full=False) for job in jobs]})


@api("GET", "/jobs/{job_id}", "Read a job, its question or its report.", "reports.view")
async def read_job(request: Request) -> Response:
    """Handle GET /jobs/{job_id} by returning a job with its question or report.

    Args:
        request: The incoming request.

    Returns:
        The full job view, or a 404 error when the job is not found.
    """
    job = find(request)
    return not_found() if job is None else JSONResponse(job_view(job, full=True))


@api(
    "POST",
    "/jobs/{job_id}/answers",
    "Answer a clarification.",
    "research.run",
    body="ApiAnswers",
)
async def answer_job(request: Request) -> Response:
    """Handle POST /jobs/{job_id}/answers by answering a clarification.

    Args:
        request: The incoming request with an ApiAnswers JSON body.

    Returns:
        A 202 response with the resumed job, or a 404 error when the job is not found.

    Raises:
        AgentError: when the job is not waiting for answers or the answers are invalid.
    """
    ctx = context_of(request)
    job = find(request)
    if job is None:
        return not_found()
    body = ApiAnswers.model_validate(await json_body(request))
    ctx.jobs.answer(job, body.answers)
    return JSONResponse(job_view(job, full=False), status_code=202)


async def report_file(request: Request, fmt: str) -> Response:
    """Return the exported report of a job as a file.

    Args:
        request: The incoming request.
        fmt: 'md' for the Markdown report; any other value selects the JSON report.

    Returns:
        The report with its media type, or a 404 error when the job or report is missing.
    """
    job = find(request)
    if job is None:
        return not_found()
    data = context_of(request).jobs.read_export(job, "md" if fmt == "md" else "json")
    if data is None:
        return error_response(404, "no_report", "The report is not available.")
    media = "text/markdown; charset=utf-8" if fmt == "md" else JSON_TYPE
    return Response(data, media_type=media)


@api("GET", "/jobs/{job_id}/report.md", "Download the Markdown report.", "reports.export")
async def report_markdown(request: Request) -> Response:
    """Handle GET /jobs/{job_id}/report.md by returning the Markdown report.

    Args:
        request: The incoming request.

    Returns:
        The Markdown report, or a 404 error.
    """
    return await report_file(request, "md")


@api("GET", "/jobs/{job_id}/report.json", "Download the JSON report.", "reports.export")
async def report_json(request: Request) -> Response:
    """Handle GET /jobs/{job_id}/report.json by returning the JSON report.

    Args:
        request: The incoming request.

    Returns:
        The JSON report, or a 404 error.
    """
    return await report_file(request, "json")


@api(
    "POST",
    "/voice/transcribe",
    "Turn a recording into text. Needs ELEVENLABS_API_KEY.",
    "voice.use",
    body="audio",
)
async def transcribe(request: Request) -> Response:
    """Handle POST /voice/transcribe by turning a recording into text.

    Args:
        request: The incoming request whose body is the audio recording.

    Returns:
        A JSON response with the transcribed text.
    """
    owner = owner_of(request.state.principal)
    text = await transcript(context_of(request), request, owner)
    return JSONResponse({"text": text})


@api(
    "GET",
    "/jobs/{job_id}/speech",
    "Hear a job summary as MP3. Needs ELEVENLABS_API_KEY.",
    "voice.use",
)
async def speak(request: Request) -> Response:
    """Handle GET /jobs/{job_id}/speech by returning a spoken job summary.

    Args:
        request: The incoming request.

    Returns:
        The summary as MP3 audio, or a 404 error when the job is not found.
    """
    job = find(request)
    if job is None:
        return not_found()
    audio = await speech(context_of(request), job, owner_of(request.state.principal))
    return Response(audio, media_type="audio/mpeg")


@api(
    "GET",
    "/jobs/{job_id}/events",
    "Poll the live activity of a job: stages, sources, findings and ratings.",
    "reports.view",
)
async def read_events(request: Request) -> Response:
    """Handle GET /jobs/{job_id}/events by returning new live activity.

    The optional 'after' query parameter is the index of the first event wanted; a
    missing or non-numeric value counts as zero.

    Args:
        request: The incoming request.

    Returns:
        The events, the index to poll from next, and the job state and stage, or a 404
        error when the job is not found.
    """
    ctx = context_of(request)
    job = find(request)
    if job is None:
        return not_found()
    raw = request.query_params.get("after", "")
    after = int(raw) if raw.isdigit() else 0
    events = ctx.jobs.events_after(job, after)
    return JSONResponse(
        {
            "events": [event.model_dump(mode="json") for event in events],
            "next": after + len(events),
            "state": job.state.value,
            "stage": job.stage,
        }
    )


@api(
    "GET",
    "/email/templates",
    "List the email templates (text only, nothing is sent).",
    "emails.view",
)
async def list_templates(request: Request) -> Response:
    """Handle GET /email/templates by listing the email templates.

    Args:
        request: The incoming request.

    Returns:
        A JSON response with all templates.
    """
    templates = store_of(context_of(request)).all()
    return JSONResponse({"templates": [item.model_dump(mode="json") for item in templates]})


@api(
    "POST",
    "/email/drafts",
    "Render email drafts from a template for one person, a group or a team.",
    "emails.view",
    body="ApiDrafts",
)
async def make_email_drafts(request: Request) -> Response:
    """Handle POST /email/drafts by rendering drafts from a template.

    The job is only used when the caller may view reports. Nothing is sent.

    Args:
        request: The incoming request with an ApiDrafts JSON body.

    Returns:
        A JSON response with the drafts, or a 404 error for an unknown template or job.
    """
    ctx = context_of(request)
    body = ApiDrafts.model_validate(await json_body(request))
    template = store_of(ctx).get(body.template)
    if template is None:
        return error_response(404, "not_found", "No such template.")
    owner = owner_of(request.state.principal)
    job = (
        ctx.jobs.get(body.job, owner)
        if body.job and request.state.principal.can("reports.view")
        else None
    )
    # A job that cannot be read, including for lack of permission, looks like a missing one.
    if body.job and job is None:
        return not_found()
    profile = Profile(sender_name=body.sender_name, company_name=body.company_name)
    drafts = make_drafts(template, ctx, profile, job, list(body.names))
    return JSONResponse({"drafts": [draft.model_dump() for draft in drafts]})


@api("DELETE", "/jobs/{job_id}", "Delete a job and its exported files.", "reports.delete")
async def delete_job(request: Request) -> Response:
    """Handle DELETE /jobs/{job_id} by deleting a job and its exported files.

    Args:
        request: The incoming request.

    Returns:
        An empty 204 response, or a 404 error when the job is not found.
    """
    ctx = context_of(request)
    job = find(request)
    if job is None:
        return not_found()
    ctx.jobs.delete(job)
    return Response(status_code=204)


def openapi_document(ctx: WebContext, principal: Principal) -> dict[str, Any]:
    """Build the OpenAPI document for the endpoints a principal may use.

    Args:
        ctx: The web context.
        principal: The caller; endpoints they lack the permission for are left out.

    Returns:
        An OpenAPI 3.1 document as a dictionary.
    """
    _, schemas = models_json_schema(
        [
            (ApiResearch, "validation"),
            (ApiSkillSearch, "validation"),
            (ApiAnswers, "validation"),
            (ApiDrafts, "validation"),
            (PersonReport, "serialization"),
            (SkillSearchReport, "serialization"),
            *[(model, "validation") for model in request_models()],
        ],
        ref_template="#/components/schemas/{model}",
    )
    paths: dict[str, dict[str, Any]] = {}
    for endpoint in visible(ctx, principal):
        operation: dict[str, Any] = {
            "summary": endpoint["summary"],
            "tags": [endpoint["tag"]],
            "security": [{"bearerAuth": []}],
            "x-permission": endpoint["permission"],
            "responses": {
                "200": {"description": "Success"},
                **{code: {"description": text} for code, text in OPERATION_ERRORS.items()},
            },
        }
        names = re.findall(r"\{(\w+)\}", endpoint["path"])
        if names:
            operation["parameters"] = [
                {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
                for name in names
            ]
        if endpoint["body"] == "audio":
            operation["requestBody"] = {
                "required": True,
                "content": {"audio/*": {"schema": {"type": "string", "format": "binary"}}},
            }
        elif endpoint["body"]:
            operation["requestBody"] = {
                "required": True,
                "content": {
                    JSON_TYPE: {"schema": {"$ref": f"#/components/schemas/{endpoint['body']}"}}
                },
            }
        paths.setdefault(endpoint["path"], {})[endpoint["method"].lower()] = operation
    return {
        "openapi": "3.1.0",
        "info": {"title": "Hiring workspace API", "version": __version__},
        "paths": paths,
        "components": {
            "schemas": schemas.get("$defs", {}),
            "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}},
        },
    }


@api(
    "GET",
    "/openapi.json",
    "This API as an OpenAPI document, limited to what you may do.",
    "api.use",
)
async def openapi(request: Request) -> Response:
    """Handle GET /openapi.json by returning the API description.

    Args:
        request: The incoming request.

    Returns:
        The OpenAPI document limited to what the caller may do.
    """
    return JSONResponse(openapi_document(context_of(request), request.state.principal))


def api_routes() -> list[Route]:
    """Return the routes of the HTTP API.

    Returns:
        A new list with every registered API route.
    """
    return list(ROUTES)
