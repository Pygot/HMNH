# src/agent/portability.py
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
)
from agent.chat import (
    load_profile,
    Profile,
    save_profile,
)
from agent.models import (
    Identity,
    Requirement,
    ResearchOptions,
)
from agent.candidates import (
    Candidates,
    IMPORT,
)
from agent.drafts import (
    EmailTemplate,
    TemplateStore,
)
from agent.errors import (
    AgentError,
    InvalidRequest,
)
from collections.abc import (
    Callable,
    Iterator,
)
from typing import (
    Any,
    ClassVar,
)
from agent.config import PortabilityTuning
from agent.finance import FinanceBook
from agent.accounts import Accounts
from agent.routines import Routines
from agent.forms import Dataset
from agent.mailer import Mailer
from agent.store import Store

import secrets
import json
import math
import time
import csv
import io

FORMAT = "hiring-workspace-export"
VERSION = 1
DATASETS = (
    Dataset(
        "candidates",
        "Candidates with their links and history",
        "candidates.view",
        "candidates.edit",
        "candidates_enabled",
        True,
    ),
    Dataset(
        "contacts", "Contact details", "contacts.view", "contacts.edit", "candidates_enabled", True
    ),
    Dataset(
        "hires",
        "Hires and their ratings",
        "candidates.view",
        "hires.manage",
        "candidates_enabled",
        True,
    ),
    Dataset(
        "finance",
        "Revenue and expenses with categories",
        "finance.view",
        "finance.edit",
        "finance_enabled",
        True,
    ),
    Dataset("routines", "Routines", "routines.view", "routines.manage", "routines_enabled", True),
    Dataset("templates", "Email templates", "emails.view", "emails.edit", None, False),
    Dataset("suppressions", "Do not contact list", "emails.log", "emails.send", None, True),
    Dataset("profile", "Hiring context", "context.edit", "context.edit", None, False),
    Dataset("reports", "Saved reports", "reports.view", None, None, False),
    Dataset("users", "Accounts without passwords", "users.view", None, None, True),
    Dataset("roles", "Roles and permissions", "roles.manage", None, None, False),
    Dataset("audit", "Audit log", "audit.view", None, None, True),
    Dataset("mail_log", "Sent mail log", "emails.log", None, None, True),
)
BY_NAME = {item.name: item for item in DATASETS}
ORDER = ("candidates", "contacts", "hires", "finance", "routines", "suppressions")
CSV_COLUMNS = {
    "candidates": (
        "id",
        "name",
        "headline",
        "location",
        "employer",
        "skills",
        "tags",
        "status",
        "rating",
        "researched",
        "notes",
        "links",
        "created",
    ),
    "contacts": ("id", "candidate_id", "candidate", "kind", "value", "label", "source"),
    "hires": (
        "id",
        "candidate_id",
        "candidate",
        "role_title",
        "department",
        "started",
        "ended",
        "outcome",
        "rating",
        "review",
    ),
    "finance": ("date", "kind", "amount", "currency", "category", "note"),
    "routines": (
        "name",
        "skill",
        "location",
        "status",
        "min_rating",
        "need_count",
        "interval_hours",
        "runs",
        "max_runs",
        "best_rating",
        "last_result",
    ),
    "suppressions": ("address", "reason", "created", "added_by"),
    "users": ("username", "name", "email", "role", "status", "two_step", "last_login"),
    "audit": ("id", "time", "actor", "action", "target", "outcome", "ip"),
    "mail_log": ("id", "time", "to", "subject", "kind", "status", "actor", "error"),
}
MAX_TIMESTAMP = 4_102_444_800.0
CSV_UNSAFE = ("=", "+", "-", "@", "\t", "\r")
LIST_SEPARATORS = (";", "\n")


class Rollback(Exception):
    """Signal that forces the surrounding import transaction to roll back."""

    pass


def _no_constants(name: str) -> float:
    """Reject the JSON constants NaN and Infinity.

    Args:
        name: The constant text found by the JSON parser.

    Raises:
        ValueError: Always, because the constant is not a real number.
    """
    raise ValueError(f"{name} is not a number")


def _number(text: str) -> float | None:
    """Parse text as a finite float, or return None.

    Args:
        text: The text to parse.

    Returns:
        The float value, or None when the text is not a finite number.
    """
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


class LinkRecord(BaseModel):
    """A candidate link as it appears in an import file."""

    model_config = ConfigDict(extra="ignore")

    url: str = Field(max_length=2000)
    label: str = Field(default="", max_length=300)


