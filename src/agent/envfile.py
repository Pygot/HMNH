# src/agent/envfile.py
from agent.config import (
    DEFAULT_ENV_FILE,
    ENV_FILE_VARIABLE,
    MAX_ENV_FILE_BYTES,
    Settings,
)
from agent.errors import ConfigError
from dotenv import dotenv_values
from pathlib import Path

import secrets
import io
import os
import re

FILE_MODE = 0o600
KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
LINE_KEY = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
PLAIN_VALUE = re.compile(r"^[A-Za-z0-9_@%+=:,./-]+$")
FORBIDDEN_VALUE = re.compile(
    r"[\x00-\x08\x0a-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]|\$\{"
)
LINE_BREAK = re.compile(r"\r\n|\n|\r")
MAX_VALUE_CHARS = 500


def env_source() -> Path | None:
    """Return the settings file to read, if there is one.

    The path comes from the environment variable for the file, or the default file name.

    Returns:
        The path of the file, or None when it does not exist.

    Raises:
        ConfigError: when the path is not a regular file or is too large.
    """
    path = Path(os.environ.get(ENV_FILE_VARIABLE) or DEFAULT_ENV_FILE)
    if not path.exists():
        return None
    if not path.is_file():
        raise ConfigError(f"{path} is not a regular file.")
    if path.stat().st_size > MAX_ENV_FILE_BYTES:
        raise ConfigError(f"{path} is larger than {MAX_ENV_FILE_BYTES} bytes.")
    return path


def env_target() -> Path:
    """Return the path of the settings file that changes are written to.

    Returns:
        The configured or default path; the file does not have to exist.
    """
    return Path(os.environ.get(ENV_FILE_VARIABLE) or DEFAULT_ENV_FILE)


def format_value(value: str) -> str:
    """Format a value for storage in the settings file.

    Args:
        value: The already checked value.

    Returns:
        The value as is when it only has safe characters, otherwise double quoted with
        backslashes and double quotes escaped.
    """
    if PLAIN_VALUE.fullmatch(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def check_value(key: str, value: str) -> str:
    """Validate and clean a value before it is stored.

    Args:
        key: Name of the setting, used in error messages.
        value: The value to check.

    Returns:
        The value with surrounding whitespace removed.

    Raises:
        ConfigError: when the value is too long or contains control characters, line
            breaks, invisible or direction changing characters, or a variable reference.
    """
    cleaned = value.strip()
    if len(cleaned) > MAX_VALUE_CHARS:
        raise ConfigError(f"{key} is too long.")
    if FORBIDDEN_VALUE.search(cleaned):
        raise ConfigError(f"{key} contains characters that cannot be stored (line breaks or ${{).")
    return cleaned


def write_values(path: Path, values: dict[str, str | None], allowed: frozenset[str]) -> None:
    """Update settings in the file while keeping all other lines.

    Existing keys are replaced in place (duplicates are dropped), new keys are appended
    and keys with an empty value are removed. The file is replaced atomically and keeps
    its line ending style.

    Args:
        path: The settings file.
        values: New value per key; None or an empty string removes the key.
        allowed: The keys that may be changed.

    Raises:
        ConfigError: when a key is not allowed, a value is invalid, the path is a link or
            not a regular file, the file would become too large or the result would
            contain unexpected keys.
    """
    unknown = sorted(set(values) - allowed)
    if unknown or any(not KEY_PATTERN.fullmatch(key) for key in values):
        raise ConfigError("Some settings cannot be changed from here.")
    cleaned = {key: (check_value(key, value) if value else None) for key, value in values.items()}
    if path.is_symlink():
        raise ConfigError(f"{path} is a link; refusing to write through it.")
    existing = ""
    if path.exists():
        if not path.is_file():
            raise ConfigError(f"{path} is not a regular file.")
        if path.stat().st_size > MAX_ENV_FILE_BYTES:
            raise ConfigError(f"{path} is larger than {MAX_ENV_FILE_BYTES} bytes.")
        existing = path.read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in existing else "\n"
    lines: list[str] = []
    written: set[str] = set()
    pieces = LINE_BREAK.split(existing)
    if pieces and pieces[-1] == "":
        pieces.pop()
    for line in pieces:
        match = LINE_KEY.match(line)
        key = match.group(1) if match else None
        if key not in cleaned:
            lines.append(line)
        elif key not in written:
            written.add(key)
            if cleaned[key] is not None:
                lines.append(f"{key}={format_value(cleaned[key] or '')}")
    lines.extend(
        f"{key}={format_value(value)}"
        for key, value in cleaned.items()
        if key not in written and value is not None
    )
    text = newline.join(lines) + newline if lines else ""
    # Parse the result back to be sure no key outside the requested ones appeared, for example
    # through an injected value.
    if set(_keys(text)) - set(_keys(existing)) - set(cleaned):
        raise ConfigError("The settings file would not be valid; nothing was written.")
    data = text.encode("utf-8")
    if len(data) > MAX_ENV_FILE_BYTES:
        raise ConfigError("The settings file would become too large.")
    _replace(path, data)


def _replace(path: Path, data: bytes) -> None:
    """Replace a file atomically with the given bytes.

    The data goes to a temporary file with owner-only permissions that is flushed to
    disk and then renamed over the target. Missing parent folders are created.

    Args:
        path: The file to replace.
        data: The new content.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    # O_EXCL on a random name never reuses an existing file, and O_BINARY avoids newline translation
    # on Windows.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(temporary, flags, FILE_MODE)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def snapshot(path: Path) -> bytes | None:
    """Read the current content of a settings file so it can be restored.

    Args:
        path: The settings file.

    Returns:
        The file bytes, or None when it is missing, a link or not a regular file.
    """
    if path.is_symlink() or not path.is_file():
        return None
    return path.read_bytes()


def restore(path: Path, data: bytes | None) -> None:
    """Put a settings file back to an earlier content.

    Args:
        path: The settings file.
        data: The bytes from snapshot, or None to delete the file.

    Raises:
        ConfigError: when the path is a link.
    """
    if path.is_symlink():
        raise ConfigError(f"{path} is a link; refusing to write through it.")
    if data is None:
        path.unlink(missing_ok=True)
    else:
        _replace(path, data)


def _keys(text: str) -> list[str]:
    """Return the keys that a dotenv parser finds in some text.

    Args:
        text: Content of a settings file.

    Returns:
        The parsed keys.
    """
    return list(dotenv_values(stream=io.StringIO(text)))


def overridden(keys: list[str]) -> list[str]:
    """Return the keys that are also set in the process environment.

    Args:
        keys: Setting names to check.

    Returns:
        The keys with a non-empty environment value, which take precedence over the file.
    """
    return [key for key in keys if os.environ.get(key)]


def env_warnings(path: Path | None) -> list[str]:
    """Return warnings about the permissions of the settings file.

    Only checked on POSIX systems.

    Args:
        path: The settings file, or None.

    Returns:
        A warning when other users can read the file, otherwise an empty list.
    """
    if path is None or os.name != "posix" or not path.stat().st_mode & 0o077:
        return []
    return [f"{path} can be read by other users; run: chmod 600 {path}"]


def load_settings() -> Settings:
    """Load the application settings from the settings file and the environment.

    Returns:
        The loaded settings.

    Raises:
        ConfigError: when the settings file is not usable.
    """
    return Settings(_env_file=env_source(), _env_file_encoding="utf-8")
