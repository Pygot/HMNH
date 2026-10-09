# src/agent/web/app.py
from agent.web.talk import (
    announcement_create,
    announcement_delete,
    announcement_read,
    announcements_page,
    conversation_keys,
    direct_create,
    feed_json,
    group_create,
    group_delete,
    group_leave,
    group_members,
    group_rename,
    key_of,
    keys_get,
    keys_save,
    message_remove,
    messages_page,
    peers_json,
    read_json,
    role_channel,
    rotate_json,
    send_json,
    unread_json,
)
from agent.web.admin import (
    admin_home,
    audit_csv,
    audit_page,
    role_delete,
    role_save,
    roles_page,
    security_page,
    security_save,
    user_create,
    user_delete,
    user_key_revoke,
    user_link,
    user_page,
    user_sessions,
    user_two_factor,
    user_update,
    users_page,
)
from agent.web.common import (
    choice,
    current_session,
    json_errors,
    landing_path,
    NETWORK_LABELS,
    number,
    prefs_cookie_name,
    protected_form,
    protected_header,
    public,
    read_preferences,
    render,
    requires,
    resolve_principal,
    save_preferences,
    text,
    voice_ready,
)
from agent.web.people import (
    candidate_create,
    candidate_delete,
    candidate_export,
    candidate_new,
    candidate_note,
    candidate_page,
    candidate_update,
    candidates_page,
    contact_add,
    contact_remove,
    hire_create,
    hire_delete,
    hire_update,
    job_candidate,
    link_add,
    link_remove,
    record_research,
)
from agent.web.auth import (
    forgot,
    invite_accept,
    invite_page,
    landing,
    link_request,
    login,
    logout,
    reset_accept,
    reset_page,
    setup_create,
    setup_page,
    signin_accept,
    signin_page,
    verify,
    verify_page,
)
from agent.web.account import (
    account_page,
    change_password,
    email_confirm,
    email_login,
    email_send_code,
    key_create,
    key_revoke,
    save_profile as account_profile,
    sessions_revoke,
    two_factor_disable,
    two_factor_enable,
    two_factor_page,
    two_factor_recovery,
)
from agent.web.automation import (
    job_routine,
    routine_create,
    routine_delete,
    routine_page,
    routine_pause,
    routine_resume,
    routine_run,
    routine_settled,
    routine_update,
    RoutineRunner,
    routines_page,
)
from agent.models import (
    Category,
    CompanyInput,
    Finding,
    Goal,
    Network,
    PersonReport,
    ScepticismMode,
    ShortText,
    Strictness,
)
from agent.web.money import (
    category_create,
    category_delete,
    category_update,
    currency_save,
    entry_create,
    entry_delete,
    entry_update,
    finance_csv,
    finance_page,
)
from agent.web.chat import (
    chat_delete,
    chat_messages,
    chat_page,
    chat_send,
    chat_speech,
    tie_clear,
    tie_messages,
    tie_send,
)
from agent.web.data import (
    data_page,
    export_csv,
    export_json,
    import_confirm,
    import_page,
    import_preview,
)
from agent.web.emails import (
    emails_delete,
    emails_page,
    emails_preview,
    emails_save,
    job_drafts,
    PLACEHOLDERS,
)
from agent.web.jobs import (
    API_OWNER,
    Job,
    JobManager,
    JobState,
    WEB_OWNER,
)
from agent.web.outbox import (
    compose_page,
    mail_log,
    mail_send,
    suppress_add,
    suppress_remove,
)
from agent.web.prefs import (
    BuddyMode,
    Density,
    LiveView,
    Preferences,
    Theme,
)
from agent.web.security import (
    BodyLimitMiddleware,
    ENROLL,
    RateLimiter,
    SecurityHeadersMiddleware,
    SessionStore,
)
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from agent.chat import (
    load_profile,
    Profile,
    save_profile,
)
from agent.web.connect import (
    connections_page,
    connections_save,
    connections_test,
)
from agent.web.context import (
    context_of,
    service_status,
    WebContext,
)
from agent.web.format import (
    device,
    span,
    stamp,
)
from agent.connections import (
    ConnectionChecker,
    Connections,
)
from agent.web.buddy import (
    buddy_line,
    choose_buddy,
)
from agent.web.dashboard import (
    chart_svg,
    dashboard_page,
)
from agent.web.hosting import (
    ServiceHost,
    ServicesFactory,
)
from agent.web.icons import (
    task_icon,
    task_icon_set,
)
from agent.web.logo import (
    logo_markup,
    tie_markup,
)
from agent.web.voice import (
    speech,
    transcript,
)
from pydantic import (
    TypeAdapter,
    ValidationError,
)
from typing import (
    Any,
    cast,
)
from starlette.middleware.trustedhost import TrustedHostMiddleware
from agent.web.overview import report_overview
from starlette.exceptions import HTTPException
from starlette.applications import Starlette
from starlette.middleware import Middleware
from agent.passwords import PasswordHasher
from agent.web.settings import WebSettings
from contextlib import asynccontextmanager
from agent.portability import Portability
from agent.web.assets import build_assets
from collections.abc import AsyncIterator
from agent.permissions import Permission
from agent.candidates import Candidates
from agent.export import delete_outputs
from agent.drafts import TemplateStore
from agent.text import describe_errors
from agent.web.api_core import visible
from starlette.requests import Request
from agent.finance import FinanceBook
from agent.messaging import Messenger
from agent.web.api import api_routes
from agent.accounts import Accounts
from agent.policy import PolicyBook
from agent.routines import Routines
from starlette.routing import Route
from agent.config import Settings
from agent.http import TTLCache
from agent.mailer import Mailer
from agent.stats import Stats
from agent.store import Store
from agent.vault import Vault
from pathlib import Path

