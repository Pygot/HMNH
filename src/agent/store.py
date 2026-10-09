# src/agent/store.py
from pydantic import (
    BaseModel,
    Field,
)
from agent.db import Database
from typing import Any

import secrets
import sqlite3
import json

DAY_SECONDS = 86_400
TABLES = ("messages", "jobs", "threads", "memory")
INSERT_THREAD = (
    "INSERT INTO threads (id, kind, title, state, created, updated) VALUES (?, ?, ?, ?, ?, ?)"
)
INSERT_MESSAGE = (
    "INSERT INTO messages (thread_id, role, kind, text, data, created) VALUES (?, ?, ?, ?, ?, ?)"
)
LIST_JOBS = (
    "SELECT id, thread_id, owner, kind, title, state, stage, rating, error, created, updated "
    "FROM jobs WHERE owner = ? ORDER BY created DESC LIMIT ?"
)
WIPE = {
    "messages": "DELETE FROM messages",
    "jobs": "DELETE FROM jobs",
    "threads": "DELETE FROM threads",
    "memory": "DELETE FROM memory",
}
COUNT = {
    "threads": "SELECT COUNT(*) AS n FROM threads",
    "messages": "SELECT COUNT(*) AS n FROM messages",
    "jobs": "SELECT COUNT(*) AS n FROM jobs",
    "memory": "SELECT COUNT(*) AS n FROM memory",
}


class Thread(BaseModel):
    """A conversation thread with a kind, a title and free-form JSON state."""

    id: str
    kind: str
    title: str
    state: dict[str, Any] = Field(default_factory=dict)
    created: float
    updated: float


class Message(BaseModel):
    """A single message stored in a thread."""

    id: int
    thread_id: str
    role: str
    kind: str
    text: str
    data: dict[str, Any] = Field(default_factory=dict)
    created: float


class JobRecord(BaseModel):
    """A stored background job with its request, report and event log.

    Jobs listed for an owner are loaded in a light form that leaves out the
    request, report, events and clarification.
    """

    id: str
    thread_id: str | None = None
    owner: str
    kind: str
    title: str
    state: str
    stage: str
    rating: float | None = None
    request: dict[str, Any] | None = None
    report: dict[str, Any] | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)
    clarification: dict[str, Any] | None = None
    error: str | None = None
    created: float
    updated: float


def _loads(value: str | None, fallback: Any) -> Any:
    """Decode a JSON string, falling back to a default.

    Args:
        value: JSON text, or None.
        fallback: Value to return when the text is empty or not valid JSON.

    Returns:
        The decoded value, or the fallback.
    """
    if not value:
        return fallback
    try:
        return json.loads(value)
    except ValueError:
        return fallback


