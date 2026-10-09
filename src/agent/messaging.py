# src/agent/messaging.py
from pydantic import (
    BaseModel,
    Field,
)
from agent.errors import InvalidRequest
from agent.config import MessageTuning
from agent.permissions import expand
from collections.abc import Callable
from agent.db import Database
from agent.vault import Vault
from typing import Any

import hashlib
import secrets
import sqlite3
import json
import time
import re

DIRECT = "direct"
GROUP = "group"
ROLE = "role"
EVERYONE = "all"
AUDIENCES = (EVERYONE, ROLE, GROUP)
UNREADABLE = "[this message cannot be read]"
REMOVED = "[removed]"
DAY = 86_400
MAX_ID = 2**62
CONVERSATION_ID = re.compile(r"[A-Za-z0-9_-]{12}")
MEMBERS_ONLY = "Only a member can change who is in a private group."
PERMISSION = "messages.use"
VISIBLE = (
    "(announcements.audience = 'all' OR (announcements.audience = 'role' AND "
    "announcements.audience_id = ?) OR (announcements.audience = 'group' AND EXISTS ("
    "SELECT 1 FROM members WHERE members.conversation_id = announcements.audience_id "
    "AND members.user_id = ?))) AND (announcements.expires IS NULL OR announcements.expires > ?)"
)


class Person(BaseModel):
    """A user who can be picked as a messaging partner.

    Carries the display name, the role name and whether the user has already
    set up end-to-end encryption keys.
    """

    id: str
    name: str
    role: str
    has_keys: bool = False


class Member(BaseModel):
    """A participant of a group or direct conversation.

    The eligible flag is false when the user can no longer use messaging, for
    example because the account is inactive or lost the permission.
    """

    user_id: str
    name: str
    owner: bool = False
    eligible: bool = True


class Conversation(BaseModel):
    """A conversation as seen by one user, including that user's unread count.

    The title and the owner flag are computed for the viewing user.
    """

    id: str
    kind: str
    title: str
    role_id: str | None = None
    encrypted: bool = False
    key_version: int = 1
    created: float
    created_by: str = ""
    updated: float
    unread: int = 0
    owner: bool = False


class Message(BaseModel):
    """A chat message as returned to one viewing user.

    Removed messages show a placeholder text. In encrypted conversations the
    text stays empty and the cipher field carries the ciphertext, which only
    the clients can decrypt.
    """

    id: int
    conversation_id: str
    sender_id: str | None = None
    sender_name: str
    text: str = ""
    cipher: str = ""
    encrypted: bool = False
    version: int = 1
    ts: float
    removed: bool = False
    mine: bool = False


class Announcement(BaseModel):
    """An announcement with its audience and the viewer's read state."""

    id: str
    title: str
    body: str
    audience: str
    audience_id: str = ""
    audience_label: str = ""
    author_id: str | None = None
    author_name: str
    created: float
    expires: float | None = None
    pinned: bool = False
    read: bool = False


class Keys(BaseModel):
    """The stored end-to-end key material of one user.

    The private key is kept only in the wrapped form sent by the client, so the
    server cannot use it.
    """

    public_key: str
    wrapped_private: str
    salt: str
    iterations: int
    fingerprint: str
    updated: float


class Wrap(BaseModel):
    """A conversation key wrapped for one member.

    Both fields are opaque client-made strings capped at 100000 characters.
    The sender key is the public key of the member who made the wrap.
    """

    wrapped: str = Field(max_length=100_000)
    sender_key: str = Field(max_length=100_000)


def _json(value: Any) -> str:
    """Serialize a value to compact JSON text.

    Args:
        value: JSON-serializable value.

    Returns:
        JSON without extra spaces; non-ASCII characters are kept as they are.
    """
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def canonical_key(raw: str) -> str | None:
    """Reduce a JSON public key to its canonical form.

    Args:
        raw: JSON text of a public key with crv, kty, x and y fields.

    Returns:
        Compact JSON holding only those four fields in a fixed order, or None
        when the text is not valid JSON or a field is missing.
    """
    try:
        parsed = json.loads(raw)
        return _json({key: parsed[key] for key in ("crv", "kty", "x", "y")})
    except ValueError, KeyError, TypeError:
        return None


