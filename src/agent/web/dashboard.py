# src/agent/web/dashboard.py
from agent.web.charts import (
    grouped_bars,
    line_plot,
    Plot,
    plot_svg,
    row_bars,
    RowPlot,
    rows_svg,
    thin,
)
from agent.web.common import (
    audit,
    render,
    requires,
)
from agent.web.context import (
    context_of,
    WebContext,
)
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.requests import Request
from agent.accounts import Principal
from dataclasses import dataclass
from agent.policy import Policy
from agent.forms import Topic

TOPICS = (
    Topic("searches", "Searches", "reports.view", None, "days"),
    Topic("candidates", "Candidates", "candidates.view", "candidates_enabled", None),
    Topic("hires", "Hires", "candidates.view", "candidates_enabled", "months"),
    Topic("routines", "Routines", "routines.view", "routines_enabled", "days"),
    Topic("emails", "Email", "emails.log", None, "days"),
    Topic("finance", "Finance", "finance.view", "finance_enabled", "months"),
    Topic("team", "Team", "users.view", None, "days"),
)
BY_NAME = {topic.name: topic for topic in TOPICS}


@dataclass(frozen=True)
class ChartSpec:
    """A chart title and kind together with the plot data that draws it."""

    title: str
    kind: str
    plot: Plot | RowPlot


@dataclass(frozen=True)
class Report:
    """The facts, charts and raw statistics of one dashboard topic."""

    topic: Topic
    facts: list[tuple[str, str]]
    charts: list[ChartSpec]
    data: dict[str, object]


def allowed(topic: Topic, principal: Principal, policy: Policy) -> bool:
    """Check whether a principal may see a dashboard topic.

    Args:
        topic: The topic.
        principal: The caller.
        policy: The current workspace policy.

    Returns:
        True when the caller has the topic's permission and its module, if any, is enabled.
    """
    if not principal.can(topic.permission):
        return False
    return topic.module is None or bool(getattr(policy, topic.module))


def visible(principal: Principal, policy: Policy) -> list[Topic]:
    """Return the dashboard topics a principal may see.

    Args:
        principal: The caller.
        policy: The current workspace policy.

    Returns:
        The permitted topics in dashboard order.
    """
    return [topic for topic in TOPICS if allowed(topic, principal, policy)]


