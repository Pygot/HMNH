# src/agent/models.py
from agent.text import (
    city_level,
    clean_text,
    http_url,
    LIMITS,
    sentence,
)
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)
from datetime import (
    datetime,
    UTC,
)
from typing import (
    Annotated,
    Literal,
)
from enum import StrEnum

ShortText = Annotated[str, AfterValidator(clean_text)]
CityName = Annotated[str, AfterValidator(city_level)]
WebUrl = Annotated[str, AfterValidator(http_url)]
Sentence = Annotated[str, AfterValidator(sentence)]


class Goal(StrEnum):
    """The purpose of a research run: hiring, sales or due diligence."""

    HIRING = "hiring"
    SALES = "sales"
    DUE_DILIGENCE = "due_diligence"


class Network(StrEnum):
    """A source network that profiles and pages can come from."""

    LINKEDIN = "linkedin"
    FACEBOOK = "facebook"
    INSTAGRAM = "instagram"
    WEB = "web"


class Tag(StrEnum):
    """The evidence strength label of a finding."""

    SUPPORTED = "supported"
    SINGLE_SOURCE = "single-source"
    UNCERTAIN = "uncertain"


# The kind of fact that a claim or finding states.
class Category(StrEnum):
    EMPLOYMENT = "employment"
    EDUCATION = "education"
    SKILL = "skill"
    PROJECT = "project"
    PUBLICATION = "publication"
    TALK = "talk"
    CERTIFICATION = "certification"
    AWARD = "award"
    PROFILE = "profile"


class Confidence(StrEnum):
    """How sure the extraction is about a claim."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Criterion(StrEnum):
    """A dimension on which a person is rated."""

    SKILL_FIT = "skill_fit"
    EVIDENCE_OF_WORK = "evidence_of_work"
    RECENCY = "recency"
    CONSISTENCY = "consistency"


class Strictness(StrEnum):
    """A preset for how strictly a profile must match the searched identity."""

    LENIENT = "lenient"
    BALANCED = "balanced"
    STRICT = "strict"


class ScepticismMode(StrEnum):
    """How much distrust is applied when rating: standard or strict."""

    STANDARD = "standard"
    STRICT = "strict"


class Severity(StrEnum):
    """How serious a scepticism flag is."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ScepticismLevel(StrEnum):
    """The overall level of scepticism raised against a rating."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class FactKind(StrEnum):
    """The kind of fact that is stated about a company."""

    CATEGORY = "category"
    PRODUCT = "product"
    MARKET = "market"
    LOCATION = "location"
    SIZE = "size"
    MISSION = "mission"
    TECHNOLOGY = "technology"
    PROVIDED = "provided"


class RequirementKind(StrEnum):
    """The kind of a candidate requirement."""

    LANGUAGE = "language"
    SKILL = "skill"
    EXPERIENCE = "experience"
    EDUCATION = "education"
    CERTIFICATION = "certification"
    LOCATION = "location"
    INDUSTRY = "industry"
    CUSTOM = "custom"


class Priority(StrEnum):
    """Whether a requirement is a must have or a nice to have."""

    MUST = "must"
    NICE = "nice"


class RequirementStatus(StrEnum):
    """How well the evidence covers a requirement."""

    MET = "met"
    PARTIAL = "partial"
    UNMET = "unmet"
    UNKNOWN = "unknown"


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime.

    Returns:
        The current moment with UTC as its timezone.
    """
    return datetime.now(UTC)


# The identity of a person to research.
class Identity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ShortText
    aliases: list[ShortText] = Field(default_factory=list, max_length=LIMITS.max_list)
    location: CityName | None = None
    employers: list[ShortText] = Field(default_factory=list, max_length=LIMITS.max_list)
    roles: list[ShortText] = Field(default_factory=list, max_length=LIMITS.max_list)
    skills: list[ShortText] = Field(default_factory=list, max_length=LIMITS.max_list)
    links: list[WebUrl] = Field(default_factory=list, max_length=25)

    @field_validator("aliases", "employers", "roles", "skills", "links", mode="before")
    @classmethod
    def _skip_blank_entries(cls, values: list[str]) -> list[str]:
        """Drop blank entries from a list before it is validated.

        Args:
            values: The raw list given for a list field.

        Returns:
            The list without entries that are empty or only whitespace.

        Raises:
            ValueError: when the value is not a list.
        """
        if not isinstance(values, list):
            raise ValueError("must be a list")
        return [value for value in values if value.strip()]


# Per-run options for sources, profile matching and scepticism.
class ResearchOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    networks: list[Network] = Field(default_factory=lambda: list(Network))
    strictness: Strictness | None = None
    threshold: float | None = Field(default=None, ge=0, le=1)
    margin: float | None = Field(default=None, ge=0, le=1)
    max_web_pages: int | None = Field(default=None, ge=0, le=20)
    scepticism: ScepticismMode = ScepticismMode.STANDARD

    @field_validator("networks")
    @classmethod
    def _at_least_one_source(cls, networks: list[Network]) -> list[Network]:
        """Reject an empty source list and drop duplicate networks.

        Args:
            networks: The chosen networks.

        Returns:
            The networks without duplicates, in their original order.

        Raises:
            ValueError: when no network is chosen.
        """
        if not networks:
            raise ValueError("choose at least one source")
        return list(dict.fromkeys(networks))


