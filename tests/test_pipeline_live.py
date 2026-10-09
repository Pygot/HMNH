# tests/test_pipeline_live.py
from agent.models import (
    CompanyInput,
    FactKind,
    Goal,
    PersonReport,
    Requirement,
    RequirementKind,
    RequirementStatus,
    ResearchRequest,
    SkillSearchRequest,
)
from tests.test_pipeline import (
    build,
    GITHUB,
    IDENTITY,
    LI_JAN,
    linkedin_collector,
    linkedin_doc,
    make_llm,
    raw,
    SITE,
)
from agent.events import (
    Kind,
    Reporter,
    Task,
)
from tests.fakes import (
    document,
    FakeSearch,
    hit,
)

import re

SITE_HOME = "https://acme.example/"
SITE_ABOUT = "https://acme.example/about"
COMPANY = CompanyInput(name="Acme", website="https://acme.example")
ENGLISH = Requirement(kind=RequirementKind.LANGUAGE, label="English", level="C1")
PYTHON = Requirement(kind=RequirementKind.SKILL, label="Python", minimum_years=5)
CLAIMS = {
    LI_JAN: [raw("Senior engineer at Acme"), raw("Knows Python", "skill", 2024)],
    SITE: [raw("Engineer at Acme")],
    GITHUB: [raw("Maintains an open source parser", "project", 2023)],
}


class Recorder:
    def __init__(self):
        self.items = []

    def __call__(self, kind, task, text, data):
        self.items.append((kind, task, text, data))

    @property
    def reporter(self):
        return Reporter(self)

    def kinds(self):
        return [item[0] for item in self.items]

    def of(self, kind):
        return [item for item in self.items if item[0] is kind]

    def tasks(self):
        return {item[1] for item in self.items}


def company_answer(user):
    return {
        "facts": [
            {"kind": "category", "statement": "Payments software", "source_url": SITE_HOME},
            {
                "kind": "product",
                "statement": "Acme builds card payment terminals.",
                "source_url": SITE_ABOUT,
            },
        ]
    }


def requirement_answer(user):
    identifiers = re.findall(r"^(R\d+) \[", user, re.MULTILINE)
    results = [
        {
            "id": identifier,
            "status": "met" if number == 0 else "unknown",
            "justification": "Evidence found." if number == 0 else "Nothing found.",
            "finding_ids": ["F1"] if number == 0 else [],
        }
        for number, identifier in enumerate(identifiers)
    ]
    return {"results": results}


def live_llm(claims, company=company_answer, requirements=requirement_answer):
    llm = make_llm(claims)
    llm.rules["public facts about one company"] = company
    llm.rules["judge whether one person satisfies"] = requirements
    return llm


def prompts(llm, fragment):
    return [user for system, user in llm.calls if fragment in system]


def acme_pages():
    return {
        SITE_HOME: document(SITE_HOME, "Acme builds payment terminals.", title="Acme"),
        SITE_ABOUT: document(SITE_ABOUT, "About Acme and its terminals.", title="About"),
    }


def hiring_request(**extra):
    return ResearchRequest(goal=Goal.HIRING, identity=IDENTITY, purpose_confirmed=True, **extra)


def full_scenario(llm):
    search = FakeSearch(
        {
            "site:linkedin.com/in": [hit(LI_JAN, "Jan Novak - Senior Python Engineer", "Brno")],
            "portfolio": [hit(SITE, "Jan", "Portfolio")],
            "site:github.com": [hit(GITHUB, "jannovak", "code")],
        }
    )
    pages = {
        SITE: document(SITE, "Jan Novak, Python engineer at Acme in Brno.", title="Jan Novak"),
        GITHUB: document(GITHUB, "jannovak Jan Novak Acme Brno python", title="jannovak"),
        **acme_pages(),
    }
    return build(search, linkedin_collector({LI_JAN: linkedin_doc()}), pages, llm)


async def test_a_run_announces_every_step_as_an_event():
    recorder = Recorder()
    pipeline, _ = full_scenario(live_llm(CLAIMS))
    report = await pipeline.research(hiring_request(), recorder.reporter)
    assert isinstance(report, PersonReport)
    assert {
        Kind.STAGE,
        Kind.STEP,
        Kind.CANDIDATE,
        Kind.SOURCE,
        Kind.CLAIM,
        Kind.FINDING,
        Kind.RATING,
        Kind.SCEPTICISM,
        Kind.NOTE,
    } <= set(recorder.kinds())
    assert {
        Task.SEARCH,
        Task.LINKEDIN,
        Task.WEB,
        Task.SUMMARISE,
        Task.MERGE,
        Task.RATE,
        Task.SCEPTICISM,
    } <= recorder.tasks()
    assert Task.COMPANY not in recorder.tasks() and Task.REQUIREMENTS not in recorder.tasks()


