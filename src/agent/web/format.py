# src/agent/web/format.py
from datetime import (
    datetime,
    UTC,
)

SPANS = (("d", 86_400), ("h", 3_600), ("min", 60))
BROWSERS = (
    # Order matters: Edge and Opera agents also contain chrome and safari, so they are tested first.
    ("edg/", "Edge"),
    ("opr/", "Opera"),
    ("firefox", "Firefox"),
    ("chrome", "Chrome"),
    ("safari", "Safari"),
)
SYSTEMS = (
    ("windows", "Windows"),
    ("android", "Android"),
    # Order matters: iPhone and iPad agents also contain mac os, and Android agents contain linux.
    ("iphone", "iOS"),
    ("ipad", "iPadOS"),
    ("mac os", "macOS"),
    ("linux", "Linux"),
)


def stamp(value: float | None, pattern: str) -> str:
    """Format a Unix timestamp as UTC text.

    Args:
        value: Seconds since the Unix epoch, or None.
        pattern: The strftime pattern to format with.

    Returns:
        The formatted time, an empty string for None, or unknown when the value cannot
        be converted to a date.
    """
    if value is None:
        return ""
    try:
        return datetime.fromtimestamp(value, UTC).strftime(pattern)
    except OverflowError, OSError, ValueError:
        return "unknown"


def device(agent: str) -> str:
    """Return a short browser and system label from a user agent string.

    Args:
        agent: The user agent header of a client.

    Returns:
        A label like Chrome on Windows, only the browser or system when just one is
        recognised, or Unknown device.
    """
    low = agent.lower()
    browser = next((label for key, label in BROWSERS if key in low), "")
    system = next((label for key, label in SYSTEMS if key in low), "")
    if browser and system:
        return f"{browser} on {system}"
    return browser or system or "Unknown device"


def span(seconds: float) -> str:
    """Format a duration in seconds as short text.

    The largest fitting unit is used (d, h, min or s). Durations under ten seconds are
    shown as moments and negative values count as zero.

    Args:
        seconds: The length of the duration in seconds.

    Returns:
        The duration, for example 3 h or 45 s.
    """
    seconds = max(0.0, float(seconds))
    for unit, size in SPANS:
        if seconds >= size:
            return f"{int(seconds // size)} {unit}"
    return "moments" if seconds < 10 else f"{int(seconds)} s"