# A hiring company as described by the user.
class CompanyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ShortText
    website: WebUrl | None = None
    description: Sentence | None = None


# A single fact about a company with an optional source URL.
class CompanyFact(BaseModel):
    kind: FactKind
    statement: Sentence
    source_url: WebUrl | None = None


# What is known about a company, kept as a list of typed facts.
class CompanyProfile(BaseModel):
    name: str
    website: WebUrl | None = None
    facts: list[CompanyFact] = Field(default_factory=list)

    def statements(self, kind: FactKind) -> list[str]:
        """Return the statements of all facts of one kind.

        Args:
            kind: The kind of fact to select.

        Returns:
            The fact statements of that kind, in stored order.
        """
        return [fact.statement for fact in self.facts if fact.kind is kind]

    @property
    def category(self) -> str | None:
        """Return the first category statement of the company, if any."""
        found = self.statements(FactKind.CATEGORY)
        return found[0] if found else None

    @property
    def products(self) -> list[str]:
        """Return the statements of all product facts of the company."""
        return self.statements(FactKind.PRODUCT)


# A candidate requirement such as a language, skill or experience.
class Requirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: RequirementKind
    label: ShortText
    level: ShortText | None = None
    minimum_years: int | None = Field(default=None, ge=0, le=60)
    priority: Priority = Priority.MUST

    @property
    def description(self) -> str:
        """Return a short readable form of the requirement.

        The label is followed by the level in parentheses and by the minimum years as
        'N+ years' when they are set and not zero.
        """
        parts = [self.label]
        if self.level:
            parts.append(f"({self.level})")
        if self.minimum_years:
            parts.append(f"{self.minimum_years}+ years")
        return " ".join(parts)


class ResearchRequest(BaseModel):
    """A request to research one person for a stated goal.

    Unknown fields are rejected.
    """

    model_config = ConfigDict(extra="forbid")

    goal: Goal
    identity: Identity
    focus: ShortText | None = None
    purpose_confirmed: bool
    skipped_networks: list[Network] = Field(default_factory=list)
    company: CompanyInput | None = None
    requirements: list[Requirement] = Field(default_factory=list, max_length=LIMITS.max_list)
    options: ResearchOptions = Field(default_factory=ResearchOptions)


class SkillSearchRequest(BaseModel):
    """A request to find people with a skill in a city or region.

    Unknown fields are rejected. The result limit is between 1 and 10.
    """

    model_config = ConfigDict(extra="forbid")

    goal: Goal
    skill: ShortText
    location: CityName
    limit: int = Field(default=5, ge=1, le=10)
    purpose_confirmed: bool
    company: CompanyInput | None = None
    requirements: list[Requirement] = Field(default_factory=list, max_length=LIMITS.max_list)
    options: ResearchOptions = Field(default_factory=ResearchOptions)


class SearchHit(BaseModel):
    """A raw web search result with its URL, title and snippet."""

    url: WebUrl
    title: str
    snippet: str


class ScoreBreakdown(BaseModel):
    """How well a page matches an identity, as scores from 0 to 1.

    A component that could not be judged because the identity lacks that input is
    None and is left out of the total.
    """

    name: float = Field(ge=0, le=1)
    employer: float | None = Field(default=None, ge=0, le=1)
    location: float | None = Field(default=None, ge=0, le=1)
    skills: float | None = Field(default=None, ge=0, le=1)
    total: float = Field(ge=0, le=1)


class Candidate(BaseModel):
    """A possible profile page of a person together with its match score."""

    url: WebUrl
    title: str
    snippet: str
    score: ScoreBreakdown


# A page or profile that was retrieved as evidence.
class Source(BaseModel):
    url: WebUrl
    network: Network
    title: str
    retrieved_at: datetime
    match_score: float | None = Field(default=None, ge=0, le=1)


class SourceDocument(Source):
    """A retrieved source together with its full text."""

    text: str

    def as_source(self) -> Source:
        """Return the source without its text.

        Returns:
            A plain Source built from the same fields, excluding the text.
        """
        return Source.model_validate(self.model_dump(exclude={"text"}))


# One statement extracted from a source, with category and confidence.
class Claim(BaseModel):
    statement: Sentence
    category: Category
    confidence: Confidence
    year: int | None = None
    start_year: int | None = None
    source_url: WebUrl


# The claims that were extracted from one source.
class SourceSummary(BaseModel):
    url: WebUrl
    network: Network
    title: str
    subject_name: str | None = None
    claims: list[Claim]
    discarded: int = Field(default=0, ge=0)


