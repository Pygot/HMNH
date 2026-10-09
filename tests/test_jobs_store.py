# tests/test_jobs_store.py
from agent.models import (
    ClarificationAnswer,
    Goal,
    Identity,
    Network,
    ResearchOptions,
)
from tests.web_helpers import (
    clarification,
    LI_A,
    StubResearcher,
    TOKEN,
)
from agent.web.jobs import (
    JobManager,
    JobState,
    WEB_OWNER,
)
from agent.store import (
    JobRecord,
    Store,
)
from agent.web.settings import WebSettings
from agent.config import StoreTuning
from agent.errors import AgentError

import asyncio
import pytest

OPTIONS = ResearchOptions()
JAN = Identity(name="Jan Novak")


@pytest.fixture
def store(tmp_path):
    opened = Store(StoreTuning(path=tmp_path / "db" / "agent.db"))
    yield opened
    opened.close()


def manager(tmp_path, store, **overrides):
    web = WebSettings(_env_file=None, access_token=TOKEN, **overrides)
    return JobManager(tmp_path / "out", web, store=store)


async def settle(job, states=(JobState.DONE, JobState.FAILED, JobState.NEEDS_INPUT)):
    for _ in range(300):
        if job.state in states:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("job did not settle")


async def test_a_finished_job_is_written_to_the_store(tmp_path, store):
    thread = store.create_thread("research", "Chat")
    jobs = manager(tmp_path, store)
    jobs.attach(StubResearcher())
    job = jobs.start_research(WEB_OWNER, Goal.HIRING, None, JAN, OPTIONS, thread_id=thread.id)
    saved = store.job(job.id)
    assert saved.state == "running" and saved.thread_id == thread.id and saved.events == []
    await settle(job)
    saved = store.job(job.id)
    assert saved.state == "done" and saved.rating == 3.4 and saved.report["mode"] == "person"
    assert saved.events and saved.events[-1]["kind"] == "done"
    assert saved.request["identity"]["name"] == "Jan Novak"


async def test_a_new_manager_reads_finished_jobs_back_from_the_store(tmp_path, store):
    first = manager(tmp_path, store)
    first.attach(StubResearcher())
    job = first.start_research(WEB_OWNER, Goal.HIRING, None, JAN, OPTIONS)
    await settle(job)
    second = manager(tmp_path, store)
    loaded = second.get(job.id, WEB_OWNER)
    assert loaded is not None and loaded is not job
    assert loaded.state is JobState.DONE and loaded.title == "Jan Novak"
    assert loaded.report.identity.name == "Jan Novak"
    assert [event.seq for event in loaded.events] == [event.seq for event in job.events]
    assert second.get(job.id, "someone-else") is None


async def test_jobs_are_listed_from_the_store_with_their_rating(tmp_path, store):
    first = manager(tmp_path, store)
    first.attach(StubResearcher())
    job = first.start_research(WEB_OWNER, Goal.HIRING, None, JAN, OPTIONS)
    await settle(job)
    second = manager(tmp_path, store)
    listed = second.for_owner(WEB_OWNER)
    assert [item.id for item in listed] == [job.id]
    assert listed[0].report is None and listed[0].rating == 3.4
    assert listed[0].state is JobState.DONE
    assert second.for_owner("api") == []


async def test_a_job_that_was_running_during_a_restart_is_reported_as_interrupted(tmp_path, store):
    store.save_job(
        JobRecord(
            id="stale",
            owner=WEB_OWNER,
            kind="person",
            title="Jan Novak",
            state="running",
            stage="Merging findings",
            created=1.0,
            updated=1.0,
        )
    )
    jobs = manager(tmp_path, store)
    listed = jobs.for_owner(WEB_OWNER)
    assert listed[0].state is JobState.FAILED and "restart" in listed[0].error
    loaded = jobs.get("stale", WEB_OWNER)
    assert loaded.state is JobState.FAILED
    assert store.job("stale").state == "failed"


