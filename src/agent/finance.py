# src/agent/finance.py
from datetime import (
    date,
    timedelta,
)
from agent.errors import InvalidRequest
from agent.config import FinanceTuning
from collections.abc import Callable
from pydantic import BaseModel
from agent.store import Store

import secrets
import sqlite3
import time
import re

MANUAL = "manual"
IMPORT = "import"
KEY = "finance"
MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
NUMBER = re.compile(r"^([+-]?)([0-9]{1,18})(?:\.([0-9]{1,40}))?$")


# A finance category of one kind with the count of entries filed under it.
class Category(BaseModel):
    id: str
    name: str
    kind: str
    created: float
    entries: int = 0


class Entry(BaseModel):
    """A single revenue or expense entry with its amount in integer minor units."""

    id: str
    day: str
    month: str
    kind: str
    amount: int
    category_id: str | None = None
    category: str = ""
    note: str = ""
    source: str = MANUAL
    created: float
    created_by: str = ""


class MonthTotal(BaseModel):
    """Revenue and expense totals, in minor units, for one calendar month."""

    month: str
    revenue: int = 0
    expense: int = 0

    @property
    def net(self) -> int:
        """Return revenue minus expense in minor units."""
        return self.revenue - self.expense


class CategoryTotal(BaseModel):
    """The summed amount for one category and kind."""

    name: str
    kind: str
    total: int


class Page(BaseModel):
    """One page of entries together with the total number of matching entries."""

    items: list[Entry]
    total: int
    offset: int
    size: int


