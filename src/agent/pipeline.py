# src/agent/pipeline.py
from agent.models import (
    Candidate,
    Clarification,
    ClarificationAnswer,
    ClarificationQuestion,
    CompanyInput,
    CompanyProfile,
    FactKind,
    Identity,
    Network,
    PersonReport,
    Rating,
    Requirement,
    RequirementsAssessment,
    ResearchOptions,
    ResearchRequest,
    ScepticismLevel,
    SearchHit,
    SkillSearchReport,
    SkillSearchRequest,
    SourceDocument,
    SourceSummary,
    utcnow,
)
from agent.disambiguation import (
    Ambiguous,
    build_question,
    Confirmed,
    decide,
    make_candidate,
    score_text,
)
from agent.events import (
    as_reporter,
    Kind,
    Notes,
    Reporter,
    StageCallback,
    Task,
)
from agent.urls import (
    hostname,
    network_of,
    normalize_web_url,
    profile_url,
    social_network,
    SOCIAL_NETWORKS,
)
from agent.summary import (
    build_findings,
    merge_claims,
    single_claim_groups,
    summarize_sources,
)
from agent.compliance import (
    check_research_request,
    check_skill_request,
    NOTICE,
)
from agent.errors import (
    InvalidRequest,
    LLMOutputError,
    SourceUnavailable,
)
from agent.rating import (
    rank_candidates,
    rate,
    Scored,
)
from agent.company import (
    company_context,
    CompanyResearcher,
)
from agent.identity import (
    extract_cv,
    identity_from_cv,
)
from agent.requirements import assess_requirements
from agent.collectors import ProfileCollector
from agent.discovery import Discovery
from agent.fetcher import PageFetcher
from collections.abc import Callable
from agent.llm import StructuredLLM
from agent.config import Settings
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import asyncio

Progress = Reporter | StageCallback | None
NETWORK_TASKS = {
    Network.LINKEDIN: Task.LINKEDIN,
    Network.FACEBOOK: Task.FACEBOOK,
    Network.INSTAGRAM: Task.INSTAGRAM,
    Network.WEB: Task.WEB,
}
SKILL_SEARCH_NETWORKS = (Network.LINKEDIN, Network.WEB)
STANDING_LIMITATIONS = (
    "Only public information reachable through the search API, the configured actors and "
    "robots.txt-permitted pages was reviewed; private or gated content was not accessed.",
    "Ratings are automated decision support based on the findings above, not a decision; a "
    "human must review the sources before acting.",
)


class Researcher(Protocol):
    """The interface of a research backend, as used by the web and chat layers."""

    @property
    def unavailable_networks(self) -> list[Network]:
        """List the social networks that cannot be collected right now."""
        ...

    async def identity_from_cv(self, data: bytes, filename: str) -> Identity:
        """Extract an identity from an uploaded CV.

        Args:
            data: Raw bytes of the CV file.
            filename: Original file name, used to pick the parser.

        Returns:
            The identity described by the CV.
        """
        ...

    async def research(
        self, request: ResearchRequest, progress: Progress = None
    ) -> PersonReport | Clarification:
        """Research one person, or ask a clarifying question first.

        Args:
            request: What to research and how.
            progress: Optional callback that receives stage names.

        Returns:
            A finished report, or a clarification that needs an answer.
        """
        ...

    async def skill_search(
        self, request: SkillSearchRequest, progress: Progress = None
    ) -> SkillSearchReport:
        """Search for people who have a skill in a place.

        Args:
            request: The skill, place and options.
            progress: Optional callback that receives stage names.

        Returns:
            A ranked report of matching candidates.
        """
        ...


@dataclass(frozen=True)
class MatchRules:
    """The score threshold and margin used to accept a profile as the right person."""

    threshold: float
    margin: float


