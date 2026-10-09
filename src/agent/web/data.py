# src/agent/web/data.py
from agent.web.common import (
    audit,
    protected_form,
    render,
    requires,
    text,
)
from agent.portability import (
    BY_NAME,
    CSV_COLUMNS,
    Result,
)
from agent.web.context import (
    context_of,
    WebContext,
)
from starlette.exceptions import HTTPException
from starlette.responses import Response
from agent.errors import InvalidRequest
from starlette.requests import Request
from agent.forms import Dataset
from typing import Any

import json
import time


def modules_of(ctx: WebContext) -> Any:
    """Return a predicate that tells whether a workspace module is enabled.

    Args:
        ctx: The web context that holds the current module policy.

    Returns:
        A callable that takes a module name, or None, and returns a bool.
    """
    policy = ctx.policy.get()

    def allowed(module: str | None) -> bool:
        """Check whether a module is switched on in the policy.

        Args:
            module: The module name, or None for a dataset that belongs to no module.

        Returns:
            True when the module is None or its policy flag is set.
        """
        return module is None or bool(getattr(policy, module))

    return allowed


def exportable(ctx: WebContext, request: Request) -> list[Dataset]:
    """Return the datasets the signed-in user may export.

    Args:
        ctx: The web context with the portability service and the module policy.
        request: The request that carries the signed-in principal.

    Returns:
        The datasets the user may view and whose module is enabled.
    """
    return ctx.portability.available(request.state.principal.can, modules_of(ctx))


def importable(ctx: WebContext, request: Request) -> list[Dataset]:
    """Return the datasets the signed-in user may import.

    Args:
        ctx: The web context with the portability service and the module policy.
        request: The request that carries the signed-in principal.

    Returns:
        The datasets the user may edit and whose module is enabled.
    """
    return ctx.portability.importable(request.state.principal.can, modules_of(ctx))


@requires("data.export")
async def data_page(request: Request) -> Response:
    """Show the export page with the datasets the user may download.

    Args:
        request: The request; it needs the data.export permission.

    Returns:
        The rendered page.
    """
    ctx = context_of(request)
    return render(
        request,
        "admin_data.html",
        active="admin",
        section="export",
        datasets=exportable(ctx, request),
        csv_columns=CSV_COLUMNS,
    )


@requires("data.export")
async def export_json(request: Request) -> Response:
    """Download the chosen datasets as one JSON bundle.

    Names the user may not export are ignored. The export is written to the audit log.

    Args:
        request: The request; it needs the data.export permission and may repeat the
            datasets query parameter.

    Returns:
        A JSON attachment named with the current UTC date.

    Raises:
        HTTPException: with status 404 when no allowed dataset was chosen.
    """
    ctx = context_of(request)
    allowed = {item.name for item in exportable(ctx, request)}
    chosen = [name for name in request.query_params.getlist("datasets") if name in allowed]
    if not chosen:
        raise HTTPException(404)
    bundle = ctx.portability.bundle(chosen)
    audit(request, "data.export", detail={"datasets": chosen, "format": "json"})
    stamp = time.strftime("%Y%m%d", time.gmtime())
    return Response(
        json.dumps(bundle, ensure_ascii=False, indent=1),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="workspace-{stamp}.json"'},
    )


@requires("data.export")
async def export_csv(request: Request) -> Response:
    """Download one dataset as a CSV file.

    The export is written to the audit log.

    Args:
        request: The request; it needs the data.export permission and the dataset path
            parameter.

    Returns:
        A CSV attachment named after the dataset.

    Raises:
        HTTPException: with status 404 when the dataset is unknown, not allowed for the
            user, or has no CSV form.
    """
    ctx = context_of(request)
    name = request.path_params["dataset"]
    spec = BY_NAME.get(name)
    allowed = {item.name for item in exportable(ctx, request)}
    if spec is None or name not in allowed or name not in CSV_COLUMNS:
        raise HTTPException(404)
    body = ctx.portability.csv_text(name)
    audit(request, "data.export", detail={"datasets": [name], "format": "csv"})
    return Response(
        body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
    )


