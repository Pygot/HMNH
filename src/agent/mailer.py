# src/agent/mailer.py
from email.utils import (
    formataddr,
    formatdate,
    getaddresses,
    make_msgid,
)
from agent.errors import (
    InvalidRequest,
    RateLimitedError,
    UpstreamError,
)
from agent.config import (
    MAIL_ADDRESS,
    Settings,
)
from email.message import EmailMessage
from agent.accounts import Principal
from collections.abc import Callable
from pydantic import BaseModel
from agent.db import Database

import contextlib
import asyncio
import smtplib
import sqlite3
import time
import ssl

SYSTEM = "system"
OUTREACH = "outreach"
KINDS = (SYSTEM, OUTREACH)
SENT = "sent"
FAILED = "failed"
BLOCKED = "blocked"
PENDING = "pending"
SUPPRESSED_PURPOSES = frozenset({"routine"})
TOO_MANY_FOR_ONE = "Too many messages were sent to this address recently. Try again later."
INSERT_MAIL = (
    "INSERT INTO mail_log (ts, actor_id, actor, to_address, subject, kind, purpose, template, "
    "job_id, candidate_id, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
Transport = Callable[[Settings, EmailMessage], None]


def suppression_key(address: str) -> str:
    """Normalize an address for suppression checks.

    The address is lowercased and any plus tag is removed from the local part, so
    name+tag@example.com matches name@example.com.

    Args:
        address: The email address to normalize.

    Returns:
        The normalized address.
    """
    local, _, domain = address.strip().lower().rpartition("@")
    return f"{local.partition('+')[0]}@{domain}"


class MailEntry(BaseModel):
    """One row of the mail log, covering a sent, failed, blocked or pending message."""

    id: int
    ts: float
    actor_id: str | None = None
    actor: str = ""
    to_address: str
    subject: str
    kind: str
    purpose: str
    template: str = ""
    job_id: str = ""
    candidate_id: str = ""
    status: str
    error: str = ""
    message_id: str = ""


class Suppression(BaseModel):
    """An address that must not receive outreach or routine mail."""

    address: str
    reason: str = ""
    created: float
    added_by: str = ""


def deliver(settings: Settings, message: EmailMessage) -> None:
    """Send a message through the configured SMTP server.

    The connection uses SSL, STARTTLS or plain text as set in the settings, and logs in
    when a user name and password are set. The call blocks until the server answers.

    Args:
        settings: The settings that hold the SMTP host, port, security and credentials.
        message: The message to send.

    Raises:
        UpstreamError: when the login or recipient is refused or the server cannot be
            used. The message never contains the password.
    """
    mail = settings.mail
    host = settings.smtp_host or ""
    security = settings.smtp_security
    default = {"starttls": mail.starttls_port, "ssl": mail.ssl_port, "none": mail.plain_port}
    port = settings.smtp_port or default[security]
    context = ssl.create_default_context()
    try:
        if security == "ssl":
            client = smtplib.SMTP_SSL(host, port, timeout=mail.timeout_seconds, context=context)
        else:
            client = smtplib.SMTP(host, port, timeout=mail.timeout_seconds)
        with client:
            if security == "starttls":
                client.starttls(context=context)
            if settings.smtp_username and settings.smtp_password is not None:
                client.login(settings.smtp_username, settings.smtp_password.get_secret_value())
            client.send_message(message)
    except smtplib.SMTPAuthenticationError as error:
        raise UpstreamError("The mail server did not accept the user name and password.") from error
    except smtplib.SMTPRecipientsRefused as error:
        raise UpstreamError("The mail server refused the recipient address.") from error
    except (smtplib.SMTPException, OSError, ssl.SSLError) as error:
        raise UpstreamError(
            f"The mail server could not be used ({type(error).__name__})."
        ) from error


class Mailer:
    """Send email through SMTP with a log, rate limits and a suppression list.

    Every attempt, including blocked ones, is recorded in the mail log. Outreach and
    routine mail is never sent to a suppressed address, and the hourly limits are
    checked in the same transaction that records the attempt.
    """

    def __init__(
        self,
        settings: Settings,
        database: Database,
        clock: Callable[[], float] = time.time,
        transport: Transport = deliver,
    ):
        """Initialize the mailer with its settings, database and delivery function.

        Args:
            settings: The settings that hold the SMTP details and mail limits.
            database: The database that holds the mail log and the suppression list.
            clock: Callable that returns the current time in seconds since the epoch.
            transport: Callable that delivers one message; replaceable in tests.
        """
        self._settings = settings
        self._db = database
        self._clock = clock
        self._transport = transport

    @property
    def settings(self) -> Settings:
        """Return the settings the mailer currently uses."""
        return self._settings

    def use(self, settings: Settings) -> None:
        """Switch the mailer to new settings for all later sends.

        Args:
            settings: The settings to use from now on.
        """
        self._settings = settings

    def configured(self) -> bool:
        """Check whether SMTP sending is set up.

        Returns:
            True when the settings hold a complete SMTP configuration.
        """
        return self._settings.smtp_configured()

    def address(self, value: str) -> str:
        """Validate a single email address.

        Args:
            value: The address as typed; surrounding spaces are removed.

        Returns:
            The cleaned address.

        Raises:
            InvalidRequest: when the address is too long, malformed, or holds several
                addresses or a display name.
        """
        cleaned = value.strip()
        if (
            len(cleaned) > self._settings.mail.max_address_chars
            or not MAIL_ADDRESS.fullmatch(cleaned)
            or getaddresses([cleaned]) != [("", cleaned)]
        ):
            raise InvalidRequest("That email address does not look right.")
        return cleaned

    def _message(self, to: str, subject: str, body: str, reply_to: str | None) -> EmailMessage:
        """Build a plain-text email from the configured sender.

        Args:
            to: The validated recipient address.
            subject: The one-line subject.
            body: The message text.
            reply_to: An optional Reply-To address, validated here.

        Returns:
            The message with From, To, Subject, Date and Message-ID headers.

        Raises:
            InvalidRequest: when the reply-to address is invalid.
        """
        settings = self._settings
        sender = settings.smtp_from or ""
        message = EmailMessage()
        message["From"] = formataddr((settings.smtp_from_name or "", sender))
        message["To"] = to
        message["Subject"] = subject
        message["Date"] = formatdate(self._clock(), usegmt=True)
        message["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2])
        if reply_to:
            message["Reply-To"] = self.address(reply_to)
        message.set_content(body)
        return message

    async def send(
        self,
        to: str,
        subject: str,
        body: str,
        *,
        kind: str,
        purpose: str,
        actor: Principal | None = None,
        template: str = "",
        job_id: str = "",
        candidate_id: str = "",
        reply_to: str | None = None,
    ) -> MailEntry:
        """Validate, log and send one email.

        The delivery runs in a worker thread so the event loop is not blocked. A failed
        delivery is logged as failed and the error is raised again.

        Args:
            to: The recipient address.
            subject: The subject; whitespace is collapsed to single spaces.
            body: The message text.
            kind: Either system or outreach.
            purpose: What the message is for, such as routine or interview.
            actor: The signed-in user who sends it, if any.
            template: The id of the template the message came from, if any.
            job_id: The id of the related research job, if any.
            candidate_id: The id of the related candidate, if any.
            reply_to: An optional Reply-To address.

        Returns:
            The log entry of the sent message.

        Raises:
            InvalidRequest: when sending is not set up, an input is invalid, or the
                address is suppressed.
            RateLimitedError: when the hourly limit or the limit for one recipient is hit.
            UpstreamError: when the mail server fails.
        """
        mail = self._settings.mail
        if not self.configured():
            raise InvalidRequest("Email sending is not set up yet.")
        if kind not in KINDS:
            raise InvalidRequest("That kind of message does not exist.")
        address = self.address(to)
        subject = " ".join(subject.split())
        if not subject or len(subject) > mail.max_subject_chars:
            raise InvalidRequest(
                "A subject needs 1 to " + str(mail.max_subject_chars) + " characters."
            )
        if not body.strip() or len(body) > mail.max_body_chars:
            raise InvalidRequest("The text needs 1 to " + str(mail.max_body_chars) + " characters.")
        identifier = self._reserve(
            address, subject, kind, purpose, actor, template, job_id, candidate_id
        )
        message = self._message(address, subject, body, reply_to)
        try:
            await asyncio.to_thread(self._transport, self._settings, message)
        except UpstreamError as error:
            self._finish(identifier, FAILED, str(error), "")
            raise
        self._finish(identifier, SENT, "", str(message["Message-ID"]))
        entry = self.entry(identifier)
        if entry is None:
            raise InvalidRequest("The sent message could not be found in the log.")
        return entry

    def _reserve(
        self,
        address: str,
        subject: str,
        kind: str,
        purpose: str,
        actor: Principal | None,
        template: str,
        job_id: str,
        candidate_id: str,
    ) -> int:
        """Check suppression and rate limits and record a pending log row.

        Args:
            address: The validated recipient address.
            subject: The cleaned subject.
            kind: Either system or outreach.
            purpose: What the message is for.
            actor: The user who sends it, if any.
            template: The id of the template used, or empty.
            job_id: The id of the related job, or empty.
            candidate_id: The id of the related candidate, or empty.

        Returns:
            The id of the new pending log row.

        Raises:
            InvalidRequest: when the address is suppressed; a blocked row is logged.
            RateLimitedError: when the hourly outreach limit or the per-recipient system
                limit is reached.
        """
        now = self._clock()
        values = (
            now,
            actor.user_id if actor else None,
            actor.name if actor else "",
            address,
            subject,
            kind,
            purpose,
            template,
            job_id,
            candidate_id,
        )
        blocked = False
        crowded = False
        identifier = 0
        mail = self._settings.mail
        with self._db.transaction() as db:
            # Only outreach and routine mail honours the suppression list; other system mail is
            # still sent.
            if (kind == OUTREACH or purpose in SUPPRESSED_PURPOSES) and db.execute(
                "SELECT 1 FROM suppressions WHERE address IN (?, ?)",
                (address, suppression_key(address)),
            ).fetchone():
                db.execute(INSERT_MAIL, (*values, BLOCKED))
                blocked = True
            else:
                recent = 0
                if kind == OUTREACH:
                    recent = db.execute(
                        "SELECT COUNT(*) AS n FROM mail_log WHERE kind = ? "
                        "AND status IN (?, ?) AND ts > ?",
                        (OUTREACH, SENT, PENDING, now - 3600),
                    ).fetchone()["n"]
                same = db.execute(
                    "SELECT COUNT(*) AS n FROM mail_log WHERE kind = ? AND status IN (?, ?) "
                    "AND ts > ? AND to_address = ? COLLATE NOCASE",
                    (SYSTEM, SENT, PENDING, now - 3600, address),
                ).fetchone()["n"]
                crowded = kind == SYSTEM and same >= mail.system_per_recipient
                if recent < mail.send_per_hour and not crowded:
                    identifier = int(db.execute(INSERT_MAIL, (*values, PENDING)).lastrowid or 0)
        # The errors are raised after the transaction closes so that the blocked row stays in the
        # log.
        if blocked:
            raise InvalidRequest("This address asked not to be contacted.")
        if crowded:
            raise RateLimitedError(TOO_MANY_FOR_ONE)
        if not identifier:
            raise RateLimitedError("The hourly email limit was reached. Try again later.")
        return identifier

    def _finish(self, identifier: int, status: str, error: str, message_id: str) -> None:
        """Record the final status of a logged message.

        Args:
            identifier: The id of the log row.
            status: The final status, sent or failed.
            error: The error text; it is cut to 300 characters.
            message_id: The Message-ID header; it is cut to 200 characters.
        """
        self._db.run(
            "UPDATE mail_log SET status = ?, error = ?, message_id = ? WHERE id = ?",
            (status, error[:300], message_id[:200], identifier),
        )

    def entry(self, identifier: int) -> MailEntry | None:
        """Return one mail log entry.

        Args:
            identifier: The id of the log row.

        Returns:
            The entry, or None when it does not exist.
        """
        row = self._db.one("SELECT * FROM mail_log WHERE id = ?", (identifier,))
        return MailEntry.model_validate(dict(row)) if row else None

    def entries(self, before: int | None = None, limit: int | None = None) -> list[MailEntry]:
        """Return a page of mail log entries, newest first.

        Args:
            before: Return only entries with an id lower than this, to page backwards.
            limit: The page size; the configured default page size when not given.

        Returns:
            The matching entries.
        """
        rows = self._db.query(
            "SELECT * FROM mail_log WHERE (? IS NULL OR id < ?) ORDER BY id DESC LIMIT ?",
            (before, before, limit or self._settings.mail.log_page_size),
        )
        return [MailEntry.model_validate(dict(row)) for row in rows]

    def suppress(self, address: str, reason: str = "", by: str = "") -> Suppression:
        """Add an address to the suppression list.

        The address is stored in its normalized form, and adding one that is already listed
        changes nothing.

        Args:
            address: The address to suppress.
            reason: Why it is suppressed; cut to 200 characters.
            by: Who added it; cut to 80 characters.

        Returns:
            The suppression record.

        Raises:
            InvalidRequest: when the address is invalid or could not be saved.
        """
        cleaned = suppression_key(self.address(address))
        with contextlib.suppress(sqlite3.IntegrityError):
            self._db.run(
                "INSERT INTO suppressions (address, reason, created, added_by) VALUES (?, ?, ?, ?)",
                (cleaned, reason[:200], self._clock(), by[:80]),
            )
        found = self._db.one("SELECT * FROM suppressions WHERE address = ?", (cleaned,))
        if found is None:
            raise InvalidRequest("The address could not be saved.")
        return Suppression.model_validate(dict(found))

    def unsuppress(self, address: str) -> bool:
        """Remove an address from the suppression list.

        Args:
            address: The address to remove; its typed and normalized forms are both removed.

        Returns:
            True when something was removed.
        """
        return bool(
            self._db.run(
                "DELETE FROM suppressions WHERE address IN (?, ?)",
                (address.strip(), suppression_key(address)),
            )
        )

    def suppressed(self, address: str) -> bool:
        """Check whether an address is on the suppression list.

        Args:
            address: The address to check; its typed and normalized forms are both checked.

        Returns:
            True when the address is suppressed.
        """
        return (
            self._db.one(
                "SELECT 1 FROM suppressions WHERE address IN (?, ?)",
                (address.strip(), suppression_key(address)),
            )
            is not None
        )

    def suppressions(self) -> list[Suppression]:
        """Return the suppression list, newest first.

        Returns:
            All suppression records.
        """
        rows = self._db.query("SELECT * FROM suppressions ORDER BY created DESC")
        return [Suppression.model_validate(dict(row)) for row in rows]

    def purge(self) -> int:
        """Delete mail log entries older than the retention period.

        Returns:
            The number of deleted entries.
        """
        cutoff = self._clock() - self._settings.mail.log_retention_days * 86_400
        return self._db.run("DELETE FROM mail_log WHERE ts < ?", (cutoff,))
