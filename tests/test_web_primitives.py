# tests/test_web_primitives.py
from agent.web.security import (
    BodyLimitMiddleware,
    BodyTooLarge,
    content_security_policy,
    new_nonce,
    RateLimiter,
    security_headers,
    SecurityHeadersMiddleware,
    SessionStore,
    tokens_match,
)
from agent.web.settings import (
    load_web_settings,
    WebSettings,
)
from agent.errors import ConfigError
from pydantic import ValidationError

import pytest

TOKEN = "x" * 24


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def web(**values):
    return WebSettings(_env_file=None, access_token=TOKEN, **values)


def test_nonces_are_unique_and_long():
    nonces = {new_nonce(18) for _ in range(200)}
    assert len(nonces) == 200
    assert all(len(nonce) >= 24 for nonce in nonces)


def test_the_nonce_size_is_configurable():
    assert len(new_nonce(6)) < len(new_nonce(30))


def test_csp_is_strict_and_uses_the_nonce():
    policy = content_security_policy("abc123", secure=True)
    assert "script-src 'nonce-abc123'" in policy
    assert "default-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "base-uri 'none'" in policy
    assert "form-action 'self'" in policy
    assert "upgrade-insecure-requests" in policy
    for forbidden in ("unsafe-inline", "unsafe-eval", "*", "http:"):
        assert forbidden not in policy
    assert "upgrade-insecure-requests" not in content_security_policy("abc", secure=False)


def test_security_headers_by_context():
    page = security_headers("n", web(), static=False)
    assert page["Cache-Control"] == "no-store"
    assert page["Strict-Transport-Security"] == "max-age=63072000; includeSubDomains"
    assert page["X-Content-Type-Options"] == "nosniff"
    asset = security_headers("n", web(cookie_secure=False), static=True)
    assert "Cache-Control" not in asset
    assert "Strict-Transport-Security" not in asset


def test_header_values_come_from_the_web_settings():
    custom = web(hsts_max_age_seconds=60, permissions_policy="camera=()")
    headers = security_headers("n", custom, static=False)
    assert headers["Strict-Transport-Security"] == "max-age=60; includeSubDomains"
    assert headers["Permissions-Policy"] == "camera=()"


def test_tokens_match_is_exact_and_tolerates_non_ascii():
    assert tokens_match("secret", "secret")
    assert not tokens_match("secret", "Secret")
    assert not tokens_match("secret", "")
    assert not tokens_match("secret", "sécret")


def test_sessions_expire_when_idle():
    clock = Clock()
    store = SessionStore(web(session_idle_seconds=10, session_max_seconds=100), clock)
    session = store.create()
    clock.now = 9
    assert store.get(session.id) is session
    clock.now = 18
    assert store.get(session.id) is session
    clock.now = 29
    assert store.get(session.id) is None


def test_sessions_expire_at_their_absolute_age_even_when_active():
    clock = Clock()
    store = SessionStore(web(session_idle_seconds=10, session_max_seconds=30), clock)
    session = store.create()
    for tick in (8, 16, 24):
        clock.now = tick
        assert store.get(session.id) is session
    clock.now = 32
    assert store.get(session.id) is None


def test_sessions_reject_unknown_ids_and_can_be_destroyed():
    store = SessionStore(web())
    session = store.create()
    assert store.get(None) is None
    assert store.get("") is None
    assert store.get("nope") is None
    store.destroy(session.id)
    store.destroy(session.id)
    assert store.get(session.id) is None


def test_session_ids_are_high_entropy_and_their_size_is_configurable():
    store = SessionStore(web(max_sessions=500))
    ids = {store.create().id for _ in range(300)}
    assert len(ids) == 300
    assert all(len(session_id) >= 40 for session_id in ids)
    short = SessionStore(web(session_id_bytes=8)).create()
    assert len(short.id) < 20


def test_session_capacity_evicts_anonymous_sessions_first():
    clock = Clock()
    store = SessionStore(web(max_sessions=3, session_idle_seconds=1000), clock)
    kept = store.create(authenticated=True)
    clock.now = 1
    first = store.create()
    clock.now = 2
    second = store.create()
    clock.now = 3
    store.create()
    assert store.get(kept.id) is kept
    assert store.get(first.id) is None
    assert store.get(second.id) is second


def test_creating_a_session_evicts_expired_ones():
    clock = Clock()
    store = SessionStore(web(session_idle_seconds=10), clock)
    stale = store.create()
    clock.now = 50
    store.create()
    assert store.get(stale.id) is None


def test_rate_limiter_window_and_reset():
    clock = Clock()
    limiter = RateLimiter(2, 10, clock=clock)
    assert limiter.allow("a") and limiter.allow("a")
    assert not limiter.allow("a")
    assert limiter.allow("b")
    clock.now = 10
    assert limiter.allow("a")
    limiter.reset("a")
    assert limiter.allow("a") and limiter.allow("a")
    assert not limiter.allow("a")


def test_rate_limiter_can_report_a_block_without_counting():
    clock = Clock()
    limiter = RateLimiter(2, 10, clock=clock)
    assert not limiter.blocked("a")
    limiter.allow("a")
    assert not limiter.blocked("a")
    limiter.allow("a")
    assert limiter.blocked("a") and limiter.blocked("a")
    assert not limiter.blocked("b")
    clock.now = 10
    assert not limiter.blocked("a")
    assert limiter.allow("a")


