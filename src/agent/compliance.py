# src/agent/compliance.py
from agent.models import (
    CompanyInput,
    Requirement,
    ResearchRequest,
    SkillSearchRequest,
)
from agent.errors import ComplianceRefusal
from collections.abc import Iterable
from agent.urls import hostname

import re

NOTICE = (
    "This tool collects only public, professional information about the named person from "
    "search results and the data providers you configured. You must have a lawful purpose for "
    "the research (for example a legitimate interest under GDPR Art. 6(1)(f)). Where the law "
    "requires it (for example GDPR Art. 14), the person must be informed that their data is "
    "being processed. The output is decision support, not a decision: a human must review it "
    "before anything is acted upon. Requests to stalk, harass or locate private individuals "
    "are refused."
)
STORAGE_NOTICE = (
    "Chats, reports and the context you give are saved in a database on this machine so you "
    "never have to repeat yourself. Nothing is uploaded, reports about people are removed "
    "automatically after the retention period, and you can delete everything in Settings at "
    "any time."
)
VOICE_NOTICE = (
    "Voice features send your recording, or the text of a spoken summary, to ElevenLabs for "
    "processing. This application stores neither the audio nor the transcript."
)

DATA_BROKER_DOMAINS = frozenset(
    {
        "192.com",
        "beenverified.com",
        "fastpeoplesearch.com",
        "intelius.com",
        "mylife.com",
        "peoplefinders.com",
        "pipl.com",
        "radaris.com",
        "spokeo.com",
        "truepeoplesearch.com",
        "whitepages.com",
    }
)

_REFUSAL_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(home|house|residential|private|current|street)\s+address\b",
        r"\bwhere\s+(does|do|is)\b.{0,40}\blive[sd]?\b",
        r"\bwhereabouts\b",
        r"\bstalk\w*",
        r"\bharass\w*",
        r"\bdox+(ing|ed)?\b",
        r"\bex[- ]?(girlfriend|boyfriend|wife|husband|partner)\b",
        r"\b(track|trace|hunt)\s+(her|him|them)\s+down\b",
        r"\bfollow\s+(her|him|them)\b",
        r"\brevenge\b",
        r"\bphone\s+number\b",
        r"\bdomácí\s+adres\w*",
        r"\bbydlišt\w*",
        r"\bkde\s+bydlí\b",
        r"\bsledovat\b",
        r"\bobtěžov\w*",
    )
)

_SENSITIVE_PATTERNS = {
    "health": re.compile(
        r"\b(health\s+(condition|issue|problem|status)|medical\s+(condition|history|leave)"
        r"|diagnos(ed|is)|suffers?\s+from|suffering\s+from|cancer|diabetes|depression"
        r"|disabilit(y|ies)|pregnan(t|cy)|illness|chronic\s+(disease|condition)"
        r"|mental\s+(health|illness)|hiv)\b",
        re.IGNORECASE,
    ),
    "religion": re.compile(
        r"\b(religio\w*|church|mosque|synagogue|muslim|jewish|buddhist|hindu|atheis\w*"
        r"|christianity)\b",
        re.IGNORECASE,
    ),
    "politics": re.compile(
        r"\b(political\s+(party|affiliation|views?|opinions?|beliefs?|activity)"
        r"|votes?\s+for|voted\s+for|party\s+member|left-wing|right-wing|partisan)\b",
        re.IGNORECASE,
    ),
    "orientation": re.compile(
        r"\b(sexual\s+orientation|gay|lesbian|bisexual|homosexual|transgender|lgbt\w*|queer"
        r"|heterosexual)\b",
        re.IGNORECASE,
    ),
    "ethnicity": re.compile(
        r"\b(ethnic\w*|racial|skin\s+colou?r|caucasian|african[- ]american|hispanic|latino"
        r"|latina)\b",
        re.IGNORECASE,
    ),
    "family": re.compile(
        r"\b(married|marital|spouse|wife|husband|girlfriend|boyfriend|divorc\w*"
        r"|has\s+(\d+\s+|two\s+|three\s+)?(kids|children)"
        r"|(his|her)\s+(son|daughter|mother|father|wife|husband))\b",
        re.IGNORECASE,
    ),
    "finances": re.compile(
        r"\b(net\s+worth|personal\s+(debt|finances|income)|salary\s+(of|is|was)|earns?\s+\$?\d"
        r"|bankrupt(cy)?|credit\s+score|mortgage|owes)\b",
        re.IGNORECASE,
    ),
}

_BIASED_REQUIREMENT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bnative[- ]speakers?\b",
        r"\bmother[- ]tongue\b",
        r"\b(age|aged|years[- ]old|young|youthful|elderly)\b",
        r"\b(male|female|man|woman|gender|sex)\b",
        r"\b(nationality|citizen\w*|work[- ]permit)\b",
        r"\b(marital|married|pregnan\w*)\b",
    )
)

