# src/agent/web/security.py
from starlette.types import (
    ASGIApp,
    Message,
    Receive,
    Scope,
    Send,
)
from starlette.datastructures import (
    Headers,
    MutableHeaders,
)
from starlette.responses import PlainTextResponse
from agent.web.settings import WebSettings
from collections.abc import Callable
from dataclasses import dataclass
from collections import deque
from typing import Any

import hashlib
import logging
import secrets
import hmac
import time
import re

logger = logging.getLogger(__name__)
STATIC_PREFIX = "/static/"
ANONYMOUS = "anonymous"
SECOND = "second"
ENROLL = "enroll"
FULL = "full"
SECRET_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{20,}$")


def new_nonce(size: int) -> str:
    """Return a random URL-safe nonce.

    Args:
        size: number of random bytes to draw.

    Returns:
        The nonce as a URL-safe text token.
    """
    return secrets.token_urlsafe(size)


def masked_path(path: str) -> str:
    """Return a request path with secret-looking segments hidden.

    Any segment of 20 or more URL-safe token characters is replaced by an asterisk, so
    invite or login tokens do not reach the logs. The result is cut to 200 characters.

    Args:
        path: URL path of the request.

    Returns:
        The masked path, at most 200 characters long.
    """
    segments = [("*" if SECRET_SEGMENT.fullmatch(part) else part) for part in path.split("/")]
    return "/".join(segments)[:200]


def content_security_policy(nonce: str, secure: bool) -> str:
    """Build the Content-Security-Policy header value.

    Scripts run only with the given nonce, everything else is limited to the same
    origin, and framing, plugins and base URLs are blocked.

    Args:
        nonce: per-request nonce that inline scripts must carry.
        secure: whether to add the upgrade-insecure-requests directive.

    Returns:
        The policy string with directives joined by semicolons.
    """
    directives = [
        "default-src 'none'",
        f"script-src 'nonce-{nonce}'",
        "style-src 'self'",
        "img-src 'self' data:",
        "media-src 'self' blob:",
        "connect-src 'self'",
        "form-action 'self'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "object-src 'none'",
    ]
    if secure:
        directives.append("upgrade-insecure-requests")
    return "; ".join(directives)


def security_headers(nonce: str, web: WebSettings, static: bool) -> dict[str, str]:
    """Return the security headers to add to a response.

    Args:
        nonce: per-request nonce used in the Content-Security-Policy.
        web: web settings supplying cookie, HSTS, permissions and robots values.
        static: whether the response is a static asset; dynamic responses also get
            no-store cache headers.

    Returns:
        A mapping of header names to values.
    """
    headers = {
        "Content-Security-Policy": content_security_policy(nonce, web.cookie_secure),
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "same-origin",
        "Permissions-Policy": web.permissions_policy,
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
        "X-Permitted-Cross-Domain-Policies": "none",
        "X-Robots-Tag": web.robots_tag,
    }
    if not static:
        headers["Cache-Control"] = "no-store"
        headers["Pragma"] = "no-cache"
    if web.cookie_secure:
        headers["Strict-Transport-Security"] = (
            f"max-age={web.hsts_max_age_seconds}; includeSubDomains"
        )
    return headers


class SecurityHeadersMiddleware:
    """ASGI middleware that adds security headers and a CSP nonce to every response.

    Unhandled exceptions are logged with a masked path and answered with a plain 500
    response when the response has not started yet. The Server header is removed.
    """

    def __init__(self, app: ASGIApp, web: WebSettings):
        """Initialize the middleware.

        Args:
            app: the wrapped ASGI application.
            web: web settings used to build the headers and nonce.
        """
        self.app = app
        self.web = web

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve one ASGI request with security headers applied.

        Non-HTTP scopes pass straight through. For HTTP, a fresh nonce is stored in the
        request state so templates can use it.

        Args:
            scope: ASGI connection scope.
            receive: ASGI receive callable.
            send: ASGI send callable.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        nonce = new_nonce(self.web.nonce_bytes)
        scope.setdefault("state", {})["nonce"] = nonce
        static = scope["path"].startswith(STATIC_PREFIX)
        started = False

        async def send_with_headers(message: Message) -> None:
            """Add the security headers to the response start message and forward it.

            Args:
                message: ASGI message about to be sent.
            """
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = MutableHeaders(scope=message)
                for name, value in security_headers(nonce, self.web, static).items():
                    headers[name] = value
                if "server" in headers:
                    del headers["server"]
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception as error:
            logger.error(
                "unhandled %s while serving %s", type(error).__name__, masked_path(scope["path"])
            )
            if started:
                raise
            response = PlainTextResponse("Internal server error", status_code=500)
            await response(scope, receive, send_with_headers)


