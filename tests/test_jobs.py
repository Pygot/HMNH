# tests/test_jobs.py
from agent.models import (
    ClarificationAnswer,
    Goal,
    Identity,
    Network,
    ResearchOptions,
    ScepticismMode,
    SkillSearchRequest,
)
from agent.web.jobs import (
    CVUpload,
    JobManager,
    JobState,
    TooManyJobs,
)
from tests.web_helpers import (
    clarification,
    LI_A,
    StubResearcher,
    TOKEN,
)
from agent.errors import (
    AgentError,
    ConfigError,
)
from agent.web.settings import WebSettings
from tests.fakes import person_report

import asyncio
import pytest

OPTIONS = ResearchOptions()
JAN = Identity(name="Jan Novak")
EVA = Identity(name="Eva Svobodova")


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def manager(tmp_path, clock=None, **overrides):
    values = {"job_ttl_seconds": 100, "max_concurrent_jobs": 2, "max_jobs_per_session": 2}
    web = WebSettings(_env_file=None, access_token=TOKEN, **{**values, **overrides})
    return JobManager(tmp_path / "out", web, clock or Clock())


async def settle(job, states=(JobState.DONE, JobState.FAILED, JobState.NEEDS_INPUT)):
    for _ in range(200):
        if job.state in states:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("job did not settle")


async def test_manager_requires_a_pipeline_before_use(tmp_path):
    jobs = manager(tmp_path)
    assert jobs.ready is False
    with pytest.raises(ConfigError, match="Connections"):
        _ = jobs.unavailable_networks
    with pytest.raises(ConfigError, match="Connections"):
        jobs.start_research("s", Goal.HIRING, None, JAN, OPTIONS)
    assert jobs.for_owner("s") == []
    jobs.attach(StubResearcher())
    assert jobs.ready is True


async def test_finished_jobs_expire_but_running_ones_do_not(tmp_path):
    clock = Clock()
    jobs = manager(tmp_path, clock)
    stub = StubResearcher()
    jobs.attach(stub)
    done = jobs.start_research("s", Goal.HIRING, None, JAN, OPTIONS)
    await settle(done)
    gate = stub.hold()
    running = jobs.start_research("s", Goal.HIRING, None, EVA, OPTIONS)
    await asyncio.sleep(0.05)
    clock.now = 1000
    assert jobs.get(done.id, "s") is None
    assert jobs.get(running.id, "s") is running
    assert [job.id for job in jobs.for_owner("s")] == [running.id]
    gate.set()
    await jobs.close()


async def test_the_job_lifetime_comes_from_the_web_settings(tmp_path):
    clock = Clock()
    jobs = manager(tmp_path, clock, job_ttl_seconds=10)
    jobs.attach(StubResearcher())
    job = jobs.start_research("s", Goal.HIRING, None, JAN, OPTIONS)
    await settle(job)
    clock.now = 9
    assert jobs.get(job.id, "s") is job
    clock.now = 11
    assert jobs.get(job.id, "s") is None


async def test_jobs_are_scoped_to_their_owner(tmp_path):
    jobs = manager(tmp_path)
    jobs.attach(StubResearcher())
    job = jobs.start_research("owner", Goal.HIRING, None, JAN, OPTIONS)
    await settle(job)
    assert jobs.get(job.id, "owner") is job
    assert jobs.get(job.id, "intruder") is None
    assert jobs.for_owner("intruder") == []


async def test_job_identifiers_are_unguessable_and_their_size_is_configurable(tmp_path):
    jobs = manager(tmp_path)
    jobs.attach(StubResearcher())
    first = jobs.start_research("s", Goal.HIRING, None, JAN, OPTIONS)
    second = jobs.start_research("s", Goal.HIRING, None, EVA, OPTIONS)
    assert first.id != second.id and len(first.id) >= 24
    await jobs.close()
    short = manager(tmp_path, job_id_bytes=6)
    short.attach(StubResearcher())
    assert len(short.start_research("s", Goal.HIRING, None, JAN, OPTIONS).id) < 12
    await short.close()


async def test_the_options_reach_the_request(tmp_path):
    jobs = manager(tmp_path)
    stub = StubResearcher()
    jobs.attach(stub)
    options = ResearchOptions(networks=[Network.WEB], scepticism=ScepticismMode.STRICT)
    job = jobs.start_research("s", Goal.SALES, "pricing", JAN, options)
    await settle(job)
    request = stub.calls[0][1]
    assert request.options == options
    assert request.focus == "pricing"
    assert request.goal is Goal.SALES
    assert request.purpose_confirmed is True


async def test_skill_searches_are_jobs_too(tmp_path):
    jobs = manager(tmp_path)
    stub = StubResearcher()
    jobs.attach(stub)
    request = SkillSearchRequest(
        goal=Goal.HIRING, skill="Rust", location="Brno", purpose_confirmed=True
    )
    job = jobs.start_skill_search("s", request)
    await settle(job)
    assert job.kind == "skill" and job.title == "Rust in Brno"
    assert job.state is JobState.DONE and job.exported is not None
    assert stub.calls[0] == ("skill", request)


async def test_close_cancels_running_work(tmp_path):
    jobs = manager(tmp_path)
    stub = StubResearcher()
    jobs.attach(stub)
    gate = stub.hold()
    job = jobs.start_research("s", Goal.HIRING, None, JAN, OPTIONS)
    await asyncio.sleep(0.05)
    await jobs.close()
    gate.set()
    assert job.state is JobState.RUNNING


