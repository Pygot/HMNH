# src/agent/export.py
from agent.models import (
    Category,
    Claim,
    CompanyProfile,
    PersonReport,
    RankedCandidate,
    Rating,
    RequirementsAssessment,
    Scepticism,
    ScepticismLevel,
    SkillSearchReport,
)
from dataclasses import dataclass
from pathlib import Path

import secrets
import re

Report = PersonReport | SkillSearchReport
OUTPUT_NAME = re.compile(r"^report-\d{8}T\d{6}Z-[0-9a-f]{8}\.(md|json)$")
ESCAPED = re.compile(r"([\\`*_\[\]<>|&~])")


@dataclass(frozen=True)
class ExportedReport:
    """The Markdown and JSON files written for one exported report."""

    markdown: Path
    json: Path


def md_text(text: str) -> str:
    """Escape Markdown special characters in text.

    A backslash is put before backslash, backtick, asterisk, underscore, brackets, angle
    brackets, pipe, ampersand and tilde.

    Args:
        text: Untrusted or plain text to place in a Markdown document.

    Returns:
        The escaped text.
    """
    return ESCAPED.sub(r"\\\1", text)


def md_url(url: str) -> str:
    # Angle brackets inside the URL are percent-encoded so they cannot end the link early.
    """Wrap a URL in angle brackets for use in Markdown.

    Args:
        url: The URL to wrap.

    Returns:
        The URL in angle brackets, with any angle brackets inside it percent-encoded.
    """
    # Angle brackets inside the URL are percent-encoded so they cannot end the link early.
    return f"<{url.replace('<', '%3C').replace('>', '%3E')}>"


def _timestamp(report: Report) -> str:
    """Format the generation time of a report as UTC text.

    Args:
        report: The report.

    Returns:
        The time as year-month-day hour:minute followed by UTC.
    """
    return report.generated_at.strftime("%Y-%m-%d %H:%M UTC")


def _urls(urls: list[str]) -> str:
    """Join URLs into one comma separated Markdown string.

    Args:
        urls: The URLs to format.

    Returns:
        The bracketed URLs separated by commas.
    """
    return ", ".join(md_url(url) for url in urls)


def _scepticism_lines(scepticism: Scepticism) -> list[str]:
    """Build the Markdown lines for a scepticism review.

    Args:
        scepticism: The scepticism result of a rating.

    Returns:
        A single line when no flags were raised, otherwise the level, the flags and the
        follow-up guidance.
    """
    if scepticism.level is ScepticismLevel.NONE:
        return [f"No scepticism flags were raised (mode: {scepticism.mode.value})."]
    cap = f", rating capped at {scepticism.cap:.1f}" if scepticism.cap is not None else ""
    lines = [f"Level: **{scepticism.level.value}** (mode: {scepticism.mode.value}{cap})", ""]
    for flag in scepticism.flags:
        ids = f" Findings: {', '.join(flag.finding_ids)}." if flag.finding_ids else ""
        lines.append(f"- **{flag.severity.value}** {md_text(flag.message)}{ids}")
    lines += ["", "What to do next:", ""]
    lines += [f"- {md_text(item)}" for item in scepticism.guidance]
    return lines


def _rating_lines(rating: Rating) -> list[str]:
    """Build the Markdown lines for a rating and its criteria table.

    Args:
        rating: The rating to describe.

    Returns:
        The overall score, the pre-cap score when it differs, and a table of criteria.
    """
    lines = [f"Overall: **{rating.overall:.1f} / 5** (target: {md_text(rating.target)})"]
    if rating.raw_overall != rating.overall:
        lines.append(f"Before the scepticism cap: {rating.raw_overall:.1f} / 5")
    lines += [
        "",
        "| Criterion | Score | Justification | Sources |",
        "| --- | --- | --- | --- |",
    ]
    for item in rating.criteria:
        label = item.criterion.value.replace("_", " ")
        sources = _urls(item.source_urls) if item.source_urls else "-"
        lines.append(f"| {label} | {item.score} / 5 | {md_text(item.justification)} | {sources} |")
    return lines


def _company_lines(profile: CompanyProfile | None) -> list[str]:
    """Build the Markdown section about the hiring company.

    Args:
        profile: The company profile, or None.

    Returns:
        The section lines, or an empty list when there is no profile.
    """
    if profile is None:
        return []
    site = f" - {md_url(profile.website)}" if profile.website else ""
    lines = ["## Hiring company", "", f"{md_text(profile.name)}{site}", ""]
    for fact in profile.facts:
        origin = md_url(fact.source_url) if fact.source_url else "provided by the requester"
        lines.append(f"- {fact.kind.value}: {md_text(fact.statement)} - {origin}")
    if not profile.facts:
        lines.append("- No public facts could be confirmed.")
    return [*lines, ""]


