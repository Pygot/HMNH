# tests/test_routines.py
from agent.models import (
    Priority,
    Requirement,
    RequirementKind,
    RequirementResult,
    RequirementsAssessment,
    RequirementStatus,
)
from agent.routines import (
    ACTIVE,
    EXHAUSTED,
    FAILED,
    PAUSED,
    Routines,
    SATISFIED,
)
from agent.config import (
    RoutineTuning,
    StoreTuning,
)
from agent.errors import (
    ComplianceRefusal,
    InvalidRequest,
)
from tests.fakes import (
    sample_rating,
    skill_report,
)
from agent.db import Database

import pytest


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now

    def tick(self, hours=1):
        self.now += hours * 3600


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def make(tmp_path, clock):
    opened = []

    def build(**changes):
        database = Database(StoreTuning(path=tmp_path / "data" / "agent.db"), clock)
        opened.append(database)
        return Routines(database, RoutineTuning(**changes), clock)

    yield build
    for database in opened:
        database.close()


@pytest.fixture
def routines(make):
    return make()


def must(label="Rust"):
    return Requirement(kind=RequirementKind.SKILL, label=label, priority=Priority.MUST)


def report(rating, missing=()):
    base = skill_report()
    candidate = base.candidates[0]
    results = [
        RequirementResult(
            id="R1",
            requirement=must(),
            status=RequirementStatus.UNMET if missing else RequirementStatus.MET,
            justification="Checked.",
        )
    ]
    assessment = RequirementsAssessment(
        results=results,
        coverage=0.0 if missing else 1.0,
        met=0 if missing else 1,
        partial=0,
        unmet=1 if missing else 0,
        unknown=0,
        must_missing=list(missing),
    )
    changed = candidate.model_copy(
        update={"rating": sample_rating(rating), "requirements": assessment}
    )
    return base.model_copy(update={"candidates": [changed]})


def create(routines, **changes):
    values = {"purpose_confirmed": True, "min_rating": 4.0}
    values.update(changes)
    return routines.create("", "Rust", "Brno", **values)


def test_creating_a_routine_checks_everything_up_front(routines):
    made = create(routines, notify_emails=["Team@Example.com", "team@example.com"])
    assert made.name == "Rust in Brno" and made.status == ACTIVE and made.next_run is not None
    assert made.notify_emails == ["team@example.com"] and made.interval_hours == 24
    with pytest.raises(ComplianceRefusal):
        create(routines, purpose_confirmed=False)
    with pytest.raises(InvalidRequest, match="Repeat every"):
        create(routines, interval_hours=0)
    with pytest.raises(InvalidRequest, match="Run between"):
        create(routines, max_runs=0)
    with pytest.raises(InvalidRequest, match="rating"):
        create(routines, min_rating=7)
    with pytest.raises(InvalidRequest, match="Matches needed"):
        create(routines, need_count=9)
    with pytest.raises(InvalidRequest, match="address"):
        create(routines, notify_emails=["nope"])
    with pytest.raises(InvalidRequest, match="not valid"):
        routines.create("x", "", "Brno", purpose_confirmed=True)


def test_the_number_of_routines_is_capped(make):
    small = make(max_active=1, max_routines=2)
    first = create(small)
    with pytest.raises(InvalidRequest, match="active"):
        create(small)
    small.set_status(first.id, PAUSED)
    create(small)
    with pytest.raises(InvalidRequest, match="too many routines"):
        create(small)
    with pytest.raises(InvalidRequest, match="Too many routines are active"):
        small.set_status(first.id, ACTIVE)


def test_a_routine_is_due_once_and_cannot_be_started_twice(routines, clock):
    made = create(routines)
    assert [item.id for item in routines.due()] == [made.id]
    assert routines.begin(made.id, "job-1") is not None
    assert routines.begin(made.id, "job-2") is None
    assert routines.due() == [] and routines.running() == 1
    assert routines.get(made.id).runs == 1


def test_a_perfect_match_satisfies_the_routine_and_stops_it(routines):
    made = create(routines, need_count=1)
    routines.begin(made.id, "job-1")
    done = routines.finish("job-1", report(4.6))
    assert done.routine.status == SATISFIED and done.routine.next_run is None
    assert done.outcome.satisfied and done.outcome.matches[0].name == "Jan Novak"
    assert "1 match" in done.run.summary and done.run.state == "done"
    assert done.routine.best_rating == pytest.approx(4.6)
    assert routines.finish("job-1", report(4.6)) is None