async def test_a_job_waiting_for_an_answer_can_be_answered_after_a_restart(tmp_path, store):
    first = manager(tmp_path, store)
    stub = StubResearcher()
    stub.results = [clarification()]
    first.attach(stub)
    job = first.start_research(WEB_OWNER, Goal.HIRING, None, JAN, OPTIONS)
    await settle(job)
    second = manager(tmp_path, store)
    second.attach(StubResearcher())
    waiting = second.get(job.id, WEB_OWNER)
    assert waiting.state is JobState.NEEDS_INPUT and waiting.clarification is not None
    second.answer(waiting, [ClarificationAnswer(network=Network.LINKEDIN, chosen_url=LI_A)])
    await settle(waiting, (JobState.DONE,))
    assert store.job(job.id).state == "done"


async def test_the_settled_callback_sees_done_failed_and_waiting_jobs(tmp_path, store):
    jobs = manager(tmp_path, store)
    stub = StubResearcher()
    stub.results = [AgentError("nope"), clarification()]
    jobs.attach(stub)
    seen = []
    jobs.on_settled = lambda job: seen.append((job.state, store.job(job.id).state))
    for _ in range(3):
        await settle(jobs.start_research(WEB_OWNER, Goal.HIRING, None, JAN, OPTIONS))
        await asyncio.sleep(0.02)
    assert seen == [
        (JobState.FAILED, "failed"),
        (JobState.NEEDS_INPUT, "needs_input"),
        (JobState.DONE, "done"),
    ]


async def test_a_failing_callback_never_breaks_the_job(tmp_path, store):
    jobs = manager(tmp_path, store)
    jobs.attach(StubResearcher())

    def explode(job):
        raise RuntimeError("callback failed")

    jobs.on_settled = explode
    job = jobs.start_research(WEB_OWNER, Goal.HIRING, None, JAN, OPTIONS)
    await settle(job)
    assert job.state is JobState.DONE and store.job(job.id).state == "done"


async def test_memory_eviction_keeps_the_job_in_the_store(tmp_path, store):
    now = [1000.0]
    web = WebSettings(_env_file=None, access_token=TOKEN, job_ttl_seconds=10)
    jobs = JobManager(tmp_path / "out", web, clock=lambda: now[0], store=store)
    jobs.attach(StubResearcher())
    job = jobs.start_research(WEB_OWNER, Goal.HIRING, None, JAN, OPTIONS)
    await settle(job)
    now[0] += 100
    jobs.for_owner(WEB_OWNER)
    reloaded = jobs.get(job.id, WEB_OWNER)
    assert reloaded is not None and reloaded is not job and reloaded.state is JobState.DONE


async def test_deleting_a_job_removes_it_from_the_store(tmp_path, store):
    jobs = manager(tmp_path, store)
    jobs.attach(StubResearcher())
    job = jobs.start_research(WEB_OWNER, Goal.HIRING, None, JAN, OPTIONS)
    await settle(job)
    jobs.delete(job)
    assert store.job(job.id) is None
    assert jobs.get(job.id, WEB_OWNER) is None


async def test_downloads_are_rendered_from_the_stored_report(tmp_path, store):
    first = manager(tmp_path, store)
    first.attach(StubResearcher())
    job = first.start_research(WEB_OWNER, Goal.HIRING, None, JAN, OPTIONS)
    await settle(job)
    loaded = manager(tmp_path, store).get(job.id, WEB_OWNER)
    assert loaded.exported is None
    assert b"Research report" in manager(tmp_path, store).read_export(loaded, "md")
    assert b'"mode"' in manager(tmp_path, store).read_export(loaded, "json")


async def test_unreadable_stored_jobs_are_skipped(tmp_path, store):
    store.save_job(
        JobRecord(
            id="bad",
            owner=WEB_OWNER,
            kind="person",
            title="x",
            state="done",
            stage="Done",
            report={"mode": "person"},
            created=1.0,
            updated=1.0,
        )
    )
    assert manager(tmp_path, store).get("bad", WEB_OWNER) is None


async def test_a_job_pointing_at_a_missing_thread_is_stored_without_the_link(tmp_path, store):
    jobs = manager(tmp_path, store)
    jobs.attach(StubResearcher())
    job = jobs.start_research(WEB_OWNER, Goal.HIRING, None, JAN, OPTIONS, thread_id="missing")
    await settle(job)
    assert store.job(job.id).thread_id is None
