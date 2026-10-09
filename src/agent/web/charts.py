# src/agent/web/charts.py
from agent.config import ChartTuning
from collections.abc import Callable
from dataclasses import dataclass
from html import escape

import math

STEPS = (1, 2, 2.5, 5, 10)


@dataclass(frozen=True)
class Tick:
    """A y-axis gridline position with its text label."""

    y: float
    label: str


@dataclass(frozen=True)
class Bar:
    """One column to draw, with its position, size, series index and accessible label."""

    x: float
    y: float
    width: float
    height: float
    series: int
    label: str


@dataclass(frozen=True)
class Mark:
    """An x-axis label together with its horizontal position."""

    x: float
    label: str


@dataclass(frozen=True)
class Legend:
    """A legend entry linking a series index to its name."""

    series: int
    name: str


@dataclass(frozen=True)
class Plot:
    """A laid-out column or line chart, ready to be drawn as SVG.

    All coordinates are in SVG pixels.
    """

    width: int
    height: int
    left: int
    right_x: float
    base_y: float
    ticks: list[Tick]
    bars: list[Bar]
    marks: list[Mark]
    legend: list[Legend]
    lines: list[tuple[int, str]]
    description: str


@dataclass(frozen=True)
class Row:
    """One horizontal bar of a row chart with its label and value text."""

    y: float
    label: str
    width: float
    value: str
    height: float


@dataclass(frozen=True)
class RowPlot:
    """A laid-out horizontal bar chart, ready to be drawn as SVG."""

    width: int
    height: int
    label_width: int
    rows: list[Row]
    description: str


def point(values: list[int], position: int) -> int:
    """Return the value at a position, or 0 past the end of the list.

    Args:
        values: series values.
        position: index to read.

    Returns:
        The value, or 0 when the position is out of range.
    """
    return values[position] if position < len(values) else 0


def nice_top(highest: float, ticks: int) -> float:
    """Return a round upper bound for the y axis.

    The bound is a whole number of ticks, each a 1, 2, 2.5, 5 or 10 times a power of
    ten, and at least as high as the highest value.

    Args:
        highest: largest value to be shown.
        ticks: number of tick intervals on the axis.

    Returns:
        The axis maximum, or 1.0 when there is nothing positive to show.
    """
    if highest <= 0:
        return 1.0
    raw = max(highest / ticks, 1.0)
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(step * magnitude for step in STEPS if step * magnitude >= raw)
    return step * ticks


def clip(label: str, limit: int) -> str:
    """Shorten a label to fit a character limit.

    Args:
        label: text to shorten.
        limit: maximum length of the result.

    Returns:
        The label unchanged if it fits, otherwise a cut label ending with a period.
    """
    return label if len(label) <= limit else label[: limit - 1].rstrip() + "."


def scale_ticks(
    top: float, tuning: ChartTuning, format_value: Callable[[int], str], base_y: float, inner: float
) -> list[Tick]:
    """Build evenly spaced y-axis ticks from zero up to the top value.

    Args:
        top: axis maximum.
        tuning: chart settings giving the number of tick intervals.
        format_value: function turning a number into label text.
        base_y: y coordinate of the zero line.
        inner: height of the drawing area in pixels.

    Returns:
        One tick per interval boundary, from zero to the top.
    """
    return [
        Tick(
            y=base_y - inner * step / tuning.ticks,
            label=format_value(int(top * step / tuning.ticks)),
        )
        for step in range(tuning.ticks + 1)
    ]