class EventRecord(BaseModel):
    """A candidate history event as it appears in an import file."""

    model_config = ConfigDict(extra="ignore")

    ts: float = Field(ge=0, le=MAX_TIMESTAMP, allow_inf_nan=False)
    kind: str = Field(default="note", max_length=20)
    summary: str = Field(default="", max_length=400)
    actor: str = Field(default="", max_length=100)


class CandidateRecord(BaseModel):
    """A candidate row of an import file, validated with size limits.

    Unknown keys are ignored and every field has a length or range cap.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default="", max_length=40)
    name: str = Field(max_length=300)
    headline: str = Field(default="", max_length=600)
    location: str = Field(default="", max_length=600)
    employer: str = Field(default="", max_length=600)
    skills: list[str] = Field(default_factory=list, max_length=200)
    tags: list[str] = Field(default_factory=list, max_length=100)
    status: str | None = Field(default=None, max_length=20)
    rating: float | None = Field(default=None, ge=0, le=5)
    researched: int = Field(default=0, ge=0, le=100_000)
    notes: str = Field(default="", max_length=60_000)
    created: float | None = Field(default=None, ge=0, le=MAX_TIMESTAMP, allow_inf_nan=False)
    links: list[LinkRecord] = Field(default_factory=list, max_length=200)
    events: list[EventRecord] = Field(default_factory=list, max_length=5000)


class ContactRecord(BaseModel):
    """A contact detail row of an import file.

    It names its candidate by id, by name, or both.
    """

    model_config = ConfigDict(extra="ignore")

    candidate_id: str = Field(default="", max_length=40)
    candidate: str = Field(default="", max_length=300)
    kind: str = Field(max_length=12)
    value: str = Field(max_length=2000)
    label: str = Field(default="", max_length=300)


class HireRecord(BaseModel):
    """A hire row of an import file.

    It names its candidate by id, by name, or both.
    """

    model_config = ConfigDict(extra="ignore")

    candidate_id: str = Field(default="", max_length=40)
    candidate: str = Field(default="", max_length=300)
    role_title: str = Field(max_length=300)
    department: str = Field(default="", max_length=300)
    started: str = Field(default="", max_length=10)
    ended: str = Field(default="", max_length=10)
    outcome: str = Field(default="active", max_length=12)
    rating: int | None = None
    review: str = Field(default="", max_length=60_000)


class FinanceRecord(BaseModel):
    """A revenue or expense row of an import file."""

    model_config = ConfigDict(extra="ignore")

    day: str = Field(max_length=10)
    kind: str = Field(max_length=10)
    amount: str = Field(max_length=24)
    category: str = Field(default="", max_length=100)
    note: str = Field(default="", max_length=1000)


class RoutineRecord(BaseModel):
    """A routine row of an import file."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(default="", max_length=200)
    skill: str = Field(max_length=200)
    location: str = Field(max_length=200)
    size: int | None = Field(default=None, ge=1, le=10)
    min_rating: float | None = Field(default=None, ge=0, le=5)
    need_musts: bool = True
    need_count: int = Field(default=1, ge=1, le=10)
    interval_hours: int | None = Field(default=None, ge=1)
    max_runs: int | None = Field(default=None, ge=1)
    notify_emails: list[str] = Field(default_factory=list, max_length=50)
    requirements: list[Requirement] = Field(default_factory=list, max_length=50)
    options: ResearchOptions = Field(default_factory=ResearchOptions)


class SuppressionRecord(BaseModel):
    """A do-not-contact row of an import file."""

    model_config = ConfigDict(extra="ignore")

    address: str = Field(max_length=320)
    reason: str = Field(default="", max_length=300)


class Outcome(BaseModel):
    """Counts and error messages for one dataset in an import."""

    dataset: str
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = Field(default_factory=list)


class Result(BaseModel):
    """The outcome of an import preview or run across all datasets."""

    outcomes: list[Outcome]
    applied: bool = False

    @property
    def ok(self) -> bool:
        """Return True when no dataset reported an error."""
        return all(not item.errors for item in self.outcomes)


class Stash(BaseModel):
    """A previewed import held until the user confirms it.

    It belongs to one owner and expires after the configured number of minutes.
    """

    owner: str
    created: float
    records: dict[str, Any]
    strategy: str
    filename: str
    confirmed_purpose: bool = False


def safe_cell(value: object) -> str:
    """Return a CSV cell value that spreadsheets will not run as a formula.

    Values starting with a formula or control character get a leading apostrophe.

    Args:
        value: The value to write, where None becomes an empty cell.

    Returns:
        The cell text, prefixed with an apostrophe when it starts unsafely.
    """
    cell = "" if value is None else str(value)
    return f"'{cell}" if cell.startswith(CSV_UNSAFE) else cell


