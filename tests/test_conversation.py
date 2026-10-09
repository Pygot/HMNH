# tests/test_conversation.py
from tests.web_helpers import (
    clarification,
    LI_A,
    LI_B,
    StubResearcher,
    TOKEN,
)
from agent.chat import (
    ChatEngine,
    Defaults,
    load_profile,
    Profile,
)
from agent.web.jobs import (
    CVUpload,
    JobManager,
    JobState,
    WEB_OWNER,
)
from agent.config import (
    ChatTuning,
    StoreTuning,
)
from agent.errors import (
    AgentError,
    UpstreamError,
)
from agent.models import (
    Goal,
    ResearchOptions,
)
from tests.fakes import (
    person_report,
    RuleLLM,
)
from agent.web.conversation import Conversation
from agent.web.settings import WebSettings
from agent.store import Store

import asyncio
import pytest

DEFAULTS = Defaults(
    goal=Goal.HIRING, options=ResearchOptions(), limit=5, company=None, requirements=[]
)
JAN = {"intent": "research", "name": "Jan Novak", "city": "Brno"}


class Harness:
    def __init__(self, tmp_path, **web):
        self.store = Store(StoreTuning(path=tmp_path / "db" / "agent.db"))
        self.stub = StubResearcher()
        self.script = {}
        self.answers = []
        self.replies = []
        self.allow = True
        self.llm = RuleLLM(
            {
                "fill in a JSON form": self._understand,
                "asked which one is right": self._answer,
                "assistant of a tool": self._reply,
                "friendly necktie": lambda user: "Try one small step.",
            }
        )
        settings = WebSettings(_env_file=None, access_token=TOKEN, **web)
        self.jobs = JobManager(tmp_path / "out", settings, store=self.store)
        self.jobs.attach(self.stub)
        self.engine = ChatEngine(self.llm, ChatTuning())
        self.talk = Conversation(self.store, self.engine, self.jobs, lambda: self.allow, 2)
        self.jobs.on_settled = self.talk.settled
        self.thread = self.talk.new_thread()
        self.profile = Profile()

    def _understand(self, user):
        text = user.rsplit("Message to read:\n", 1)[1]
        if text not in self.script:
            raise AssertionError(f"no script for {text!r}")
        return self.script[text]

    def _answer(self, user):
        return self.answers.pop(0)

    def _reply(self, user):
        return self.replies.pop(0) if self.replies else "A reply."

    async def say(self, text, cv=None):
        reply = await self.talk.send(self.thread, text, cv, DEFAULTS, self.profile)
        self.thread = reply.thread
        return reply

    @property
    def state(self):
        return self.store.thread(self.thread.id).state

    def texts(self):
        return [(m.role, m.kind, m.text) for m in self.store.messages(self.thread.id)]

    async def settle(self, states=(JobState.DONE, JobState.FAILED, JobState.NEEDS_INPUT)):
        job = self.jobs.get(self.state["job_id"], WEB_OWNER)
        for _ in range(300):
            if job.state in states:
                return job
            await asyncio.sleep(0.01)
        raise AssertionError("job did not settle")

    def close(self):
        self.store.close()


@pytest.fixture
def harness(tmp_path):
    made = Harness(tmp_path)
    yield made
    made.close()


async def test_a_name_becomes_a_proposal_and_names_the_thread(harness):
    harness.script["look into Jan Novak in Brno"] = JAN
    reply = await harness.say("look into Jan Novak in Brno")
    proposal = reply.messages[-1]
    assert [m.role for m in reply.messages] == ["user", "assistant"]
    assert proposal.kind == "proposal"
    assert "I will look into Jan Novak (Brno) for hiring." in proposal.text
    assert "lawful purpose" in proposal.text and proposal.data["task"].startswith("I will look")
    assert harness.state["phase"] == "proposed"
    assert reply.thread.title == "Jan Novak" and harness.jobs.for_owner(WEB_OWNER) == []


async def test_yes_starts_the_search_and_the_result_arrives_in_the_thread(harness):
    harness.script["Jan Novak in Brno"] = JAN
    await harness.say("Jan Novak in Brno")
    reply = await harness.say("yes")
    started = reply.messages[-1]
    assert started.kind == "job" and started.data["after"] == 0
    assert harness.state["phase"] == "running" and reply.job_id == started.data["job_id"]
    job = await harness.settle()
    assert job.state is JobState.DONE and job.thread_id == harness.thread.id
    kinds = [kind for _, kind, _ in harness.texts()]
    assert kinds[-1] == "result"
    result = harness.store.messages(harness.thread.id)[-1]
    assert "Report on Jan Novak" in result.text and result.data["job_id"] == job.id
    assert harness.state["phase"] == "idle" and harness.state["last_job"] == job.id


