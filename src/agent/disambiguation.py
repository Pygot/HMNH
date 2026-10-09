# src/agent/disambiguation.py
from agent.models import (
    Candidate,
    ClarificationOption,
    ClarificationQuestion,
    Identity,
    Network,
    ScoreBreakdown,
)
from agent.config import ScoringTuning
from agent.urls import profile_slug
from difflib import SequenceMatcher
from dataclasses import dataclass

import unicodedata
import re

TITLE_SEPARATORS = "".join(chr(code) for code in (0x2013, 0x2014, 0xB7, 0x2022))
TITLE_SPLIT = re.compile(rf"\s[-{TITLE_SEPARATORS}|]\s|\s\(|,")
SEPARATORS = re.compile(r"[\W_]+")
SLUG_SPLIT = re.compile(r"[-_.]+")


@dataclass(frozen=True)
class Confirmed:
    """A decision that one candidate clearly matches the identity."""

    candidate: Candidate


@dataclass(frozen=True)
class Ambiguous:
    """A decision that no candidate won clearly.

    The candidates are kept sorted from best to worst match.
    """

    candidates: list[Candidate]


@dataclass(frozen=True)
class NoCandidates:
    """A decision that the search found no candidates at all."""

    pass


def normalize(text: str) -> str:
    """Return text in a form that is safe to compare.

    Accents are stripped, the text is case-folded and every run of punctuation,
    spaces and underscores becomes a single space.

    Args:
        text: The text to normalize.

    Returns:
        The normalized text, with single spaces between words.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(SEPARATORS.sub(" ", stripped.casefold()).split())


def _token_matches(wanted: str, tokens: list[str], fuzzy_ratio: float) -> bool:
    """Check whether a wanted name token matches any candidate token.

    A token matches when it is equal, when one side is a single letter initial of the
    other, or when both are longer than three characters and similar enough.

    Args:
        wanted: A normalized token of the searched name.
        tokens: The normalized tokens of the candidate name.
        fuzzy_ratio: The minimum similarity ratio from 0 to 1 for a fuzzy match.

    Returns:
        True if any candidate token matches.
    """
    for token in tokens:
        if wanted == token:
            return True
        if len(wanted) == 1 and token.startswith(wanted):
            return True
        if len(token) == 1 and wanted.startswith(token):
            return True
        if (
            min(len(wanted), len(token)) > 3
            and SequenceMatcher(None, wanted, token).ratio() >= fuzzy_ratio
        ):
            return True
    return False


def _name_match(wanted_name: str, candidate_name: str, fuzzy_ratio: float) -> float:
    """Return the share of the wanted name that appears in a candidate name.

    Args:
        wanted_name: The searched name or alias.
        candidate_name: The name found on the candidate page.
        fuzzy_ratio: The minimum similarity ratio from 0 to 1 for a fuzzy token match.

    Returns:
        The fraction from 0 to 1 of wanted name tokens that match, or 0.0 when either
        name has no tokens.
    """
    wanted = normalize(wanted_name).split()
    tokens = normalize(candidate_name).split()
    if not wanted or not tokens:
        return 0.0
    return sum(1 for token in wanted if _token_matches(token, tokens, fuzzy_ratio)) / len(wanted)


def _candidate_names(url: str, title: str) -> list[str]:
    """Return the names a candidate page seems to stand for.

    The first name is the page title up to its first separator. A second name is
    taken from the profile slug of the URL, without a trailing part that contains
    digits.

    Args:
        url: The URL of the candidate page.
        title: The title of the candidate page.

    Returns:
        The candidate name guesses, one or two.
    """
    names = [TITLE_SPLIT.split(title)[0]]
    slug = profile_slug(url)
    if slug:
        parts = SLUG_SPLIT.split(slug)
        if len(parts) > 1 and any(ch.isdigit() for ch in parts[-1]):
            parts = parts[:-1]
        names.append(" ".join(parts))
    return names


def _name_score(
    identity: Identity, url: str, title: str, haystack: str, fuzzy_ratio: float
) -> float:
    """Score how well a candidate matches the searched name or aliases.

    Args:
        identity: The searched identity.
        url: The URL of the candidate page.
        title: The title of the candidate page.
        haystack: The normalized text of the candidate to search in.
        fuzzy_ratio: The minimum similarity ratio from 0 to 1 for a fuzzy token match.

    Returns:
        1.0 when the full name or an alias appears as whole words in the haystack,
        otherwise the best token level match from 0 to 1.
    """
    padded = f" {haystack} "
    candidate_names = _candidate_names(url, title)
    best = 0.0
    for wanted in (identity.name, *identity.aliases):
        phrase = normalize(wanted)
        if phrase and f" {phrase} " in padded:
            return 1.0
        for candidate_name in candidate_names:
            best = max(best, _name_match(wanted, candidate_name, fuzzy_ratio))
    return best


def _mentions(padded_haystack: str, term: str) -> bool:
    """Check whether a term appears as whole words in a text.

    Args:
        padded_haystack: Normalized text with one space added on each side.
        term: The term to look for, before normalization.

    Returns:
        True if the normalized term is non-empty and found as whole words.
    """
    normalized = normalize(term)
    return bool(normalized) and f" {normalized} " in padded_haystack


def score_text(
    identity: Identity, url: str, title: str, text: str, tuning: ScoringTuning
) -> ScoreBreakdown:
    """Score how well a page matches an identity.

    The name is always judged. Employer, location and skills are judged only when the
    identity provides them, and the total is the weighted mean of the judged parts.

    Args:
        identity: The searched identity.
        url: The URL of the page.
        title: The title of the page.
        text: The text of the page.
        tuning: The scoring weights and fuzzy matching ratio.

    Returns:
        The component scores and the total, all from 0 to 1.
    """
    haystack = normalize(f"{title} {text}")
    padded = f" {haystack} "
    name = _name_score(identity, url, title, haystack, tuning.fuzzy_token_ratio)
    employer = (
        float(any(_mentions(padded, employer) for employer in identity.employers))
        if identity.employers
        else None
    )
    location = float(_mentions(padded, identity.location)) if identity.location else None
    terms = [*identity.skills, *identity.roles]
    skills = (
        # Three matching terms already give a full skills score, so long skill lists are not
        # penalised.
        min(1.0, sum(_mentions(padded, term) for term in terms) / min(3, len(terms)))
        if terms
        else None
    )
    weights = {
        "name": tuning.name_weight,
        "employer": tuning.employer_weight,
        "location": tuning.location_weight,
        "skills": tuning.skills_weight,
    }
    parts = {"name": name, "employer": employer, "location": location, "skills": skills}
    available = {key: value for key, value in parts.items() if value is not None}
    # Parts without input are dropped and the remaining weights are renormalised, so missing data
    # does not lower the total.
    total = sum(weights[key] * value for key, value in available.items()) / sum(
        weights[key] for key in available
    )
    return ScoreBreakdown(
        name=name, employer=employer, location=location, skills=skills, total=round(total, 4)
    )


def make_candidate(
    identity: Identity, url: str, title: str, snippet: str, text: str, tuning: ScoringTuning
) -> Candidate:
    """Create a scored candidate from a search result and its text.

    Args:
        identity: The searched identity.
        url: The URL of the candidate page.
        title: The title of the candidate page.
        snippet: The search result snippet, also used for scoring.
        text: The page text, also used for scoring.
        tuning: The scoring weights and fuzzy matching ratio.

    Returns:
        The candidate with its score breakdown.
    """
    return Candidate(
        url=url,
        title=title,
        snippet=snippet,
        score=score_text(identity, url, title, f"{snippet} {text}", tuning),
    )


def describe_score(score: ScoreBreakdown) -> str:
    """Return a short text that explains the parts of a score.

    Parts that were not judged are left out. A result looks like 'name 100%, skills 33%'.

    Args:
        score: The score breakdown to describe.

    Returns:
        The judged parts as percentages, separated by commas.
    """
    parts = {
        "name": score.name,
        "employer": score.employer,
        "location": score.location,
        "skills": score.skills,
    }
    return ", ".join(f"{label} {value:.0%}" for label, value in parts.items() if value is not None)


def rank(candidates: list[Candidate]) -> list[Candidate]:
    """Return the candidates sorted from best to worst match.

    Candidates with equal totals are ordered by URL, so the order is deterministic.

    Args:
        candidates: The candidates to sort.

    Returns:
        A new sorted list.
    """
    return sorted(candidates, key=lambda candidate: (-candidate.score.total, candidate.url))


def decide(
    candidates: list[Candidate], threshold: float, margin: float
) -> Confirmed | Ambiguous | NoCandidates:
    """Decide whether one candidate is clearly the right one.

    The top candidate is confirmed only when its total reaches the threshold and it
    leads the runner up by at least the margin. Otherwise the decision is ambiguous.

    Args:
        candidates: The candidates found on one network.
        threshold: The minimum total score of the top candidate.
        margin: The minimum lead of the top candidate over the second one.

    Returns:
        Confirmed with the top candidate, Ambiguous with all candidates ranked, or
        NoCandidates when the list is empty.
    """
    ranked = rank(candidates)
    if not ranked:
        return NoCandidates()
    top = ranked[0]
    if top.score.total < threshold:
        return Ambiguous(ranked)
    if len(ranked) > 1 and top.score.total - ranked[1].score.total < margin:
        return Ambiguous(ranked)
    return Confirmed(top)


def build_question(
    identity: Identity, network: Network, candidates: list[Candidate], tuning: ScoringTuning
) -> ClarificationQuestion:
    """Create the question that asks the user to pick the right profile.

    Only the best ranked candidates are offered, up to the configured maximum, each
    with an explanation of its score.

    Args:
        identity: The searched identity.
        network: The network the candidates were found on.
        candidates: The candidates to choose from.
        tuning: The scoring settings that limit the number of options.

    Returns:
        The clarification question with its options.
    """
    options = [
        ClarificationOption(
            url=candidate.url,
            title=candidate.title,
            snippet=candidate.snippet,
            score=candidate.score.total,
            evidence=describe_score(candidate.score),
        )
        for candidate in rank(candidates)[: tuning.max_options]
    ]
    text = (
        f"Which {network.value} profile belongs to {identity.name}? "
        "No candidate was clearly better than the others or confident enough. "
        "Choose one, reject all of them, or add a city or employer to narrow the search."
    )
    return ClarificationQuestion(network=network, text=text, options=options)