def _dumps(value: Any) -> str | None:
    """Encode a value as compact JSON.

    Args:
        value: Any JSON-serialisable value, or None.

    Returns:
        The JSON text without extra spaces, or None when the value is None.
    """
    return None if value is None else json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store(Database):
    """SQLite storage for threads, messages, jobs and memory entries.

    All access goes through the shared lock of Database, so one instance can be
    used from several threads. Text sizes and row counts are capped by the store
    tuning.
    """

    def _thread(self, row: sqlite3.Row) -> Thread:
        """Convert a database row to a Thread.

        Args:
            row: A row from the threads table.

        Returns:
            The thread, with its state decoded from JSON.
        """
        return Thread(
            id=row["id"],
            kind=row["kind"],
            title=row["title"],
            state=_loads(row["state"], {}),
            created=row["created"],
            updated=row["updated"],
        )

    def create_thread(self, kind: str, title: str, state: dict[str, Any] | None = None) -> Thread:
        """Create a thread and prune the oldest threads of the same kind.

        The title is cut to the configured message length. After the insert, threads
        of this kind beyond the configured maximum are deleted, oldest update first.

        Args:
            kind: Thread kind, such as research or assistant.
            title: Thread title.
            state: Optional initial JSON state.

        Returns:
            The stored thread.

        Raises:
            RuntimeError: when the new thread was pruned straight away.
        """
        now = self._clock()
        identifier = secrets.token_urlsafe(12)
        with self._lock, self._db:
            self._db.execute(
                INSERT_THREAD,
                (
                    identifier,
                    kind,
                    title[: self._tuning.message_chars],
                    _dumps(state or {}),
                    now,
                    now,
                ),
            )
            # Keep only the newest threads of this kind; older ones are removed, and their messages
            # go with them.
            surplus = self._db.execute(
                "SELECT id FROM threads WHERE kind = ? ORDER BY updated DESC LIMIT -1 OFFSET ?",
                (kind, self._tuning.max_threads),
            ).fetchall()
            for row in surplus:
                self._db.execute("DELETE FROM threads WHERE id = ?", (row["id"],))
        found = self.thread(identifier)
        if found is None:
            raise RuntimeError("the thread was removed straight after it was created")
        return found

    def thread(self, identifier: str) -> Thread | None:
        """Return one thread by id.

        Args:
            identifier: Thread id.

        Returns:
            The thread, or None when it does not exist.
        """
        with self._lock:
            row = self._db.execute("SELECT * FROM threads WHERE id = ?", (identifier,)).fetchone()
        return self._thread(row) if row else None

    def threads(self, kind: str, limit: int | None = None) -> list[Thread]:
        """List threads of one kind, most recently updated first.

        Args:
            kind: Thread kind to list.
            limit: Maximum number of threads, defaulting to the configured maximum.

        Returns:
            The matching threads.
        """
        size = limit if limit is not None else self._tuning.max_threads
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM threads WHERE kind = ? ORDER BY updated DESC LIMIT ?", (kind, size)
            ).fetchall()
        return [self._thread(row) for row in rows]

    def rename_thread(self, identifier: str, title: str) -> None:
        """Change the title of a thread.

        The thread update time is not changed.

        Args:
            identifier: Thread id.
            title: New title.
        """
        with self._lock, self._db:
            self._db.execute("UPDATE threads SET title = ? WHERE id = ?", (title, identifier))

    def set_state(self, identifier: str, state: dict[str, Any]) -> None:
        """Replace the JSON state of a thread and mark it as updated.

        Args:
            identifier: Thread id.
            state: New state, stored as a whole.
        """
        with self._lock, self._db:
            self._db.execute(
                "UPDATE threads SET state = ?, updated = ? WHERE id = ?",
                (_dumps(state), self._clock(), identifier),
            )

    def delete_thread(self, identifier: str) -> bool:
        """Delete a thread together with its messages.

        Jobs that pointed at the thread are kept but lose the link.

        Args:
            identifier: Thread id.

        Returns:
            True when a thread was deleted.
        """
        with self._lock, self._db:
            cursor = self._db.execute("DELETE FROM threads WHERE id = ?", (identifier,))
        return cursor.rowcount > 0

    def add_message(
        self, thread_id: str, role: str, kind: str, text: str, data: dict[str, Any] | None = None
    ) -> Message:
        """Append a message to a thread.

        The text is cut to the configured message length, the thread is marked as
        updated, and the oldest messages beyond the configured maximum per thread are
        deleted.

        Args:
            thread_id: Id of the thread to append to.
            role: Author role, such as user or assistant.
            kind: Message kind.
            text: Message text.
            data: Optional JSON payload stored with the message.

        Returns:
            The stored message.

        Raises:
            RuntimeError: when the new message was removed straight away.
        """
        now = self._clock()
        with self._lock, self._db:
            cursor = self._db.execute(
                INSERT_MESSAGE,
                (
                    thread_id,
                    role,
                    kind,
                    text[: self._tuning.message_chars],
                    _dumps(data or {}),
                    now,
                ),
            )
            identifier = cursor.lastrowid
            self._db.execute("UPDATE threads SET updated = ? WHERE id = ?", (now, thread_id))
            self._db.execute(
                # Trim the thread to its newest messages by deleting everything up to the id found
                # at the cap offset.
                "DELETE FROM messages WHERE thread_id = ? AND id <= "
                "(SELECT id FROM messages WHERE thread_id = ? ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (thread_id, thread_id, self._tuning.max_messages),
            )
        found = self.message(identifier) if identifier is not None else None
        if found is None:
            raise RuntimeError("the message was removed straight after it was added")
        return found

    def _message(self, row: sqlite3.Row) -> Message:
        """Convert a database row to a Message.

        Args:
            row: A row from the messages table.

        Returns:
            The message, with its data decoded from JSON.
        """
        return Message(
            id=row["id"],
            thread_id=row["thread_id"],
            role=row["role"],
            kind=row["kind"],
            text=row["text"],
            data=_loads(row["data"], {}),
            created=row["created"],
        )

    def message(self, identifier: int) -> Message | None:
        """Return one message by id.

        Args:
            identifier: Message id.

        Returns:
            The message, or None when it does not exist.
        """
        with self._lock:
            row = self._db.execute("SELECT * FROM messages WHERE id = ?", (identifier,)).fetchone()
        return self._message(row) if row else None

    def messages(self, thread_id: str, after: int = 0, limit: int | None = None) -> list[Message]:
        """List the messages of a thread in order, oldest first.

        Args:
            thread_id: Thread id.
            after: Only messages with an id greater than this are returned.
            limit: Maximum number of messages, defaulting to the configured maximum.

        Returns:
            The matching messages.
        """
        size = limit if limit is not None else self._tuning.max_messages
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM messages WHERE thread_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
                (thread_id, max(after, 0), size),
            ).fetchall()
        return [self._message(row) for row in rows]

    def recent_messages(self, thread_id: str, count: int) -> list[Message]:
        """Return the latest messages of a thread in chronological order.

        Args:
            thread_id: Thread id.
            count: How many of the newest messages to return.

        Returns:
            Up to count messages, oldest first.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM messages WHERE thread_id = ? ORDER BY id DESC LIMIT ?",
                (thread_id, count),
            ).fetchall()
        return [self._message(row) for row in reversed(rows)]

    def save_job(self, record: JobRecord) -> None:
        """Insert a job or update it when its id already exists.

        Only the mutable fields are updated on an existing job. Owner, kind and the
        creation time stay as first stored.

        Args:
            record: The job to save.
        """
        with self._lock, self._db:
            known = self._db.execute(
                "SELECT 1 FROM threads WHERE id = ?", (record.thread_id,)
            ).fetchone()
            # A job whose thread was already deleted is saved unlinked instead of failing the
            # foreign key.
            if record.thread_id is not None and known is None:
                record = record.model_copy(update={"thread_id": None})
            self._db.execute(
                "INSERT INTO jobs (id, thread_id, owner, kind, title, state, stage, rating, "
                "request, report, events, clarification, error, created, updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET thread_id = excluded.thread_id, "
                "title = excluded.title, state = excluded.state, stage = excluded.stage, "
                "rating = excluded.rating, request = excluded.request, report = excluded.report, "
                "events = excluded.events, clarification = excluded.clarification, "
                "error = excluded.error, updated = excluded.updated",
                (
                    record.id,
                    record.thread_id,
                    record.owner,
                    record.kind,
                    record.title,
                    record.state,
                    record.stage,
                    record.rating,
                    _dumps(record.request),
                    _dumps(record.report),
                    _dumps(record.events),
                    _dumps(record.clarification),
                    record.error,
                    record.created,
                    record.updated,
                ),
            )

    def _job(self, row: sqlite3.Row, light: bool) -> JobRecord:
        """Convert a database row to a JobRecord.

        Args:
            row: A row from the jobs table.
            light: When True, the request, report, events and clarification are left
                empty instead of being decoded.

        Returns:
            The job record.
        """
        keys = row.keys()
        return JobRecord(
            id=row["id"],
            thread_id=row["thread_id"],
            owner=row["owner"],
            kind=row["kind"],
            title=row["title"],
            state=row["state"],
            stage=row["stage"],
            rating=row["rating"],
            request=None if light else _loads(row["request"], None),
            report=None if light else _loads(row["report"], None),
            events=[] if light else _loads(row["events"], []),
            clarification=None if light else _loads(row["clarification"], None),
            error=row["error"] if "error" in keys else None,
            created=row["created"],
            updated=row["updated"],
        )

    def job(self, identifier: str) -> JobRecord | None:
        """Return one job by id, including its request and report.

        Args:
            identifier: Job id.

        Returns:
            The full job record, or None when it does not exist.
        """
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE id = ?", (identifier,)).fetchone()
        return self._job(row, light=False) if row else None

    def jobs(self, owner: str, limit: int | None = None) -> list[JobRecord]:
        """List the jobs of an owner, newest first, in light form.

        Args:
            owner: Owner label the jobs were created under.
            limit: Maximum number of jobs, defaulting to the configured thread maximum.

        Returns:
            Job records without request, report, events or clarification.
        """
        size = limit if limit is not None else self._tuning.max_threads
        with self._lock:
            rows = self._db.execute(LIST_JOBS, (owner, size)).fetchall()
        return [self._job(row, light=True) for row in rows]

    def delete_job(self, identifier: str) -> bool:
        """Delete a job.

        Args:
            identifier: Job id.

        Returns:
            True when a job was deleted.
        """
        with self._lock, self._db:
            cursor = self._db.execute("DELETE FROM jobs WHERE id = ?", (identifier,))
        return cursor.rowcount > 0

    def memory(self, key: str) -> dict[str, Any] | None:
        """Read a memory entry.

        Args:
            key: Memory key.

        Returns:
            The stored dict, or None when the key is missing or the value is not an
            object.
        """
        with self._lock:
            row = self._db.execute("SELECT value FROM memory WHERE key = ?", (key,)).fetchone()
        value = _loads(row["value"], None) if row else None
        return value if isinstance(value, dict) else None

    def remember(self, key: str, value: dict[str, Any]) -> None:
        """Store or replace a memory entry.

        Args:
            key: Memory key.
            value: JSON-serialisable dict to store.

        Raises:
            ValueError: when the encoded value exceeds the configured size.
        """
        payload = _dumps(value) or "{}"
        if len(payload) > self._tuning.memory_chars:
            raise ValueError("the memory entry is too large")
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO memory (key, value, updated) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated = excluded.updated",
                (key, payload, self._clock()),
            )

    def forget(self, key: str) -> bool:
        """Delete a memory entry.

        Args:
            key: Memory key.

        Returns:
            True when an entry was deleted.
        """
        with self._lock, self._db:
            cursor = self._db.execute("DELETE FROM memory WHERE key = ?", (key,))
        return cursor.rowcount > 0

    def purge(self) -> dict[str, int]:
        """Delete threads and jobs not updated within the retention period.

        Messages of deleted threads are removed with them.

        Returns:
            The number of deleted threads and jobs, keyed by table name.
        """
        cutoff = self._clock() - self._tuning.retention_days * DAY_SECONDS
        with self._lock, self._db:
            threads = self._db.execute("DELETE FROM threads WHERE updated < ?", (cutoff,)).rowcount
            jobs = self._db.execute("DELETE FROM jobs WHERE updated < ?", (cutoff,)).rowcount
        return {"threads": threads, "jobs": jobs}

    def wipe(self) -> dict[str, int]:
        """Delete all threads, messages, jobs and memory entries.

        Returns:
            The row count of each table as it was before the deletion.
        """
        with self._lock, self._db:
            counts = self.counts()
            for table in TABLES:
                self._db.execute(WIPE[table])
        return counts

    def counts(self) -> dict[str, int]:
        """Count the rows of each stored table.

        Returns:
            A dict mapping table name to its row count.
        """
        with self._lock:
            return {table: self._db.execute(sql).fetchone()["n"] for table, sql in COUNT.items()}
