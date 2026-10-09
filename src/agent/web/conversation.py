# src/agent/web/conversation.py
from agent.chat import (
    answer_for,
    build_plan,
    ChatEngine,
    Defaults,
    describe_plan,
    is_confirm,
    is_decline,
    is_none,
    merge_requirements,
    pick_option,
    Plan,
    plan_for_cv,
    plan_title,
    Profile,
    save_profile,
    Understanding,
)
from agent.models import (
    Clarification,
    ClarificationAnswer,
    CompanyInput,
    Network,
    PersonReport,
    Requirement,
    ResearchRequest,
    SkillSearchRequest,
)
from agent.web.jobs import (
    CVUpload,
    Job,
    JobManager,
    JobState,
    TooManyJobs,
    WEB_OWNER,
)
from agent.compliance import (
    check_research_request,
    check_skill_request,
    refuse_harmful_requests,
)
from agent.store import (
    Message,
    Store,
    Thread,
)
from agent.errors import (
    AgentError,
    ComplianceRefusal,
)
from agent.speech import describe_report
from agent.text import describe_errors
from collections.abc import Callable
from pydantic import ValidationError
from dataclasses import dataclass
from typing import Any

import logging

logger = logging.getLogger(__name__)
NEW_TITLE = "New chat"
TIE_KIND = "tie"
RESEARCH_KIND = "research"
CONTEXT_FINDINGS = 8


@dataclass(frozen=True)
class Reply:
    """The messages and job produced by one conversation turn.

    Holds the thread as it is after the turn, the new messages in the order they were
    stored, and the id of the job tied to the thread, if any.
    """

    thread: Thread
    messages: list[Message]
    job_id: str | None = None


def profile_context(profile: Profile) -> str:
    """Describe the user's company and standing requirements as plain text.

    Args:
        profile: The saved hiring profile of the user.

    Returns:
        Sentences for the language model, or an empty string when the profile is empty.
    """
    parts = []
    company = profile.company()
    if company is not None:
        site = f" ({company.website})" if company.website else ""
        about = f". {company.description}" if company.description else ""
        parts.append(f"The user hires for {company.name}{site}{about}.")
    if profile.requirements:
        listing = "; ".join(item.description for item in profile.requirements)
        parts.append(f"Standing requirements: {listing}.")
    return " ".join(parts)


def job_context(job: Job, spoken_flags: int) -> str:
    """Describe the finished report of a job as plain text for the language model.

    Args:
        job: The job whose report is described.
        spoken_flags: How many scepticism flags the spoken summary may include.

    Returns:
        The summary, plus the top findings and requirement results for a person
        report, or an empty string when the job has no report yet.
    """
    report = job.report
    if report is None:
        return ""
    lines = [describe_report(report, spoken_flags)]
    if isinstance(report, PersonReport):
        lines += [
            f"Finding ({finding.tag.value}): {finding.statement}"
            for finding in report.findings[:CONTEXT_FINDINGS]
        ]
        if report.requirements is not None:
            lines += [
                f"Requirement {item.requirement.description}: {item.status.value}"
                for item in report.requirements.results
            ]
    return "\n".join(lines)


