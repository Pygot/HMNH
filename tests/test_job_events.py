# tests/test_job_events.py
from agent.models import (
    ClarificationAnswer,
    CompanyInput,
    Goal,
    Identity,
    Network,
    Requirement,
    RequirementKind,
    ResearchOptions,
)
from tests.web_helpers import (
    clarification,
    LI_A,
    ScriptedResearcher,
    StubResearcher,
    TOKEN,
)
from agent.events import (
    Kind,
    Task,
)
from agent.web.jobs import (
    JobManager,
    JobState,
)
from agent.web.settings import WebSettings
from agent.web.app import event_stream
from tests.fakes import hiring_report
from agent.config import EventTuning
from agent.errors import AgentError

import asyncio

OPTIONS = ResearchOptions()
JAN = Identity(name="Jan Novak")


def manager(tmp_path, tuning=None, **overrides):
    web = WebSettings(_env_file=None, access_token=TOKEN, **overrides)
    return JobManager(tmp_path / "out", web, tuning=tuning)


async def settle(job, states=(JobState.DONE, JobState.FAILED, JobState.NEEDS_INPUT)):
    for _ in range(300):
        if job.state in states:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("job did not settle")


class Noisy(StubResearcher):
    async def research(self, request, progress):
        progress.emit(
            Kind.NOTE,
            "x" * 500,
            Task.SEARCH,
            blob="y" * 500,
            items=list(range(80)),
            nested={"deep": ["z" * 500]},
            number=7,
        )
        for number in range(200):
            progress.emit(Kind.NOTE, f"note {number}", Task.SEARCH)
        return await super().research(request, progress)


async def test_a_run_records_its_events_in_order(tmp_path):
    jobs = manager(tmp_path)
    jobs.attach(ScriptedResearcher())
    job = jobs.start_research("s", Goal.HIRING, None, JAN, OPTIONS)
    await settle(job)
    kinds = [event.kind for event in job.events]
    assert [event.seq for event in job.events] == list(range(len(job.events)))
    assert kinds[-1] is Kind.DONE
    assert {Kind.COMPANY, Kind.SOURCE, Kind.FINDING, Kind.REQUIREMENT, Kind.RATING} <= set(kinds)
    assert job.events[-1].task is Task.EXPORT
    stages = [event.text for event in job.events if event.kind is Kind.STAGE]
    assert stages[-1] == "Exporting the report"
    assert job.stage == "Done"


async def test_stage_events_update_the_job_stage_and_task(tmp_path):
    jobs = manager(tmp_path)
    stub = StubResearcher()
    jobs.attach(stub)
    gate = stub.hold()
    job = jobs.start_research("s", Goal.HIRING, None, JAN, OPTIONS)
    await asyncio.sleep(0.05)
    assert job.stage == "Searching for public profiles" and job.task is None
    reporter = jobs.reporter(job)
    reporter("Merging findings", Task.MERGE)
    assert job.stage == "Merging findings" and job.task is Task.MERGE
    reporter.emit(Kind.NOTE, "a note", Task.SEARCH)
    assert job.stage == "Merging findings" and job.task is Task.MERGE
    gate.set()
    await jobs.close()


async def test_text_and_data_are_bounded(tmp_path):
    jobs = manager(tmp_path, tuning=EventTuning(text_chars=40))
    jobs.attach(Noisy())
    job = jobs.start_research("s", Goal.HIRING, None, JAN, OPTIONS)
    await settle(job)
    first = next(event for event in job.events if event.kind is Kind.NOTE)
    assert len(first.text) == 40
    assert len(first.data["blob"]) == 80
    assert len(first.data["items"]) == 50
    assert len(first.data["nested"]["deep"][0]) == 80
    assert first.data["number"] == 7


async def test_the_event_count_is_capped_but_terminal_events_are_kept(tmp_path):
    jobs = manager(tmp_path, tuning=EventTuning(max_events=50))
    jobs.attach(Noisy())
    job = jobs.start_research("s", Goal.HIRING, None, JAN, OPTIONS)
    await settle(job)
    assert len(job.events) == 51
    assert job.events[-1].kind is Kind.DONE
    assert job.state is JobState.DONE
    assert [event.seq for event in job.events] == list(range(51))


async def test_failures_are_recorded_as_a_failed_event(tmp_path):
    jobs = manager(tmp_path)
    stub = StubResearcher()
    stub.results = [AgentError("The search provider refused the request.")]
    jobs.attach(stub)
    job = jobs.start_research("s", Goal.HIRING, None, JAN, OPTIONS)
    await settle(job)
    assert job.events[-1].kind is Kind.FAILED
    assert job.events[-1].text == "The search provider refused the request."
    assert job.events[-1].task is None


async def test_unexpected_failures_never_leak_details_into_events(tmp_path):
    jobs = manager(tmp_path)
    stub = StubResearcher()
    stub.results = [RuntimeError("password is hunter2")]
    jobs.attach(stub)
    job = jobs.start_research("s", Goal.HIRING, None, JAN, OPTIONS)
    await settle(job)
    assert job.events[-1].kind is Kind.FAILED
    assert "hunter2" not in " ".join(event.text for event in job.events)


async def test_a_question_is_a_terminal_event_naming_the_networks(tmp_path):
    jobs = manager(tmp_path)
    stub = StubResearcher()
    stub.results = [clarification()]
    jobs.attach(stub)
    job = jobs.start_research("s", Goal.HIRING, None, JAN, OPTIONS)
    await settle(job)
    event = job.events[-1]
    assert event.kind is Kind.QUESTION and event.task is Task.WAIT
    assert event.data == {"networks": ["linkedin"]}
    assert job.task is Task.WAIT and job.stage == "Waiting for your answer"


