# src/agent/scepticism.py
from agent.models import (
    Category,
    Finding,
    Scepticism,
    ScepticismFlag,
    ScepticismLevel,
    ScepticismMode,
    Severity,
    Tag,
)
from agent.config import (
    ScepticismProfile,
    ScepticismTuning,
)
from collections.abc import Sequence
from agent.urls import source_key

SEVERITY_ORDER = {Severity.LOW: 1, Severity.MEDIUM: 2, Severity.HIGH: 3}
LEVEL_OF = {
    Severity.LOW: ScepticismLevel.LOW,
    Severity.MEDIUM: ScepticismLevel.MEDIUM,
    Severity.HIGH: ScepticismLevel.HIGH,
}
GUIDANCE = {
    "uncorroborated_excellence": (
        "Treat the rating as unproven. Verify the strongest claims with references or the "
        "issuing organisations before relying on them."
    ),
    "thin_sources": (
        "Everything comes from one source that the person may control. Look for independent "
        "evidence such as public repositories, publications or references."
    ),
    "prestige_density": (
        "Ask for proof of the awards and certifications, such as certificate numbers or "
        "verification links from the issuer."
    ),
    "hype_language": (
        "The wording is promotional. Ask for concrete results, numbers and people who can "
        "confirm them."
    ),
    "overlapping_roles": (
        "Several roles overlap in time. Confirm the dates and the real scope of each role "
        "with the employers."
    ),
    "career_span": (
        "The career span is unusually long for the stated dates. Check the dates against "
        "other sources."
    ),
    "skill_inflation": (
        "A very long skill list says little. Test the skills that matter for this goal in a "
        "practical way."
    ),
}


def _flag(
    code: str, severity: Severity, message: str, findings: Sequence[Finding]
) -> ScepticismFlag:
    """Build a scepticism flag that points at the findings behind it.

    Args:
        code: the short code of the check, also used to look up guidance.
        severity: how serious the concern is.
        message: the human readable explanation.
        findings: the findings that triggered the flag.

    Returns:
        The flag, holding the ids of the given findings.
    """
    return ScepticismFlag(
        code=code, severity=severity, message=message, finding_ids=[f.id for f in findings]
    )


def _uncorroborated_excellence(
    findings: Sequence[Finding], raw_overall: float, profile: ScepticismProfile
) -> ScepticismFlag | None:
    """Flag a very high rating that few findings support.

    The findings must not be empty, because the supported share is a ratio.

    Args:
        findings: all findings of the candidate.
        raw_overall: the overall rating before any cap.
        profile: the thresholds of the active scepticism mode.

    Returns:
        A high severity flag over the unsupported findings, or None.
    """
    supported = [f for f in findings if f.tag is Tag.SUPPORTED]
    share = len(supported) / len(findings)
    if raw_overall < profile.too_good_score or share >= profile.min_corroborated_share:
        return None
    return _flag(
        "uncorroborated_excellence",
        Severity.HIGH,
        f"The overall rating of {raw_overall:.1f} is very high, but only {len(supported)} of "
        f"{len(findings)} findings are backed by two independent sources.",
        [f for f in findings if f.tag is not Tag.SUPPORTED],
    )


def _thin_sources(findings: Sequence[Finding], profile: ScepticismProfile) -> ScepticismFlag | None:
    """Flag a candidate whose findings all come from one independent source.

    Args:
        findings: all findings of the candidate.
        profile: the thresholds of the active scepticism mode.

    Returns:
        A medium severity flag, or None when there are too few findings or more
        than one source.
    """
    keys = {source_key(url) for f in findings for url in f.source_urls}
    if len(findings) < profile.thin_sources_min_findings or len(keys) != 1:
        return None
    return _flag(
        "thin_sources",
        Severity.MEDIUM,
        f"All {len(findings)} findings come from a single independent source.",
        findings,
    )


def _prestige_density(
    findings: Sequence[Finding], profile: ScepticismProfile
) -> ScepticismFlag | None:
    """Flag many awards or certifications that are mostly uncorroborated.

    Args:
        findings: all findings of the candidate.
        profile: the thresholds of the active scepticism mode.

    Returns:
        A medium severity flag over the prestige findings, or None.
    """
    prestige = [f for f in findings if f.category in {Category.AWARD, Category.CERTIFICATION}]
    verified = [f for f in prestige if f.tag is Tag.SUPPORTED]
    if len(prestige) < profile.max_prestige_claims or len(verified) * 2 >= len(prestige):
        return None
    return _flag(
        "prestige_density",
        Severity.MEDIUM,
        f"{len(prestige)} awards or certifications are claimed and fewer than half are "
        "corroborated.",
        prestige,
    )


def _hype_language(
    findings: Sequence[Finding], profile: ScepticismProfile, hype_words: Sequence[str]
) -> ScepticismFlag | None:
    """Flag findings worded with promotional language.

    Args:
        findings: all findings of the candidate.
        profile: the thresholds of the active scepticism mode.
        hype_words: lower case words that mark promotional wording.

    Returns:
        A medium severity flag over the matching findings, or None.
    """
    hyped = [f for f in findings if any(word in f.statement.lower() for word in hype_words)]
    if len(hyped) < profile.min_hype_findings:
        return None
    return _flag(
        "hype_language",
        Severity.MEDIUM,
        f"{len(hyped)} findings use promotional wording instead of concrete facts.",
        hyped,
    )


