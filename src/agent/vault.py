# src/agent/vault.py
from cryptography.fernet import (
    Fernet,
    InvalidToken,
)
from pathlib import Path

import secrets
import os

KEY_MODE = 0o600


class Vault:
    """A Fernet encryptor for short secrets whose key lives in a key file.

    The key file is created on first use with mode 0o600. Anyone who can read it can
    decrypt the stored values, so it must be kept out of backups and version control.
    """

    def __init__(self, path: Path):
        """Initialize the vault from a key file, creating the key if it is missing.

        Args:
            path: Location of the key file.
        """
        self._fernet = Fernet(self._load(path))

    @staticmethod
    def _load(path: Path) -> bytes:
        """Read the key file, or create it atomically when it does not exist.

        Parent folders are created as needed. A new key is written to a private temporary
        file and then linked into place, so concurrent callers end up with the same key.

        Args:
            path: Location of the key file.

        Returns:
            The key bytes.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            return path.read_bytes().strip()
        key = Fernet.generate_key()
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}")
        # O_BINARY exists only on Windows, where it stops the key bytes from being newline
        # translated.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(temporary, flags, KEY_MODE)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(key)
            # Linking fails if the file already exists, so two processes creating the key at once
            # cannot overwrite each other.
            os.link(temporary, path)
        except FileExistsError:
            return path.read_bytes().strip()
        finally:
            temporary.unlink(missing_ok=True)
        return key

    def encrypt(self, text: str) -> str:
        """Encrypt text into an ASCII token.

        Args:
            text: The plain text.

        Returns:
            The Fernet token.
        """
        return self._fernet.encrypt(text.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str | None) -> str | None:
        """Decrypt a token produced by encrypt.

        Args:
            token: The Fernet token, or None.

        Returns:
            The plain text, or None when the token is empty, invalid, tampered with, or
            made with a different key.
        """
        if not token:
            return None
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken, ValueError:
            return None