def named(pairs: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """Turn raw count keys into display labels.

    Underscores become spaces and the first letter is capitalised.

    Args:
        pairs: Pairs of raw key and count.

    Returns:
        Pairs of label and count.
    """
    return [(str(key).replace("_", " ").capitalize(), count) for key, count in pairs]


def figure(value: object, digits: int = 1) -> str:
    """Format a number with fixed decimals, or a placeholder when it is missing.

    Args:
        value: A number, a numeric string, or None.
        digits: Number of decimal places.

    Returns:
        The formatted number, or the text none yet for None.
    """
    return "none yet" if value is None else f"{float(str(value)):.{digits}f}"


def build_report(ctx: WebContext, name: str, window: int | None = None) -> Report:
    """Build the facts and charts for one dashboard topic.

    Args:
        ctx: The web context.
        name: Name of a topic in TOPICS.
        window: Look-back size in the topic's own unit (days or months), or None for the
            default.

    Returns:
        The report with its headline facts, charts and raw data.
    """
    topic = BY_NAME[name]
    tuning = ctx.settings.charts
    small = tuning.compact()
    stats = ctx.stats
    if name == "searches":
        data = stats.searches(window)
        days = [label[5:] for label, _ in data["per_day"]]
        return Report(
            topic,
            [
                ("Searches run", str(data["total"])),
                ("Average rating", figure(data["average_rating"])),
                ("Best rating", figure(data["best_rating"])),
            ],
            [
                ChartSpec(
                    f"Searches per day, last {data['days']} days",
                    "lines",
                    line_plot(
                        thin(days, tuning.max_labels),
                        [("Searches", [count for _, count in data["per_day"]])],
                        str,
                        small,
                        f"Searches per day over the last {data['days']} days",
                    ),
                ),
                ChartSpec(
                    "How searches ended",
                    "rows",
                    row_bars(named(data["by_state"]), str, small, "Searches by state"),
                ),
            ],
            data,
        )
    if name == "candidates":
        data = stats.candidates()
        return Report(
            topic,
            [
                ("Candidates", str(data["total"])),
                ("Hired before", str(data["hired_before"])),
                ("Average rating", figure(data["average_rating"])),
                ("Times researched", str(data["researched"])),
            ],
            [
                ChartSpec(
                    "Candidates by status",
                    "rows",
                    row_bars(named(data["by_status"]), str, small, "Candidates by status"),
                ),
                ChartSpec(
                    "Ratings",
                    "grouped",
                    grouped_bars(
                        [label for label, _ in data["ratings"]],
                        [("Candidates", [count for _, count in data["ratings"]])],
                        str,
                        small,
                        "Number of candidates per rating band",
                    ),
                ),
            ],
            data,
        )
    if name == "hires":
        data = stats.hires(window)
        return Report(
            topic,
            [
                ("Hires recorded", str(data["total"])),
                ("Average rating of hires", figure(data["average_rating"])),
                ("Rated", str(data["rated"])),
            ],
            [
                ChartSpec(
                    f"Hires per month, last {data['months']} months",
                    "grouped",
                    grouped_bars(
                        thin([label[2:] for label, _ in data["per_month"]], tuning.max_labels),
                        [("Hires", [count for _, count in data["per_month"]])],
                        str,
                        small,
                        "Hires recorded per month",
                    ),
                ),
                ChartSpec(
                    "Outcomes",
                    "rows",
                    row_bars(named(data["by_outcome"]), str, small, "Hires by outcome"),
                ),
            ],
            data,
        )
    if name == "routines":
        data = stats.routines(window)
        return Report(
            topic,
            [
                ("Runs", str(data["runs"])),
                ("Found someone", str(data["satisfied"])),
            ],
            [
                ChartSpec(
                    f"Runs per day, last {data['days']} days",
                    "lines",
                    line_plot(
                        thin([label[5:] for label, _ in data["runs_per_day"]], tuning.max_labels),
                        [("Runs", [count for _, count in data["runs_per_day"]])],
                        str,
                        small,
                        "Routine runs per day",
                    ),
                ),
                ChartSpec(
                    "Routines by status",
                    "rows",
                    row_bars(named(data["by_status"]), str, small, "Routines by status"),
                ),
            ],
            data,
        )
    if name == "emails":
        data = stats.emails(window)
        return Report(
            topic,
            [
                ("Sent", str(data["sent"])),
                ("Do not contact", str(data["do_not_contact"])),
            ],
            [
                ChartSpec(
                    "Messages sent per week",
                    "grouped",
                    grouped_bars(
                        thin([label[-2:] for label, _ in data["per_week"]], tuning.max_labels),
                        [("Sent", [count for _, count in data["per_week"]])],
                        str,
                        small,
                        "Messages sent per week",
                    ),
                ),
                ChartSpec(
                    "Results",
                    "rows",
                    row_bars(named(data["by_status"]), str, small, "Messages by result"),
                ),
            ],
            data,
        )
    if name == "finance":
        data = stats.finance(window)
        months = data["per_month"]
        unit = data["currency"]
        return Report(
            topic,
            [
                ("Revenue", ctx.finance.money(data["totals"]["revenue"])),
                ("Expenses", ctx.finance.money(data["totals"]["expense"])),
                ("Net", ctx.finance.money(data["totals"]["net"])),
            ],
            [
                ChartSpec(
                    f"Revenue and expenses per month in {unit}",
                    "grouped",
                    grouped_bars(
                        thin([item["month"][2:] for item in months], tuning.max_labels),
                        [
                            ("Revenue", [item["revenue"] for item in months]),
                            ("Expenses", [item["expense"] for item in months]),
                        ],
                        ctx.finance.compact,
                        small,
                        f"Revenue and expenses per month in {unit}",
                    ),
                ),
                ChartSpec(
                    "Expenses by category",
                    "rows",
                    row_bars(
                        data["expense_by_category"],
                        ctx.finance.compact,
                        small,
                        "Expenses by category",
                    ),
                ),
            ],
            data,
        )
    # The team topic is the fallthrough case after all other topic names.
    data = stats.team(window)
    return Report(
        topic,
        [("Accounts", str(data["total"]))],
        [
            ChartSpec(
                "People per role",
                "rows",
                row_bars(data["by_role"], str, small, "Accounts per role"),
            ),
            ChartSpec(
                f"Sign ins per day, last {data['days']} days",
                "lines",
                line_plot(
                    thin([label[5:] for label, _ in data["logins_per_day"]], tuning.max_labels),
                    [("Sign ins", [count for _, count in data["logins_per_day"]])],
                    str,
                    small,
                    "Sign ins per day",
                ),
            ),
        ],
        data,
    )


def window_of(request: Request) -> int | None:
    """Read the window query parameter of a request.

    Args:
        request: The incoming request.

    Returns:
        The window as a positive whole number below 100000, or None when it is missing or
        not valid.
    """
    raw = request.query_params.get("window", "")
    return int(raw) if raw.isdigit() and 0 < int(raw) < 100_000 else None


@requires("stats.view")
async def dashboard_page(request: Request) -> Response:
    """Show the dashboard with a report for each topic the caller may see.

    The window only applies to topics that support one.

    Args:
        request: The incoming request.

    Returns:
        The rendered dashboard page.
    """
    ctx = context_of(request)
    topics = visible(request.state.principal, ctx.policy.get())
    window = window_of(request)
    reports = [build_report(ctx, topic.name, window if topic.window else None) for topic in topics]
    return render(
        request,
        "dashboard.html",
        active="dashboard",
        reports=reports,
        window=window,
        choices=[(str(days), f"{days} days") for days in ctx.settings.stats.day_choices],
    )


@requires("stats.view")
async def chart_svg(request: Request) -> Response:
    """Download one dashboard chart as an SVG file.

    Each download is written to the audit log.

    Args:
        request: The incoming request with topic and index path parameters.

    Returns:
        The SVG as an attachment.

    Raises:
        HTTPException: 404 when the topic is unknown or not permitted, or the chart index
            is not valid.
    """
    ctx = context_of(request)
    name = request.path_params["topic"]
    topic = BY_NAME.get(name)
    # A topic the caller may not see gets the same 404 as one that does not exist.
    if topic is None or not allowed(topic, request.state.principal, ctx.policy.get()):
        raise HTTPException(404)
    index = request.path_params["index"]
    report = build_report(ctx, name, window_of(request))
    if not index.isdigit() or int(index) >= len(report.charts):
        raise HTTPException(404)
    spec = report.charts[int(index)]
    tuning = ctx.settings.charts
    body = (
        rows_svg(spec.plot, tuning)
        if isinstance(spec.plot, RowPlot)
        else plot_svg(spec.plot, tuning)
    )
    audit(request, "stats.chart", target=f"{name}/{index}")
    return Response(
        body,
        media_type="image/svg+xml",
        headers={"Content-Disposition": f'attachment; filename="{name}-{index}.svg"'},
    )