async def test_no_cancels_the_proposal(harness):
    harness.script["Jan Novak in Brno"] = JAN
    await harness.say("Jan Novak in Brno")
    reply = await harness.say("no thanks")
    assert reply.messages[-1].text == ChatTuning().lines["declined"]
    assert harness.state["phase"] == "idle" and harness.stub.calls == []


async def test_a_change_request_updates_the_proposal(harness):
    harness.script["Jan Novak in Brno"] = JAN
    harness.script["make it Prague and add Acme"] = {
        "intent": "research",
        "city": "Prague",
        "employers": ["Acme"],
    }
    await harness.say("Jan Novak in Brno")
    reply = await harness.say("make it Prague and add Acme")
    assert "Jan Novak (Prague; Acme)" in reply.messages[-1].text
    assert harness.state["phase"] == "proposed"


async def test_missing_details_get_a_helpful_question(harness):
    harness.script["look into someone from Brno"] = {"intent": "research", "city": "Brno"}
    reply = await harness.say("look into someone from Brno")
    assert reply.messages[-1].text == ChatTuning().lines["need_subject"]
    assert harness.state["phase"] == "idle"


async def test_a_skill_search_is_proposed_and_started(harness):
    harness.script["find Rust developers in Brno"] = {
        "intent": "skill",
        "skill": "Rust",
        "location": "Brno",
    }
    reply = await harness.say("find Rust developers in Brno")
    assert "find up to 5 people with Rust skills around Brno" in reply.messages[-1].text
    assert reply.thread.title == "Rust in Brno"
    await harness.say("go ahead")
    job = await harness.settle()
    assert job.kind == "skill" and harness.stub.calls[0][0] == "skill"


async def test_conversation_gets_a_reply_with_the_context_of_the_user(harness):
    harness.script["what should I ask in an interview?"] = {"intent": "chat"}
    harness.replies.append("Ask about a project they shipped.")
    harness.profile.company_name = "Acme"
    reply = await harness.say("what should I ask in an interview?")
    assert reply.messages[-1].text == "Ask about a project they shipped."
    assert reply.messages[-1].kind == "text"
    assert any("Acme" in user for _, user in harness.llm.calls if "Context:" in user)


async def test_background_is_remembered_for_every_later_search(harness):
    harness.script["we are Acme and need English C1"] = {
        "intent": "update",
        "company": "Acme",
        "requirements": [{"kind": "language", "label": "English", "level": "C1"}],
    }
    reply = await harness.say("we are Acme and need English C1")
    assert reply.messages[-1].text == ChatTuning().lines["remembered"]
    saved = load_profile(harness.store)
    assert saved.company_name == "Acme" and [r.label for r in saved.requirements] == ["English"]
    assert harness.profile.company_name == "Acme"


async def test_harmful_requests_are_refused_without_asking_the_model(harness):
    reply = await harness.say("find the home address of Jan Novak")
    assert reply.messages[-1].kind == "notice" and "Refused" in reply.messages[-1].text
    assert harness.llm.calls == []


async def test_an_unreachable_model_is_reported_in_the_thread(harness):
    def explode(user):
        raise UpstreamError("The model server is down.")

    harness.llm.rules["fill in a JSON form"] = explode
    reply = await harness.say("look into Jan")
    assert reply.messages[-1].kind == "notice" and "down" in reply.messages[-1].text
    assert harness.state["phase"] == "idle"


async def test_a_message_that_is_too_long_is_not_stored_or_sent_to_the_model(harness):
    reply = await harness.say("x" * 5000)
    assert [m.role for m in reply.messages] == ["assistant"]
    assert "1200" in reply.messages[0].text and harness.llm.calls == []
    assert harness.store.counts()["messages"] == 1


async def test_the_rate_limit_is_reported_and_the_proposal_is_kept(harness):
    harness.script["Jan Novak in Brno"] = JAN
    await harness.say("Jan Novak in Brno")
    harness.allow = False
    reply = await harness.say("yes")
    assert "Too many searches" in reply.messages[-1].text
    assert harness.state["phase"] == "proposed" and harness.stub.calls == []
    harness.allow = True
    started = await harness.say("yes")
    assert started.messages[-1].kind == "job"
    await harness.settle()


