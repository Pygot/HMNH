# src/agent/passwords.py
from agent.config import AuthTuning

import unicodedata
import hashlib
import base64
import hmac
import os

SCHEME = "scrypt"
MAX_STORED_N = 2**20
MAX_STORED_FACTOR = 64


def _normal(password: str) -> bytes:
    """Return a password in its normalized UTF-8 byte form.

    NFKC normalization makes equivalent spellings of one password hash the same.

    Args:
        password: The password text.

    Returns:
        The normalized password encoded as UTF-8.
    """
    return unicodedata.normalize("NFKC", password).encode("utf-8")


def _b64(raw: bytes) -> str:
    """Return bytes as base64 text.

    Args:
        raw: The bytes to encode.

    Returns:
        The standard base64 encoding as an ASCII string.
    """
    return base64.b64encode(raw).decode("ascii")


class PasswordHasher:
    """A scrypt password hasher with a self-describing storage format.

    A stored hash has the form scrypt$n$r$p$salt$digest, so old hashes stay verifiable
    after the cost settings change. Verification compares in constant time and does
    the same work for unknown or malformed hashes, to hide which accounts exist.
    """

    def __init__(self, tuning: AuthTuning):
        """Initialize the hasher.

        Args:
            tuning: The scrypt cost, salt and hash sizes and password length rules.
        """
        self._tuning = tuning
        self._dummy: str | None = None

    def _derive(
        self, password: str, salt: bytes, n: int, r: int, p: int, size: int, memory: int
    ) -> bytes:
        """Derive a scrypt key from a password.

        Args:
            password: The password text, normalized before use.
            salt: The random salt.
            n: The scrypt CPU and memory cost, a power of two.
            r: The scrypt block size.
            p: The scrypt parallelism.
            size: The length of the derived key in bytes.
            memory: The memory limit for scrypt in bytes.

        Returns:
            The derived key.
        """
        return hashlib.scrypt(
            _normal(password), salt=salt, n=n, r=r, p=p, maxmem=memory, dklen=size
        )

    def hash(self, password: str) -> str:
        """Hash a password with a new random salt.

        The configured cost settings are used. Length and strength rules are not checked
        here, see problems.

        Args:
            password: The password text.

        Returns:
            The encoded hash as scrypt$n$r$p$salt$digest with a base64 salt and digest.
        """
        tuning = self._tuning
        salt = os.urandom(tuning.salt_bytes)
        digest = self._derive(
            password,
            salt,
            tuning.scrypt_n,
            tuning.scrypt_r,
            tuning.scrypt_p,
            tuning.hash_bytes,
            tuning.scrypt_maxmem,
        )
        parts = (
            SCHEME,
            tuning.scrypt_n,
            tuning.scrypt_r,
            tuning.scrypt_p,
            _b64(salt),
            _b64(digest),
        )
        return "$".join(str(part) for part in parts)

    def _parse(self, encoded: str) -> tuple[int, int, int, bytes, bytes] | None:
        """Parse a stored hash and check that its parameters are sane.

        Args:
            encoded: The stored hash.

        Returns:
            The tuple n, r, p, salt and digest, or None when the value is malformed, uses
            another scheme, or has parameters outside the accepted bounds.
        """
        pieces = encoded.split("$") if isinstance(encoded, str) else []
        if len(pieces) != 6 or pieces[0] != SCHEME:
            return None
        try:
            n, r, p = (int(piece) for piece in pieces[1:4])
            salt = base64.b64decode(pieces[4], validate=True)
            digest = base64.b64decode(pieces[5], validate=True)
        except ValueError:
            return None
        sane = 1 < n <= MAX_STORED_N and n & (n - 1) == 0
        sane = sane and 1 <= r <= MAX_STORED_FACTOR and 1 <= p <= MAX_STORED_FACTOR
        sane = sane and bool(salt) and bool(digest)
        return (n, r, p, salt, digest) if sane else None

    def verify(self, password: str, encoded: str | None) -> bool:
        # Very long passwords are rejected before hashing so they cannot be used to burn CPU.
        """Check a password against a stored hash.

        The cost settings stored in the hash are used, not the current ones. Passwords
        longer than the maximum length are rejected without hashing. A missing or invalid
        hash costs the time of a real check and then fails.

        Args:
            password: The password text to check.
            encoded: The stored hash, or None when there is none.

        Returns:
            True if the password matches the hash.
        """
        # Very long passwords are rejected before hashing so they cannot be used to burn CPU.
        if len(password) > self._tuning.password_max_length:
            return False
        parsed = self._parse(encoded) if encoded else None
        if parsed is None:
            # Unknown or malformed hashes still cost a hash computation, so timing does not reveal
            # whether an account exists.
            self.burn(password)
            return False
        n, r, p, salt, digest = parsed
        # Hashes made with stronger settings than the current ones need more memory than the
        # configured limit allows.
        memory = max(self._tuning.scrypt_maxmem, 130 * n * r * p)
        candidate = self._derive(password, salt, n, r, p, len(digest), memory)
        return hmac.compare_digest(candidate, digest)

    def burn(self, password: str) -> None:
        """Spend the time of a password check without comparing anything real.

        The first call only prepares a dummy hash of random data. Later calls verify the
        password against that dummy hash.

        Args:
            password: The password text to run the check with.
        """
        if self._dummy is None:
            self._dummy = self.hash(_b64(os.urandom(self._tuning.salt_bytes)))
            return
        self.verify(password, self._dummy)

    def needs_rehash(self, encoded: str) -> bool:
        """Check whether a stored hash should be replaced by a new one.

        Args:
            encoded: The stored hash.

        Returns:
            True if the hash is malformed or its cost settings or digest length differ from
            the current settings.
        """
        parsed = self._parse(encoded)
        if parsed is None:
            return True
        n, r, p, _, digest = parsed
        tuning = self._tuning
        return (n, r, p, len(digest)) != (
            tuning.scrypt_n,
            tuning.scrypt_r,
            tuning.scrypt_p,
            tuning.hash_bytes,
        )

    def problems(self, password: str, *names: str) -> list[str]:
        """Return the reasons why a password is not acceptable.

        The rules are the minimum and maximum length, a blocklist of common passwords and
        fewer than four distinct characters, and containing a name or username of at
        least three characters. Only the first name found is reported.

        Args:
            password: The password text to check.
            names: Names or usernames that the password must not contain.

        Returns:
            Messages for the user, empty when the password is acceptable.
        """
        tuning = self._tuning
        found = []
        if len(password) < tuning.password_min_length:
            found.append(f"Use at least {tuning.password_min_length} characters.")
        if len(password) > tuning.password_max_length:
            found.append(f"Use at most {tuning.password_max_length} characters.")
        lowered = password.lower()
        if lowered in tuning.password_blocklist or len(set(password)) < 4:
            found.append("That password is too easy to guess.")
        for name in names:
            if name and len(name) >= 3 and name.lower() in lowered:
                found.append("Do not use your name or username inside the password.")
                break
        return found
