# tests/test_pipeline.py
from agent.models import (
    Clarification,
    ClarificationAnswer,
    Goal,
    Identity,
    Network,
    PersonReport,
    ResearchRequest,
    SkillSearchReport,
    SkillSearchRequest,
    Tag,
)
from tests.fakes import (
    document,
    FakeCollector,
    FakeFetcher,
    FakeSearch,
    hit,
    NOW,
    RuleLLM,
)
from agent.pipeline import (
    apply_answers,
    known_profiles,
    Pipeline,
)
from agent.errors import ComplianceRefusal
from agent.discovery import Discovery
from tests.documents import make_pdf
from agent.config import Settings

import pytest
import re

LI_JAN = "https://www.linkedin.com/in/jan-novak"
LI_OTHER = "https://www.linkedin.com/in/jan-novak-2"
SITE = "https://jan.dev/about"
GITHUB = "https://github.com/jannovak"
JAN_TEXT = "Jan Novak. Senior Python Engineer at Acme. Based in Brno."

IDENTITY = Identity(name="Jan Novak", location="Brno", employers=["Acme"], skills=["python"])


def request(identity=IDENTITY, confirmed=True, **extra):
    return ResearchRequest(
        goal=Goal.HIRING, identity=identity, purpose_confirmed=confirmed, **extra
    )


def raw(statement, category="employment", year=2025):
    return {"statement": statement, "category": category, "confidence": "high", "year": year}


def make_llm(claims_by_url, groups=None, fit=4):
    def summarize(user):
        url = re.search(r'<source url="([^"]+)">', user).group(1)
        return {"subject_name": "Jan Novak", "claims": claims_by_url.get(url, [])}

    def rate(user):
        cited = ["F1"] if "F1 " in user else []
        score = fit if cited else 0
        verdict = {"score": score, "justification": "Good evidence.", "finding_ids": cited}
        return {"skill_fit": verdict, "evidence_of_work": verdict}

    return RuleLLM(
        {
            "extract professional facts": summarize,
            "group claims": lambda user: {"groups": groups or []},
            "rate one person": rate,
            "professional identity": lambda user: {
                "name": "Jan Novak",
                "location": "Brno",
                "employers": ["Acme"],
                "skills": ["Python"],
            },
        }
    )


def linkedin_doc(url=LI_JAN, text=JAN_TEXT, title="Jan Novak - Senior Python Engineer"):
    return document(url, text, title=title, network=Network.LINKEDIN)


def build(search=None, collectors=None, pages=None, llm=None, settings=None):
    fetcher = FakeFetcher(pages or {})
    config = settings or Settings(_env_file=None)
    pipeline = Pipeline(
        llm=llm or make_llm({}),
        discovery=Discovery(search or FakeSearch({}), config.discovery),
        collectors=collectors if collectors is not None else {},
        fetcher=fetcher,
        settings=config,
        now=lambda: NOW,
    )
    return pipeline, fetcher


def linkedin_collector(docs):
    return {Network.LINKEDIN: FakeCollector(Network.LINKEDIN, docs)}


async def test_full_run_merges_sources_tags_findings_and_rates():
    search = FakeSearch(
        {
            "site:linkedin.com/in": [hit(LI_JAN, "Jan Novak - Senior Python Engineer", "Brno")],
            "portfolio": [hit(SITE, "Jan", "Portfolio")],
            "site:github.com": [hit(GITHUB, "jannovak", "code")],
        }
    )
    claims = {
        LI_JAN: [raw("Senior engineer at Acme"), raw("Knows Python", "skill", 2024)],
        SITE: [raw("Engineer at Acme")],
        GITHUB: [raw("Maintains an open source parser", "project", 2023)],
    }
    llm = make_llm(claims, groups=[{"claim_ids": ["c0", "c2"], "conflict": False}])
    collectors = linkedin_collector({LI_JAN: linkedin_doc()})
    pages = {
        SITE: document(SITE, "Jan Novak, Python engineer at Acme in Brno.", title="Jan Novak"),
        GITHUB: document(GITHUB, "jannovak Jan Novak Acme Brno python", title="jannovak"),
    }
    stages = []
    pipeline, _ = build(search, collectors, pages, llm)

    report = await pipeline.research(request(), stages.append)

    assert isinstance(report, PersonReport)
    assert stages == [
        "Searching for public profiles",
        "Searching the open web",
        "Summarising sources",
        "Merging findings",
        "Rating",
    ]
    assert {s.url for s in report.sources} == {LI_JAN, SITE, GITHUB}
    linkedin_source = next(s for s in report.sources if s.url == LI_JAN)
    assert linkedin_source.match_score == 1.0
    supported = next(f for f in report.findings if f.statement.startswith("Senior engineer"))
    assert supported.tag is Tag.SUPPORTED
    assert set(supported.source_urls) == {LI_JAN, SITE}
    assert all(f.source_urls for f in report.findings)
    assert {f.tag for f in report.findings} == {Tag.SUPPORTED, Tag.SINGLE_SOURCE}
    assert report.rating.target == "python"
    assert report.rating.overall > 0
    assert report.generated_at == NOW
    assert report.purpose_confirmed is True
    assert any("not configured" in item or "no Apify actor" in item for item in report.limitations)
    assert any("not a decision" in item for item in report.limitations)
    assert all(s.claims for s in report.source_summaries)