import logging
import secrets
import jinja2
import json

logger = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent
TEMPLATE_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"
ERROR_TITLES = {
    400: "That request could not be processed",
    403: "Request rejected",
    404: "Page not found",
    405: "Method not allowed",
    413: "Request too large",
    429: "Too many requests",
}
SETTINGS_FIELDS = (
    "theme",
    "density",
    "live_view",
    "buddy",
    "show_api",
    "backdrop",
    "goal",
    "strictness",
    "scepticism",
    "threshold",
    "margin",
    "max_web_pages",
    "skill_limit",
    "company_name",
    "company_website",
    "company_description",
    "sender_name",
)
DELETE_TARGETS = ("history", "memory", "all")
SHORT_TEXT = TypeAdapter(ShortText)


@public
async def home(request: Request) -> Response:
    """Serve the landing page, the chat page, or a redirect to the right place.

    Sessions that still have to enrol in two-factor sign-in are sent to the enrolment
    page. Signed-out visitors get the public landing page.

    Args:
        request: The incoming request.

    Returns:
        The chat page, the landing page, or a 303 redirect.
    """
    ctx = context_of(request)
    session = current_session(request)
    if session is not None and session.stage == ENROLL:
        return RedirectResponse("/account/2fa", status_code=303)
    principal = resolve_principal(ctx, session)
    if principal is None:
        return landing(request)
    if principal.can(Permission.CHAT_USE):
        return await chat_page(request)
    return RedirectResponse(landing_path(principal), status_code=303)


@public
async def login_page(_: Request) -> Response:
    """Redirect GET requests for the login URL to the home page.

    Args:
        _: The incoming request, unused.

    Returns:
        A 303 redirect to the home page.
    """
    return RedirectResponse("/", status_code=303)


def group_findings(findings: list[Finding]) -> list[tuple[Category, list[Finding]]]:
    """Group report findings by category, skipping categories without findings.

    Args:
        findings: The findings of a person report.

    Returns:
        Pairs of category and its findings, in category order.
    """
    return [
        (category, group)
        for category in Category
        if (group := [finding for finding in findings if finding.category is category])
    ]


def job_or_404(request: Request) -> Job:
    """Return the web-owned job named in the URL, or raise a 404.

    A missing job and a caller without report access both produce the same error.

    Args:
        request: The incoming request with a job_id path parameter.

    Returns:
        The job.

    Raises:
        HTTPException: 404 when the job does not exist or the caller cannot view reports.
    """
    job = context_of(request).jobs.get(request.path_params["job_id"], WEB_OWNER)
    # Both cases give the same 404 so job ids cannot be probed.
    if job is None or not request.state.principal.can("reports.view"):
        raise HTTPException(404)
    return job


def seen_before(ctx: WebContext, request: Request, report: object, job: Job) -> object:
    """Look up whether a researched person is already a saved candidate.

    Args:
        ctx: The web context.
        request: The incoming request, used for the caller's permissions.
        report: The job's report.
        job: The job that produced the report.

    Returns:
        The earlier candidate match, or None when the report is not a person report, the
        candidates module is off, or the caller cannot view candidates.
    """
    principal = request.state.principal
    if not (
        isinstance(report, PersonReport)
        and ctx.policy.get().candidates_enabled
        and principal.can("candidates.view")
    ):
        return None
    return ctx.candidates.seen(report.identity, job.id)


def job_response(request: Request, job: Job, status: int = 200) -> HTMLResponse:
    """Render the page for a research job.

    Args:
        request: The incoming request.
        job: The job to show.
        status: HTTP status code of the response.

    Returns:
        The job page with its report, grouped findings, overview, email drafts and a flag
        saying whether the caller may send mail.
    """
    ctx = context_of(request)
    profile = load_profile(ctx.store)
    report = job.report
    thread = ctx.store.thread(job.thread_id) if job.thread_id else None
    groups = group_findings(report.findings) if isinstance(report, PersonReport) else []
    return render(
        request,
        "job.html",
        status,
        job=job,
        thread=thread,
        report=report,
        groups=groups,
        overview=report_overview(report, ctx.settings.scale),
        seen=seen_before(ctx, request, report, job),
        send_mail=(
            ctx.mailer.configured()
            and ctx.policy.get().email_sending_enabled
            and request.state.principal.can("emails.send")
        ),
        drafts=job_drafts(ctx, profile, job),
        running=job.state is JobState.RUNNING,
        states=JobState,
    )


@requires(Permission.REPORTS_VIEW)
async def job_page(request: Request) -> Response:
    """Show the page for one research job.

    Args:
        request: The incoming request.

    Returns:
        The rendered job page.
    """
    return job_response(request, job_or_404(request))


@requires(Permission.REPORTS_VIEW)
async def job_status(request: Request) -> Response:
    """Return the state, stage and current task of a job as JSON.

    Args:
        request: The incoming request.

    Returns:
        A JSON response with the state, stage and task fields.
    """
    job = job_or_404(request)
    return JSONResponse(
        {"state": job.state.value, "stage": job.stage, "task": job.task.value if job.task else None}
    )


