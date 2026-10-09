# src/agent/web/icons.py
from agent.events import Task
from markupsafe import Markup

FALLBACK = Task.SEARCH.value
TASK_ICONS = {
    Task.PARSE.value: (
        '<path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4"/><path d="M9.5 11h5M9.5 15h5"/>'
        '<path class="a" d="M5 11h14"/>'
    ),
    Task.COMPANY.value: (
        '<path d="M5 21V7l7-4 7 4v14zM3 21h18"/>'
        '<path class="w" d="M9 10h2"/><path class="w w2" d="M13 10h2"/>'
        '<path class="w w3" d="M9 14h2"/><path class="w w4" d="M13 14h2"/>'
    ),
    Task.SEARCH.value: (
        '<path class="dots" d="M4 5h2M18 4h2M3 19h2M19 20h2"/>'
        '<g class="a"><circle cx="10.5" cy="10.5" r="5.5"/><path d="M15 15l5 5"/></g>'
    ),
    Task.LINKEDIN.value: (
        '<rect x="4" y="5" width="16" height="14" rx="2"/><circle cx="9" cy="11" r="2"/>'
        '<path d="M6.5 16.5c.6-1.8 4.4-1.8 5 0"/>'
        '<path class="a" d="M14 10h4"/><path class="a a2" d="M14 14h4"/>'
    ),
    Task.FACEBOOK.value: (
        '<circle cx="9" cy="9" r="3"/><path d="M3 20c0-3.5 3-5 6-5s6 1.5 6 5"/>'
        '<g class="a"><circle cx="17" cy="8" r="2.4"/><path d="M16 14c3 0 5 1.2 5 4"/></g>'
    ),
    Task.INSTAGRAM.value: (
        '<rect x="4" y="6" width="16" height="13" rx="3"/><path d="M9 6l1-2h4l1 2"/>'
        '<circle class="a" cx="12" cy="12.5" r="3.5"/><circle class="b" cx="17" cy="9.5" r=".7"/>'
    ),
    Task.WEB.value: (
        '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/>'
        '<ellipse class="a" cx="12" cy="12" rx="4" ry="9"/>'
        '<ellipse class="b" cx="12" cy="12" rx="4" ry="9"/>'
    ),
    Task.SUMMARISE.value: (
        '<rect x="4" y="3" width="16" height="18" rx="2"/>'
        '<path class="l" d="M8 8h8"/><path class="l l2" d="M8 12h8"/>'
        '<path class="l l3" d="M8 16h5"/>'
    ),
    Task.MERGE.value: (
        '<circle class="a" cx="6" cy="7" r="2.5"/><circle class="b" cx="6" cy="17" r="2.5"/>'
        '<circle cx="18" cy="12" r="3"/><path d="M8.5 8l6.5 3M8.5 16l6.5-3"/>'
    ),
    Task.REQUIREMENTS.value: (
        '<rect x="4" y="3" width="16" height="18" rx="2"/>'
        '<path class="c" pathLength="1" d="M7.5 8l1.5 1.5L12 6.5"/>'
        '<path class="c c2" pathLength="1" d="M7.5 13l1.5 1.5L12 11.5"/>'
        '<path class="c c3" pathLength="1" d="M7.5 18l1.5 1.5L12 16.5"/>'
        '<path d="M14 8h3M14 13h3M14 18h3"/>'
    ),
    Task.RATE.value: (
        '<path class="a" pathLength="1" d="M12 3.5l2.6 5.3 5.9.8-4.3 4.1 1 5.8'
        'L12 16.8 6.8 19.5l1-5.8L3.5 9.6l5.9-.8z"/>'
    ),
    Task.SCEPTICISM.value: (
        '<path class="a" d="M12 3l7 3v5c0 5-3 8-7 10-4-2-7-5-7-10V6z"/>'
        '<path class="b" d="M12 8.5v4"/><circle class="b" cx="12" cy="15.8" r=".6"/>'
    ),
    Task.EXPORT.value: ('<path d="M4 17v3h16v-3"/><path class="a" d="M12 4v10M8 10.5l4 4 4-4"/>'),
    Task.WAIT.value: (
        '<path d="M4 5h16v11H9l-5 4z"/><circle class="d" cx="9" cy="10.5" r="1"/>'
        '<circle class="d d2" cx="12.5" cy="10.5" r="1"/>'
        '<circle class="d d3" cx="16" cy="10.5" r="1"/>'
    ),
}


def task_icon(name: str | None, extra: str = "") -> Markup:
    # Only known icon names reach the markup, so an outside value can never be written into the svg.
    """Return the inline SVG icon for a research task.

    Args:
        name: The task name. Unknown or missing names use the search icon.
        extra: Extra CSS class names for the svg. They are inserted without escaping, so
            only pass trusted text.

    Returns:
        The svg markup, marked safe for templates.
    """
    # Only known icon names reach the markup, so an outside value can never be written into the svg.
    key = name if name in TASK_ICONS else FALLBACK
    classes = " ".join(part for part in ("task", f"t-{key}", extra) if part)
    return Markup(
        f'<svg class="{classes}" viewBox="0 0 24 24" aria-hidden="true">{TASK_ICONS[key]}</svg>'
    )


def task_icon_set() -> Markup:
    """Return a template element that holds every task icon.

    Each svg inside carries a data-task attribute so that scripts can copy the icon
    for a task.

    Returns:
        The template markup, marked safe for templates.
    """
    items = "".join(
        f'<svg class="task t-{key}" data-task="{key}" viewBox="0 0 24 24" aria-hidden="true">'
        f"{markup}</svg>"
        for key, markup in TASK_ICONS.items()
    )
    return Markup(f"<template data-icons>{items}</template>")
