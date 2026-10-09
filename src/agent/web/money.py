# src/agent/web/money.py
from agent.web.common import (
    audit,
    protected_form,
    render,
    requires,
    text,
)
from agent.web.charts import (
    grouped_bars,
    row_bars,
)
from agent.web.context import (
    context_of,
    WebContext,
)
from starlette.responses import (
    RedirectResponse,
    Response,
)
from starlette.exceptions import HTTPException
from starlette.datastructures import FormData
from starlette.requests import Request
from agent.errors import AgentError
from agent.finance import MONTH

import csv
import io

MODULE = "finance_enabled"
CSV_UNSAFE = ("=", "+", "-", "@", "\t", "\r")
NOTES = {
    "saved": "Saved.",
    "added": "The entry was added.",
    "removed": "Removed.",
    "category": "The category was added.",
    "currency": "The currency was changed.",
}


def safe_cell(value: object) -> str:
    """Return a CSV cell value that spreadsheets will not run as a formula.

    Values starting with a formula or control character get a leading apostrophe.

    Args:
        value: The value to write.

    Returns:
        The cell text, prefixed with an apostrophe when it starts unsafely.
    """
    cell = str(value)
    return f"'{cell}" if cell.startswith(CSV_UNSAFE) else cell


def short(ctx: WebContext, minor: int) -> str:
    """Format an amount in minor currency units in compact form.

    Args:
        ctx: The web context.
        minor: The amount in minor units, such as cents.

    Returns:
        The short text used on chart labels.
    """
    return ctx.finance.compact(minor)


