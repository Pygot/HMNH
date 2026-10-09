# src/agent/speech.py
from agent.models import (
    Clarification,
    PersonReport,
    ScepticismLevel,
    SkillSearchReport,
    Tag,
)


def describe_report(report: PersonReport | SkillSearchReport, spoken_flags: int) -> str:
    """Turn a report into a short text meant to be read aloud.

    A person report is summarised with its rating, any scepticism reduction and flags,
    and the number of findings and sources. A skill search report is summarised by
    its best candidate.

    Args:
        report: Person report or skill search report to describe.
        spoken_flags: Maximum number of scepticism flag messages to include.

    Returns:
        The spoken summary as one line of plain text.
    """
    if isinstance(report, SkillSearchReport):
        return _describe_ranking(report)
    rating = report.rating
    parts = [
        f"Report on {report.identity.name}.",
        f"Overall rating {rating.overall:.1f} out of 5, rated against {rating.target}.",
    ]
    if rating.raw_overall != rating.overall:
        parts.append(f"It was reduced from {rating.raw_overall:.1f} because of scepticism.")
    if rating.scepticism.level is not ScepticismLevel.NONE:
        parts.append(f"Scepticism is {rating.scepticism.level.value}.")
        parts.extend(flag.message for flag in rating.scepticism.flags[:spoken_flags])
    supported = sum(1 for finding in report.findings if finding.tag is Tag.SUPPORTED)
    parts.append(
        f"{len(report.findings)} findings from {len(report.sources)} sources, "
        f"{supported} supported by two or more sources."
    )
    return " ".join(parts)


def _describe_ranking(report: SkillSearchReport) -> str:
    """Summarise a skill search report as spoken text.

    Args:
        report: Skill search report to describe.

    Returns:
        A sentence naming the search and its best candidate, or saying that no
        candidates could be assessed.
    """
    head = f"Skill search for {report.skill} in {report.location}."
    if not report.candidates:
        return f"{head} No candidates could be assessed."
    best = next((item for item in report.candidates if item.best), report.candidates[0])
    return (
        f"{head} {len(report.candidates)} candidates were assessed. "
        f"The best match is {best.name} with a rating of {best.rating.overall:.1f} out of 5."
    )


def describe_clarification(clarification: Clarification) -> str:
    """Turn clarification questions into text that can be read aloud.

    Each question is followed by its numbered options, and a closing sentence tells
    the listener how to answer.

    Args:
        clarification: Questions and options that need an answer from the user.

    Returns:
        The spoken prompt as one line of plain text.
    """
    parts: list[str] = []
    for question in clarification.questions:
        parts.append(question.text)
        for number, option in enumerate(question.options, 1):
            parts.append(f"Option {number}: {option.title}. {option.snippet}")
    parts.append("Say the option number, say none to skip, or say narrow to add details.")
    return " ".join(parts)
