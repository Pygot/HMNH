# src/agent/routines.py
from agent.models import (
    Goal,
    RankedCandidate,
    Requirement,
    ResearchOptions,
    SkillSearchReport,
    SkillSearchRequest,
)
from pydantic import (
    BaseModel,
    Field,
    ValidationError,
)
from agent.config import (
    MAIL_ADDRESS,
    RoutineTuning,
)
from agent.compliance import check_skill_request
from agent.errors import InvalidRequest
from collections.abc import Callable
from agent.db import Database
from typing import Any

import secrets
import sqlite3
import json
import time

ACTIVE = "active"
PAUSED = "paused"
SATISFIED = "satisfied"
EXHAUSTED = "exhausted"
FAILED = "failed"
RUNNING = "running"
DONE = "done"
INTERRUPTED = "interrupted"
DAY = 86_400
HOUR = 3_600


class Routine(BaseModel):
    """A recurring candidate search with its schedule, criteria and run history."""

    id: str
    name: str
    skill: str
    location: str
    size: int
    goal: Goal = Goal.HIRING
    requirements: list[Requirement] = Field(default_factory=list)
    options: ResearchOptions = Field(default_factory=ResearchOptions)
    min_rating: float
    need_musts: bool = True
    need_count: int = 1
    interval_hours: int
    max_runs: int
    runs: int = 0
    failures: int = 0
    status: str = ACTIVE
    next_run: float | None = None
    last_run: float | None = None
    last_job: str = ""
    last_result: str = ""
    best_rating: float | None = None
    notify_emails: list[str] = Field(default_factory=list)
    purpose_confirmed: bool = False
    created: float
    updated: float
    created_by: str = ""


class Run(BaseModel):
    """One execution of a routine with its state and result."""

    id: int
    routine_id: str
    ts: float
    finished: float | None = None
    job_id: str = ""
    state: str = RUNNING
    best_rating: float | None = None
    matches: int = 0
    summary: str = ""


class Match(BaseModel):
    """A candidate that met the criteria of a routine."""

    name: str
    rating: float
    url: str


class Outcome(BaseModel):
    """The evaluated result of one routine search."""

    best_rating: float | None = None
    matches: list[Match] = Field(default_factory=list)
    satisfied: bool = False
    summary: str = ""


class Finished(BaseModel):
    """A completed run bundled with its updated routine and evaluated outcome."""

    routine: Routine
    run: Run
    outcome: Outcome | None = None
    job_id: str = ""


def _json(value: Any) -> str:
    """Serialize a value to compact JSON that keeps non-ASCII characters.

    Args:
        value: JSON compatible value.

    Returns:
        The JSON text without extra spaces.
    """
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, fallback: Any) -> Any:
    """Parse JSON text, falling back when it is empty or invalid.

    Args:
        value: JSON text or None.
        fallback: Value to return when there is nothing valid to parse.

    Returns:
        The parsed value or the fallback.
    """
    try:
        return json.loads(value) if value else fallback
    except ValueError:
        return fallback


def qualifies(candidate: RankedCandidate, routine: Routine) -> bool:
    """Check whether a candidate meets the criteria of a routine.

    The overall rating must reach the minimum. When the routine needs its must have
    requirements met, the candidate must also have an assessment with none missing.

    Args:
        candidate: Ranked candidate from a search report.
        routine: Routine whose criteria apply.

    Returns:
        True when the candidate counts as a match.
    """
    if candidate.rating.overall < routine.min_rating:
        return False
    if routine.need_musts and routine.requirements:
        assessment = candidate.requirements
        return assessment is not None and not assessment.must_missing
    return True