def import_view(
    request: Request,
    status: int = 200,
    *,
    errors: list[str] | None = None,
    result: Result | None = None,
    token: str = "",
    strategy: str = "skip",
    notes: list[str] | None = None,
) -> Response:
    """Render the import page, with an optional preview, errors or notes.

    Args:
        request: The current request.
        status: The HTTP status code of the response.
        errors: Error messages to show.
        result: The preview or outcome of an import to show.
        token: The preview token the confirm form sends back.
        strategy: The selected strategy, skip or update.
        notes: Informational messages to show.

    Returns:
        The rendered page.
    """
    ctx = context_of(request)
    return render(
        request,
        "admin_import.html",
        status,
        active="admin",
        section="import",
        datasets=importable(ctx, request),
        csv_options=[(item.name, item.label) for item in importable(ctx, request) if item.csv],
        strategies=[
            ("skip", "Add what is new, keep what exists"),
            ("update", "Add what is new and update what exists"),
        ],
        strategy=strategy,
        errors=errors or [],
        notes=notes or [],
        result=result,
        token=token,
        limit_mb=ctx.settings.portability.max_bytes // 1_000_000,
        minutes=ctx.settings.portability.stash_minutes,
    )


@requires("data.import")
async def import_page(request: Request) -> Response:
    """Show the empty import page.

    Args:
        request: The request; it needs the data.import permission.

    Returns:
        The rendered page.
    """
    return import_view(request)


@requires("data.import")
async def import_preview(request: Request) -> Response:
    """Read an uploaded file and show what an import would change.

    The form is checked for forgery protection. The file is parsed as CSV when its name
    ends in .csv and as JSON otherwise. Nothing is written: the records are checked in
    a dry run and held under a token for the confirm step.

    Args:
        request: The request; it needs the data.import permission.

    Returns:
        The import page with the preview, or with errors and status 400 when the file is
        missing, too large or invalid.
    """
    ctx = context_of(request)
    principal = request.state.principal
    tuning = ctx.settings.portability
    form = await protected_form(request, request.state.session)
    try:
        dataset = text(form, "dataset", 20)
        strategy = text(form, "strategy", 10)
        purpose = text(form, "purpose", 4) == "yes"
        upload = form.get("file")
        # Reading one byte over the limit detects an oversized file without loading all of it.
        raw = await upload.read(tuning.max_bytes + 1) if hasattr(upload, "read") else b""
        filename = getattr(upload, "filename", "") or "upload"
    finally:
        await form.close()
    if not raw:
        return import_view(request, 400, errors=["Choose a file first."], strategy=strategy)
    if len(raw) > tuning.max_bytes:
        return import_view(request, 400, errors=["That file is too large."], strategy=strategy)
    try:
        if filename.lower().endswith(".csv"):
            records = ctx.portability.parse_csv(dataset, raw)
        else:
            records = ctx.portability.parse_json(raw)
        token, result = ctx.portability.preview(
            records, strategy, principal.user_id, filename, principal.can, principal.name, purpose
        )
    except InvalidRequest as error:
        return import_view(request, 400, errors=[str(error)], strategy=strategy)
    audit(request, "data.import_preview", detail={"file": filename[:80], "datasets": list(records)})
    return import_view(request, result=result, token=token, strategy=strategy)


@requires("data.import")
async def import_confirm(request: Request) -> Response:
    """Apply an import that was previewed before.

    The import is written to the audit log with the created and updated counts.

    Args:
        request: The request; it needs the data.import permission and the preview token.

    Returns:
        The import page with the outcome, or with status 400 when the preview expired or
        the import was not applied.
    """
    ctx = context_of(request)
    principal = request.state.principal
    form = await protected_form(request, request.state.session)
    try:
        token = text(form, "token", 80)
    finally:
        await form.close()
    # The token is single use and tied to the user who made the preview; permissions are checked
    # again when applying.
    stash = ctx.portability.take(token, principal.user_id)
    if stash is None:
        return import_view(request, 400, errors=["That preview expired. Choose the file again."])
    result = ctx.portability.apply(
        stash.records,
        stash.strategy,
        principal.name,
        principal.can,
        False,
        stash.confirmed_purpose,
    )
    audit(
        request,
        "data.import",
        outcome="ok" if result.applied else "failed",
        detail={
            "file": stash.filename,
            "created": sum(item.created for item in result.outcomes),
            "updated": sum(item.updated for item in result.outcomes),
        },
    )
    if not result.applied:
        return import_view(request, 400, result=result, strategy=stash.strategy)
    return import_view(
        request,
        result=result,
        notes=["The import is done."],
        strategy=stash.strategy,
    )