def fingerprint_of(public_key: dict[str, Any]) -> str:
    """Compute a short readable fingerprint of a public key.

    Args:
        public_key: Parsed public key containing crv, kty, x and y.

    Returns:
        The first 32 hex characters of the SHA-256 of the canonical key JSON,
        split into groups of four separated by spaces.
    """
    canonical = _json(
        {
            "crv": public_key["crv"],
            "kty": public_key["kty"],
            "x": public_key["x"],
            "y": public_key["y"],
        }
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return " ".join(digest[i : i + 4] for i in range(0, len(digest), 4))


class Messenger:
    """Service for chats, groups, role channels and announcements.

    Every operation first checks that the acting user is active and holds the
    messages.use permission. Plain text is encrypted at rest with the vault;
    end-to-end conversations store only client-made ciphertext, which the
    server cannot read.
    """

    def __init__(
        self,
        database: Database,
        vault: Vault,
        tuning: MessageTuning,
        clock: Callable[[], float] = time.time,
    ):
        """Initialize the messenger.

        Args:
            database: Database used for all reads and writes.
            vault: Vault that encrypts and decrypts plain text bodies at rest.
            tuning: Limits and sizes for messages, groups and announcements.
            clock: Function returning the current time in seconds since the epoch.
        """
        self._db = database
        self._vault = vault
        self._tuning = tuning
        self._clock = clock

    def _seal(self, text: str) -> str:
        """Encrypt a plain text body with the vault.

        Args:
            text: Plain text to protect.

        Returns:
            The encrypted body ready to be stored.
        """
        return self._vault.encrypt(text)

    def _open(self, body: str) -> str:
        """Decrypt a stored body, falling back to a placeholder.

        Args:
            body: Body encrypted by the vault.

        Returns:
            The plain text, or a placeholder when it cannot be decrypted.
        """
        return self._vault.decrypt(body) or UNREADABLE

    def _clean(self, text: str, limit: int) -> str:
        """Normalize line breaks, trim and truncate user text.

        Args:
            text: Raw text from the user.
            limit: Maximum number of characters to keep.

        Returns:
            The text with Windows line breaks converted, trimmed and cut to limit.
        """
        cleaned = text.replace("\r\n", "\n").strip()
        return cleaned[:limit]

    def _allowed_roles(self, db: sqlite3.Connection, permission: str = PERMISSION) -> set[str]:
        """Return the ids of the roles that grant a permission.

        Roles whose stored permission list cannot be parsed grant nothing.

        Args:
            db: Open database connection.
            permission: Permission name to look for, messages.use by default.

        Returns:
            The set of role ids that include the permission.
        """
        allowed = set()
        for row in db.execute("SELECT id, permissions FROM roles").fetchall():
            try:
                granted = expand(json.loads(row["permissions"] or "[]"))
            except ValueError:
                granted = frozenset()
            if permission in granted:
                allowed.add(row["id"])
        return allowed

    def _user_row(self, db: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
        """Fetch a user together with the name, role and permissions.

        Args:
            db: Open database connection.
            user_id: Id of the user.

        Returns:
            A row with id, status, role_id, name, role_name and permissions, or
            None when the user does not exist.
        """
        row = db.execute(
            "SELECT users.id AS id, users.status AS status, users.role_id AS role_id, "
            "COALESCE(NULLIF(users.display_name, ''), users.username) AS name, "
            "roles.name AS role_name, roles.permissions AS permissions "
            "FROM users JOIN roles ON roles.id = users.role_id WHERE users.id = ?",
            (user_id,),
        ).fetchone()
        return row

    def _can_message(self, row: sqlite3.Row | None) -> bool:
        """Check whether a user row may use messaging.

        Args:
            row: Row from the user lookup, or None.

        Returns:
            True when the user is active and the role grants messages.use; false
            for a missing row, an inactive user or unreadable permissions.
        """
        if row is None or row["status"] != "active":
            return False
        try:
            return PERMISSION in expand(json.loads(row["permissions"] or "[]"))
        except ValueError:
            return False

    def people(self, user_id: str) -> list[Person]:
        """List the active users that a user can start a conversation with.

        Args:
            user_id: Id of the asking user, who is left out of the result.

        Returns:
            People whose role grants messaging, sorted by name and capped at the
            directory size. Empty when no role grants messaging.
        """
        with self._db.transaction() as db:
            roles = sorted(self._allowed_roles(db))
            if not roles:
                return []
            marks = ",".join("?" for _ in roles)
            rows = db.execute(
                "SELECT users.id AS id, COALESCE(NULLIF(users.display_name, ''), users.username) "
                "AS name, roles.name AS role, users.role_id AS role_id, "
                "EXISTS (SELECT 1 FROM message_keys WHERE message_keys.user_id = users.id) AS keys "
                "FROM users JOIN roles ON roles.id = users.role_id "
                "WHERE users.status = 'active' AND users.id != ? AND users.role_id IN ("
                + marks
                + ") ORDER BY name COLLATE NOCASE LIMIT ?",
                (user_id, *roles, self._tuning.directory_size),
            ).fetchall()
        return [
            Person(id=row["id"], name=row["name"], role=row["role"], has_keys=bool(row["keys"]))
            for row in rows
        ]

    def save_keys(
        self,
        user_id: str,
        public_key: str,
        wrapped_private: str,
        salt: str,
        iterations: int,
        replace: bool = False,
    ) -> Keys:
        """Validate and store the end-to-end keys of a user.

        The public key must be a P-256 EC key and is stored in canonical form.
        Replacing existing keys also deletes the user's wrapped conversation keys.

        Args:
            user_id: Id of the key owner.
            public_key: JSON text of the public key.
            wrapped_private: Private key wrapped by the client with its passphrase.
            salt: Salt used by the client for the key derivation.
            iterations: Iteration count used by the client for the key derivation.
            replace: Whether existing keys may be overwritten.

        Returns:
            The stored keys.

        Raises:
            InvalidRequest: when a size, salt, iteration or key check fails, the
                account cannot use messaging, keys exist and replace is false, or
                the saved keys cannot be read back.
        """
        tuning = self._tuning
        if len(public_key) > tuning.max_key_chars or len(wrapped_private) > tuning.max_blob_chars:
            raise InvalidRequest("The key material is too large.")
        if len(salt) > 200 or not salt:
            raise InvalidRequest("The key material is not valid.")
        if not tuning.min_iterations <= iterations <= tuning.kdf_iterations * 20:
            raise InvalidRequest("The key derivation is too weak.")
        try:
            parsed = json.loads(public_key)
            valid = (
                isinstance(parsed, dict)
                and parsed.get("kty") == "EC"
                and parsed.get("crv") == "P-256"
                and isinstance(parsed.get("x"), str)
                and isinstance(parsed.get("y"), str)
                and len(parsed["x"]) < 100
                and len(parsed["y"]) < 100
            )
        except ValueError:
            valid = False
        if not valid:
            raise InvalidRequest("The public key is not valid.")
        canonical = _json({key: parsed[key] for key in ("crv", "kty", "x", "y")})
        now = self._clock()
        with self._db.transaction() as db:
            if not self._can_message(self._user_row(db, user_id)):
                raise InvalidRequest("Messaging is not available for this account.")
            exists = db.execute(
                "SELECT 1 FROM message_keys WHERE user_id = ?", (user_id,)
            ).fetchone()
            if exists and not replace:
                raise InvalidRequest("Keys already exist. Reset them to make new ones.")
            db.execute(
                "INSERT INTO message_keys (user_id, public_key, wrapped_private, salt, "
                "iterations, fingerprint, created, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET public_key = excluded.public_key, "
                "wrapped_private = excluded.wrapped_private, salt = excluded.salt, "
                "iterations = excluded.iterations, fingerprint = excluded.fingerprint, "
                "updated = excluded.updated",
                (
                    user_id,
                    canonical,
                    wrapped_private,
                    salt,
                    iterations,
                    fingerprint_of(parsed),
                    now,
                    now,
                ),
            )
            if exists:
                db.execute("DELETE FROM conversation_keys WHERE user_id = ?", (user_id,))
        found = self.keys_of(user_id)
        if found is None:
            raise InvalidRequest("The keys could not be saved.")
        return found

    def keys_of(self, user_id: str) -> Keys | None:
        """Return the stored key material of a user.

        Args:
            user_id: Id of the user.

        Returns:
            The keys, or None when the user has not set any up.
        """
        row = self._db.one(
            "SELECT public_key, wrapped_private, salt, iterations, fingerprint, updated "
            "FROM message_keys WHERE user_id = ?",
            (user_id,),
        )
        return Keys.model_validate(dict(row)) if row else None

    def public_key_of(self, user_id: str) -> dict[str, str] | None:
        """Return the public key and fingerprint of an active user.

        Args:
            user_id: Id of the user.

        Returns:
            A dict with public_key and fingerprint, or None when the user has no
            keys or is not active.
        """
        row = self._db.one(
            "SELECT public_key, fingerprint FROM message_keys "
            "JOIN users ON users.id = message_keys.user_id WHERE message_keys.user_id = ? "
            "AND users.status = 'active'",
            (user_id,),
        )
        return dict(row) if row else None

    def _wraps(
        self, db: sqlite3.Connection, raw: dict[str, Any] | None, expected: set[str], sender: str
    ) -> dict[str, Wrap]:
        """Validate the wrapped conversation keys sent by a client.

        Args:
            db: Open database connection.
            raw: Mapping of user id to wrap data as received.
            expected: User ids that must each have exactly one wrap.
            sender: Id of the user who made the wraps.

        Returns:
            The parsed wraps keyed by user id.

        Raises:
            InvalidRequest: when the user ids differ from the expected ones, a wrap
                is malformed or too large, or a wrap was not made with the key
                registered by the sender.
        """
        if not isinstance(raw, dict) or set(raw) != expected:
            raise InvalidRequest("Every member needs a key for an encrypted conversation.")
        try:
            wraps = {user: Wrap.model_validate(item) for user, item in raw.items()}
        except ValueError as error:
            raise InvalidRequest("The conversation keys are not valid.") from error
        limit = self._tuning.max_blob_chars
        if any(
            len(item.wrapped) > limit or len(item.sender_key) > limit for item in wraps.values()
        ):
            raise InvalidRequest("The conversation keys are too large.")
        registered = db.execute(
            "SELECT public_key FROM message_keys WHERE user_id = ?", (sender,)
        ).fetchone()
        # Every wrap must be made with the sender's own registered key, so a client cannot attach a
        # key that does not belong to the sender.
        if registered is None or any(
            canonical_key(item.sender_key) != registered["public_key"] for item in wraps.values()
        ):
            raise InvalidRequest("The conversation keys were not made with your own key.")
        return wraps

    def peer_keys(self, user_id: str, conversation_id: str) -> dict[str, dict[str, Any]] | None:
        """Return the public keys of everyone in a conversation.

        Only members who have set up keys are included.

        Args:
            user_id: Id of the asking user.
            conversation_id: Id of the conversation.

        Returns:
            A mapping of member id to public_key, fingerprint and eligible, or
            None when the user has no access or the conversation is a role channel.
        """
        with self._db.transaction() as db:
            row = self._row_access(db, user_id, conversation_id)
            if row is None or row["kind"] == ROLE:
                return None
            rows = db.execute(
                "SELECT members.user_id AS id, message_keys.public_key AS public_key, "
                "message_keys.fingerprint AS fingerprint FROM members JOIN message_keys ON "
                "message_keys.user_id = members.user_id WHERE members.conversation_id = ?",
                (conversation_id,),
            ).fetchall()
            return {
                item["id"]: {
                    "public_key": item["public_key"],
                    "fingerprint": item["fingerprint"],
                    "eligible": self._can_message(self._user_row(db, item["id"])),
                }
                for item in rows
            }

    def _eligible(self, db: sqlite3.Connection, user_ids: set[str]) -> set[str]:
        """Filter user ids down to those who can use messaging.

        Args:
            db: Open database connection.
            user_ids: Ids to check.

        Returns:
            The ids of users who are active and hold the messaging permission.
        """
        return {item for item in user_ids if self._can_message(self._user_row(db, item))}

    def _rekey(
        self,
        db: sqlite3.Connection,
        row: sqlite3.Row,
        keep: set[str],
        raw: dict[str, Any] | None,
        sender: str,
    ) -> int:
        """Move a conversation to a new key version for a set of members.

        Stores the new wraps, bumps the key version and removes every member and
        wrapped key outside the keep set.

        Args:
            db: Open database connection.
            row: Conversation row.
            keep: Ids of the members who stay.
            raw: New wraps for the kept members as received from the client.
            sender: Id of the user who made the wraps.

        Returns:
            The new key version.

        Raises:
            InvalidRequest: when a kept member has no keys or the wraps are invalid.
        """
        self._require_keys(db, keep)
        version = row["key_version"] + 1
        self._store_wraps(db, row["id"], version, self._wraps(db, raw, keep, sender))
        db.execute("UPDATE conversations SET key_version = ? WHERE id = ?", (version, row["id"]))
        marks = ",".join("?" for _ in keep)
        for table in ("members", "conversation_keys"):
            db.execute(
                "DELETE FROM "
                + table
                + " WHERE conversation_id = ? AND user_id NOT IN ("
                + marks
                + ")",
                (row["id"], *keep),
            )
        return version

    def rotate_key(self, user_id: str, conversation_id: str, wraps: dict[str, Any] | None) -> int:
        """Replace the key of an encrypted conversation.

        Members who can no longer use messaging are dropped from the conversation.

        Args:
            user_id: Id of the member rotating the key.
            conversation_id: Id of the conversation.
            wraps: New wrapped keys for every remaining member.

        Returns:
            The new key version.

        Raises:
            InvalidRequest: when the conversation is missing, not encrypted or a
                role channel, the user is no longer eligible, a direct partner is no
                longer eligible, or the wraps are invalid.
        """
        with self._db.transaction() as db:
            row = self._row_access(db, user_id, conversation_id)
            if row is None or not row["encrypted"] or row["kind"] == ROLE:
                raise InvalidRequest("That conversation cannot change its key.")
            members = {
                item["user_id"]
                for item in db.execute(
                    "SELECT user_id FROM members WHERE conversation_id = ?", (conversation_id,)
                ).fetchall()
            }
            keep = self._eligible(db, members)
            if user_id not in keep or (row["kind"] == DIRECT and keep != members):
                raise InvalidRequest("Everyone in it must still be able to take part.")
            return self._rekey(db, row, keep, wraps, user_id)

    def _new_id(self, db: sqlite3.Connection, wanted: str | None, encrypted: bool) -> str:
        """Choose the id for a new conversation.

        Args:
            db: Open database connection.
            wanted: Id proposed by the client.
            encrypted: Whether the conversation is end-to-end encrypted.

        Returns:
            The proposed id for an encrypted conversation, otherwise a random id.

        Raises:
            InvalidRequest: when the proposed id has the wrong shape or is taken.
        """
        if not (encrypted and wanted):
            return secrets.token_urlsafe(9)
        taken = db.execute("SELECT 1 FROM conversations WHERE id = ?", (wanted,)).fetchone()
        if not CONVERSATION_ID.fullmatch(wanted) or taken:
            raise InvalidRequest("That conversation could not be created; try again.")
        return wanted

    def _store_wraps(
        self, db: sqlite3.Connection, conversation_id: str, version: int, wraps: dict[str, Wrap]
    ) -> None:
        """Store the wrapped conversation keys of a key version.

        Existing entries for the same member and version are replaced.

        Args:
            db: Open database connection.
            conversation_id: Id of the conversation.
            version: Key version the wraps belong to.
            wraps: Wraps keyed by member id.
        """
        for user_id, wrap in wraps.items():
            db.execute(
                "INSERT OR REPLACE INTO conversation_keys (conversation_id, user_id, version, "
                "wrapped, sender_key) VALUES (?, ?, ?, ?, ?)",
                (conversation_id, user_id, version, wrap.wrapped, wrap.sender_key),
            )

    def _require_keys(self, db: sqlite3.Connection, user_ids: set[str]) -> None:
        """Ensure that every given user has registered keys.

        Args:
            db: Open database connection.
            user_ids: Ids of the users to check.

        Raises:
            InvalidRequest: when at least one user has no keys.
        """
        marks = ",".join("?" for _ in user_ids)
        found = db.execute(
            "SELECT COUNT(*) AS n FROM message_keys WHERE user_id IN (" + marks + ")",
            tuple(user_ids),
        ).fetchone()["n"]
        if found != len(user_ids):
            raise InvalidRequest("Everyone needs private keys first. Ask them to set theirs up.")

    def direct(
        self,
        user_id: str,
        other_id: str,
        encrypted: bool = False,
        wraps: dict[str, Any] | None = None,
        conversation_id: str | None = None,
    ) -> Conversation:
        """Open or create the direct conversation between two users.

        An existing conversation for the same pair and the same encryption mode is
        reused instead of creating a new one.

        Args:
            user_id: Id of the user opening the conversation.
            other_id: Id of the other participant.
            encrypted: Whether the conversation is end-to-end encrypted.
            wraps: Wrapped keys for both users, required when creating an
                encrypted conversation.
            conversation_id: Id chosen by the client for a new encrypted one.

        Returns:
            The conversation as seen by user_id.

        Raises:
            InvalidRequest: when both ids are the same, either user cannot be
                messaged, keys or wraps are missing or invalid, the id is not
                acceptable, or the conversation cannot be opened.
        """
        if user_id == other_id:
            raise InvalidRequest("Choose someone else.")
        pair = ":".join(sorted((user_id, other_id))) + (":e2ee" if encrypted else ":plain")
        now = self._clock()
        with self._db.transaction() as db:
            if not (
                self._can_message(self._user_row(db, user_id))
                and self._can_message(self._user_row(db, other_id))
            ):
                raise InvalidRequest("That person cannot be messaged.")
            row = db.execute("SELECT id FROM conversations WHERE pair = ?", (pair,)).fetchone()
            if row:
                identifier = row["id"]
            else:
                identifier = self._new_id(db, conversation_id, encrypted)
                prepared = None
                if encrypted:
                    self._require_keys(db, {user_id, other_id})
                    prepared = self._wraps(db, wraps, {user_id, other_id}, user_id)
                db.execute(
                    "INSERT INTO conversations (id, kind, title, encrypted, pair, created, "
                    "created_by, updated) VALUES (?, 'direct', '', ?, ?, ?, ?, ?)",
                    (identifier, int(encrypted), pair, now, user_id, now),
                )
                for member in (user_id, other_id):
                    db.execute(
                        "INSERT INTO members (conversation_id, user_id, owner, joined) "
                        "VALUES (?, ?, 0, ?)",
                        (identifier, member, now),
                    )
                if prepared:
                    self._store_wraps(db, identifier, 1, prepared)
        found = self.conversation(user_id, identifier)
        if found is None:
            raise InvalidRequest("The conversation could not be opened.")
        return found

    def create_group(
        self,
        owner_id: str,
        title: str,
        member_ids: list[str],
        encrypted: bool = False,
        wraps: dict[str, Any] | None = None,
        conversation_id: str | None = None,
    ) -> Conversation:
        """Create a group conversation owned by one user.

        The title is trimmed, its whitespace is collapsed and it is cut to the
        configured length.

        Args:
            owner_id: Id of the creator, who becomes the owner and a member.
            title: Name of the group.
            member_ids: Ids of the other members.
            encrypted: Whether the group is end-to-end encrypted.
            wraps: Wrapped keys for all members, required when encrypted.
            conversation_id: Id chosen by the client for a new encrypted group.

        Returns:
            The new group as seen by the owner.

        Raises:
            InvalidRequest: when the name is too short, there is no other member,
                the group or the owner's group count is over the limit, someone
                cannot be messaged, keys or wraps are invalid, or creation fails.
        """
        tuning = self._tuning
        label = " ".join(title.split())[: tuning.title_chars]
        if len(label) < 2:
            raise InvalidRequest("Give the group a name.")
        members = {owner_id, *member_ids}
        if len(members) < 2:
            raise InvalidRequest("A group needs at least one other person.")
        if len(members) > tuning.max_members:
            raise InvalidRequest("The group is too large.")
        now = self._clock()
        with self._db.transaction() as db:
            identifier = self._new_id(db, conversation_id, encrypted)
            owned = db.execute(
                "SELECT COUNT(*) AS n FROM conversations WHERE kind = 'group' AND created_by = ?",
                (owner_id,),
            ).fetchone()["n"]
            if owned >= tuning.max_groups_per_user:
                raise InvalidRequest("You have too many groups.")
            for member in members:
                if not self._can_message(self._user_row(db, member)):
                    raise InvalidRequest("Someone in the list cannot be messaged.")
            prepared = None
            if encrypted:
                self._require_keys(db, members)
                prepared = self._wraps(db, wraps, members, owner_id)
            db.execute(
                "INSERT INTO conversations (id, kind, title, encrypted, created, created_by, "
                "updated) VALUES (?, 'group', ?, ?, ?, ?, ?)",
                (identifier, label, int(encrypted), now, owner_id, now),
            )
            for member in members:
                db.execute(
                    "INSERT INTO members (conversation_id, user_id, owner, joined) "
                    "VALUES (?, ?, ?, ?)",
                    (identifier, member, int(member == owner_id), now),
                )
            if prepared:
                self._store_wraps(db, identifier, 1, prepared)
        found = self.conversation(owner_id, identifier)
        if found is None:
            raise InvalidRequest("The group could not be created.")
        return found

    def role_channel(self, role_id: str) -> str | None:
        """Return the id of the channel of a role, creating it if needed.

        Args:
            role_id: Id of the role.

        Returns:
            The channel id, or None when the role does not exist.
        """
        with self._db.transaction() as db:
            if db.execute("SELECT 1 FROM roles WHERE id = ?", (role_id,)).fetchone() is None:
                return None
            return self.role_channel_row(db, role_id)

    def _row_access(
        self, db: sqlite3.Connection, user_id: str, conversation_id: str
    ) -> sqlite3.Row | None:
        """Fetch a conversation row if the user is allowed to see it.

        Args:
            db: Open database connection.
            user_id: Id of the user.
            conversation_id: Id of the conversation.

        Returns:
            The row with is_owner, last_read and member columns added, or None when
            it is missing, the user cannot use messaging, the user is not a member,
            or a role channel belongs to a different role.
        """
        row = db.execute(
            "SELECT conversations.*, COALESCE(members.owner, 0) AS is_owner, "
            "COALESCE(members.last_read, 0) AS last_read, members.user_id AS member "
            "FROM conversations LEFT JOIN members ON members.conversation_id = conversations.id "
            "AND members.user_id = ? WHERE conversations.id = ?",
            (user_id, conversation_id),
        ).fetchone()
        if row is None:
            return None
        person = self._user_row(db, user_id)
        if not self._can_message(person):
            return None
        if row["kind"] == ROLE:
            return row if person is not None and person["role_id"] == row["role_id"] else None
        return row if row["member"] else None

    def conversation(self, user_id: str, conversation_id: str) -> Conversation | None:
        """Return one conversation as seen by a user.

        Args:
            user_id: Id of the user.
            conversation_id: Id of the conversation.

        Returns:
            The conversation, or None when the user has no access.
        """
        with self._db.transaction() as db:
            row = self._row_access(db, user_id, conversation_id)
            if row is None:
                return None
            return self._conversation(db, row, user_id)

    def _title(self, db: sqlite3.Connection, row: sqlite3.Row, user_id: str) -> str:
        """Return the title of a conversation for one user.

        Args:
            db: Open database connection.
            row: Conversation row.
            user_id: Id of the viewing user.

        Returns:
            The stored title, or for a direct conversation the name of the other
            participant, or a fallback name when that person is gone.
        """
        if row["kind"] != DIRECT:
            return row["title"]
        other = db.execute(
            "SELECT COALESCE(NULLIF(users.display_name, ''), users.username) AS name "
            "FROM members JOIN users ON users.id = members.user_id "
            "WHERE members.conversation_id = ? AND members.user_id != ?",
            (row["id"], user_id),
        ).fetchone()
        return other["name"] if other else "Former colleague"

    def _conversation(self, db: sqlite3.Connection, row: sqlite3.Row, user_id: str) -> Conversation:
        """Build the conversation model with the unread count of a user.

        Unread messages are those after the user's read marker that are not
        removed and were not sent by the user.

        Args:
            db: Open database connection.
            row: Conversation row from the access lookup.
            user_id: Id of the viewing user.

        Returns:
            The conversation model.
        """
        unread = db.execute(
            "SELECT COUNT(*) AS n FROM chat_messages WHERE conversation_id = ? AND id > ? "
            "AND removed = 0 AND (sender_id IS NULL OR sender_id != ?)",
            (row["id"], row["last_read"], user_id),
        ).fetchone()["n"]
        return Conversation(
            id=row["id"],
            kind=row["kind"],
            title=self._title(db, row, user_id),
            role_id=row["role_id"],
            encrypted=bool(row["encrypted"]),
            key_version=row["key_version"],
            created=row["created"],
            created_by=row["created_by"],
            updated=row["updated"],
            unread=unread,
            owner=bool(row["is_owner"]),
        )

    def conversations(self, user_id: str) -> list[Conversation]:
        """List the conversations of a user, most recently active first.

        Includes the user's own groups and direct chats and the channel of the
        user's role, which is created when it does not exist yet.

        Args:
            user_id: Id of the user.

        Returns:
            The conversations, empty when the user cannot use messaging.
        """
        with self._db.transaction() as db:
            person = self._user_row(db, user_id)
            if not self._can_message(person) or person is None:
                return []
            channel = self.role_channel_row(db, person["role_id"])
            rows = db.execute(
                "SELECT conversations.*, COALESCE(members.owner, 0) AS is_owner, "
                "COALESCE(members.last_read, 0) AS last_read, members.user_id AS member "
                "FROM conversations LEFT JOIN members ON members.conversation_id = "
                "conversations.id AND members.user_id = ? "
                "WHERE (conversations.kind != 'role' AND members.user_id IS NOT NULL) "
                "OR conversations.id = ? ORDER BY conversations.updated DESC",
                (user_id, channel),
            ).fetchall()
            return [self._conversation(db, row, user_id) for row in rows]

    def role_channel_row(self, db: sqlite3.Connection, role_id: str) -> str:
        """Return the id of a role channel, creating it when missing.

        The role existence is not checked here; a missing role gets a generic title.

        Args:
            db: Open database connection.
            role_id: Id of the role.

        Returns:
            The channel id.
        """
        row = db.execute(
            "SELECT id FROM conversations WHERE kind = 'role' AND role_id = ?", (role_id,)
        ).fetchone()
        if row:
            return row["id"]
        role = db.execute("SELECT name FROM roles WHERE id = ?", (role_id,)).fetchone()
        identifier = secrets.token_urlsafe(9)
        now = self._clock()
        db.execute(
            "INSERT INTO conversations (id, kind, title, role_id, created, updated) "
            "VALUES (?, 'role', ?, ?, ?, ?)",
            (identifier, role["name"] if role else "Role", role_id, now, now),
        )
        return identifier

    def members(self, user_id: str, conversation_id: str) -> list[Member]:
        """List the members of a group or direct conversation.

        Args:
            user_id: Id of the asking user.
            conversation_id: Id of the conversation.

        Returns:
            Members with owners first, then sorted by name. Empty when the user has
            no access or the conversation is a role channel.
        """
        with self._db.transaction() as db:
            row = self._row_access(db, user_id, conversation_id)
            if row is None or row["kind"] == ROLE:
                return []
            rows = db.execute(
                "SELECT members.user_id AS id, members.owner AS owner, "
                "COALESCE(NULLIF(users.display_name, ''), users.username) AS name "
                "FROM members JOIN users ON users.id = members.user_id "
                "WHERE members.conversation_id = ? "
                "ORDER BY members.owner DESC, name COLLATE NOCASE",
                (conversation_id,),
            ).fetchall()
            eligible = self._eligible(db, {item["id"] for item in rows})
        return [
            Member(
                user_id=row["id"],
                name=row["name"],
                owner=bool(row["owner"]),
                eligible=row["id"] in eligible,
            )
            for row in rows
        ]

    def send(
        self,
        user_id: str,
        conversation_id: str,
        text: str = "",
        cipher: str = "",
        version: int = 1,
    ) -> Message:
        """Post a message to a conversation.

        Plain text is trimmed, cut to the maximum length and encrypted with the
        vault. Ciphertext for encrypted conversations is stored as received. The
        sender's read marker moves to the new message.

        Args:
            user_id: Id of the sender.
            conversation_id: Id of the conversation.
            text: Message text for conversations that are not end-to-end encrypted.
            cipher: JSON with iv and ct fields for end-to-end encrypted ones.
            version: Key version the ciphertext was made with.

        Returns:
            The stored message as seen by the sender.

        Raises:
            InvalidRequest: when the conversation is not accessible, the text is
                empty, the ciphertext is missing, too large or malformed, or the
                key version is not the current one.
        """
        tuning = self._tuning
        now = self._clock()
        with self._db.transaction() as db:
            row = self._row_access(db, user_id, conversation_id)
            if row is None:
                raise InvalidRequest("That conversation does not exist.")
            person = self._user_row(db, user_id)
            name = person["name"] if person else "Unknown"
            if row["encrypted"]:
                if not cipher or len(cipher) > tuning.max_cipher_chars:
                    raise InvalidRequest("The encrypted message is missing or too large.")
                # Ciphertext made with an old key version is rejected so nothing is sent under a key
                # that was rotated away.
                if version != row["key_version"]:
                    raise InvalidRequest("The conversation key changed; reload and send again.")
                try:
                    parsed = json.loads(cipher)
                    valid = (
                        isinstance(parsed, dict)
                        and isinstance(parsed.get("iv"), str)
                        and isinstance(parsed.get("ct"), str)
                    )
                except ValueError:
                    valid = False
                if not valid:
                    raise InvalidRequest("The encrypted message is not valid.")
                body = cipher
            else:
                cleaned = self._clean(text, tuning.max_chars)
                if not cleaned:
                    raise InvalidRequest("Write something first.")
                body = self._seal(cleaned)
            cursor = db.execute(
                "INSERT INTO chat_messages (conversation_id, sender_id, sender_name, body, "
                "encrypted, version, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    user_id,
                    name,
                    body,
                    row["encrypted"],
                    row["key_version"],
                    now,
                ),
            )
            identifier = cursor.lastrowid
            db.execute("UPDATE conversations SET updated = ? WHERE id = ?", (now, conversation_id))
            self._touch_read(db, user_id, conversation_id, identifier or 0, now)
            stored = db.execute(
                "SELECT * FROM chat_messages WHERE id = ?", (identifier,)
            ).fetchone()
        return self._message(stored, user_id)

    def _touch_read(
        self, db: sqlite3.Connection, user_id: str, conversation_id: str, upto: int, now: float
    ) -> None:
        """Advance the read marker of a user in a conversation.

        Creates the membership row when it is missing, which is the case for role
        channels, and never moves the marker backwards.

        Args:
            db: Open database connection.
            user_id: Id of the user.
            conversation_id: Id of the conversation.
            upto: Id of the newest message that counts as read.
            now: Current time in seconds.
        """
        db.execute(
            "INSERT INTO members (conversation_id, user_id, owner, joined, last_read) "
            "VALUES (?, ?, 0, ?, ?) ON CONFLICT(conversation_id, user_id) DO UPDATE SET "
            "last_read = MAX(last_read, excluded.last_read)",
            (conversation_id, user_id, now, upto),
        )

    def _message(self, row: sqlite3.Row, user_id: str) -> Message:
        """Convert a stored message row into a Message for one user.

        Args:
            row: Row from the chat messages table.
            user_id: Id of the viewing user.

        Returns:
            The message with a placeholder text when removed, the ciphertext when
            encrypted, or the decrypted text otherwise.
        """
        removed = bool(row["removed"])
        encrypted = bool(row["encrypted"])
        return Message(
            id=row["id"],
            conversation_id=row["conversation_id"],
            sender_id=row["sender_id"],
            sender_name=row["sender_name"],
            text=REMOVED if removed else ("" if encrypted else self._open(row["body"])),
            cipher="" if removed or not encrypted else row["body"],
            encrypted=encrypted,
            version=row["version"],
            ts=row["ts"],
            removed=removed,
            mine=row["sender_id"] == user_id,
        )

    def feed(
        self, user_id: str, conversation_id: str, after: int = 0, limit: int | None = None
    ) -> list[Message] | None:
        """Return messages of a conversation in ascending id order.

        Args:
            user_id: Id of the asking user.
            conversation_id: Id of the conversation.
            after: Only messages with a larger id are returned; 0 returns the
                newest ones.
            limit: Maximum number of messages, the configured feed size by default.

        Returns:
            The messages oldest first, or None when the user has no access.
        """
        size = limit or self._tuning.feed_size
        with self._db.transaction() as db:
            row = self._row_access(db, user_id, conversation_id)
            if row is None:
                return None
            if after:
                rows = db.execute(
                    "SELECT * FROM chat_messages WHERE conversation_id = ? AND id > ? "
                    "ORDER BY id LIMIT ?",
                    (conversation_id, after, size),
                ).fetchall()
            else:
                rows = list(
                    reversed(
                        db.execute(
                            "SELECT * FROM chat_messages WHERE conversation_id = ? "
                            "ORDER BY id DESC LIMIT ?",
                            (conversation_id, size),
                        ).fetchall()
                    )
                )
        return [self._message(item, user_id) for item in rows]

    def mark_read(self, user_id: str, conversation_id: str, upto: int) -> bool:
        """Mark the messages of a conversation as read up to an id.

        The id is clamped to the range from 0 to the newest message of the
        conversation.

        Args:
            user_id: Id of the user.
            conversation_id: Id of the conversation.
            upto: Id of the newest message that has been read.

        Returns:
            True when the marker was updated, false when the user has no access.
        """
        with self._db.transaction() as db:
            if self._row_access(db, user_id, conversation_id) is None:
                return False
            newest = db.execute(
                "SELECT COALESCE(MAX(id), 0) AS n FROM chat_messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()["n"]
            self._touch_read(
                db, user_id, conversation_id, max(0, min(upto, newest, MAX_ID)), self._clock()
            )
        return True

    def wrapped_keys(self, user_id: str, conversation_id: str) -> list[dict[str, Any]] | None:
        """Return the wrapped conversation keys that belong to a user.

        Args:
            user_id: Id of the user.
            conversation_id: Id of the conversation.

        Returns:
            A list of dicts with version, wrapped and sender_key ordered by version,
            or None when the user has no access.
        """
        with self._db.transaction() as db:
            if self._row_access(db, user_id, conversation_id) is None:
                return None
            rows = db.execute(
                "SELECT version, wrapped, sender_key FROM conversation_keys "
                "WHERE conversation_id = ? AND user_id = ? ORDER BY version",
                (conversation_id, user_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def remove_message(self, user_id: str, message_id: int, moderator: bool = False) -> bool:
        """Remove a message by marking it removed and erasing its body.

        Args:
            user_id: Id of the user asking for the removal.
            message_id: Id of the message.
            moderator: Whether the user may remove messages of other people.

        Returns:
            True when the message was removed, false when it does not exist, the
            user has no access to it or may not remove it.
        """
        with self._db.transaction() as db:
            row = db.execute("SELECT * FROM chat_messages WHERE id = ?", (message_id,)).fetchone()
            if row is None or self._row_access(db, user_id, row["conversation_id"]) is None:
                return False
            if row["sender_id"] != user_id and not moderator:
                return False
            db.execute(
                "UPDATE chat_messages SET removed = 1, body = '' WHERE id = ?", (message_id,)
            )
        return True

    def _manage(
        self, db: sqlite3.Connection, user_id: str, conversation_id: str, moderator: bool
    ) -> sqlite3.Row:
        """Fetch a group row that the user is allowed to change.

        Args:
            db: Open database connection.
            user_id: Id of the user.
            conversation_id: Id of the group.
            moderator: Whether the user may manage groups they do not own.

        Returns:
            The group row with is_owner and member columns.

        Raises:
            InvalidRequest: when the group does not exist or the user is neither an
                owner nor a moderator.
        """
        row = db.execute(
            "SELECT conversations.*, COALESCE(members.owner, 0) AS is_owner, "
            "members.user_id AS member FROM conversations "
            "LEFT JOIN members ON members.conversation_id = conversations.id "
            "AND members.user_id = ? WHERE conversations.id = ? "
            "AND conversations.kind = 'group'",
            (user_id, conversation_id),
        ).fetchone()
        if row is None or not (row["is_owner"] or moderator):
            raise InvalidRequest("You cannot change this group.")
        return row

    def add_members(
        self,
        user_id: str,
        conversation_id: str,
        member_ids: list[str],
        moderator: bool = False,
        wraps: dict[str, Any] | None = None,
    ) -> int:
        """Add users to a group, re-keying it when it is encrypted.

        Users who are already members are ignored.

        Args:
            user_id: Id of the user making the change.
            conversation_id: Id of the group.
            member_ids: Ids of the users to add.
            moderator: Whether the user may change groups they do not own.
            wraps: New wrapped keys for all eligible members, required for an
                encrypted group.

        Returns:
            The number of members added.

        Raises:
            InvalidRequest: when the user may not change the group, it would grow
                too large, someone cannot be messaged, a non-member tries to change
                an encrypted group, or the wraps are invalid.
        """
        now = self._clock()
        added = 0
        with self._db.transaction() as db:
            row = self._manage(db, user_id, conversation_id, moderator)
            current = {
                item["user_id"]
                for item in db.execute(
                    "SELECT user_id FROM members WHERE conversation_id = ?", (conversation_id,)
                ).fetchall()
            }
            fresh = {item for item in member_ids if item not in current}
            if len(current) + len(fresh) > self._tuning.max_members:
                raise InvalidRequest("The group is too large.")
            for member in fresh:
                if not self._can_message(self._user_row(db, member)):
                    raise InvalidRequest("Someone in the list cannot be messaged.")
            if fresh and row["encrypted"] and row["member"] is None:
                raise InvalidRequest(MEMBERS_ONLY)
            for member in fresh:
                db.execute(
                    "INSERT INTO members (conversation_id, user_id, owner, joined) "
                    "VALUES (?, ?, 0, ?)",
                    (conversation_id, member, now),
                )
                added += 1
            if fresh and row["encrypted"]:
                self._rekey(db, row, self._eligible(db, current | fresh), wraps, user_id)
        return added

    def remove_member(
        self,
        user_id: str,
        conversation_id: str,
        member_id: str,
        moderator: bool = False,
        wraps: dict[str, Any] | None = None,
    ) -> bool:
        """Remove a member from a group or let a member leave.

        When the last member goes the group and its announcements are deleted.
        When the last owner goes the longest-standing member becomes owner.

        Args:
            user_id: Id of the user making the change.
            conversation_id: Id of the group.
            member_id: Id of the member to remove, equal to user_id when leaving.
            moderator: Whether the user may change groups they do not own.
            wraps: New wrapped keys for the remaining eligible members, needed when
                someone else is removed from an encrypted group.

        Returns:
            True when the member was removed, false when the group or the
            membership does not exist.

        Raises:
            InvalidRequest: when the user may not change the group, a non-member
                removes someone from an encrypted group, or the wraps are invalid.
        """
        with self._db.transaction() as db:
            row = db.execute(
                "SELECT conversations.*, COALESCE(members.owner, 0) AS is_owner, "
                "members.user_id AS member FROM conversations "
                "LEFT JOIN members ON members.conversation_id = conversations.id "
                "AND members.user_id = ? WHERE conversations.id = ? "
                "AND conversations.kind = 'group'",
                (user_id, conversation_id),
            ).fetchone()
            if row is None:
                return False
            leaving = member_id == user_id
            if not (leaving or row["is_owner"] or moderator):
                raise InvalidRequest("You cannot change this group.")
            target = db.execute(
                "SELECT owner FROM members WHERE conversation_id = ? AND user_id = ?",
                (conversation_id, member_id),
            ).fetchone()
            if target is None:
                return False
            remaining = {
                item["user_id"]
                for item in db.execute(
                    "SELECT user_id FROM members WHERE conversation_id = ? AND user_id != ?",
                    (conversation_id, member_id),
                ).fetchall()
            }
            if not remaining:
                db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
                db.execute(
                    "DELETE FROM announcements WHERE audience = 'group' AND audience_id = ?",
                    (conversation_id,),
                )
                return True
            if (
                target["owner"]
                and not db.execute(
                    "SELECT 1 FROM members WHERE conversation_id = ? "
                    "AND user_id != ? AND owner = 1",
                    (conversation_id, member_id),
                ).fetchone()
            ):
                heir = db.execute(
                    "SELECT user_id FROM members WHERE conversation_id = ? AND user_id != ? "
                    "ORDER BY joined LIMIT 1",
                    (conversation_id, member_id),
                ).fetchone()
                db.execute(
                    "UPDATE members SET owner = 1 WHERE conversation_id = ? AND user_id = ?",
                    (conversation_id, heir["user_id"]),
                )
            # Leaving only bumps the key version and stores no new wraps, so messages with the old
            # version are rejected until the key is rotated.
            if row["encrypted"] and leaving:
                db.execute(
                    "UPDATE conversations SET key_version = key_version + 1 WHERE id = ?",
                    (conversation_id,),
                )
            elif row["encrypted"]:
                if row["member"] is None:
                    raise InvalidRequest(MEMBERS_ONLY)
                self._rekey(db, row, self._eligible(db, remaining), wraps, user_id)
            db.execute(
                "DELETE FROM members WHERE conversation_id = ? AND user_id = ?",
                (conversation_id, member_id),
            )
            db.execute(
                "DELETE FROM conversation_keys WHERE conversation_id = ? AND user_id = ?",
                (conversation_id, member_id),
            )
        return True

    def rename_group(
        self, user_id: str, conversation_id: str, title: str, moderator: bool = False
    ) -> None:
        """Change the title of a group.

        Args:
            user_id: Id of the user making the change.
            conversation_id: Id of the group.
            title: New name; whitespace is collapsed and the length is limited.
            moderator: Whether the user may change groups they do not own.

        Raises:
            InvalidRequest: when the name is too short or the user may not change
                the group.
        """
        label = " ".join(title.split())[: self._tuning.title_chars]
        if len(label) < 2:
            raise InvalidRequest("Give the group a name.")
        with self._db.transaction() as db:
            self._manage(db, user_id, conversation_id, moderator)
            db.execute("UPDATE conversations SET title = ? WHERE id = ?", (label, conversation_id))

    def delete_group(self, user_id: str, conversation_id: str, moderator: bool = False) -> None:
        """Delete a group together with the announcements aimed at it.

        Args:
            user_id: Id of the user making the change.
            conversation_id: Id of the group.
            moderator: Whether the user may change groups they do not own.

        Raises:
            InvalidRequest: when the group does not exist or the user may not
                change it.
        """
        with self._db.transaction() as db:
            self._manage(db, user_id, conversation_id, moderator)
            db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
            db.execute(
                "DELETE FROM announcements WHERE audience = 'group' AND audience_id = ?",
                (conversation_id,),
            )

    def _audience_label(self, db: sqlite3.Connection, audience: str, audience_id: str) -> str:
        """Return a readable name for the audience of an announcement.

        Args:
            db: Open database connection.
            audience: Audience kind: all, role or group.
            audience_id: Id of the role or group, unused for everyone.

        Returns:
            Everyone, the role name or the group title, or a generic label when the
            target no longer exists.
        """
        if audience == EVERYONE:
            return "Everyone"
        if audience == ROLE:
            row = db.execute("SELECT name FROM roles WHERE id = ?", (audience_id,)).fetchone()
            return row["name"] if row else "A role"
        row = db.execute(
            "SELECT title FROM conversations WHERE id = ? AND kind = 'group'", (audience_id,)
        ).fetchone()
        return row["title"] if row else "A group"

    def announce(
        self,
        user_id: str,
        title: str,
        body: str,
        audience: str = EVERYONE,
        audience_id: str = "",
        days: int = 0,
        pinned: bool = False,
        moderator: bool = False,
    ) -> Announcement:
        """Publish an announcement to everyone, a role or a group.

        The body is encrypted at rest with the vault. The author counts as having
        read it already.

        Args:
            user_id: Id of the author.
            title: Heading; whitespace is collapsed and the length is limited.
            body: Text of the announcement.
            audience: One of all, role or group.
            audience_id: Id of the role or group, ignored for everyone.
            days: Lifetime in days, which must be an offered choice; 0 means never
                expires.
            pinned: Whether to show it above other announcements.
            moderator: Whether the author may target groups they are not in.

        Returns:
            The stored announcement.

        Raises:
            InvalidRequest: when the title or text is empty, the audience or the
                lifetime is not allowed, the author cannot post, the role or group
                is unknown, or too many announcements are active.
        """
        tuning = self._tuning
        heading = " ".join(title.split())[: tuning.announcement_title_chars]
        text = self._clean(body, tuning.announcement_chars)
        if not heading or not text:
            raise InvalidRequest("An announcement needs a title and a text.")
        if audience not in AUDIENCES:
            raise InvalidRequest("Choose who should receive it.")
        if days not in tuning.expiry_choices:
            raise InvalidRequest("Choose one of the offered lifetimes.")
        now = self._clock()
        identifier = secrets.token_urlsafe(9)
        with self._db.transaction() as db:
            author = self._user_row(db, user_id)
            if not self._can_message(author) or author is None:
                raise InvalidRequest("You cannot post announcements.")
            if (
                audience == ROLE
                and not db.execute("SELECT 1 FROM roles WHERE id = ?", (audience_id,)).fetchone()
            ):
                raise InvalidRequest("That role does not exist.")
            if audience == GROUP:
                row = db.execute(
                    "SELECT conversations.id AS id, members.user_id AS member FROM conversations "
                    "LEFT JOIN members ON members.conversation_id = conversations.id "
                    "AND members.user_id = ? WHERE conversations.id = ? "
                    "AND conversations.kind = 'group'",
                    (user_id, audience_id),
                ).fetchone()
                if row is None or not (row["member"] or moderator):
                    raise InvalidRequest("That group does not exist.")
            if audience == EVERYONE:
                audience_id = ""
            active = db.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(author_id = ?), 0) AS mine FROM announcements "
                "WHERE expires IS NULL OR expires > ?",
                (user_id, now),
            ).fetchone()
            if (
                active["n"] >= tuning.announcement_max_active
                or active["mine"] >= tuning.announcement_max_per_author
            ):
                raise InvalidRequest("There are too many active announcements.")
            db.execute(
                "INSERT INTO announcements (id, title, body, audience, audience_id, author_id, "
                "author_name, created, expires, pinned) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    identifier,
                    heading,
                    self._seal(text),
                    audience,
                    audience_id,
                    user_id,
                    author["name"],
                    now,
                    now + days * DAY if days else None,
                    int(pinned),
                ),
            )
            db.execute(
                "INSERT INTO announcement_reads (announcement_id, user_id, ts) VALUES (?, ?, ?)",
                (identifier, user_id, now),
            )
            stored = db.execute(
                "SELECT announcements.*, 1 AS seen FROM announcements WHERE id = ?", (identifier,)
            ).fetchone()
            return self._announcement(db, stored)

    def _announcement(self, db: sqlite3.Connection, row: sqlite3.Row) -> Announcement:
        """Convert an announcement row into an Announcement model.

        Args:
            db: Open database connection.
            row: Announcement row that includes the seen column.

        Returns:
            The announcement with a decrypted body and a readable audience label.
        """
        return Announcement(
            id=row["id"],
            title=row["title"],
            body=self._open(row["body"]),
            audience=row["audience"],
            audience_id=row["audience_id"],
            audience_label=self._audience_label(db, row["audience"], row["audience_id"]),
            author_id=row["author_id"],
            author_name=row["author_name"],
            created=row["created"],
            expires=row["expires"],
            pinned=bool(row["pinned"]),
            read=bool(row["seen"]),
        )

    def announcements(
        self, user_id: str, unread_only: bool = False, limit: int | None = None
    ) -> list[Announcement]:
        """List the announcements that a user can see.

        Expired announcements and those aimed at other roles or groups are left
        out.

        Args:
            user_id: Id of the user.
            unread_only: Whether to return only announcements not yet read.
            limit: Maximum number of items, the configured default when omitted.

        Returns:
            Pinned announcements first, then the newest ones. Empty when the user
            cannot use messaging.
        """
        with self._db.transaction() as db:
            person = self._user_row(db, user_id)
            if not self._can_message(person) or person is None:
                return []
            rows = db.execute(
                "SELECT announcements.*, EXISTS (SELECT 1 FROM announcement_reads WHERE "
                "announcement_reads.announcement_id = announcements.id AND "
                "announcement_reads.user_id = ?) AS seen FROM announcements WHERE "
                + VISIBLE
                + " AND (? = 0 OR NOT EXISTS (SELECT 1 FROM announcement_reads WHERE "
                "announcement_reads.announcement_id = announcements.id AND "
                "announcement_reads.user_id = ?)) "
                "ORDER BY announcements.pinned DESC, announcements.created DESC LIMIT ?",
                (
                    user_id,
                    person["role_id"],
                    user_id,
                    self._clock(),
                    int(unread_only),
                    user_id,
                    limit or self._tuning.announcement_shown,
                ),
            ).fetchall()
            return [self._announcement(db, row) for row in rows]

    def read_announcement(self, user_id: str, identifier: str) -> bool:
        """Mark an announcement as read by a user.

        Args:
            user_id: Id of the user.
            identifier: Id of the announcement.

        Returns:
            True when the announcement is visible to the user and is now read,
            false otherwise.
        """
        with self._db.transaction() as db:
            person = self._user_row(db, user_id)
            if not self._can_message(person) or person is None:
                return False
            visible = db.execute(
                "SELECT 1 FROM announcements WHERE announcements.id = ? AND " + VISIBLE,
                (identifier, person["role_id"], user_id, self._clock()),
            ).fetchone()
            if visible is None:
                return False
            db.execute(
                "INSERT OR IGNORE INTO announcement_reads (announcement_id, user_id, ts) "
                "VALUES (?, ?, ?)",
                (identifier, user_id, self._clock()),
            )
        return True

    def delete_announcement(self, user_id: str, identifier: str, moderator: bool = False) -> bool:
        """Delete an announcement as its author or as a moderator.

        Args:
            user_id: Id of the user asking for the deletion.
            identifier: Id of the announcement.
            moderator: Whether the user may delete announcements of other people.

        Returns:
            True when an announcement was deleted.
        """
        return bool(
            self._db.run(
                "DELETE FROM announcements WHERE id = ? AND (author_id = ? OR ? = 1)",
                (identifier, user_id, int(moderator)),
            )
        )

    def unread(self, user_id: str) -> dict[str, int]:
        """Count the unread messages and announcements of a user.

        Args:
            user_id: Id of the user.

        Returns:
            A dict with the keys messages and announcements; both are 0 when the
            user cannot use messaging.
        """
        with self._db.transaction() as db:
            person = self._user_row(db, user_id)
            if not self._can_message(person) or person is None:
                return {"messages": 0, "announcements": 0}
            channel = self.role_channel_row(db, person["role_id"])
            messages = db.execute(
                "SELECT COUNT(*) AS n FROM chat_messages JOIN conversations ON "
                "conversations.id = chat_messages.conversation_id LEFT JOIN members ON "
                "members.conversation_id = conversations.id AND members.user_id = ? "
                "WHERE ((conversations.kind != 'role' AND members.user_id IS NOT NULL) "
                "OR conversations.id = ?) AND chat_messages.id > COALESCE(members.last_read, 0) "
                "AND chat_messages.removed = 0 "
                "AND (chat_messages.sender_id IS NULL OR chat_messages.sender_id != ?)",
                (user_id, channel, user_id),
            ).fetchone()["n"]
            notices = db.execute(
                "SELECT COUNT(*) AS n FROM announcements WHERE " + VISIBLE + " AND NOT EXISTS ("
                "SELECT 1 FROM announcement_reads WHERE announcement_reads.announcement_id = "
                "announcements.id AND announcement_reads.user_id = ?)",
                (person["role_id"], user_id, self._clock(), user_id),
            ).fetchone()["n"]
        return {"messages": messages, "announcements": notices}

    def purge(self) -> dict[str, int]:
        """Delete messages and announcements that are no longer needed.

        Removes messages older than the retention period when one is set,
        announcements that expired more than 30 days ago and announcements whose
        role or group no longer exists.

        Returns:
            A dict with the number of deleted messages and announcements.
        """
        now = self._clock()
        removed_messages = 0
        if self._tuning.retention_days:
            removed_messages = self._db.run(
                "DELETE FROM chat_messages WHERE ts < ?", (now - self._tuning.retention_days * DAY,)
            )
        expired = self._db.run(
            "DELETE FROM announcements WHERE expires IS NOT NULL AND expires < ?", (now - 30 * DAY,)
        )
        orphaned = self._db.run(
            "DELETE FROM announcements WHERE (audience = 'group' AND NOT EXISTS ("
            "SELECT 1 FROM conversations WHERE conversations.id = announcements.audience_id)) "
            "OR (audience = 'role' AND NOT EXISTS ("
            "SELECT 1 FROM roles WHERE roles.id = announcements.audience_id))"
        )
        return {"messages": removed_messages, "announcements": expired + orphaned}