def evaluate(report: SkillSearchReport, routine: Routine, tuning: RoutineTuning) -> Outcome:
    """Judge a search report against the criteria of a routine.

    Args:
        report: Finished skill search report.
        routine: Routine whose criteria apply.
        tuning: Routine settings giving the summary limits.

    Returns:
        The outcome with the qualifying matches, the best rating seen, whether enough
        matches were found, and a summary truncated to the configured length.
    """
    ranked = sorted(report.candidates, key=lambda item: item.rating.overall, reverse=True)
    matches = [
        Match(name=item.name, rating=item.rating.overall, url=str(item.profile_url))
        for item in ranked
        if qualifies(item, routine)
    ]
    best = ranked[0].rating.overall if ranked else None
    satisfied = len(matches) >= routine.need_count
    if satisfied:
        names = ", ".join(
            f"{item.name} ({item.rating:.1f})" for item in matches[: tuning.matches_listed]
        )
        summary = f"{len(matches)} match{'es' if len(matches) != 1 else ''}: {names}"
    elif ranked:
        summary = (
            f"No perfect match yet. Best was {ranked[0].name} at {ranked[0].rating.overall:.1f}"
            f" (needs {routine.min_rating:.1f})."
        )
    else:
        summary = "No candidates were found."
    return Outcome(
        best_rating=best,
        matches=matches,
        satisfied=satisfied,
        summary=summary[: tuning.summary_chars],
    )


