# tests/fakes.py
from agent.models import (
    Category,
    Claim,
    CompanyFact,
    CompanyProfile,
    Confidence,
    Criterion,
    CriterionRating,
    FactKind,
    Finding,
    Goal,
    Identity,
    Network,
    PersonReport,
    Priority,
    RankedCandidate,
    Rating,
    Requirement,
    RequirementKind,
    RequirementResult,
    RequirementStatus,
    ResearchOptions,
    Scepticism,
    ScepticismFlag,
    ScepticismLevel,
    ScepticismMode,
    SearchHit,
    Severity,
    SkillSearchReport,
    Source,
    SourceDocument,
    SourceSummary,
    Tag,
)
from agent.config import (
    LlmTuning,
    RequirementTuning,
)
from agent.events import (
    Kind,
    Task,
)
from datetime import (
    datetime,
    UTC,
)
from agent.errors import SourceUnavailable
from agent.requirements import summarise
from collections.abc import Callable
from agent.compliance import NOTICE
from agent.llm import StructuredLLM

import json

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def hit(url: str, title: str = "", snippet: str = "") -> SearchHit:
    return SearchHit(url=url, title=title, snippet=snippet)


def document(url: str, text: str, title: str = "Doc", network: Network = Network.WEB, **extra):
    return SourceDocument(
        url=url, network=network, title=title, text=text, retrieved_at=NOW, **extra
    )


class FakeSearch:
    def __init__(self, routes: dict[str, list[SearchHit]]):
        self.routes = routes
        self.queries: list[str] = []
        self.max_query_words = 32
        self.max_query_chars = 400

    async def search(self, query: str, count: int) -> list[SearchHit]:
        self.queries.append(query)
        for fragment, hits in self.routes.items():
            if fragment in query:
                return list(hits)
        return []


class FakeCollector:
    def __init__(self, network: Network, documents: dict[str, SourceDocument | str]):
        self.network = network
        self.documents = documents
        self.calls: list[str] = []

    async def collect(self, url: str) -> SourceDocument:
        self.calls.append(url)
        item = self.documents[url]
        if isinstance(item, str):
            raise SourceUnavailable(item)
        return item


class FakeFetcher:
    def __init__(self, documents: dict[str, SourceDocument | str]):
        self.documents = documents
        self.calls: list[str] = []

    async def fetch(self, url: str) -> SourceDocument:
        self.calls.append(url)
        item = self.documents.get(url)
        if item is None:
            raise SourceUnavailable(f"{url} is not available")
        if isinstance(item, str):
            raise SourceUnavailable(item)
        return item


class RuleLLM(StructuredLLM):
    def __init__(self, rules: dict[str, Callable[[str], dict]]):
        super().__init__(self, LlmTuning())
        self.rules = rules
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, as_json: bool = True) -> str:
        self.calls.append((system, user))
        for fragment, handler in self.rules.items():
            if fragment in system:
                answer = handler(user)
                return answer if isinstance(answer, str) else json.dumps(answer)
        raise AssertionError(f"no rule for system prompt: {system[:80]}")


def sample_rating(overall: float = 3.4):
    criteria = [
        CriterionRating(
            criterion=criterion,
            score=3,
            justification=f"{criterion.value} is fine",
            source_urls=["https://jan.dev/about"],
        )
        for criterion in Criterion
    ]
    return Rating(
        target="Python engineer",
        criteria=criteria,
        raw_overall=overall,
        overall=overall,
        scepticism=Scepticism(mode=ScepticismMode.STANDARD, level=ScepticismLevel.NONE),
    )


def person_report():
    url = "https://jan.dev/about"
    claim = Claim(
        statement="Senior engineer at Acme",
        category=Category.EMPLOYMENT,
        confidence=Confidence.HIGH,
        year=2025,
        source_url=url,
    )
    return PersonReport(
        generated_at=NOW,
        goal=Goal.HIRING,
        notice=NOTICE,
        purpose_confirmed=True,
        options=ResearchOptions(),
        sources=[
            Source(
                url=url,
                network=Network.WEB,
                title="Jan | Portfolio",
                retrieved_at=NOW,
                match_score=0.9,
            )
        ],
        limitations=["No Instagram profile was found in the search results."],
        identity=Identity(name="Jan Novak", location="Brno", employers=["Acme"], skills=["python"]),
        focus="Python engineer",
        source_summaries=[
            SourceSummary(url=url, network=Network.WEB, title="Jan | Portfolio", claims=[claim])
        ],
        findings=[
            Finding(
                id="F1",
                statement=claim.statement,
                category=Category.EMPLOYMENT,
                tag=Tag.SINGLE_SOURCE,
                source_urls=[url],
                independent_sources=1,
                year=2025,
            )
        ],
        rating=sample_rating(),
    )


