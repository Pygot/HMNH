# src/agent/totp.py
from agent.config import AuthTuning
from urllib.parse import quote

import hashlib
import secrets
import base64
import struct
import segno
import hmac
import io
import re

SIZE = re.compile(r'width="(\d+)" height="(\d+)"')


def new_secret(tuning: AuthTuning) -> str:
    """Create a random TOTP shared secret.

    Args:
        tuning: Authentication settings giving the secret length in bytes.

    Returns:
        The secret as unpadded base32 text.
    """
    raw = secrets.token_bytes(tuning.totp_secret_bytes)
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def new_recovery_code(tuning: AuthTuning) -> str:
    """Create one random recovery code.

    Args:
        tuning: Authentication settings giving the code length in bytes.

    Returns:
        Lower-case hex text split into groups of five characters by hyphens.
    """
    raw = secrets.token_hex(tuning.recovery_code_bytes)
    return "-".join(raw[i : i + 5] for i in range(0, len(raw), 5))


def new_recovery_codes(tuning: AuthTuning) -> list[str]:
    """Create a full set of recovery codes.

    Args:
        tuning: Authentication settings giving the number of codes.

    Returns:
        The new recovery codes.
    """
    return [new_recovery_code(tuning) for _ in range(tuning.recovery_codes)]


def counter_for(now: float, tuning: AuthTuning) -> int:
    """Return the TOTP time step that contains a moment.

    Args:
        now: The time in epoch seconds.
        tuning: Authentication settings giving the step length in seconds.

    Returns:
        The number of whole steps since the epoch.
    """
    return int(now // tuning.totp_period_seconds)


def code_at(secret: str, counter: int, digits: int) -> str:
    """Compute the one-time code for a secret and time step.

    This is the HMAC-SHA1 based algorithm of RFC 4226.

    Args:
        secret: The base32 shared secret, with or without padding.
        counter: The time step.
        digits: The number of digits in the code.

    Returns:
        The code, zero-padded to the requested length.
    """
    padded = secret + "=" * (-len(secret) % 8)
    key = base64.b32decode(padded.upper(), casefold=True)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**digits).zfill(digits)


def check(secret: str, code: str, now: float, tuning: AuthTuning, last: int | None) -> int | None:
    """Verify a user-supplied code and return the time step it matches.

    Spaces in the code are ignored. Steps within the configured window around now
    are accepted, and a step at or below the last accepted one is refused so a
    code cannot be used twice.

    Args:
        secret: The base32 shared secret.
        code: The code typed by the user.
        now: The time in epoch seconds.
        tuning: Authentication settings for digits, step length and window.
        last: The last accepted time step, or None if there is none.

    Returns:
        The matching time step, to be stored as the new last step, or None when
        the code is wrong or has been used.
    """
    cleaned = "".join(code.split())
    if len(cleaned) != tuning.totp_digits or not cleaned.isdigit():
        return None
    middle = counter_for(now, tuning)
    matched: int | None = None
    for counter in range(middle - tuning.totp_window, middle + tuning.totp_window + 1):
        expected = code_at(secret, counter, tuning.totp_digits)
        # Every step in the window is compared without stopping early, and steps up to last are
        # refused to block replay.
        if hmac.compare_digest(expected, cleaned) and (last is None or counter > last):
            matched = counter
    return matched


def provisioning_uri(secret: str, account: str, tuning: AuthTuning) -> str:
    """Build the otpauth URI that authenticator apps read from a QR code.

    Args:
        secret: The base32 shared secret.
        account: The account name shown in the app.
        tuning: Authentication settings for issuer, digits and step length.

    Returns:
        The URI, with the issuer and account percent-encoded.
    """
    issuer = quote(tuning.totp_issuer, safe="")
    label = f"{issuer}:{quote(account, safe='')}"
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={issuer}"
        f"&digits={tuning.totp_digits}&period={tuning.totp_period_seconds}"
    )


def qr_svg(uri: str, scale: int, border: int) -> str:
    """Render text as an inline SVG QR code.

    Uses error correction level M and no XML declaration.

    Args:
        uri: The text to encode.
        scale: The pixel size of one module.
        border: The width of the quiet zone in modules.

    Returns:
        The SVG markup with a viewBox so it can be resized by CSS.
    """
    buffer = io.BytesIO()
    segno.make(uri, error="m").save(
        buffer,
        kind="svg",
        scale=scale,
        border=border,
        xmldecl=False,
        nl=False,
        svgclass="qr",
        lineclass=None,
    )
    # Swap the fixed width and height for a viewBox so CSS can size the image.
    return SIZE.sub(r'viewBox="0 0 \1 \2"', buffer.getvalue().decode("utf-8"), count=1)