class Routines:
    """Storage and scheduling state for recurring search routines.

    Claiming a due routine and recording its result happen inside database
    transactions, so one routine is never started twice at the same time.
    """

    def __init__(
        self,
        database: Database,
        tuning: RoutineTuning,
        clock: Callable[[], float] = time.time,
    ):
        """Initialize the routine store.

        Args:
            database: Database holding the routines and their run history.
            tuning: Routine limits and defaults.
            clock: Function returning the current time as a Unix timestamp in seconds.
        """
        self._db = database
        self._tuning = tuning
        self._clock = clock

    def _routine(self, row: sqlite3.Row) -> Routine:
        """Convert a database row into a Routine.

        If the stored requirements or options no longer validate, they are cleared and
        the routine is marked failed instead of raising.

        Args:
            row: Row from the routines table.

        Returns:
            The routine with its JSON columns decoded.
        """
        values = dict(row)
        values["requirements"] = _loads(values["requirements"], [])
        values["options"] = _loads(values["options"], {})
        values["notify_emails"] = _loads(values["notify_emails"], [])
        values["need_musts"] = bool(values["need_musts"])
        values["purpose_confirmed"] = bool(values["purpose_confirmed"])
        try:
            return Routine.model_validate(values)
        except ValidationError:
            values["requirements"] = []
            values["options"] = {}
            values["status"] = FAILED
            return Routine.model_validate(values)

    def request_of(self, routine: Routine, company: Any = None) -> SkillSearchRequest:
        """Build the skill search request that a routine runs.

        Args:
            routine: Routine to run.
            company: Optional company context passed through to the request.

        Returns:
            The search request using the routine's skill, location, size and options.
        """
        return SkillSearchRequest(
            goal=routine.goal,
            skill=routine.skill,
            location=routine.location,
            limit=routine.size,
            purpose_confirmed=routine.purpose_confirmed,
            company=company,
            requirements=routine.requirements,
            options=routine.options,
        )

    def get(self, identifier: str) -> Routine | None:
        """Fetch one routine by id.

        Args:
            identifier: Routine id.

        Returns:
            The routine, or None when it does not exist.
        """
        row = self._db.one("SELECT * FROM routines WHERE id = ?", (identifier,))
        return self._routine(row) if row else None

    def all(self) -> list[Routine]:
        """Return all routines.

        Returns:
            Active routines first, then the rest, most recently updated first.
        """
        rows = self._db.query(
            "SELECT * FROM routines ORDER BY status = 'active' DESC, updated DESC"
        )
        return [self._routine(row) for row in rows]

    def _recipients(self, values: list[str]) -> list[str]:
        """Normalize and validate notification addresses.

        Args:
            values: Raw email addresses.

        Returns:
            Lower case addresses without blanks or duplicates, in the original order.

        Raises:
            InvalidRequest: when there are too many addresses or one is malformed.
        """
        cleaned = list(dict.fromkeys(item.strip().lower() for item in values if item.strip()))
        if len(cleaned) > self._tuning.max_recipients:
            raise InvalidRequest("Too many people to notify.")
        if any(not MAIL_ADDRESS.fullmatch(item) for item in cleaned):
            raise InvalidRequest("One of the notification addresses does not look right.")
        return cleaned

    def create(
        self,
        name: str,
        skill: str,
        location: str,
        *,
        size: int | None = None,
        requirements: list[Requirement] | None = None,
        options: ResearchOptions | None = None,
        min_rating: float | None = None,
        need_musts: bool = True,
        need_count: int = 1,
        interval_hours: int | None = None,
        max_runs: int | None = None,
        notify_emails: list[str] | None = None,
        purpose_confirmed: bool = False,
        company: Any = None,
        actor: str = "",
        paused: bool = False,
    ) -> Routine:
        """Validate and save a new routine.

        The search request is checked against the compliance rules before anything is
        stored, and the routine is scheduled to run immediately unless it starts paused.

        Args:
            name: Display name; defaults to the skill and location when blank.
            skill: Skill to search for.
            location: City or area to search in.
            size: Candidates per run; defaults to the configured size.
            requirements: Optional requirements candidates are assessed against.
            options: Optional research options.
            min_rating: Minimum overall rating from 0 to 5 for a match.
            need_musts: Whether a match must meet every must have requirement.
            need_count: Number of matches needed to finish the routine.
            interval_hours: Hours between runs.
            max_runs: Maximum number of runs before the routine is exhausted.
            notify_emails: Addresses to tell when a run finishes.
            purpose_confirmed: Whether the hiring purpose was confirmed by the user.
            company: Optional company context for the request.
            actor: Who is creating the routine.
            paused: Create the routine paused instead of active.

        Returns:
            The saved routine.

        Raises:
            InvalidRequest: when a value is out of range, the request is not valid, a
                routine limit is reached, or the routine cannot be read back.
            ComplianceRefusal: when the search request is not allowed.
        """
        tuning = self._tuning
        label = " ".join(name.split())[: tuning.max_name_chars]
        interval = tuning.default_interval_hours if interval_hours is None else interval_hours
        runs = tuning.default_max_runs if max_runs is None else max_runs
        rating = tuning.default_min_rating if min_rating is None else min_rating
        count = size or tuning.default_size
        if not tuning.min_interval_hours <= interval <= tuning.max_interval_hours:
            raise InvalidRequest(
                f"Repeat every {tuning.min_interval_hours} to {tuning.max_interval_hours} hours."
            )
        if not 1 <= runs <= tuning.max_runs_limit:
            raise InvalidRequest(f"Run between 1 and {tuning.max_runs_limit} times.")
        if not 0 <= rating <= 5:
            raise InvalidRequest("A rating goes from 0 to 5.")
        if not 1 <= need_count <= count:
            raise InvalidRequest("Matches needed must fit the number of candidates per run.")
        try:
            request = SkillSearchRequest(
                goal=Goal.HIRING,
                skill=skill,
                location=location,
                limit=count,
                purpose_confirmed=purpose_confirmed,
                company=company,
                requirements=requirements or [],
                options=options or ResearchOptions(),
            )
        except ValidationError as error:
            raise InvalidRequest("The skill, city or requirements are not valid.") from error
        check_skill_request(request)
        recipients = self._recipients(notify_emails or [])
        label = label or f"{request.skill} in {request.location}"
        identifier = secrets.token_urlsafe(9)
        now = self._clock()
        with self._db.transaction() as db:
            total = db.execute("SELECT COUNT(*) AS n FROM routines").fetchone()["n"]
            active = db.execute(
                "SELECT COUNT(*) AS n FROM routines WHERE status = ?", (ACTIVE,)
            ).fetchone()["n"]
            if total >= tuning.max_routines:
                raise InvalidRequest("There are too many routines.")
            if active >= tuning.max_active and not paused:
                raise InvalidRequest("Too many routines are active; pause or finish one first.")
            db.execute(
                "INSERT INTO routines (id, name, skill, location, size, goal, requirements, "
                "options, min_rating, need_musts, need_count, interval_hours, max_runs, status, "
                "next_run, notify_emails, purpose_confirmed, created, updated, created_by) "
                "VALUES (?, ?, ?, ?, ?, 'hiring', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (
                    identifier,
                    label,
                    request.skill,
                    request.location,
                    count,
                    _json([item.model_dump(mode="json") for item in request.requirements]),
                    _json(request.options.model_dump(mode="json")),
                    rating,
                    int(need_musts),
                    need_count,
                    interval,
                    runs,
                    PAUSED if paused else ACTIVE,
                    None if paused else now,
                    _json(recipients),
                    now,
                    now,
                    actor,
                ),
            )
        created = self.get(identifier)
        if created is None:
            raise InvalidRequest("The routine could not be saved.")
        return created

    def update(
        self,
        identifier: str,
        *,
        name: str | None = None,
        min_rating: float | None = None,
        need_musts: bool | None = None,
        need_count: int | None = None,
        interval_hours: int | None = None,
        max_runs: int | None = None,
        notify_emails: list[str] | None = None,
    ) -> Routine:
        """Change the tunable settings of an existing routine.

        Only the arguments that are given change; the search itself cannot be edited.

        Args:
            identifier: Routine id.
            name: New display name; an empty result keeps the old one.
            min_rating: New minimum rating from 0 to 5.
            need_musts: New must have setting.
            need_count: New number of matches needed, up to the candidates per run.
            interval_hours: New hours between runs.
            max_runs: New maximum number of runs.
            notify_emails: New notification addresses, replacing the old list.

        Returns:
            The updated routine.

        Raises:
            InvalidRequest: when the routine does not exist or a value is out of range.
        """
        tuning = self._tuning
        current = self.get(identifier)
        if current is None:
            raise InvalidRequest("That routine does not exist.")
        interval = current.interval_hours if interval_hours is None else interval_hours
        runs = current.max_runs if max_runs is None else max_runs
        rating = current.min_rating if min_rating is None else min_rating
        count = current.need_count if need_count is None else need_count
        if not tuning.min_interval_hours <= interval <= tuning.max_interval_hours:
            raise InvalidRequest(
                f"Repeat every {tuning.min_interval_hours} to {tuning.max_interval_hours} hours."
            )
        if not 1 <= runs <= tuning.max_runs_limit:
            raise InvalidRequest(f"Run between 1 and {tuning.max_runs_limit} times.")
        if not 0 <= rating <= 5:
            raise InvalidRequest("A rating goes from 0 to 5.")
        if not 1 <= count <= current.size:
            raise InvalidRequest("Matches needed must fit the number of candidates per run.")
        recipients = (
            current.notify_emails if notify_emails is None else self._recipients(notify_emails)
        )
        label = current.name if name is None else " ".join(name.split())[: tuning.max_name_chars]
        self._db.run(
            "UPDATE routines SET name = ?, min_rating = ?, need_musts = ?, need_count = ?, "
            "interval_hours = ?, max_runs = ?, notify_emails = ?, updated = ? WHERE id = ?",
            (
                label or current.name,
                rating,
                int(current.need_musts if need_musts is None else need_musts),
                count,
                interval,
                runs,
                _json(recipients),
                self._clock(),
                identifier,
            ),
        )
        updated = self.get(identifier)
        if updated is None:
            raise InvalidRequest("That routine does not exist.")
        return updated

    def set_status(self, identifier: str, status: str) -> Routine:
        """Pause or resume a routine.

        Resuming schedules an immediate run and clears the failure count. A routine that
        was satisfied, exhausted or failed also restarts its run counter.

        Args:
            identifier: Routine id.
            status: Either active or paused.

        Returns:
            The updated routine.

        Raises:
            InvalidRequest: when the status is not allowed, the routine does not exist, or
                too many routines would be active.
        """
        if status not in (ACTIVE, PAUSED):
            raise InvalidRequest("Routines can be paused or resumed.")
        with self._db.transaction() as db:
            row = db.execute("SELECT * FROM routines WHERE id = ?", (identifier,)).fetchone()
            if row is None:
                raise InvalidRequest("That routine does not exist.")
            if status == ACTIVE:
                active = db.execute(
                    "SELECT COUNT(*) AS n FROM routines WHERE status = ?", (ACTIVE,)
                ).fetchone()["n"]
                if row["status"] != ACTIVE and active >= self._tuning.max_active:
                    raise InvalidRequest("Too many routines are active; pause one first.")
                runs = row["runs"] if row["status"] not in (SATISFIED, EXHAUSTED, FAILED) else 0
                db.execute(
                    "UPDATE routines SET status = ?, runs = ?, failures = 0, next_run = ?, "
                    "updated = ? WHERE id = ?",
                    (ACTIVE, runs, self._clock(), self._clock(), identifier),
                )
            else:
                db.execute(
                    "UPDATE routines SET status = ?, next_run = NULL, updated = ? WHERE id = ?",
                    (PAUSED, self._clock(), identifier),
                )
        found = self.get(identifier)
        if found is None:
            raise InvalidRequest("That routine does not exist.")
        return found

    def delete(self, identifier: str) -> bool:
        """Delete a routine by id.

        Args:
            identifier: Routine id.

        Returns:
            True when a routine was removed, False when none matched.
        """
        return bool(self._db.run("DELETE FROM routines WHERE id = ?", (identifier,)))

    def runs(self, identifier: str) -> list[Run]:
        """Return the recorded runs of a routine.

        Args:
            identifier: Routine id.

        Returns:
            Up to the configured number of runs, newest first.
        """
        rows = self._db.query(
            "SELECT * FROM routine_runs WHERE routine_id = ? ORDER BY id DESC LIMIT ?",
            (identifier, self._tuning.runs_kept),
        )
        return [Run.model_validate(dict(row)) for row in rows]

    def runs_today(self) -> int:
        """Count the runs started during the last 24 hours.

        Returns:
            The number of runs across all routines.
        """
        row = self._db.one(
            "SELECT COUNT(*) AS n FROM routine_runs WHERE ts > ?", (self._clock() - DAY,)
        )
        return row["n"] if row else 0

    def running(self) -> int:
        """Count the runs that are currently in progress.

        Returns:
            The number of runs in the running state.
        """
        row = self._db.one("SELECT COUNT(*) AS n FROM routine_runs WHERE state = ?", (RUNNING,))
        return row["n"] if row else 0

    def due(self, limit: int = 1) -> list[Routine]:
        """Return active routines whose next run time has arrived.

        Routines that already have a run in progress are skipped.

        Args:
            limit: Maximum number of routines to return.

        Returns:
            Due routines, the longest waiting first.
        """
        rows = self._db.query(
            "SELECT * FROM routines WHERE status = ? AND next_run IS NOT NULL AND next_run <= ? "
            "AND NOT EXISTS (SELECT 1 FROM routine_runs WHERE routine_runs.routine_id = "
            "routines.id AND routine_runs.state = ?) ORDER BY next_run LIMIT ?",
            (ACTIVE, self._clock(), RUNNING, limit),
        )
        return [self._routine(row) for row in rows]

    def begin(self, identifier: str, job_id: str) -> Run | None:
        """Claim a due routine and record the start of a run.

        The claim is a conditional update, so only one caller can start a given run.
        Older run records beyond the configured history are deleted.

        Args:
            identifier: Routine id.
            job_id: Id of the search job that performs the run.

        Returns:
            The new run, or None when the routine is not active or not due.
        """
        now = self._clock()
        with self._db.transaction() as db:
            claimed = db.execute(
                # Clearing next_run in the same conditional update is what claims the routine, so a
                # second caller matches no row.
                "UPDATE routines SET next_run = NULL, last_run = ?, last_job = ?, "
                "runs = runs + 1, updated = ? WHERE id = ? AND status = ? AND next_run IS NOT NULL "
                "AND next_run <= ?",
                (now, job_id, now, identifier, ACTIVE, now),
            ).rowcount
            if not claimed:
                return None
            cursor = db.execute(
                "INSERT INTO routine_runs (routine_id, ts, job_id, state) VALUES (?, ?, ?, ?)",
                (identifier, now, job_id, RUNNING),
            )
            run_id = cursor.lastrowid
            db.execute(
                "DELETE FROM routine_runs WHERE routine_id = ? AND id NOT IN ("
                "SELECT id FROM routine_runs WHERE routine_id = ? ORDER BY id DESC LIMIT ?)",
                (identifier, identifier, self._tuning.runs_kept),
            )
        row = self._db.one("SELECT * FROM routine_runs WHERE id = ?", (run_id,))
        return Run.model_validate(dict(row)) if row else None

    def queue(self, identifier: str) -> bool:
        """Schedule a routine to run as soon as possible.

        Args:
            identifier: Routine id.

        Returns:
            True when the routine was queued; False when it is not active or already has
            a run in progress.
        """
        return bool(
            self._db.run(
                "UPDATE routines SET next_run = ?, updated = ? WHERE id = ? AND status = ? "
                # Skipping routines that already have a run in progress prevents two runs of one
                # routine from overlapping.
                "AND NOT EXISTS (SELECT 1 FROM routine_runs WHERE routine_id = ? AND state = ?)",
                (self._clock(), self._clock(), identifier, ACTIVE, identifier, RUNNING),
            )
        )

    def finish(
        self,
        job_id: str,
        report: SkillSearchReport | None,
        error: str = "",
    ) -> Finished | None:
        """Record the result of a run and decide the next state of its routine.

        The routine becomes satisfied when enough matches were found, failed after too
        many consecutive failures, exhausted at its run limit, and otherwise stays
        scheduled at its interval.

        Args:
            job_id: Id of the search job that performed the run.
            report: The search report, or None when the search failed.
            error: Failure description used as the summary when there is no report.

        Returns:
            The finished run with its routine and outcome, or None when no running run
            matches the job.
        """
        now = self._clock()
        with self._db.transaction() as db:
            run = db.execute(
                "SELECT * FROM routine_runs WHERE job_id = ? AND state = ?", (job_id, RUNNING)
            ).fetchone()
            if run is None:
                return None
            row = db.execute("SELECT * FROM routines WHERE id = ?", (run["routine_id"],)).fetchone()
            if row is None:
                return None
            routine = self._routine(row)
            outcome = evaluate(report, routine, self._tuning) if report is not None else None
            failures = 0 if outcome is not None else routine.failures + 1
            if outcome is not None and outcome.satisfied:
                status, next_run = SATISFIED, None
            elif outcome is None and failures >= self._tuning.max_failures:
                status, next_run = FAILED, None
            elif routine.runs >= routine.max_runs:
                status, next_run = EXHAUSTED, None
            elif routine.status != ACTIVE:
                status, next_run = routine.status, None
            else:
                status, next_run = ACTIVE, now + routine.interval_hours * HOUR
            summary = (
                outcome.summary if outcome is not None else (error or "The search did not finish.")
            )[: self._tuning.summary_chars]
            best = outcome.best_rating if outcome is not None else None
            db.execute(
                "UPDATE routine_runs SET finished = ?, state = ?, best_rating = ?, matches = ?, "
                "summary = ? WHERE id = ?",
                (
                    now,
                    DONE if outcome is not None else FAILED,
                    best,
                    len(outcome.matches) if outcome else 0,
                    summary,
                    run["id"],
                ),
            )
            top = routine.best_rating
            if best is not None and (top is None or best > top):
                top = best
            db.execute(
                "UPDATE routines SET status = ?, next_run = ?, failures = ?, last_result = ?, "
                "best_rating = ?, updated = ? WHERE id = ?",
                (status, next_run, failures, summary, top, now, routine.id),
            )
        finished_run = self._db.one("SELECT * FROM routine_runs WHERE id = ?", (run["id"],))
        current = self.get(routine.id)
        if finished_run is None or current is None:
            return None
        return Finished(
            routine=current,
            run=Run.model_validate(dict(finished_run)),
            outcome=outcome,
            job_id=job_id,
        )

    def recover(self) -> int:
        """Mark runs left in progress by a restart as interrupted.

        Each affected routine that is waiting without a next run is rescheduled
        immediately and does not lose a run from its budget.

        Returns:
            The number of runs that were interrupted.
        """
        now = self._clock()
        with self._db.transaction() as db:
            stuck = db.execute(
                "SELECT id, routine_id FROM routine_runs WHERE state = ?", (RUNNING,)
            ).fetchall()
            for row in stuck:
                db.execute(
                    "UPDATE routine_runs SET state = ?, finished = ?, summary = ? WHERE id = ?",
                    (INTERRUPTED, now, "The server restarted during this run.", row["id"]),
                )
                db.execute(
                    "UPDATE routines SET next_run = ?, runs = MAX(0, runs - 1) "
                    "WHERE id = ? AND status = ? AND next_run IS NULL",
                    (now, row["routine_id"], ACTIVE),
                )
        return len(stuck)
