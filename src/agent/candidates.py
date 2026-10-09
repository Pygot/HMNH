# src/agent/candidates.py
from agent.models import (
    Identity,
    PersonReport,
    SkillSearchReport,
)
from agent.urls import (
    normalize_web_url,
    profile_url,
)
from pydantic import (
    BaseModel,
    Field,
)
from agent.config import CandidateTuning
from agent.errors import InvalidRequest
from collections.abc import Callable
from agent.accounts import EMAIL
from agent.export import Report
from agent.db import Database
from datetime import date
from typing import Any

import unicodedata
import secrets
import sqlite3
import json
import time
import re

RESEARCH = "research"
MANUAL = "manual"
HIRE = "hire"
IMPORT = "import"
SORTS = {
    "updated": "candidates.updated DESC",
    "name": "candidates.name_key ASC",
    "rating": "candidates.rating IS NULL, candidates.rating DESC",
    "created": "candidates.created DESC",
}
SUMMARY_SQL = (
    "SELECT candidates.*, "
    "(SELECT COUNT(*) FROM hires WHERE hires.candidate_id = candidates.id) AS hire_count "
    "FROM candidates"
)
URL_WORDS = re.compile(r"^https?://", re.IGNORECASE)
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{6,40}$")
WORDS = re.compile(r"\s+")


class Link(BaseModel):
    """A web link saved for a candidate, with an optional label."""

    url: str
    label: str = ""


class Contact(BaseModel):
    """A contact detail such as an email or phone number for a candidate."""

    id: str
    candidate_id: str
    kind: str
    value: str
    label: str = ""
    source: str = MANUAL
    created: float
    created_by: str = ""


class Hire(BaseModel):
    """A record of a candidate being hired for a role, with its outcome."""

    id: str
    candidate_id: str
    role_title: str
    department: str = ""
    started: str = ""
    ended: str = ""
    outcome: str = "active"
    rating: int | None = None
    review: str = ""
    job_id: str = ""
    created: float
    updated: float
    created_by: str = ""


class Event(BaseModel):
    """A timeline entry recording something that happened to a candidate."""

    id: int
    candidate_id: str
    ts: float
    kind: str
    summary: str
    job_id: str = ""
    actor: str = ""


class Candidate(BaseModel):
    """A saved candidate with profile details, status, and rating."""

    id: str
    name: str
    headline: str = ""
    location: str = ""
    employer: str = ""
    skills: list[str] = Field(default_factory=list)
    status: str
    rating: float | None = None
    researched: int = 0
    tags: list[str] = Field(default_factory=list)
    notes: str = ""
    created: float
    updated: float
    created_by: str = ""
    hire_count: int = 0

    @property
    def hired_before(self) -> bool:
        """Return whether the candidate has at least one hire on record."""
        return self.hire_count > 0


class Seen(BaseModel):
    """What is already known about a person who was researched before."""

    candidate: Candidate
    ratings: list[tuple[float, float]]
    hires: list[Hire]
    last_researched: float | None = None


class Page(BaseModel):
    """One page of candidate search results with paging details."""

    items: list[Candidate]
    total: int
    offset: int
    size: int


def name_key(name: str) -> str:
    """Return a lowercase ASCII key used to compare names.

    Args:
        name: The name to normalise.

    Returns:
        The name with accents removed, case folded, and whitespace collapsed.
    """
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return WORDS.sub(" ", plain.casefold()).strip()


def link_key(url: str) -> str:
    """Return a canonical key used to compare profile links.

    Args:
        url: The link to reduce.

    Returns:
        The canonical link without scheme, leading www., or trailing slash, in
        lowercase.
    """
    canonical = profile_url(url) or normalize_web_url(url)
    return URL_WORDS.sub("", canonical).removeprefix("www.").rstrip("/").lower()


def _json(value: Any) -> str:
    """Serialize a value to compact JSON that keeps non-ASCII characters.

    Args:
        value: The value to serialize.

    Returns:
        The JSON text.
    """
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _list(value: str | None) -> list[str]:
    """Parse stored JSON text into a list of strings.

    Args:
        value: JSON text, or None.

    Returns:
        The items as strings, or an empty list when the text is missing, invalid,
        or not a JSON list.
    """
    try:
        loaded = json.loads(value) if value else []
    except ValueError:
        return []
    return [str(item) for item in loaded] if isinstance(loaded, list) else []


def _clean(value: str, limit: int) -> str:
    """Collapse whitespace in a string and cut it to a maximum length.

    Args:
        value: The text to clean.
        limit: The maximum number of characters to keep.

    Returns:
        The cleaned, truncated text.
    """
    return " ".join(value.split())[:limit]