def finance_view(
    request: Request,
    status: int = 200,
    *,
    errors: list[str] | None = None,
    values: dict[str, str] | None = None,
) -> Response:
    """Render the finance page with entries, charts and totals.

    The kind, month, category and page query values are validated and fall back
    to no filter or the first page. Charts and totals cover all months with data.

    Args:
        request: The incoming request.
        status: The HTTP status code of the response.
        errors: Error messages to show.
        values: Form values to refill.

    Returns:
        The rendered page.
    """
    ctx = context_of(request)
    tuning = ctx.settings.finance
    params = request.query_params
    kind = params.get("kind", "")
    kind = kind if kind in tuning.kinds else ""
    month = params.get("month", "")
    month = month if MONTH.fullmatch(month) else ""
    category = params.get("category", "")[:40]
    raw_page = params.get("page", "1")
    page_number = int(raw_page) if raw_page.isdigit() and 0 < int(raw_page) < 100_000 else 1
    found = ctx.finance.page(
        kind=kind, category_id=category, month=month, offset=(page_number - 1) * tuning.page_size
    )
    months = ctx.finance.monthly()
    unit = ctx.finance.currency()
    first, last = months[0].month, months[-1].month
    totals = ctx.finance.totals(first, last)
    chart = grouped_bars(
        [item.month[2:] for item in months],
        [
            ("Revenue", [item.revenue for item in months]),
            ("Expenses", [item.expense for item in months]),
        ],
        lambda value: short(ctx, value),
        ctx.settings.charts,
        f"Revenue and expenses per month in {unit}, {first} to {last}",
    )
    breakdowns = []
    for label, name in (("Revenue", "revenue"), ("Expenses", "expense")):
        items = ctx.finance.by_category(name, first, last)
        breakdowns.append(
            (
                label,
                row_bars(
                    [(item.name, item.total) for item in items],
                    lambda value: short(ctx, value),
                    ctx.settings.charts.compact(),
                    f"{label} by category in {unit}, {first} to {last}",
                )
                if items
                else None,
            )
        )
    note = NOTES.get(params.get("done", ""))
    categories = ctx.finance.categories()
    return render(
        request,
        "finance.html",
        status,
        active="finance",
        found=found,
        page_number=page_number,
        # Ceiling division, with at least one page when there are no entries.
        pages=max(1, -(-found.total // found.size)),
        filters={"kind": kind, "month": month, "category": category},
        chart=chart,
        breakdowns=breakdowns,
        months=months,
        totals=totals,
        net=totals["revenue"] - totals["expense"],
        range=(first, last),
        categories=categories,
        category_options=[
            ("", "Any category"),
            *((item.id, f"{item.name} ({item.kind})") for item in categories),
        ],
        entry_categories=[
            ("", "No category"),
            *((item.id, f"{item.name} ({item.kind})") for item in categories),
        ],
        kind_options=[
            ("", "Revenue and expenses"),
            ("revenue", "Revenue"),
            ("expense", "Expenses"),
        ],
        entry_kinds=[("revenue", "Revenue"), ("expense", "Expense")],
        month_options=[
            ("", "Any month"),
            *((item, item) for item in ctx.finance.months_with_data()),
        ],
        money=ctx.finance.money,
        plain=ctx.finance.plain,
        currency=unit,
        today=ctx.finance.today().isoformat(),
        errors=errors or [],
        notes=[note] if note else [],
        values=values or {},
        can_export=request.state.principal.can("data.export"),
    )


@requires("finance.view", MODULE)
async def finance_page(request: Request) -> Response:
    """Show the finance page.

    Args:
        request: The incoming request.

    Returns:
        The rendered page.
    """
    return finance_view(request)


def entry_form(ctx: WebContext, form: FormData) -> dict[str, str]:
    """Read the entry form fields as trimmed, length-limited text.

    Args:
        ctx: The web context.
        form: The submitted form.

    Returns:
        The day, kind, amount, category and note values.
    """
    tuning = ctx.settings.finance
    return {
        "day": text(form, "day", 10),
        "kind": text(form, "kind", 10),
        "amount": text(form, "amount", 24),
        "category": text(form, "category", 40),
        "note": text(form, "note", tuning.note_chars),
    }


@requires("finance.edit", MODULE)
async def entry_create(request: Request) -> Response:
    """Add a revenue or expense entry from the submitted form.

    The form is checked for forgery protection and the action is audited.

    Args:
        request: The incoming form submission.

    Returns:
        A redirect to the finance page, or the page with status 400 and the problem.
    """
    ctx = context_of(request)
    form = await protected_form(request, request.state.session)
    try:
        values = entry_form(ctx, form)
    finally:
        await form.close()
    try:
        entry = ctx.finance.add_entry(
            values["day"],
            values["kind"],
            values["amount"],
            category_id=values["category"] or None,
            note=values["note"],
            actor=request.state.principal.name,
        )
    except AgentError as error:
        return finance_view(request, 400, errors=[str(error)], values=values)
    audit(request, "finance.add", target=entry.id)
    return RedirectResponse("/finance?done=added", status_code=303)


@requires("finance.edit", MODULE)
async def entry_update(request: Request) -> Response:
    """Change an existing entry from the submitted form.

    The form is checked for forgery protection and the action is audited.

    Args:
        request: The incoming form submission with an entry_id path parameter.

    Returns:
        A redirect to the finance page, or the page with status 400 and the problem.
    """
    ctx = context_of(request)
    form = await protected_form(request, request.state.session)
    try:
        values = entry_form(ctx, form)
    finally:
        await form.close()
    try:
        ctx.finance.update_entry(
            request.path_params["entry_id"],
            day=values["day"],
            kind=values["kind"],
            amount=values["amount"],
            category_id=values["category"] or None,
            note=values["note"],
        )
    except AgentError as error:
        return finance_view(request, 400, errors=[str(error)])
    audit(request, "finance.update", target=request.path_params["entry_id"])
    return RedirectResponse("/finance?done=saved", status_code=303)


@requires("finance.edit", MODULE)
async def entry_delete(request: Request) -> Response:
    """Delete an entry.

    The deletion is audited only when an entry was actually removed.

    Args:
        request: The incoming form submission with an entry_id path parameter.

    Returns:
        A redirect to the finance page.
    """
    ctx = context_of(request)
    form = await protected_form(request, request.state.session)
    await form.close()
    if ctx.finance.delete_entry(request.path_params["entry_id"]):
        audit(request, "finance.delete", target=request.path_params["entry_id"])
    return RedirectResponse("/finance?done=removed", status_code=303)


@requires("finance.edit", MODULE)
async def category_create(request: Request) -> Response:
    """Add a finance category from the submitted form.

    Args:
        request: The incoming form submission.

    Returns:
        A redirect to the categories section, or the page with status 400 and the
        problem.
    """
    ctx = context_of(request)
    form = await protected_form(request, request.state.session)
    try:
        name = text(form, "name", ctx.settings.finance.name_chars)
        kind = text(form, "kind", 10)
    finally:
        await form.close()
    try:
        ctx.finance.add_category(name, kind)
    except AgentError as error:
        return finance_view(request, 400, errors=[str(error)])
    return RedirectResponse("/finance?done=category#categories", status_code=303)


@requires("finance.edit", MODULE)
async def category_update(request: Request) -> Response:
    """Rename a finance category from the submitted form.

    Args:
        request: The incoming form submission with a category_id path parameter.

    Returns:
        A redirect to the categories section, or the page with status 400 and the
        problem.
    """
    ctx = context_of(request)
    form = await protected_form(request, request.state.session)
    try:
        name = text(form, "name", ctx.settings.finance.name_chars)
    finally:
        await form.close()
    try:
        ctx.finance.rename_category(request.path_params["category_id"], name)
    except AgentError as error:
        return finance_view(request, 400, errors=[str(error)])
    return RedirectResponse("/finance?done=saved#categories", status_code=303)


@requires("finance.edit", MODULE)
async def category_delete(request: Request) -> Response:
    """Delete a finance category.

    The action is written to the audit log.

    Args:
        request: The incoming form submission with a category_id path parameter.

    Returns:
        A redirect to the categories section.
    """
    ctx = context_of(request)
    form = await protected_form(request, request.state.session)
    await form.close()
    ctx.finance.delete_category(request.path_params["category_id"])
    audit(request, "finance.category_delete", target=request.path_params["category_id"])
    return RedirectResponse("/finance?done=removed#categories", status_code=303)


@requires("finance.edit", MODULE)
async def currency_save(request: Request) -> Response:
    """Change the currency code used for all amounts.

    The code is written to the audit log in upper case.

    Args:
        request: The incoming form submission.

    Returns:
        A redirect to the finance page, or the page with status 400 and the problem.
    """
    ctx = context_of(request)
    form = await protected_form(request, request.state.session)
    try:
        code = text(form, "currency", 12)
    finally:
        await form.close()
    try:
        ctx.finance.set_currency(code)
    except AgentError as error:
        return finance_view(request, 400, errors=[str(error)])
    audit(request, "finance.currency", detail={"currency": code.upper()})
    return RedirectResponse("/finance?done=currency", status_code=303)


@requires("finance.view", MODULE)
async def finance_csv(request: Request) -> Response:
    """Download all finance entries as a CSV file.

    The caller also needs the data.export permission. Free-text columns are
    escaped against spreadsheet formula injection, and the export is audited with
    the row count.

    Args:
        request: The incoming request.

    Returns:
        A CSV attachment named finance.csv.

    Raises:
        HTTPException: With status 404 when the caller may not export data.
    """
    ctx = context_of(request)
    if not request.state.principal.can("data.export"):
        # Answer 404 so the export route is not revealed to users without the permission.
        raise HTTPException(404)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["date", "kind", "amount", "currency", "category", "note"])
    unit = ctx.finance.currency()
    for entry in ctx.finance.all_entries():
        writer.writerow(
            [
                entry.day,
                entry.kind,
                ctx.finance.plain(entry.amount),
                unit,
                # Free-text fields are escaped so a spreadsheet cannot run them as formulas.
                safe_cell(entry.category),
                safe_cell(entry.note),
            ]
        )
    audit(request, "finance.export", detail={"rows": ctx.finance.count()})
    return Response(
        buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="finance.csv"'},
    )
