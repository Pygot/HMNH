# tests/test_chat_engine.py
from agent.chat import (
    Answer,
    answer_for,
    build_plan,
    ChatEngine,
    company_from,
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
    requirement_from,
    RequirementDraft,
    transcript,
    Understanding,
)
from agent.models import (
    ClarificationOption,
    CompanyInput,
    Goal,
    Identity,
    Network,
    Priority,
    Requirement,
    RequirementKind,
    ResearchOptions,
)
from agent.config import ChatTuning
from agent.store import Message
from tests.fakes import RuleLLM

import pytest

TUNING = ChatTuning()
DEFAULTS = Defaults(
    goal=Goal.HIRING, options=ResearchOptions(), limit=5, company=None, requirements=[]
)
ENGLISH = Requirement(kind=RequirementKind.LANGUAGE, label="English", level="C1")
OPTIONS = [
    ClarificationOption(
        url="https://www.linkedin.com/in/a", title="A", snippet="", score=0.6, evidence=""
    ),
    ClarificationOption(
        url="https://www.linkedin.com/in/b", title="B", snippet="", score=0.5, evidence=""
    ),
]


def message(role, text):
    return Message(id=1, thread_id="t", role=role, kind="text", text=text, created=1.0)


@pytest.mark.parametrize(
    "text",
    ["yes", "Yes!", "y", "ok", "okay, start", "yes please", "go ahead", "Let's go", "sure thanks"],
)
def test_short_agreements_are_confirmations(text):
    assert is_confirm(text, TUNING) is True


@pytest.mark.parametrize(
    "text", ["", "no", "yes but use Brno", "maybe", "start with Eva instead", "yes no", "Jan Novak"]
)
def test_everything_else_is_not_a_confirmation(text):
    assert is_confirm(text, TUNING) is False


@pytest.mark.parametrize("text", ["no", "No thanks", "cancel", "never mind", "stop", "nope"])
def test_short_refusals_are_declines(text):
    assert is_decline(text, TUNING) is True


@pytest.mark.parametrize("text", ["", "yes", "no, he lives in Brno", "cancel the second one"])
def test_other_replies_are_not_declines(text):
    assert is_decline(text, TUNING) is False


@pytest.mark.parametrize("text", ["none", "None of them", "neither", "skip", "no one", "skip it"])
def test_none_replies_are_recognised(text):
    assert is_none(text, TUNING) is True


def test_none_needs_a_short_reply():
    assert is_none("the second one", TUNING) is False
    assert (
        is_none("I looked at all of them and none of them has the right employer", TUNING) is False
    )


@pytest.mark.parametrize(
    ("text", "count", "expected"),
    [
        ("2", 3, 2),
        ("the second one", 3, 2),
        ("option 3", 3, 3),
        ("first", 3, 1),
        ("last", 4, 4),
        ("7", 3, None),
        ("0", 3, None),
        ("I think the first is right because he works at Acme", 3, None),
        ("Brno", 3, None),
        ("", 3, None),
    ],
)
def test_options_are_picked_by_number_or_ordinal(text, count, expected):
    assert pick_option(text, count) == expected


def test_requirements_are_cleaned_and_validated():
    item = requirement_from(
        RequirementDraft(
            kind="Language", label="  English ", level="C1", years=None, priority="NICE"
        )
    )
    assert item.kind is RequirementKind.LANGUAGE and item.label == "English"
    assert item.level == "C1" and item.priority is Priority.NICE
    assert (
        requirement_from(RequirementDraft(kind="hobby", label="Chess")).kind
        is RequirementKind.CUSTOM
    )
    assert (
        requirement_from(RequirementDraft(label="Rust", priority="vital")).priority is Priority.MUST
    )
    assert requirement_from(RequirementDraft(label="Rust", years=99)).minimum_years is None
    assert requirement_from(RequirementDraft(label="Rust", years=5)).minimum_years == 5
    assert requirement_from(RequirementDraft(label="   ")) is None


def test_new_requirements_replace_those_with_the_same_label():
    old = [ENGLISH, Requirement(kind=RequirementKind.SKILL, label="Python")]
    new = [Requirement(kind=RequirementKind.LANGUAGE, label="english", level="B2")]
    merged = merge_requirements(old, new)
    assert [(item.label, item.level) for item in merged] == [("english", "B2"), ("Python", None)]


def test_company_details_merge_with_what_is_already_known():
    current = CompanyInput(name="Acme", website="https://acme.example")
    update = Understanding(intent="update", about="We build payment software")
    merged = company_from(update, current)
    assert merged.name == "Acme" and merged.website == "https://acme.example"
    assert merged.description == "We build payment software"
    assert company_from(Understanding(), None) is None
    assert company_from(Understanding(company="Globex"), current).name == "Globex"
    assert company_from(Understanding(company="Acme", website="ftp://bad"), current) == current