def grouped_bars(
    labels: list[str],
    series: list[tuple[str, list[int]]],
    format_value: Callable[[int], str],
    tuning: ChartTuning,
    description: str,
) -> Plot:
    """Lay out a grouped column chart with one group per label.

    Args:
        labels: category labels along the x axis.
        series: pairs of series name and values; short value lists count as zeros.
        format_value: function turning a number into label text.
        tuning: chart size, spacing and label settings.
        description: text description of the chart for screen readers.

    Returns:
        The computed plot.
    """
    inner_width = tuning.width - tuning.left - tuning.right
    inner_height = tuning.height - tuning.top - tuning.bottom
    base_y = float(tuning.top + inner_height)
    top = nice_top(max((max(values, default=0) for _, values in series), default=0), tuning.ticks)
    slot = inner_width / max(1, len(labels))
    group = slot * (1 - tuning.bar_gap)
    count = max(1, len(series))
    width = group / count * (1 - tuning.series_gap)
    bars = []
    marks = []
    for position, label in enumerate(labels):
        start = tuning.left + slot * position + (slot - group) / 2
        marks.append(
            Mark(
                x=round(tuning.left + slot * (position + 0.5), 1),
                label=clip(label, tuning.label_chars),
            )
        )
        for index, (name, values) in enumerate(series):
            value = values[position] if position < len(values) else 0
            height = inner_height * value / top if top else 0
            bars.append(
                Bar(
                    x=round(start + group / count * index, 1),
                    y=round(base_y - height, 1),
                    width=round(width, 1),
                    height=round(height, 1),
                    series=index,
                    label=f"{name}, {label}: {format_value(value)}",
                )
            )
    return Plot(
        width=tuning.width,
        height=tuning.height,
        left=tuning.left,
        right_x=float(tuning.width - tuning.right),
        base_y=base_y,
        ticks=scale_ticks(top, tuning, format_value, base_y, inner_height),
        bars=bars,
        marks=marks,
        legend=[Legend(series=index, name=name) for index, (name, _) in enumerate(series)],
        lines=[],
        description=description,
    )


def line_plot(
    labels: list[str],
    series: list[tuple[str, list[int]]],
    format_value: Callable[[int], str],
    tuning: ChartTuning,
    description: str,
) -> Plot:
    """Lay out a line chart with one polyline per series.

    Args:
        labels: category labels along the x axis.
        series: pairs of series name and values; short value lists count as zeros.
        format_value: function turning a number into label text.
        tuning: chart size, spacing and label settings.
        description: text description of the chart for screen readers.

    Returns:
        The computed plot.
    """
    inner_width = tuning.width - tuning.left - tuning.right
    inner_height = tuning.height - tuning.top - tuning.bottom
    base_y = float(tuning.top + inner_height)
    top = nice_top(max((max(values, default=0) for _, values in series), default=0), tuning.ticks)
    steps = max(1, len(labels) - 1)
    lines = []
    for index, (_, values) in enumerate(series):
        points = " ".join(
            f"{tuning.left + inner_width * position / steps:.1f},"
            f"{base_y - inner_height * point(values, position) / top:.1f}"
            for position in range(len(labels))
        )
        lines.append((index, points))
    marks = [
        Mark(
            x=round(tuning.left + inner_width * position / steps, 1),
            label=clip(label, tuning.label_chars),
        )
        for position, label in enumerate(labels)
    ]
    return Plot(
        width=tuning.width,
        height=tuning.height,
        left=tuning.left,
        right_x=float(tuning.width - tuning.right),
        base_y=base_y,
        ticks=scale_ticks(top, tuning, format_value, base_y, inner_height),
        bars=[],
        marks=marks,
        legend=[Legend(series=index, name=name) for index, (name, _) in enumerate(series)],
        lines=lines,
        description=description,
    )


def row_bars(
    items: list[tuple[str, int]],
    format_value: Callable[[int], str],
    tuning: ChartTuning,
    description: str,
) -> RowPlot:
    """Lay out a horizontal bar chart with one row per item.

    Bars are scaled to the largest value and are never narrower than 2 pixels.

    Args:
        items: pairs of label and value.
        format_value: function turning a number into value text.
        tuning: chart size and row settings.
        description: text description of the chart for screen readers.

    Returns:
        The computed row plot.
    """
    top = max((value for _, value in items), default=0) or 1
    # Reserve 90 pixels on the right for the value text printed after the longest bar.
    available = tuning.width - tuning.row_label_width - tuning.right - 90
    rows = []
    for position, (label, value) in enumerate(items):
        rows.append(
            Row(
                y=float(position * tuning.row_height + 4),
                label=clip(label, tuning.label_chars * 2),
                width=round(max(2.0, available * value / top), 1),
                value=format_value(value),
                height=float(tuning.row_height - 10),
            )
        )
    return RowPlot(
        width=tuning.width,
        height=max(tuning.row_height, len(items) * tuning.row_height + 4),
        label_width=tuning.row_label_width,
        rows=rows,
        description=description,
    )


def fills(tuning: ChartTuning) -> list[tuple[str, str]]:
    """Return the fill and stroke colour pairs used for successive series.

    Args:
        tuning: chart settings holding the colours.

    Returns:
        A list of (fill, stroke) pairs, to be cycled through by series index.
    """
    return [
        (tuning.ink, "none"),
        (tuning.faint, "none"),
        (tuning.tint, tuning.faint),
        (tuning.paper, tuning.ink),
    ]


