# src/agent/text.py
from agent.config import ValidationLimits
from pydantic import ValidationError
from urllib.parse import urlsplit

import unicodedata

LIMITS = ValidationLimits()


def clean_text(value: str, max_length: int | None = None) -> str:
    """Normalise whitespace in a text value and check its length and characters.

    Runs of whitespace, including line breaks, become single spaces.

    Args:
        value: Raw text.
        max_length: Maximum length after cleaning, defaulting to the configured limit.

    Returns:
        The cleaned text.

    Raises:
        ValueError: when the text is empty, too long, or contains control or
            formatting characters.
    """
    limit = LIMITS.max_text if max_length is None else max_length
    cleaned = " ".join(value.split())
    if not cleaned:
        raise ValueError("must not be empty")
    if len(cleaned) > limit:
        raise ValueError(f"must be at most {limit} characters")
    # Category C covers control, format and unassigned characters, which blocks hidden or invisible
    # text.
    if any(unicodedata.category(ch).startswith("C") for ch in cleaned):
        raise ValueError("control and formatting characters are not allowed")
    return cleaned


def city_level(value: str) -> str:
    """Validate a city or region name.

    Args:
        value: Raw location text.

    Returns:
        The cleaned location.

    Raises:
        ValueError: when the text fails cleaning or contains digits, which would
            indicate a street number or postal code.
    """
    cleaned = clean_text(value, LIMITS.max_city)
    if any(ch.isdigit() for ch in cleaned):
        raise ValueError("must be a city or region name without street numbers or postal codes")
    return cleaned


def http_url(value: str) -> str:
    """Validate that a value is an http or https URL.

    Args:
        value: Raw URL text.

    Returns:
        The URL with surrounding whitespace removed, otherwise unchanged.

    Raises:
        ValueError: when the URL is too long, contains whitespace or control
            characters, uses another scheme, or has no host.
    """
    stripped = value.strip()
    if len(stripped) > LIMITS.max_url or any(ch.isspace() or ord(ch) < 32 for ch in stripped):
        raise ValueError("invalid URL")
    parts = urlsplit(stripped)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("must be an http(s) URL")
    return stripped


def sentence(value: str) -> str:
    """Clean a single sentence of free text using the sentence length limit.

    Args:
        value: Raw text.

    Returns:
        The cleaned text.

    Raises:
        ValueError: when the text fails the checks of clean_text.
    """
    return clean_text(value, LIMITS.max_sentence)


def describe_errors(error: ValidationError) -> str:
    """Summarise a pydantic validation error as one readable line.

    Args:
        error: The validation error.

    Returns:
        Entries of the form location: message joined by semicolons, where the
        location is the dotted field path or input for the whole value.
    """
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or 'input'}: {item['msg']}"
        for item in error.errors()
    )