def test_a_person_plan_is_built_from_what_the_user_said():
    plan = build_plan(
        Understanding(
            intent="research",
            name="Jan Novak",
            city="Brno",
            employers=["Acme", "acme"],
            skills=["python"],
            focus="Senior engineer",
        ),
        None,
        DEFAULTS,
    )
    assert plan.kind == "person" and plan.goal is Goal.HIRING
    assert plan.identity.name == "Jan Novak" and plan.identity.location == "Brno"
    assert plan.identity.employers == ["Acme"] and plan.focus == "Senior engineer"


def test_a_name_is_required_for_a_person_plan_unless_a_cv_was_given():
    assert build_plan(Understanding(intent="research", city="Brno"), None, DEFAULTS) is None
    plan = build_plan(
        Understanding(intent="research", focus="Python"), plan_for_cv(DEFAULTS), DEFAULTS
    )
    assert plan.cv is True and plan.identity is None and plan.focus == "Python"


def test_later_messages_add_to_the_pending_plan():
    first = build_plan(Understanding(intent="research", name="Jan Novak"), None, DEFAULTS)
    second = build_plan(
        Understanding(intent="research", city="Brno", employers=["Acme"]), first, DEFAULTS
    )
    third = build_plan(Understanding(intent="research", employers=["Globex"]), second, DEFAULTS)
    assert third.identity.name == "Jan Novak" and third.identity.location == "Brno"
    assert third.identity.employers == ["Acme", "Globex"]


def test_a_new_name_replaces_the_old_one_but_keeps_the_context():
    first = build_plan(
        Understanding(
            intent="research",
            name="Jan Novak",
            company="Acme",
            requirements=[RequirementDraft(label="Python")],
        ),
        None,
        DEFAULTS,
    )
    second = build_plan(Understanding(intent="research", name="Eva Svobodova"), first, DEFAULTS)
    assert second.identity.name == "Eva Svobodova"
    assert second.company.name == "Acme" and [item.label for item in second.requirements] == [
        "Python"
    ]


def test_standing_company_and_requirements_are_used_without_retyping():
    defaults = Defaults(
        goal=Goal.SALES,
        options=ResearchOptions(),
        limit=3,
        company=CompanyInput(name="Acme"),
        requirements=[ENGLISH],
    )
    plan = build_plan(Understanding(intent="research", name="Jan Novak"), None, defaults)
    assert (
        plan.goal is Goal.SALES and plan.company.name == "Acme" and plan.requirements == [ENGLISH]
    )
    extra = build_plan(
        Understanding(
            intent="research",
            name="Jan",
            requirements=[RequirementDraft(label="Rust", priority="nice")],
        ),
        None,
        defaults,
    )
    assert [item.label for item in extra.requirements] == ["English", "Rust"]
    assert extra.requirements[1].priority is Priority.NICE


def test_the_goal_can_be_changed_in_a_message():
    plan = build_plan(
        Understanding(intent="research", name="Jan", goal="due diligence"), None, DEFAULTS
    )
    assert plan.goal is Goal.DUE_DILIGENCE
    assert (
        build_plan(
            Understanding(intent="research", name="Jan", goal="nonsense"), None, DEFAULTS
        ).goal
        is Goal.HIRING
    )


def test_a_skill_plan_needs_a_skill_and_a_place():
    assert build_plan(Understanding(intent="skill", skill="Rust"), None, DEFAULTS) is None
    assert build_plan(Understanding(intent="skill", location="Brno"), None, DEFAULTS) is None
    plan = build_plan(
        Understanding(intent="skill", skill="Rust", location="Brno", limit=50), None, DEFAULTS
    )
    assert plan.kind == "skill" and plan.limit == 10 and plan.skill == "Rust"
    assert (
        build_plan(
            Understanding(intent="skill", skill="Rust", city="Brno"), None, DEFAULTS
        ).location
        == "Brno"
    )
    assert (
        build_plan(
            Understanding(intent="skill", skill="Rust", location="Brno", limit=0), None, DEFAULTS
        ).limit
        == 5
    )


def test_a_skill_plan_can_be_adjusted_by_a_follow_up():
    plan = build_plan(Understanding(intent="skill", skill="Rust", location="Brno"), None, DEFAULTS)
    adjusted = build_plan(Understanding(intent="chat", location="Prague", limit=3), plan, DEFAULTS)
    assert adjusted.kind == "skill" and adjusted.location == "Prague" and adjusted.limit == 3


def test_plans_are_described_in_plain_words():
    plan = build_plan(
        Understanding(
            intent="research",
            name="Jan Novak",
            city="Brno",
            employers=["Acme"],
            company="Acme",
            focus="Python engineer",
            requirements=[
                RequirementDraft(kind="language", label="English", level="C1"),
                RequirementDraft(label="AWS", priority="nice"),
            ],
        ),
        None,
        DEFAULTS,
    )
    task, context = describe_plan(plan)
    assert task == "I will look into Jan Novak (Brno; Acme) for hiring."
    assert "Hiring company: Acme." in context and "Rating against: Python engineer." in context
    assert "English (C1); AWS, nice to have" in context
    bare, nothing = describe_plan(build_plan(Understanding(name="Eva"), None, DEFAULTS))
    assert bare == "I will look into Eva for hiring." and nothing == ""