def thin(labels: list[str], limit: int) -> list[str]:
    """Blank out labels so that only about a limit of them stay visible.

    Args:
        labels: labels in order.
        limit: number of labels to keep at most.

    Returns:
        The same labels when they fit, otherwise a list of the same length where all
        but every n-th label are empty strings.
    """
    if len(labels) <= limit:
        return labels
    # Negated floor division gives the ceiling, so at most limit labels are kept.
    step = -(-len(labels) // limit)
    return [label if position % step == 0 else "" for position, label in enumerate(labels)]


def frame(width: int, height: int, description: str, body: str, tuning: ChartTuning) -> str:
    """Wrap SVG body markup in an svg element with a background.

    The description is HTML-escaped and used as the accessible label.

    Args:
        width: image width in pixels.
        height: image height in pixels.
        description: text description of the chart for screen readers.
        body: inner SVG markup.
        tuning: chart settings giving the background colour.

    Returns:
        The complete SVG markup.
    """
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" aria-label="{escape(description)}" '
        f'font-family="monospace" font-size="11">'
        f'<rect width="100%" height="100%" fill="{tuning.paper}"/>{body}</svg>'
    )


def plot_svg(plot: Plot, tuning: ChartTuning) -> str:
    """Render a column or line plot as SVG markup.

    All label text is HTML-escaped. The legend appears only when there is more than one
    series, and line series use different dash patterns.

    Args:
        plot: computed plot to draw.
        tuning: chart settings giving the colours.

    Returns:
        The SVG markup.
    """
    colours = fills(tuning)
    parts = []
    for tick in plot.ticks:
        parts.append(
            f'<line x1="{plot.left}" x2="{plot.right_x}" y1="{tick.y}" y2="{tick.y}" '
            f'stroke="{tuning.grid}"/>'
            f'<text x="{plot.left - 8}" y="{tick.y + 4}" text-anchor="end" fill="{tuning.text}">'
            f"{escape(tick.label)}</text>"
        )
    for bar in plot.bars:
        fill, stroke = colours[bar.series % len(colours)]
        parts.append(
            f'<rect x="{bar.x}" y="{bar.y}" width="{bar.width}" height="{bar.height}" rx="2" '
            f'fill="{fill}" stroke="{stroke}"/>'
        )
    dashes = ("", "6 4", "2 4")
    for index, points in plot.lines:
        dash = dashes[index % len(dashes)]
        parts.append(
            f'<polyline points="{points}" fill="none" stroke-width="2.5" '
            f'stroke="{colours[index % len(colours)][0]}" stroke-linecap="round" '
            f'stroke-linejoin="round"' + (f' stroke-dasharray="{dash}"' if dash else "") + "/>"
        )
    for mark in plot.marks:
        parts.append(
            f'<text x="{mark.x}" y="{plot.base_y + 18}" text-anchor="middle" fill="{tuning.text}">'
            f"{escape(mark.label)}</text>"
        )
    for position, item in enumerate(plot.legend if len(plot.legend) > 1 else []):
        fill, stroke = colours[item.series % len(colours)]
        parts.append(
            f'<rect x="{plot.left + position * 120}" y="2" width="10" height="10" fill="{fill}" '
            f'stroke="{stroke}"/><text x="{plot.left + position * 120 + 14}" y="11" '
            f'fill="{tuning.text}">{escape(item.name)}</text>'
        )
    return frame(plot.width, plot.height, plot.description, "".join(parts), tuning)


def rows_svg(plot: RowPlot, tuning: ChartTuning) -> str:
    """Render a row plot as SVG markup.

    All label text is HTML-escaped.

    Args:
        plot: computed row plot to draw.
        tuning: chart settings giving the colours.

    Returns:
        The SVG markup.
    """
    parts = []
    for row in plot.rows:
        parts.append(
            f'<text x="0" y="{row.y + row.height - 3}" fill="{tuning.text}">'
            f"{escape(row.label)}</text>"
            f'<rect x="{plot.label_width}" y="{row.y}" width="{row.width}" height="{row.height}" '
            f'rx="2" fill="{tuning.ink}"/>'
            f'<text x="{plot.label_width + row.width + 8}" y="{row.y + row.height - 3}" '
            f'fill="{tuning.text}">{escape(row.value)}</text>'
        )
    return frame(plot.width, plot.height, plot.description, "".join(parts), tuning)