def split_list(raw: str) -> list[str]:
    """Split a cell into a list on semicolons and line breaks.

    Args:
        raw: The cell text.

    Returns:
        The trimmed, non-empty items in their original order.
    """
    parts = [raw]
    for separator in LIST_SEPARATORS:
        parts = [piece for part in parts for piece in part.split(separator)]
    return [item.strip() for item in parts if item.strip()]


class Portability:
    """Export and import of workspace data as JSON and CSV.

    Imports are previewed first, validated row by row, and applied inside one
    database transaction. Every dataset is checked against the caller's permissions.
    """

    MODELS: ClassVar[dict[str, type[BaseModel]]] = {
        "candidates": CandidateRecord,
        "contacts": ContactRecord,
        "hires": HireRecord,
        "finance": FinanceRecord,
        "routines": RoutineRecord,
        "suppressions": SuppressionRecord,
    }

    def __init__(
        self,
        store: Store,
        candidates: Candidates,
        finance: FinanceBook,
        routines: Routines,
        mailer_of: Callable[[], Mailer],
        accounts: Accounts,
        templates_of: Callable[[], TemplateStore],
        tuning: PortabilityTuning,
        clock: Callable[[], float] = time.time,
    ):
        """Initialize the export and import service.

        Args:
            store: Database access, also used for the profile and saved reports.
            candidates: Candidate book for candidates, contacts and hires.
            finance: Finance book for revenue and expense entries.
            routines: Routine manager.
            mailer_of: Callable returning the current mailer.
            accounts: Accounts, roles and audit log.
            templates_of: Callable returning the current email template store.
            tuning: Limits for exports, imports and the preview stash.
            clock: Callable returning the current time in epoch seconds.
        """
        self._store = store
        self._candidates = candidates
        self._finance = finance
        self._routines = routines
        self._mailer_of = mailer_of
        self._accounts = accounts
        self._templates_of = templates_of
        self._tuning = tuning
        self._clock = clock
        self._stash: dict[str, Stash] = {}
        self._names: dict[str, list[str]] | None = None

    def available(
        self, can: Callable[[str], bool], modules: Callable[[str | None], bool]
    ) -> list[Dataset]:
        """Return the datasets the caller may export.

        Args:
            can: Check whether the caller holds a permission.
            modules: Check whether a feature module is enabled (None means always).

        Returns:
            The datasets whose view permission and module are both allowed.
        """
        return [item for item in DATASETS if can(item.view) and modules(item.module)]

    def importable(
        self, can: Callable[[str], bool], modules: Callable[[str | None], bool]
    ) -> list[Dataset]:
        """Return the datasets the caller may import.

        Args:
            can: Check whether the caller holds a permission.
            modules: Check whether a feature module is enabled (None means always).

        Returns:
            The datasets that can be edited and whose permission and module are allowed.
        """
        return [
            item
            for item in DATASETS
            if item.edit is not None and can(item.edit) and modules(item.module)
        ]

    def _everyone(self) -> Iterator[Any]:
        """Iterate over every candidate, fetched in pages of 500.

        Returns:
            An iterator of candidate summaries.
        """
        offset = 0
        while True:
            page = self._candidates.page(sort="created", offset=offset, size=500)
            yield from page.items
            offset += 500
            if offset >= page.total:
                return

    def rows(self, name: str) -> list[dict[str, Any]]:
        """Return the exportable rows of one dataset.

        Candidates include their links and a capped list of events. Users are listed
        without passwords, and reports, audit and mail log rows are capped by tuning.

        Args:
            name: The dataset name.

        Returns:
            One dictionary per row, ready for JSON.

        Raises:
            InvalidRequest: When the dataset does not exist.
        """
        tuning = self._tuning
        if name == "candidates":
            return [
                {
                    **item.model_dump(mode="json", exclude={"hire_count"}),
                    "links": [link.model_dump() for link in self._candidates.links(item.id)],
                    "events": [
                        {
                            "ts": event.ts,
                            "kind": event.kind,
                            "summary": event.summary,
                            "actor": event.actor,
                        }
                        for event in self._candidates.export_events(item.id, tuning.export_events)
                    ],
                }
                for item in self._everyone()
            ]
        if name == "contacts":
            return [
                {**contact.model_dump(mode="json"), "candidate": item.name}
                for item in self._everyone()
                for contact in self._candidates.contacts(item.id)
            ]
        if name == "hires":
            return [
                {**hire.model_dump(mode="json"), "candidate": item.name}
                for item in self._everyone()
                for hire in self._candidates.hires(item.id)
            ]
        if name == "finance":
            unit = self._finance.currency()
            return [
                {
                    "date": entry.day,
                    "day": entry.day,
                    "kind": entry.kind,
                    "amount": self._finance.plain(entry.amount),
                    "currency": unit,
                    "category": entry.category,
                    "note": entry.note,
                }
                for entry in self._finance.all_entries()
            ]
        if name == "routines":
            return [item.model_dump(mode="json") for item in self._routines.all()]
        if name == "templates":
            return [
                item.model_dump(mode="json", exclude={"builtin"})
                for item in self._templates_of().custom()
            ]
        if name == "suppressions":
            return [item.model_dump(mode="json") for item in self._mailer_of().suppressions()]
        if name == "profile":
            return [load_profile(self._store).model_dump(mode="json")]
        if name == "reports":
            rows = self._store.query(
                "SELECT id, kind, title, state, rating, created, report FROM jobs "
                "WHERE report IS NOT NULL ORDER BY created DESC LIMIT ?",
                (tuning.export_reports,),
            )
            return [
                {
                    **{
                        key: row[key]
                        for key in ("id", "kind", "title", "state", "rating", "created")
                    },
                    "report": json.loads(row["report"]),
                }
                for row in rows
            ]
        if name == "users":
            return [
                {
                    "username": user.username,
                    "name": user.label,
                    "email": user.email,
                    "role": user.role_name,
                    "status": user.status,
                    "two_step": user.totp_enabled,
                    "last_login": user.last_login,
                }
                for user in self._accounts.users()
            ]
        if name == "roles":
            return [
                {
                    "name": role.name,
                    "description": role.description,
                    "permissions": sorted(role.effective()),
                    "builtin": role.builtin,
                }
                for role in self._accounts.roles()
            ]
        if name == "audit":
            return [
                {
                    "id": entry.id,
                    "time": entry.ts,
                    "actor": entry.actor,
                    "action": entry.action,
                    "target": entry.target,
                    "outcome": entry.outcome,
                    "ip": entry.ip,
                }
                for entry in self._accounts.audit(limit=tuning.export_log_rows)
            ]
        if name == "mail_log":
            return [
                {
                    "id": entry.id,
                    "time": entry.ts,
                    "to": entry.to_address,
                    "subject": entry.subject,
                    "kind": entry.kind,
                    "status": entry.status,
                    "actor": entry.actor,
                    "error": entry.error,
                }
                for entry in self._mailer_of().entries(limit=tuning.export_log_rows)
            ]
        raise InvalidRequest("That dataset does not exist.")

    def bundle(self, names: list[str]) -> dict[str, Any]:
        """Build the JSON export document for the chosen datasets.

        Args:
            names: The dataset names to export.

        Returns:
            A dictionary with the format, version, export time, currency and rows.

        Raises:
            InvalidRequest: When no names are given or one is unknown.
        """
        unknown = [name for name in names if name not in BY_NAME]
        if unknown or not names:
            raise InvalidRequest("Choose which data to export.")
        return {
            "format": FORMAT,
            "version": VERSION,
            "exported": self._clock(),
            "currency": self._finance.currency(),
            "datasets": {name: self.rows(name) for name in names},
        }

    def csv_text(self, name: str) -> str:
        """Render one dataset as CSV text.

        Every cell goes through safe_cell to block spreadsheet formula injection.

        Args:
            name: The dataset name.

        Returns:
            The CSV text with a header row.

        Raises:
            InvalidRequest: When the dataset has no CSV form.
        """
        columns = CSV_COLUMNS.get(name)
        if columns is None:
            raise InvalidRequest("That dataset has no CSV form.")
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(columns)
        for row in self.rows(name):
            writer.writerow([safe_cell(self._cell(name, row, column)) for column in columns])
        return buffer.getvalue()

    def _cell(self, name: str, row: dict[str, Any], column: str) -> Any:
        """Return the raw value of one CSV cell.

        Args:
            name: The dataset name.
            row: The exported row.
            column: The CSV column name.

        Returns:
            The value, with candidate skills, tags and link URLs joined by semicolons.
        """
        if name == "candidates" and column in {"skills", "tags"}:
            return "; ".join(row.get(column, []))
        if name == "candidates" and column == "links":
            return "; ".join(link["url"] for link in row.get("links", []))
        if name == "audit" and column == "time":
            return row.get("time")
        return row.get(column)

    def parse_json(self, raw: bytes) -> dict[str, Any]:
        """Parse and check an uploaded JSON export.

        Args:
            raw: The uploaded file bytes, optionally with a UTF-8 byte order mark.

        Returns:
            The rows of each recognised dataset, keyed by dataset name.

        Raises:
            InvalidRequest: When the file is too large, is not valid JSON, is not an
                export of this workspace or version, or has too many rows.
        """
        if len(raw) > self._tuning.max_bytes:
            raise InvalidRequest("That file is too large.")
        try:
            document = json.loads(raw.decode("utf-8-sig"), parse_constant=_no_constants)
        except (ValueError, UnicodeDecodeError, RecursionError) as error:
            raise InvalidRequest("The file is not valid JSON.") from error
        if not isinstance(document, dict) or document.get("format") != FORMAT:
            raise InvalidRequest("This is not an export from this workspace.")
        if document.get("version") != VERSION:
            raise InvalidRequest("This export was made by a different version.")
        datasets = document.get("datasets")
        if not isinstance(datasets, dict):
            raise InvalidRequest("The export has no data in it.")
        found = {name: rows for name, rows in datasets.items() if name in BY_NAME}
        total = sum(len(rows) for rows in found.values() if isinstance(rows, list))
        if total > self._tuning.max_rows:
            raise InvalidRequest("The file has too many rows.")
        return found

    def parse_csv(self, dataset: str, raw: bytes) -> dict[str, Any]:
        """Parse an uploaded CSV file into rows for one dataset.

        Header names are lower-cased and values trimmed.

        Args:
            dataset: The dataset the file is meant for.
            raw: The uploaded file bytes.

        Returns:
            A dictionary mapping the dataset name to its list of rows.

        Raises:
            InvalidRequest: When the dataset cannot be imported from CSV, or the file
                is too large, not UTF-8, not valid CSV, or has too many rows.
        """
        if dataset not in self.MODELS or not BY_NAME[dataset].csv:
            raise InvalidRequest("That dataset cannot be imported from CSV.")
        if len(raw) > self._tuning.max_bytes:
            raise InvalidRequest("That file is too large.")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise InvalidRequest("The file must be UTF-8 text.") from error
        reader = csv.DictReader(io.StringIO(text))
        rows: list[dict[str, Any]] = []
        try:
            for number, row in enumerate(reader, start=1):
                if number > self._tuning.max_rows:
                    raise InvalidRequest("The file has too many rows.")
                clean = {
                    (key or "").strip().lower(): (value if isinstance(value, str) else "").strip()
                    for key, value in row.items()
                    if key
                }
                rows.append(self._from_csv(dataset, clean))
        except csv.Error as error:
            raise InvalidRequest("The file is not valid CSV.") from error
        return {dataset: rows}

    def _from_csv(self, dataset: str, row: dict[str, str]) -> dict[str, Any]:
        """Convert one CSV row into the shape of an import record.

        Args:
            dataset: The dataset name.
            row: The cleaned CSV row.

        Returns:
            The row with lists split, numbers parsed and column aliases applied.
        """
        if dataset == "candidates":
            return {
                **row,
                "skills": split_list(row.get("skills", "")),
                "tags": split_list(row.get("tags", "")),
                "links": [{"url": url} for url in split_list(row.get("links", ""))],
                "rating": _number(row.get("rating", "")),
                # isdigit accepts characters such as superscripts that int cannot parse, so ASCII is
                # required as well.
                "researched": int(row["researched"])
                if row.get("researched", "").isascii() and row.get("researched", "").isdigit()
                else 0,
                "created": _number(row.get("created", "")),
                "status": row.get("status") or None,
            }
        if dataset == "finance":
            return {**row, "day": row.get("day") or row.get("date", "")}
        if dataset == "hires":
            return {
                **row,
                "rating": int(row["rating"])
                if row.get("rating", "").isascii() and row.get("rating", "").isdigit()
                else None,
            }
        return row

    def preview(
        self,
        records: dict[str, Any],
        strategy: str,
        owner: str,
        filename: str,
        can: Callable[[str], bool],
        actor: str,
        confirmed_purpose: bool,
    ) -> tuple[str, Result]:
        """Dry-run an import and keep it for later confirmation.

        Expired entries are dropped and the per-owner and total stash limits are
        enforced by evicting the oldest entries.

        Args:
            records: The parsed rows keyed by dataset name.
            strategy: What to do with data that already exists.
            owner: The user the preview belongs to.
            filename: The uploaded file name, cut to 100 characters.
            can: Check whether the caller holds a permission.
            actor: The name recorded as the author of changes.
            confirmed_purpose: Whether the user confirmed the purpose for routines.

        Returns:
            A random single-use token and the dry-run result.

        Raises:
            InvalidRequest: When the strategy is not allowed.
        """
        result = self.apply(records, strategy, actor, can, True, confirmed_purpose)
        now = self._clock()
        self._stash = {
            token: item
            for token, item in self._stash.items()
            if now - item.created < self._tuning.stash_minutes * 60
        }
        mine = [token for token, item in self._stash.items() if item.owner == owner]
        for token in mine[: max(0, len(mine) - self._tuning.stash_per_owner + 1)]:
            del self._stash[token]
        while len(self._stash) >= self._tuning.stash_max:
            del self._stash[next(iter(self._stash))]
        token = secrets.token_urlsafe(24)
        self._stash[token] = Stash(
            owner=owner,
            created=now,
            records=records,
            strategy=strategy,
            filename=filename[:100],
            confirmed_purpose=confirmed_purpose,
        )
        return token, result

    def take(self, token: str, owner: str) -> Stash | None:
        # The token is removed before the owner check, so it can only be tried once.
        """Remove and return a stashed preview if it is still valid.

        The token is consumed even when the owner does not match.

        Args:
            token: The token returned by preview.
            owner: The user claiming the preview.

        Returns:
            The stashed import, or None when the token is unknown, belongs to someone
            else, or has expired.
        """
        # The token is removed before the owner check, so it can only be tried once.
        item = self._stash.pop(token, None)
        if item is None or item.owner != owner:
            return None
        if self._clock() - item.created > self._tuning.stash_minutes * 60:
            return None
        return item

    def apply(
        self,
        records: dict[str, Any],
        strategy: str,
        actor: str,
        can: Callable[[str], bool],
        dry_run: bool,
        confirmed_purpose: bool = False,
    ) -> Result:
        """Check and optionally apply an import.

        Datasets are checked for permission first. When applying, file-backed datasets
        are dry-run, then database datasets run in one transaction that is rolled back
        on a dry run or any error, and finally templates and profile are written.

        Args:
            records: The rows keyed by dataset name.
            strategy: What to do with data that already exists.
            actor: The name recorded as the author of changes.
            can: Check whether the caller holds a permission.
            dry_run: When true nothing is saved.
            confirmed_purpose: Whether the user confirmed the purpose for routines.

        Returns:
            The per-dataset outcomes and whether the import was applied.

        Raises:
            InvalidRequest: When the strategy is not allowed.
        """
        if strategy not in self._tuning.strategies:
            raise InvalidRequest("Choose what to do with what already exists.")
        outcomes = {name: Outcome(dataset=name) for name in records}
        for name in records:
            spec = BY_NAME.get(name)
            if spec is None or spec.edit is None:
                outcomes[name].errors.append("This dataset cannot be imported.")
            elif not can(spec.edit):
                outcomes[name].errors.append("You may not import this dataset.")
            elif not isinstance(records[name], list) and name != "profile":
                outcomes[name].errors.append("The data is not a list.")
        blocked = any(item.errors for item in outcomes.values())
        if not blocked and not dry_run:
            # Dry-run templates and profile before the transaction so a bad file row cannot leave
            # the database changed.
            scratch = {name: Outcome(dataset=name) for name in outcomes}
            self._files(records, scratch, True, strategy)
            for name, item in scratch.items():
                outcomes[name].errors.extend(item.errors)
            blocked = any(item.errors for item in outcomes.values())
        failed = blocked
        if not blocked:
            try:
                with self._store.transaction():
                    self._database(records, strategy, actor, outcomes, confirmed_purpose)
                    failed = any(item.errors for item in outcomes.values())
                    if dry_run or failed:
                        raise Rollback
            except Rollback:
                pass
            except AgentError as error:
                outcomes[next(iter(outcomes))].errors.append(str(error))
                failed = True
        applied = not dry_run and not failed
        if not failed:
            self._files(records, outcomes, dry_run, strategy)
            failed = any(item.errors for item in outcomes.values())
            applied = not dry_run and not failed
        return Result(outcomes=list(outcomes.values()), applied=applied)

    def _note(self, outcome: Outcome, index: int, error: Exception) -> None:
        """Record a row error on an outcome, up to the configured limit.

        Args:
            outcome: The dataset outcome to add to.
            index: The 1-based row number.
            error: The problem found.
        """
        if len(outcome.errors) < self._tuning.errors_shown:
            outcome.errors.append(f"Row {index}: {error}")

    def _database(
        self,
        records: dict[str, Any],
        strategy: str,
        actor: str,
        outcomes: dict[str, Outcome],
        confirmed: bool,
    ) -> None:
        """Import the database-backed datasets in dependency order.

        Rows that fail validation or import are reported on their outcome instead of
        being raised. The candidate name cache is reset first.

        Args:
            records: The rows keyed by dataset name.
            strategy: What to do with data that already exists.
            actor: The name recorded as the author of changes.
            outcomes: The outcomes to update.
            confirmed: Whether the user confirmed the purpose for routines.
        """
        mapping: dict[str, str] = {}
        self._names = None
        for name in ORDER:
            if name not in records:
                continue
            outcome = outcomes[name]
            for index, raw in enumerate(records[name], start=1):
                try:
                    record = self.MODELS[name].model_validate(raw)
                    self._one(name, record, strategy, actor, outcome, mapping, confirmed)
                except (AgentError, ValidationError, ValueError, TypeError) as error:
                    self._note(outcome, index, self._plain(error))

    def _plain(self, error: Exception) -> Exception:
        """Turn a validation error into a short readable error.

        Args:
            error: The error to simplify.

        Returns:
            A ValueError naming the first failing field, or the original error.
        """
        if isinstance(error, ValidationError):
            first = error.errors()[0]
            return ValueError(f"{'.'.join(str(part) for part in first['loc'])}: {first['msg']}")
        return error

    def _resolve(self, record: Any, mapping: dict[str, str]) -> str:
        """Find the candidate a contact or hire row belongs to.

        A row is matched by an id from this import, by an existing id, or by a unique
        case-insensitive name.

        Args:
            record: The contact or hire record.
            mapping: Imported ids mapped to stored candidate ids.

        Returns:
            The stored candidate id.

        Raises:
            InvalidRequest: When the candidate is missing, unknown or ambiguous.
        """
        if record.candidate_id and record.candidate_id in mapping:
            return mapping[record.candidate_id]
        if record.candidate_id and self._candidates.get(record.candidate_id):
            return str(record.candidate_id)
        if record.candidate:
            if self._names is None:
                self._names = {}
                for identifier, name in self._candidates.names():
                    self._names.setdefault(name.casefold(), []).append(identifier)
            exact = self._names.get(record.candidate.casefold(), [])
            if len(exact) == 1:
                return exact[0]
            raise InvalidRequest(f"The candidate {record.candidate} was not found or is ambiguous.")
        raise InvalidRequest("The row does not say which candidate it belongs to.")

    def _one(
        self,
        name: str,
        record: Any,
        strategy: str,
        actor: str,
        outcome: Outcome,
        mapping: dict[str, str],
        confirmed: bool,
    ) -> None:
        """Import one validated record into its dataset.

        Args:
            name: The dataset name.
            record: The validated record.
            strategy: What to do with data that already exists.
            actor: The name recorded as the author of changes.
            outcome: The dataset outcome to update.
            mapping: Imported ids mapped to stored candidate ids.
            confirmed: Whether the user confirmed the purpose for routines.
        """
        if name == "candidates":
            self._candidate(record, strategy, actor, outcome, mapping)
        elif name == "contacts":
            target = self._resolve(record, mapping)
            added = self._candidates.add_contact(
                target, record.kind, record.value, label=record.label, source=IMPORT, actor=actor
            )
            outcome.created += int(added)
            outcome.skipped += int(not added)
        elif name == "hires":
            self._hire(record, actor, outcome, mapping)
        elif name == "finance":
            if self._finance.has_entry(
                record.day, record.kind, record.amount, record.category, record.note
            ):
                outcome.skipped += 1
                return
            self._finance.add_many(
                [(record.day, record.kind, record.amount, record.category, record.note)], actor
            )
            outcome.created += 1
        elif name == "routines":
            self._routine(record, actor, outcome, confirmed)
        elif name == "suppressions":
            mailer = self._mailer_of()
            existed = mailer.suppressed(record.address)
            mailer.suppress(record.address, record.reason, actor)
            outcome.skipped += int(existed)
            outcome.created += int(not existed)

    def _candidate(
        self,
        record: CandidateRecord,
        strategy: str,
        actor: str,
        outcome: Outcome,
        mapping: dict[str, str],
    ) -> None:
        """Create a candidate, or merge into a matching one.

        A match is found by id or by identity. With the update strategy matched
        candidates are updated and gain links, otherwise they are skipped. New
        candidates also receive their imported history events.

        Args:
            record: The candidate record.
            strategy: What to do with data that already exists.
            actor: The name recorded as the author of changes.
            outcome: The dataset outcome to update.
            mapping: Imported ids mapped to stored candidate ids, updated here.
        """
        links = [item.url for item in record.links]
        existing = self._candidates.get(record.id) if record.id else None
        if existing is None:
            identity = Identity(
                name=record.name,
                location=record.location or None,
                employers=[record.employer] if record.employer else [],
                links=links,
            )
            existing = self._candidates.find_match(identity)
        if existing is not None:
            mapping[record.id or existing.id] = existing.id
            if strategy == "update":
                self._candidates.update(
                    existing.id,
                    name=record.name,
                    headline=record.headline or None,
                    location=record.location or None,
                    employer=record.employer or None,
                    skills=record.skills or None,
                    tags=record.tags or None,
                    status=record.status,
                    notes=record.notes or None,
                    actor=actor,
                )
                for item in record.links:
                    self._candidates.add_link(existing.id, item.url, item.label)
                outcome.updated += 1
            else:
                outcome.skipped += 1
            return
        created = self._candidates.create(
            record.name,
            headline=record.headline,
            location=record.location,
            employer=record.employer,
            skills=record.skills,
            tags=record.tags,
            status=record.status,
            notes=record.notes,
            links=links,
            actor=actor,
            source=IMPORT,
            rating=record.rating,
            researched=record.researched,
            created=record.created,
            identifier=record.id or None,
        )
        mapping[record.id or created.id] = created.id
        if self._names is not None:
            self._names.setdefault(created.name.casefold(), []).append(created.id)
        for event in record.events:
            self._candidates.import_event(
                created.id, event.ts, event.kind, event.summary, event.actor
            )
        outcome.created += 1

    def _hire(
        self, record: HireRecord, actor: str, outcome: Outcome, mapping: dict[str, str]
    ) -> None:
        """Import a hire unless the same role and start date already exists.

        The candidate status is left unchanged. Ending details, outcome, rating and
        review are applied afterwards when present.

        Args:
            record: The hire record.
            actor: The name recorded as the author of changes.
            outcome: The dataset outcome to update.
            mapping: Imported ids mapped to stored candidate ids.
        """
        target = self._resolve(record, mapping)
        for known in self._candidates.hires(target):
            if known.role_title == record.role_title and known.started == record.started:
                outcome.skipped += 1
                return
        hire = self._candidates.hire(
            target,
            record.role_title,
            department=record.department,
            started=record.started,
            actor=actor,
            set_status=False,
        )
        if record.outcome != "active" or record.ended or record.rating or record.review:
            self._candidates.update_hire(
                target,
                hire.id,
                ended=record.ended,
                outcome=record.outcome,
                rating=record.rating,
                review=record.review,
                actor=actor,
            )
        outcome.created += 1

    def _routine(
        self, record: RoutineRecord, actor: str, outcome: Outcome, confirmed: bool
    ) -> None:
        """Import a routine as a paused routine unless its name already exists.

        The name defaults to the skill and location.

        Args:
            record: The routine record.
            actor: The name recorded as the author of changes.
            outcome: The dataset outcome to update.
            confirmed: Whether the user confirmed the purpose for routines.
        """
        label = record.name or f"{record.skill} in {record.location}"
        if any(item.name == label for item in self._routines.all()):
            outcome.skipped += 1
            return
        self._routines.create(
            label,
            record.skill,
            record.location,
            size=record.size,
            requirements=record.requirements,
            options=record.options,
            min_rating=record.min_rating,
            need_musts=record.need_musts,
            need_count=record.need_count,
            interval_hours=record.interval_hours,
            max_runs=record.max_runs,
            notify_emails=record.notify_emails,
            purpose_confirmed=confirmed,
            actor=actor,
            paused=True,
        )
        outcome.created += 1

    def _files(
        self, records: dict[str, Any], outcomes: dict[str, Outcome], dry_run: bool, strategy: str
    ) -> None:
        """Import the templates and profile datasets.

        These are validated on every call but only saved when dry_run is false.

        Args:
            records: The rows keyed by dataset name.
            outcomes: The outcomes to update.
            dry_run: When true nothing is saved.
            strategy: What to do with data that already exists.
        """
        if "templates" in records:
            outcome = outcomes["templates"]
            templates = self._templates_of()
            fresh = 0
            for index, raw in enumerate(records["templates"], start=1):
                try:
                    item = EmailTemplate.model_validate({**raw, "builtin": False})
                    known = templates.get(item.id) is not None
                    if known and strategy == "skip" and item.id not in templates.builtin_ids():
                        outcome.skipped += 1
                        continue
                    templates.check(item, fresh)
                    if not dry_run:
                        templates.save(item)
                    fresh += int(not known)
                    outcome.updated += int(known)
                    outcome.created += int(not known)
                except (AgentError, ValidationError, ValueError, TypeError) as error:
                    self._note(outcome, index, self._plain(error))
        if "profile" in records:
            outcome = outcomes["profile"]
            data = records["profile"]
            items = data if isinstance(data, list) else [data]
            try:
                profile = Profile.model_validate(items[0] if items else {})
                if strategy == "skip" and load_profile(self._store) != Profile():
                    outcome.skipped += 1
                    return
                if not dry_run:
                    save_profile(self._store, profile)
                outcome.updated += 1
            except (ValidationError, ValueError, TypeError, IndexError) as error:
                self._note(outcome, 1, self._plain(error))