class Conversation:
    """Drive the chat flow of research threads and of the Tie assistant.

    The phase of a thread (idle, proposed, running or asking) lives in the thread state
    in the store, so a flow survives a restart. Uploaded CVs are kept in memory only.
    """

    def __init__(
        self,
        store: Store,
        engine: ChatEngine,
        jobs: JobManager,
        admit: Callable[[], bool],
        spoken_flags: int,
    ):
        """Initialize the conversation with its store, language engine and job manager.

        Args:
            store: The store that holds threads and messages.
            engine: The chat engine that understands text and writes replies.
            jobs: The manager that starts and tracks research jobs.
            admit: Callable that returns False when the user started too many searches.
            spoken_flags: How many scepticism flags a spoken report summary includes.
        """
        self._store = store
        self._engine = engine
        self._jobs = jobs
        self._admit = admit
        self._spoken_flags = spoken_flags
        self._cvs: dict[str, CVUpload] = {}

    def say(
        self, thread_id: str, text: str, kind: str = "text", data: dict[str, Any] | None = None
    ) -> Message:
        """Store an assistant message in a thread.

        Args:
            thread_id: The id of the thread that receives the message.
            text: The message text.
            kind: The message kind, such as text, notice, proposal, job or result.
            data: Optional structured data attached to the message.

        Returns:
            The stored message.
        """
        return self._store.add_message(thread_id, "assistant", kind, text, data)

    def new_thread(self) -> Thread:
        """Create an empty research thread in the idle phase.

        Returns:
            The new thread.
        """
        return self._store.create_thread(RESEARCH_KIND, NEW_TITLE, {"phase": "idle"})

    def tie_thread(self) -> Thread:
        """Return the thread of the Tie assistant, creating it on first use.

        Returns:
            The existing Tie thread, or a new one when none exists.
        """
        found = self._store.threads(TIE_KIND, 1)
        return found[0] if found else self._store.create_thread(TIE_KIND, "Tie")

    def _state(self, thread: Thread) -> dict[str, Any]:
        """Read the current state of a thread from the store.

        Args:
            thread: The thread whose state is wanted.

        Returns:
            A copy of the stored state, or an empty dict when the thread is gone.
        """
        fresh = self._store.thread(thread.id)
        return dict(fresh.state) if fresh else {}

    def _save(self, thread: Thread, state: dict[str, Any]) -> None:
        """Replace the stored state of a thread.

        Args:
            thread: The thread to update.
            state: The new state.
        """
        self._store.set_state(thread.id, state)

    def _defaults(self, base: Defaults, state: dict[str, Any]) -> Defaults:
        """Merge the brief saved in a thread into the default company and requirements.

        An unreadable brief is logged and ignored.

        Args:
            base: The defaults that apply to every thread.
            state: The thread state that may hold a brief.

        Returns:
            Defaults with the thread company and the merged requirements.
        """
        brief = state.get("brief") or {}
        company = base.company
        requirements = list(base.requirements)
        try:
            if brief.get("company"):
                company = CompanyInput.model_validate(brief["company"])
            extra = [Requirement.model_validate(item) for item in brief.get("requirements", [])]
            requirements = merge_requirements(requirements, extra)
        except ValidationError:
            logger.warning("a thread brief could not be read")
        return Defaults(
            goal=base.goal,
            options=base.options,
            limit=base.limit,
            company=company,
            requirements=requirements,
        )

    def _last_job_context(self, state: dict[str, Any]) -> str:
        """Describe the most recent job of a thread for the language model.

        Args:
            state: The thread state that names the last or current job.

        Returns:
            The job summary, or an empty string when there is no readable job.
        """
        job_id = state.get("last_job") or state.get("job_id")
        job = self._jobs.get(job_id, WEB_OWNER) if job_id else None
        return job_context(job, self._spoken_flags) if job is not None else ""

    async def send(
        self,
        thread: Thread,
        text: str,
        cv: CVUpload | None,
        base: Defaults,
        profile: Profile,
        can_research: bool = True,
    ) -> Reply:
        """Handle one user message and advance the thread.

        A message over the length limit gets a notice and is not stored. Otherwise the user
        message is stored, a new thread is titled from the text, harmful requests are
        refused, and the message is routed by the phase of the thread.

        Args:
            thread: The thread that receives the message.
            text: The text the user typed.
            cv: The CV uploaded with the message, if any.
            base: The default company and requirements for the user.
            profile: The saved hiring profile of the user.
            can_research: Whether the user may confirm and start a research job.

        Returns:
            The reply with the thread, the new messages and the job id.
        """
        tuning = self._engine.tuning
        text = text.strip()
        if len(text) > tuning.max_user_chars:
            notice = self.say(
                thread.id, self._engine.line("limit", count=tuning.max_user_chars), "notice"
            )
            return Reply(thread, [notice])
        label = text or (cv.filename if cv else "")
        sent = self._store.add_message(thread.id, "user", "text", label, {"cv": cv is not None})
        out = [sent]
        state = self._state(thread)
        if thread.title == NEW_TITLE and text:
            thread = self._rename(thread, text)
        try:
            refuse_harmful_requests([text])
        except ComplianceRefusal as refusal:
            out.append(self.say(thread.id, str(refusal), "notice"))
            return Reply(thread, out)
        try:
            await self._route(thread, state, text, cv, base, profile, out, can_research)
        except AgentError as error:
            out.append(
                self.say(thread.id, self._engine.line("model_down", error=str(error)), "notice")
            )
        # The handlers change state in place, so job_id here includes a job started in this turn.
        return Reply(self._store.thread(thread.id) or thread, out, state.get("job_id"))

    def _rename(self, thread: Thread, text: str) -> Thread:
        """Title a thread from the first words of the user's text.

        Args:
            thread: The thread to rename.
            text: The text the title is cut from.

        Returns:
            A copy of the thread with the new title.
        """
        title = " ".join(text.split())[: self._engine.tuning.title_chars]
        self._store.rename_thread(thread.id, title)
        return thread.model_copy(update={"title": title})

    async def _route(
        self,
        thread: Thread,
        state: dict[str, Any],
        text: str,
        cv: CVUpload | None,
        base: Defaults,
        profile: Profile,
        out: list[Message],
        can_research: bool = True,
    ) -> None:
        """Pass a message to the handler for the current phase of the thread.

        Args:
            thread: The thread that received the message.
            state: The mutable thread state.
            text: The text the user typed.
            cv: The CV uploaded with the message, if any.
            base: The default company and requirements.
            profile: The saved hiring profile of the user.
            out: The list that collects the messages of this turn.
            can_research: Whether the user may start a research job.
        """
        phase = state.get("phase", "idle")
        defaults = self._defaults(base, state)
        if phase == "asking":
            await self._answer(thread, state, text, out)
        elif phase == "proposed":
            await self._reply_to_proposal(thread, state, text, defaults, profile, out, can_research)
        elif phase == "running":
            await self._while_running(thread, state, text, profile, out)
        elif cv is not None and not text:
            self._cv_plan(thread, state, cv, defaults, out)
        else:
            await self._from_idle(thread, state, text, cv, defaults, profile, out)

    def _cv_plan(
        self,
        thread: Thread,
        state: dict[str, Any],
        cv: CVUpload,
        defaults: Defaults,
        out: list[Message],
    ) -> None:
        """Remember an uploaded CV and propose a research plan for it.

        Args:
            thread: The thread that received the CV.
            state: The mutable thread state.
            cv: The uploaded CV.
            defaults: The defaults used to build the plan.
            out: The list that collects the messages of this turn.
        """
        self._cvs[thread.id] = cv
        out.append(self.say(thread.id, self._engine.line("cv_received"), "text"))
        self._propose(thread, state, plan_for_cv(defaults), out)

    async def _from_idle(
        self,
        thread: Thread,
        state: dict[str, Any],
        text: str,
        cv: CVUpload | None,
        defaults: Defaults,
        profile: Profile,
        out: list[Message],
    ) -> None:
        """Interpret a message sent while the thread is idle.

        Args:
            thread: The thread that received the message.
            state: The mutable thread state.
            text: The text the user typed.
            cv: The CV uploaded with the message, if any.
            defaults: The defaults used to build a plan.
            profile: The saved hiring profile of the user.
            out: The list that collects the messages of this turn.
        """
        if cv is not None:
            self._cvs[thread.id] = cv
        understanding = await self._engine.understand(text, self._history(thread))
        current = plan_for_cv(defaults) if cv is not None else None
        await self._act(thread, state, understanding, current, defaults, profile, text, out)

    async def _act(self, thread, state, understanding, current, defaults, profile, text, out):
        """Act on what the engine understood from a message.

        A research or skill intent, or any change to an existing plan, produces a proposal.
        An update intent is saved to the profile, and anything else gets a chat reply.

        Args:
            thread: The thread that received the message.
            state: The mutable thread state.
            understanding: The intent and details read from the text.
            current: The plan being revised, or None.
            defaults: The defaults used to build a plan.
            profile: The saved hiring profile of the user.
            text: The text the user typed.
            out: The list that collects the messages of this turn.
        """
        intent = understanding.intent
        if intent in ("research", "skill") or (current is not None and intent != "chat"):
            try:
                plan = build_plan(understanding, current, defaults)
            except ValidationError as error:
                out.append(self.say(thread.id, describe_errors(error), "notice"))
                return
            if plan is None:
                out.append(self.say(thread.id, self._engine.line("need_subject"), "text"))
                return
            self._propose(thread, state, plan, out)
        elif intent == "update":
            self._remember(profile, understanding)
            out.append(self.say(thread.id, self._engine.line("remembered"), "text"))
        else:
            reply = await self._engine.converse(
                "assistant", text, self._history(thread), self._context(profile, state)
            )
            out.append(self.say(thread.id, reply, "text"))

    def _remember(self, profile: Profile, understanding: Understanding) -> None:
        """Save facts from a message into the hiring profile.

        Args:
            profile: The profile to update and persist.
            understanding: The update read from the user's text.
        """
        profile.absorb(understanding)
        save_profile(self._store, profile)

    def _context(self, profile: Profile, state: dict[str, Any]) -> str:
        """Join the profile and last job descriptions into one context text.

        Args:
            profile: The saved hiring profile of the user.
            state: The thread state that names the last job.

        Returns:
            The non-empty parts, one per line.
        """
        return "\n".join(
            part for part in (profile_context(profile), self._last_job_context(state)) if part
        )

    def _history(self, thread: Thread) -> list[Message]:
        """Return the recent messages of a thread before the current one.

        Args:
            thread: The thread whose history is read.

        Returns:
            The latest messages, oldest first, without the message just stored.
        """
        history = self._store.recent_messages(thread.id, self._engine.tuning.history_messages + 1)
        return history[:-1]

    def _propose(
        self, thread: Thread, state: dict[str, Any], plan: Plan, out: list[Message]
    ) -> None:
        """Present a plan to the user for confirmation.

        A plan that fails the compliance check is answered with a notice instead. Otherwise
        the plan is saved in the thread state, the thread is retitled and a proposal
        message is added.

        Args:
            thread: The thread that receives the proposal.
            state: The mutable thread state.
            plan: The plan to propose.
            out: The list that collects the messages of this turn.
        """
        try:
            self._check(plan)
        except ComplianceRefusal as refusal:
            out.append(self.say(thread.id, str(refusal), "notice"))
            return
        task, context = describe_plan(plan)
        state.update(phase="proposed", plan=plan.model_dump(mode="json"))
        self._save(thread, state)
        title = plan_title(plan, self._engine.tuning.title_chars)
        self._store.rename_thread(thread.id, title)
        text = self._engine.line("proposal", task=task, context=context)
        out.append(
            self.say(thread.id, text, "proposal", {"task": task, "context": context.strip()})
        )

    def _check(self, plan: Plan) -> None:
        """Run the compliance check on the request that a plan would start.

        Args:
            plan: The plan to check.

        Raises:
            ComplianceRefusal: when the request is not allowed.
        """
        if plan.kind == "skill":
            check_skill_request(
                SkillSearchRequest(
                    goal=plan.goal,
                    skill=plan.skill or "",
                    location=plan.location or "",
                    limit=plan.limit,
                    purpose_confirmed=True,
                    company=plan.company,
                    requirements=plan.requirements,
                    options=plan.options,
                )
            )
        elif plan.identity is not None:
            check_research_request(
                ResearchRequest(
                    goal=plan.goal,
                    identity=plan.identity,
                    focus=plan.focus,
                    purpose_confirmed=True,
                    company=plan.company,
                    requirements=plan.requirements,
                    options=plan.options,
                )
            )

    async def _reply_to_proposal(
        self,
        thread: Thread,
        state: dict[str, Any],
        text: str,
        defaults: Defaults,
        profile: Profile,
        out: list[Message],
        can_research: bool = True,
    ) -> None:
        """Handle the user's answer to a proposed plan.

        A confirmation starts the job, a decline returns the thread to idle, and any other
        text is read as a change to the plan.

        Args:
            thread: The thread that holds the proposal.
            state: The mutable thread state.
            text: The text the user typed.
            defaults: The defaults used to rebuild the plan.
            profile: The saved hiring profile of the user.
            out: The list that collects the messages of this turn.
            can_research: Whether the user may start a research job.
        """
        tuning = self._engine.tuning
        plan = Plan.model_validate(state["plan"])
        if is_confirm(text, tuning):
            if not can_research:
                out.append(self.say(thread.id, self._engine.line("not_allowed"), "notice"))
                return
            self._start(thread, state, plan, out)
        elif is_decline(text, tuning):
            self._cvs.pop(thread.id, None)
            state.update(phase="idle", plan=None)
            self._save(thread, state)
            out.append(self.say(thread.id, self._engine.line("declined"), "text"))
        else:
            task, context = describe_plan(plan)
            understanding = await self._engine.understand(
                text, self._history(thread), f"{task}{context}"
            )
            await self._act(thread, state, understanding, plan, defaults, profile, text, out)

    def _start(self, thread: Thread, state: dict[str, Any], plan: Plan, out: list[Message]) -> None:
        """Start the job for a confirmed plan and move the thread to running.

        A notice is added instead when the job system is not ready, the search rate limit is
        exceeded, there are too many jobs, or the request is refused.

        Args:
            thread: The thread that holds the plan.
            state: The mutable thread state.
            plan: The confirmed plan.
            out: The list that collects the messages of this turn.
        """
        if not self._jobs.ready:
            out.append(self.say(thread.id, self._engine.line("not_ready"), "notice"))
            return
        if not self._admit():
            out.append(
                self.say(thread.id, "Too many searches were started; wait a few minutes.", "notice")
            )
            return
        try:
            job = self._launch(thread, plan)
        except TooManyJobs as error:
            out.append(self.say(thread.id, str(error), "notice"))
            return
        except (ComplianceRefusal, ValidationError) as error:
            out.append(self.say(thread.id, str(error), "notice"))
            return
        self._cvs.pop(thread.id, None)
        brief = {
            "company": plan.company.model_dump(mode="json") if plan.company else None,
            "requirements": [item.model_dump(mode="json") for item in plan.requirements],
        }
        state.update(phase="running", plan=None, job_id=job.id, brief=brief)
        self._save(thread, state)
        out.append(
            self.say(
                thread.id,
                self._engine.line("started"),
                "job",
                {"job_id": job.id, "after": 0, "title": job.title},
            )
        )

    def _launch(self, thread: Thread, plan: Plan) -> Job:
        """Start a skill search or research job for a plan.

        Args:
            thread: The thread the job belongs to.
            plan: The plan to run.

        Returns:
            The started job.

        Raises:
            ComplianceRefusal: when the request is refused, or when a CV research plan has
                lost its CV after a restart.
            TooManyJobs: when the job limit is reached.
        """
        if plan.kind == "skill":
            request = SkillSearchRequest(
                goal=plan.goal,
                skill=plan.skill or "",
                location=plan.location or "",
                limit=plan.limit,
                purpose_confirmed=True,
                company=plan.company,
                requirements=plan.requirements,
                options=plan.options,
            )
            check_skill_request(request)
            return self._jobs.start_skill_search(WEB_OWNER, request, thread_id=thread.id)
        # CVs are kept in memory only, so a restart loses them and the user must upload again.
        source = plan.identity or self._cvs.get(thread.id)
        if source is None:
            raise ComplianceRefusal("Send the CV again; it is not kept after a restart.")
        return self._jobs.start_research(
            WEB_OWNER,
            plan.goal,
            plan.focus,
            source,
            plan.options,
            plan.company,
            plan.requirements,
            thread_id=thread.id,
        )

    async def _while_running(
        self,
        thread: Thread,
        state: dict[str, Any],
        text: str,
        profile: Profile,
        out: list[Message],
    ) -> None:
        """Handle a message sent while the thread has a job running.

        A confirm or decline gets a busy notice, chat is answered, a profile update is
        saved, and any other request gets the busy notice.

        Args:
            thread: The thread that has a running job.
            state: The mutable thread state.
            text: The text the user typed.
            profile: The saved hiring profile of the user.
            out: The list that collects the messages of this turn.
        """
        job = self._jobs.get(state.get("job_id", ""), WEB_OWNER)
        tuning = self._engine.tuning
        busy = self._engine.line(
            "busy", title=job.title if job else "a search", stage=job.stage if job else "Working"
        )
        if is_confirm(text, tuning) or is_decline(text, tuning):
            out.append(self.say(thread.id, busy, "text"))
            return
        understanding = await self._engine.understand(text, self._history(thread))
        if understanding.intent == "chat":
            reply = await self._engine.converse(
                "assistant", text, self._history(thread), self._context(profile, state)
            )
            out.append(self.say(thread.id, reply, "text"))
        elif understanding.intent == "update":
            self._remember(profile, understanding)
            out.append(self.say(thread.id, self._engine.line("remembered"), "text"))
        else:
            out.append(self.say(thread.id, busy, "text"))

    async def _answer(
        self, thread: Thread, state: dict[str, Any], text: str, out: list[Message]
    ) -> None:
        """Handle the user's reply to a pending clarification question.

        The reply is read as an option number, a none answer, or free text matched by the
        engine. An unclear reply repeats the question. Once every question is answered the
        job is resumed.

        Args:
            thread: The thread that is asking.
            state: The mutable thread state with the pending networks and answers.
            text: The text the user typed.
            out: The list that collects the messages of this turn.
        """
        job = self._jobs.get(state.get("job_id", ""), WEB_OWNER)
        clarification = job.clarification if job else None
        pending = list(state.get("pending", []))
        if job is None or clarification is None or not pending:
            state.update(phase="idle", pending=[], answers=[])
            self._save(thread, state)
            out.append(self.say(thread.id, self._engine.line("not_asking"), "text"))
            return
        network = Network(pending[0])
        question = next(item for item in clarification.questions if item.network is network)
        options = question.options[: self._engine.tuning.max_options]
        chosen = self._read_choice(text, network, options)
        if chosen is None:
            understanding = await self._engine.answer(text, options)
            chosen = answer_for(understanding, network, options)
        if chosen is None:
            out.append(
                self._question_message(thread, job, network, options, self._engine.line("ask"))
            )
            return
        answers = [*state.get("answers", []), chosen.model_dump(mode="json")]
        pending.pop(0)
        state.update(pending=pending, answers=answers)
        if pending:
            self._save(thread, state)
            following = Network(pending[0])
            next_options = next(
                item for item in clarification.questions if item.network is following
            ).options[: self._engine.tuning.max_options]
            out.append(self._question_message(thread, job, following, next_options, ""))
            return
        self._resume(thread, state, job, answers, out)

    def _read_choice(self, text, network, options) -> ClarificationAnswer | None:
        """Read a numbered or none reply to a clarification question.

        Args:
            text: The text the user typed.
            network: The network the question is about.
            options: The candidate profiles offered to the user.

        Returns:
            An answer with the chosen URL, an answer without a URL for none, or None when
            the text is neither.
        """
        tuning = self._engine.tuning
        number = pick_option(text, len(options))
        if number is not None:
            return ClarificationAnswer(network=network, chosen_url=options[number - 1].url)
        if is_none(text, tuning):
            return ClarificationAnswer(network=network)
        return None

    def _resume(
        self,
        thread: Thread,
        state: dict[str, Any],
        job: Job,
        answers: list[dict],
        out: list[Message],
    ) -> None:
        """Give the collected answers to the paused job and mark the thread running.

        A notice is added instead when the job cannot be resumed.

        Args:
            thread: The thread that was asking.
            state: The mutable thread state.
            job: The job waiting for input.
            answers: The answers as stored in the thread state.
            out: The list that collects the messages of this turn.
        """
        try:
            self._jobs.answer(job, [ClarificationAnswer.model_validate(item) for item in answers])
        except (TooManyJobs, AgentError) as error:
            out.append(self.say(thread.id, str(error), "notice"))
            return
        state.update(phase="running", pending=[], answers=[])
        self._save(thread, state)
        out.append(
            self.say(
                thread.id,
                self._engine.line("started"),
                "job",
                {"job_id": job.id, "after": len(job.events), "title": job.title},
            )
        )

    def _question_message(self, thread, job, network, options, intro) -> Message:
        """Store a message that asks the user to pick one profile for a network.

        Args:
            thread: The thread that receives the question.
            job: The job that is waiting for the answer.
            network: The network the question is about.
            options: The numbered candidate profiles to offer.
            intro: The sentence placed before the network name, or empty for a follow-up.

        Returns:
            The stored question message.
        """
        label = network.value.capitalize()
        text = f"{intro} {label}:".strip() if intro else f"Next, {label}:"
        data = {
            "job_id": job.id,
            "network": network.value,
            "options": [
                {
                    "n": number,
                    "title": option.title,
                    "snippet": option.snippet,
                    "url": option.url,
                    "evidence": option.evidence,
                }
                for number, option in enumerate(options, 1)
            ],
        }
        return self.say(thread.id, text, "question", data)

    def settled(self, job: Job) -> None:
        """Report the outcome of a finished job in the thread that started it.

        A completed job adds its result and returns the thread to idle. A job that needs
        input starts the clarification questions. Any other end adds a failure notice. Jobs
        without a thread, or whose thread is gone, are ignored.

        Args:
            job: The job that stopped running.
        """
        if not job.thread_id:
            return
        thread = self._store.thread(job.thread_id)
        if thread is None:
            return
        state = dict(thread.state)
        if job.state is JobState.DONE and job.report is not None:
            text = describe_report(job.report, self._spoken_flags)
            self.say(thread.id, text, "result", {"job_id": job.id, "rating": job.rating})
            state.update(phase="idle", job_id=job.id, last_job=job.id, pending=[], answers=[])
        elif job.state is JobState.NEEDS_INPUT and job.clarification is not None:
            self._ask(thread, state, job, job.clarification)
            return
        else:
            error = job.error or "The search did not finish."
            self.say(thread.id, self._engine.line("failed", error=error), "notice")
            state.update(phase="idle", job_id=job.id, pending=[], answers=[])
        self._save(thread, state)

    def _ask(
        self, thread: Thread, state: dict[str, Any], job: Job, clarification: Clarification
    ) -> None:
        """Move a thread to the asking phase and post the first clarification question.

        Args:
            thread: The thread of the paused job.
            state: The mutable thread state.
            job: The job that needs input.
            clarification: The questions the job wants answered.
        """
        pending = [question.network.value for question in clarification.questions]
        state.update(phase="asking", job_id=job.id, pending=pending, answers=[])
        self._save(thread, state)
        first = clarification.questions[0]
        options = first.options[: self._engine.tuning.max_options]
        self._question_message(thread, job, first.network, options, self._engine.line("ask"))

    def recover(self) -> None:
        """Repair research threads that were left running or asking by a restart.

        A thread whose job is gone is reset to idle with an interrupted notice. A running
        thread whose job already stopped is settled.
        """
        for thread in self._store.threads(RESEARCH_KIND):
            phase = thread.state.get("phase")
            job_id = thread.state.get("job_id")
            if phase not in ("running", "asking") or not job_id:
                continue
            job = self._jobs.get(job_id, WEB_OWNER)
            if job is None:
                self._save(thread, {**thread.state, "phase": "idle", "pending": []})
                self.say(thread.id, self._engine.line("interrupted"), "notice")
            elif job.state is not JobState.RUNNING and phase == "running":
                self.settled(job)

    async def tie(self, text: str, profile: Profile, job: Job | None, page: str) -> list[Message]:
        """Answer a message sent to the Tie assistant from any page.

        The user message is stored first. Harmful requests get a notice, and a model error
        is reported as a notice. Otherwise the reply uses the page, profile and job context.

        Args:
            text: The text the user typed.
            profile: The saved hiring profile of the user.
            job: The job being viewed, if any.
            page: The name of the page the user is on, or empty.

        Returns:
            The stored user message followed by the assistant reply or notice.
        """
        thread = self.tie_thread()
        text = text.strip()
        sent = self._store.add_message(
            thread.id, "user", "text", text[: self._engine.tuning.max_user_chars]
        )
        out = [sent]
        try:
            refuse_harmful_requests([text])
            context = "\n".join(
                part
                for part in (
                    f"The user is on the {page} page." if page else "",
                    profile_context(profile),
                    job_context(job, self._spoken_flags) if job else "",
                )
                if part
            )
            history = self._store.recent_messages(
                thread.id, self._engine.tuning.history_messages + 1
            )
            reply = await self._engine.converse("tie", text, history[:-1], context)
            out.append(self._store.add_message(thread.id, "assistant", "text", reply))
        except ComplianceRefusal as refusal:
            out.append(self._store.add_message(thread.id, "assistant", "notice", str(refusal)))
        except AgentError as error:
            line = self._engine.line("model_down", error=str(error))
            out.append(self._store.add_message(thread.id, "assistant", "notice", line))
        return out