def _requirement_lines(assessment: RequirementsAssessment | None) -> list[str]:
    """Build the Markdown section about requirement coverage.

    Args:
        assessment: The requirements assessment, or None.

    Returns:
        The section lines with a coverage summary and a results table, or an empty list
        when there is no assessment.
    """
    if assessment is None:
        return []
    lines = [
        "## Requirements",
        "",
        f"Coverage: **{assessment.coverage * 100:.0f}%** (met {assessment.met}, partial "
        f"{assessment.partial}, unmet {assessment.unmet}, unknown {assessment.unknown}).",
    ]
    if assessment.must_missing:
        lines.append(
            f"Must-have requirements without evidence: {', '.join(assessment.must_missing)}."
        )
    lines += [
        "",
        "| Id | Requirement | Priority | Status | Justification | Sources |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in assessment.results:
        sources = _urls(item.source_urls) if item.source_urls else "-"
        lines.append(
            f"| {item.id} | {md_text(item.requirement.description)} | "
            f"{item.requirement.priority.value} | {item.status.value} | "
            f"{md_text(item.justification)} | {sources} |"
        )
    return [*lines, ""]


def _claim_lines(claims: list[Claim]) -> list[str]:
    """Build Markdown bullet lines for claims extracted from a source.

    Args:
        claims: The claims to list.

    Returns:
        One bullet per claim, or a single bullet saying none could be extracted.
    """
    lines = []
    for claim in claims:
        year = f", {claim.year}" if claim.year is not None else ""
        lines.append(
            f"- {md_text(claim.statement)} ({claim.category.value}, {claim.confidence.value}"
            f"{year}) - {md_url(claim.source_url)}"
        )
    return lines or ["- No claims could be extracted from this source."]


def _header(report: Report, title: str) -> list[str]:
    """Build the title and summary lines at the top of a report.

    Args:
        report: The report.
        title: The already-escaped document title.

    Returns:
        The heading, goal, time, enabled sources, scepticism mode and the notice.
    """
    networks = ", ".join(network.value for network in report.options.networks)
    return [
        f"# {title}",
        "",
        f"- Goal: {report.goal.value.replace('_', ' ')}",
        f"- Generated: {_timestamp(report)}",
        "- Lawful purpose confirmed by the requester: yes",
        f"- Sources enabled: {networks}",
        f"- Scepticism mode: {report.options.scepticism.value}",
        "",
        f"> {md_text(report.notice)}",
        "",
    ]


def _tail(report: Report) -> list[str]:
    """Build the closing sources and limitations sections.

    Args:
        report: The report.

    Returns:
        A numbered list of sources, or a note that none were read, then the limitations.
    """
    lines = ["## Sources", ""]
    if report.sources:
        for number, source in enumerate(report.sources, 1):
            match = f", match {source.match_score:.2f}" if source.match_score is not None else ""
            lines.append(
                f"{number}. {md_text(source.title)} - {md_url(source.url)} "
                f"({source.network.value}{match}, retrieved {source.retrieved_at:%Y-%m-%d})"
            )
    else:
        lines.append("No sources were read.")
    lines += ["", "## Limitations", ""]
    lines += [f"- {md_text(item)}" for item in report.limitations]
    return lines


def _person_markdown(report: PersonReport) -> list[str]:
    """Build the full Markdown lines for a person report.

    Args:
        report: The person report.

    Returns:
        Identity details, company, rating, requirements, scepticism review, findings by
        category, source summaries, sources and limitations.
    """
    identity = report.identity
    lines = _header(report, f"Research report: {md_text(identity.name)}")
    details = [
        ("Aliases", identity.aliases),
        ("Location", [identity.location] if identity.location else []),
        ("Employers", identity.employers),
        ("Roles", identity.roles),
        ("Skills", identity.skills),
    ]
    lines += [f"- {label}: {md_text(', '.join(v))}" for label, v in details if v]
    if report.focus:
        lines.append(f"- Focus: {md_text(report.focus)}")
    lines += [""]
    lines += _company_lines(report.company)
    lines += ["## Rating", "", *_rating_lines(report.rating)]
    lines += [""]
    lines += _requirement_lines(report.requirements)
    lines += ["## Scepticism review", "", *_scepticism_lines(report.rating.scepticism)]
    lines += ["", "## Findings", ""]
    if not report.findings:
        lines.append("No findings could be confirmed.")
    for category in Category:
        group = [f for f in report.findings if f.category is category]
        if not group:
            continue
        lines += [f"### {category.value.capitalize()}", ""]
        for finding in group:
            year = f" ({finding.year})" if finding.year is not None else ""
            lines.append(
                f"- **{finding.tag.value}** {md_text(finding.statement)}{year} - "
                f"{finding.independent_sources} independent source(s): "
                f"{_urls(finding.source_urls)}"
            )
        lines.append("")
    lines += ["## Source summaries", ""]
    for summary in report.source_summaries:
        lines += [f"### {md_text(summary.title)}", "", md_url(summary.url), ""]
        lines += _claim_lines(summary.claims)
        lines.append("")
    return lines + _tail(report)


def _candidate_markdown(candidate: RankedCandidate) -> list[str]:
    """Build the Markdown subsection for one ranked candidate.

    Args:
        candidate: The ranked candidate.

    Returns:
        The heading, profile link, rating, scepticism review, optional requirement
        coverage and claims.
    """
    marker = " (best match)" if candidate.best else ""
    lines = [
        f"### {candidate.rank}. {md_text(candidate.name)}{marker}",
        "",
        f"Profile: {md_url(candidate.profile_url)} ({candidate.network.value})",
        "",
    ]
    lines += [*_rating_lines(candidate.rating), ""]
    lines += [*_scepticism_lines(candidate.rating.scepticism), ""]
    if candidate.requirements is not None:
        lines += [f"Requirement coverage: {candidate.requirements.coverage * 100:.0f}%", ""]
    lines += _claim_lines(candidate.claims)
    return [*lines, ""]


def _skill_markdown(report: SkillSearchReport) -> list[str]:
    """Build the full Markdown lines for a skill search report.

    Args:
        report: The skill search report.

    Returns:
        The ranking table, the per-candidate sections, sources and limitations.
    """
    title = f"Candidate ranking: {md_text(report.skill)} in {md_text(report.location)}"
    lines = _header(report, title)
    lines += _company_lines(report.company)
    lines += [
        "## Ranking",
        "",
        "| Rank | Candidate | Overall | Scepticism | Profile |",
        "| --- | --- | --- | --- | --- |",
    ]
    for candidate in report.candidates:
        label = f"{md_text(candidate.name)}{' (best)' if candidate.best else ''}"
        lines.append(
            f"| {candidate.rank} | {label} | {candidate.rating.overall:.1f} / 5 | "
            f"{candidate.rating.scepticism.level.value} | {md_url(candidate.profile_url)} |"
        )
    if not report.candidates:
        lines.append("| - | No candidates could be assessed | - | - | - |")
    lines += ["", "## Candidates", ""]
    for candidate in report.candidates:
        lines += _candidate_markdown(candidate)
    return lines + _tail(report)


def render_markdown(report: Report) -> str:
    """Render a report as a Markdown document.

    Args:
        report: A person report or a skill search report.

    Returns:
        The Markdown text ending with a single newline.
    """
    lines = (
        _person_markdown(report) if isinstance(report, PersonReport) else _skill_markdown(report)
    )
    return "\n".join(lines).rstrip() + "\n"


def render_json(report: Report) -> str:
    """Render a report as indented JSON.

    Args:
        report: A person report or a skill search report.

    Returns:
        The JSON text ending with a newline.
    """
    return report.model_dump_json(indent=2) + "\n"


def export_report(report: Report, directory: Path) -> ExportedReport:
    """Write a report to disk as a Markdown and a JSON file.

    The directory is created when missing. Both files share a name made of the report time
    and a random suffix, and are made readable by the owner only.

    Args:
        report: The report to write.
        directory: Folder that receives the files.

    Returns:
        The paths of the two files.
    """
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"report-{report.generated_at:%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"
    exported = ExportedReport(directory / f"{stem}.md", directory / f"{stem}.json")
    exported.markdown.write_text(render_markdown(report), encoding="utf-8")
    exported.json.write_text(render_json(report), encoding="utf-8")
    for path in (exported.markdown, exported.json):
        # Reports can contain personal data, so the files are restricted to the owner.
        path.chmod(0o600)
    return exported


def delete_exported(exported: ExportedReport) -> None:
    """Delete the files of an exported report.

    Missing files are ignored.

    Args:
        exported: The paths returned by export_report.
    """
    exported.markdown.unlink(missing_ok=True)
    exported.json.unlink(missing_ok=True)


def delete_outputs(directory: Path) -> list[Path]:
    """Delete all exported report files in a directory.

    Only regular files whose names match the export naming pattern are removed.

    Args:
        directory: The output folder.

    Returns:
        The removed paths in sorted order, or an empty list when the folder is missing.
    """
    if not directory.is_dir():
        return []
    removed = []
    for path in sorted(directory.iterdir()):
        # Only files that match the export naming pattern are removed, and symlinks are skipped, so
        # unrelated files are never deleted.
        if OUTPUT_NAME.fullmatch(path.name) and path.is_file() and not path.is_symlink():
            path.unlink()
            removed.append(path)
    return removed