def test_a_near_miss_schedules_the_next_run_and_keeps_the_best_rating(routines, clock):
    made = create(routines, interval_hours=12)
    routines.begin(made.id, "job-1")
    first = routines.finish("job-1", report(3.8))
    assert first.routine.status == ACTIVE and not first.outcome.satisfied
    assert first.routine.next_run == pytest.approx(clock.now + 12 * 3600)
    assert "No perfect match yet" in first.run.summary
    assert routines.due() == []
    clock.tick(13)
    assert routines.due()[0].id == made.id
    routines.begin(made.id, "job-2")
    second = routines.finish("job-2", report(3.5))
    assert second.routine.best_rating == pytest.approx(3.8)
    assert [run.job_id for run in routines.runs(made.id)] == ["job-2", "job-1"]


def test_missing_must_haves_block_a_match_even_with_a_high_rating(routines):
    made = routines.create(
        "",
        "Rust",
        "Brno",
        purpose_confirmed=True,
        requirements=[must()],
        min_rating=4.0,
    )
    routines.begin(made.id, "job-1")
    assert not routines.finish("job-1", report(4.9, missing=["Rust"])).outcome.satisfied
    clock_free = routines.get(made.id)
    assert clock_free.status == ACTIVE
    relaxed = routines.update(made.id, need_musts=False)
    assert relaxed.need_musts is False


def test_running_out_of_runs_or_failing_repeatedly_ends_the_routine(routines, clock):
    short = create(routines, max_runs=1)
    routines.begin(short.id, "job-1")
    assert routines.finish("job-1", report(3.0)).routine.status == EXHAUSTED
    broken = create(routines)
    for number in range(3):
        clock.tick(25)
        routines.begin(broken.id, f"job-f{number}")
        done = routines.finish(f"job-f{number}", None, error="The search stopped.")
    assert done.routine.status == FAILED and done.routine.failures == 3
    assert done.run.state == "failed" and done.run.summary == "The search stopped."


def test_pausing_and_resuming(routines, clock):
    made = create(routines)
    paused = routines.set_status(made.id, PAUSED)
    assert paused.status == PAUSED and paused.next_run is None and routines.due() == []
    resumed = routines.set_status(made.id, ACTIVE)
    assert resumed.status == ACTIVE and routines.due()[0].id == made.id
    with pytest.raises(InvalidRequest):
        routines.set_status(made.id, "satisfied")
    with pytest.raises(InvalidRequest, match="exist"):
        routines.set_status("missing", ACTIVE)


def test_a_finished_routine_can_be_restarted_with_a_fresh_run_count(routines):
    made = create(routines, max_runs=1)
    routines.begin(made.id, "job-1")
    routines.finish("job-1", report(3.0))
    again = routines.set_status(made.id, ACTIVE)
    assert again.status == ACTIVE and again.runs == 0


def test_editing_validates_and_keeps_the_rest(routines):
    made = create(routines)
    changed = routines.update(
        made.id, name="  Rust hunt ", min_rating=4.5, interval_hours=6, notify_emails=["a@b.co"]
    )
    assert changed.name == "Rust hunt" and changed.min_rating == 4.5
    assert changed.interval_hours == 6 and changed.notify_emails == ["a@b.co"]
    assert changed.skill == "Rust"
    with pytest.raises(InvalidRequest):
        routines.update(made.id, interval_hours=100_000)
    with pytest.raises(InvalidRequest, match="exist"):
        routines.update("missing", name="x")


def test_a_restart_frees_interrupted_runs(routines, clock):
    made = create(routines)
    routines.begin(made.id, "job-1")
    assert routines.recover() == 1
    run = routines.runs(made.id)[0]
    assert run.state == "interrupted" and routines.get(made.id).runs == 0
    assert routines.due()[0].id == made.id


def test_the_daily_counter_and_deletion(routines, clock):
    made = create(routines)
    routines.begin(made.id, "job-1")
    assert routines.runs_today() == 1
    clock.tick(25)
    assert routines.runs_today() == 0
    assert routines.delete(made.id) is True and routines.get(made.id) is None
    assert routines.runs(made.id) == []