class Candidates:
    """Store that manages candidates and their links, contacts, hires, and events.

    All state lives in the database. Time comes from an injectable clock that
    returns seconds since the epoch.
    """

    def __init__(
        self,
        database: Database,
        tuning: CandidateTuning,
        clock: Callable[[], float] = time.time,
    ):
        """Initialize the candidate store.

        Args:
            database: The database that holds the candidate tables.
            tuning: Limits, allowed statuses, and other candidate settings.
            clock: Callable returning the current time in seconds since the epoch.
        """
        self._db = database
        self._tuning = tuning
        self._clock = clock
        self._phone = re.compile(tuning.phone_pattern)

    def _candidate(self, row: sqlite3.Row) -> Candidate:
        """Build a Candidate from a database row.

        Args:
            row: A row from the candidates table, optionally with a hire count.

        Returns:
            The candidate, with skills and tags decoded from JSON.
        """
        values = dict(row)
        values["skills"] = _list(values["skills"])
        values["tags"] = _list(values["tags"])
        values.pop("name_key", None)
        return Candidate.model_validate(values)

    def _checked_status(self, status: str) -> str:
        """Return the status if it is a configured candidate status.

        Args:
            status: The status to check.

        Returns:
            The same status.

        Raises:
            InvalidRequest: when the status is not configured.
        """
        if status not in self._tuning.statuses:
            raise InvalidRequest("That status does not exist.")
        return status

    def _words(self, values: list[str], limit: int, size: int) -> list[str]:
        """Clean, deduplicate, and cap a list of short strings.

        Args:
            values: The strings to process.
            limit: The maximum number of items to keep.
            size: The maximum number of characters per item.

        Returns:
            The cleaned non-empty items in their original order without duplicates.
        """
        cleaned = [_clean(item, size) for item in values]
        return list(dict.fromkeys(item for item in cleaned if item))[:limit]

    def get(self, identifier: str) -> Candidate | None:
        """Return one candidate by id.

        Args:
            identifier: The candidate id.

        Returns:
            The candidate with its hire count, or None when it does not exist.
        """
        row = self._db.one(SUMMARY_SQL + " WHERE candidates.id = ?", (identifier,))
        return self._candidate(row) if row else None

    def names(self) -> list[tuple[str, str]]:
        """Return the id and name of every candidate.

        Returns:
            A list of (id, name) pairs.
        """
        return [
            (row["id"], row["name"]) for row in self._db.query("SELECT id, name FROM candidates")
        ]

    def page(
        self,
        *,
        query: str = "",
        status: str = "",
        minimum: float | None = None,
        hired: bool | None = None,
        sort: str = "updated",
        offset: int = 0,
        size: int | None = None,
    ) -> Page:
        """Return one page of candidates matching the filters.

        The query is matched against the normalised name, employer, headline, and tags.

        Args:
            query: Text to search for. Empty matches everything.
            status: Only include this status. Empty includes every status.
            minimum: Only include candidates rated at least this value.
            hired: True for candidates with hires, False for those without, None for any.
            sort: A key of the sort table. Unknown keys sort by last update.
            offset: Number of matches to skip. Negative values count as zero.
            size: Page size. Defaults to the configured page size.

        Returns:
            The page with the matching candidates and the total number of matches.
        """
        size = size or self._tuning.page_size
        needle = f"%{name_key(query)}%" if query.strip() else "%"
        where = (
            " WHERE (candidates.name_key LIKE ? OR candidates.employer LIKE ? "
            "OR candidates.headline LIKE ? OR candidates.tags LIKE ?) "
            "AND (? = '' OR candidates.status = ?) "
            "AND (? IS NULL OR candidates.rating >= ?) "
            "AND (? IS NULL OR (EXISTS (SELECT 1 FROM hires WHERE hires.candidate_id = "
            "candidates.id)) = ?)"
        )
        raw = f"%{query.strip().lower()}%" if query.strip() else "%"
        params = (
            needle,
            raw,
            raw,
            raw,
            status,
            status,
            minimum,
            minimum,
            None if hired is None else int(hired),
            None if hired is None else int(hired),
        )
        total = self._db.one(
            "SELECT COUNT(*) AS n FROM candidates" + where,
            params,
        )
        order = SORTS.get(sort, SORTS["updated"])
        rows = self._db.query(
            SUMMARY_SQL + where + f" ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, size, max(0, offset)),
        )
        return Page(
            items=[self._candidate(row) for row in rows],
            total=total["n"] if total else 0,
            offset=max(0, offset),
            size=size,
        )

    def count(self) -> int:
        """Return the total number of candidates.

        Returns:
            The number of stored candidates.
        """
        row = self._db.one("SELECT COUNT(*) AS n FROM candidates")
        return row["n"] if row else 0

    def create(
        self,
        name: str,
        *,
        headline: str = "",
        location: str = "",
        employer: str = "",
        skills: list[str] | None = None,
        status: str | None = None,
        notes: str = "",
        tags: list[str] | None = None,
        links: list[str] | None = None,
        actor: str = "",
        source: str = MANUAL,
        rating: float | None = None,
        researched: int = 0,
        created: float | None = None,
        identifier: str | None = None,
    ) -> Candidate:
        """Create a candidate and return it.

        The name must have at least two characters after cleaning. A requested id is used
        only when it has a valid shape and is not taken, otherwise a random id is made.
        The rating is clamped to the range 0 to 5. A "created" event is logged.

        Args:
            name: The candidate's full name.
            headline: Short professional headline.
            location: Where the candidate is based.
            employer: Current employer.
            skills: Skill names, deduplicated and capped.
            status: Initial status. Defaults to the first configured status.
            notes: Free-form notes, stripped and capped.
            tags: Tags, deduplicated and capped.
            links: Profile or web links to attach.
            actor: Who created the candidate.
            source: How the candidate was found, recorded in the event summary.
            rating: Initial rating from 0 to 5.
            researched: Number of times the candidate was already researched.
            created: Creation time in seconds since the epoch. Defaults to now.
            identifier: Preferred candidate id, for example from an import.

        Returns:
            The saved candidate.

        Raises:
            InvalidRequest: when the name is too short, the status is unknown, the limit
                of candidates is reached, a link is not an http(s) link, or saving fails.
        """
        tuning = self._tuning
        name = _clean(name, tuning.max_name_chars)
        if len(name) < 2:
            raise InvalidRequest("A candidate needs a name.")
        chosen = self._checked_status(status or tuning.first_status)
        wanted = identifier if identifier and ID_PATTERN.fullmatch(identifier) else None
        identifier = wanted or secrets.token_urlsafe(9)
        now = created if created is not None else self._clock()
        with self._db.transaction() as db:
            total = db.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
            # A requested id that is already taken is replaced by a random one, so an import cannot
            # overwrite or collide with an existing candidate.
            if wanted and db.execute("SELECT 1 FROM candidates WHERE id = ?", (wanted,)).fetchone():
                identifier = secrets.token_urlsafe(9)
            if total >= tuning.max_candidates:
                raise InvalidRequest("There are too many candidates.")
            db.execute(
                "INSERT INTO candidates (id, name, name_key, headline, location, employer, "
                "skills, status, tags, notes, created, updated, created_by, rating, researched) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    identifier,
                    name,
                    name_key(name),
                    _clean(headline, tuning.max_text_chars),
                    _clean(location, tuning.max_text_chars),
                    _clean(employer, tuning.max_text_chars),
                    _json(self._words(skills or [], tuning.max_skills, tuning.max_text_chars)),
                    chosen,
                    _json(self._words(tags or [], tuning.max_tags, tuning.tag_chars)),
                    notes.strip()[: tuning.max_notes_chars],
                    now,
                    now,
                    actor,
                    None if rating is None else max(0.0, min(5.0, float(rating))),
                    max(0, int(researched)),
                ),
            )
            for url in links or []:
                self._insert_link(db, identifier, url)
            self._event(db, identifier, "created", f"Added ({source})", "", actor)
        created = self.get(identifier)
        if created is None:
            raise InvalidRequest("The candidate could not be saved.")
        return created

    def update(
        self,
        identifier: str,
        *,
        name: str | None = None,
        headline: str | None = None,
        location: str | None = None,
        employer: str | None = None,
        skills: list[str] | None = None,
        status: str | None = None,
        notes: str | None = None,
        tags: list[str] | None = None,
        actor: str = "",
    ) -> Candidate:
        """Change the given fields of a candidate and return it.

        Fields left as None keep their value. The update time is refreshed and a status
        event is logged when the status changes.

        Args:
            identifier: The candidate id.
            name: New name.
            headline: New headline.
            location: New location.
            employer: New employer.
            skills: New skill list, replacing the old one.
            status: New status.
            notes: New notes, replacing the old ones.
            tags: New tag list, replacing the old one.
            actor: Who made the change.

        Returns:
            The updated candidate.

        Raises:
            InvalidRequest: when the candidate does not exist, the new name is too short,
                or the status is unknown.
        """
        tuning = self._tuning
        with self._db.transaction() as db:
            row = db.execute("SELECT * FROM candidates WHERE id = ?", (identifier,)).fetchone()
            if row is None:
                raise InvalidRequest("That candidate does not exist.")
            new_name = row["name"] if name is None else _clean(name, tuning.max_name_chars)
            if len(new_name) < 2:
                raise InvalidRequest("A candidate needs a name.")
            new_status = row["status"] if status is None else self._checked_status(status)
            db.execute(
                "UPDATE candidates SET name = ?, name_key = ?, headline = ?, location = ?, "
                "employer = ?, skills = ?, status = ?, tags = ?, notes = ?, updated = ? "
                "WHERE id = ?",
                (
                    new_name,
                    name_key(new_name),
                    row["headline"]
                    if headline is None
                    else _clean(headline, tuning.max_text_chars),
                    row["location"]
                    if location is None
                    else _clean(location, tuning.max_text_chars),
                    row["employer"]
                    if employer is None
                    else _clean(employer, tuning.max_text_chars),
                    row["skills"]
                    if skills is None
                    else _json(self._words(skills, tuning.max_skills, tuning.max_text_chars)),
                    new_status,
                    row["tags"]
                    if tags is None
                    else _json(self._words(tags, tuning.max_tags, tuning.tag_chars)),
                    row["notes"] if notes is None else notes.strip()[: tuning.max_notes_chars],
                    self._clock(),
                    identifier,
                ),
            )
            if new_status != row["status"]:
                self._event(
                    db, identifier, "status", f"Status: {row['status']} to {new_status}", "", actor
                )
        updated = self.get(identifier)
        if updated is None:
            raise InvalidRequest("That candidate does not exist.")
        return updated

    def delete(self, identifier: str) -> bool:
        """Delete a candidate.

        Args:
            identifier: The candidate id.

        Returns:
            True when a candidate was deleted.
        """
        return bool(self._db.run("DELETE FROM candidates WHERE id = ?", (identifier,)))

    def _event(
        self,
        db: sqlite3.Connection,
        identifier: str,
        kind: str,
        summary: str,
        job_id: str = "",
        actor: str = "",
    ) -> None:
        """Add an event to a candidate's timeline using the current time.

        Args:
            db: The connection of the open transaction.
            identifier: The candidate id.
            kind: The event kind.
            summary: What happened, cut to the configured text length.
            job_id: The research job that caused the event, if any.
            actor: Who caused the event.
        """
        db.execute(
            "INSERT INTO candidate_events (candidate_id, ts, kind, summary, job_id, actor) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                identifier,
                self._clock(),
                kind,
                summary[: self._tuning.max_text_chars],
                job_id,
                actor,
            ),
        )

    def import_event(
        self, identifier: str, ts: float, kind: str, summary: str, actor: str = ""
    ) -> None:
        """Add an event with a given timestamp, used when importing history.

        Opens its own transaction.

        Args:
            identifier: The candidate id.
            ts: Event time in seconds since the epoch.
            kind: The event kind, cut to 20 characters.
            summary: What happened, cut to the configured text length.
            actor: Who caused the event.
        """
        with self._db.transaction() as db:
            self._event_at(db, identifier, ts, kind, summary, "", actor)

    def _event_at(
        self,
        db: sqlite3.Connection,
        identifier: str,
        ts: float,
        kind: str,
        summary: str,
        job_id: str,
        actor: str,
    ) -> None:
        """Add an event with an explicit timestamp to a candidate's timeline.

        Args:
            db: The connection of the open transaction.
            identifier: The candidate id.
            ts: Event time in seconds since the epoch.
            kind: The event kind, cut to 20 characters.
            summary: What happened, cut to the configured text length.
            job_id: The research job that caused the event, if any.
            actor: Who caused the event.
        """
        db.execute(
            "INSERT INTO candidate_events (candidate_id, ts, kind, summary, job_id, actor) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (identifier, ts, kind[:20], summary[: self._tuning.max_text_chars], job_id, actor),
        )

    def note(self, identifier: str, summary: str, actor: str = "", job_id: str = "") -> None:
        """Add a note to a candidate's timeline and mark the candidate as updated.

        Args:
            identifier: The candidate id.
            summary: The note text.
            actor: Who wrote the note.
            job_id: The related research job, if any.

        Raises:
            InvalidRequest: when the candidate does not exist.
        """
        with self._db.transaction() as db:
            if (
                db.execute("SELECT 1 FROM candidates WHERE id = ?", (identifier,)).fetchone()
                is None
            ):
                raise InvalidRequest("That candidate does not exist.")
            self._event(db, identifier, "note", summary, job_id, actor)
            db.execute(
                "UPDATE candidates SET updated = ? WHERE id = ?", (self._clock(), identifier)
            )

    def export_events(self, identifier: str, limit: int) -> list[Event]:
        """Return a candidate's events oldest first, for export.

        Args:
            identifier: The candidate id.
            limit: The maximum number of events to return.

        Returns:
            The events in the order they were recorded.
        """
        rows = self._db.query(
            "SELECT * FROM candidate_events WHERE candidate_id = ? ORDER BY id LIMIT ?",
            (identifier, limit),
        )
        return [Event.model_validate(dict(row)) for row in rows]

    def events(self, identifier: str) -> list[Event]:
        """Return the most recent events of a candidate.

        Args:
            identifier: The candidate id.

        Returns:
            The newest events first, at most the configured number to show.
        """
        rows = self._db.query(
            "SELECT * FROM candidate_events WHERE candidate_id = ? ORDER BY id DESC LIMIT ?",
            (identifier, self._tuning.events_shown),
        )
        return [Event.model_validate(dict(row)) for row in rows]

    def _insert_link(
        self, db: sqlite3.Connection, identifier: str, url: str, label: str = ""
    ) -> bool:
        """Insert a link for a candidate inside an open transaction.

        Links are deduplicated by their canonical key.

        Args:
            db: The connection of the open transaction.
            identifier: The candidate id.
            url: The link, which must start with http:// or https://.
            label: Optional display label.

        Returns:
            True when a new link was stored, False for a duplicate or when the candidate
            already has the maximum number of links.

        Raises:
            InvalidRequest: when the link is not an http(s) link or is too long.
        """
        cleaned = url.strip()
        if not URL_WORDS.match(cleaned) or len(cleaned) > self._tuning.max_text_chars * 3:
            raise InvalidRequest("A link must start with http:// or https://.")
        count = db.execute(
            "SELECT COUNT(*) AS n FROM candidate_links WHERE candidate_id = ?", (identifier,)
        ).fetchone()["n"]
        if count >= self._tuning.max_links:
            return False
        before = db.total_changes
        db.execute(
            "INSERT OR IGNORE INTO candidate_links (candidate_id, url_key, url, label) "
            "VALUES (?, ?, ?, ?)",
            (identifier, link_key(cleaned), cleaned, _clean(label, self._tuning.max_text_chars)),
        )
        return db.total_changes > before

    def add_link(self, identifier: str, url: str, label: str = "") -> bool:
        """Add a link to a candidate.

        Args:
            identifier: The candidate id.
            url: The link, which must start with http:// or https://.
            label: Optional display label.

        Returns:
            True when the link was added, False for a duplicate or when the limit is reached.

        Raises:
            InvalidRequest: when the candidate does not exist or the link is invalid.
        """
        with self._db.transaction() as db:
            if (
                db.execute("SELECT 1 FROM candidates WHERE id = ?", (identifier,)).fetchone()
                is None
            ):
                raise InvalidRequest("That candidate does not exist.")
            return self._insert_link(db, identifier, url, label)

    def remove_link(self, identifier: str, url_key: str) -> bool:
        """Remove a link from a candidate.

        Args:
            identifier: The candidate id.
            url_key: The canonical key of the link, as produced by link_key.

        Returns:
            True when a link was removed.
        """
        return bool(
            self._db.run(
                "DELETE FROM candidate_links WHERE candidate_id = ? AND url_key = ?",
                (identifier, url_key),
            )
        )

    def links(self, identifier: str) -> list[Link]:
        """Return the links of a candidate.

        Args:
            identifier: The candidate id.

        Returns:
            The links ordered by URL.
        """
        rows = self._db.query(
            "SELECT url, label, url_key FROM candidate_links WHERE candidate_id = ? ORDER BY url",
            (identifier,),
        )
        return [Link(url=row["url"], label=row["label"]) for row in rows]

    def contacts(self, identifier: str) -> list[Contact]:
        """Return the contact details of a candidate.

        Args:
            identifier: The candidate id.

        Returns:
            The contacts ordered by kind and then creation time.
        """
        rows = self._db.query(
            "SELECT * FROM contacts WHERE candidate_id = ? ORDER BY kind, created", (identifier,)
        )
        return [Contact.model_validate(dict(row)) for row in rows]

    def _contact_value(self, kind: str, value: str) -> str:
        """Validate a contact value for its kind and return it in stored form.

        Email addresses are lowercased. Phone numbers must match the configured pattern.
        Websites must start with http:// or https://.

        Args:
            kind: The contact kind, such as email or phone.
            value: The raw value.

        Returns:
            The cleaned value.

        Raises:
            InvalidRequest: when the kind is unknown or the value is empty, too long, or
                not valid for its kind.
        """
        cleaned = value.strip()
        if kind not in self._tuning.contact_kinds:
            raise InvalidRequest("That kind of contact does not exist.")
        if kind == "email":
            if not EMAIL.fullmatch(cleaned):
                raise InvalidRequest("That email address does not look right.")
            return cleaned.lower()
        if kind == "phone":
            if not self._phone.fullmatch(cleaned):
                raise InvalidRequest("That phone number does not look right.")
            return cleaned
        if kind == "website" and not URL_WORDS.match(cleaned):
            raise InvalidRequest("A website must start with http:// or https://.")
        if not cleaned or len(cleaned) > self._tuning.max_text_chars * 2:
            raise InvalidRequest("That contact detail is empty or too long.")
        return cleaned

    def _insert_contact(
        self,
        db: sqlite3.Connection,
        identifier: str,
        kind: str,
        value: str,
        label: str,
        source: str,
        actor: str,
    ) -> bool:
        """Insert a contact detail inside an open transaction.

        Exact duplicates are ignored.

        Args:
            db: The connection of the open transaction.
            identifier: The candidate id.
            kind: The contact kind.
            value: The raw contact value.
            label: Optional display label.
            source: Where the detail came from.
            actor: Who added the detail.

        Returns:
            True when a new contact was stored, False for a duplicate.

        Raises:
            InvalidRequest: when the value is invalid or the candidate already has the
                maximum number of contacts.
        """
        cleaned = self._contact_value(kind, value)
        count = db.execute(
            "SELECT COUNT(*) AS n FROM contacts WHERE candidate_id = ?", (identifier,)
        ).fetchone()["n"]
        if count >= self._tuning.max_contacts:
            raise InvalidRequest("This candidate has too many contact details.")
        before = db.total_changes
        db.execute(
            "INSERT OR IGNORE INTO contacts (id, candidate_id, kind, value, label, source, "
            "created, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                secrets.token_urlsafe(9),
                identifier,
                kind,
                cleaned,
                _clean(label, self._tuning.max_text_chars),
                source,
                self._clock(),
                actor,
            ),
        )
        return db.total_changes > before

    def add_contact(
        self,
        identifier: str,
        kind: str,
        value: str,
        *,
        label: str = "",
        source: str = MANUAL,
        actor: str = "",
    ) -> bool:
        """Add a contact detail to a candidate and log an event.

        Args:
            identifier: The candidate id.
            kind: The contact kind, such as email or phone.
            value: The contact value.
            label: Optional display label.
            source: Where the detail came from.
            actor: Who added the detail.

        Returns:
            True when a contact was added, False for a duplicate.

        Raises:
            InvalidRequest: when the candidate does not exist, the value is invalid, or
                the contact limit is reached.
        """
        with self._db.transaction() as db:
            if (
                db.execute("SELECT 1 FROM candidates WHERE id = ?", (identifier,)).fetchone()
                is None
            ):
                raise InvalidRequest("That candidate does not exist.")
            added = self._insert_contact(db, identifier, kind, value, label, source, actor)
            if added:
                self._event(db, identifier, "contact", f"Added a {kind}", "", actor)
            return added

    def remove_contact(self, identifier: str, contact_id: str, actor: str = "") -> bool:
        """Remove a contact detail from a candidate and log an event.

        Args:
            identifier: The candidate id.
            contact_id: The id of the contact to remove.
            actor: Who removed the detail.

        Returns:
            True when a contact was removed.
        """
        with self._db.transaction() as db:
            removed = db.execute(
                "DELETE FROM contacts WHERE id = ? AND candidate_id = ?", (contact_id, identifier)
            ).rowcount
            if removed:
                self._event(db, identifier, "contact", "Removed a contact detail", "", actor)
            return bool(removed)

    def hires(self, identifier: str) -> list[Hire]:
        """Return the hire records of a candidate.

        Args:
            identifier: The candidate id.

        Returns:
            The hires, newest first.
        """
        rows = self._db.query(
            "SELECT * FROM hires WHERE candidate_id = ? ORDER BY created DESC", (identifier,)
        )
        return [Hire.model_validate(dict(row)) for row in rows]

    def _day(self, value: str) -> str:
        """Validate a date and return it in ISO form.

        Args:
            value: A date such as 2026-03-01, or empty text.

        Returns:
            The ISO date, or an empty string when the value is blank.

        Raises:
            InvalidRequest: when the value is not an ISO date.
        """
        cleaned = value.strip()
        if not cleaned:
            return ""
        try:
            return date.fromisoformat(cleaned).isoformat()
        except ValueError as error:
            raise InvalidRequest("Dates look like 2026-03-01.") from error

    def _grade(self, value: int | None) -> int | None:
        """Check that a hire rating is inside the allowed range.

        Args:
            value: The rating, or None.

        Returns:
            The same rating, or None.

        Raises:
            InvalidRequest: when the rating is outside the configured low and high bounds.
        """
        if value is None:
            return None
        if not self._tuning.rating_low <= value <= self._tuning.rating_high:
            raise InvalidRequest(
                f"A rating is a whole number from {self._tuning.rating_low} "
                f"to {self._tuning.rating_high}."
            )
        return value

    def hire(
        self,
        identifier: str,
        role_title: str,
        *,
        department: str = "",
        started: str = "",
        contacts: list[tuple[str, str]] | None = None,
        job_id: str = "",
        actor: str = "",
        set_status: bool = True,
    ) -> Hire:
        """Record that a candidate was hired and return the hire.

        The hire starts with the outcome "active". By default the candidate's status
        becomes the configured hired status. Contact details are saved with the source
        "hire" and a hire event is logged.

        Args:
            identifier: The candidate id.
            role_title: The role the person was hired for.
            department: The department of the role.
            started: Start date in ISO form.
            contacts: Pairs of (kind, value) contact details to save for the candidate.
            job_id: The related job, if any.
            actor: Who recorded the hire.
            set_status: Whether to set the candidate's status to the hired status.

        Returns:
            The saved hire.

        Raises:
            InvalidRequest: when the role is empty, the candidate does not exist, the
                hire limit is reached, the date is invalid, or a contact is invalid.
        """
        tuning = self._tuning
        title = _clean(role_title, tuning.max_text_chars)
        if not title:
            raise InvalidRequest("Say which role the person was hired for.")
        hire_id = secrets.token_urlsafe(9)
        now = self._clock()
        with self._db.transaction() as db:
            if (
                db.execute("SELECT 1 FROM candidates WHERE id = ?", (identifier,)).fetchone()
                is None
            ):
                raise InvalidRequest("That candidate does not exist.")
            count = db.execute(
                "SELECT COUNT(*) AS n FROM hires WHERE candidate_id = ?", (identifier,)
            ).fetchone()["n"]
            if count >= tuning.max_hires:
                raise InvalidRequest("This candidate has too many hires on record.")
            db.execute(
                "INSERT INTO hires (id, candidate_id, role_title, department, started, ended, "
                "outcome, rating, review, job_id, created, updated, created_by) "
                "VALUES (?, ?, ?, ?, ?, '', 'active', NULL, '', ?, ?, ?, ?)",
                (
                    hire_id,
                    identifier,
                    title,
                    _clean(department, tuning.max_text_chars),
                    self._day(started),
                    job_id,
                    now,
                    now,
                    actor,
                ),
            )
            if set_status:
                db.execute(
                    "UPDATE candidates SET status = ?, updated = ? WHERE id = ?",
                    (tuning.hired_status, now, identifier),
                )
            imported = 0
            for kind, value in contacts or []:
                if value.strip() and self._insert_contact(
                    db, identifier, kind, value, "", HIRE, actor
                ):
                    imported += 1
            self._event(
                db,
                identifier,
                "hire",
                f"Hired as {title}" + (f"; {imported} contact details saved" if imported else ""),
                job_id,
                actor,
            )
        found = self.hire_of(hire_id)
        if found is None:
            raise InvalidRequest("The hire could not be saved.")
        return found

    def hire_of(self, hire_id: str) -> Hire | None:
        """Return one hire by id.

        Args:
            hire_id: The hire id.

        Returns:
            The hire, or None when it does not exist.
        """
        row = self._db.one("SELECT * FROM hires WHERE id = ?", (hire_id,))
        return Hire.model_validate(dict(row)) if row else None

    def update_hire(
        self,
        identifier: str,
        hire_id: str,
        *,
        role_title: str | None = None,
        department: str | None = None,
        started: str | None = None,
        ended: str | None = None,
        outcome: str | None = None,
        rating: int | None = None,
        review: str | None = None,
        clear_rating: bool = False,
        actor: str = "",
    ) -> Hire:
        """Change the given fields of a hire and return it.

        Fields left as None keep their value. When the outcome changes to completed,
        left, or dismissed and the candidate has no other active hire, the candidate's
        status becomes alumni. A hire event is logged when the outcome changes.

        Args:
            identifier: The candidate id the hire belongs to.
            hire_id: The hire id.
            role_title: New role title.
            department: New department.
            started: New start date in ISO form.
            ended: New end date in ISO form.
            outcome: New outcome, one of the configured hire outcomes.
            rating: New rating within the configured bounds.
            review: New review text, stripped and capped.
            clear_rating: Remove the rating, ignoring the rating argument.
            actor: Who made the change.

        Returns:
            The updated hire.

        Raises:
            InvalidRequest: when the hire does not exist, the outcome is unknown, the role
                is empty, a date or rating is invalid.
        """
        tuning = self._tuning
        with self._db.transaction() as db:
            row = db.execute(
                "SELECT * FROM hires WHERE id = ? AND candidate_id = ?", (hire_id, identifier)
            ).fetchone()
            if row is None:
                raise InvalidRequest("That hire does not exist.")
            new_outcome = row["outcome"] if outcome is None else outcome
            if new_outcome not in tuning.hire_outcomes:
                raise InvalidRequest("That outcome does not exist.")
            new_title = (
                row["role_title"]
                if role_title is None
                else _clean(role_title, tuning.max_text_chars)
            )
            if not new_title:
                raise InvalidRequest("Say which role the person was hired for.")
            new_rating = (
                None if clear_rating else (row["rating"] if rating is None else self._grade(rating))
            )
            db.execute(
                "UPDATE hires SET role_title = ?, department = ?, started = ?, ended = ?, "
                "outcome = ?, rating = ?, review = ?, updated = ? WHERE id = ?",
                (
                    new_title,
                    row["department"]
                    if department is None
                    else _clean(department, tuning.max_text_chars),
                    row["started"] if started is None else self._day(started),
                    row["ended"] if ended is None else self._day(ended),
                    new_outcome,
                    new_rating,
                    row["review"] if review is None else review.strip()[: tuning.max_notes_chars],
                    self._clock(),
                    hire_id,
                ),
            )
            if new_outcome != row["outcome"]:
                # The candidate only becomes an alumnus when no other hire is still active.
                alumni = new_outcome in {"completed", "left", "dismissed"}
                if alumni:
                    active = db.execute(
                        "SELECT COUNT(*) AS n FROM hires WHERE candidate_id = ? "
                        "AND outcome = 'active' AND id != ?",
                        (identifier, hire_id),
                    ).fetchone()["n"]
                    if not active:
                        db.execute(
                            "UPDATE candidates SET status = 'alumni', updated = ? WHERE id = ?",
                            (self._clock(), identifier),
                        )
                self._event(db, identifier, "hire", f"Hire outcome: {new_outcome}", "", actor)
        found = self.hire_of(hire_id)
        if found is None:
            raise InvalidRequest("That hire does not exist.")
        return found

    def delete_hire(self, identifier: str, hire_id: str) -> bool:
        """Delete a hire record.

        Args:
            identifier: The candidate id the hire belongs to.
            hire_id: The hire id.

        Returns:
            True when a hire was deleted.
        """
        return bool(
            self._db.run(
                "DELETE FROM hires WHERE id = ? AND candidate_id = ?", (hire_id, identifier)
            )
        )

    def find_match(
        self, identity: Identity, extra_links: list[str] | None = None
    ) -> Candidate | None:
        """Find the stored candidate that matches a researched identity.

        Links are checked first, using canonical link keys. Otherwise the normalised name
        must match and the location or an employer must also agree. When more than one
        candidate fits, the match is ambiguous and nothing is returned.

        Args:
            identity: The researched person's identity.
            extra_links: More links to check besides the identity's own links.

        Returns:
            The matching candidate, or None.
        """
        keys = [link_key(url) for url in [*identity.links, *(extra_links or [])]]
        if keys:
            marks = ",".join("?" for _ in keys)
            row = self._db.one(
                "SELECT candidate_id FROM candidate_links WHERE url_key IN (" + marks + ") "
                "ORDER BY candidate_id LIMIT 1",
                tuple(keys),
            )
            if row:
                return self.get(row["candidate_id"])
        rows = self._db.query(
            SUMMARY_SQL + " WHERE candidates.name_key = ?", (name_key(identity.name),)
        )
        wanted_places = {name_key(identity.location or "")} - {""}
        wanted_employers = {name_key(item) for item in identity.employers} - {""}
        close = []
        for row in rows:
            candidate = self._candidate(row)
            place = {name_key(candidate.location)} - {""}
            employer = {name_key(candidate.employer)} - {""}
            if (wanted_places and wanted_places & place) or (
                wanted_employers and wanted_employers & employer
            ):
                close.append(candidate)
        # Several equally good name matches are ambiguous, so none is returned to avoid merging two
        # different people.
        return close[0] if len(close) == 1 else None

    def record(self, report: Report, job_id: str, actor: str = "research") -> list[Candidate]:
        """Save the people found in a research report as candidates.

        A person report gives one candidate and a skill search report gives one candidate
        per ranked result. Each is matched to an existing candidate or created, rated, and
        logged as researched. Other report types are ignored.

        Args:
            report: The finished research report.
            job_id: The research job that produced the report.
            actor: Who is credited with the change.

        Returns:
            The saved candidates, empty for other report types.
        """
        if isinstance(report, PersonReport):
            return [
                self._record_one(
                    report.identity,
                    report.rating.overall,
                    report.rating.target,
                    job_id,
                    actor,
                )
            ]
        if isinstance(report, SkillSearchReport):
            found = []
            for ranked in report.candidates:
                identity = Identity(name=ranked.name, links=[ranked.profile_url])
                found.append(
                    self._record_one(
                        identity, ranked.rating.overall, ranked.rating.target, job_id, actor
                    )
                )
            return found
        return []

    def _record_one(
        self, identity: Identity, rating: float, target: str, job_id: str, actor: str
    ) -> Candidate:
        """Create or update the candidate for one researched person.

        The candidate is found with find_match or created with the research source. Then
        the rating is stored, the researched counter goes up, blank location, employer,
        and headline are filled in, skills are merged up to the limit, links are added, and
        a research event is logged.

        Args:
            identity: The researched person's identity.
            rating: The overall rating from 0 to 5.
            target: The role or target the rating was made for.
            job_id: The research job that produced the rating.
            actor: Who is credited with the change.

        Returns:
            The saved candidate.

        Raises:
            InvalidRequest: when the candidate cannot be found after saving.
        """
        tuning = self._tuning
        match = self.find_match(identity)
        summary = f"Researched: {rating:.1f} / 5 as {target}"
        if match is None:
            match = self.create(
                identity.name,
                headline=", ".join(identity.roles[:1]),
                location=identity.location or "",
                employer=identity.employers[0] if identity.employers else "",
                skills=list(identity.skills),
                links=[str(link) for link in identity.links],
                actor=actor,
                source=RESEARCH,
            )
        with self._db.transaction() as db:
            row = db.execute("SELECT * FROM candidates WHERE id = ?", (match.id,)).fetchone()
            if row is None:
                raise InvalidRequest("That candidate does not exist.")
            skills = list(dict.fromkeys([*_list(row["skills"]), *identity.skills]))[
                : tuning.max_skills
            ]
            db.execute(
                "UPDATE candidates SET rating = ?, researched = researched + 1, location = ?, "
                "employer = ?, headline = ?, skills = ?, updated = ? WHERE id = ?",
                (
                    rating,
                    row["location"] or _clean(identity.location or "", tuning.max_text_chars),
                    row["employer"]
                    or _clean(
                        identity.employers[0] if identity.employers else "", tuning.max_text_chars
                    ),
                    row["headline"]
                    or _clean(identity.roles[0] if identity.roles else "", tuning.max_text_chars),
                    _json(skills),
                    self._clock(),
                    match.id,
                ),
            )
            for url in identity.links:
                self._insert_link(db, match.id, str(url))
            self._event(db, match.id, "research", summary, job_id, actor)
        found = self.get(match.id)
        if found is None:
            raise InvalidRequest("That candidate does not exist.")
        return found

    def seen(self, identity: Identity, current_job: str = "") -> Seen | None:
        """Return what is known about a person researched before.

        Past ratings come from earlier research events of other jobs, newest first and
        capped at the configured history length.

        Args:
            identity: The researched person's identity.
            current_job: A job id whose events are left out of the history.

        Returns:
            The earlier ratings, hires, and candidate, or None when the person is unknown or
            has no earlier ratings, no hires, and was researched at most once.
        """
        match = self.find_match(identity)
        if match is None:
            return None
        rows = self._db.query(
            "SELECT ts, summary, job_id FROM candidate_events WHERE candidate_id = ? "
            "AND kind = 'research' AND job_id != ? ORDER BY id DESC LIMIT ?",
            (match.id, current_job, self._tuning.history_ratings),
        )
        ratings = []
        for row in rows:
            found = re.search(r"Researched: ([0-9.]+)", row["summary"])
            if found:
                ratings.append((row["ts"], float(found.group(1))))
        hires = self.hires(match.id)
        if not ratings and not hires and match.researched <= 1:
            return None
        return Seen(
            candidate=match,
            ratings=ratings,
            hires=hires,
            last_researched=ratings[0][0] if ratings else None,
        )

    def stats(self) -> dict[str, Any]:
        """Count candidates by status.

        Returns:
            A mapping from status to the number of candidates with that status.
        """
        rows = self._db.query("SELECT status, COUNT(*) AS n FROM candidates GROUP BY status")
        return {row["status"]: row["n"] for row in rows}

    def purge(self) -> int:
        """Delete candidates that have not been updated within the retention period.

        Candidates whose status is in the configured kept statuses are never deleted.

        Returns:
            The number of deleted candidates, or 0 when retention is disabled.
        """
        days = self._tuning.retention_days
        if not days:
            return 0
        kept = self._tuning.kept_statuses
        marks = ",".join("?" for _ in kept)
        return self._db.run(
            "DELETE FROM candidates WHERE updated < ? AND status NOT IN (" + marks + ")",
            (self._clock() - days * 86_400, *kept),
        )