async def test_unconfigured_networks_are_reported_and_not_searched():
    search = FakeSearch({})
    pipeline, _ = build(search, collectors={})
    report = await pipeline.research(request())
    assert all("site:linkedin.com/in" not in q for q in search.queries)
    assert sum("no Apify actor is configured" in item for item in report.limitations) == 3
    assert any("No portfolio" in item for item in report.limitations)
    assert report.findings == []
    assert report.rating.overall == 0


async def test_unconfigured_networks_are_listed_on_the_pipeline():
    pipeline, _ = build(collectors=linkedin_collector({}))
    assert pipeline.unavailable_networks == [Network.FACEBOOK, Network.INSTAGRAM]


async def test_two_similar_profiles_stop_the_run_with_a_question():
    search = FakeSearch(
        {
            "site:linkedin.com/in": [
                hit(LI_JAN, "Jan Novak", "Brno"),
                hit(LI_OTHER, "Jan Novak", "Brno"),
            ]
        }
    )
    collectors = linkedin_collector({LI_JAN: linkedin_doc(), LI_OTHER: linkedin_doc(LI_OTHER)})
    llm = make_llm({})
    pipeline, fetcher = build(search, collectors, llm=llm)

    result = await pipeline.research(request())

    assert isinstance(result, Clarification)
    (question,) = result.questions
    assert question.network is Network.LINKEDIN
    assert {option.url for option in question.options} == {LI_JAN, LI_OTHER}
    assert llm.calls == []
    assert fetcher.calls == []
    assert all("portfolio" not in query for query in search.queries)


async def test_a_single_weak_profile_asks_instead_of_guessing():
    search = FakeSearch({"site:linkedin.com/in": [hit(LI_JAN, "Jan Novak", "Prague teacher")]})
    other = linkedin_doc(text="Jan Novak. Teacher in Prague.", title="Jan Novak - Teacher")
    pipeline, _ = build(search, linkedin_collector({LI_JAN: other}))
    result = await pipeline.research(request())
    assert isinstance(result, Clarification)
    assert [o.url for o in result.questions[0].options] == [LI_JAN]


async def test_choosing_a_profile_continues_without_asking_again():
    search = FakeSearch(
        {"site:linkedin.com/in": [hit(LI_JAN, "Jan Novak", ""), hit(LI_OTHER, "Jan Novak", "")]}
    )
    docs = {LI_JAN: linkedin_doc(), LI_OTHER: linkedin_doc(LI_OTHER)}
    collectors = linkedin_collector(docs)
    llm = make_llm({LI_OTHER: [raw("Engineer at Acme")]})
    pipeline, _ = build(search, collectors, llm=llm)
    first = await pipeline.research(request())
    assert isinstance(first, Clarification)

    answered = apply_answers(
        request(), [ClarificationAnswer(network=Network.LINKEDIN, chosen_url=LI_OTHER)]
    )
    assert LI_OTHER in answered.identity.links
    report = await pipeline.research(answered)

    assert isinstance(report, PersonReport)
    assert LI_OTHER in {s.url for s in report.sources}
    assert LI_JAN not in {s.url for s in report.sources}
    assert collectors[Network.LINKEDIN].calls[-1] == LI_OTHER
    assert len(search.queries) == 1 + 4


