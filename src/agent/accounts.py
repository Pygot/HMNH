# src/agent/accounts.py
from agent.config import (
    AuthTuning,
    RoleSpec,
)
from agent.permissions import (
    expand,
    KNOWN,
)
from dataclasses import (
    dataclass,
    replace,
)
from pydantic import (
    BaseModel,
    Field,
)
from agent.errors import InvalidRequest
from collections.abc import Callable
from agent.db import Database
from typing import Any

import hashlib
import secrets
import sqlite3
import hmac
import json
import time
import re

OWNER_ID = "owner"
KEY_SEPARATOR = "_"
ACTIVE = "active"
DISABLED = "disabled"
INVITED = "invited"
STATUSES = (ACTIVE, DISABLED, INVITED)
ADMIN_NEEDS = frozenset({"users.manage", "roles.manage", "config.manage"})
INVITE = "invite"
RESET = "reset"
LOGIN = "login"
PURPOSES = (INVITE, RESET, LOGIN)
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,63}$")
INVISIBLE = re.compile(r"[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")
SELECT_USER = (
    "SELECT users.id AS id, username, display_name, email, role_id, roles.name AS role_name, "
    "status, totp_enabled, email_code_login, email_verified, must_change_password, "
    "token_version, users.created AS created, users.updated AS updated, password_changed, "
    "last_login, "
    "(password_hash IS NOT NULL) AS has_password "
    "FROM users JOIN roles ON roles.id = users.role_id"
)
INSERT_ROLE = (
    "INSERT INTO roles (id, name, description, permissions, builtin, created, updated) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)
INSERT_USER = (
    "INSERT INTO users (id, username, display_name, email, role_id, password_hash, status, "
    "must_change_password, created, updated, password_changed) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
INSERT_AUDIT = (
    "INSERT INTO audit (ts, actor_id, actor, action, target, outcome, ip, detail) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)
SELECT_KEY = (
    "SELECT api_keys.id AS id, api_keys.user_id AS user_id, name, prefix, permissions, "
    "api_keys.created AS created, "
    "expires, last_used, revoked, secret_hash, users.username AS owner "
    "FROM api_keys JOIN users ON users.id = api_keys.user_id"
)


class Role(BaseModel):
    """A named set of permissions that can be assigned to accounts."""

    id: str
    name: str
    description: str = ""
    permissions: list[str] = Field(default_factory=list)
    builtin: bool = False
    created: float
    updated: float

    def effective(self) -> frozenset[str]:
        """Return the permissions this role grants after expansion.

        Returns:
            The full set of permission names, including those implied by grants.
        """
        return expand(self.permissions)


class User(BaseModel):
    """A stored account with its profile and security state.

    The password hash, TOTP secret and recovery codes are not part of this model;
    only the has_password and totp_enabled flags are exposed.
    """

    id: str
    username: str
    display_name: str = ""
    email: str | None = None
    role_id: str
    role_name: str = ""
    status: str = ACTIVE
    totp_enabled: bool = False
    email_code_login: bool = False
    email_verified: bool = False
    must_change_password: bool = False
    token_version: int = 0
    created: float
    updated: float
    password_changed: float | None = None
    last_login: float | None = None
    has_password: bool = False

    @property
    def label(self) -> str:
        """Return the name to show for this account."""
        return self.display_name or self.username


class ApiKey(BaseModel):
    """Metadata of an API key; the key secret itself is never stored here.

    A permissions value of None means the key may use everything its owner can.
    """

    id: str
    user_id: str
    owner: str = ""
    name: str
    prefix: str
    permissions: list[str] | None = None
    created: float
    expires: float | None = None
    last_used: float | None = None
    revoked: float | None = None


class AuditEntry(BaseModel):
    """One recorded security-relevant action in the audit log."""

    id: int
    ts: float
    actor_id: str | None = None
    actor: str
    action: str
    target: str = ""
    outcome: str = "ok"
    ip: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)


class LinkInfo(BaseModel):
    """A one-time link record (invite, reset or login) without its token."""

    id: str
    user_id: str
    purpose: str
    expires: float


class UserSecrets(BaseModel):
    """The secret material stored for one account.

    Holds the password hash, the sealed TOTP secret, the last used TOTP counter and
    the hashed recovery codes. For internal use only.
    """

    password_hash: str | None = None
    totp_secret: str | None = None
    totp_counter: int | None = None
    recovery_codes: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class Principal:
    """The authenticated identity and permissions behind a request.

    Instances are immutable.
    """

    user_id: str
    name: str
    role: str
    permissions: frozenset[str]
    version: int
    source: str
    key_id: str | None = None
    totp: bool = False
    username: str = ""

    def can(self, permission: str) -> bool:
        """Check whether this principal holds a permission.

        Args:
            permission: the permission name to look for.

        Returns:
            True when the permission is in the effective permission set.
        """
        return permission in self.permissions


class LastAdminError(InvalidRequest):
    """Raised when a change would leave no active account able to administer."""

    pass


def owner_principal() -> Principal:
    """Return the built-in owner principal holding every known permission.

    Returns:
        A principal with the owner id, the admin role and all known permissions.
    """
    return Principal(
        user_id=OWNER_ID,
        name="Owner",
        role="admin",
        permissions=KNOWN,
        version=0,
        source="owner",
        username="owner",
    )


def clean_name(raw: str) -> str:
    """Return a display name without invisible characters or extra spaces.

    Args:
        raw: the name as typed by the user.

    Returns:
        The name with invisible and bidirectional control characters removed,
        whitespace collapsed, and cut to 80 characters.
    """
    return " ".join(INVISIBLE.sub("", raw).split())[:80]


def recovery_digest(code: str) -> str:
    """Return the digest of a recovery code, ignoring spaces and case.

    Args:
        code: the recovery code as entered or generated.

    Returns:
        The hex SHA-256 digest of the code in lower case without whitespace.
    """
    return digest("".join(code.split()).lower())


def digest(value: str) -> str:
    """Return the SHA-256 hex digest of a string.

    Args:
        value: the text to hash, encoded as UTF-8.

    Returns:
        The hexadecimal digest.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    """Serialize a value to compact JSON that keeps non-ASCII characters.

    Args:
        value: any JSON-serializable value.

    Returns:
        The JSON text without extra spaces.
    """
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _list(value: str | None) -> list[str]:
    """Parse a JSON list of strings, tolerating bad input.

    Args:
        value: JSON text, or None.

    Returns:
        The list items converted to strings, or an empty list when the text is
        empty, invalid or not a list.
    """
    try:
        loaded = json.loads(value) if value else []
    except ValueError:
        return []
    return [str(item) for item in loaded] if isinstance(loaded, list) else []


class Accounts:
    """Storage and rules for roles, accounts, API keys, links and the audit log.

    All state lives in the database and time comes from an injectable clock. Link
    tokens, emailed codes, recovery codes and API key secrets are stored only as
    SHA-256 digests.
    """

    def __init__(
        self,
        database: Database,
        tuning: AuthTuning,
        clock: Callable[[], float] = time.time,
    ):
        """Initialize the account store.

        Args:
            database: the database holding the account tables.
            tuning: limits and lifetimes for accounts, keys, links and codes.
            clock: function returning the current time in seconds since the epoch.
        """
        self._db = database
        self._tuning = tuning
        self._clock = clock

    def _role(self, row: sqlite3.Row) -> Role:
        """Build a role from a database row.

        Args:
            row: a row of the roles table.

        Returns:
            The matching role.
        """
        return Role(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            permissions=_list(row["permissions"]),
            builtin=bool(row["builtin"]),
            created=row["created"],
            updated=row["updated"],
        )

    def sync_roles(self, specs: dict[str, RoleSpec]) -> None:
        """Insert built-in roles that are missing from the database.

        Roles whose name already exists are left untouched.

        Args:
            specs: the configured role specifications keyed by role name.
        """
        now = self._clock()
        with self._db.transaction() as db:
            for name, spec in specs.items():
                known = db.execute("SELECT 1 FROM roles WHERE name = ?", (name,)).fetchone()
                if known is None:
                    db.execute(
                        INSERT_ROLE,
                        (
                            secrets.token_urlsafe(9),
                            name,
                            spec.description,
                            _json(list(spec.permissions)),
                            1,
                            now,
                            now,
                        ),
                    )

    def roles(self) -> list[Role]:
        """Return all roles, built-in ones first and then by name.

        Returns:
            The list of roles.
        """
        rows = self._db.query("SELECT * FROM roles ORDER BY builtin DESC, name")
        return [self._role(row) for row in rows]

    def role(self, identifier: str) -> Role | None:
        """Return the role with the given id.

        Args:
            identifier: the role id.

        Returns:
            The role, or None when it does not exist.
        """
        row = self._db.one("SELECT * FROM roles WHERE id = ?", (identifier,))
        return self._role(row) if row else None

    def role_named(self, name: str) -> Role | None:
        """Return the role with the given name.

        Args:
            name: the exact role name.

        Returns:
            The role, or None when it does not exist.
        """
        row = self._db.one("SELECT * FROM roles WHERE name = ?", (name,))
        return self._role(row) if row else None

    def role_sizes(self) -> dict[str, int]:
        """Count the accounts assigned to each role.

        Returns:
            A mapping of role id to account count; unused roles are absent.
        """
        rows = self._db.query("SELECT role_id, COUNT(*) AS n FROM users GROUP BY role_id")
        return {row["role_id"]: row["n"] for row in rows}

    def save_role(
        self, name: str, description: str, permissions: list[str], identifier: str | None = None
    ) -> Role:
        """Create a role or update an existing one.

        Updating a role bumps the token version of every account holding it, which
        ends their sessions.

        Args:
            name: the role name; whitespace is collapsed and it needs 2 to 40 characters.
            description: free text, cut to 300 characters.
            permissions: permission names, which must all be known; stored sorted and
                without duplicates.
            identifier: the id of the role to update, or None to create a new role.

        Returns:
            The saved role.

        Raises:
            InvalidRequest: when the name length is wrong, a permission is unknown, the
                role limit is reached, the role to update is missing or the name is taken.
            LastAdminError: when an update leaves no active account able to administer.
        """
        name = " ".join(name.split())
        if not 2 <= len(name) <= 40:
            raise InvalidRequest("A role name needs 2 to 40 characters.")
        unknown = [item for item in permissions if item not in KNOWN]
        if unknown:
            raise InvalidRequest("Some permissions do not exist.")
        chosen = sorted(set(permissions))
        now = self._clock()
        try:
            with self._db.transaction() as db:
                if identifier is None:
                    count = db.execute("SELECT COUNT(*) AS n FROM roles").fetchone()["n"]
                    if count >= self._tuning.max_roles:
                        raise InvalidRequest("There are too many roles.")
                    identifier = secrets.token_urlsafe(9)
                    db.execute(
                        INSERT_ROLE,
                        (identifier, name, description[:300], _json(chosen), 0, now, now),
                    )
                else:
                    changed = db.execute(
                        "UPDATE roles SET name = ?, description = ?, permissions = ?, updated = ? "
                        "WHERE id = ?",
                        (name, description[:300], _json(chosen), now, identifier),
                    ).rowcount
                    if not changed:
                        raise InvalidRequest("That role does not exist.")
                    db.execute(
                        "UPDATE users SET token_version = token_version + 1 WHERE role_id = ?",
                        (identifier,),
                    )
                    self._require_admin(db, allow_empty=True)
        except sqlite3.IntegrityError as error:
            raise InvalidRequest("A role with that name already exists.") from error
        saved = self.role(identifier)
        if saved is None:
            raise InvalidRequest("That role does not exist.")
        return saved

    def delete_role(self, identifier: str) -> None:
        """Delete a custom role that no account uses.

        Args:
            identifier: the id of the role to delete.

        Raises:
            InvalidRequest: when the role does not exist, is built in or is still
                assigned to accounts.
        """
        with self._db.transaction() as db:
            row = db.execute("SELECT builtin FROM roles WHERE id = ?", (identifier,)).fetchone()
            if row is None:
                raise InvalidRequest("That role does not exist.")
            if row["builtin"]:
                raise InvalidRequest("Built-in roles cannot be removed.")
            used = db.execute(
                "SELECT COUNT(*) AS n FROM users WHERE role_id = ?", (identifier,)
            ).fetchone()["n"]
            if used:
                raise InvalidRequest("Move the accounts of this role to another role first.")
            db.execute("DELETE FROM roles WHERE id = ?", (identifier,))

    def _require_admin(self, db: sqlite3.Connection, allow_empty: bool = False) -> None:
        """Check that an active account can still manage everything.

        Args:
            db: the connection of the transaction being checked.
            allow_empty: skip the check when no account exists at all.

        Raises:
            LastAdminError: when no active account holds the user, role and config
                management permissions.
        """
        if allow_empty and not db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            return
        rows = db.execute(
            "SELECT roles.permissions AS permissions FROM users "
            "JOIN roles ON roles.id = users.role_id WHERE users.status = ?",
            (ACTIVE,),
        ).fetchall()
        if not any(expand(_list(row["permissions"])) >= ADMIN_NEEDS for row in rows):
            raise LastAdminError("At least one active account must be able to manage everything.")

    def _user(self, row: sqlite3.Row) -> User:
        """Build a user from a database row.

        Args:
            row: a row selected with the user column set.

        Returns:
            The matching user.
        """
        return User.model_validate(dict(row))

    def count_users(self) -> int:
        """Return the number of accounts.

        Returns:
            The account count.
        """
        return self._db.query("SELECT COUNT(*) AS n FROM users")[0]["n"]

    def needs_setup(self) -> bool:
        """Check whether the first account still has to be created.

        Returns:
            True when no account exists yet.
        """
        return self.count_users() == 0

    def users(self) -> list[User]:
        """Return all accounts sorted by username, ignoring case.

        Returns:
            The list of users.
        """
        rows = self._db.query(SELECT_USER + " ORDER BY lower(username)")
        return [self._user(row) for row in rows]

    def user(self, identifier: str) -> User | None:
        """Return the account with the given id.

        Args:
            identifier: the account id.

        Returns:
            The user, or None when it does not exist.
        """
        row = self._db.one(SELECT_USER + " WHERE users.id = ?", (identifier,))
        return self._user(row) if row else None

    def user_named(self, username: str) -> User | None:
        """Return the account with the given username.

        Args:
            username: the username; surrounding whitespace is ignored.

        Returns:
            The user, or None when it does not exist.
        """
        row = self._db.one(SELECT_USER + " WHERE username = ?", (username.strip(),))
        return self._user(row) if row else None

    def user_with_email(self, email: str) -> User | None:
        """Return the oldest active account that uses an email address.

        Args:
            email: the address, compared in lower case.

        Returns:
            The user, or None when no active account has that address.
        """
        row = self._db.one(
            SELECT_USER + " WHERE lower(email) = ? AND status = ? ORDER BY created LIMIT 1",
            (email.strip().lower(), ACTIVE),
        )
        return self._user(row) if row else None

    def validate_identity(self, username: str, email: str | None) -> tuple[str, str | None]:
        """Validate and normalize a username and an optional email address.

        Args:
            username: the requested username; surrounding whitespace is removed.
            email: the requested address, or None or blank for none.

        Returns:
            The cleaned username and the cleaned address, or None when blank.

        Raises:
            InvalidRequest: when the username does not match the configured pattern or
                the email address is malformed.
        """
        username = username.strip()
        if not re.fullmatch(self._tuning.username_pattern, username):
            raise InvalidRequest("A username uses 2 to 64 letters, digits and . _ @ + -.")
        cleaned = (email or "").strip()
        if cleaned and not EMAIL.fullmatch(cleaned):
            raise InvalidRequest("That email address does not look right.")
        return username, cleaned or None

    def create_user(
        self,
        username: str,
        role_id: str,
        *,
        display_name: str = "",
        email: str | None = None,
        status: str = ACTIVE,
        password_hash: str | None = None,
        must_change_password: bool = False,
        only_if_empty: bool = False,
    ) -> User:
        """Create an account.

        Args:
            username: the login name.
            role_id: the id of the role to assign.
            display_name: an optional friendly name.
            email: an optional email address.
            status: one of the known account statuses.
            password_hash: an encoded password hash, or None for an account without a
                password.
            must_change_password: require a password change at the next login.
            only_if_empty: fail when any account already exists, as in first-run setup.

        Returns:
            The created user.

        Raises:
            InvalidRequest: when the username or email is invalid, the status or role is
                unknown, setup was already completed, the account limit is reached, the
                display name belongs to another account or the username is taken.
        """
        username, email = self.validate_identity(username, email)
        display_name = clean_name(display_name)
        if status not in STATUSES:
            raise InvalidRequest("That status does not exist.")
        identifier = secrets.token_urlsafe(9)
        now = self._clock()
        try:
            with self._db.transaction() as db:
                count = db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
                if only_if_empty and count:
                    raise InvalidRequest("The setup was already completed.")
                if count >= self._tuning.max_users:
                    raise InvalidRequest("There are too many accounts.")
                if db.execute("SELECT 1 FROM roles WHERE id = ?", (role_id,)).fetchone() is None:
                    raise InvalidRequest("That role does not exist.")
                self._name_free(db, display_name, identifier, username)
                db.execute(
                    INSERT_USER,
                    (
                        identifier,
                        username,
                        display_name,
                        email,
                        role_id,
                        password_hash,
                        status,
                        int(must_change_password),
                        now,
                        now,
                        now if password_hash else None,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise InvalidRequest("That username is already taken.") from error
        created = self.user(identifier)
        if created is None:
            raise InvalidRequest("The account could not be created.")
        return created

    def update_user(
        self,
        identifier: str,
        *,
        display_name: str | None = None,
        email: str | None = None,
        role_id: str | None = None,
        status: str | None = None,
        clear_email: bool = False,
    ) -> User:
        """Change the profile, role or status of an account.

        Changing the email address clears its verification and pending emailed codes.
        Changing the role or status bumps the token version, which ends sessions.

        Args:
            identifier: the account id.
            display_name: a new display name, or None to keep it.
            email: a new email address, or None to keep it.
            role_id: a new role id, or None to keep it.
            status: a new status, or None to keep it.
            clear_email: remove the stored email address when no new one is given.

        Returns:
            The updated user.

        Raises:
            InvalidRequest: when the account is missing, the name, email, status or role
                is invalid, or the display name belongs to another account.
            LastAdminError: when the change leaves no active account able to administer.
        """
        with self._db.transaction() as db:
            row = db.execute("SELECT * FROM users WHERE id = ?", (identifier,)).fetchone()
            if row is None:
                raise InvalidRequest("That account does not exist.")
            name = row["display_name"] if display_name is None else clean_name(display_name)
            self._name_free(db, name, identifier, row["username"])
            address = row["email"]
            verified = row["email_verified"]
            if email is not None or clear_email:
                _, address = self.validate_identity(row["username"], email)
                if address != row["email"]:
                    verified = 0
                    db.execute("DELETE FROM email_codes WHERE user_id = ?", (identifier,))
            role = row["role_id"] if role_id is None else role_id
            state = row["status"] if status is None else status
            if state not in STATUSES:
                raise InvalidRequest("That status does not exist.")
            if role != row["role_id"] and (
                db.execute("SELECT 1 FROM roles WHERE id = ?", (role,)).fetchone() is None
            ):
                raise InvalidRequest("That role does not exist.")
            bump = int(role != row["role_id"] or state != row["status"])
            db.execute(
                "UPDATE users SET display_name = ?, email = ?, email_verified = ?, role_id = ?, "
                "status = ?, token_version = token_version + ?, updated = ? WHERE id = ?",
                (name, address, verified, role, state, bump, self._clock(), identifier),
            )
            self._require_admin(db)
        updated = self.user(identifier)
        if updated is None:
            raise InvalidRequest("That account does not exist.")
        return updated

    def _name_free(self, db: sqlite3.Connection, name: str, identifier: str, username: str) -> None:
        """Check that a display name does not impersonate another username.

        An empty name, or one equal to the account's own username, is always allowed.

        Args:
            db: the connection of the current transaction.
            name: the display name to check.
            identifier: the id of the account that wants the name.
            username: the username of that account.

        Raises:
            InvalidRequest: when another account has that name as its username.
        """
        if not name or name.casefold() == username.casefold():
            return
        taken = db.execute(
            "SELECT 1 FROM users WHERE lower(username) = lower(?) AND id != ?", (name, identifier)
        ).fetchone()
        if taken:
            raise InvalidRequest("That name belongs to another account.")

    def delete_user(self, identifier: str) -> None:
        """Delete an account.

        Args:
            identifier: the id of the account to delete.

        Raises:
            InvalidRequest: when the account does not exist.
            LastAdminError: when the deletion leaves no active account able to
                administer.
        """
        with self._db.transaction() as db:
            removed = db.execute("DELETE FROM users WHERE id = ?", (identifier,)).rowcount
            if not removed:
                raise InvalidRequest("That account does not exist.")
            self._require_admin(db)

    def secrets_of(self, identifier: str) -> UserSecrets:
        """Return the stored secret material of an account.

        Args:
            identifier: the account id.

        Returns:
            The secrets, or an empty record when the account does not exist.
        """
        row = self._db.one(
            "SELECT password_hash, totp_secret, totp_counter, recovery_codes FROM users "
            "WHERE id = ?",
            (identifier,),
        )
        if row is None:
            return UserSecrets()
        return UserSecrets(
            password_hash=row["password_hash"],
            totp_secret=row["totp_secret"],
            totp_counter=row["totp_counter"],
            recovery_codes=_list(row["recovery_codes"]),
        )

    def set_password(self, identifier: str, encoded: str, *, must_change: bool = False) -> None:
        """Store a new password hash and end all sessions and API keys of the account.

        An invited account becomes active.

        Args:
            identifier: the account id.
            encoded: the encoded password hash to store.
            must_change: require another password change at the next login.

        Raises:
            InvalidRequest: when the account does not exist.
        """
        with self._db.transaction() as db:
            changed = db.execute(
                "UPDATE users SET password_hash = ?, password_changed = ?, updated = ?, "
                "must_change_password = ?, token_version = token_version + 1, "
                "status = CASE WHEN status = ? THEN ? ELSE status END WHERE id = ?",
                (
                    encoded,
                    self._clock(),
                    self._clock(),
                    int(must_change),
                    INVITED,
                    ACTIVE,
                    identifier,
                ),
            ).rowcount
            if not changed:
                raise InvalidRequest("That account does not exist.")
            self._revoke_keys(db, identifier)

    def _revoke_keys(self, db: sqlite3.Connection, identifier: str) -> None:
        """Revoke every active API key of an account inside a transaction.

        Args:
            db: the connection of the current transaction.
            identifier: the account id.
        """
        db.execute(
            "UPDATE api_keys SET revoked = ? WHERE user_id = ? AND revoked IS NULL",
            (self._clock(), identifier),
        )

    def revoke_user_keys(self, identifier: str) -> None:
        """Revoke every active API key of an account.

        Args:
            identifier: the account id.
        """
        with self._db.transaction() as db:
            self._revoke_keys(db, identifier)

    def upgrade_hash(self, identifier: str, encoded: str, previous: str) -> None:
        """Replace a stored password hash with a re-encoded one.

        The update applies only while the stored hash still equals the previous one, so
        a concurrent password change is never overwritten.

        Args:
            identifier: the account id.
            encoded: the new encoded hash.
            previous: the hash that is expected to be stored now.
        """
        self._db.run(
            "UPDATE users SET password_hash = ? WHERE id = ? AND password_hash = ?",
            (encoded, identifier, previous),
        )

    def note_login(self, identifier: str) -> None:
        """Record the current time as the last login of an account.

        Args:
            identifier: the account id.
        """
        self._db.run("UPDATE users SET last_login = ? WHERE id = ?", (self._clock(), identifier))

    def bump_version(self, identifier: str) -> None:
        """End all sessions and API keys of an account.

        The token version is incremented and active API keys are revoked.

        Args:
            identifier: the account id.
        """
        with self._db.transaction() as db:
            db.execute(
                "UPDATE users SET token_version = token_version + 1 WHERE id = ?", (identifier,)
            )
            self._revoke_keys(db, identifier)

    def begin_totp(self, identifier: str, sealed_secret: str) -> None:
        """Store a pending TOTP secret and reset the related state.

        TOTP stays disabled until it is confirmed; the counter and recovery codes are
        cleared.

        Args:
            identifier: the account id.
            sealed_secret: the encrypted TOTP secret.
        """
        self._db.run(
            "UPDATE users SET totp_secret = ?, totp_enabled = 0, totp_counter = NULL, "
            "recovery_codes = '[]' WHERE id = ?",
            (sealed_secret, identifier),
        )

    def enable_totp(self, identifier: str, recovery: list[str], counter: int) -> bool:
        """Activate TOTP with the pending secret.

        The token version is bumped, which ends existing sessions.

        Args:
            identifier: the account id.
            recovery: the plain recovery codes; only their digests are stored.
            counter: the time-step counter of the code that confirmed the setup.

        Returns:
            True when TOTP was enabled, False when there is no pending secret or TOTP is
            already enabled.
        """
        return bool(
            self._db.run(
                "UPDATE users SET totp_enabled = 1, recovery_codes = ?, totp_counter = ?, "
                "token_version = token_version + 1 "
                "WHERE id = ? AND totp_secret IS NOT NULL AND totp_enabled = 0",
                (_json([recovery_digest(code) for code in recovery]), counter, identifier),
            )
        )

    def replace_recovery_codes(self, identifier: str, recovery: list[str]) -> bool:
        """Replace the recovery codes of an account that has TOTP enabled.

        Args:
            identifier: the account id.
            recovery: the new plain recovery codes; only their digests are stored.

        Returns:
            True when the codes were replaced, False when TOTP is not enabled.
        """
        return bool(
            self._db.run(
                "UPDATE users SET recovery_codes = ? WHERE id = ? AND totp_enabled = 1",
                (_json([recovery_digest(code) for code in recovery]), identifier),
            )
        )

    def disable_totp(self, identifier: str) -> None:
        """Remove TOTP and the recovery codes, ending all sessions and API keys.

        Args:
            identifier: the account id.
        """
        with self._db.transaction() as db:
            db.execute(
                "UPDATE users SET totp_secret = NULL, totp_enabled = 0, totp_counter = NULL, "
                "recovery_codes = '[]', token_version = token_version + 1 WHERE id = ?",
                (identifier,),
            )
            self._revoke_keys(db, identifier)

    def use_counter(self, identifier: str, counter: int) -> bool:
        """Record a TOTP counter if it is newer than the last accepted one.

        Args:
            identifier: the account id.
            counter: the time-step counter of a verified code.

        Returns:
            True when the counter was newer and is now stored, False for a replay.
        """
        return bool(
            self._db.run(
                "UPDATE users SET totp_counter = ? WHERE id = ? "
                "AND (totp_counter IS NULL OR totp_counter < ?)",
                (counter, identifier, counter),
            )
        )

    def use_recovery_code(self, identifier: str, code: str) -> bool:
        """Consume a recovery code when it matches a stored one.

        Args:
            identifier: the account id.
            code: the recovery code as entered; spaces and case are ignored.

        Returns:
            True when the code matched and was removed, False otherwise.
        """
        wanted = recovery_digest(code)
        with self._db.transaction() as db:
            row = db.execute(
                "SELECT recovery_codes FROM users WHERE id = ?", (identifier,)
            ).fetchone()
            stored = _list(row["recovery_codes"]) if row else []
            matched = None
            for item in stored:
                # The loop never exits early so the time taken does not depend on which code
                # matched.
                if hmac.compare_digest(item, wanted):
                    matched = item
            if matched is None:
                return False
            stored.remove(matched)
            db.execute(
                "UPDATE users SET recovery_codes = ? WHERE id = ?", (_json(stored), identifier)
            )
            return True

    def recovery_remaining(self, identifier: str) -> int:
        """Return how many recovery codes an account has left.

        Args:
            identifier: the account id.

        Returns:
            The number of unused recovery codes.
        """
        return len(self.secrets_of(identifier).recovery_codes)

    def set_email_login(self, identifier: str, enabled: bool) -> None:
        """Enable or disable login with emailed codes for an account.

        Args:
            identifier: the account id.
            enabled: whether emailed codes may be used to log in.
        """
        self._db.run(
            "UPDATE users SET email_code_login = ? WHERE id = ?", (int(enabled), identifier)
        )

    def mark_email_verified(self, identifier: str) -> None:
        """Mark the email address of an account as verified.

        Args:
            identifier: the account id.
        """
        self._db.run("UPDATE users SET email_verified = 1 WHERE id = ?", (identifier,))

    def create_link(
        self, identifier: str, purpose: str, seconds: float, created_by: str = ""
    ) -> str:
        """Create a one-time link token for an account.

        Unused links of the same purpose for that account are invalidated first. Only
        the digest of the token is stored.

        Args:
            identifier: the account id.
            purpose: one of the invite, reset or login purposes.
            seconds: how long the link stays valid, in seconds.
            created_by: who created the link, recorded for traceability.

        Returns:
            The raw token, which cannot be recovered later.

        Raises:
            InvalidRequest: when the purpose is unknown or the account does not exist.
        """
        if purpose not in PURPOSES:
            raise InvalidRequest("That kind of link does not exist.")
        token = secrets.token_urlsafe(self._tuning.token_bytes)
        now = self._clock()
        with self._db.transaction() as db:
            if db.execute("SELECT 1 FROM users WHERE id = ?", (identifier,)).fetchone() is None:
                raise InvalidRequest("That account does not exist.")
            db.execute(
                "UPDATE links SET used = ? WHERE user_id = ? AND purpose = ? AND used IS NULL",
                (now, identifier, purpose),
            )
            db.execute(
                "INSERT INTO links (id, user_id, purpose, token_hash, created, expires, used, "
                "created_by) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
                (
                    secrets.token_urlsafe(9),
                    identifier,
                    purpose,
                    digest(token),
                    now,
                    now + seconds,
                    created_by,
                ),
            )
        return token

    def recent_link(self, identifier: str, purpose: str, seconds: float) -> bool:
        """Check whether a usable link was created recently.

        Args:
            identifier: the account id.
            purpose: the link purpose.
            seconds: the look-back window, in seconds.

        Returns:
            True when an unused, unexpired link of that purpose was created within the
            window.
        """
        found = self._db.one(
            "SELECT 1 FROM links WHERE user_id = ? AND purpose = ? AND used IS NULL "
            "AND expires > ? AND created > ?",
            (identifier, purpose, self._clock(), self._clock() - seconds),
        )
        return found is not None

    def peek_link(self, token: str, purpose: str) -> LinkInfo | None:
        """Look up a valid link without consuming it.

        Args:
            token: the raw link token.
            purpose: the expected link purpose.

        Returns:
            The link details, or None when it is unknown, used, expired or of another
            purpose.
        """
        row = self._db.one(
            "SELECT id, user_id, purpose, expires FROM links "
            "WHERE token_hash = ? AND purpose = ? AND used IS NULL AND expires > ?",
            (digest(token), purpose, self._clock()),
        )
        return LinkInfo.model_validate(dict(row)) if row else None

    def redeem_link(self, token: str, purpose: str) -> LinkInfo | None:
        """Consume a valid link in a single atomic step.

        Args:
            token: the raw link token.
            purpose: the expected link purpose.

        Returns:
            The link details, or None when it is unknown, already used, expired or of
            another purpose.
        """
        now = self._clock()
        with self._db.transaction() as db:
            row = db.execute(
                "UPDATE links SET used = ? WHERE token_hash = ? AND purpose = ? "
                "AND used IS NULL AND expires > ? RETURNING id, user_id, purpose, expires",
                (now, digest(token), purpose, now),
            ).fetchone()
        return LinkInfo.model_validate(dict(row)) if row else None

    def revoke_links(self, identifier: str) -> None:
        """Invalidate every unused link of an account.

        Args:
            identifier: the account id.
        """
        self._db.run(
            "UPDATE links SET used = ? WHERE user_id = ? AND used IS NULL",
            (self._clock(), identifier),
        )

    def issue_code(self, identifier: str, purpose: str) -> str:
        """Create an emailed one-time numeric code.

        Only a digest bound to the account and purpose is stored. A previous code for the
        same account and purpose is replaced and its attempt counter is reset.

        Args:
            identifier: the account id.
            purpose: what the code is for.

        Returns:
            The zero-padded code, valid for the configured number of minutes.
        """
        tuning = self._tuning
        code = str(secrets.randbelow(10**tuning.email_code_digits)).zfill(tuning.email_code_digits)
        self._db.run(
            "INSERT INTO email_codes (user_id, purpose, code_hash, expires, attempts) "
            "VALUES (?, ?, ?, ?, 0) ON CONFLICT(user_id, purpose) DO UPDATE SET "
            "code_hash = excluded.code_hash, expires = excluded.expires, attempts = 0",
            (
                identifier,
                purpose,
                digest(f"{identifier}:{purpose}:{code}"),
                self._clock() + tuning.email_code_minutes * 60,
            ),
        )
        return code

    def check_code(self, identifier: str, purpose: str, code: str) -> bool:
        """Check an emailed code and consume it when it is right.

        Every check counts as an attempt. The stored code is deleted once it expires,
        runs out of attempts or is matched.

        Args:
            identifier: the account id.
            purpose: what the code is for.
            code: the code as entered; whitespace is ignored.

        Returns:
            True when the code is valid, False otherwise.
        """
        cleaned = "".join(code.split())
        with self._db.transaction() as db:
            row = db.execute(
                "SELECT code_hash, expires, attempts FROM email_codes "
                "WHERE user_id = ? AND purpose = ?",
                (identifier, purpose),
            ).fetchone()
            if row is None:
                return False
            if row["expires"] <= self._clock() or row["attempts"] >= self._tuning.code_attempts:
                db.execute(
                    "DELETE FROM email_codes WHERE user_id = ? AND purpose = ?",
                    (identifier, purpose),
                )
                return False
            db.execute(
                # Count the attempt before comparing so a wrong guess always uses up one try.
                "UPDATE email_codes SET attempts = attempts + 1 WHERE user_id = ? AND purpose = ?",
                (identifier, purpose),
            )
            expected = digest(f"{identifier}:{purpose}:{cleaned}")
            if not hmac.compare_digest(expected, row["code_hash"]):
                return False
            db.execute(
                "DELETE FROM email_codes WHERE user_id = ? AND purpose = ?", (identifier, purpose)
            )
            return True

    def _key(self, row: sqlite3.Row) -> ApiKey:
        """Build an API key record from a database row.

        Args:
            row: a row selected with the API key column set.

        Returns:
            The key metadata, with permissions None when the key is unrestricted.
        """
        values = dict(row)
        raw = values.pop("permissions")
        return ApiKey.model_validate({**values, "permissions": None if raw is None else _list(raw)})

    def create_key(
        self, identifier: str, name: str, permissions: list[str] | None, days: int | None = None
    ) -> tuple[ApiKey, str]:
        """Create an API key for an account.

        Only a digest of the secret is stored, so the token cannot be shown again.

        Args:
            identifier: the id of the owning account.
            name: a label of 1 to 60 characters; whitespace is collapsed.
            permissions: permission names the key is limited to, or None for no limit
                beyond the owner's own permissions.
            days: lifetime in days, None for the configured default, or 0 for no expiry.

        Returns:
            The key metadata and the full token to hand to the user once.

        Raises:
            InvalidRequest: when the name is invalid, a permission is unknown or the
                account already has the maximum number of active keys.
        """
        name = " ".join(name.split())
        if not 1 <= len(name) <= 60:
            raise InvalidRequest("Give the key a name of up to 60 characters.")
        if permissions is not None and any(item not in KNOWN for item in permissions):
            raise InvalidRequest("Some permissions do not exist.")
        lifetime = self._tuning.api_key_days if days is None else days
        now = self._clock()
        prefix = secrets.token_hex(4)
        secret = secrets.token_urlsafe(self._tuning.api_key_bytes)
        token = KEY_SEPARATOR.join((self._tuning.api_key_prefix, prefix, secret))
        key_id = secrets.token_urlsafe(9)
        with self._db.transaction() as db:
            owned = db.execute(
                "SELECT COUNT(*) AS n FROM api_keys WHERE user_id = ? AND revoked IS NULL",
                (identifier,),
            ).fetchone()["n"]
            if owned >= self._tuning.max_api_keys_per_user:
                raise InvalidRequest("This account has too many API keys.")
            db.execute(
                "INSERT INTO api_keys (id, user_id, name, prefix, secret_hash, permissions, "
                "created, expires) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key_id,
                    identifier,
                    name,
                    prefix,
                    digest(secret),
                    None if permissions is None else _json(sorted(set(permissions))),
                    now,
                    now + lifetime * 86_400 if lifetime else None,
                ),
            )
        row = self._db.one(SELECT_KEY + " WHERE api_keys.id = ?", (key_id,))
        if row is None:
            raise InvalidRequest("The key could not be created.")
        return self._key(row), token

    def keys(self, identifier: str | None = None) -> list[ApiKey]:
        """Return API keys, newest first, including revoked ones.

        Args:
            identifier: restrict the list to this account id, or None for all accounts.

        Returns:
            The list of key records.
        """
        if identifier is None:
            rows = self._db.query(SELECT_KEY + " ORDER BY created DESC")
        else:
            rows = self._db.query(
                SELECT_KEY + " WHERE api_keys.user_id = ? ORDER BY created DESC", (identifier,)
            )
        return [self._key(row) for row in rows]

    def revoke_key(self, key_id: str, identifier: str | None = None) -> bool:
        """Revoke an API key.

        Args:
            key_id: the id of the key.
            identifier: when given, only revoke the key if this account owns it.

        Returns:
            True when the key was revoked, False when it was missing, already revoked
            or owned by another account.
        """
        return bool(
            self._db.run(
                "UPDATE api_keys SET revoked = ? WHERE id = ? AND revoked IS NULL "
                "AND (? IS NULL OR user_id = ?)",
                (self._clock(), key_id, identifier, identifier),
            )
        )

    def authenticate_key(self, token: str) -> tuple[ApiKey, Principal] | None:
        """Validate an API key token and return the key with its principal.

        The secret is compared in constant time. The principal's permissions are
        narrowed to the key's own permissions when it has any. A successful check
        updates the key's last-used time.

        Args:
            token: the full API key token.

        Returns:
            The key and its principal, or None when the token is malformed, unknown,
            wrong, revoked, expired or its owner is not active.
        """
        pieces = token.split(KEY_SEPARATOR, 2)
        valid = len(pieces) == 3 and pieces[0] == self._tuning.api_key_prefix
        row = self._db.one(SELECT_KEY + " WHERE prefix = ?", (pieces[1] if valid else "",))
        supplied = digest(pieces[2] if valid else token)
        # Always compare a digest, even for an unknown prefix, so timing does not reveal whether a
        # key prefix exists.
        expected = row["secret_hash"] if row else digest("")
        matches = hmac.compare_digest(supplied, expected)
        if not (valid and row is not None and matches):
            return None
        key = self._key(row)
        now = self._clock()
        if key.revoked is not None or (key.expires is not None and key.expires <= now):
            return None
        principal = self.principal_for(key.user_id, source="key", key_id=key.id)
        if principal is None:
            return None
        if key.permissions is not None:
            principal = replace(
                principal, permissions=principal.permissions & expand(key.permissions)
            )
        self._db.run("UPDATE api_keys SET last_used = ? WHERE id = ?", (now, key.id))
        return key, principal

    def principal_for(
        self,
        identifier: str,
        version: int | None = None,
        source: str = "session",
        key_id: str | None = None,
    ) -> Principal | None:
        """Build the principal for an active account.

        Args:
            identifier: the account id.
            version: the token version the caller holds; when given it must match.
            source: how the principal authenticated, such as session or key.
            key_id: the API key id when the source is a key.

        Returns:
            The principal, or None when the account is missing, not active or the token
            version is stale.
        """
        row = self._db.one(
            "SELECT users.id AS id, username, display_name, status, token_version, totp_enabled, "
            "roles.name AS role_name, roles.permissions AS permissions FROM users "
            "JOIN roles ON roles.id = users.role_id WHERE users.id = ?",
            (identifier,),
        )
        if row is None or row["status"] != ACTIVE:
            return None
        if version is not None and version != row["token_version"]:
            return None
        return Principal(
            user_id=row["id"],
            name=row["display_name"] or row["username"],
            role=row["role_name"],
            permissions=expand(_list(row["permissions"])),
            version=row["token_version"],
            source=source,
            key_id=key_id,
            totp=bool(row["totp_enabled"]),
            username=row["username"],
        )

    def log(
        self,
        action: str,
        *,
        actor: Principal | None = None,
        actor_name: str = "",
        target: str = "",
        outcome: str = "ok",
        ip: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Append an entry to the audit log.

        Details longer than the configured limit are replaced by a truncated copy. The
        actor name, target and address are cut to 80, 120 and 64 characters.

        Args:
            action: the action name.
            actor: the principal that acted, if any.
            actor_name: the name to record when there is no principal.
            target: what the action was applied to.
            outcome: the result, such as ok or denied.
            ip: the client address.
            detail: extra structured data to store.
        """
        text = _json(detail or {})
        if len(text) > self._tuning.audit_detail_chars:
            text = _json({"truncated": text[: self._tuning.audit_detail_chars]})
        name = (actor.username or actor.name) if actor else actor_name
        self._db.run(
            INSERT_AUDIT,
            (
                self._clock(),
                actor.user_id if actor else None,
                (name or "anonymous")[:80],
                action,
                target[:120],
                outcome,
                ip[:64],
                text,
            ),
        )

    def audit(
        self,
        *,
        limit: int | None = None,
        before: int | None = None,
        action: str = "",
        actor: str = "",
        outcome: str = "",
    ) -> list[AuditEntry]:
        """Return audit entries, newest first.

        Args:
            limit: the maximum number of entries, or None for the configured page size.
            before: only entries with an id lower than this, for paging.
            action: only actions starting with this text; empty for all.
            actor: only entries by this actor name; empty for all.
            outcome: only entries with this outcome; empty for all.

        Returns:
            The matching entries.
        """
        rows = self._db.query(
            "SELECT * FROM audit WHERE (? IS NULL OR id < ?) AND (? = '' OR action LIKE ?) "
            "AND (? = '' OR actor = ?) AND (? = '' OR outcome = ?) ORDER BY id DESC LIMIT ?",
            (
                before,
                before,
                action,
                f"{action}%",
                actor,
                actor,
                outcome,
                outcome,
                limit or self._tuning.audit_page_size,
            ),
        )
        entries = []
        for row in rows:
            values = dict(row)
            try:
                values["detail"] = json.loads(values["detail"] or "{}")
            except ValueError:
                values["detail"] = {}
            entries.append(AuditEntry.model_validate(values))
        return entries

    def purge(self) -> dict[str, int]:
        """Delete expired audit entries, spent links and stale emailed codes.

        Audit entries older than the retention period are removed, as are links that
        expired or were used more than a day ago and emailed codes that have expired.

        Returns:
            The number of rows removed, keyed by audit, links and codes.
        """
        now = self._clock()
        cutoff = now - self._tuning.audit_retention_days * 86_400
        with self._db.transaction() as db:
            audit = db.execute("DELETE FROM audit WHERE ts < ?", (cutoff,)).rowcount
            links = db.execute(
                "DELETE FROM links WHERE expires < ? OR used < ?", (now - 86_400, now - 86_400)
            ).rowcount
            codes = db.execute("DELETE FROM email_codes WHERE expires < ?", (now,)).rowcount
        return {"audit": audit, "links": links, "codes": codes}