async def test_source_and_claim_events_carry_their_urls():
    recorder = Recorder()
    pipeline, _ = full_scenario(live_llm(CLAIMS))
    await pipeline.research(hiring_request(), recorder.reporter)
    sources = {item[3]["url"] for item in recorder.of(Kind.SOURCE)}
    assert sources == {LI_JAN, SITE, GITHUB}
    assert all(item[3]["network"] for item in recorder.of(Kind.SOURCE))
    assert {item[3]["url"] for item in recorder.of(Kind.CLAIM)} <= sources
    candidate = recorder.of(Kind.CANDIDATE)[0]
    assert candidate[3]["network"] == "linkedin" and 0 < candidate[3]["score"] <= 1
    finding = recorder.of(Kind.FINDING)[0]
    assert finding[3]["id"] == "F1" and finding[3]["urls"]


async def test_the_rating_event_lists_the_criteria_and_the_scepticism_verdict():
    recorder = Recorder()
    pipeline, _ = full_scenario(live_llm(CLAIMS))
    report = await pipeline.research(hiring_request(), recorder.reporter)
    (rating,) = recorder.of(Kind.RATING)
    assert rating[3]["overall"] == report.rating.overall
    assert {item["name"] for item in rating[3]["criteria"]} >= {"skill_fit", "evidence_of_work"}
    (scepticism,) = recorder.of(Kind.SCEPTICISM)
    assert scepticism[3]["level"] == report.rating.scepticism.level.value


async def test_notes_are_announced_as_they_are_written():
    recorder = Recorder()
    pipeline, _ = build()
    report = await pipeline.research(hiring_request(), recorder.reporter)
    announced = [item[2] for item in recorder.of(Kind.NOTE)]
    assert announced
    assert all(note in report.limitations for note in announced)


async def test_the_company_is_researched_first_and_reaches_the_report():
    recorder = Recorder()
    llm = live_llm(CLAIMS)
    pipeline, fetcher = full_scenario(llm)
    report = await pipeline.research(hiring_request(company=COMPANY), recorder.reporter)
    assert report.company is not None and report.company.category == "Payments software"
    assert report.company.products == ["Acme builds card payment terminals."]
    stages = [item[2] for item in recorder.of(Kind.STAGE)]
    assert stages[0] == "Learning about Acme"
    assert recorder.of(Kind.STAGE)[0][1] is Task.COMPANY
    facts = recorder.of(Kind.COMPANY)
    assert {item[3]["fact_kind"] for item in facts} == {"category", "product"}
    assert all(item[3]["name"] == "Acme" for item in facts)
    assert SITE_HOME in fetcher.calls


async def test_the_company_details_reach_the_rating_prompt():
    llm = live_llm(CLAIMS)
    pipeline, _ = full_scenario(llm)
    await pipeline.research(hiring_request(company=COMPANY))
    (prompt,) = prompts(llm, "rate one person")
    assert "Hiring company (untrusted text): Acme (Payments software)" in prompt
    assert "card payment terminals" in prompt


async def test_without_a_company_the_prompts_have_no_company_line():
    llm = live_llm(CLAIMS)
    pipeline, _ = full_scenario(llm)
    report = await pipeline.research(hiring_request())
    assert report.company is None
    assert "Hiring company" not in prompts(llm, "rate one person")[0]
    assert prompts(llm, "public facts about one company") == []


async def test_a_company_with_unreadable_pages_still_uses_the_provided_description():
    llm = live_llm({})
    pipeline, _ = build(llm=llm)
    company = CompanyInput(name="Acme", description="We build payment software for shops")
    recorder = Recorder()
    report = await pipeline.research(hiring_request(company=company), recorder.reporter)
    assert [fact.kind for fact in report.company.facts] == [FactKind.PROVIDED]
    assert "No public page about Acme could be read." in report.limitations
    assert any("No public facts about Acme" in item for item in report.limitations)
    assert prompts(llm, "public facts about one company") == []
    assert recorder.of(Kind.COMPANY)[0][3]["fact_kind"] == "provided"


async def test_a_model_failure_on_the_company_pages_does_not_stop_the_run():
    llm = live_llm(CLAIMS, company=lambda user: {"facts": "not a list"})
    pipeline, _ = full_scenario(llm)
    report = await pipeline.research(hiring_request(company=COMPANY))
    assert isinstance(report, PersonReport)
    assert report.company is not None and report.company.facts == []
    assert any("could not be summarised" in item for item in report.limitations)
    assert any("No public facts about Acme" in item for item in report.limitations)


async def test_requirements_are_assessed_and_announced():
    recorder = Recorder()
    llm = live_llm(CLAIMS)
    pipeline, _ = full_scenario(llm)
    request = hiring_request(requirements=[ENGLISH, PYTHON])
    report = await pipeline.research(request, recorder.reporter)
    assessment = report.requirements
    assert [item.status for item in assessment.results] == [
        RequirementStatus.MET,
        RequirementStatus.UNKNOWN,
    ]
    assert assessment.must_missing == ["R2"]
    events = recorder.of(Kind.REQUIREMENT)
    assert [item[3]["status"] for item in events] == ["met", "unknown"]
    assert events[0][1] is Task.REQUIREMENTS and events[0][3]["detail"] == "Evidence found."
    assert events[0][2] == "English (C1): met"
    assert "Checking the requirements" in [item[2] for item in recorder.of(Kind.STAGE)]