_CONTACT_PATTERNS = {
    "email address": re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    "phone number": re.compile(
        r"\+\d[\d ()-]{7,}\d|\(\d{2,4}\)[\d ]{5,}|\b\d{3}[ -]\d{3}[ -]\d{3,4}\b"
    ),
    "street address": re.compile(
        r"\b\d{1,5}\s+(?:[A-Z][\w.'-]*\s+){1,3}"
        r"(?:Street|St|Road|Rd|Avenue|Ave|Boulevard|Blvd|Lane|Ln|Drive|Dr|Way)\b"
        r"|\b[A-ZÀ-Ž][\w.'-]+\s+\d{1,4}(?:/\d{1,4})?,\s*\d{3}\s?\d{2}\b"
    ),
}


def require_lawful_purpose(confirmed: bool) -> None:
    """Check that the user confirmed a lawful purpose for the research.

    Args:
        confirmed: whether the user ticked the lawful purpose confirmation.

    Raises:
        ComplianceRefusal: when the purpose was not confirmed.
    """
    if not confirmed:
        raise ComplianceRefusal(
            "A lawful purpose must be confirmed before any research is performed."
        )


def refuse_harmful_requests(texts: Iterable[str | None]) -> None:
    """Refuse text that looks like stalking, harassment or locating a person.

    Each text is matched against refusal patterns in English and Czech, such as
    home address, whereabouts, doxxing and phone number requests.

    Args:
        texts: the strings to scan; None and empty values are skipped.

    Raises:
        ComplianceRefusal: when any text matches a refusal pattern.
    """
    for text in texts:
        if not text:
            continue
        for pattern in _REFUSAL_PATTERNS:
            if pattern.search(text):
                raise ComplianceRefusal(
                    "Refused: the request looks like an attempt to stalk, harass or locate a "
                    "private individual. This tool only researches public professional "
                    "information."
                )


def rejection_reason(statement: str) -> str | None:
    """Explain why a statement must not be kept, if it must not.

    A statement is rejected when it mentions a sensitive personal attribute or
    contains a private contact detail.

    Args:
        statement: the claim or finding text to check.

    Returns:
        A short reason such as a sensitive attribute or contact detail with its
        category, or None when the statement is acceptable.
    """
    for reason, pattern in _SENSITIVE_PATTERNS.items():
        if pattern.search(statement):
            return f"sensitive attribute ({reason})"
    for reason, pattern in _CONTACT_PATTERNS.items():
        if pattern.search(statement):
            return f"contact detail ({reason})"
    return None


def is_data_broker(url: str) -> bool:
    """Check whether a URL belongs to a people-search data broker.

    Args:
        url: the address to check.

    Returns:
        True when the host is a listed broker domain or one of its subdomains.
    """
    host = hostname(url)
    # Match the dot-prefixed suffix so lookalike hosts such as notspokeo.com are not treated as
    # brokers.
    return any(host == domain or host.endswith(f".{domain}") for domain in DATA_BROKER_DOMAINS)


def check_requirements(requirements: Iterable[Requirement]) -> None:
    """Refuse requirements that concern protected or sensitive attributes.

    The label and level of each requirement are matched against the sensitive
    attribute patterns and the biased requirement patterns, such as age, gender,
    nationality or native speaker.

    Args:
        requirements: the requirements of a research request.

    Raises:
        ComplianceRefusal: when a requirement matches, naming its label.
    """
    patterns = (*_SENSITIVE_PATTERNS.values(), *_BIASED_REQUIREMENT_PATTERNS)
    for requirement in requirements:
        text = f"{requirement.label} {requirement.level or ''}"
        if any(pattern.search(text) for pattern in patterns):
            raise ComplianceRefusal(
                f"Refused: the requirement '{requirement.label}' concerns a protected or "
                "sensitive attribute. Use job related requirements such as skills, languages as "
                "CEFR levels (A1 to C2), experience, education or certifications."
            )


def check_company(company: CompanyInput | None) -> None:
    """Refuse a company description that looks like a harmful request.

    Args:
        company: the company details, or None when none were given.

    Raises:
        ComplianceRefusal: when the name or description matches a refusal pattern.
    """
    if company is not None:
        refuse_harmful_requests([company.name, company.description])


def check_research_request(request: ResearchRequest) -> None:
    """Check a person research request before any work starts.

    The lawful purpose must be confirmed, and the identity fields, focus, company
    and requirements must pass the harmful request and requirement checks.

    Args:
        request: the research request to check.

    Raises:
        ComplianceRefusal: when the purpose is unconfirmed or any check fails.
    """
    require_lawful_purpose(request.purpose_confirmed)
    identity = request.identity
    refuse_harmful_requests(
        [
            identity.name,
            identity.location,
            request.focus,
            *identity.aliases,
            *identity.employers,
            *identity.roles,
            *identity.skills,
        ]
    )
    check_company(request.company)
    check_requirements(request.requirements)


def check_skill_request(request: SkillSearchRequest) -> None:
    """Check a skill search request before any work starts.

    The lawful purpose must be confirmed, and the skill, location, company and
    requirements must pass the harmful request and requirement checks.

    Args:
        request: the skill search request to check.

    Raises:
        ComplianceRefusal: when the purpose is unconfirmed or any check fails.
    """
    require_lawful_purpose(request.purpose_confirmed)
    refuse_harmful_requests([request.skill, request.location])
    check_company(request.company)
    check_requirements(request.requirements)
