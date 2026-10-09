# src/agent/stats.py
from datetime import (
    datetime,
    timedelta,
    UTC,
)
from agent.finance import FinanceBook
from agent.config import StatsTuning
from collections.abc import Callable
from agent.db import Database
from typing import Any

import time

DAY = 86_400
WEEK = 7 * DAY
Series = list[tuple[str, int]]


def clamp(value: int | None, default: int, low: int, high: int) -> int:
    """Return a value limited to a range, or a default when it is missing.

    Args:
        value: the requested value, or None.
        default: the result when value is None; it is not clamped.
        low: the smallest allowed value.
        high: the largest allowed value.

    Returns:
        The default for None, otherwise the value limited to the range.
    """
    return default if value is None else max(low, min(high, value))


class Stats:
    """Read-only dashboard figures computed from the database and the finance book.

    Days are counted in UTC and every window is clamped to the configured limits.
    """

    def __init__(
        self,
        database: Database,
        finance: FinanceBook,
        tuning: StatsTuning,
        clock: Callable[[], float] = time.time,
    ):
        """Initialize the statistics reader.

        Args:
            database: the database to query.
            finance: the finance book used for money figures.
            tuning: default and maximum window sizes.
            clock: function returning the current time in seconds since the epoch.
        """
        self._db = database
        self._finance = finance
        self._tuning = tuning
        self._clock = clock

    def _days(self, days: int | None) -> int:
        """Return a day window within the configured limits.

        Args:
            days: the requested number of days, or None for the default.

        Returns:
            The number of days, at least 1 and at most the configured maximum.
        """
        return clamp(days, self._tuning.default_days, 1, self._tuning.max_days)

    def _months(self, months: int | None) -> int:
        """Return a month window within the configured limits.

        Args:
            months: the requested number of months, or None for the default.

        Returns:
            The number of months, at least 1 and at most the configured maximum.
        """
        return clamp(months, self._tuning.default_months, 1, self._tuning.max_months)

    def _labels(self, days: int) -> list[str]:
        """Return the ISO dates of the last days, oldest first.

        Args:
            days: how many days to list, ending with today in UTC.

        Returns:
            The dates as YYYY-MM-DD strings.
        """
        end = datetime.fromtimestamp(self._clock(), UTC).date()
        return [(end - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]

    def _filled(self, rows: list[Any], days: int) -> Series:
        """Turn per-day counts into a series with a zero for every missing day.

        Args:
            rows: rows with day and n columns.
            days: the length of the window in days.

        Returns:
            One (date, count) pair per day, oldest first.
        """
        counted = {row["day"]: row["n"] for row in rows}
        return [(label, counted.get(label, 0)) for label in self._labels(days)]

    def _weeks(self, rows: list[Any], weeks: int) -> Series:
        """Turn per-week counts into a series with a zero for every missing week.

        Args:
            rows: rows with week and n columns.
            weeks: the number of weeks to list, ending with the current one.

        Returns:
            One (year-week label, count) pair per week, oldest first.
        """
        counted = {row["week"]: row["n"] for row in rows}
        now = datetime.fromtimestamp(self._clock(), UTC)
        labels = []
        for offset in range(weeks - 1, -1, -1):
            moment = now - timedelta(weeks=offset)
            # Use the same Monday-first %Y-%W format as SQLite so counted weeks line up with the
            # labels.
            labels.append(moment.strftime("%Y-%W"))
        return [(label, counted.get(label, 0)) for label in labels]

    def searches(self, days: int | None = None) -> dict[str, Any]:
        """Summarize research jobs created within a recent window.

        Args:
            days: the window length in days, or None for the default.

        Returns:
            A dictionary with the window, the total, the per-day series, counts by kind
            and by state, and the average and best rating (None when nothing is rated).
        """
        window = self._days(days)
        since = self._clock() - window * DAY
        per_day = self._db.query(
            "SELECT date(created, 'unixepoch') AS day, COUNT(*) AS n FROM jobs "
            "WHERE created >= ? GROUP BY day",
            (since,),
        )
        kinds = self._db.query(
            "SELECT kind, COUNT(*) AS n FROM jobs WHERE created >= ? GROUP BY kind", (since,)
        )
        states = self._db.query(
            "SELECT state, COUNT(*) AS n FROM jobs WHERE created >= ? GROUP BY state", (since,)
        )
        rated = self._db.one(
            "SELECT AVG(rating) AS mean, MAX(rating) AS best, COUNT(rating) AS n FROM jobs "
            "WHERE created >= ? AND rating IS NOT NULL",
            (since,),
        )
        return {
            "days": window,
            "total": sum(row["n"] for row in kinds),
            "per_day": self._filled(per_day, window),
            "by_kind": [(row["kind"], row["n"]) for row in kinds],
            "by_state": [(row["state"], row["n"]) for row in states],
            "average_rating": round(rated["mean"], 2)
            if rated and rated["mean"] is not None
            else None,
            "best_rating": rated["best"] if rated else None,
        }

    def candidates(self) -> dict[str, Any]:
        """Summarize the saved candidates.

        Returns:
            A dictionary with the total, how many were hired before, how many were
            researched, the average rating, counts by status and a rating histogram.
        """
        statuses = self._db.query(
            "SELECT status, COUNT(*) AS n FROM candidates GROUP BY status ORDER BY n DESC"
        )
        buckets = self._db.query(
            # Cap the rating just below 5 so a perfect score falls into the 4-5 bucket.
            "SELECT CAST(MIN(rating, 4.999) AS INTEGER) AS bucket, COUNT(*) AS n FROM candidates "
            "WHERE rating IS NOT NULL GROUP BY bucket"
        )
        figures = self._db.one(
            "SELECT COUNT(*) AS total, AVG(rating) AS mean, SUM(researched) AS looked, "
            "(SELECT COUNT(DISTINCT candidate_id) FROM hires) AS hired FROM candidates"
        )
        counted = {row["bucket"]: row["n"] for row in buckets}
        return {
            "total": figures["total"] if figures else 0,
            "hired_before": figures["hired"] if figures else 0,
            "researched": (figures["looked"] or 0) if figures else 0,
            "average_rating": round(figures["mean"], 2)
            if figures and figures["mean"] is not None
            else None,
            "by_status": [(row["status"], row["n"]) for row in statuses],
            "ratings": [(f"{low}-{low + 1}", counted.get(low, 0)) for low in range(5)],
        }

    def hires(self, months: int | None = None) -> dict[str, Any]:
        """Summarize recorded hires over recent months.

        Args:
            months: the window length in months, or None for the default.

        Returns:
            A dictionary with the window, the total across all time, hires per month for
            the window, counts by outcome and the average rating with its sample size.
        """
        window = self._months(months)
        today = datetime.fromtimestamp(self._clock(), UTC).date()
        labels = []
        year, month = today.year, today.month
        for _ in range(window):
            labels.append(f"{year:04d}-{month:02d}")
            month -= 1
            if month == 0:
                year, month = year - 1, 12
        labels.reverse()
        rows = self._db.query(
            "SELECT strftime('%Y-%m', created, 'unixepoch') AS month, COUNT(*) AS n FROM hires "
            "WHERE strftime('%Y-%m', created, 'unixepoch') >= ? GROUP BY month",
            (labels[0],),
        )
        counted = {row["month"]: row["n"] for row in rows}
        outcomes = self._db.query(
            "SELECT outcome, COUNT(*) AS n FROM hires GROUP BY outcome ORDER BY n DESC"
        )
        rated = self._db.one("SELECT AVG(rating) AS mean, COUNT(rating) AS n FROM hires")
        return {
            "months": window,
            "total": sum(row["n"] for row in outcomes),
            "per_month": [(label, counted.get(label, 0)) for label in labels],
            "by_outcome": [(row["outcome"], row["n"]) for row in outcomes],
            "average_rating": round(rated["mean"], 2)
            if rated and rated["mean"] is not None
            else None,
            "rated": rated["n"] if rated else 0,
        }

    def routines(self, days: int | None = None) -> dict[str, Any]:
        """Summarize routines and their recent runs.

        Args:
            days: the window length in days, or None for the default.

        Returns:
            A dictionary with counts by status, runs per day and in total within the
            window, and the number of satisfied routines.
        """
        window = self._days(days)
        since = self._clock() - window * DAY
        statuses = self._db.query(
            "SELECT status, COUNT(*) AS n FROM routines GROUP BY status ORDER BY n DESC"
        )
        runs = self._db.query(
            "SELECT date(ts, 'unixepoch') AS day, COUNT(*) AS n FROM routine_runs "
            "WHERE ts >= ? GROUP BY day",
            (since,),
        )
        found = self._db.one("SELECT COUNT(*) AS n FROM routines WHERE status = 'satisfied'")
        return {
            "days": window,
            "by_status": [(row["status"], row["n"]) for row in statuses],
            "runs_per_day": self._filled(runs, window),
            "runs": sum(row["n"] for row in runs),
            "satisfied": found["n"] if found else 0,
        }

    def emails(self, days: int | None = None) -> dict[str, Any]:
        """Summarize outgoing mail activity.

        Args:
            days: the window length in days for the status counts, or None for the
                default. The weekly series always covers the configured number of weeks.

        Returns:
            A dictionary with sent mail in the window, sent mail per week, counts by
            status and the number of do-not-contact entries.
        """
        window = self._days(days)
        since = self._clock() - window * DAY
        weeks = self._tuning.weeks_shown
        sent = self._db.query(
            "SELECT strftime('%Y-%W', ts, 'unixepoch') AS week, COUNT(*) AS n FROM mail_log "
            "WHERE status = 'sent' AND ts >= ? GROUP BY week",
            (self._clock() - weeks * WEEK,),
        )
        statuses = self._db.query(
            "SELECT status, COUNT(*) AS n FROM mail_log WHERE ts >= ? GROUP BY status", (since,)
        )
        blocked = self._db.one("SELECT COUNT(*) AS n FROM suppressions")
        return {
            "days": window,
            "sent": sum(row["n"] for row in statuses if row["status"] == "sent"),
            "per_week": self._weeks(sent, weeks),
            "by_status": [(row["status"], row["n"]) for row in statuses],
            "do_not_contact": blocked["n"] if blocked else 0,
        }

    def team(self, days: int | None = None) -> dict[str, Any]:
        """Summarize accounts and recent logins.

        Args:
            days: the window length in days, or None for the default.

        Returns:
            A dictionary with the account total, counts by role and by status, and the
            successful logins per day within the window.
        """
        window = self._days(days)
        since = self._clock() - window * DAY
        roles = self._db.query(
            "SELECT roles.name AS name, COUNT(*) AS n FROM users JOIN roles ON roles.id = "
            "users.role_id GROUP BY roles.name ORDER BY n DESC"
        )
        statuses = self._db.query("SELECT status, COUNT(*) AS n FROM users GROUP BY status")
        logins = self._db.query(
            "SELECT date(ts, 'unixepoch') AS day, COUNT(*) AS n FROM audit "
            "WHERE action = 'login.ok' AND ts >= ? GROUP BY day",
            (since,),
        )
        return {
            "days": window,
            "total": sum(row["n"] for row in statuses),
            "by_role": [(row["name"], row["n"]) for row in roles],
            "by_status": [(row["status"], row["n"]) for row in statuses],
            "logins_per_day": self._filled(logins, window),
        }

    def finance(self, months: int | None = None) -> dict[str, Any]:
        """Summarize revenue and expenses over recent months.

        Args:
            months: the window length in months, or None for the default.

        Returns:
            A dictionary with the currency, monthly revenue, expense and net, the totals
            for the window and the revenue and expense totals by category.
        """
        window = self._months(months)
        series = self._finance.monthly(window)
        totals = self._finance.totals(series[0].month, series[-1].month)
        return {
            "months": window,
            "currency": self._finance.currency(),
            "per_month": [
                {
                    "month": item.month,
                    "revenue": item.revenue,
                    "expense": item.expense,
                    "net": item.net,
                }
                for item in series
            ],
            "totals": {**totals, "net": totals["revenue"] - totals["expense"]},
            "revenue_by_category": [
                (item.name, item.total)
                for item in self._finance.by_category("revenue", series[0].month, series[-1].month)
            ],
            "expense_by_category": [
                (item.name, item.total)
                for item in self._finance.by_category("expense", series[0].month, series[-1].month)
            ],
        }