async def test_rejecting_every_candidate_skips_the_network():
    search = FakeSearch(
        {"site:linkedin.com/in": [hit(LI_JAN, "Jan Novak", ""), hit(LI_OTHER, "Jan Novak", "")]}
    )
    collectors = linkedin_collector({LI_JAN: linkedin_doc(), LI_OTHER: linkedin_doc(LI_OTHER)})
    pipeline, _ = build(search, collectors)
    answered = apply_answers(request(), [ClarificationAnswer(network=Network.LINKEDIN)])
    assert answered.skipped_networks == [Network.LINKEDIN]
    report = await pipeline.research(answered)
    assert isinstance(report, PersonReport)
    assert any("all candidates were rejected" in item for item in report.limitations)
    assert collectors[Network.LINKEDIN].calls == []


async def test_adding_an_employer_resolves_the_ambiguity():
    name_only = Identity(name="Jan Novak")
    search = FakeSearch(
        {"site:linkedin.com/in": [hit(LI_JAN, "Jan Novak", ""), hit(LI_OTHER, "Jan Novak", "")]}
    )
    docs = {
        LI_JAN: linkedin_doc(text="Jan Novak. Engineer at Acme."),
        LI_OTHER: linkedin_doc(LI_OTHER, text="Jan Novak. Teacher at Globex."),
    }
    pipeline, _ = build(search, linkedin_collector(docs))
    assert isinstance(await pipeline.research(request(name_only)), Clarification)

    answered = apply_answers(
        request(name_only),
        [ClarificationAnswer(network=Network.LINKEDIN, employer="Acme", location="Brno")],
    )
    assert answered.identity.employers == ["Acme"]
    assert answered.identity.location == "Brno"
    assert answered.skipped_networks == []
    report = await pipeline.research(answered)
    assert isinstance(report, PersonReport)
    assert {s.url for s in report.sources} == {LI_JAN}


def test_apply_answers_keeps_the_original_request_untouched():
    original = request()
    apply_answers(original, [ClarificationAnswer(network=Network.LINKEDIN, chosen_url=LI_JAN)])
    assert original.identity.links == []


async def test_profiles_known_from_a_cv_are_read_without_searching():
    identity = IDENTITY.model_copy(update={"links": [LI_JAN, "https://www.linkedin.com/company/x"]})
    assert known_profiles(identity) == {Network.LINKEDIN: LI_JAN}
    search = FakeSearch({})
    collectors = linkedin_collector({LI_JAN: linkedin_doc()})
    pipeline, _ = build(search, collectors, llm=make_llm({LI_JAN: [raw("Engineer")]}))
    report = await pipeline.research(request(identity))
    assert isinstance(report, PersonReport)
    assert LI_JAN in {s.url for s in report.sources}
    assert all("site:linkedin.com/in" not in query for query in search.queries)
    source = next(s for s in report.sources if s.url == LI_JAN)
    assert source.match_score is None


async def test_provided_web_links_are_fetched_and_not_scored():
    identity = IDENTITY.model_copy(update={"links": ["https://jan.dev/about/"]})
    pages = {"https://jan.dev/about": document(SITE, "Totally unrelated words", title="x")}
    llm = make_llm({SITE: [raw("Built a compiler", "project")]})
    pipeline, fetcher = build(pages=pages, llm=llm)
    report = await pipeline.research(request(identity))
    assert fetcher.calls == ["https://jan.dev/about"]
    assert [s.url for s in report.sources] == [SITE]
    assert report.sources[0].match_score is None


async def test_an_unreadable_known_profile_is_reported():
    identity = IDENTITY.model_copy(update={"links": [LI_JAN]})
    collectors = linkedin_collector({LI_JAN: "The linkedin profile is private; it was not read."})
    pipeline, _ = build(collectors=collectors)
    report = await pipeline.research(request(identity))
    assert report.sources == []
    assert "The linkedin profile is private; it was not read." in report.limitations


async def test_no_plausible_profile_in_the_results_is_reported():
    search = FakeSearch(
        {
            "site:linkedin.com/in": [
                hit("https://www.linkedin.com/in/petr-svoboda", "Petr Svoboda", "")
            ]
        }
    )
    collectors = linkedin_collector({})
    pipeline, _ = build(search, collectors)
    report = await pipeline.research(request())
    assert any(
        "No linkedin profile was found in the search results" in n for n in report.limitations
    )
    assert collectors[Network.LINKEDIN].calls == []