# A statement backed by one or more sources, tagged by evidence strength.
class Finding(BaseModel):
    id: str
    statement: Sentence
    category: Category
    tag: Tag
    source_urls: list[WebUrl] = Field(min_length=1)
    independent_sources: int = Field(ge=1)
    conflict: bool = False
    year: int | None = None
    start_year: int | None = None


# A score from 0 to 5 for one rating criterion, with its justification.
class CriterionRating(BaseModel):
    criterion: Criterion
    score: int = Field(ge=0, le=5)
    justification: Sentence
    source_urls: list[WebUrl] = Field(default_factory=list)


# One reason for scepticism about the evidence, with a severity.
class ScepticismFlag(BaseModel):
    code: str
    severity: Severity
    message: str
    finding_ids: list[str] = Field(default_factory=list)


# The scepticism assessment that was applied to a rating.
class Scepticism(BaseModel):
    mode: ScepticismMode
    level: ScepticismLevel
    flags: list[ScepticismFlag] = Field(default_factory=list)
    cap: float | None = None
    guidance: list[str] = Field(default_factory=list)


# A rating per criterion and overall, before and after scepticism.
class Rating(BaseModel):
    target: str
    criteria: list[CriterionRating]
    raw_overall: float = Field(ge=0, le=5)
    overall: float = Field(ge=0, le=5)
    scepticism: Scepticism

    @field_validator("criteria")
    @classmethod
    def _one_of_each(cls, criteria: list[CriterionRating]) -> list[CriterionRating]:
        """Check that every criterion is rated exactly once.

        Args:
            criteria: The criterion ratings of the rating.

        Returns:
            The same list of criterion ratings.

        Raises:
            ValueError: when a criterion is missing or rated more than once.
        """
        if sorted(c.criterion for c in criteria) != sorted(Criterion):
            raise ValueError("exactly one rating per criterion is required")
        return criteria

    def score_of(self, criterion: Criterion) -> int:
        """Return the score given for one criterion.

        Args:
            criterion: The criterion to look up.

        Returns:
            The score from 0 to 5 of that criterion.
        """
        return next(c.score for c in self.criteria if c.criterion == criterion)


# The verdict on one requirement, with justification and evidence.
class RequirementResult(BaseModel):
    id: str
    requirement: Requirement
    status: RequirementStatus
    justification: Sentence
    finding_ids: list[str] = Field(default_factory=list)
    source_urls: list[WebUrl] = Field(default_factory=list)


# The verdicts on all requirements, with counts and overall coverage.
class RequirementsAssessment(BaseModel):
    results: list[RequirementResult]
    coverage: float = Field(ge=0, le=1)
    met: int = Field(ge=0)
    partial: int = Field(ge=0)
    unmet: int = Field(ge=0)
    unknown: int = Field(ge=0)
    must_missing: list[str] = Field(default_factory=list)


class ClarificationOption(BaseModel):
    """One candidate profile that is offered to the user to choose from."""

    url: WebUrl
    title: str
    snippet: str
    score: float
    evidence: str


class ClarificationQuestion(BaseModel):
    """A question asking which profile on a network belongs to the person."""

    network: Network
    text: str
    options: list[ClarificationOption] = Field(min_length=1)


class Clarification(BaseModel):
    """The questions that must be answered before research can continue."""

    questions: list[ClarificationQuestion] = Field(min_length=1)


# The user's answer to a clarification question.
class ClarificationAnswer(BaseModel):
    network: Network
    chosen_url: WebUrl | None = None
    location: CityName | None = None
    employer: ShortText | None = None


# A candidate in a skill search result with its rank, claims and rating.
class RankedCandidate(BaseModel):
    rank: int = Field(ge=1)
    best: bool
    name: str
    profile_url: WebUrl
    network: Network
    claims: list[Claim]
    rating: Rating
    requirements: RequirementsAssessment | None = None


class ReportBase(BaseModel):
    """The fields that all reports share.

    A report can only exist when the lawful purpose was confirmed, and it must list at
    least one limitation.
    """

    generated_at: datetime
    goal: Goal
    notice: str
    # Only True is accepted, so a report cannot be built for research whose lawful purpose was never
    # confirmed.
    purpose_confirmed: Literal[True]
    options: ResearchOptions
    sources: list[Source]
    limitations: list[str] = Field(min_length=1)


# The research report for one person.
class PersonReport(ReportBase):
    mode: Literal["person"] = "person"
    identity: Identity
    focus: str | None
    source_summaries: list[SourceSummary]
    findings: list[Finding]
    rating: Rating
    company: CompanyProfile | None = None
    requirements: RequirementsAssessment | None = None


# The report of a skill search with its ranked candidates.
class SkillSearchReport(ReportBase):
    mode: Literal["skill_search"] = "skill_search"
    skill: str
    location: str
    candidates: list[RankedCandidate]
    company: CompanyProfile | None = None