def sceptical_report():
    report = person_report()
    scepticism = Scepticism(
        mode=ScepticismMode.STANDARD,
        level=ScepticismLevel.HIGH,
        flags=[
            ScepticismFlag(
                code="overlapping_roles", severity=Severity.HIGH, message="Roles overlap."
            )
        ],
        cap=2.5,
        guidance=["Check dates."],
    )
    rating = report.rating.model_copy(
        update={"raw_overall": 4.4, "overall": 2.5, "scepticism": scepticism}
    )
    return report.model_copy(update={"rating": rating})


def skill_report():
    url = "https://github.com/jan"
    claim = Claim(
        statement="Maintains a Rust crate",
        category=Category.PROJECT,
        confidence=Confidence.HIGH,
        source_url=url,
    )
    candidates = [
        RankedCandidate(
            rank=1,
            best=True,
            name="Jan Novak",
            profile_url=url,
            network=Network.WEB,
            claims=[claim],
            rating=sample_rating(),
        )
    ]
    return SkillSearchReport(
        generated_at=NOW,
        goal=Goal.HIRING,
        notice=NOTICE,
        purpose_confirmed=True,
        options=ResearchOptions(),
        sources=[Source(url=url, network=Network.WEB, title="Jan", retrieved_at=NOW)],
        limitations=["Each candidate is a single profile."],
        skill="Rust",
        location="Brno",
        candidates=candidates,
    )


def company_profile():
    site = "https://acme.example"
    return CompanyProfile(
        name="Acme",
        website=site,
        facts=[
            CompanyFact(kind=FactKind.CATEGORY, statement="Payments software", source_url=site),
            CompanyFact(
                kind=FactKind.PRODUCT,
                statement="Acme builds card payment terminals for small shops.",
                source_url=f"{site}/products",
            ),
            CompanyFact(
                kind=FactKind.MARKET, statement="Customers are small retailers.", source_url=site
            ),
            CompanyFact(kind=FactKind.PROVIDED, statement="We build payment software for shops"),
        ],
    )


def requirements_assessment():
    results = [
        RequirementResult(
            id="R1",
            requirement=Requirement(kind=RequirementKind.LANGUAGE, label="English", level="C1"),
            status=RequirementStatus.MET,
            justification="The LinkedIn profile lists English as full professional proficiency.",
            finding_ids=["F1"],
            source_urls=["https://jan.dev/about"],
        ),
        RequirementResult(
            id="R2",
            requirement=Requirement(
                kind=RequirementKind.SKILL, label="Python", minimum_years=5, priority=Priority.MUST
            ),
            status=RequirementStatus.PARTIAL,
            justification="Python work is documented for about three years.",
            finding_ids=["F2"],
            source_urls=["https://github.com/jannovak"],
        ),
        RequirementResult(
            id="R3",
            requirement=Requirement(
                kind=RequirementKind.LANGUAGE, label="Czech", level="B2", priority=Priority.NICE
            ),
            status=RequirementStatus.UNKNOWN,
            justification="No finding mentions Czech.",
        ),
    ]
    return summarise(results, RequirementTuning())