async def test_an_unreadable_provided_link_is_reported_and_others_continue():
    identity = IDENTITY.model_copy(
        update={"links": ["https://gone.example/me", "https://jan.dev/about"]}
    )
    pages = {
        "https://gone.example/me": "gone.example answered HTTP 410.",
        "https://jan.dev/about": document(SITE, "Jan Novak works at Acme", title="Jan"),
    }
    pipeline, _ = build(pages=pages, llm=make_llm({SITE: [raw("Engineer at Acme")]}))
    report = await pipeline.research(request(identity))
    assert "gone.example answered HTTP 410." in report.limitations
    assert [s.url for s in report.sources] == [SITE]


async def test_unreadable_profiles_and_pages_are_reported_not_hidden():
    search = FakeSearch(
        {
            "site:linkedin.com/in": [hit(LI_JAN, "Jan Novak", "")],
            "portfolio": [hit(SITE, "Jan", "")],
        }
    )
    collectors = linkedin_collector({LI_JAN: "The linkedin profile is private; it was not read."})
    pages = {SITE: "jan.dev answered HTTP 403."}
    pipeline, _ = build(search, collectors, pages)
    report = await pipeline.research(request())
    assert report.sources == []
    assert "The linkedin profile is private; it was not read." in report.limitations
    assert "jan.dev answered HTTP 403." in report.limitations
    assert any("No readable linkedin profile" in item for item in report.limitations)


async def test_web_pages_about_other_people_are_discarded_and_counted():
    search = FakeSearch(
        {"portfolio": [hit(SITE, "x", ""), hit("https://other.example/p", "y", "")]}
    )
    pages = {
        SITE: document(SITE, "Jan Novak, Python engineer at Acme in Brno.", title="Jan Novak"),
        "https://other.example/p": document(
            "https://other.example/p", "Petr Svoboda sells cars.", title="Petr"
        ),
    }
    pipeline, _ = build(search, pages=pages, llm=make_llm({SITE: [raw("Engineer at Acme")]}))
    report = await pipeline.research(request())
    assert [s.url for s in report.sources] == [SITE]
    assert any("1 web page(s) were discarded" in item for item in report.limitations)


async def test_weakly_matched_pages_cannot_make_findings_stronger_than_uncertain():
    page = document(SITE, "Jan Novak works at Acme.", title="Jan Novak")
    search = FakeSearch({"portfolio": [hit(SITE, "", "")]})
    pipeline, _ = build(search, pages={SITE: page}, llm=make_llm({SITE: [raw("Engineer at Acme")]}))
    report = await pipeline.research(request())
    source = report.sources[0]
    assert 0.7 <= source.match_score < 0.85
    assert [f.tag for f in report.findings] == [Tag.UNCERTAIN]


async def test_sensitive_statements_are_discarded_and_reported():
    search = FakeSearch({"portfolio": [hit(SITE, "", "")]})
    page = document(SITE, "Jan Novak, Python engineer at Acme in Brno.", title="Jan Novak")
    claims = {
        SITE: [raw("Engineer at Acme"), raw("Attends church weekly"), raw("Is married", "profile")]
    }
    pipeline, _ = build(search, pages={SITE: page}, llm=make_llm(claims))
    report = await pipeline.research(request())
    assert [f.statement for f in report.findings] == ["Engineer at Acme"]
    assert any("2 extracted statement(s) were discarded" in item for item in report.limitations)
    assert "church" not in report.model_dump_json()


async def test_name_only_identity_is_flagged_as_weakly_verifiable():
    pipeline, _ = build()
    report = await pipeline.research(request(Identity(name="Jan Novak")))
    assert any("contains only a name" in item for item in report.limitations)


async def test_focus_is_the_rating_target():
    pipeline, _ = build()
    report = await pipeline.research(request(focus="Staff backend engineer"))
    assert report.rating.target == "Staff backend engineer"
    assert report.focus == "Staff backend engineer"


async def test_refusals_happen_before_any_external_call():
    search = FakeSearch({})
    llm = make_llm({})
    pipeline, fetcher = build(search, llm=llm)
    with pytest.raises(ComplianceRefusal, match="lawful purpose"):
        await pipeline.research(request(confirmed=False))
    harmful = Identity(name="Jan Novak", employers=["find home address"])
    with pytest.raises(ComplianceRefusal, match="Refused"):
        await pipeline.research(request(harmful))
    with pytest.raises(ComplianceRefusal):
        await pipeline.skill_search(
            SkillSearchRequest(
                goal=Goal.HIRING, skill="stalk", location="Brno", purpose_confirmed=True
            )
        )
    assert search.queries == [] and llm.calls == [] and fetcher.calls == []