async def event_stream(ctx: WebContext, job: Job, after: int) -> AsyncIterator[str]:
    """Yield the events of a job as server-sent event text.

    Args:
        ctx: The web context.
        job: The job to watch.
        after: Sequence number of the first event to send.

    Returns:
        An async iterator of event frames, keep-alive comments, and a final end event.
    """
    async for event in ctx.jobs.watch(job, after):
        if event is None:
            yield ": ping\n\n"
        else:
            yield f"id: {event.seq}\ndata: {event.model_dump_json()}\n\n"
    yield "event: end\ndata: {}\n\n"


def resume_point(request: Request) -> int:
    """Return the event sequence number a job stream should resume from.

    The Last-Event-ID header (plus one) wins over the after query parameter.

    Args:
        request: The incoming request.

    Returns:
        The sequence number, or 0 when neither value is a plain number.
    """
    last = request.headers.get("last-event-id", "")
    if last.isdigit():
        return int(last) + 1
    after = request.query_params.get("after", "")
    return int(after) if after.isdigit() else 0


@requires(Permission.REPORTS_VIEW)
async def job_events(request: Request) -> Response:
    """Stream a job's live events as text/event-stream.

    Args:
        request: The incoming request.

    Returns:
        An uncached, unbuffered streaming response.
    """
    job = job_or_404(request)
    stream = event_stream(context_of(request), job, resume_point(request))
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@requires(Permission.REPORTS_EXPORT)
async def download(request: Request) -> Response:
    """Send the exported report of a job as a Markdown or JSON attachment.

    Args:
        request: The incoming request with job_id and fmt path parameters.

    Returns:
        The file as a download named report.md or report.json.

    Raises:
        HTTPException: 404 when the format is unknown or the export no longer exists.
    """
    job = job_or_404(request)
    fmt = request.path_params["fmt"]
    if fmt not in {"md", "json"}:
        raise HTTPException(404)
    data = context_of(request).jobs.read_export(job, fmt)
    if data is None:
        raise HTTPException(404)
    media = "text/markdown; charset=utf-8" if fmt == "md" else "application/json"
    return Response(
        data,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="report.{fmt}"'},
    )


@requires(Permission.REPORTS_DELETE)
async def delete_job(request: Request) -> Response:
    """Delete a job after checking the form token.

    Args:
        request: The incoming POST request.

    Returns:
        A 303 redirect to the home page.
    """
    job = job_or_404(request)
    form = await protected_form(request, request.state.session)
    await form.close()
    context_of(request).jobs.delete(job)
    return RedirectResponse("/", status_code=303)


def settings_context(
    request: Request, errors: list[str], saved: bool, profile: Profile
) -> dict[str, Any]:
    """Build the template values shared by the settings page and its error view.

    The buddy modes offered leave out voice when voice services are not available.

    Args:
        request: The incoming request.
        errors: Validation messages to show.
        saved: Whether to show the saved confirmation.
        profile: The stored company and requirements profile.

    Returns:
        The template context.
    """
    ctx = context_of(request)
    return {
        "active": "settings",
        "errors": errors,
        "saved": saved,
        "profile": profile,
        "counts": ctx.store.counts(),
        "store_path": ctx.settings.store.path.as_posix(),
        "retention_days": ctx.settings.store.retention_days,
        "themes": list(Theme),
        "densities": list(Density),
        "live_views": list(LiveView),
        "buddy_modes": [
            mode for mode in BuddyMode if mode is not BuddyMode.VOICE or voice_ready(ctx)
        ],
        "goals": list(Goal),
        "networks": list(Network),
        "strictness_levels": list(Strictness),
        "scepticism_modes": list(ScepticismMode),
        "presets": ctx.settings.scoring.presets,
    }


@requires()
async def settings_page(request: Request) -> Response:
    """Show the settings page.

    Args:
        request: The incoming request.

    Returns:
        The rendered settings page including the status of the research service.
    """
    ctx = context_of(request)
    service, problem = await service_status(ctx)
    saved = request.query_params.get("saved") == "1"
    context = settings_context(request, [], saved, load_profile(ctx.store))
    return render(request, "settings.html", service=service, service_problem=problem, **context)


def profile_from(values: dict[str, str], current: Profile, errors: list[str]) -> Profile:
    """Apply submitted company fields to a profile and validate them.

    Empty values become None. Validation problems are appended to errors instead of
    raised.

    Args:
        values: The submitted form values by field name.
        current: The stored profile to copy.
        errors: List that receives error messages.

    Returns:
        A copy of the profile with the submitted values applied.
    """
    updated = current.model_copy(
        update={
            "company_name": values["company_name"] or None,
            "company_website": values["company_website"] or None,
            "company_description": values["company_description"] or None,
            "sender_name": values["sender_name"] or None,
        }
    )
    try:
        if updated.company_name:
            CompanyInput(
                name=updated.company_name,
                website=updated.company_website,
                description=updated.company_description,
            )
        elif updated.company_website or updated.company_description:
            errors.append("Enter the company name, or clear its website and description.")
        if updated.sender_name:
            SHORT_TEXT.validate_python(updated.sender_name)
    except ValidationError as error:
        errors.append(describe_errors(error))
    return updated