def _employment_spans(findings: Sequence[Finding]) -> list[tuple[Finding, int, int]]:
    """Return the start and end years of employment findings that have a start.

    A finding without an end year is treated as lasting a single year.

    Args:
        findings: all findings of the candidate.

    Returns:
        Tuples of finding, start year and end year.
    """
    spans = []
    for finding in findings:
        if finding.category is Category.EMPLOYMENT and finding.start_year is not None:
            end = finding.year if finding.year is not None else finding.start_year
            spans.append((finding, finding.start_year, max(end, finding.start_year)))
    return spans


def _overlapping_roles(
    findings: Sequence[Finding], profile: ScepticismProfile
) -> ScepticismFlag | None:
    """Flag too many employment roles that overlap in time.

    Args:
        findings: all findings of the candidate.
        profile: the thresholds of the active scepticism mode.

    Returns:
        A high severity flag over the employment findings, or None.
    """
    spans = _employment_spans(findings)
    peak = 0
    for _, start, _ in spans:
        # Counts the roles active on each start year, including the role itself.
        peak = max(peak, sum(1 for _, other_start, end in spans if other_start <= start <= end))
    if peak < profile.max_concurrent_roles:
        return None
    return _flag(
        "overlapping_roles",
        Severity.HIGH,
        f"Up to {peak} employment roles overlap in time.",
        [finding for finding, _, _ in spans],
    )


def _career_span(findings: Sequence[Finding], profile: ScepticismProfile) -> ScepticismFlag | None:
    """Flag an employment history that spans an unusually long time.

    Args:
        findings: all findings of the candidate.
        profile: the thresholds of the active scepticism mode.

    Returns:
        A medium severity flag over the employment findings, or None.
    """
    spans = _employment_spans(findings)
    if not spans:
        return None
    first = min(start for _, start, _ in spans)
    last = max(end for _, _, end in spans)
    if last - first <= profile.max_career_years:
        return None
    return _flag(
        "career_span",
        Severity.MEDIUM,
        f"The employment history spans {last - first} years, from {first} to {last}.",
        [finding for finding, _, _ in spans],
    )


def _skill_inflation(
    findings: Sequence[Finding], profile: ScepticismProfile
) -> ScepticismFlag | None:
    """Flag a very long list of claimed skills.

    Args:
        findings: all findings of the candidate.
        profile: the thresholds of the active scepticism mode.

    Returns:
        A low severity flag over the skill findings, or None.
    """
    skills = [f for f in findings if f.category is Category.SKILL]
    if len(skills) < profile.max_skill_claims:
        return None
    return _flag(
        "skill_inflation",
        Severity.LOW,
        f"{len(skills)} separate skills are claimed.",
        skills,
    )


def assess(
    findings: Sequence[Finding],
    raw_overall: float,
    mode: ScepticismMode,
    tuning: ScepticismTuning,
    corroboration_expected: bool,
) -> Scepticism:
    """Run the scepticism checks and summarise them with a score cap.

    The corroboration based checks run only when corroboration is expected. The
    level and cap follow the most severe flag found.

    Args:
        findings: all findings of the candidate.
        raw_overall: the overall rating before any cap.
        mode: the scepticism mode, which picks the standard or strict thresholds.
        tuning: the scepticism settings for both modes.
        corroboration_expected: whether independent sources are expected to exist.

    Returns:
        The assessment with sorted flags, level, cap and unique guidance texts.
    """
    profile = tuning.strict if mode is ScepticismMode.STRICT else tuning.standard
    if not findings:
        return Scepticism(mode=mode, level=ScepticismLevel.NONE)
    checks = [
        _prestige_density(findings, profile),
        _hype_language(findings, profile, tuning.hype_words),
        _overlapping_roles(findings, profile),
        _career_span(findings, profile),
        _skill_inflation(findings, profile),
    ]
    # Skip source based checks when independent sources are not expected to exist.
    if corroboration_expected:
        checks.extend(
            [
                _uncorroborated_excellence(findings, raw_overall, profile),
                _thin_sources(findings, profile),
            ]
        )
    flags = sorted(
        (flag for flag in checks if flag is not None),
        key=lambda flag: -SEVERITY_ORDER[flag.severity],
    )
    if not flags:
        return Scepticism(mode=mode, level=ScepticismLevel.NONE)
    worst = flags[0].severity
    caps = {
        Severity.LOW: profile.cap_low,
        Severity.MEDIUM: profile.cap_medium,
        Severity.HIGH: profile.cap_high,
    }
    return Scepticism(
        mode=mode,
        level=LEVEL_OF[worst],
        flags=flags,
        cap=caps[worst],
        guidance=list(dict.fromkeys(GUIDANCE[flag.code] for flag in flags)),
    )


def capped(raw_overall: float, scepticism: Scepticism) -> float:
    """Return the overall rating limited by the scepticism cap.

    Args:
        raw_overall: the overall rating before any cap.
        scepticism: the assessment, whose cap may be None.

    Returns:
        The rating, or the cap when the rating is above it.
    """
    return raw_overall if scepticism.cap is None else min(raw_overall, scepticism.cap)