def skill_request(limit=3):
    return SkillSearchRequest(
        goal=Goal.HIRING, skill="Rust", location="Brno", limit=limit, purpose_confirmed=True
    )


async def test_skill_search_ranks_candidates_and_marks_the_best():
    gh_a, gh_b = "https://github.com/ada", "https://github.com/bob"
    search = FakeSearch(
        {
            "site:linkedin.com/in": [hit(LI_JAN, "Jan", "")],
            "site:github.com": [hit(gh_a, "ada", ""), hit(gh_b, "bob", "")],
        }
    )
    collectors = linkedin_collector({LI_JAN: linkedin_doc()})
    pages = {gh_a: document(gh_a, "ada rust"), gh_b: document(gh_b, "bob rust")}

    def summarize(user):
        url = re.search(r'<source url="([^"]+)">', user).group(1)
        names = {LI_JAN: "Jan Novak", gh_a: "Ada Lovelace", gh_b: "Bob Builder"}
        years = {LI_JAN: 2012, gh_a: 2026, gh_b: 2020}
        return {
            "subject_name": names[url],
            "claims": [raw(f"Writes Rust for {names[url]}", "skill", years[url])],
        }

    def rate(user):
        verdict = {"score": 4, "justification": "Rust work.", "finding_ids": ["F1"]}
        return {"skill_fit": verdict, "evidence_of_work": verdict}

    llm = RuleLLM({"extract professional facts": summarize, "rate one person": rate})
    pipeline, _ = build(search, collectors, pages, llm)
    stages = []

    report = await pipeline.skill_search(skill_request(), stages.append)

    assert isinstance(report, SkillSearchReport)
    assert stages == [
        "Searching for candidates",
        "Reading candidate profiles",
        "Summarising candidates",
        "Rating candidates",
    ]
    assert [c.name for c in report.candidates] == ["Ada Lovelace", "Bob Builder", "Jan Novak"]
    assert [c.best for c in report.candidates] == [True, False, False]
    assert [c.rank for c in report.candidates] == [1, 2, 3]
    assert all(c.claims and c.claims[0].source_url == c.profile_url for c in report.candidates)
    assert report.skill == "Rust" and report.location == "Brno"
    assert any("single profile" in item for item in report.limitations)


async def test_skill_search_leaves_out_unverifiable_candidates_and_reports_them():
    gh = "https://github.com/ada"
    search = FakeSearch(
        {"site:linkedin.com/in": [hit(LI_JAN, "Jan", "")], "site:github.com": [hit(gh, "ada", "")]}
    )
    collectors = linkedin_collector({LI_JAN: "The linkedin profile is private; it was not read."})
    pages = {gh: document(gh, "ada")}

    def summarize(user):
        return {"subject_name": None, "claims": [raw("Writes Rust")]}

    llm = RuleLLM({"extract professional facts": summarize})
    pipeline, _ = build(search, collectors, pages, llm)
    report = await pipeline.skill_search(skill_request())
    assert report.candidates == []
    assert "The linkedin profile is private; it was not read." in report.limitations
    assert any(f"{gh} yielded no named" in item for item in report.limitations)


async def test_skill_search_without_results_or_actor_says_so():
    pipeline, _ = build(FakeSearch({"site:linkedin.com/in": [hit(LI_JAN, "Jan", "")]}))
    report = await pipeline.skill_search(skill_request())
    assert report.candidates == []
    assert any("no actor is configured" in item for item in report.limitations)
    empty, _ = build()
    assert any(
        "no candidate profiles" in item
        for item in (await empty.skill_search(skill_request())).limitations
    )


async def test_identity_from_cv_runs_extraction_and_the_model():
    pipeline, _ = build(llm=make_llm({}))
    data = make_pdf(["Jan Novak", "Senior Python Engineer at Acme, Brno", "Skills: Python"])
    identity = await pipeline.identity_from_cv(data, "cv.pdf")
    assert identity.name == "Jan Novak"
    assert identity.employers == ["Acme"]
