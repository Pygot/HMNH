# src/agent/web/jobs.py
from agent.models import (
    Clarification,
    ClarificationAnswer,
    CompanyInput,
    Goal,
    Identity,
    Network,
    PersonReport,
    Requirement,
    ResearchOptions,
    ResearchRequest,
    SkillSearchReport,
    SkillSearchRequest,
)
from agent.export import (
    delete_exported,
    export_report,
    ExportedReport,
    render_json,
    render_markdown,
    Report,
)
from agent.events import (
    Event,
    Kind,
    Reporter,
    Task,
)
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
)
from pydantic import (
    Field,
    TypeAdapter,
    ValidationError,
)
from typing import (
    Annotated,
    Any,
    Literal,
)
from agent.errors import (
    AgentError,
    ConfigError,
)
from agent.pipeline import (
    apply_answers,
    Researcher,
)
from agent.store import (
    JobRecord,
    Store,
)
from dataclasses import (
    dataclass,
    field,
)
from agent.web.settings import WebSettings
from agent.config import EventTuning
from enum import StrEnum
from pathlib import Path

import asyncio
import logging
import secrets
import time

logger = logging.getLogger(__name__)
API_OWNER = "api"
WEB_OWNER = "web"
UNEXPECTED_ERROR = "An unexpected error occurred."
INTERRUPTED = "The search was interrupted by a restart."
REPORTS = TypeAdapter(Annotated[PersonReport | SkillSearchReport, Field(discriminator="mode")])
Settled = Callable[["Job"], None]
Work = Callable[[Researcher, Reporter], Awaitable[PersonReport | Clarification | SkillSearchReport]]
TERMINAL_KINDS = (Kind.DONE, Kind.FAILED, Kind.QUESTION)


class JobState(StrEnum):
    """The lifecycle state of a research job."""

    RUNNING = "running"
    NEEDS_INPUT = "needs_input"
    DONE = "done"
    FAILED = "failed"


def _clipped(value: Any, limit: int) -> Any:
    """Return a copy of a value with long strings and lists shortened.

    Strings are cut to twice the limit, lists keep their first 50 items and dict values
    are clipped recursively. Other values are returned unchanged.

    Args:
        value: The value to clip.
        limit: Base character limit; strings keep at most twice this many characters.

    Returns:
        The clipped copy of the value.
    """
    if isinstance(value, str):
        return value[: limit * 2]
    if isinstance(value, list):
        return [_clipped(item, limit) for item in value[:50]]
    if isinstance(value, dict):
        return {key: _clipped(item, limit) for key, item in value.items()}
    return value


class TooManyJobs(AgentError):
    """Raised when an owner already has the maximum number of running jobs."""

    pass


@dataclass(frozen=True)
class CVUpload:
    """An uploaded CV file held in memory."""

    data: bytes
    filename: str


@dataclass
class Job:
    """A research job with its progress, events and result.

    Jobs live in memory inside JobManager and are mirrored to the store when one is
    configured. A job belongs to exactly one owner.
    """

    id: str
    owner: str
    kind: Literal["person", "skill"]
    title: str
    created: float
    state: JobState = JobState.RUNNING
    stage: str = "Queued"
    task: Task | None = None
    events: list[Event] = field(default_factory=list)
    request: ResearchRequest | None = None
    clarification: Clarification | None = None
    report: Report | None = None
    exported: ExportedReport | None = None
    error: str | None = None
    thread_id: str | None = None
    stored_rating: float | None = None

    @property
    def rating(self) -> float | None:
        """Return the overall rating of the job's report, if known."""
        if isinstance(self.report, PersonReport):
            return self.report.rating.overall
        if isinstance(self.report, SkillSearchReport) and self.report.candidates:
            best = next(
                (item for item in self.report.candidates if item.best), self.report.candidates[0]
            )
            return best.rating.overall
        return self.stored_rating