class BodyTooLarge(Exception):
    """Raised internally when a request body exceeds its byte limit."""

    pass


class BodyLimitMiddleware:
    """ASGI middleware that rejects request bodies above a byte limit.

    The declared Content-Length is checked first, and the bytes actually received are
    counted as well, so a missing or false header cannot bypass the limit.
    """

    def __init__(self, app: ASGIApp, max_bytes: int, overrides: dict[str, int] | None = None):
        """Initialize the middleware.

        Args:
            app: the wrapped ASGI application.
            max_bytes: default maximum body size in bytes.
            overrides: maximum body size in bytes for exact request paths.
        """
        self.app = app
        self.max_bytes = max_bytes
        self.overrides = overrides or {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve one ASGI request, answering 413 when the body is too large.

        Non-HTTP scopes pass straight through. If the limit is hit after the response has
        started, the exception is re-raised instead of sending a second response.

        Args:
            scope: ASGI connection scope.
            receive: ASGI receive callable.
            send: ASGI send callable.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.overrides.get(scope["path"], self.max_bytes)
        declared = Headers(scope=scope).get("content-length")
        too_large = PlainTextResponse("Request body too large", status_code=413)
        # Reject a malformed or oversized declared length early. Streamed bytes are still counted
        # below because the header may be absent or false.
        if declared is not None and (not declared.isdigit() or int(declared) > limit):
            await too_large(scope, receive, send)
            return
        received = 0
        started = False

        async def limited_receive() -> Message:
            """Receive the next message and enforce the body size limit.

            Returns:
                The ASGI message from the wrapped receive callable.

            Raises:
                BodyTooLarge: when the bytes received so far exceed the limit.
            """
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise BodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            """Forward a message and remember whether the response has started.

            Args:
                message: ASGI message to send.
            """
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except BodyTooLarge:
            if started:
                raise
            await too_large(scope, receive, send)


@dataclass
class Session:
    """A web session with its CSRF token, sign-in stage and client details.

    Timestamps come from the store clock, in seconds. A session counts as
    authenticated only at the full stage, not while sign-in is partly done.
    """

    id: str
    csrf_token: str
    created: float
    last_seen: float
    user_id: str | None = None
    version: int = 0
    stage: str = ANONYMOUS
    pending: str = ""
    attempts: int = 0
    ip: str = ""
    agent: str = ""

    @property
    def authenticated(self) -> bool:
        """Return whether the session has completed the full sign-in."""
        return self.stage == FULL


@dataclass(frozen=True)
class SessionInfo:
    """A read-only summary of a session that is safe to show to its owner.

    It carries a hashed handle instead of the real session id.
    """

    handle: str
    ip: str
    agent: str
    age_seconds: float
    idle_seconds: float
    current: bool


def handle_of(session_id: str) -> str:
    """Return a short non-reversible handle for a session id.

    Args:
        session_id: the secret session id.

    Returns:
        The first 16 hex characters of the SHA-256 digest of the id.
    """
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]