def shift_month(month: str, delta: int) -> str:
    """Move a month forward or backward by a number of months.

    Args:
        month: Month formatted as YYYY-MM.
        delta: Number of months to move; negative values go back in time.

    Returns:
        The shifted month formatted as YYYY-MM.
    """
    year, number = int(month[:4]), int(month[5:7])
    index = year * 12 + (number - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def parse_amount(raw: str, tuning: FinanceTuning) -> int:
    # A comma is read as the decimal point, so thousands separators are not supported. Non-string
    # input becomes an empty string and fails the number check.
    """Parse user supplied money text into a positive integer of minor units.

    Spaces are dropped and a comma is read as the decimal point. Zero, negative and
    oversized amounts are rejected.

    Args:
        raw: Amount text such as 12.50 or 1 200,5.
        tuning: Finance settings giving the decimal places and the maximum amount.

    Returns:
        The amount in minor units, for example cents.

    Raises:
        InvalidRequest: when the text is not a number, has too many decimals, or is
            not above zero and within the maximum.
    """
    # A comma is read as the decimal point, so thousands separators are not supported. Non-string
    # input becomes an empty string and fails the number check.
    cleaned = raw.strip().replace(" ", "").replace(",", ".") if isinstance(raw, str) else ""
    match = NUMBER.fullmatch(cleaned)
    if match is None:
        raise InvalidRequest("The amount must be a number.")
    sign, whole, fraction = match.group(1), match.group(2), (match.group(3) or "").rstrip("0")
    if len(fraction) > tuning.decimals:
        raise InvalidRequest(f"Use at most {tuning.decimals} decimals in an amount.")
    minor = int(whole) * 10**tuning.decimals + int(fraction.ljust(tuning.decimals, "0") or 0)
    if sign == "-" or minor <= 0 or minor > tuning.max_amount:
        raise InvalidRequest("The amount must be above zero and not absurdly large.")
    return minor


def format_amount(minor: int, tuning: FinanceTuning) -> str:
    """Format an amount in minor units as a grouped decimal string.

    Args:
        minor: Amount in minor units; may be negative.
        tuning: Finance settings giving the number of decimal places.

    Returns:
        The amount with thousands separators and a leading minus sign when negative.
    """
    scale = 10**tuning.decimals
    whole, fraction = divmod(abs(minor), scale)
    text = f"{whole:,}"
    if tuning.decimals:
        text += f".{fraction:0{tuning.decimals}d}"
    return f"-{text}" if minor < 0 else text


class FinanceBook:
    """A store backed ledger of revenue and expense entries and their categories.

    Amounts are kept as integers in minor units. Every write validates its input and
    raises InvalidRequest instead of storing bad data.
    """

    def __init__(
        self,
        store: Store,
        tuning: FinanceTuning,
        clock: Callable[[], float] = time.time,
    ):
        """Initialize the book on top of a store.

        Args:
            store: Store that holds the finance tables and the saved currency.
            tuning: Finance limits and formatting settings.
            clock: Function returning the current time as a Unix timestamp in seconds.
        """
        self._db = store
        self._store = store
        self._tuning = tuning
        self._clock = clock

    def currency(self) -> str:
        """Return the active currency code.

        The code saved in the store is used when it matches the configured pattern;
        otherwise the default from the settings is returned.

        Returns:
            The three letter currency code.
        """
        saved = self._store.memory(KEY) or {}
        value = saved.get("currency") if isinstance(saved, dict) else None
        if isinstance(value, str) and re.fullmatch(self._tuning.currency_pattern, value):
            return value
        return self._tuning.currency

    def set_currency(self, code: str) -> str:
        """Validate and save the currency code used for display.

        Args:
            code: Currency code such as eur or USD; case and outer spaces are ignored.

        Returns:
            The cleaned upper case code that was saved.

        Raises:
            InvalidRequest: when the code does not match the allowed pattern.
        """
        cleaned = code.strip().upper()
        if not re.fullmatch(self._tuning.currency_pattern, cleaned):
            raise InvalidRequest("A currency is a three letter code such as EUR or USD.")
        self._store.remember(KEY, {"currency": cleaned})
        return cleaned

    def money(self, minor: int) -> str:
        """Format an amount with grouping and the active currency code.

        Args:
            minor: Amount in minor units.

        Returns:
            Text such as 1,234.50 EUR.
        """
        return f"{format_amount(minor, self._tuning)} {self.currency()}"

    def plain(self, minor: int) -> str:
        """Format an amount as a plain decimal without grouping or currency.

        Args:
            minor: Amount in minor units.

        Returns:
            Text such as 1234.50, suitable for exports and form fields.
        """
        scale = 10**self._tuning.decimals
        whole, fraction = divmod(abs(minor), scale)
        text = str(whole)
        if self._tuning.decimals:
            text += f".{fraction:0{self._tuning.decimals}d}"
        return f"-{text}" if minor < 0 else text

    def compact(self, minor: int) -> str:
        """Format an amount as a short label with a k, M or B suffix.

        Args:
            minor: Amount in minor units.

        Returns:
            Text such as 1.2k or 3M; values below one thousand have no suffix.
        """
        units = minor / 10**self._tuning.decimals
        for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
            if abs(units) >= limit:
                return f"{units / limit:.1f}".rstrip("0").rstrip(".") + suffix
        return f"{units:.0f}"

    def today(self) -> date:
        """Return the current local date according to the clock.

        Returns:
            Today's date.
        """
        return date.fromtimestamp(self._clock())

    def _day(self, raw: str) -> str:
        """Validate an ISO date and check it against the allowed window.

        Args:
            raw: Date text formatted as YYYY-MM-DD.

        Returns:
            The normalized ISO date.

        Raises:
            InvalidRequest: when the text is not a date, is before the first allowed year,
                or lies too many days in the future.
        """
        try:
            parsed = date.fromisoformat(raw.strip())
        except ValueError as error:
            raise InvalidRequest("Dates look like 2026-03-01.") from error
        if parsed.year < self._tuning.first_year:
            raise InvalidRequest("That date is too far in the past.")
        if parsed > self.today() + timedelta(days=self._tuning.future_days):
            raise InvalidRequest("That date is too far ahead.")
        return parsed.isoformat()

    def _kind(self, kind: str) -> str:
        """Check that an entry kind is one of the configured kinds.

        Args:
            kind: Kind to check, such as revenue or expense.

        Returns:
            The same kind.

        Raises:
            InvalidRequest: when the kind is not configured.
        """
        if kind not in self._tuning.kinds:
            raise InvalidRequest("Choose revenue or expense.")
        return kind

    def _category(self, row: sqlite3.Row) -> Category:
        """Convert a database row into a Category.

        Args:
            row: Row from the finance categories table.

        Returns:
            The validated category.
        """
        return Category.model_validate(dict(row))

    def categories(self) -> list[Category]:
        """Return all categories with their entry counts.

        Returns:
            Categories ordered by kind and then name.
        """
        rows = self._db.query(
            "SELECT finance_categories.*, (SELECT COUNT(*) FROM finance_entries "
            "WHERE category_id = finance_categories.id) AS entries "
            "FROM finance_categories ORDER BY kind, name"
        )
        return [self._category(row) for row in rows]

    def category(self, identifier: str) -> Category | None:
        """Look up one category by id.

        Args:
            identifier: Category id.

        Returns:
            The category with its entry count left at zero, or None when it is unknown.
        """
        row = self._db.one(
            "SELECT finance_categories.*, 0 AS entries FROM finance_categories WHERE id = ?",
            (identifier,),
        )
        return self._category(row) if row else None

    def add_category(self, name: str, kind: str) -> Category:
        """Create a category after tidying its name.

        Args:
            name: Display name; whitespace is collapsed and the name is truncated.
            kind: Kind the category belongs to.

        Returns:
            The new category.

        Raises:
            InvalidRequest: when the name is empty, the kind is unknown, the category limit
                is reached, or the name already exists for that kind.
        """
        label = " ".join(name.split())[: self._tuning.name_chars]
        if not label:
            raise InvalidRequest("Give the category a name.")
        self._kind(kind)
        identifier = secrets.token_urlsafe(8)
        try:
            with self._db.transaction() as db:
                count = db.execute("SELECT COUNT(*) AS n FROM finance_categories").fetchone()["n"]
                if count >= self._tuning.max_categories:
                    raise InvalidRequest("There are too many categories.")
                db.execute(
                    "INSERT INTO finance_categories (id, name, kind, created) VALUES (?, ?, ?, ?)",
                    (identifier, label, kind, self._clock()),
                )
        except sqlite3.IntegrityError as error:
            raise InvalidRequest("That category already exists.") from error
        return Category(id=identifier, name=label, kind=kind, created=self._clock())

    def rename_category(self, identifier: str, name: str) -> None:
        """Rename a category after tidying the new name.

        Args:
            identifier: Category id.
            name: New display name; whitespace is collapsed and the name is truncated.

        Raises:
            InvalidRequest: when the name is empty, already exists, or the category is
                unknown.
        """
        label = " ".join(name.split())[: self._tuning.name_chars]
        if not label:
            raise InvalidRequest("Give the category a name.")
        try:
            changed = self._db.run(
                "UPDATE finance_categories SET name = ? WHERE id = ?", (label, identifier)
            )
        except sqlite3.IntegrityError as error:
            raise InvalidRequest("That category already exists.") from error
        if not changed:
            raise InvalidRequest("That category does not exist.")

    def delete_category(self, identifier: str) -> bool:
        """Delete a category by id.

        Args:
            identifier: Category id.

        Returns:
            True when a category was removed, False when none matched.
        """
        return bool(self._db.run("DELETE FROM finance_categories WHERE id = ?", (identifier,)))

    def category_for(
        self, db: sqlite3.Connection, kind: str, name: str, create: bool
    ) -> str | None:
        """Find a category by name and kind, optionally creating it.

        Runs on the caller's connection so it joins the caller's transaction.

        Args:
            db: Open connection inside a transaction.
            kind: Category kind.
            name: Category name; whitespace is collapsed and the name is truncated.
            create: Create the category when it is missing.

        Returns:
            The category id, or None when the cleaned name is empty.

        Raises:
            InvalidRequest: when the category is missing and create is False, or when
                creating it would exceed the category limit.
        """
        label = " ".join(name.split())[: self._tuning.name_chars]
        if not label:
            return None
        row = db.execute(
            "SELECT id FROM finance_categories WHERE name = ? AND kind = ?", (label, kind)
        ).fetchone()
        if row:
            return row["id"]
        if not create:
            raise InvalidRequest("That category does not exist.")
        count = db.execute("SELECT COUNT(*) AS n FROM finance_categories").fetchone()["n"]
        if count >= self._tuning.max_categories:
            raise InvalidRequest("There are too many categories.")
        identifier = secrets.token_urlsafe(8)
        db.execute(
            "INSERT INTO finance_categories (id, name, kind, created) VALUES (?, ?, ?, ?)",
            (identifier, label, kind, self._clock()),
        )
        return identifier

    def has_entry(self, day: str, kind: str, amount: str, category: str, note: str) -> bool:
        """Check whether an identical entry is already recorded.

        Args:
            day: ISO date of the entry.
            kind: Entry kind.
            amount: Amount text, parsed like a new entry.
            category: Category name, empty for none.
            note: Entry note; whitespace is collapsed and the note is truncated.

        Returns:
            True when an entry matches on day, kind, amount, category name and note.

        Raises:
            InvalidRequest: when the amount is not valid.
        """
        minor = parse_amount(amount, self._tuning)
        label = " ".join(category.split())[: self._tuning.name_chars]
        row = self._db.one(
            "SELECT 1 FROM finance_entries LEFT JOIN finance_categories ON "
            "finance_categories.id = finance_entries.category_id WHERE finance_entries.day = ? "
            "AND finance_entries.kind = ? AND finance_entries.amount = ? "
            "AND COALESCE(finance_categories.name, '') = ? AND finance_entries.note = ?",
            (day, kind, minor, label, " ".join(note.split())[: self._tuning.note_chars]),
        )
        return row is not None

    def ensure_category(self, name: str, kind: str) -> str | None:
        """Return the id of a category, creating it when it does not exist.

        Args:
            name: Category name.
            kind: Category kind.

        Returns:
            The category id, or None when the cleaned name is empty.

        Raises:
            InvalidRequest: when the kind is unknown or the category limit is reached.
        """
        self._kind(kind)
        with self._db.transaction() as db:
            return self.category_for(db, kind, name, create=True)

    def _insert(
        self,
        db: sqlite3.Connection,
        day: str,
        kind: str,
        amount: int,
        category_id: str | None,
        note: str,
        source: str,
        actor: str,
    ) -> str:
        """Insert one entry row on the caller's connection.

        The note is whitespace collapsed and truncated, and the month is derived from
        the day.

        Args:
            db: Open connection inside a transaction.
            day: Validated ISO date.
            kind: Validated entry kind.
            amount: Amount in minor units.
            category_id: Category id, or None.
            note: Free text note.
            source: Where the entry came from, such as manual or import.
            actor: Who created the entry.

        Returns:
            The id of the new entry.

        Raises:
            InvalidRequest: when the entry limit is reached.
        """
        count = db.execute("SELECT COUNT(*) AS n FROM finance_entries").fetchone()["n"]
        if count >= self._tuning.max_entries:
            raise InvalidRequest("There are too many entries.")
        identifier = secrets.token_urlsafe(9)
        db.execute(
            "INSERT INTO finance_entries (id, day, month, kind, amount, category_id, note, "
            "source, created, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                identifier,
                day,
                day[:7],
                kind,
                amount,
                category_id,
                " ".join(note.split())[: self._tuning.note_chars],
                source,
                self._clock(),
                actor,
            ),
        )
        return identifier

    def add_entry(
        self,
        day: str,
        kind: str,
        amount: str,
        *,
        category_id: str | None = None,
        note: str = "",
        actor: str = "",
        source: str = MANUAL,
    ) -> Entry:
        """Validate and save one entry.

        Args:
            day: ISO date of the entry.
            kind: Entry kind, such as revenue or expense.
            amount: Amount text, parsed into minor units.
            category_id: Optional category id, which must be of the same kind.
            note: Free text note.
            actor: Who is creating the entry.
            source: Where the entry came from.

        Returns:
            The saved entry including its category name.

        Raises:
            InvalidRequest: when the date, kind, amount or category is invalid, the entry
                limit is reached, or the entry cannot be read back.
        """
        when = self._day(day)
        self._kind(kind)
        minor = parse_amount(amount, self._tuning)
        with self._db.transaction() as db:
            if category_id:
                row = db.execute(
                    "SELECT kind FROM finance_categories WHERE id = ?", (category_id,)
                ).fetchone()
                if row is None or row["kind"] != kind:
                    raise InvalidRequest("Choose a category of the same kind.")
            identifier = self._insert(
                db, when, kind, minor, category_id or None, note, source, actor
            )
        found = self.entry(identifier)
        if found is None:
            raise InvalidRequest("The entry could not be saved.")
        return found

    def add_many(
        self,
        rows: list[tuple[str, str, str, str, str]],
        actor: str = "",
    ) -> int:
        """Import several entries in a single transaction.

        Categories are created as needed and every entry is marked as imported. If any
        row is invalid nothing is saved.

        Args:
            rows: Tuples of day, kind, amount text, category name and note.
            actor: Who is importing the rows.

        Returns:
            The number of entries added.

        Raises:
            InvalidRequest: when a row has an invalid date, kind or amount, or a limit is
                exceeded.
        """
        added = 0
        with self._db.transaction() as db:
            for day, kind, amount, category, note in rows:
                when = self._day(day)
                self._kind(kind)
                minor = parse_amount(amount, self._tuning)
                category_id = self.category_for(db, kind, category, create=True)
                self._insert(db, when, kind, minor, category_id, note, IMPORT, actor)
                added += 1
        return added

    def entry(self, identifier: str) -> Entry | None:
        """Fetch one entry together with its category name.

        Args:
            identifier: Entry id.

        Returns:
            The entry, or None when it does not exist.
        """
        row = self._db.one(
            "SELECT finance_entries.*, COALESCE(finance_categories.name, '') AS category "
            "FROM finance_entries LEFT JOIN finance_categories "
            "ON finance_categories.id = finance_entries.category_id WHERE finance_entries.id = ?",
            (identifier,),
        )
        return Entry.model_validate(dict(row)) if row else None

    def update_entry(
        self,
        identifier: str,
        *,
        day: str,
        kind: str,
        amount: str,
        category_id: str | None,
        note: str,
    ) -> Entry:
        """Validate and replace the editable fields of an entry.

        Args:
            identifier: Entry id.
            day: New ISO date.
            kind: New entry kind.
            amount: New amount text, parsed into minor units.
            category_id: New category id, which must be of the same kind, or None.
            note: New free text note.

        Returns:
            The updated entry.

        Raises:
            InvalidRequest: when a value is invalid or the entry does not exist.
        """
        when = self._day(day)
        self._kind(kind)
        minor = parse_amount(amount, self._tuning)
        with self._db.transaction() as db:
            if category_id:
                row = db.execute(
                    "SELECT kind FROM finance_categories WHERE id = ?", (category_id,)
                ).fetchone()
                if row is None or row["kind"] != kind:
                    raise InvalidRequest("Choose a category of the same kind.")
            changed = db.execute(
                "UPDATE finance_entries SET day = ?, month = ?, kind = ?, amount = ?, "
                "category_id = ?, note = ? WHERE id = ?",
                (
                    when,
                    when[:7],
                    kind,
                    minor,
                    category_id or None,
                    " ".join(note.split())[: self._tuning.note_chars],
                    identifier,
                ),
            ).rowcount
            if not changed:
                raise InvalidRequest("That entry does not exist.")
        found = self.entry(identifier)
        if found is None:
            raise InvalidRequest("That entry does not exist.")
        return found

    def delete_entry(self, identifier: str) -> bool:
        """Delete an entry by id.

        Args:
            identifier: Entry id.

        Returns:
            True when an entry was removed, False when none matched.
        """
        return bool(self._db.run("DELETE FROM finance_entries WHERE id = ?", (identifier,)))

    def page(
        self,
        *,
        kind: str = "",
        category_id: str = "",
        month: str = "",
        offset: int = 0,
        size: int | None = None,
    ) -> Page:
        """Return one page of entries, newest day first.

        Args:
            kind: Only this kind; empty for all kinds.
            category_id: Only this category; empty for all categories.
            month: Only this month as YYYY-MM; a malformed value is ignored.
            offset: Number of entries to skip; negative values count as zero.
            size: Entries per page; defaults to the configured page size.

        Returns:
            The page with the matching entries and the total number of matches.
        """
        size = size or self._tuning.page_size
        month = month if MONTH.fullmatch(month) else ""
        where = (
            " WHERE (? = '' OR finance_entries.kind = ?) "
            "AND (? = '' OR finance_entries.category_id = ?) "
            "AND (? = '' OR finance_entries.month = ?)"
        )
        params = (kind, kind, category_id, category_id, month, month)
        total = self._db.one("SELECT COUNT(*) AS n FROM finance_entries" + where, params)
        rows = self._db.query(
            "SELECT finance_entries.*, COALESCE(finance_categories.name, '') AS category "
            "FROM finance_entries LEFT JOIN finance_categories "
            "ON finance_categories.id = finance_entries.category_id"
            + where
            + " ORDER BY finance_entries.day DESC, finance_entries.created DESC LIMIT ? OFFSET ?",
            (*params, size, max(0, offset)),
        )
        return Page(
            items=[Entry.model_validate(dict(row)) for row in rows],
            total=total["n"] if total else 0,
            offset=max(0, offset),
            size=size,
        )

    def all_entries(self) -> list[Entry]:
        """Return every entry with its category name.

        Returns:
            All entries ordered by day and then creation time, oldest first.
        """
        rows = self._db.query(
            "SELECT finance_entries.*, COALESCE(finance_categories.name, '') AS category "
            "FROM finance_entries LEFT JOIN finance_categories "
            "ON finance_categories.id = finance_entries.category_id "
            "ORDER BY finance_entries.day, finance_entries.created"
        )
        return [Entry.model_validate(dict(row)) for row in rows]

    def count(self) -> int:
        """Count all entries in the book.

        Returns:
            The number of entries.
        """
        row = self._db.one("SELECT COUNT(*) AS n FROM finance_entries")
        return row["n"] if row else 0

    def monthly(self, months: int | None = None, end: str | None = None) -> list[MonthTotal]:
        """Return revenue and expense totals for a run of consecutive months.

        Args:
            months: Number of months to cover; defaults to the configured count.
            end: Last month as YYYY-MM; defaults to the current month.

        Returns:
            One total per month, oldest first, with zeros for months without entries.
        """
        count = months or self._tuning.months_shown
        last = end or self.today().isoformat()[:7]
        first = shift_month(last, -(count - 1))
        rows = self._db.query(
            "SELECT month, kind, SUM(amount) AS total FROM finance_entries "
            "WHERE month >= ? AND month <= ? GROUP BY month, kind",
            (first, last),
        )
        table = {
            shift_month(first, step): MonthTotal(month=shift_month(first, step))
            for step in range(count)
        }
        for row in rows:
            entry = table.get(row["month"])
            if entry is not None:
                # The kind value doubles as the MonthTotal field name, which works for revenue and
                # expense.
                setattr(entry, row["kind"], row["total"])
        return list(table.values())

    def by_category(
        self, kind: str, since: str = "", until: str = "", limit: int | None = None
    ) -> list[CategoryTotal]:
        """Return the summed amount per category for one kind, largest first.

        Entries without a category are grouped as Uncategorised. When there are more
        groups than the limit, the smallest ones are merged into Everything else.

        Args:
            kind: Entry kind to total.
            since: First month to include as YYYY-MM; empty for no lower bound.
            until: Last month to include as YYYY-MM; empty for no upper bound.
            limit: Maximum number of rows; defaults to the configured value.

        Returns:
            The category totals in minor units.

        Raises:
            InvalidRequest: when the kind is unknown.
        """
        self._kind(kind)
        rows = self._db.query(
            "SELECT COALESCE(finance_categories.name, 'Uncategorised') AS name, "
            "SUM(finance_entries.amount) AS total FROM finance_entries "
            "LEFT JOIN finance_categories ON finance_categories.id = finance_entries.category_id "
            "WHERE finance_entries.kind = ? AND (? = '' OR finance_entries.month >= ?) "
            "AND (? = '' OR finance_entries.month <= ?) GROUP BY name ORDER BY total DESC",
            (kind, since, since, until, until),
        )
        shown = limit or self._tuning.top_categories
        items = [CategoryTotal(name=row["name"], kind=kind, total=row["total"]) for row in rows]
        if len(items) > shown:
            # One slot is kept for the merged bucket, so only shown - 1 categories stay named.
            rest = sum(item.total for item in items[shown - 1 :])
            items = [
                *items[: shown - 1],
                CategoryTotal(name="Everything else", kind=kind, total=rest),
            ]
        return items

    def totals(self, since: str = "", until: str = "") -> dict[str, int]:
        """Return the summed amount per entry kind.

        Args:
            since: First month to include as YYYY-MM; empty for no lower bound.
            until: Last month to include as YYYY-MM; empty for no upper bound.

        Returns:
            A mapping from every configured kind to its total in minor units, with zero
            for kinds that have no entries.
        """
        rows = self._db.query(
            "SELECT kind, SUM(amount) AS total FROM finance_entries "
            "WHERE (? = '' OR month >= ?) AND (? = '' OR month <= ?) GROUP BY kind",
            (since, since, until, until),
        )
        found = {row["kind"]: row["total"] for row in rows}
        return {kind: found.get(kind, 0) for kind in self._tuning.kinds}

    def months_with_data(self) -> list[str]:
        """Return the months that contain at least one entry.

        Returns:
            Months formatted as YYYY-MM, newest first.
        """
        rows = self._db.query("SELECT DISTINCT month FROM finance_entries ORDER BY month DESC")
        return [row["month"] for row in rows]