class JobManager:
    """Run research jobs in the background and track their progress.

    Jobs are scoped to an owner and are only handed back to that owner. A semaphore
    limits how many jobs run at the same time, and each owner has its own cap on
    running jobs.
    """

    def __init__(
        self,
        output_dir: Path,
        web: WebSettings,
        clock: Callable[[], float] = time.time,
        tuning: EventTuning | None = None,
        store: Store | None = None,
    ):
        """Initialize the manager.

        Args:
            output_dir: Directory where finished reports are exported.
            web: Web settings with the job TTL, concurrency limits and job id size.
            clock: Function returning the current time in seconds.
            tuning: Event limits and streaming intervals; defaults are used when omitted.
            store: Optional store used to persist jobs across restarts.
        """
        self._output_dir = output_dir
        self._events = tuning or EventTuning()
        self._ttl = web.job_ttl_seconds
        self._max_concurrent = web.max_concurrent_jobs
        self._max_per_owner = web.max_jobs_per_session
        self._id_bytes = web.job_id_bytes
        self._clock = clock
        self._store = store
        self.on_settled: Settled | None = None
        self.observers: list[Settled] = []
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._pipeline: Researcher | None = None
        self._gate: asyncio.Semaphore | None = None
        self._frozen = False

    def attach(self, pipeline: Researcher) -> None:
        """Attach the research pipeline and allow jobs to run.

        Args:
            pipeline: The researcher that jobs will use.
        """
        self._pipeline = pipeline
        self._gate = asyncio.Semaphore(self._max_concurrent)

    def detach(self) -> None:
        """Detach the research pipeline so new searches are refused."""
        self._pipeline = None
        self._gate = None

    def freeze(self) -> None:
        """Refuse new searches until thaw is called."""
        self._frozen = True

    def thaw(self) -> None:
        """Allow new searches again after a freeze."""
        self._frozen = False

    def busy(self) -> bool:
        """Check whether any job is currently running.

        Returns:
            True when at least one job is in the running state.
        """
        return any(job.state is JobState.RUNNING for job in self._jobs.values())

    async def close(self) -> None:
        """Cancel all running job tasks and wait for them to stop."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    @property
    def unavailable_networks(self) -> list[Network]:
        """Return the networks the attached pipeline cannot search."""
        return self._attached()[0].unavailable_networks

    @property
    def ready(self) -> bool:
        """Report whether searches can be started right now."""
        return self._pipeline is not None and self._gate is not None and not self._frozen

    def _attached(self) -> tuple[Researcher, asyncio.Semaphore]:
        """Return the pipeline and the concurrency gate.

        Returns:
            The attached researcher and the semaphore limiting concurrent jobs.

        Raises:
            ConfigError: when no pipeline is attached or the manager is frozen.
        """
        if self._pipeline is None or self._gate is None or self._frozen:
            raise ConfigError(
                "Searches need a language model and a search provider. An administrator can "
                "connect them under Connections."
            )
        return self._pipeline, self._gate

    def _purge(self) -> None:
        """Drop finished jobs older than the time to live from memory.

        Jobs that are still running are never dropped. The store is not touched.
        """
        now = self._clock()
        expired = [
            job.id
            for job in self._jobs.values()
            if job.state is not JobState.RUNNING and now - job.created > self._ttl
        ]
        for job_id in expired:
            del self._jobs[job_id]

    def _snapshot(self, job: Job) -> JobRecord:
        """Build the storable record of a job.

        Events are only included once the job has settled.

        Args:
            job: The job to describe.

        Returns:
            A JSON-ready record with the job state, request, report and timestamps.
        """
        settled = job.state is not JobState.RUNNING
        return JobRecord(
            id=job.id,
            thread_id=job.thread_id,
            owner=job.owner,
            kind=job.kind,
            title=job.title,
            state=job.state.value,
            stage=job.stage,
            rating=job.rating,
            request=job.request.model_dump(mode="json") if job.request else None,
            report=REPORTS.dump_python(job.report, mode="json") if job.report else None,
            events=[event.model_dump(mode="json") for event in job.events] if settled else [],
            clarification=job.clarification.model_dump(mode="json") if job.clarification else None,
            error=job.error,
            created=job.created,
            updated=self._clock(),
        )

    def _persist(self, job: Job) -> None:
        """Save the job to the store when one is configured.

        Args:
            job: The job to save.
        """
        if self._store is not None:
            self._store.save_job(self._snapshot(job))

    def _light(self, record: JobRecord) -> Job:
        # A job stored as running has no task after a restart, so it is reported as interrupted.
        """Build a job without events, request or report from a stored record.

        A record stored as running is reported as failed, because its task cannot have
        survived a restart.

        Args:
            record: The stored job record.

        Returns:
            A lightweight job suitable for listings.
        """
        # A job stored as running has no task after a restart, so it is reported as interrupted.
        stale = record.state == JobState.RUNNING.value
        return Job(
            id=record.id,
            owner=record.owner,
            kind="skill" if record.kind == "skill" else "person",
            title=record.title,
            created=record.created,
            state=JobState.FAILED if stale else JobState(record.state),
            stage=INTERRUPTED if stale else record.stage,
            error=INTERRUPTED if stale else record.error,
            thread_id=record.thread_id,
            stored_rating=record.rating,
        )

    def _hydrate(self, record: JobRecord) -> Job | None:
        """Rebuild a complete job from a stored record.

        A job that was interrupted by a restart is saved back as failed.

        Args:
            record: The stored job record.

        Returns:
            The full job, or None when the stored data no longer validates.
        """
        job = self._light(record)
        try:
            job.events = [Event.model_validate(item) for item in record.events]
            job.request = ResearchRequest.model_validate(record.request) if record.request else None
            job.clarification = (
                Clarification.model_validate(record.clarification) if record.clarification else None
            )
            job.report = REPORTS.validate_python(record.report) if record.report else None
        except ValidationError:
            logger.warning("job %s could not be read back from the store", record.id)
            return None
        if job.state is JobState.FAILED and record.state == JobState.RUNNING.value:
            self._persist(job)
        return job

    def _load(self, job_id: str) -> Job | None:
        """Load a job from the store and cache it in memory.

        Args:
            job_id: Identifier of the job.

        Returns:
            The job, or None when there is no store, no such record or it is unreadable.
        """
        if self._store is None:
            return None
        record = self._store.job(job_id)
        job = self._hydrate(record) if record else None
        if job is not None:
            self._jobs[job.id] = job
        return job

    def for_owner(self, owner: str) -> list[Job]:
        """Return all jobs of an owner, newest first.

        In-memory jobs are merged with lightweight jobs read from the store.

        Args:
            owner: The owner whose jobs are wanted.

        Returns:
            The owner's jobs sorted by creation time, newest first.
        """
        self._purge()
        jobs = [job for job in self._jobs.values() if job.owner == owner]
        if self._store is not None:
            known = {job.id for job in jobs}
            jobs.extend(
                self._light(record) for record in self._store.jobs(owner) if record.id not in known
            )
        return sorted(jobs, key=lambda job: job.created, reverse=True)

    def get(self, job_id: str, owner: str) -> Job | None:
        """Return a job if it exists and belongs to the owner.

        Args:
            job_id: Identifier of the job.
            owner: The caller's owner id.

        Returns:
            The job, or None when it is unknown or belongs to another owner.
        """
        self._purge()
        job = self._jobs.get(job_id) or self._load(job_id)
        # Owners are compared in constant time and another owner's job looks the same as a missing
        # one.
        if job is None or not secrets.compare_digest(job.owner, owner):
            return None
        return job

    def _admit(self, owner: str) -> None:
        """Check that the owner may start another job.

        Args:
            owner: The owner who wants to start a job.

        Raises:
            TooManyJobs: when the owner is at the limit of running jobs.
        """
        self._purge()
        running = sum(
            1 for job in self._jobs.values() if job.owner == owner and job.state is JobState.RUNNING
        )
        if running >= self._max_per_owner:
            raise TooManyJobs("Too many searches are running; wait for one to finish.")

    def _spawn(self, job: Job, work: Work) -> Job:
        """Register a job and run its work in the background.

        The job is saved before it starts and runs once a concurrency slot is free.

        Args:
            job: The job to run.
            work: Coroutine function that produces the outcome of the job.

        Returns:
            The same job.

        Raises:
            ConfigError: when no pipeline is attached or the manager is frozen.
        """
        pipeline, gate = self._attached()
        self._jobs[job.id] = job
        self._persist(job)
        task = asyncio.create_task(self._execute(job, work, pipeline, gate))
        self._tasks[job.id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job.id, None))
        return job

    def _new_job(
        self, owner: str, kind: Literal["person", "skill"], title: str, thread_id: str | None
    ) -> Job:
        """Create a running job with a random URL-safe id.

        Args:
            owner: The owner of the job.
            kind: Either a person research or a skill search.
            title: Display title of the job.
            thread_id: Chat thread the job belongs to, if any.

        Returns:
            The new job, not yet registered or started.
        """
        return Job(
            id=secrets.token_urlsafe(self._id_bytes),
            owner=owner,
            kind=kind,
            title=title,
            created=self._clock(),
            thread_id=thread_id,
        )

    def start_research(
        self,
        owner: str,
        goal: Goal,
        focus: str | None,
        source: Identity | CVUpload,
        options: ResearchOptions,
        company: CompanyInput | None = None,
        requirements: list[Requirement] | None = None,
        thread_id: str | None = None,
    ) -> Job:
        """Start researching one person in the background.

        Args:
            owner: The owner of the job.
            goal: Why the person is being researched.
            focus: Optional topic to concentrate on.
            source: A known identity, or an uploaded CV to extract one from.
            options: Research options.
            company: Optional company the search is done for.
            requirements: Optional requirements to rate the person against.
            thread_id: Chat thread the job belongs to, if any.

        Returns:
            The running job.

        Raises:
            TooManyJobs: when the owner is at the limit of running jobs.
            ConfigError: when no pipeline is attached or the manager is frozen.
        """
        self._admit(owner)
        title = source.name if isinstance(source, Identity) else "CV upload"
        job = self._new_job(owner, "person", title, thread_id)

        async def work(pipeline: Researcher, progress: Reporter):
            """Read the CV if one was uploaded, then research the person.

            Args:
                pipeline: The researcher to use.
                progress: Reporter that records progress events.

            Returns:
                The report, or a clarification when a profile must be chosen.
            """
            if isinstance(source, CVUpload):
                progress("Reading the CV", Task.PARSE)
                identity = await pipeline.identity_from_cv(source.data, source.filename)
                job.title = identity.name
            else:
                identity = source
            job.request = ResearchRequest(
                goal=goal,
                identity=identity,
                focus=focus,
                purpose_confirmed=True,
                company=company,
                requirements=requirements or [],
                options=options,
            )
            return await pipeline.research(job.request, progress)

        return self._spawn(job, work)

    def start_skill_search(
        self, owner: str, request: SkillSearchRequest, thread_id: str | None = None
    ) -> Job:
        """Start a search for candidates by skill in the background.

        Args:
            owner: The owner of the job.
            request: The skill search request.
            thread_id: Chat thread the job belongs to, if any.

        Returns:
            The running job.

        Raises:
            TooManyJobs: when the owner is at the limit of running jobs.
            ConfigError: when no pipeline is attached or the manager is frozen.
        """
        self._admit(owner)
        job = self._new_job(owner, "skill", f"{request.skill} in {request.location}", thread_id)

        async def work(pipeline: Researcher, progress: Reporter):
            """Run the skill search.

            Args:
                pipeline: The researcher to use.
                progress: Reporter that records progress events.

            Returns:
                The skill search report.
            """
            return await pipeline.skill_search(request, progress)

        return self._spawn(job, work)

    def answer(self, job: Job, answers: list[ClarificationAnswer]) -> Job:
        """Resume a job that is waiting for clarification answers.

        Every question must be answered and a chosen profile must be one of the offered
        options. The job goes back to the running state.

        Args:
            job: The job waiting for input.
            answers: One answer per clarification question.

        Returns:
            The same job, now running again.

        Raises:
            AgentError: when the job is not waiting, a question is unanswered or a chosen
                profile was not offered.
            TooManyJobs: when the owner is at the limit of running jobs.
        """
        if (
            job.state is not JobState.NEEDS_INPUT
            or job.request is None
            or job.clarification is None
        ):
            raise AgentError("This search is not waiting for an answer.")
        offered = {
            question.network: {option.url for option in question.options}
            for question in job.clarification.questions
        }
        if {answer.network for answer in answers} != set(offered):
            raise AgentError("Every question must be answered.")
        for answer in answers:
            if answer.chosen_url is not None and answer.chosen_url not in offered[answer.network]:
                raise AgentError("The chosen profile was not one of the offered options.")
        self._admit(job.owner)
        request = apply_answers(job.request, answers)

        async def work(pipeline: Researcher, progress: Reporter):
            """Continue the research using the answered request.

            Args:
                pipeline: The researcher to use.
                progress: Reporter that records progress events.

            Returns:
                The finished report.
            """
            job.request = request
            progress.emit(Kind.STEP, "Continuing with your answers", Task.WAIT)
            return await pipeline.research(request, progress)

        job.state = JobState.RUNNING
        job.stage = "Queued"
        job.clarification = None
        return self._spawn(job, work)

    def _record(
        self, job: Job, kind: Kind, task: Task | None, text: str, data: dict[str, Any]
    ) -> None:
        """Store one progress event on a job.

        Stage events also update the current stage and task. Once the event cap is reached
        only terminal events are kept. Text and data are clipped to the configured size.

        Args:
            job: The job the event belongs to.
            kind: The kind of event.
            task: The task the event relates to, if any.
            text: Human readable message.
            data: Extra structured data for the event.
        """
        if kind is Kind.STAGE:
            job.stage = text
            job.task = task
        # Past the cap only terminal events are kept, so a watcher can still see how the job ended.
        if len(job.events) >= self._events.max_events and kind not in TERMINAL_KINDS:
            return
        limit = self._events.text_chars
        job.events.append(
            Event(
                seq=len(job.events),
                kind=kind,
                task=task,
                text=text[:limit],
                data={key: _clipped(value, limit) for key, value in data.items()},
            )
        )

    def reporter(self, job: Job) -> Reporter:
        """Return a reporter that records progress events on the job.

        Args:
            job: The job to report on.

        Returns:
            A reporter writing into the job's event list.
        """
        return Reporter(lambda kind, task, text, data: self._record(job, kind, task, text, data))

    def events_after(self, job: Job, after: int, limit: int | None = None) -> list[Event]:
        """Return a page of events following a position.

        Args:
            job: The job whose events are read.
            after: Index of the first event wanted; negative values count as zero.
            limit: Maximum number of events; the configured page size when omitted.

        Returns:
            The events from that position on.
        """
        size = limit or self._events.page_size
        return job.events[max(after, 0) : max(after, 0) + size]

    async def watch(self, job: Job, after: int = 0) -> AsyncIterator[Event | None]:
        """Follow a job's events as they are produced.

        Stops once the job is no longer running and all events were delivered, or after
        the maximum stream time.

        Args:
            job: The job to follow.
            after: Index of the first event wanted.

        Returns:
            An async iterator of events; None is a keep-alive heartbeat.
        """
        cursor = max(after, 0)
        started = self._clock()
        last_beat = started
        while True:
            batch = self.events_after(job, cursor)
            for event in batch:
                yield event
            cursor += len(batch)
            if batch:
                continue
            now = self._clock()
            if job.state is not JobState.RUNNING or now - started > self._events.max_stream_seconds:
                return
            if now - last_beat >= self._events.heartbeat_seconds:
                last_beat = now
                yield None
            await asyncio.sleep(self._events.poll_seconds)

    async def _execute(
        self, job: Job, work: Work, pipeline: Researcher, gate: asyncio.Semaphore
    ) -> None:
        """Run the work of a job and record how it ended.

        Waits for a free concurrency slot, then stores either a clarification, the exported
        report or a failure. Unexpected errors are logged by type only and shown to the
        user as a generic message.

        Args:
            job: The job to run.
            work: Coroutine function that produces the outcome.
            pipeline: The researcher passed to the work.
            gate: Semaphore limiting concurrent jobs.
        """
        reporter = self.reporter(job)
        async with gate:
            try:
                outcome = await work(pipeline, reporter)
                if isinstance(outcome, Clarification):
                    job.clarification = outcome
                    job.stage = "Waiting for your answer"
                    job.task = Task.WAIT
                    networks = [question.network.value for question in outcome.questions]
                    reporter.emit(
                        Kind.QUESTION,
                        "I need your help to choose the right profile.",
                        Task.WAIT,
                        networks=networks,
                    )
                    job.state = JobState.NEEDS_INPUT
                    self._settled(job)
                    return
                reporter("Exporting the report", Task.EXPORT)
                job.exported = await asyncio.to_thread(export_report, outcome, self._output_dir)
                job.report = outcome
                job.stage = "Done"
                reporter.emit(Kind.DONE, "The report is ready.", Task.EXPORT)
                job.state = JobState.DONE
            except AgentError as error:
                job.error = str(error)
                reporter.emit(Kind.FAILED, job.error, None)
                job.state = JobState.FAILED
            except Exception as error:
                logger.error("job %s failed with %s", job.id, type(error).__name__)
                job.error = UNEXPECTED_ERROR
                reporter.emit(Kind.FAILED, job.error, None)
                job.state = JobState.FAILED
            self._settled(job)

    def _settled(self, job: Job) -> None:
        """Save a job that stopped running and notify the listeners.

        Failures while saving or notifying are logged and never raised.

        Args:
            job: The job that finished, failed or needs input.
        """
        try:
            self._persist(job)
        except Exception as error:
            logger.error("job %s could not be saved: %s", job.id, type(error).__name__)
        for listener in [self.on_settled, *self.observers]:
            if listener is None:
                continue
            try:
                listener(job)
            except Exception as error:
                logger.error("job %s could not be settled: %s", job.id, type(error).__name__)

    def delete(self, job: Job) -> None:
        """Delete a job completely.

        Cancels a running task, removes the exported files and drops the job from memory
        and from the store.

        Args:
            job: The job to delete.
        """
        task = self._tasks.pop(job.id, None)
        if task is not None:
            task.cancel()
        if job.exported is not None:
            delete_exported(job.exported)
        self._jobs.pop(job.id, None)
        if self._store is not None:
            self._store.delete_job(job.id)

    def forget_all(self, owner: str) -> int:
        """Delete every job of an owner.

        Args:
            owner: The owner whose jobs are deleted.

        Returns:
            The number of jobs deleted.
        """
        jobs = self.for_owner(owner)
        for job in jobs:
            self.delete(job)
        return len(jobs)

    def read_export(self, job: Job, fmt: Literal["md", "json"]) -> bytes | None:
        """Return the bytes of a job's exported report.

        Falls back to rendering the in-memory report when the exported file is missing.

        Args:
            job: The job whose report is wanted.
            fmt: Either 'md' for Markdown or 'json'.

        Returns:
            The encoded report, or None when the job has no report.
        """
        if job.exported is not None:
            path = job.exported.markdown if fmt == "md" else job.exported.json
            try:
                return path.read_bytes()
            except FileNotFoundError:
                pass
        if job.report is None:
            return None
        rendered = render_markdown(job.report) if fmt == "md" else render_json(job.report)
        return rendered.encode()