class SessionStore:
    """In-memory store of web sessions with expiry and size caps.

    Sessions expire after an idle period or a maximum age. Nothing is persisted, so
    all sessions are lost on restart and are not shared between processes.
    """

    def __init__(self, web: WebSettings, clock: Callable[[], float] = time.monotonic):
        """Initialize the store from the web settings.

        Args:
            web: web settings with the idle, maximum age, count and id size limits.
            clock: function returning the current time in seconds.
        """
        self._idle = web.session_idle_seconds
        self._max_age = web.session_max_seconds
        self._max_sessions = web.max_sessions
        self._per_user = web.max_sessions_per_user
        self._id_bytes = web.session_id_bytes
        self._clock = clock
        self._sessions: dict[str, Session] = {}

    def _expired(self, session: Session, now: float) -> bool:
        """Check whether a session passed its maximum age or idle limit.

        Args:
            session: session to check.
            now: current time in seconds.

        Returns:
            True when the session is expired.
        """
        return now - session.created > self._max_age or now - session.last_seen > self._idle

    def create(self, authenticated: bool = False, **fields: Any) -> Session:
        """Create and store a new session with a fresh id and CSRF token.

        Expired sessions are purged first. When the store is full the least recently seen
        unauthenticated session is evicted, or the least recently seen of all if there is
        none. A user's oldest sessions are dropped so the per-user cap holds.

        Args:
            authenticated: start at the full stage; otherwise the stage field or anonymous.
            **fields: extra Session fields such as user_id, version, ip and agent.

        Returns:
            The new session.
        """
        now = self._clock()
        for session_id in [s.id for s in self._sessions.values() if self._expired(s, now)]:
            del self._sessions[session_id]
        while len(self._sessions) >= self._max_sessions:
            anonymous = [s for s in self._sessions.values() if not s.authenticated]
            victim = min(anonymous or self._sessions.values(), key=lambda s: s.last_seen)
            del self._sessions[victim.id]
        owner = fields.get("user_id")
        if owner:
            mine = sorted(
                (s for s in self._sessions.values() if s.user_id == owner),
                key=lambda s: s.last_seen,
            )
            # Drop enough of the oldest sessions that adding the new one keeps the user at or under
            # the per-user cap.
            for old in mine[: max(0, len(mine) - self._per_user + 1)]:
                del self._sessions[old.id]
        session = Session(
            id=secrets.token_urlsafe(self._id_bytes),
            csrf_token=secrets.token_urlsafe(self._id_bytes),
            created=now,
            last_seen=now,
            stage=FULL if authenticated else fields.pop("stage", ANONYMOUS),
            **fields,
        )
        self._sessions[session.id] = session
        return session

    def get(self, session_id: str | None) -> Session | None:
        """Return the live session for an id and refresh its last-seen time.

        Args:
            session_id: session id from the cookie, if any.

        Returns:
            The session, or None when the id is empty, unknown or expired.
        """
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is None:
            return None
        now = self._clock()
        if self._expired(session, now):
            del self._sessions[session_id]
            return None
        session.last_seen = now
        return session

    def destroy(self, session_id: str) -> None:
        """Remove a session if it exists.

        Args:
            session_id: id of the session to remove.
        """
        self._sessions.pop(session_id, None)

    def rotate(self, session: Session, **fields: Any) -> Session:
        """Replace a session with a new one that has a new id and CSRF token.

        The client address and user agent are carried over.

        Args:
            session: session to replace.
            **fields: Session fields for the new session, such as user_id and stage.

        Returns:
            The new session.
        """
        self.destroy(session.id)
        return self.create(ip=session.ip, agent=session.agent, **fields)

    def of_user(self, user_id: str) -> list[Session]:
        """Return the unexpired, fully authenticated sessions of a user.

        Args:
            user_id: id of the user.

        Returns:
            The matching sessions.
        """
        now = self._clock()
        found = [s for s in self._sessions.values() if s.user_id == user_id and s.authenticated]
        return [s for s in found if not self._expired(s, now)]

    def listing(self, user_id: str, current_id: str | None = None) -> list[SessionInfo]:
        """Return display information for a user's sessions, newest first.

        Args:
            user_id: id of the user.
            current_id: id of the session making the request, flagged as current.

        Returns:
            One SessionInfo per live session, identified by handle only.
        """
        now = self._clock()
        return [
            SessionInfo(
                handle=handle_of(session.id),
                ip=session.ip,
                agent=session.agent,
                age_seconds=now - session.created,
                idle_seconds=now - session.last_seen,
                current=session.id == current_id,
            )
            for session in sorted(self.of_user(user_id), key=lambda s: s.created, reverse=True)
        ]

    def destroy_handle(self, user_id: str, handle: str, keep: str | None = None) -> int:
        """Destroy a user's session identified by its handle.

        The handle is compared in constant time and only the user's own sessions can
        match.

        Args:
            user_id: id of the owner.
            handle: handle as returned by handle_of.
            keep: session id that must not be destroyed.

        Returns:
            The number of sessions destroyed.
        """
        doomed = [
            s.id
            for s in self._sessions.values()
            if s.user_id == user_id
            and s.id != keep
            and hmac.compare_digest(handle_of(s.id).encode(), handle.encode())
        ]
        for session_id in doomed:
            del self._sessions[session_id]
        return len(doomed)

    def destroy_user(self, user_id: str, keep: str | None = None) -> int:
        """Destroy every session of a user.

        Args:
            user_id: id of the user.
            keep: session id that must not be destroyed.

        Returns:
            The number of sessions destroyed.
        """
        doomed = [s.id for s in self._sessions.values() if s.user_id == user_id and s.id != keep]
        for session_id in doomed:
            del self._sessions[session_id]
        return len(doomed)