def hiring_report():
    base = person_report()
    urls = [
        "https://jan.dev/about",
        "https://github.com/jannovak",
        "https://www.linkedin.com/in/jan-novak",
    ]
    spec = [
        (
            Category.EMPLOYMENT,
            "Senior Python engineer at Acme since 2021",
            Tag.SUPPORTED,
            [0, 2],
            2025,
        ),
        (Category.EMPLOYMENT, "Backend developer at Globex", Tag.SINGLE_SOURCE, [2], 2019),
        (
            Category.EDUCATION,
            "MSc in Computer Science at Brno University of Technology",
            Tag.SINGLE_SOURCE,
            [2],
            2015,
        ),
        (
            Category.PROJECT,
            "Maintains an open source HTML parser with 1.2k stars",
            Tag.SUPPORTED,
            [0, 1],
            2024,
        ),
        (Category.TALK, "Spoke about async Python at PyCon CZ", Tag.UNCERTAIN, [0], 2023),
    ]
    findings = [
        Finding(
            id=f"F{number}",
            statement=statement,
            category=category,
            tag=tag,
            source_urls=[urls[index] for index in indexes],
            independent_sources=len(indexes),
            year=year,
        )
        for number, (category, statement, tag, indexes, year) in enumerate(spec, 1)
    ]
    sources = [
        Source(url=url, network=network, title=title, retrieved_at=NOW, match_score=0.9)
        for url, network, title in (
            (urls[0], Network.WEB, "Jan | Portfolio"),
            (urls[1], Network.WEB, "jannovak on GitHub"),
            (urls[2], Network.LINKEDIN, "Jan Novak | LinkedIn"),
        )
    ]
    return base.model_copy(
        update={
            "findings": findings,
            "sources": sources,
            "rating": sample_rating(3.6),
            "company": company_profile(),
            "requirements": requirements_assessment(),
        }
    )


def replay(report, reporter):
    company = report.company
    if company is not None:
        reporter(f"Learning about {company.name}", Task.COMPANY)
        for fact in company.facts:
            reporter.emit(
                Kind.COMPANY,
                fact.statement,
                Task.COMPANY,
                fact_kind=fact.kind.value,
                url=fact.source_url,
                name=company.name,
            )
    reporter("Searching for public profiles", Task.SEARCH)
    reporter.emit(Kind.STEP, "Looking for a linkedin profile", Task.LINKEDIN)
    for source in report.sources:
        task = Task(source.network.value)
        reporter.emit(
            Kind.CANDIDATE,
            f"Possible profile: {source.title}",
            task,
            network=source.network.value,
            url=source.url,
            score=0.8,
        )
        reporter.emit(
            Kind.SOURCE, source.title, task, network=source.network.value, url=source.url, score=0.9
        )
    reporter("Summarising sources", Task.SUMMARISE)
    for summary in report.source_summaries:
        for claim in summary.claims:
            reporter.emit(
                Kind.CLAIM,
                claim.statement,
                Task.SUMMARISE,
                url=claim.source_url,
                category=claim.category.value,
                confidence="high",
            )
    reporter("Merging findings", Task.MERGE)
    for finding in report.findings:
        reporter.emit(
            Kind.FINDING,
            finding.statement,
            Task.MERGE,
            id=finding.id,
            tag=finding.tag.value,
            category=finding.category.value,
            urls=finding.source_urls,
        )
    if report.requirements is not None:
        reporter("Checking the requirements", Task.REQUIREMENTS)
        for item in report.requirements.results:
            reporter.emit(
                Kind.REQUIREMENT,
                f"{item.requirement.description}: {item.status.value}",
                Task.REQUIREMENTS,
                id=item.id,
                status=item.status.value,
                priority=item.requirement.priority.value,
                detail=item.justification,
                urls=item.source_urls,
            )
    reporter("Rating", Task.RATE)
    rating = report.rating
    reporter.emit(
        Kind.RATING,
        f"Overall rating {rating.overall:.1f} out of 5",
        Task.RATE,
        overall=rating.overall,
        raw=rating.raw_overall,
        criteria=[
            {"name": item.criterion.value, "score": item.score, "text": item.justification}
            for item in rating.criteria
        ],
    )
    reporter.emit(
        Kind.SCEPTICISM,
        "Scepticism check: no warning signs",
        Task.SCEPTICISM,
        level="none",
        flags=[],
    )


def structured(client) -> StructuredLLM:
    return StructuredLLM(client, LlmTuning())


def rated_skill_report(overall):
    base = skill_report()
    candidate = base.candidates[0].model_copy(update={"rating": sample_rating(overall)})
    return base.model_copy(update={"candidates": [candidate]})