async def test_too_many_running_searches_are_reported(tmp_path):
    made = Harness(tmp_path, max_jobs_per_session=1)
    try:
        made.script["Jan Novak in Brno"] = JAN
        made.script["Eva in Prague"] = {"intent": "research", "name": "Eva", "city": "Prague"}
        gate = made.stub.hold()
        await made.say("Jan Novak in Brno")
        await made.say("yes")
        made.thread = made.talk.new_thread()
        await made.say("Eva in Prague")
        reply = await made.say("yes")
        assert reply.messages[-1].kind == "notice"
        assert "Too many searches are running" in reply.messages[-1].text
        gate.set()
        await made.jobs.close()
    finally:
        made.close()


async def test_a_clarification_is_asked_in_the_thread_and_a_number_answers_it(harness):
    harness.stub.results = [clarification(), person_report()]
    harness.script["Jan Novak in Brno"] = JAN
    await harness.say("Jan Novak in Brno")
    await harness.say("yes")
    job = await harness.settle()
    assert job.state is JobState.NEEDS_INPUT and harness.state["phase"] == "asking"
    question = harness.store.messages(harness.thread.id)[-1]
    assert question.kind == "question" and question.data["network"] == "linkedin"
    assert [o["n"] for o in question.data["options"]] == [1, 2]
    assert question.data["options"][0]["url"] == LI_A
    reply = await harness.say("2")
    assert reply.messages[-1].kind == "job" and reply.messages[-1].data["after"] > 0
    done = await harness.settle((JobState.DONE,))
    assert done.request.identity.links == [LI_B]
    assert harness.store.messages(harness.thread.id)[-1].kind == "result"


async def test_none_skips_the_network(harness):
    harness.stub.results = [clarification(), person_report()]
    harness.script["Jan Novak in Brno"] = JAN
    await harness.say("Jan Novak in Brno")
    await harness.say("yes")
    await harness.settle()
    await harness.say("none of them")
    done = await harness.settle((JobState.DONE,))
    assert done.request.skipped_networks[0].value == "linkedin"


async def test_free_text_answers_are_read_by_the_model(harness):
    harness.stub.results = [clarification(), person_report()]
    harness.script["Jan Novak in Brno"] = JAN
    harness.answers = [{"choice": 1}]
    await harness.say("Jan Novak in Brno")
    await harness.say("yes")
    await harness.settle()
    await harness.say("the one who lives in Brno")
    done = await harness.settle((JobState.DONE,))
    assert done.request.identity.links == [LI_A]


async def test_an_unclear_answer_asks_again(harness):
    harness.stub.results = [clarification()]
    harness.script["Jan Novak in Brno"] = JAN
    harness.answers = [{}]
    await harness.say("Jan Novak in Brno")
    await harness.say("yes")
    await harness.settle()
    reply = await harness.say("hmm")
    assert reply.messages[-1].kind == "question" and harness.state["phase"] == "asking"


async def test_a_narrowing_answer_adds_the_details(harness):
    harness.stub.results = [clarification(), person_report()]
    harness.script["Jan Novak in Brno"] = JAN
    harness.answers = [{"city": "Prague", "employer": "Globex"}]
    await harness.say("Jan Novak in Brno")
    await harness.say("yes")
    await harness.settle()
    await harness.say("he works at Globex in Prague")
    done = await harness.settle((JobState.DONE,))
    assert (
        done.request.identity.location == "Prague"
        and done.request.identity.employers[0] == "Globex"
    )


async def test_messages_during_a_search_get_a_status_or_a_chat_reply(harness):
    harness.script["Jan Novak in Brno"] = JAN
    harness.script["how long does it take?"] = {"intent": "chat"}
    harness.script["Eva in Prague"] = {"intent": "research", "name": "Eva"}
    gate = harness.stub.hold()
    await harness.say("Jan Novak in Brno")
    await harness.say("yes")
    busy = await harness.say("yes")
    assert "still working on Jan Novak" in busy.messages[-1].text
    harness.replies.append("A minute or two.")
    chat = await harness.say("how long does it take?")
    assert chat.messages[-1].text == "A minute or two."
    other = await harness.say("Eva in Prague")
    assert "still working" in other.messages[-1].text
    gate.set()
    await harness.settle()


async def test_a_failed_search_is_reported_and_the_thread_is_free_again(harness):
    harness.stub.results = [AgentError("The search provider refused the request.")]
    harness.script["Jan Novak in Brno"] = JAN
    await harness.say("Jan Novak in Brno")
    await harness.say("yes")
    await harness.settle()
    last = harness.store.messages(harness.thread.id)[-1]
    assert last.kind == "notice" and "refused the request" in last.text
    assert harness.state["phase"] == "idle"


