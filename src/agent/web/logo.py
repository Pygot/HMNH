# src/agent/web/logo.py
from markupsafe import Markup
from functools import cache
from pathlib import Path

import re

STATIC = Path(__file__).parent / "static"
LOGO_FILE = "logo.svg"
TIE_FILE = "tie.svg"


@cache
def _source(file: str) -> str:
    """Read a bundled SVG file and strip comments and whitespace between tags.

    The result is cached for the life of the process.

    Args:
        file: File name inside the static folder.

    Returns:
        The compacted SVG source.
    """
    raw = (STATIC / file).read_text(encoding="utf-8")
    return re.sub(r">\s+<", "><", re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL)).strip()


def _inline(file: str, css_class: str) -> Markup:
    """Return a bundled SVG ready to embed in a page.

    The svg element gets the given class and aria-hidden so screen readers skip it.

    Args:
        file: File name inside the static folder.
        css_class: CSS class added to the svg element.

    Returns:
        The SVG as trusted markup.
    """
    marked = _source(file).replace("<svg ", f'<svg class="{css_class}" aria-hidden="true" ', 1)
    return Markup(marked)


def logo_markup(css_class: str = "logo") -> Markup:
    """Return the inline SVG of the logo.

    Args:
        css_class: CSS class added to the svg element.

    Returns:
        The logo as trusted markup.
    """
    return _inline(LOGO_FILE, css_class)


def tie_markup(css_class: str = "tie") -> Markup:
    """Return the inline SVG of the tie icon.

    Args:
        css_class: CSS class added to the svg element.

    Returns:
        The tie icon as trusted markup.
    """
    return _inline(TIE_FILE, css_class)