@requires()
async def save_settings(request: Request) -> Response:
    """Validate and save the settings form.

    Preferences go into a cookie. The company profile is only saved when the caller may
    edit context. With any validation error the page is shown again and nothing is saved.

    Args:
        request: The incoming POST request.

    Returns:
        A 303 redirect to the settings page, or the settings page with status 400.
    """
    ctx = context_of(request)
    form = await protected_form(request, request.state.session)
    errors: list[str] = []
    try:
        values = {key: text(form, key, ctx.web.max_field_length) for key in SETTINGS_FIELDS}
        sources = [
            value[: ctx.web.max_field_length]
            for value in form.getlist("source")
            if isinstance(value, str)
        ]
    finally:
        await form.close()
    networks = [
        network
        for raw in sources
        if (network := choice(raw, Network, "Source", errors)) is not None
    ]
    skill_limit = number(values["skill_limit"], int, "Candidates", errors)
    buddy = choice(values["buddy"], BuddyMode, "Buddy", errors) or BuddyMode.TEXT
    if buddy is BuddyMode.VOICE and not voice_ready(ctx):
        errors.append("Voice is not enabled, so the buddy can only use text.")
    can_edit = request.state.principal.can(Permission.CONTEXT_EDIT)
    profile = (
        profile_from(values, load_profile(ctx.store), errors)
        if can_edit
        else load_profile(ctx.store)
    )
    try:
        preferences = Preferences(
            theme=choice(values["theme"], Theme, "Theme", errors) or Theme.SYSTEM,
            density=choice(values["density"], Density, "Density", errors) or Density.COMFORTABLE,
            live_view=choice(values["live_view"], LiveView, "Live view", errors) or LiveView.CHAT,
            buddy=buddy,
            show_api=values["show_api"] == "yes",
            backdrop=values["backdrop"] == "yes",
            goal=choice(values["goal"], Goal, "Goal", errors) or Goal.HIRING,
            networks=networks,
            strictness=choice(values["strictness"], Strictness, "Matching", errors)
            or Strictness.BALANCED,
            threshold=number(values["threshold"], float, "Match threshold", errors),
            margin=number(values["margin"], float, "Match margin", errors),
            max_web_pages=number(values["max_web_pages"], int, "Web pages", errors),
            scepticism=choice(values["scepticism"], ScepticismMode, "Scepticism", errors)
            or ScepticismMode.STANDARD,
            # A blank field is left out so the model default for skill_limit applies.
            **({} if skill_limit is None else {"skill_limit": skill_limit}),
        )
    except ValidationError as error:
        errors.append(describe_errors(error))
        preferences = None
    if errors or preferences is None:
        service, problem = await service_status(ctx)
        context = settings_context(request, errors, False, profile)
        return render(
            request, "settings.html", 400, service=service, service_problem=problem, **context
        )
    if can_edit:
        save_profile(ctx.store, profile)
    response = RedirectResponse("/settings?saved=1", status_code=303)
    save_preferences(response, ctx.web, preferences)
    return response