async def test_answering_continues_the_same_event_log(tmp_path):
    jobs = manager(tmp_path)
    stub = StubResearcher()
    stub.results = [clarification()]
    jobs.attach(stub)
    job = jobs.start_research("s", Goal.HIRING, None, JAN, OPTIONS)
    await settle(job)
    before = len(job.events)
    jobs.answer(job, [ClarificationAnswer(network=Network.LINKEDIN, chosen_url=LI_A)])
    await settle(job, (JobState.DONE,))
    later = job.events[before:]
    assert later[0].kind is Kind.STEP and later[0].task is Task.WAIT
    assert later[0].text == "Continuing with your answers"
    assert later[-1].kind is Kind.DONE
    assert [event.seq for event in job.events] == list(range(len(job.events)))


async def test_events_after_pages_through_the_log(tmp_path):
    jobs = manager(tmp_path, tuning=EventTuning(page_size=3))
    jobs.attach(ScriptedResearcher())
    job = jobs.start_research("s", Goal.HIRING, None, JAN, OPTIONS)
    await settle(job)
    assert [event.seq for event in jobs.events_after(job, 0)] == [0, 1, 2]
    assert [event.seq for event in jobs.events_after(job, 2)] == [2, 3, 4]
    assert [event.seq for event in jobs.events_after(job, -5)] == [0, 1, 2]
    assert len(jobs.events_after(job, 0, 5)) == 5
    assert jobs.events_after(job, len(job.events)) == []


async def test_watch_streams_everything_and_ends_when_the_job_is_done(tmp_path):
    jobs = manager(tmp_path, tuning=EventTuning(page_size=4, poll_seconds=0.01))
    jobs.attach(ScriptedResearcher())
    job = jobs.start_research("s", Goal.HIRING, None, JAN, OPTIONS)
    seen = [event async for event in jobs.watch(job) if event is not None]
    assert [event.seq for event in seen] == list(range(len(job.events)))
    assert seen[-1].kind is Kind.DONE


async def test_watch_resumes_after_a_given_event(tmp_path):
    jobs = manager(tmp_path, tuning=EventTuning(poll_seconds=0.01))
    jobs.attach(ScriptedResearcher())
    job = jobs.start_research("s", Goal.HIRING, None, JAN, OPTIONS)
    await settle(job)
    resumed = [event async for event in jobs.watch(job, after=5) if event is not None]
    assert [event.seq for event in resumed] == list(range(5, len(job.events)))
    assert [event async for event in jobs.watch(job, after=len(job.events))] == []
    everything = [event async for event in jobs.watch(job, after=-3) if event is not None]
    assert len(everything) == len(job.events)


async def test_watch_sends_heartbeats_while_a_job_is_quiet(tmp_path):
    tuning = EventTuning(poll_seconds=0.01, heartbeat_seconds=0.03)
    jobs = manager(tmp_path, tuning=tuning)
    stub = StubResearcher()
    jobs.attach(stub)
    gate = stub.hold()
    job = jobs.start_research("s", Goal.HIRING, None, JAN, OPTIONS)
    beats = 0
    async for event in jobs.watch(job):
        if event is None:
            beats += 1
            if beats == 2:
                break
    gate.set()
    await settle(job)
    assert beats == 2


async def test_watch_gives_up_after_the_maximum_stream_time(tmp_path):
    tuning = EventTuning(poll_seconds=0.01, heartbeat_seconds=5.0, max_stream_seconds=0.05)
    jobs = manager(tmp_path, tuning=tuning)
    stub = StubResearcher()
    jobs.attach(stub)
    gate = stub.hold()
    job = jobs.start_research("s", Goal.HIRING, None, JAN, OPTIONS)
    seen = [event async for event in jobs.watch(job)]
    assert all(event is not None for event in seen)
    assert job.state is JobState.RUNNING
    gate.set()
    await jobs.close()


async def test_company_and_requirements_reach_the_request(tmp_path):
    jobs = manager(tmp_path)
    stub = ScriptedResearcher(hiring_report())
    jobs.attach(stub)
    company = CompanyInput(name="Acme", website="https://acme.example")
    wanted = [Requirement(kind=RequirementKind.LANGUAGE, label="English", level="C1")]
    job = jobs.start_research("s", Goal.HIRING, None, JAN, OPTIONS, company, wanted)
    await settle(job)
    request = stub.calls[0][1]
    assert request.company == company
    assert request.requirements == wanted


class Context:
    def __init__(self, jobs):
        self.jobs = jobs


async def test_the_sse_stream_sends_comment_pings_while_quiet_and_ends_with_an_end_event(tmp_path):
    tuning = EventTuning(poll_seconds=0.01, heartbeat_seconds=0.03)
    jobs = manager(tmp_path, tuning=tuning)
    stub = StubResearcher()
    jobs.attach(stub)
    gate = stub.hold()
    job = jobs.start_research("s", Goal.HIRING, None, JAN, OPTIONS)
    chunks = []
    async for chunk in event_stream(Context(jobs), job, 0):
        chunks.append(chunk)
        if chunk == ": ping\n\n":
            gate.set()
            await settle(job)
    assert ": ping\n\n" in chunks
    assert chunks[-1] == "event: end\ndata: {}\n\n"
    assert chunks[0].startswith("id: 0\ndata: ")