class RateLimiter:
    """A sliding-window rate limiter keyed by string.

    Events are kept in memory per process. The number of tracked keys is bounded, and
    the oldest keys are dropped when the bound is reached.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ):
        """Initialize the limiter.

        Args:
            limit: maximum events per key inside the window.
            window_seconds: window length in seconds.
            clock: function returning the current time in seconds.
            max_keys: maximum number of keys tracked at once.
        """
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._max_keys = max_keys
        self._events: dict[str, deque[float]] = {}
        self._swept = clock()

    def _make_room(self, now: float) -> None:
        """Free space for a new key by sweeping stale keys and dropping the oldest.

        Args:
            now: current time in seconds.
        """
        if now - self._swept >= self._window:
            self._events = {
                k: v for k, v in self._events.items() if v and now - v[-1] < self._window
            }
            self._swept = now
        while len(self._events) >= self._max_keys:
            # Dicts keep insertion order, so this evicts the oldest key.
            del self._events[next(iter(self._events))]

    def allow(self, key: str) -> bool:
        """Record an event for a key unless it is already at the limit.

        Args:
            key: identifier being limited, such as a client address.

        Returns:
            True if the event was allowed and counted, False if the limit was reached.
        """
        now = self._clock()
        if len(self._events) >= self._max_keys and key not in self._events:
            self._make_room(now)
        events = self._events.setdefault(key, deque())
        while events and now - events[0] >= self._window:
            events.popleft()
        if len(events) >= self._limit:
            return False
        events.append(now)
        return True

    def blocked(self, key: str) -> bool:
        """Check whether a key is at its limit without recording an event.

        Args:
            key: identifier being limited.

        Returns:
            True when the key has reached the limit inside the window.
        """
        events = self._events.get(key)
        if events is None:
            return False
        now = self._clock()
        while events and now - events[0] >= self._window:
            events.popleft()
        return len(events) >= self._limit

    def reset(self, key: str) -> None:
        """Forget all recorded events for a key.

        Args:
            key: identifier to clear.
        """
        self._events.pop(key, None)


def tokens_match(expected: str, supplied: str) -> bool:
    """Compare two secret tokens in constant time.

    Args:
        expected: the known token.
        supplied: the token provided by the client.

    Returns:
        True when both tokens are equal.
    """
    return hmac.compare_digest(expected.encode(), supplied.encode())
