# src/agent/web/automation.py
from agent.routines import (
    ACTIVE,
    EXHAUSTED,
    FAILED,
    Finished,
    PAUSED,
    Routine,
    SATISFIED,
)
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
    TooManyJobs,
    WEB_OWNER,
)
from agent.errors import (
    AgentError,
    ComplianceRefusal,
)
from agent.web.context import (
    context_of,
    WebContext,
)
from starlette.responses import (
    RedirectResponse,
    Response,
)
from agent.compliance import check_skill_request
from starlette.exceptions import HTTPException
from starlette.datastructures import FormData
from agent.models import SkillSearchReport
from starlette.requests import Request
from agent.web.auth import background
from agent.chat import load_profile
from agent.mailer import SYSTEM

import asyncio
import logging

logger = logging.getLogger(__name__)
MODULE = "routines_enabled"
NOTES = {
    "saved": "Saved.",
    "created": "The routine is set. The first run starts in a moment.",
    "paused": "The routine is paused.",
    "resumed": "The routine is running again.",
    "queued": "A run was queued.",
    "removed": "The routine was removed.",
}
HEADLINES = {
    SATISFIED: "A routine found what you were looking for",
    EXHAUSTED: "A routine used all its runs without a perfect match",
    FAILED: "A routine stopped after repeated failures",
}


def routine_or_404(request: Request) -> Routine:
    """Return the routine named in the request path.

    Args:
        request: The incoming request with a routine_id path parameter.

    Returns:
        The matching routine.

    Raises:
        HTTPException: With status 404 when the routine does not exist.
    """
    found = context_of(request).routines.get(request.path_params["routine_id"])
    if found is None:
        raise HTTPException(404)
    return found


def base_of(ctx: WebContext) -> str:
    """Return the origin used to build absolute links in notices.

    Args:
        ctx: The web context.

    Returns:
        The configured public origin, or the request base URL when none is set.
    """
    return ctx.web.public_origin or ctx.base_url


def message_for(ctx: WebContext, finished: Finished) -> tuple[str, str]:
    """Build the subject and plain-text body of a routine notice.

    The body lists the routine, the run summary, up to the configured number of
    matches, and links to the report and the routine.

    Args:
        ctx: The web context.
        finished: The finished run with its routine and outcome.

    Returns:
        The subject and the body. The subject depends on the routine status.
    """
    routine = finished.routine
    link = f"{base_of(ctx)}/routines/{routine.id}"
    lines = [f"Routine: {routine.name}", finished.run.summary]
    if finished.outcome is not None and finished.outcome.matches:
        lines.append("")
        lines.extend(
            f"- {item.name}, rated {item.rating:.1f} / 5: {item.url}"
            for item in finished.outcome.matches[: ctx.settings.routines.matches_listed]
        )
    lines.extend(["", f"Report: {base_of(ctx)}/jobs/{finished.job_id}", f"Routine: {link}"])
    return HEADLINES.get(routine.status, ctx.settings.mail.system_subjects["routine"]), "\n".join(
        lines
    )


async def send_notices(ctx: WebContext, finished: Finished) -> None:
    """Email the routine's notify addresses once the routine has settled.

    Nothing is sent while the routine is still active, when it has no addresses,
    or when mail is not configured. A failed send is logged by error type only.

    Args:
        ctx: The web context.
        finished: The finished run with its routine and outcome.
    """
    routine = finished.routine
    if routine.status == ACTIVE or not routine.notify_emails or not ctx.mailer.configured():
        return
    subject, body = message_for(ctx, finished)
    for address in routine.notify_emails:
        try:
            await ctx.mailer.send(address, subject, body, kind=SYSTEM, purpose="routine")
        except AgentError as error:
            logger.warning("a routine notice could not be sent: %s", type(error).__name__)


def routine_settled(ctx: WebContext, job: Job) -> None:
    """Record a finished skill job against its routine and send notices.

    Jobs of other kinds are ignored. The report is passed on only when the job
    ended in the done state, and the notices are sent in the background.

    Args:
        ctx: The web context.
        job: The job that reached a final state.
    """
    if job.kind != "skill":
        return
    report = job.report if isinstance(job.report, SkillSearchReport) else None
    if job.state is not JobState.DONE:
        report = None
    finished = ctx.routines.finish(job.id, report, job.error or "")
    if finished is None:
        return
    background(send_notices(ctx, finished))


