# tests/test_web_security.py
from tests.web_helpers import (
    Browser,
    build_app,
    open_client,
    ORIGIN,
    say,
    start,
    TOKEN,
    wait_for,
)
from starlette.routing import Route

import pytest
import re

NONCE = re.compile(r"script-src 'nonce-([^']+)'")
CHAT_FORM = {"text": "Jan Novak", "thread": ""}


def test_every_response_carries_the_hardening_headers(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        responses = [
            client.get("/"),
            client.get("/missing"),
            client.get("/login"),
            client.post("/login", data={}, headers={"origin": ORIGIN}),
        ]
        browser.login()
        responses.append(client.get("/"))
    for response in responses:
        headers = response.headers
        assert "script-src 'nonce-" in headers["content-security-policy"]
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"
        assert headers["referrer-policy"] == "same-origin"
        assert headers["cross-origin-opener-policy"] == "same-origin"
        assert headers["cross-origin-resource-policy"] == "same-origin"
        assert "camera=()" in headers["permissions-policy"]
        assert headers["strict-transport-security"].startswith("max-age=")
        assert headers["cache-control"] == "no-store"
        assert "server" not in headers


def test_the_nonce_changes_on_every_response_and_matches_every_script(tmp_path):
    app, stub = build_app(tmp_path)
    gate = stub.hold()
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        nonces = {
            NONCE.search(client.get("/").headers["content-security-policy"]).group(1)
            for _ in range(5)
        }
        assert len(nonces) == 5
        location = start(browser, token)
        for path in (location, "/", "/settings", "/emails"):
            page = client.get(path)
            policy_nonce = NONCE.search(page.headers["content-security-policy"]).group(1)
            scripts = re.findall(r"<script\b([^>]*)>", page.text)
            code = [attrs for attrs in scripts if "application/json" not in attrs]
            assert code and all(f'nonce="{policy_nonce}"' in attrs for attrs in code), path
        gate.set()


def test_pages_contain_no_inline_handlers_styles_or_external_resources(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        pages = [client.get(path) for path in ("/", "/settings", "/emails")]
        location = start(browser, token)
        wait_for(browser, location)
        pages.append(client.get(location))
        thread = say(browser, token, "Jan").json()["thread"]["id"]
        pages.append(client.get(f"/c/{thread}"))
    with open_client(app) as anonymous:
        pages.append(anonymous.get("/"))
    for page in pages:
        html = page.text
        assert not re.search(r"\son\w+\s*=", html, re.IGNORECASE)
        assert not re.search(r"\sstyle\s*=", html, re.IGNORECASE)
        assert "javascript:" not in html.lower()
        assert "<style" not in html.lower()
        assert not re.search(
            r'<(?:script|link|img|iframe|form)\b[^>]*(?:src|href|action)="https?://', html
        )


def test_static_stylesheet_is_served_with_the_right_type_and_is_cacheable(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        response = client.get("/static/app.css")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    assert "public" in response.headers["cache-control"]
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("path", ["/static/../pyproject.toml", "/static/other.css", "/static/"])
def test_no_other_static_files_are_reachable(tmp_path, path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        assert client.get(path).status_code == 404


def test_unauthenticated_requests_are_sent_to_the_login_page(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        for path in (
            "/settings",
            "/emails",
            "/api",
            "/c/abc",
            "/chat/abc/messages",
            "/tie/messages",
            "/jobs/abc",
            "/jobs/abc/status",
            "/jobs/abc/events",
            "/jobs/abc/speech",
            "/jobs/abc/download/md",
        ):
            response = client.get(path)
            assert response.status_code == 303 and response.headers["location"] == "/login", path
        for path in (
            "/chat/send",
            "/chat/abc/delete",
            "/tie/send",
            "/tie/clear",
            "/settings",
            "/settings/reset",
            "/settings/requirements/remove",
            "/settings/data/delete",
            "/jobs/abc/delete",
            "/live-view",
            "/buddy",
            "/voice/transcribe",
            "/emails/preview",
            "/logout",
        ):
            response = client.post(path, data={}, headers={"origin": ORIGIN})
            assert response.status_code == 303 and response.headers["location"] == "/login", path


def test_login_sets_a_hardened_cookie_and_rotates_the_session(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        first = client.get("/")
        before = first.cookies.get("__Host-session")
        cookie_header = first.headers["set-cookie"].lower()
        for flag in ("httponly", "secure", "samesite=strict", "path=/"):
            assert flag in cookie_header
        assert "domain" not in cookie_header
        response = browser.post("/login", {"token": TOKEN}, token=browser.csrf())
        after = response.cookies.get("__Host-session")
        assert response.status_code == 303 and response.headers["location"] == "/"
        assert after and after != before
        assert client.get("/").status_code == 200


def test_wrong_tokens_are_rejected_with_a_generic_message(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        response = browser.login(token="x" * 30)
        assert response.status_code == 401
        assert "not accepted" in response.text and "Know who you are hiring." in response.text
        assert TOKEN not in response.text
        assert client.get("/settings").status_code == 303


def test_login_requires_a_session_a_csrf_token_and_a_same_origin_request(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        no_cookie = client.post("/login", data={"token": TOKEN}, headers={"origin": ORIGIN})
        assert no_cookie.status_code == 403
        browser = Browser(client)
        token = browser.csrf()
        assert browser.post("/login", {"token": TOKEN}).status_code == 403
        assert browser.post("/login", {"token": TOKEN}, token="wrong").status_code == 403
        foreign = browser.post(
            "/login", {"token": TOKEN}, token=token, origin="https://evil.example"
        )
        assert foreign.status_code == 403
        assert browser.post("/login", {"token": TOKEN}, token=token).status_code == 303


def test_repeated_failures_are_rate_limited(tmp_path):
    app, _ = build_app(tmp_path, login_attempts=3)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.csrf()
        statuses = [
            browser.post("/login", {"token": "wrong-" * 6}, token=token).status_code
            for _ in range(5)
        ]
        assert statuses == [401, 401, 401, 429, 429]
        assert browser.post("/login", {"token": TOKEN}, token=token).status_code == 429


def test_anonymous_session_creation_is_limited_per_address(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        statuses = []
        for _ in range(35):
            client.cookies.clear()
            statuses.append(client.get("/").status_code)
    assert statuses[:30] == [200] * 30
    assert set(statuses[30:]) == {429}


def test_logout_needs_csrf_and_invalidates_the_session(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        assert browser.post("/logout", token="wrong").status_code == 403
        assert client.get("/").status_code == 200
        stale = client.cookies.get("__Host-session")
        response = browser.post("/logout", token=token)
        assert response.status_code == 303 and response.headers["location"] == "/"
        assert "clear-site-data" in response.headers
        client.cookies.set("__Host-session", stale)
        assert client.get("/settings").status_code == 303


@pytest.mark.parametrize(
    ("headers", "origin"),
    [
        ({}, "https://evil.example"),
        ({}, "http://localhost"),
        ({"sec-fetch-site": "cross-site"}, None),
        ({"sec-fetch-site": "same-site"}, None),
        ({"sec-fetch-site": "cross-site"}, "null"),
        ({"sec-fetch-site": "same-site"}, "null"),
    ],
)
def test_state_changing_requests_must_be_same_origin(tmp_path, headers, origin):
    app, stub = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        response = browser.post(
            "/chat/send", CHAT_FORM, token=token, origin=origin, headers=headers
        )
        assert response.status_code == 403
    assert stub.calls == []


def test_state_changing_requests_need_a_valid_csrf_token(tmp_path):
    app, stub = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        assert browser.post("/chat/send", CHAT_FORM).status_code == 403
        assert browser.post("/chat/send", CHAT_FORM, token="forged").status_code == 403
        assert browser.post("/chat/send", CHAT_FORM, token=token[:-1]).status_code == 403
        assert browser.post("/chat/send", CHAT_FORM, token="").status_code == 403
        assert browser.post("/settings/reset", token="forged").status_code == 403
        assert browser.post("/logout", token="forged").status_code == 403
    assert stub.calls == []


def test_a_csrf_token_from_another_session_is_useless(tmp_path):
    app, stub = build_app(tmp_path)
    with open_client(app) as client, open_client(app) as other:
        mine = Browser(client)
        theirs = Browser(other)
        my_token = mine.logged_in_token()
        theirs.logged_in_token()
        assert theirs.post("/chat/send", CHAT_FORM, token=my_token).status_code == 403
    assert stub.calls == []


def test_the_origin_can_be_pinned_for_deployments_behind_a_proxy(tmp_path):
    app, _ = build_app(tmp_path, public_origin="https://agent.example.com")
    with open_client(app) as client:
        browser = Browser(client)
        pinned_origin = "https://agent.example.com"
        assert browser.login().status_code == 403
        form_token = browser.csrf()
        login = browser.post("/login", {"token": TOKEN}, token=form_token, origin=pinned_origin)
        assert login.status_code == 303
        token = browser.csrf("/")
        assert browser.post("/chat/send", CHAT_FORM, token=token).status_code == 403
        pinned = browser.post(
            "/chat/send", CHAT_FORM, token=token, origin="https://agent.example.com"
        )
        assert pinned.status_code == 200


def test_unknown_hosts_are_rejected(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        response = client.get("/", headers={"host": "evil.example"})
        assert response.status_code == 400
        assert "content-security-policy" in response.headers


def test_wrong_methods_get_a_405_page_with_allow(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        response = client.get("/logout")
        assert response.status_code == 405
        assert "POST" in response.headers["allow"]
        assert client.post("/", data={}).status_code == 405
        assert client.post("/login", data={}).status_code == 403
        assert client.delete("/login").status_code == 405


def test_oversized_bodies_are_rejected_before_parsing(tmp_path):
    app, stub = build_app(tmp_path)
    limit = int(app.state.ctx.settings.cv.max_bytes * 4 / 3) + 65_536
    with open_client(app) as client:
        browser = Browser(client)
        browser.logged_in_token()
        declared = client.post(
            "/chat/send",
            content=b"x" * (limit + 1),
            headers={"origin": ORIGIN, "content-type": "application/x-www-form-urlencoded"},
        )
        assert declared.status_code == 413
        assert "content-security-policy" in declared.headers

        def chunks():
            for _ in range(limit // 4096 + 2):
                yield b"a" * 4096

        streamed = client.post(
            "/chat/send",
            content=chunks(),
            headers={"origin": ORIGIN, "content-type": "application/x-www-form-urlencoded"},
        )
        assert streamed.status_code == 413
        assert client.get("/").status_code == 200
    assert stub.calls == []


def test_unexpected_errors_never_leak_details(tmp_path):
    app, _ = build_app(tmp_path)

    async def boom(request):
        raise RuntimeError("secret internal detail /etc/passwd")

    app.router.routes.insert(0, Route("/boom", boom))
    with open_client(app) as client:
        response = client.get("/boom")
    assert response.status_code == 500
    assert "secret" not in response.text and "passwd" not in response.text
    assert "content-security-policy" in response.headers
    assert response.headers["cache-control"] == "no-store"


def test_the_app_factory_does_not_expose_api_docs(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404


def test_browsers_that_send_a_null_origin_for_same_origin_forms_still_work(tmp_path):
    app, stub = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.csrf("/")
        headers = {"sec-fetch-site": "same-origin"}
        login = browser.post(
            "/login", {"token": TOKEN}, token=token, origin="null", headers=headers
        )
        assert login.status_code == 303
        token = browser.csrf("/")
        chat = browser.post("/chat/send", CHAT_FORM, token=token, origin="null", headers=headers)
        assert chat.status_code == 200
        forged = browser.post(
            "/chat/send", CHAT_FORM, token="forged", origin="null", headers=headers
        )
        assert forged.status_code == 403
    assert stub.calls == []


def test_the_referrer_policy_keeps_the_origin_header_useful_but_leaks_nothing_outward(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        response = client.get("/")
    assert response.headers["referrer-policy"] == "same-origin"