def test_skill_and_cv_plans_are_described_too():
    skill = build_plan(Understanding(intent="skill", skill="Rust", location="Brno"), None, DEFAULTS)
    assert (
        describe_plan(skill)[0]
        == "I will find up to 5 people with Rust skills around Brno for hiring."
    )
    assert "from your CV" in describe_plan(plan_for_cv(DEFAULTS))[0]


def test_plan_titles_are_short():
    skill = build_plan(Understanding(intent="skill", skill="Rust", location="Brno"), None, DEFAULTS)
    person = build_plan(Understanding(name="Jan Novak"), None, DEFAULTS)
    assert plan_title(skill, 48) == "Rust in Brno" and plan_title(person, 5) == "Jan N"
    assert plan_title(plan_for_cv(DEFAULTS), 48) == "CV"


def test_answers_are_turned_into_clarification_answers():
    assert answer_for(Answer(skip=True), Network.LINKEDIN, OPTIONS).chosen_url is None
    chosen = answer_for(Answer(choice=2), Network.LINKEDIN, OPTIONS)
    assert chosen.chosen_url == "https://www.linkedin.com/in/b"
    narrowed = answer_for(Answer(city="Brno", employer="Acme"), Network.LINKEDIN, OPTIONS)
    assert narrowed.location == "Brno" and narrowed.employer == "Acme"
    assert answer_for(Answer(choice=5), Network.LINKEDIN, OPTIONS) is None
    assert answer_for(Answer(), Network.LINKEDIN, OPTIONS) is None
    assert answer_for(Answer(city="Brno 602 00"), Network.LINKEDIN, OPTIONS) is None


def test_the_transcript_keeps_only_the_latest_messages():
    messages = [
        message("user", "one"),
        message("assistant", "two  words"),
        message("user", "three"),
    ]
    assert transcript(messages, 2) == "Assistant: two words\nUser: three"
    assert transcript([], 5) == ""


async def test_understanding_goes_through_the_model_with_the_pending_plan():
    seen = []

    def handle(user):
        seen.append(user)
        return {"intent": "research", "name": "Jan Novak", "unknown_field": 1}

    engine = ChatEngine(RuleLLM({"fill in a JSON form": handle}), TUNING)
    result = await engine.understand("look into Jan", [message("user", "hello")], "Jan (Brno)")
    assert result.intent == "research" and result.name == "Jan Novak"
    assert "Pending plan: Jan (Brno)" in seen[0] and "User: hello" in seen[0]
    assert seen[0].endswith("Message to read:\nlook into Jan")


async def test_answers_are_read_with_the_numbered_options():
    seen = []

    def handle(user):
        seen.append(user)
        return {"choice": 2}

    engine = ChatEngine(RuleLLM({"asked which one is right": handle}), TUNING)
    result = await engine.answer("the one at B", OPTIONS)
    assert result.choice == 2 and "1. A" in seen[0] and "2. B" in seen[0]


async def test_a_conversation_reply_is_plain_text_clipped_and_never_empty():
    seen = []

    def handle(user):
        seen.append(user)
        return "  Ask about their delivery record.  "

    llm = RuleLLM({"assistant of a tool": handle, "friendly necktie": lambda user: "x" * 5000})
    engine = ChatEngine(llm, ChatTuning(reply_chars=200))
    reply = await engine.converse("assistant", "What next?", [message("user", "hi")], "Acme")
    assert reply == "Ask about their delivery record."
    assert "Context:\nAcme" in seen[0] and "User: What next?" in seen[0]
    assert len(await engine.converse("tie", "hello", [], "")) == 200
    empty = ChatEngine(RuleLLM({"assistant of a tool": lambda user: "   "}), TUNING)
    assert await empty.converse("assistant", "hi", [], "") == TUNING.lines["misunderstood"]


def test_canned_lines_are_filled_in():
    engine = ChatEngine(RuleLLM({}), TUNING)
    assert "Jan" in engine.line("busy", title="Jan", stage="Merging findings")
    assert engine.line("welcome").startswith("Who would you like")


def test_the_default_chat_lines_are_complete_and_safe():
    assert ChatTuning().lines["tie_hello"]
    with pytest.raises(ValueError, match="missing"):
        ChatTuning(lines={"welcome": "Hi"})
    with pytest.raises(ValueError, match="placeholder"):
        ChatTuning(lines={**ChatTuning().lines, "busy": "Working on {nobody}"})


def test_plans_round_trip_through_json_for_the_thread_state():
    plan = build_plan(
        Understanding(
            intent="research",
            name="Jan Novak",
            company="Acme",
            requirements=[RequirementDraft(label="English", level="C1")],
        ),
        None,
        DEFAULTS,
    )
    assert Plan.model_validate_json(plan.model_dump_json()) == plan
    assert Identity(name="Jan Novak") == Plan.model_validate_json(
        plan.model_dump_json()
    ).identity.model_copy(
        update={"location": None, "aliases": [], "employers": [], "roles": [], "skills": []}
    )