class RoutineRunner:
    """A background loop that starts due routines as skill search jobs."""

    def __init__(self, ctx: WebContext):
        """Initialize the runner.

        Args:
            ctx: The web context.
        """
        self._ctx = ctx
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the polling loop as an asyncio task."""
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Cancel the polling loop and wait until it has finished."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _loop(self) -> None:
        """Run tick at the configured poll interval until cancelled.

        A failing tick is logged by error type only and does not stop the loop.
        """
        while True:
            await asyncio.sleep(self._ctx.settings.routines.poll_seconds)
            try:
                self.tick()
            except Exception as error:
                logger.error("a routine tick failed with %s", type(error).__name__)

    def tick(self) -> int:
        """Start due routines while the parallel and daily limits allow.

        Nothing starts when routines are disabled by policy or jobs are not ready.

        Returns:
            The number of routines started.
        """
        ctx = self._ctx
        tuning = ctx.settings.routines
        if not (ctx.policy.get().routines_enabled and ctx.jobs.ready):
            return 0
        started = 0
        while (
            ctx.routines.running() < tuning.max_parallel
            and ctx.routines.runs_today() < tuning.max_runs_per_day
        ):
            due = ctx.routines.due(1)
            if not due or not self._start(due[0]):
                break
            started += 1
        return started

    def _start(self, routine: Routine) -> bool:
        """Start the skill search job for one routine.

        A routine that fails the compliance check is paused. A job limit or other
        agent error leaves the routine due for a later tick. When the routine can no
        longer be claimed, the job that was just created is deleted.

        Args:
            routine: The due routine.

        Returns:
            True when a job was started and recorded for the routine.
        """
        ctx = self._ctx
        request = ctx.routines.request_of(routine, load_profile(ctx.store).company())
        try:
            check_skill_request(request)
            job = ctx.jobs.start_skill_search(WEB_OWNER, request)
        except ComplianceRefusal as error:
            ctx.routines.set_status(routine.id, PAUSED)
            logger.warning("a routine was paused: %s", error)
            return False
        # Python 3.14 allows several exception types here without parentheses.
        except TooManyJobs, AgentError:
            return False
        # begin claims the routine atomically and fails if it was paused or started meanwhile, so
        # the new job is discarded.
        if ctx.routines.begin(routine.id, job.id) is None:
            ctx.jobs.delete(job)
            return False
        return True


def routines_view(
    request: Request,
    status: int = 200,
    *,
    errors: list[str] | None = None,
    values: dict[str, str] | None = None,
) -> Response:
    """Render the routines list page.

    Args:
        request: The incoming request. Its done query value selects a notice.
        status: The HTTP status code of the response.
        errors: Error messages to show.
        values: Form values to refill.

    Returns:
        The rendered page.
    """
    ctx = context_of(request)
    tuning = ctx.settings.routines
    note = NOTES.get(request.query_params.get("done", ""))
    profile = load_profile(ctx.store)
    return render(
        request,
        "routines.html",
        status,
        active="routines",
        routines=ctx.routines.all(),
        errors=errors or [],
        notes=[note] if note else [],
        values=values or {},
        intervals=[(str(hours), every(hours)) for hours in tuning.interval_choices],
        defaults=tuning,
        standing=len(profile.requirements),
        mail_ready=ctx.mailer.configured(),
        runs_today=ctx.routines.runs_today(),
    )


def every(hours: int) -> str:
    """Describe a repeat interval as a label such as Every 2 days.

    Args:
        hours: The interval in hours.

    Returns:
        The label, in whole weeks, days or hours.
    """
    if hours % 168 == 0:
        return "Every week" if hours == 168 else f"Every {hours // 168} weeks"
    if hours % 24 == 0:
        return "Every day" if hours == 24 else f"Every {hours // 24} days"
    return "Every hour" if hours == 1 else f"Every {hours} hours"


@requires("routines.view", MODULE)
async def routines_page(request: Request) -> Response:
    """Show the list of routines.

    Args:
        request: The incoming request.

    Returns:
        The rendered page.
    """
    return routines_view(request)


def read_settings(ctx: WebContext, form: FormData) -> tuple[dict[str, str], list[str]]:
    """Read the routine form fields as trimmed, length-limited text.

    Args:
        ctx: The web context.
        form: The submitted form.

    Returns:
        The field values and an empty list of errors for the caller to fill.
    """
    tuning = ctx.settings.routines
    values = {
        "name": text(form, "name", tuning.max_name_chars),
        "skill": text(form, "skill", 120),
        "location": text(form, "location", 120),
        "min_rating": text(form, "min_rating", 5),
        "interval": text(form, "interval", 6),
        "max_runs": text(form, "max_runs", 5),
        "need_count": text(form, "need_count", 3),
        "size": text(form, "size", 3),
        "notify": text(form, "notify", 600),
        "musts": text(form, "musts", 4),
        "standing": text(form, "standing", 4),
        "purpose": text(form, "purpose", 4),
    }
    errors: list[str] = []
    return values, errors


@requires("routines.manage", MODULE)
async def routine_create(request: Request) -> Response:
    """Create a routine from the submitted form.

    The form is checked for forgery protection. The routine can reuse the standing
    requirements of the hiring profile, and the purpose can be confirmed. The
    action is written to the audit log.

    Args:
        request: The incoming form submission.

    Returns:
        A redirect to the new routine, or the list page with status 400 and the
        problems found.
    """
    ctx = context_of(request)
    form = await protected_form(request, request.state.session)
    try:
        values, errors = read_settings(ctx, form)
    finally:
        await form.close()
    rating = number(values["min_rating"], float, "The minimum rating", errors)
    hours = number(values["interval"], int, "The interval", errors)
    runs = number(values["max_runs"], int, "The number of runs", errors)
    needed = number(values["need_count"], int, "Matches needed", errors)
    size = number(values["size"], int, "Candidates per run", errors)
    if errors:
        return routines_view(request, 400, errors=errors, values=values)
    profile = load_profile(ctx.store)
    try:
        created = ctx.routines.create(
            values["name"],
            values["skill"],
            values["location"],
            size=size,
            requirements=profile.requirements if values["standing"] == "yes" else [],
            min_rating=rating,
            need_musts=values["musts"] == "yes",
            need_count=needed or 1,
            interval_hours=hours,
            max_runs=runs,
            notify_emails=items(values["notify"]),
            purpose_confirmed=values["purpose"] == "yes",
            company=profile.company(),
            actor=request.state.principal.name,
        )
    except AgentError as error:
        return routines_view(request, 400, errors=[str(error)], values=values)
    audit(request, "routine.create", target=created.id)
    return RedirectResponse(f"/routines/{created.id}?done=created", status_code=303)


def detail_view(
    request: Request, routine: Routine, status: int = 200, errors: list[str] | None = None
) -> Response:
    """Render the page of one routine.

    Args:
        request: The incoming request. Its done query value selects a notice.
        routine: The routine to show.
        status: The HTTP status code of the response.
        errors: Error messages to show.

    Returns:
        The rendered page.
    """
    ctx = context_of(request)
    tuning = ctx.settings.routines
    note = NOTES.get(request.query_params.get("done", ""))
    return render(
        request,
        "routine.html",
        status,
        active="routines",
        routine=routine,
        runs=ctx.routines.runs(routine.id),
        intervals=[(str(hours), every(hours)) for hours in tuning.interval_choices],
        every=every(routine.interval_hours),
        errors=errors or [],
        notes=[note] if note else [],
        mail_ready=ctx.mailer.configured(),
    )


@requires("routines.view", MODULE)
async def routine_page(request: Request) -> Response:
    """Show one routine with its recent runs.

    Args:
        request: The incoming request.

    Returns:
        The rendered page.
    """
    return detail_view(request, routine_or_404(request))


@requires("routines.manage", MODULE)
async def routine_update(request: Request) -> Response:
    """Update the editable settings of a routine from the submitted form.

    An empty name keeps the current name. The action is written to the audit log.

    Args:
        request: The incoming form submission.

    Returns:
        A redirect to the routine, or its page with status 400 and the problems.
    """
    ctx = context_of(request)
    routine = routine_or_404(request)
    form = await protected_form(request, request.state.session)
    try:
        values, errors = read_settings(ctx, form)
    finally:
        await form.close()
    rating = number(values["min_rating"], float, "The minimum rating", errors)
    hours = number(values["interval"], int, "The interval", errors)
    runs = number(values["max_runs"], int, "The number of runs", errors)
    needed = number(values["need_count"], int, "Matches needed", errors)
    if errors:
        return detail_view(request, routine, 400, errors)
    try:
        ctx.routines.update(
            routine.id,
            name=values["name"] or None,
            min_rating=rating,
            need_musts=values["musts"] == "yes",
            need_count=needed,
            interval_hours=hours,
            max_runs=runs,
            notify_emails=items(values["notify"]),
        )
    except AgentError as error:
        return detail_view(request, routine, 400, [str(error)])
    audit(request, "routine.update", target=routine.id)
    return RedirectResponse(f"/routines/{routine.id}?done=saved", status_code=303)


@requires("routines.manage", MODULE)
async def routine_pause(request: Request) -> Response:
    """Pause a routine so it stops running.

    Args:
        request: The incoming form submission.

    Returns:
        A redirect to the routine.
    """
    ctx = context_of(request)
    routine = routine_or_404(request)
    form = await protected_form(request, request.state.session)
    await form.close()
    ctx.routines.set_status(routine.id, PAUSED)
    audit(request, "routine.pause", target=routine.id)
    return RedirectResponse(f"/routines/{routine.id}?done=paused", status_code=303)


@requires("routines.manage", MODULE)
async def routine_resume(request: Request) -> Response:
    """Make a paused routine active again.

    Args:
        request: The incoming form submission.

    Returns:
        A redirect to the routine, or its page with status 400 when it cannot be
        resumed, for example because too many routines are active.
    """
    ctx = context_of(request)
    routine = routine_or_404(request)
    form = await protected_form(request, request.state.session)
    await form.close()
    try:
        ctx.routines.set_status(routine.id, ACTIVE)
    except AgentError as error:
        return detail_view(request, routine, 400, [str(error)])
    audit(request, "routine.resume", target=routine.id)
    return RedirectResponse(f"/routines/{routine.id}?done=resumed", status_code=303)


@requires("routines.manage", MODULE)
async def routine_run(request: Request) -> Response:
    """Queue a run of a routine as soon as possible.

    Args:
        request: The incoming form submission.

    Returns:
        A redirect to the routine, its page with status 400 when the routine is
        not active, or status 429 when the daily limit of runs is reached.
    """
    ctx = context_of(request)
    routine = routine_or_404(request)
    form = await protected_form(request, request.state.session)
    await form.close()
    if routine.status != ACTIVE:
        return detail_view(request, routine, 400, ["Resume the routine first."])
    if ctx.routines.runs_today() >= ctx.settings.routines.max_runs_per_day:
        return detail_view(request, routine, 429, ["The daily limit of runs was reached."])
    ctx.routines.queue(routine.id)
    audit(request, "routine.run", target=routine.id)
    return RedirectResponse(f"/routines/{routine.id}?done=queued", status_code=303)


@requires("routines.manage", MODULE)
async def routine_delete(request: Request) -> Response:
    """Delete a routine.

    The action is written to the audit log with the routine name.

    Args:
        request: The incoming form submission.

    Returns:
        A redirect to the list of routines.
    """
    ctx = context_of(request)
    routine = routine_or_404(request)
    form = await protected_form(request, request.state.session)
    await form.close()
    ctx.routines.delete(routine.id)
    audit(request, "routine.delete", target=routine.id, detail={"name": routine.name})
    return RedirectResponse("/routines?done=removed", status_code=303)


@requires("routines.manage", MODULE)
async def job_routine(request: Request) -> Response:
    """Create a routine that repeats a finished skill search job.

    The skill, location, options and size come from the report. The requirements
    come from the first candidate that has requirement results. The caller needs
    the reports.view permission as well.

    Args:
        request: The incoming form submission with a job_id path parameter.

    Returns:
        A redirect to the new routine, or the list page with status 400 and the
        problem found.

    Raises:
        HTTPException: With status 404 when the job has no skill search report or
            the caller may not view reports.
    """
    ctx = context_of(request)
    job = ctx.jobs.get(request.path_params["job_id"], WEB_OWNER)
    report = job.report if job is not None else None
    if not isinstance(report, SkillSearchReport) or not request.state.principal.can("reports.view"):
        raise HTTPException(404)
    form = await protected_form(request, request.state.session)
    try:
        raw = text(form, "interval", 6)
        rating = text(form, "min_rating", 5)
    finally:
        await form.close()
    errors: list[str] = []
    hours = number(raw, int, "The interval", errors)
    minimum = number(rating, float, "The minimum rating", errors)
    requirements = []
    for candidate in report.candidates:
        if candidate.requirements is not None:
            requirements = [item.requirement for item in candidate.requirements.results]
            break
    try:
        if errors:
            raise AgentError(errors[0])
        created = ctx.routines.create(
            "",
            report.skill,
            report.location,
            size=len(report.candidates) or None,
            requirements=requirements,
            options=report.options,
            min_rating=minimum,
            interval_hours=hours,
            purpose_confirmed=report.purpose_confirmed,
            company=load_profile(ctx.store).company(),
            actor=request.state.principal.name,
        )
    except AgentError as error:
        return routines_view(request, 400, errors=[str(error)])
    audit(request, "routine.create", target=created.id, detail={"from": job.id if job else ""})
    return RedirectResponse(f"/routines/{created.id}?done=created", status_code=303)