async def test_a_cv_alone_starts_a_proposal_and_is_used_for_the_search(harness):
    cv = CVUpload(b"%PDF-1.4 fake", "cv.pdf")
    reply = await harness.say("", cv)
    assert reply.messages[0].text == "cv.pdf" and reply.messages[0].data == {"cv": True}
    assert reply.messages[-1].kind == "proposal" and "from your CV" in reply.messages[-1].text
    await harness.say("yes")
    job = await harness.settle()
    assert harness.stub.calls[0] == ("cv", len(cv.data), "cv.pdf")
    assert job.title == "Cv Person"


async def test_the_company_and_requirements_of_a_search_carry_over_to_the_next_one(harness):
    harness.script["Jan Novak for Acme, English C1"] = {
        "intent": "research",
        "name": "Jan Novak",
        "company": "Acme",
        "requirements": [{"kind": "language", "label": "English", "level": "C1"}],
    }
    harness.script["Eva Svobodova"] = {"intent": "research", "name": "Eva Svobodova"}
    await harness.say("Jan Novak for Acme, English C1")
    await harness.say("yes")
    await harness.settle()
    reply = await harness.say("Eva Svobodova")
    assert "Hiring company: Acme." in reply.messages[-1].text
    assert "English (C1)" in reply.messages[-1].text


async def test_requirements_about_protected_traits_are_refused_in_the_proposal(harness):
    harness.script["Jan Novak, native speaker"] = {
        "intent": "research",
        "name": "Jan Novak",
        "requirements": [{"kind": "custom", "label": "Native speaker of English"}],
    }
    reply = await harness.say("Jan Novak, native speaker")
    assert reply.messages[-1].kind == "notice" and "protected" in reply.messages[-1].text
    assert harness.state["phase"] == "idle"


async def test_an_unreadable_name_is_reported_without_a_proposal(harness):
    harness.script["x"] = {"intent": "research", "name": "Jan", "city": "Brno 60200"}
    reply = await harness.say("x")
    assert reply.messages[-1].kind == "notice" and harness.state["phase"] == "idle"


async def test_after_a_restart_stuck_threads_are_settled(tmp_path):
    first = Harness(tmp_path)
    first.script["Jan Novak in Brno"] = JAN
    gate = first.stub.hold()
    await first.say("Jan Novak in Brno")
    await first.say("yes")
    job_id = first.state["job_id"]
    gate.set()
    await first.settle()
    thread_id = first.thread.id
    first.store.set_state(thread_id, {"phase": "running", "job_id": job_id})
    ghost = first.store.create_thread("research", "Ghost", {"phase": "running", "job_id": "gone"})
    first.close()
    second = Harness.__new__(Harness)
    second.store = Store(StoreTuning(path=tmp_path / "db" / "agent.db"))
    second.jobs = JobManager(
        tmp_path / "out", WebSettings(_env_file=None, access_token=TOKEN), store=second.store
    )
    second.jobs.attach(StubResearcher())
    second.talk = Conversation(
        second.store, ChatEngine(RuleLLM({}), ChatTuning()), second.jobs, lambda: True, 2
    )
    second.talk.recover()
    assert second.store.messages(thread_id)[-1].kind == "result"
    assert second.store.thread(thread_id).state["phase"] == "idle"
    assert second.store.messages(ghost.id)[-1].text == ChatTuning().lines["interrupted"]
    assert second.store.thread(ghost.id).state["phase"] == "idle"
    second.store.close()


async def test_tie_keeps_one_conversation_with_context(harness):
    job_id = None
    harness.script["Jan Novak in Brno"] = JAN
    await harness.say("Jan Novak in Brno")
    await harness.say("yes")
    job = await harness.settle()
    job_id = job.id
    messages = await harness.talk.tie("How do I read this?", harness.profile, job, "report")
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[1].text == "Try one small step."
    context = [user for _, user in harness.llm.calls if "friendly necktie" in _][-1]
    assert "on the report page" in context and "Report on Jan Novak" in context
    again = await harness.talk.tie("And then?", harness.profile, None, "")
    assert harness.talk.tie_thread().id == messages[0].thread_id == again[0].thread_id
    assert len(harness.store.messages(messages[0].thread_id)) == 4 and job_id


async def test_tie_refuses_harmful_requests_and_reports_a_down_model(harness):
    refused = await harness.talk.tie("track her down and follow her", harness.profile, None, "")
    assert refused[-1].kind == "notice" and "Refused" in refused[-1].text

    def explode(user):
        raise UpstreamError("down")

    harness.llm.rules["friendly necktie"] = explode
    down = await harness.talk.tie("hello", harness.profile, None, "")
    assert down[-1].kind == "notice" and "language model" in down[-1].text