async def test_answers_are_validated_against_the_offered_questions(tmp_path):
    jobs = manager(tmp_path)
    stub = StubResearcher()
    stub.results = [clarification(), person_report()]
    jobs.attach(stub)
    job = jobs.start_research("s", Goal.HIRING, None, JAN, OPTIONS)
    await settle(job)
    assert job.state is JobState.NEEDS_INPUT
    with pytest.raises(AgentError, match="Every question"):
        jobs.answer(job, [])
    with pytest.raises(AgentError, match="Every question"):
        jobs.answer(job, [ClarificationAnswer(network=Network.FACEBOOK)])
    with pytest.raises(AgentError, match="offered options"):
        jobs.answer(
            job,
            [ClarificationAnswer(network=Network.LINKEDIN, chosen_url="https://x.example/in/a")],
        )
    jobs.answer(job, [ClarificationAnswer(network=Network.LINKEDIN, chosen_url=LI_A)])
    await settle(job, (JobState.DONE,))
    assert job.report is not None and job.exported is not None
    assert stub.calls[1][1].options == OPTIONS


async def test_a_finished_job_cannot_be_answered(tmp_path):
    jobs = manager(tmp_path)
    jobs.attach(StubResearcher())
    job = jobs.start_research("s", Goal.HIRING, None, JAN, OPTIONS)
    await settle(job)
    with pytest.raises(AgentError, match="not waiting"):
        jobs.answer(job, [])


async def test_cv_failures_are_reported_as_job_errors(tmp_path):
    class Rejecting(StubResearcher):
        async def identity_from_cv(self, data, filename):
            raise AgentError("The PDF could not be read.")

    jobs = manager(tmp_path)
    jobs.attach(Rejecting())
    job = jobs.start_research("s", Goal.HIRING, None, CVUpload(b"%PDF-", "cv.pdf"), OPTIONS)
    await settle(job)
    assert job.state is JobState.FAILED
    assert job.error == "The PDF could not be read."
    assert job.exported is None


async def test_the_cv_name_becomes_the_job_title(tmp_path):
    jobs = manager(tmp_path)
    jobs.attach(StubResearcher())
    job = jobs.start_research("s", Goal.HIRING, None, CVUpload(b"%PDF-", "cv.pdf"), OPTIONS)
    assert job.title == "CV upload"
    await settle(job)
    assert job.title == "Cv Person"


async def test_unexpected_errors_are_generic(tmp_path):
    jobs = manager(tmp_path)
    stub = StubResearcher()
    stub.results = [RuntimeError("database password is hunter2")]
    jobs.attach(stub)
    job = jobs.start_research("s", Goal.HIRING, None, JAN, OPTIONS)
    await settle(job)
    assert job.state is JobState.FAILED
    assert "hunter2" not in job.error and "unexpected error" in job.error


async def test_deleting_a_running_job_cancels_it(tmp_path):
    jobs = manager(tmp_path)
    stub = StubResearcher()
    jobs.attach(stub)
    stub.hold()
    job = jobs.start_research("s", Goal.HIRING, None, JAN, OPTIONS)
    await asyncio.sleep(0.05)
    jobs.delete(job)
    await asyncio.sleep(0.05)
    assert jobs.get(job.id, "s") is None
    assert job.state is JobState.RUNNING
    assert jobs.read_export(job, "md") is None
    await jobs.close()


async def test_owner_job_limit_applies_to_running_jobs_only(tmp_path):
    jobs = manager(tmp_path, max_jobs_per_session=1)
    stub = StubResearcher()
    jobs.attach(stub)
    gate = stub.hold()
    first = jobs.start_research("s", Goal.HIRING, None, JAN, OPTIONS)
    with pytest.raises(TooManyJobs):
        jobs.start_research("s", Goal.HIRING, None, EVA, OPTIONS)
    other = jobs.start_research("t", Goal.HIRING, None, EVA, OPTIONS)
    gate.set()
    await settle(first)
    await settle(other)
    jobs.start_research("s", Goal.HIRING, None, Identity(name="Petr Svoboda"), OPTIONS)
    await jobs.close()


async def test_answering_counts_against_the_owner_limit(tmp_path):
    jobs = manager(tmp_path, max_jobs_per_session=1)
    stub = StubResearcher()
    stub.results = [clarification()]
    jobs.attach(stub)
    waiting = jobs.start_research("s", Goal.HIRING, None, JAN, OPTIONS)
    await settle(waiting)
    gate = stub.hold()
    jobs.start_research("s", Goal.HIRING, None, EVA, OPTIONS)
    with pytest.raises(TooManyJobs):
        jobs.answer(waiting, [ClarificationAnswer(network=Network.LINKEDIN, chosen_url=LI_A)])
    gate.set()
    await jobs.close()


async def test_exports_can_be_read_back_and_are_rendered_again_when_the_files_vanish(tmp_path):
    jobs = manager(tmp_path)
    jobs.attach(StubResearcher())
    job = jobs.start_research("s", Goal.HIRING, None, JAN, OPTIONS)
    await settle(job)
    assert b"Research report" in jobs.read_export(job, "md")
    assert b'"mode"' in jobs.read_export(job, "json")
    job.exported.markdown.unlink()
    assert b"Research report" in jobs.read_export(job, "md")
    jobs.delete(job)
    assert not job.exported.json.exists()
    assert jobs.get(job.id, "s") is None
