# src/agent/web/api_core.py
from agent.errors import (
    AgentError,
    ComplianceRefusal,
    InvalidRequest,
    RateLimitedError,
    UpstreamError,
    VoiceDisabled,
)
from agent.web.context import (
    client_key,
    context_of,
    WebContext,
)
from agent.web.jobs import (
    API_OWNER,
    TooManyJobs,
    WEB_OWNER,
)
from collections.abc import (
    Awaitable,
    Callable,
)
from starlette.responses import (
    JSONResponse,
    Response,
)
from agent.web.security import tokens_match
from agent.text import describe_errors
from starlette.requests import Request
from agent.accounts import Principal
from agent.permissions import expand
from pydantic import ValidationError
from starlette.routing import Route
from typing import Any

import functools

BASE = "/api/v1"
JSON_TYPE = "application/json"
SHARED_SOURCE = "shared"
Handler = Callable[[Request], Awaitable[Response]]
ENDPOINTS: list[dict[str, Any]] = []
ROUTES: list[Route] = []


def error_response(
    status: int, code: str, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    """Build a JSON error response in the API's standard shape.

    Args:
        status: HTTP status code.
        code: Short machine-readable error code.
        message: Human-readable explanation.
        headers: Optional extra response headers.

    Returns:
        A JSON response whose body is {"error": {"code": ..., "message": ...}}.
    """
    body = {"error": {"code": code, "message": message}}
    return JSONResponse(body, status_code=status, headers=headers)


def bearer(header: str | None) -> str | None:
    """Extract the token from an Authorization header.

    Args:
        header: Raw Authorization header value, or None.

    Returns:
        The bearer token, or None when the header is missing, uses another scheme
        or carries an empty token.
    """
    if header is None:
        return None
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def token_principal(ctx: WebContext) -> Principal:
    """Build the shared principal for the configured API token.

    The permissions come from the account role named by the api_role setting, or
    from the settings role definition when no such account role exists.

    Args:
        ctx: Shared web context.

    Returns:
        A principal marked with the shared source.
    """
    tuning = ctx.settings.auth
    role = ctx.accounts.role_named(tuning.api_role)
    spec = tuning.roles.get(tuning.api_role)
    granted = role.effective() if role else expand(list(spec.permissions) if spec else [])
    return Principal(
        user_id=ctx.settings.api.shared_user,
        name=ctx.settings.api.shared_name,
        role=tuning.api_role,
        permissions=granted,
        version=0,
        source=SHARED_SOURCE,
    )


def owner_of(principal: Principal) -> str:
    """Return the job owner label for a principal.

    Args:
        principal: The authenticated caller.

    Returns:
        The API owner for the shared token, otherwise the web owner.
    """
    return API_OWNER if principal.source == SHARED_SOURCE else WEB_OWNER


def unauthorized(ctx: WebContext, address: str) -> JSONResponse:
    """Record a failed authentication and build the 401 response.

    The attempt counts against the per-address failure limiter.

    Args:
        ctx: Shared web context.
        address: Client address key used for rate limiting.

    Returns:
        A 401 JSON error with a WWW-Authenticate header.
    """
    ctx.api_failure_limiter.allow(address)
    return error_response(
        401,
        "unauthorized",
        "Send an API key or the API token as 'Authorization: Bearer <token>'.",
        {"WWW-Authenticate": "Bearer"},
    )


def who(ctx: WebContext, supplied: str) -> Principal | None:
    """Resolve a bearer token to the principal it belongs to.

    Tokens with the API key prefix are checked as account API keys, and only when
    the policy allows keys. Any other token is compared in constant time with the
    configured shared API token.

    Args:
        ctx: Shared web context.
        supplied: The bearer token sent by the client.

    Returns:
        The matching principal, or None when the token is not valid.
    """
    prefix = f"{ctx.settings.auth.api_key_prefix}_"
    if supplied.startswith(prefix):
        if not ctx.policy.get().allow_api_keys:
            return None
        found = ctx.accounts.authenticate_key(supplied)
        return found[1] if found else None
    configured = ctx.web.api_token
    if configured is not None and tokens_match(configured.get_secret_value(), supplied):
        return token_principal(ctx)
    return None


def visible(ctx: WebContext, principal: Principal) -> list[dict[str, Any]]:
    """List the API endpoints that a principal may call.

    Endpoints are dropped when the principal lacks the permission or the workspace
    module behind them is switched off.

    Args:
        ctx: Shared web context.
        principal: The authenticated caller.

    Returns:
        Endpoint descriptions from the registry.
    """
    policy = ctx.policy.get()
    return [
        item
        for item in ENDPOINTS
        if principal.can(item["permission"])
        and (item["module"] is None or bool(getattr(policy, item["module"])))
    ]


def api(
    method: str,
    path: str,
    summary: str,
    permission: str,
    *,
    body: str | None = None,
    module: str | None = None,
    tag: str = "General",
) -> Callable[[Handler], Handler]:
    """Register an API endpoint and wrap its handler with access control.

    The wrapper rejects calls when the API is disabled, rate limits by address and
    by principal, authenticates the bearer token, checks the api.use permission,
    the endpoint permission and the module switch, and records denied calls and
    successful writes in the audit log. Project errors are mapped to JSON error
    responses. The endpoint is added to ENDPOINTS and ROUTES at decoration time.

    Args:
        method: HTTP method.
        path: Route path relative to the API base.
        summary: One-line description shown in the API documentation.
        permission: Permission the caller must hold.
        body: Name of the request body model, if any.
        module: Policy flag that must be on for the endpoint to exist.
        tag: Documentation group.

    Returns:
        A decorator that returns the wrapped handler.
    """

    def decorate(handler: Handler) -> Handler:
        """Wrap a handler, register its route and return the wrapped handler.

        Args:
            handler: The async request handler to protect.

        Returns:
            The wrapped handler.
        """

        @functools.wraps(handler)
        async def wrapper(request: Request) -> Response:
            """Authenticate and authorize a request, then run the handler.

            Args:
                request: The incoming request.

            Returns:
                The handler response, or a JSON error for failed checks and for project
                errors raised by the handler.
            """
            ctx = context_of(request)
            if ctx.web.api_token is None and not ctx.policy.get().allow_api_keys:
                return error_response(
                    404, "api_disabled", "The API is disabled; set WEB_API_TOKEN or allow API keys."
                )
            address = client_key(request)
            if not ctx.api_limiter.allow(address):
                return error_response(429, "rate_limited", "Too many requests; slow down.")
            supplied = bearer(request.headers.get("authorization"))
            principal = who(ctx, supplied) if supplied else None
            if principal is None:
                # A client with too many failed attempts gets 429 instead of 401 so guessing tokens
                # is throttled.
                if ctx.api_failure_limiter.blocked(address):
                    return error_response(429, "locked_out", "Too many failed attempts; wait.")
                return unauthorized(ctx, address)
            if not principal.can("api.use"):
                return error_response(403, "forbidden", "This credential may not use the API.")
            if not ctx.api_limiter.allow(f"principal:{principal.user_id}:{principal.key_id}"):
                return error_response(429, "rate_limited", "Too many requests; slow down.")
            if not principal.can(permission):
                ctx.accounts.log(
                    "api.denied",
                    actor=principal,
                    target=f"{method} {path}"[:100],
                    outcome="denied",
                    ip=address,
                )
                return error_response(403, "forbidden", "This credential may not do that.")
            if module is not None and not getattr(ctx.policy.get(), module):
                return error_response(404, "not_found", "That part of the workspace is off.")
            request.state.principal = principal
            try:
                response = await handler(request)
                # Only successful writes are audit logged here; denied calls are logged earlier.
                if method != "GET" and response.status_code < 400:
                    ctx.accounts.log(
                        "api.write",
                        actor=principal,
                        target=f"{method} {path}"[:100],
                        ip=address,
                        detail={"key": principal.key_id or "shared"},
                    )
                return response
            except ComplianceRefusal as error:
                return error_response(403, "compliance_refused", str(error))
            except TooManyJobs as error:
                return error_response(429, "too_many_jobs", str(error))
            except InvalidRequest as error:
                return error_response(422, "invalid_request", str(error))
            except VoiceDisabled as error:
                return error_response(404, "voice_disabled", str(error))
            except RateLimitedError as error:
                return error_response(429, "rate_limited", str(error))
            except UpstreamError as error:
                return error_response(502, "upstream_error", str(error))
            except ValidationError as error:
                return error_response(422, "validation_error", describe_errors(error))
            except AgentError as error:
                return error_response(400, "request_failed", str(error))

        ENDPOINTS.append(
            {
                "method": method,
                "path": f"{BASE}{path}",
                "summary": summary,
                "body": body,
                "permission": permission,
                "module": module,
                "tag": tag,
            }
        )
        ROUTES.append(Route(f"{BASE}{path}", wrapper, methods=[method]))
        return wrapper

    return decorate


async def json_body(request: Request) -> Any:
    """Parse the request body as JSON.

    Args:
        request: The incoming request.

    Returns:
        The decoded JSON value.

    Raises:
        InvalidRequest: when the content type is not application/json or the body
            is not valid JSON.
    """
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != JSON_TYPE:
        raise InvalidRequest("Send the body as application/json.")
    try:
        return await request.json()
    except ValueError as error:
        raise InvalidRequest("The body is not valid JSON.") from error


def page_of(request: Request) -> tuple[int, int]:
    """Read the page number and page size from the query string.

    Invalid values fall back to page 1 and the default size. The size is capped at
    the configured maximum.

    Args:
        request: The incoming request.

    Returns:
        A tuple of page number and page size.
    """
    ctx = context_of(request)
    tuning = ctx.settings.api
    raw_page = request.query_params.get("page", "")
    raw_size = request.query_params.get("size", "")
    page = int(raw_page) if raw_page.isdigit() and 0 < int(raw_page) < 1_000_000 else 1
    size = int(raw_size) if raw_size.isdigit() and int(raw_size) > 0 else tuning.default_page_size
    return page, min(size, tuning.max_page_size)


def paged(items: list[Any], total: int, page: int, size: int, path: str) -> dict[str, Any]:
    """Wrap a list of items in a paging envelope.

    Args:
        items: Items of the current page.
        total: Total number of matching items.
        page: Current page number, starting at 1.
        size: Page size.
        path: Endpoint path used to build the next page link.

    Returns:
        A dict with items, total, page, size and a next link or None.
    """
    more = page * size < total
    return {
        "items": items,
        "total": total,
        "page": page,
        "size": size,
        "next": f"{BASE}{path}?page={page + 1}&size={size}" if more else None,
    }
