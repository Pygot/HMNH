# src/agent/web/overview.py
from agent.models import (
    Finding,
    PersonReport,
    Rating,
    ScepticismLevel,
    SkillSearchReport,
    Tag,
)
from agent.config import ScaleTuning
from dataclasses import dataclass
from collections import Counter
from typing import Any


@dataclass(frozen=True)
class Tick:
    """A labelled tick mark on the rating scale."""

    x: float
    label: str


@dataclass(frozen=True)
class Scale:
    """Drawing coordinates for the rating scale graphic."""

    width: float
    height: float
    left: float
    y: float
    tick_top: float
    tick_bottom: float
    label_y: float
    fill: float
    raw: float | None
    ticks: list[Tick]


@dataclass(frozen=True)
class Segment:
    """One coloured section of the evidence mix bar."""

    tag: str
    count: int
    x: float
    width: float


def rating_scale(rating: Rating, tuning: ScaleTuning) -> Scale:
    """Compute the drawing coordinates of the rating scale for a report.

    The scale has a filled track up to the overall rating and one tick for each whole
    number from zero to the top of the scale.

    Args:
        rating: The rating to draw.
        tuning: Sizes and spacing of the scale graphic.

    Returns:
        The scale, with the raw rating position set only when the overall rating was
        capped below the raw one.
    """
    unit = (tuning.width - 2 * tuning.margin) / tuning.top

    def place(value: float) -> float:
        """Convert a rating value to a horizontal position on the scale.

        Args:
            value: The rating value. Values outside the scale are clamped to it.

        Returns:
            The position, rounded to two decimals.
        """
        return round(tuning.margin + min(max(value, 0.0), tuning.top) * unit, 2)

    # The raw position is drawn only when scepticism capped the overall rating below the raw score.
    capped = rating.raw_overall > rating.overall
    return Scale(
        width=tuning.width,
        height=tuning.height,
        left=tuning.margin,
        y=tuning.track_y,
        tick_top=tuning.track_y + tuning.tick_gap,
        tick_bottom=tuning.track_y + tuning.tick_gap + tuning.tick_length,
        label_y=tuning.track_y + tuning.tick_gap + tuning.tick_length + tuning.label_gap,
        fill=place(rating.overall),
        raw=place(rating.raw_overall) if capped else None,
        ticks=[Tick(place(step), str(step)) for step in range(int(tuning.top) + 1)],
    )


def evidence_mix(findings: list[Finding], tuning: ScaleTuning) -> list[Segment]:
    """Split the evidence bar into one segment per finding tag.

    Segment widths are proportional to the number of findings with each tag. Tags
    without findings get no segment.

    Args:
        findings: The findings of a report.
        tuning: Sizes of the bar graphic.

    Returns:
        The segments in tag order, laid out from left to right.
    """
    counts = Counter(finding.tag for finding in findings)
    total = sum(counts.values())
    segments: list[Segment] = []
    cursor = 0.0
    for tag in Tag:
        if not counts[tag]:
            continue
        width = round(tuning.bar_width * counts[tag] / total, 2)
        segments.append(Segment(tag.value, counts[tag], cursor, width))
        cursor += width
    return segments


def report_overview(
    report: PersonReport | SkillSearchReport | None, tuning: ScaleTuning
) -> dict[str, Any]:
    """Build the values that the report page needs for its overview graphics.

    Args:
        report: The report to summarise, or None.
        tuning: Sizes and spacing of the graphics.

    Returns:
        For a person report, the rating scale, evidence mix, bar size, and number of
        supported findings. For a skill search report, the number of candidates with
        a scepticism warning. An empty mapping for anything else.
    """
    if isinstance(report, PersonReport):
        return {
            "scale": rating_scale(report.rating, tuning),
            "mix": evidence_mix(report.findings, tuning),
            "bar_width": tuning.bar_width,
            "bar_height": tuning.bar_height,
            "supported": sum(1 for finding in report.findings if finding.tag is Tag.SUPPORTED),
        }
    if isinstance(report, SkillSearchReport):
        cautioned = sum(
            1
            for candidate in report.candidates
            if candidate.rating.scepticism.level is not ScepticismLevel.NONE
        )
        return {"cautioned": cautioned}
    return {}