async def test_requirement_sources_are_the_urls_of_the_cited_findings():
    pipeline, _ = full_scenario(live_llm(CLAIMS))
    report = await pipeline.research(hiring_request(requirements=[ENGLISH]))
    cited = next(item for item in report.findings if item.id == "F1")
    assert report.requirements.results[0].source_urls == cited.source_urls


async def test_the_company_text_reaches_the_requirement_prompt():
    llm = live_llm(CLAIMS)
    pipeline, _ = full_scenario(llm)
    await pipeline.research(hiring_request(company=COMPANY, requirements=[ENGLISH]))
    (prompt,) = prompts(llm, "judge whether one person satisfies")
    assert "Hiring company (untrusted text): Acme (Payments software)" in prompt
    assert "R1 [language, must] English (C1)" in prompt


async def test_without_requirements_the_model_is_not_asked_and_nothing_is_announced():
    recorder = Recorder()
    llm = live_llm(CLAIMS)
    pipeline, _ = full_scenario(llm)
    report = await pipeline.research(hiring_request(), recorder.reporter)
    assert report.requirements is None
    assert prompts(llm, "judge whether one person satisfies") == []
    assert recorder.of(Kind.REQUIREMENT) == []


async def test_an_unusable_requirement_answer_is_reported_without_failing_the_run():
    llm = live_llm(CLAIMS, requirements=lambda user: {"results": [{"id": "R9"}]})
    pipeline, _ = full_scenario(llm)
    report = await pipeline.research(hiring_request(requirements=[ENGLISH]))
    assert isinstance(report, PersonReport)
    assert report.requirements is None
    assert any("could not be assessed" in item for item in report.limitations)


async def test_requirements_without_findings_are_unknown():
    llm = live_llm({})
    pipeline, _ = build(llm=llm)
    report = await pipeline.research(hiring_request(requirements=[ENGLISH, PYTHON]))
    assert [item.status for item in report.requirements.results] == [RequirementStatus.UNKNOWN] * 2
    assert report.requirements.coverage == 0.0
    assert prompts(llm, "judge whether one person satisfies") == []


def skill_request(**extra):
    return SkillSearchRequest(
        goal=Goal.HIRING,
        skill="Rust",
        location="Brno",
        limit=3,
        purpose_confirmed=True,
        **extra,
    )


def skill_scenario(llm):
    gh = "https://github.com/ada"
    search = FakeSearch(
        {"site:linkedin.com/in": [hit(LI_JAN, "Jan", "")], "site:github.com": [hit(gh, "ada", "")]}
    )

    def summarize(user):
        url = re.search(r'<source url="([^"]+)">', user).group(1)
        names = {LI_JAN: "Jan Novak", gh: "Ada Lovelace"}
        return {"subject_name": names[url], "claims": [raw(f"Writes Rust for {names[url]}")]}

    llm.rules["extract professional facts"] = summarize
    pages = {gh: document(gh, "ada rust"), **acme_pages()}
    return build(search, linkedin_collector({LI_JAN: linkedin_doc()}), pages, llm)


async def test_skill_search_announces_candidates_and_each_rating():
    recorder = Recorder()
    llm = live_llm({})
    pipeline, _ = skill_scenario(llm)
    report = await pipeline.skill_search(skill_request(), recorder.reporter)
    assert len(report.candidates) == 2
    announced = recorder.of(Kind.CANDIDATE)
    assert {item[3]["network"] for item in announced} == {"linkedin", "web"}
    assert all(item[1] is Task.SEARCH for item in announced)
    subjects = {item[3]["subject"] for item in recorder.of(Kind.RATING)}
    assert subjects == {"Jan Novak", "Ada Lovelace"}
    assert len(recorder.of(Kind.SOURCE)) == 2


async def test_skill_search_checks_requirements_for_every_candidate():
    recorder = Recorder()
    llm = live_llm({})
    pipeline, _ = skill_scenario(llm)
    report = await pipeline.skill_search(
        skill_request(company=COMPANY, requirements=[ENGLISH]), recorder.reporter
    )
    assert report.company is not None and report.company.category == "Payments software"
    assert all(item.requirements is not None for item in report.candidates)
    assert len(prompts(llm, "judge whether one person satisfies")) == 2
    assert len(recorder.of(Kind.REQUIREMENT)) == 2
    assert all(
        "Hiring company (untrusted text): Acme (Payments software)" in prompt
        for prompt in prompts(llm, "rate one person")
    )
    assert recorder.of(Kind.STAGE)[0][2] == "Learning about Acme"


async def test_skill_search_networks_map_to_the_tasks_of_their_loaders():
    recorder = Recorder()
    pipeline, _ = skill_scenario(live_llm({}))
    await pipeline.skill_search(skill_request(), recorder.reporter)
    sources = recorder.of(Kind.SOURCE)
    tasks = {item[3]["network"]: item[1] for item in sources}
    assert tasks == {"linkedin": Task.LINKEDIN, "web": Task.WEB}
