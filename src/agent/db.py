# src/agent/db.py
from collections.abc import (
    Callable,
    Iterator,
)
from contextlib import contextmanager
from agent.config import StoreTuning
from agent.schema import MIGRATIONS
from typing import Any

import threading
import sqlite3
import time


class Database:
    """A thread-safe SQLite database with lazy connection and nested transactions.

    One shared connection is guarded by a re-entrant lock. The schema is brought up
    to date with the migrations on first connect.
    """

    def __init__(self, tuning: StoreTuning, clock: Callable[[], float] = time.time):
        """Initialize the database wrapper without opening a connection.

        Args:
            tuning: Store settings with the file path and busy timeout.
            clock: Function returning the current time in seconds since the epoch.
        """
        self._tuning = tuning
        self._clock = clock
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._depth = 0

    @property
    def _db(self) -> sqlite3.Connection:
        """Return the shared connection, opening it on first use."""
        with self._lock:
            if self._connection is None:
                self._connection = self._connect()
            return self._connection

    def _connect(self) -> sqlite3.Connection:
        """Open the SQLite file and apply pending migrations.

        The parent folder is created if needed. The connection uses write-ahead
        logging, enforces foreign keys and returns rows addressable by column name.

        Returns:
            The open connection.
        """
        self._tuning.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self._tuning.path, check_same_thread=False)
        db.row_factory = sqlite3.Row
        # PRAGMA values cannot be bound as parameters, so the value is forced to an int before it
        # enters the SQL.
        db.execute(f"PRAGMA busy_timeout = {int(self._tuning.busy_timeout_ms)}")
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA foreign_keys = ON")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        # The stored user_version counts applied migrations, so only the newer ones run.
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            db.executescript(script)
            db.execute(f"PRAGMA user_version = {number}")
        return db

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        """Run a read statement and return all rows.

        Args:
            sql: SQL text with ? placeholders.
            params: Values bound to the placeholders.

        Returns:
            All result rows.
        """
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        """Run a read statement and return the first row.

        Args:
            sql: SQL text with ? placeholders.
            params: Values bound to the placeholders.

        Returns:
            The first result row, or None when there is none.
        """
        with self._lock:
            return self._db.execute(sql, params).fetchone()

    def run(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        """Run a write statement in its own transaction.

        Args:
            sql: SQL text with ? placeholders.
            params: Values bound to the placeholders.

        Returns:
            The number of rows changed.
        """
        with self.transaction() as db:
            return db.execute(sql, params).rowcount

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Open a transaction that can be nested.

        The outermost call takes the write lock immediately and commits on success or
        rolls back on any exception. Nested calls use savepoints, so an inner failure
        undoes only the inner work before the exception continues to propagate.

        Returns:
            A context manager that provides the shared connection.
        """
        with self._lock:
            db = self._db
            if self._depth:
                self._depth += 1
                name = f"nested_{self._depth}"
                db.execute(f"SAVEPOINT {name}")
                try:
                    yield db
                except BaseException:
                    db.execute(f"ROLLBACK TO {name}")
                    db.execute(f"RELEASE {name}")
                    raise
                else:
                    db.execute(f"RELEASE {name}")
                finally:
                    self._depth -= 1
                return
            db.commit()
            # Taking the write lock at the start avoids a failed lock upgrade halfway through the
            # transaction.
            db.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                self._depth = 0

    def close(self) -> None:
        """Close the connection if it is open.

        A later query opens a new connection.
        """
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