def known_profiles(identity: Identity) -> dict[Network, str]:
    """Find the social profile URLs that an identity already links to.

    Args:
        identity: The identity whose links are inspected.

    Returns:
        A map of network to canonical profile URL, keeping the first link per network.
    """
    profiles: dict[Network, str] = {}
    for link in identity.links:
        canonical = profile_url(link)
        network = social_network(hostname(canonical)) if canonical else None
        if canonical and network:
            profiles.setdefault(network, canonical)
    return profiles


def apply_answers(request: ResearchRequest, answers: list[ClarificationAnswer]) -> ResearchRequest:
    """Apply the user's clarification answers to a research request.

    A chosen URL is added to the identity links, a city or employer refines the
    identity (the employer goes first), and an answer without details marks the
    network as skipped.

    Args:
        request: The request that produced the clarification.
        answers: The user's answers, one per question.

    Returns:
        A new validated request; the original is not modified.
    """
    identity = request.identity.model_dump()
    skipped = list(request.skipped_networks)
    for answer in answers:
        if answer.chosen_url is not None:
            identity["links"].append(answer.chosen_url)
        elif answer.location is not None or answer.employer is not None:
            identity["location"] = answer.location or identity["location"]
            if answer.employer is not None:
                identity["employers"].insert(0, answer.employer)
        elif answer.network not in skipped:
            skipped.append(answer.network)
    return ResearchRequest.model_validate(
        {**request.model_dump(), "identity": identity, "skipped_networks": skipped}
    )


def _discard_note(summaries: list[SourceSummary]) -> list[str]:
    """Create the limitation note about discarded statements.

    Args:
        summaries: The per-source summaries.

    Returns:
        A one-item list with the total count of discarded statements, or an empty list
        when none were discarded.
    """
    discarded = sum(summary.discarded for summary in summaries)
    if not discarded:
        return []
    return [
        f"{discarded} extracted statement(s) were discarded because they were outside the "
        "permitted professional scope or contained sensitive or contact details."
    ]


def _announce_claims(reporter: Reporter, summaries: list[SourceSummary]) -> None:
    """Report every extracted claim as a live progress event.

    Args:
        reporter: The reporter that receives the events.
        summaries: The per-source summaries holding the claims.
    """
    for summary in summaries:
        for claim in summary.claims:
            reporter.emit(
                Kind.CLAIM,
                claim.statement,
                Task.SUMMARISE,
                url=claim.source_url,
                category=claim.category.value,
                confidence=claim.confidence.value,
            )


def _announce_rating(reporter: Reporter, rating: Rating, subject: str | None = None) -> None:
    """Report a rating and its scepticism check as live progress events.

    Args:
        reporter: The reporter that receives the events.
        rating: The rating to announce.
        subject: The name of the rated person for skill searches, or None.
    """
    label = f"Overall rating {rating.overall:.1f} out of 5"
    reporter.emit(
        Kind.RATING,
        f"{subject}: {label}" if subject else label,
        Task.RATE,
        subject=subject,
        overall=rating.overall,
        raw=rating.raw_overall,
        criteria=[
            {"name": item.criterion.value, "score": item.score, "text": item.justification}
            for item in rating.criteria
        ],
    )
    scepticism = rating.scepticism
    if scepticism.level is ScepticismLevel.NONE:
        reporter.emit(
            Kind.SCEPTICISM,
            "Scepticism check: no warning signs",
            Task.SCEPTICISM,
            level=scepticism.level.value,
            flags=[],
        )
        return
    reporter.emit(
        Kind.SCEPTICISM,
        f"Scepticism check: {scepticism.level.value}. "
        + " ".join(flag.message for flag in scepticism.flags[:2]),
        Task.SCEPTICISM,
        level=scepticism.level.value,
        flags=[{"code": flag.code, "message": flag.message} for flag in scepticism.flags],
    )