@requires()
async def reset_settings(request: Request) -> Response:
    """Delete the preferences cookie so defaults apply again.

    Args:
        request: The incoming POST request.

    Returns:
        A 303 redirect to the settings page.
    """
    ctx = context_of(request)
    form = await protected_form(request, request.state.session)
    await form.close()
    response = RedirectResponse("/settings", status_code=303)
    response.delete_cookie(
        prefs_cookie_name(ctx.web),
        path="/",
        secure=ctx.web.cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return response


@requires(Permission.CONTEXT_EDIT)
async def remove_requirement(request: Request) -> Response:
    """Remove one saved hiring requirement from the stored profile.

    An index that is not a valid position is ignored.

    Args:
        request: The incoming POST request with an index form field.

    Returns:
        A 303 redirect to the settings page.
    """
    ctx = context_of(request)
    form = await protected_form(request, request.state.session)
    try:
        raw = text(form, "index", 4)
    finally:
        await form.close()
    profile = load_profile(ctx.store)
    if raw.isdigit() and int(raw) < len(profile.requirements):
        del profile.requirements[int(raw)]
        save_profile(ctx.store, profile)
    return RedirectResponse("/settings?saved=1#context", status_code=303)


@requires(Permission.DATA_DELETE)
async def delete_data(request: Request) -> Response:
    """Delete stored history, memory, or everything, as chosen in the form.

    History removes jobs, research and tie threads and exported report files. The all
    target also removes API jobs and wipes the store. Memory forgets the saved profile.

    Args:
        request: The incoming POST request with a what form field.

    Returns:
        A 303 redirect to the settings page.

    Raises:
        HTTPException: 400 when the target is not history, memory or all.
    """
    ctx = context_of(request)
    form = await protected_form(request, request.state.session)
    try:
        target = text(form, "what", 12)
    finally:
        await form.close()
    if target not in DELETE_TARGETS:
        raise HTTPException(400)
    if target in {"history", "all"}:
        for owner in (WEB_OWNER, API_OWNER) if target == "all" else (WEB_OWNER,):
            ctx.jobs.forget_all(owner)
        for thread in [*ctx.store.threads("research"), *ctx.store.threads("tie")]:
            ctx.store.delete_thread(thread.id)
        delete_outputs(ctx.settings.output_dir)
    if target == "all":
        ctx.store.wipe()
    elif target == "memory":
        ctx.store.forget(Profile.KEY)
    return RedirectResponse("/settings?saved=1#data", status_code=303)


@requires()
@json_errors
async def choose_live_view(request: Request) -> Response:
    """Save the chosen live view in the preferences cookie.

    Args:
        request: The incoming POST request with a JSON body holding a view field.

    Returns:
        An empty 204 response that sets the preferences cookie.

    Raises:
        HTTPException: 400 when the body is not valid JSON or the view is unknown.
    """
    ctx = protected_header(request, request.state.session)
    try:
        view = LiveView(json.loads(await request.body()).get("view", ""))
    except (ValueError, AttributeError) as error:
        raise HTTPException(400) from error
    preferences, _ = read_preferences(request)
    response = Response(status_code=204)
    save_preferences(response, ctx.web, preferences.model_copy(update={"live_view": view}))
    return response


@requires(Permission.API_USE)
async def api_page(request: Request) -> Response:
    """Show the API reference for the endpoints the caller may use.

    Args:
        request: The incoming request.

    Returns:
        The API page, or a redirect to the settings page when the show API preference is
        off.
    """
    ctx = context_of(request)
    preferences, _ = read_preferences(request)
    if not preferences.show_api:
        return RedirectResponse("/settings#developer", status_code=303)
    base_url = f"{request.url.scheme}://{request.headers.get('host', 'localhost')}"
    shown = visible(ctx, request.state.principal)
    groups = {}
    for endpoint in shown:
        groups.setdefault(endpoint["tag"], []).append(endpoint)
    return render(request, "api.html", active="api", groups=groups, base_url=base_url)


@public
async def robots(request: Request) -> Response:
    """Serve the configured robots.txt text.

    Args:
        request: The incoming request.

    Returns:
        The plain text response.
    """
    return Response(context_of(request).web.robots_txt, media_type="text/plain; charset=utf-8")


@public
async def static_file(request: Request) -> Response:
    """Serve a bundled static asset with cache validation and optional gzip.

    Args:
        request: The incoming request with a name path parameter.

    Returns:
        The asset, or an empty 304 when If-None-Match matches its ETag.

    Raises:
        HTTPException: 404 when no asset has that name.
    """
    asset = context_of(request).assets.get(request.path_params["name"])
    if asset is None:
        raise HTTPException(404)
    etag = f'"{asset.etag}"'
    headers = {"ETag": etag, "Cache-Control": asset.cache_control, "Vary": "Accept-Encoding"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    packed = "gzip" in request.headers.get("accept-encoding", "")
    if packed:
        headers["Content-Encoding"] = "gzip"
    body = asset.packed if packed else asset.body
    return Response(body, media_type=asset.media_type, headers=headers)


@requires(Permission.VOICE_USE)
@json_errors
async def voice_transcribe(request: Request) -> Response:
    """Turn an uploaded recording into text.

    Args:
        request: The incoming POST request with the audio and a CSRF header.

    Returns:
        A JSON response with the transcript in the text field.
    """
    session = request.state.session
    ctx = protected_header(request, session)
    return JSONResponse({"text": await transcript(ctx, request, session.id)})


@requires(Permission.VOICE_USE)
@json_errors
async def job_speech(request: Request) -> Response:
    """Return a spoken audio version of a job's report.

    Args:
        request: The incoming request.

    Returns:
        An audio/mpeg response.
    """
    job = job_or_404(request)
    audio = await speech(context_of(request), job, request.state.session.id)
    return Response(audio, media_type="audio/mpeg")


async def http_error(request: Request, error: Exception) -> Response:
    """Render an HTTP exception as an error page.

    The detail text is only shown for 403 and 429 responses. Headers carried by the
    exception are copied onto the response.

    Args:
        request: The incoming request.
        error: The raised HTTPException.

    Returns:
        The rendered error page with the exception's status code.
    """
    exc = cast(HTTPException, error)
    title = ERROR_TITLES.get(exc.status_code, "Something went wrong")
    detail = exc.detail if exc.status_code in {403, 429} else ""
    response = render(request, "error.html", exc.status_code, title=title, detail=detail)
    for name, value in (exc.headers or {}).items():
        response.headers[name] = value
    return response


def build_templates(urls: dict[str, str], time_format: str) -> jinja2.Environment:
    """Create the Jinja environment used to render pages.

    Autoescaping is on and undefined variables raise errors. Filters and globals used
    by the templates are registered here.

    Args:
        urls: Map from asset names to their public URLs.
        time_format: Format string used by the stamp filter.

    Returns:
        The configured environment.
    """
    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATE_DIR),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.filters["network_label"] = lambda value: NETWORK_LABELS.get(value, value)
    environment.filters["plural"] = lambda number_, word: (
        f"{number_} {word}"
        if number_ == 1
        else f"{number_} {word[:-1] + 'ies' if word.endswith('y') else word + 's'}"
    )
    environment.filters["stamp"] = lambda value: stamp(value, time_format)
    environment.filters["span"] = span
    environment.filters["device"] = device
    environment.globals["asset"] = urls.__getitem__
    environment.globals["task_icon"] = task_icon
    environment.globals["task_icon_set"] = task_icon_set
    environment.globals["logo"] = logo_markup
    environment.globals["tie"] = tie_markup
    environment.globals["placeholders"] = PLACEHOLDERS
    return environment


def create_app(settings: Settings, web: WebSettings, open_services: ServicesFactory) -> Starlette:
    """Build the web application with all services, routes and middleware.

    The shared WebContext is stored on app.state.ctx.

    Args:
        settings: Application settings.
        web: Web server settings.
        open_services: Factory that opens the research services at startup.

    Returns:
        The Starlette application.
    """
    served, urls = build_assets(STATIC_DIR, web)
    store = Store(settings.store)
    auth = settings.auth
    finance = FinanceBook(store, settings.finance)
    vault = Vault(auth.key_file)
    mailer = Mailer(settings, store)
    candidates = Candidates(store, settings.candidates)
    routines = Routines(store, settings.routines)
    accounts = Accounts(store, auth)
    ctx = WebContext(
        settings=settings,
        web=web,
        sessions=SessionStore(web),
        jobs=JobManager(settings.output_dir, web, tuning=settings.events, store=store),
        templates=build_templates(urls, web.time_format),
        login_limiter=RateLimiter(web.login_attempts, web.login_window_seconds),
        submit_limiter=RateLimiter(web.submissions, web.submission_window_seconds),
        session_limiter=RateLimiter(web.anonymous_sessions, web.anonymous_session_window_seconds),
        api_limiter=RateLimiter(web.api_requests, web.api_window_seconds),
        api_failure_limiter=RateLimiter(web.api_failures, web.api_failure_window_seconds),
        voice_limiter=RateLimiter(web.voice_requests, web.voice_window_seconds),
        chat_limiter=RateLimiter(web.chat_requests, web.chat_window_seconds),
        mail_limiter=RateLimiter(web.mail_requests, web.mail_window_seconds),
        store=store,
        accounts=accounts,
        hasher=PasswordHasher(auth),
        vault=vault,
        policy=PolicyBook(store, auth.policy),
        mailer=mailer,
        portability=Portability(
            store,
            candidates,
            finance,
            routines,
            lambda: mailer,
            accounts,
            lambda: TemplateStore(settings.email),
            settings.portability,
        ),
        user_login_limiter=RateLimiter(web.user_login_attempts, web.login_window_seconds),
        user_ceiling_limiter=RateLimiter(web.user_login_ceiling, web.login_window_seconds),
        second_factor_limiter=RateLimiter(
            web.second_factor_attempts, web.second_factor_window_seconds
        ),
        link_limiter=RateLimiter(web.link_attempts, web.link_window_seconds),
        account_limiter=RateLimiter(web.account_actions, web.account_window_seconds),
        assets=served,
        status_cache=TTLCache(web.status_cache_seconds, 4),
        candidates=candidates,
        routines=routines,
        finance=finance,
        stats=Stats(store, finance, settings.stats),
        messenger=Messenger(store, vault, settings.messages),
        message_limiter=RateLimiter(settings.messages.send_per_minute, 60),
        runner=None,
        connections=Connections(),
        checker=ConnectionChecker(),
        setup_code=secrets.token_urlsafe(web.setup_code_bytes),
    )

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        """Run startup work before serving and cleanup after.

        Startup syncs roles, registers job observers, recovers routines, opens the services,
        purges expired data and starts the routine runner. Shutdown stops the runner, jobs
        and services, then closes the store.

        Args:
            _: The application, unused.

        Returns:
            An async iterator that yields once while the app is serving.
        """
        ctx.accounts.sync_roles(auth.roles)
        ctx.jobs.observers.append(lambda job: record_research(ctx, job))
        ctx.jobs.observers.append(lambda job: routine_settled(ctx, job))
        ctx.routines.recover()
        if not ctx.accounts.needs_setup():
            ctx.setup_code = None
        if ctx.setup_code is not None:
            logger.warning(
                "Open %s/setup?code=%s to create the first account.", ctx.base_url, ctx.setup_code
            )
        ctx.host = ServiceHost(ctx, open_services)
        await ctx.host.start()
        store.purge()
        ctx.accounts.purge()
        ctx.mailer.purge()
        ctx.candidates.purge()
        ctx.messenger.purge()
        ctx.runner = RoutineRunner(ctx)
        ctx.runner.start()
        try:
            yield
        finally:
            await ctx.runner.stop()
            await ctx.jobs.close()
            await ctx.host.stop()
            store.close()

    routes = [
        Route("/", home, methods=["GET"]),
        Route("/c/{thread_id}", chat_page, methods=["GET"]),
        Route("/chat/send", chat_send, methods=["POST"]),
        Route("/chat/{thread_id}/messages", chat_messages, methods=["GET"]),
        Route("/chat/{thread_id}/delete", chat_delete, methods=["POST"]),
        Route("/chat/messages/{message_id}/speech", chat_speech, methods=["GET"]),
        Route("/tie/messages", tie_messages, methods=["GET"]),
        Route("/tie/send", tie_send, methods=["POST"]),
        Route("/tie/clear", tie_clear, methods=["POST"]),
        Route("/emails", emails_page, methods=["GET"]),
        Route("/emails/preview", emails_preview, methods=["POST"]),
        Route("/emails/compose", compose_page, methods=["GET"]),
        Route("/emails/send", mail_send, methods=["POST"]),
        Route("/emails/log", mail_log, methods=["GET"]),
        Route("/emails/suppress", suppress_add, methods=["POST"]),
        Route("/emails/suppress/remove", suppress_remove, methods=["POST"]),
        Route("/emails/templates", emails_save, methods=["POST"]),
        Route("/emails/templates/{template_id}/delete", emails_delete, methods=["POST"]),
        Route("/settings", settings_page, methods=["GET"]),
        Route("/settings", save_settings, methods=["POST"]),
        Route("/settings/reset", reset_settings, methods=["POST"]),
        Route("/settings/requirements/remove", remove_requirement, methods=["POST"]),
        Route("/settings/data/delete", delete_data, methods=["POST"]),
        Route("/live-view", choose_live_view, methods=["POST"]),
        Route("/buddy", choose_buddy, methods=["POST"]),
        Route("/buddy/speech/{line}", buddy_line, methods=["GET"]),
        Route("/api", api_page, methods=["GET"]),
        Route("/jobs/{job_id}", job_page, methods=["GET"]),
        Route("/jobs/{job_id}/status", job_status, methods=["GET"]),
        Route("/jobs/{job_id}/events", job_events, methods=["GET"]),
        Route("/jobs/{job_id}/delete", delete_job, methods=["POST"]),
        Route("/jobs/{job_id}/download/{fmt}", download, methods=["GET"]),
        Route("/login", login_page, methods=["GET"]),
        Route("/login", login, methods=["POST"]),
        Route("/login/verify", verify_page, methods=["GET"]),
        Route("/login/verify", verify, methods=["POST"]),
        Route("/login/forgot", forgot, methods=["POST"]),
        Route("/login/link", link_request, methods=["POST"]),
        Route("/invite/{token}", invite_page, methods=["GET"]),
        Route("/invite/{token}", invite_accept, methods=["POST"]),
        Route("/reset/{token}", reset_page, methods=["GET"]),
        Route("/reset/{token}", reset_accept, methods=["POST"]),
        Route("/l/{token}", signin_page, methods=["GET"]),
        Route("/l/{token}", signin_accept, methods=["POST"]),
        Route("/setup", setup_page, methods=["GET"]),
        Route("/setup", setup_create, methods=["POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/account", account_page, methods=["GET"]),
        Route("/account/profile", account_profile, methods=["POST"]),
        Route("/account/password", change_password, methods=["POST"]),
        Route("/account/email/code", email_send_code, methods=["POST"]),
        Route("/account/email/confirm", email_confirm, methods=["POST"]),
        Route("/account/email-login", email_login, methods=["POST"]),
        Route("/account/sessions/revoke", sessions_revoke, methods=["POST"]),
        Route("/account/2fa", two_factor_page, methods=["GET"]),
        Route("/account/2fa/enable", two_factor_enable, methods=["POST"]),
        Route("/account/2fa/recovery", two_factor_recovery, methods=["POST"]),
        Route("/account/2fa/disable", two_factor_disable, methods=["POST"]),
        Route("/account/keys", key_create, methods=["POST"]),
        Route("/account/keys/{key_id}/revoke", key_revoke, methods=["POST"]),
        Route("/candidates", candidates_page, methods=["GET"]),
        Route("/candidates", candidate_create, methods=["POST"]),
        Route("/candidates/new", candidate_new, methods=["GET"]),
        Route("/candidates/{candidate_id}", candidate_page, methods=["GET"]),
        Route("/candidates/{candidate_id}", candidate_update, methods=["POST"]),
        Route("/candidates/{candidate_id}/note", candidate_note, methods=["POST"]),
        Route("/candidates/{candidate_id}/links", link_add, methods=["POST"]),
        Route("/candidates/{candidate_id}/links/remove", link_remove, methods=["POST"]),
        Route("/candidates/{candidate_id}/contacts", contact_add, methods=["POST"]),
        Route(
            "/candidates/{candidate_id}/contacts/{contact_id}/remove",
            contact_remove,
            methods=["POST"],
        ),
        Route("/candidates/{candidate_id}/hires", hire_create, methods=["POST"]),
        Route("/candidates/{candidate_id}/hires/{hire_id}", hire_update, methods=["POST"]),
        Route("/candidates/{candidate_id}/hires/{hire_id}/delete", hire_delete, methods=["POST"]),
        Route("/candidates/{candidate_id}/delete", candidate_delete, methods=["POST"]),
        Route("/candidates/{candidate_id}/export", candidate_export, methods=["GET"]),
        Route("/jobs/{job_id}/candidate", job_candidate, methods=["POST"]),
        Route("/jobs/{job_id}/routine", job_routine, methods=["POST"]),
        Route("/messages", messages_page, methods=["GET"]),
        Route("/messages/role", role_channel, methods=["POST"]),
        Route("/messages/direct", direct_create, methods=["POST"]),
        Route("/messages/groups", group_create, methods=["POST"]),
        Route("/messages/unread", unread_json, methods=["GET"]),
        Route("/messages/keys", keys_get, methods=["GET"]),
        Route("/messages/keys", keys_save, methods=["POST"]),
        Route("/messages/keys/{user_id}", key_of, methods=["GET"]),
        Route("/messages/c/{conversation_id}/feed", feed_json, methods=["GET"]),
        Route("/messages/c/{conversation_id}/send", send_json, methods=["POST"]),
        Route("/messages/c/{conversation_id}/read", read_json, methods=["POST"]),
        Route("/messages/c/{conversation_id}/keys", conversation_keys, methods=["GET"]),
        Route("/messages/c/{conversation_id}/peers", peers_json, methods=["GET"]),
        Route("/messages/c/{conversation_id}/rotate", rotate_json, methods=["POST"]),
        Route("/messages/c/{conversation_id}/members", group_members, methods=["POST"]),
        Route("/messages/c/{conversation_id}/leave", group_leave, methods=["POST"]),
        Route("/messages/c/{conversation_id}/rename", group_rename, methods=["POST"]),
        Route("/messages/c/{conversation_id}/delete", group_delete, methods=["POST"]),
        Route("/messages/m/{message_id}/remove", message_remove, methods=["POST"]),
        Route("/announcements", announcements_page, methods=["GET"]),
        Route("/announcements", announcement_create, methods=["POST"]),
        Route("/announcements/{announcement_id}/read", announcement_read, methods=["POST"]),
        Route("/announcements/{announcement_id}/delete", announcement_delete, methods=["POST"]),
        Route("/dashboard", dashboard_page, methods=["GET"]),
        Route("/dashboard/{topic}/{index}.svg", chart_svg, methods=["GET"]),
        Route("/finance", finance_page, methods=["GET"]),
        Route("/finance/export.csv", finance_csv, methods=["GET"]),
        Route("/finance/entries", entry_create, methods=["POST"]),
        Route("/finance/entries/{entry_id}", entry_update, methods=["POST"]),
        Route("/finance/entries/{entry_id}/delete", entry_delete, methods=["POST"]),
        Route("/finance/categories", category_create, methods=["POST"]),
        Route("/finance/categories/{category_id}", category_update, methods=["POST"]),
        Route("/finance/categories/{category_id}/delete", category_delete, methods=["POST"]),
        Route("/finance/currency", currency_save, methods=["POST"]),
        Route("/routines", routines_page, methods=["GET"]),
        Route("/routines", routine_create, methods=["POST"]),
        Route("/routines/{routine_id}", routine_page, methods=["GET"]),
        Route("/routines/{routine_id}", routine_update, methods=["POST"]),
        Route("/routines/{routine_id}/pause", routine_pause, methods=["POST"]),
        Route("/routines/{routine_id}/resume", routine_resume, methods=["POST"]),
        Route("/routines/{routine_id}/run", routine_run, methods=["POST"]),
        Route("/routines/{routine_id}/delete", routine_delete, methods=["POST"]),
        Route("/admin", admin_home, methods=["GET"]),
        Route("/admin/users", users_page, methods=["GET"]),
        Route("/admin/users", user_create, methods=["POST"]),
        Route("/admin/users/{user_id}", user_page, methods=["GET"]),
        Route("/admin/users/{user_id}", user_update, methods=["POST"]),
        Route("/admin/users/{user_id}/link", user_link, methods=["POST"]),
        Route("/admin/users/{user_id}/sessions", user_sessions, methods=["POST"]),
        Route("/admin/users/{user_id}/keys/{key_id}/revoke", user_key_revoke, methods=["POST"]),
        Route("/admin/users/{user_id}/2fa", user_two_factor, methods=["POST"]),
        Route("/admin/users/{user_id}/delete", user_delete, methods=["POST"]),
        Route("/admin/roles", roles_page, methods=["GET"]),
        Route("/admin/roles", role_save, methods=["POST"]),
        Route("/admin/roles/{role_id}/delete", role_delete, methods=["POST"]),
        Route("/admin/audit", audit_page, methods=["GET"]),
        Route("/admin/audit.csv", audit_csv, methods=["GET"]),
        Route("/admin/connections", connections_page, methods=["GET"]),
        Route("/admin/connections/test", connections_test, methods=["POST"]),
        Route("/admin/connections/{section}", connections_save, methods=["POST"]),
        Route("/admin/data", data_page, methods=["GET"]),
        Route("/admin/data/export.json", export_json, methods=["GET"]),
        Route("/admin/data/{dataset}.csv", export_csv, methods=["GET"]),
        Route("/admin/import", import_page, methods=["GET"]),
        Route("/admin/import", import_preview, methods=["POST"]),
        Route("/admin/import/confirm", import_confirm, methods=["POST"]),
        Route("/admin/security", security_page, methods=["GET"]),
        Route("/admin/security", security_save, methods=["POST"]),
        Route("/voice/transcribe", voice_transcribe, methods=["POST"]),
        Route("/jobs/{job_id}/speech", job_speech, methods=["GET"]),
        Route("/robots.txt", robots, methods=["GET"]),
        Route("/static/{name}", static_file, methods=["GET"]),
        *api_routes(),
    ]
    # The 4/3 factor leaves room for the larger encoded size of a CV upload.
    largest = max(int(settings.cv.max_bytes * 4 / 3), settings.voice.max_audio_bytes)
    max_body = largest + web.body_overhead_bytes
    import_body = settings.portability.max_bytes + web.body_overhead_bytes
    middleware = [
        Middleware(SecurityHeadersMiddleware, web=web),
        Middleware(TrustedHostMiddleware, allowed_hosts=web.hosts),
        Middleware(
            BodyLimitMiddleware,
            max_bytes=max_body,
            overrides={"/admin/import": import_body, "/api/v1/import": import_body},
        ),
    ]
    app = Starlette(
        routes=routes,
        middleware=middleware,
        exception_handlers={HTTPException: http_error},
        lifespan=lifespan,
    )
    app.state.ctx = ctx
    return app