def test_rate_limiter_forgets_stale_keys_when_full():
    clock = Clock()
    limiter = RateLimiter(1, 10, clock=clock, max_keys=2)
    limiter.allow("a")
    limiter.allow("b")
    clock.now = 20
    assert limiter.allow("c")
    assert len(limiter._events) == 1


async def run_asgi(middleware, app_under_test, receive_messages=()):
    sent = []
    messages = list(receive_messages)

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "path": "/page", "headers": [], "method": "POST"}
    await middleware(app_under_test)(scope, receive, send)
    return sent


async def test_security_middleware_strips_the_server_header_and_adds_headers():
    async def application(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"server", b"x")]})
        await send({"type": "http.response.body", "body": b"ok"})

    sent = await run_asgi(lambda app: SecurityHeadersMiddleware(app, web=web()), application)
    names = {name.lower() for name, _ in sent[0]["headers"]}
    assert b"server" not in names
    assert b"content-security-policy" in names


async def test_security_middleware_turns_early_failures_into_a_generic_500():
    async def application(scope, receive, send):
        raise RuntimeError("secret detail")

    sent = await run_asgi(lambda app: SecurityHeadersMiddleware(app, web=web()), application)
    assert sent[0]["status"] == 500
    assert b"secret" not in sent[1]["body"]
    assert any(name.lower() == b"content-security-policy" for name, _ in sent[0]["headers"])


async def test_security_middleware_cannot_hide_failures_after_the_response_started():
    async def application(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("late failure")

    with pytest.raises(RuntimeError, match="late failure"):
        await run_asgi(lambda app: SecurityHeadersMiddleware(app, web=web()), application)


async def test_body_limit_middleware_counts_streamed_bodies_and_respects_started_responses():
    async def reader(scope, receive, send):
        while (message := await receive())["type"] == "http.request":
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    chunks = [{"type": "http.request", "body": b"x" * 6, "more_body": True}] * 2
    sent = await run_asgi(lambda app: BodyLimitMiddleware(app, max_bytes=10), reader, chunks)
    assert sent[0]["status"] == 413

    async def late(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await receive()
        await receive()

    with pytest.raises(BodyTooLarge):
        await run_asgi(lambda app: BodyLimitMiddleware(app, max_bytes=10), late, chunks)


async def test_non_http_scopes_pass_through_the_middlewares():
    seen = []

    async def application(scope, receive, send):
        seen.append(scope["type"])

    for factory in (
        lambda app: SecurityHeadersMiddleware(app, web=web()),
        lambda app: BodyLimitMiddleware(app, max_bytes=1),
    ):
        await factory(application)({"type": "lifespan"}, None, None)
    assert seen == ["lifespan", "lifespan"]


def test_web_settings_require_a_strong_token():
    for token in ("short", " " + "x" * 30):
        with pytest.raises(ValidationError):
            WebSettings(_env_file=None, access_token=token)
    assert load_web_settings().access_token is None


def test_the_minimum_token_length_is_configurable():
    WebSettings(_env_file=None, min_token_length=8, access_token="x" * 8)
    with pytest.raises(ValidationError):
        WebSettings(_env_file=None, min_token_length=40, access_token="x" * 30)


def test_the_api_token_is_optional_strong_and_different():
    assert web().api_token is None
    assert web(api_token="y" * 24).api_token.get_secret_value() == "y" * 24
    with pytest.raises(ValidationError):
        web(api_token="short")
    with pytest.raises(ValidationError, match="must differ"):
        web(api_token=TOKEN)


def test_web_settings_from_the_environment(monkeypatch):
    monkeypatch.setenv("WEB_ACCESS_TOKEN", "Ab3dEf6hIj9kLm2nOp5qRs8t")
    monkeypatch.setenv("WEB_API_TOKEN", "Zy7xWv4uTs1rQp0oNm9lKj6i")
    monkeypatch.setenv("WEB_ALLOWED_HOSTS", "a.example, b.example")
    monkeypatch.setenv("WEB_PUBLIC_ORIGIN", "https://a.example/")
    settings = load_web_settings()
    assert settings.hosts == ["a.example", "b.example"]
    assert settings.public_origin == "https://a.example"
    assert "Ab3dEf6hIj9kLm2nOp5qRs8t" not in repr(settings)
    assert "Zy7xWv4uTs1rQp0oNm9lKj6i" not in repr(settings)


def test_web_settings_defaults_document_the_security_limits():
    settings = web()
    assert settings.loopback_hosts == ("127.0.0.1", "localhost", "::1")
    assert settings.nonce_bytes >= 16 and settings.session_id_bytes >= 32
    assert settings.max_field_length == 2000 and settings.body_overhead_bytes == 65_536
    assert settings.cookie_secure is True


def test_web_settings_refuse_a_wildcard_host_list():
    with pytest.raises(ValidationError):
        web(allowed_hosts="localhost, *")
    web(allowed_hosts="*.example.com")


@pytest.mark.parametrize("origin", ["ftp://a.example", "https://a.example/path", "a.example"])
def test_web_settings_reject_bad_origins(origin):
    with pytest.raises(ValidationError):
        web(public_origin=origin)


def test_weak_tokens_are_refused_when_loading_from_the_environment(monkeypatch):
    monkeypatch.setenv("WEB_ACCESS_TOKEN", "x" * 24)
    with pytest.raises(ConfigError, match="different characters"):
        load_web_settings()