class Pipeline:
    """The research workflow: find sources, read them, summarise, merge and rate.

    It searches social networks and the open web for public information only, never
    returns a report without the compliance notice, and records every skipped or
    failed source as a limitation instead of failing the whole run.
    """

    def __init__(
        self,
        *,
        llm: StructuredLLM,
        discovery: Discovery,
        collectors: dict[Network, ProfileCollector],
        fetcher: PageFetcher,
        settings: Settings,
        now: Callable[[], datetime] = utcnow,
    ):
        """Initialize the pipeline.

        Args:
            llm: The structured language model client.
            discovery: The search service that finds profiles and pages.
            collectors: The profile collectors keyed by social network.
            fetcher: The fetcher for open web pages.
            settings: The application settings.
            now: A clock returning the current time, replaceable in tests.
        """
        self._llm = llm
        self._discovery = discovery
        self._collectors = collectors
        self._fetcher = fetcher
        self._settings = settings
        self._now = now
        self._company = CompanyResearcher(llm, fetcher, discovery, settings.company)

    @property
    def unavailable_networks(self) -> list[Network]:
        """Return the social networks that have no collector configured."""
        return [network for network in SOCIAL_NETWORKS if network not in self._collectors]

    def match_rules(self, options: ResearchOptions) -> MatchRules:
        """Work out the match threshold and margin for a request.

        A strictness preset replaces the default values, and an explicit threshold or
        margin in the options replaces those in turn.

        Args:
            options: The research options of the request.

        Returns:
            The MatchRules to apply.
        """
        scoring = self._settings.scoring
        threshold, margin = scoring.threshold, scoring.margin
        if options.strictness is not None:
            preset = scoring.presets[options.strictness.value]
            threshold, margin = preset.threshold, preset.margin
        return MatchRules(
            threshold=threshold if options.threshold is None else options.threshold,
            margin=margin if options.margin is None else options.margin,
        )

    async def identity_from_cv(self, data: bytes, filename: str) -> Identity:
        """Extract a candidate identity from an uploaded CV.

        The file is parsed in a worker thread.

        Args:
            data: The raw bytes of the CV file.
            filename: The file name, used to pick the parser.

        Returns:
            The identity read from the CV.
        """
        content = await asyncio.to_thread(extract_cv, data, filename, self._settings.cv)
        return await identity_from_cv(self._llm, content)

    async def research(
        self, request: ResearchRequest, progress: Progress = None
    ) -> PersonReport | Clarification:
        """Research one person: collect sources, summarise, merge and rate.

        Profiles on all social networks are searched concurrently. If any network has
        several plausible profiles, the run stops early with questions to answer.

        Args:
            request: The identity, goal, options and requirements to research.
            progress: A reporter or stage callback for live progress, or None.

        Returns:
            A PersonReport, or a Clarification when profiles need to be disambiguated.

        Raises:
            ComplianceRefusal: when the request fails the compliance check.
        """
        reporter = as_reporter(progress)
        check_research_request(request)
        identity = request.identity
        options = request.options
        rules = self.match_rules(options)
        notes = Notes(reporter)
        known = known_profiles(identity)
        skipped = set(request.skipped_networks)

        company = await self._company_profile(request.company, reporter, notes)
        company_text = company_context(company, self._settings.company)

        reporter("Searching for public profiles", Task.SEARCH)
        outcomes = await asyncio.gather(
            *(
                self._social_source(
                    identity, network, known, skipped, options, rules, notes, reporter
                )
                for network in SOCIAL_NETWORKS
            )
        )
        questions = [item for item in outcomes if isinstance(item, ClarificationQuestion)]
        if questions:
            return Clarification(questions=questions)
        documents = [item for item in outcomes if isinstance(item, SourceDocument)]

        reporter("Searching the open web", Task.WEB)
        documents.extend(await self._web_sources(identity, options, rules, notes, reporter))

        reporter("Summarising sources", Task.SUMMARISE)
        year = self._now().year
        summary = self._settings.summary
        summaries = await summarize_sources(
            self._llm, request.goal, identity, documents, year, summary
        )
        _announce_claims(reporter, summaries)
        # Sources scoring below threshold plus margin do not count as independent corroboration.
        weak_below = rules.threshold + rules.margin
        weak_urls = {
            doc.url
            for doc in documents
            if doc.match_score is not None and doc.match_score < weak_below
        }
        reporter("Merging findings", Task.MERGE)
        findings = await merge_claims(self._llm, summaries, weak_urls, summary)
        for finding in findings:
            reporter.emit(
                Kind.FINDING,
                finding.statement,
                Task.MERGE,
                id=finding.id,
                tag=finding.tag.value,
                category=finding.category.value,
                urls=finding.source_urls,
            )
        notes.extend(_discard_note(summaries))
        if not findings:
            notes.append("No professional findings could be confirmed from the sources read.")
        if not (identity.location or identity.employers or identity.skills or identity.roles):
            notes.append(
                "The identity contains only a name, so matches could not be checked against a "
                "location, employer or skills; pages about other people with the same name may "
                "be included."
            )

        assessment = await self._requirements(
            request.requirements, findings, company_text, reporter, notes
        )
        reporter("Rating", Task.RATE)
        target = request.focus or ", ".join([*identity.roles, *identity.skills]) or None
        rating = await rate(
            self._llm,
            request.goal,
            target,
            findings,
            year,
            self._settings.rating,
            self._settings.scepticism,
            options.scepticism,
            company=company_text,
        )
        _announce_rating(reporter, rating)
        return PersonReport(
            generated_at=self._now(),
            goal=request.goal,
            notice=NOTICE,
            purpose_confirmed=True,
            options=options,
            sources=[doc.as_source() for doc in documents],
            limitations=[*notes, *STANDING_LIMITATIONS],
            identity=identity,
            focus=request.focus,
            source_summaries=summaries,
            findings=findings,
            rating=rating,
            company=company,
            requirements=assessment,
        )

    async def skill_search(
        self, request: SkillSearchRequest, progress: Progress = None
    ) -> SkillSearchReport:
        """Find people with a skill in a location, then read and rate each profile.

        Each profile is treated as its own candidate. Profiles without a named subject or
        verifiable claims are left out and noted as limitations.

        Args:
            request: The skill, location, goal and options to search with.
            progress: A reporter or stage callback for live progress, or None.

        Returns:
            A SkillSearchReport with the ranked candidates.

        Raises:
            ComplianceRefusal: when the request fails the compliance check.
            InvalidRequest: when neither LinkedIn nor the open web is enabled.
        """
        reporter = as_reporter(progress)
        check_skill_request(request)
        options = request.options
        networks = [n for n in SKILL_SEARCH_NETWORKS if n in options.networks]
        if not networks:
            raise InvalidRequest("Skill search needs LinkedIn or the open web as a source.")
        notes = Notes(reporter)
        company = await self._company_profile(request.company, reporter, notes)
        company_text = company_context(company, self._settings.company)
        reporter("Searching for candidates", Task.SEARCH)
        hits = await self._discovery.find_skill_candidates(
            request.skill, request.location, request.limit, networks
        )
        for hit in hits:
            reporter.emit(
                Kind.CANDIDATE,
                hit.title or hit.url,
                Task.SEARCH,
                network=network_of(hit.url).value,
                url=hit.url,
                snippet=hit.snippet,
            )
        if not hits:
            notes.append("The search returned no candidate profiles for this skill and location.")

        reporter("Reading candidate profiles", Task.WEB)
        documents = await self._read_hits(hits, notes, reporter)

        reporter("Summarising candidates", Task.SUMMARISE)
        year = self._now().year
        summary = self._settings.summary
        summaries = await summarize_sources(self._llm, request.goal, None, documents, year, summary)
        _announce_claims(reporter, summaries)

        reporter("Rating candidates", Task.RATE)
        scored: list[Scored] = []
        # Summaries are returned in document order, so the two lists line up one to one.
        for document, source_summary in zip(documents, summaries, strict=True):
            if not source_summary.subject_name or not source_summary.claims:
                notes.append(
                    f"The profile {document.url} yielded no named, verifiable claims and was "
                    "left out."
                )
                continue
            count = len(source_summary.claims)
            findings = build_findings(
                source_summary.claims, single_claim_groups(count), set(), summary
            )
            assessment = await self._requirements(
                request.requirements, findings, company_text, reporter, notes
            )
            rating = await rate(
                self._llm,
                request.goal,
                request.skill,
                findings,
                year,
                self._settings.rating,
                self._settings.scepticism,
                options.scepticism,
                corroboration_expected=False,
                company=company_text,
            )
            _announce_rating(reporter, rating, source_summary.subject_name)
            scored.append(
                Scored(
                    source_summary.subject_name,
                    document.url,
                    document.network,
                    source_summary.claims,
                    rating,
                    assessment,
                )
            )
        notes.extend(_discard_note(summaries))
        notes.append(
            "Each candidate is a single profile; the same person may appear more than once, and "
            "location and skill relevance come from search ranking and profile text only."
        )
        return SkillSearchReport(
            generated_at=self._now(),
            goal=request.goal,
            notice=NOTICE,
            purpose_confirmed=True,
            options=options,
            sources=[doc.as_source() for doc in documents],
            limitations=[*notes, *STANDING_LIMITATIONS],
            skill=request.skill,
            location=request.location,
            candidates=rank_candidates(scored),
            company=company,
        )

    async def _company_profile(
        self, company: CompanyInput | None, reporter: Reporter, notes: list[str]
    ) -> CompanyProfile | None:
        """Build the profile of the hiring company, if one was given.

        Failures to summarise the company pages are recorded as limitations instead of
        being raised.

        Args:
            company: The company details provided by the user, or None.
            reporter: The reporter that receives progress events.
            notes: The list of limitation notes to append to.

        Returns:
            The CompanyProfile, or None when no company was given.
        """
        if company is None:
            return None
        reporter(f"Learning about {company.name}", Task.COMPANY)
        try:
            profile = await self._company.profile(company, notes)
        except LLMOutputError:
            notes.append(
                f"The public pages about {company.name} could not be summarised; only the "
                "details you provided were used."
            )
            profile = CompanyProfile(name=company.name, website=company.website)
        for fact in profile.facts:
            reporter.emit(
                Kind.COMPANY,
                fact.statement,
                Task.COMPANY,
                fact_kind=fact.kind.value,
                url=fact.source_url,
                name=company.name,
            )
        if not any(fact.kind is not FactKind.PROVIDED for fact in profile.facts):
            notes.append(f"No public facts about {company.name} could be confirmed.")
        return profile

    async def _requirements(
        self,
        requirements: list[Requirement],
        findings: list,
        company_text: str | None,
        reporter: Reporter,
        notes: list[str],
    ) -> RequirementsAssessment | None:
        """Assess the requirements against the findings.

        Args:
            requirements: The requirements to check.
            findings: The merged findings to check them against.
            company_text: The company context for the model, or None.
            reporter: The reporter that receives progress events.
            notes: The list of limitation notes to append to.

        Returns:
            The assessment, or None when there are no requirements or the model gave no
            usable answer.
        """
        if not requirements:
            return None
        reporter("Checking the requirements", Task.REQUIREMENTS)
        try:
            assessment = await assess_requirements(
                self._llm, requirements, findings, self._settings.requirements, company_text
            )
        except LLMOutputError:
            notes.append(
                "The requirements could not be assessed because the model did not return a "
                "usable answer."
            )
            return None
        for result in assessment.results:
            reporter.emit(
                Kind.REQUIREMENT,
                f"{result.requirement.description}: {result.status.value}",
                Task.REQUIREMENTS,
                id=result.id,
                status=result.status.value,
                priority=result.requirement.priority.value,
                detail=result.justification,
                urls=result.source_urls,
            )
        return assessment

    async def _read_hits(
        self, hits: list[SearchHit], notes: list[str], reporter: Reporter
    ) -> list[SourceDocument]:
        """Read the pages behind search hits concurrently.

        Hits that cannot be read are recorded in the notes and skipped.

        Args:
            hits: The search hits to read.
            notes: The list of limitation notes to append to.
            reporter: The reporter that receives progress events.

        Returns:
            The documents that could be read, in hit order.
        """

        async def read(hit: SearchHit) -> SourceDocument | None:
            """Read one hit and announce the source.

            Args:
                hit: The search hit to read.

            Returns:
                The document, or None when the source is unavailable.
            """
            try:
                document = await self._read(hit.url)
            except SourceUnavailable as error:
                notes.append(str(error))
                return None
            self._announce_source(reporter, document)
            return document

        results = await asyncio.gather(*(read(hit) for hit in hits))
        return [document for document in results if document is not None]

    @staticmethod
    def _announce_source(reporter: Reporter, document: SourceDocument) -> None:
        """Report a source that was read as a live progress event.

        Args:
            reporter: The reporter that receives the event.
            document: The document that was read.
        """
        reporter.emit(
            Kind.SOURCE,
            document.title or document.url,
            NETWORK_TASKS[document.network],
            network=document.network.value,
            url=document.url,
            score=document.match_score,
        )

    async def _read(self, url: str) -> SourceDocument:
        """Read a URL with the fetcher or the collector of its network.

        Args:
            url: The page or profile URL to read.

        Returns:
            The document read from the URL.

        Raises:
            SourceUnavailable: when a social network has no collector or reading fails.
        """
        network = network_of(url)
        if network is Network.WEB:
            return await self._fetcher.fetch(url)
        collector = self._collectors.get(network)
        if collector is None:
            raise SourceUnavailable(
                f"{network.value} content was not read because no actor is configured for it."
            )
        return await collector.collect(url)

    async def _social_source(
        self,
        identity: Identity,
        network: Network,
        known: dict[Network, str],
        skipped: set[Network],
        options: ResearchOptions,
        rules: MatchRules,
        notes: list[str],
        reporter: Reporter,
    ) -> SourceDocument | ClarificationQuestion | None:
        """Find and read the profile of a person on one social network.

        A profile already linked in the identity is read directly. Otherwise the search
        results are shortlisted, read and scored, and the best match is accepted only if
        it clears the threshold and margin.

        Args:
            identity: The person to look for.
            network: The social network to search.
            known: Profile URLs already known per network.
            skipped: Networks the user rejected in a previous clarification.
            options: The research options of the request.
            rules: The threshold and margin for accepting a match.
            notes: The list of limitation notes to append to.
            reporter: The reporter that receives progress events.

        Returns:
            The matched document, a question when several profiles are plausible, or None
            when nothing was read.
        """
        task = NETWORK_TASKS[network]
        if network not in options.networks:
            notes.append(f"{network.value} was switched off in the options and was not read.")
            return None
        if network in skipped:
            notes.append(f"No {network.value} profile was used: all candidates were rejected.")
            return None
        collector = self._collectors.get(network)
        if collector is None:
            notes.append(
                f"{network.value} was not searched because no Apify actor is configured for it."
            )
            return None
        reporter.emit(Kind.STEP, f"Looking for a {network.value} profile", task)
        if network in known:
            try:
                document = await collector.collect(known[network])
            except SourceUnavailable as error:
                notes.append(str(error))
                return None
            self._announce_source(reporter, document)
            return document
        shortlist = await self._shortlist(identity, network)
        if not shortlist:
            notes.append(f"No {network.value} profile was found in the search results.")
            return None
        scoring = self._settings.scoring
        documents: dict[str, SourceDocument] = {}
        candidates: list[Candidate] = []
        for hit in shortlist:
            try:
                document = await collector.collect(hit.url)
            except SourceUnavailable as error:
                notes.append(str(error))
                continue
            documents[hit.url] = document
            candidate = make_candidate(
                identity, hit.url, document.title, hit.snippet, document.text, scoring
            )
            candidates.append(candidate)
            reporter.emit(
                Kind.CANDIDATE,
                f"Possible {network.value} profile: {document.title or hit.url}",
                task,
                network=network.value,
                url=hit.url,
                score=candidate.score.total,
                snippet=hit.snippet,
            )
        decision = decide(candidates, rules.threshold, rules.margin)
        if isinstance(decision, Confirmed):
            chosen = decision.candidate
            document = documents[chosen.url].model_copy(update={"match_score": chosen.score.total})
            self._announce_source(reporter, document)
            return document
        if isinstance(decision, Ambiguous):
            return build_question(identity, network, decision.candidates, scoring)
        notes.append(f"No readable {network.value} profile was found.")
        return None

    async def _shortlist(self, identity: Identity, network: Network) -> list[SearchHit]:
        """Find the most plausible search hits for a person on a network.

        Args:
            identity: The person to look for.
            network: The social network to search.

        Returns:
            The hits whose text mentions the name, best first, capped at the configured
            number of candidates per network.
        """
        scoring = self._settings.scoring
        hits = await self._discovery.find_profiles(identity, network)
        scored = [
            (score_text(identity, hit.url, hit.title, hit.snippet, scoring), hit) for hit in hits
        ]
        plausible = [(score, hit) for score, hit in scored if score.name > 0]
        plausible.sort(key=lambda pair: -pair[0].total)
        limit = self._settings.discovery.candidates_per_network
        return [hit for _, hit in plausible[:limit]]

    async def _web_sources(
        self,
        identity: Identity,
        options: ResearchOptions,
        rules: MatchRules,
        notes: list[str],
        reporter: Reporter,
    ) -> list[SourceDocument]:
        """Collect pages from the open web about a person.

        Links given in the identity are fetched first. Discovered pages are kept only if
        their match score reaches the threshold.

        Args:
            identity: The person to look for.
            options: The research options of the request.
            rules: The threshold and margin for accepting a match.
            notes: The list of limitation notes to append to.
            reporter: The reporter that receives progress events.

        Returns:
            The accepted web documents, possibly empty.
        """
        if Network.WEB not in options.networks:
            notes.append("The open web was switched off in the options and was not searched.")
            return []
        limit = (
            self._settings.discovery.max_web_pages
            if options.max_web_pages is None
            else options.max_web_pages
        )
        provided = [
            normalize_web_url(link) for link in identity.links if network_of(link) is Network.WEB
        ][:limit]
        hits = await self._discovery.find_pages(identity) if limit else []
        discovered = [hit for hit in hits if normalize_web_url(hit.url) not in provided]
        documents: list[SourceDocument] = []
        # Links the user supplied are trusted and skip the match score check applied to discovered
        # pages.
        for link in provided:
            try:
                document = await self._fetcher.fetch(link)
            except SourceUnavailable as error:
                notes.append(str(error))
                continue
            documents.append(document)
            self._announce_source(reporter, document)
        rejected = 0
        scoring = self._settings.scoring
        for hit in discovered[:limit]:
            try:
                document = await self._fetcher.fetch(hit.url)
            except SourceUnavailable as error:
                notes.append(str(error))
                continue
            score = score_text(
                identity, document.url, document.title, f"{hit.snippet} {document.text}", scoring
            )
            if score.total >= rules.threshold:
                kept = document.model_copy(update={"match_score": score.total})
                documents.append(kept)
                self._announce_source(reporter, kept)
            else:
                rejected += 1
        if rejected:
            notes.append(
                f"{rejected} web page(s) were discarded because they did not match the identity."
            )
        if not documents:
            notes.append("No portfolio, personal site, GitHub, publication or talk was confirmed.")
        return documents
