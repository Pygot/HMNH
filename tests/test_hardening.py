# tests/test_hardening.py
from tests.web_helpers import (
    add_user,
    Browser,
    build_app,
    open_client,
    ORIGIN,
    PASSWORD,
    start,
    wait_for,
)
from agent.web.security import (
    RateLimiter,
    SessionStore,
)
from tests.test_web_settings import (
    CONTEXT_FORM,
    SETTINGS_FORM,
)
from starlette.exceptions import HTTPException
from agent.web.settings import WebSettings
from agent.web.context import client_key
from agent.web.common import revalidate
from agent.config import StoreTuning
from types import SimpleNamespace
from agent.db import Database

import sqlite3
import pytest


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def request_from(host, prefix=64):
    web = WebSettings(_env_file=None, ipv6_prefix=prefix)
    ctx = SimpleNamespace(web=web)
    return SimpleNamespace(
        client=SimpleNamespace(host=host) if host else None,
        app=SimpleNamespace(state=SimpleNamespace(ctx=ctx)),
    )


def test_ipv6_clients_share_one_bucket_per_network_so_rotating_addresses_does_not_help():
    first = client_key(request_from("2001:db8:1:2::1"))
    second = client_key(request_from("2001:db8:1:2:ffff:ffff:ffff:ffff"))
    other = client_key(request_from("2001:db8:1:3::1"))
    assert first == second and first != other
    assert client_key(request_from("203.0.113.9")) == "203.0.113.9"
    assert client_key(request_from(None)) == "unknown"
    assert client_key(request_from("not-an-address")) == "not-an-address"
    assert client_key(request_from("2001:db8:1:2::1", prefix=128)) == "2001:db8:1:2::1"


def test_the_rate_limiter_stays_bounded_and_quick_when_flooded_with_new_keys():
    clock = Clock()
    limiter = RateLimiter(1, 60, clock=clock, max_keys=50)
    for number in range(5000):
        limiter.allow(f"key-{number}")
    assert len(limiter._events) <= 50
    assert limiter.allow("fresh") is True and limiter.allow("fresh") is False


def test_one_account_cannot_fill_the_session_table():
    web = WebSettings(_env_file=None, max_sessions_per_user=3, max_sessions=100)
    store = SessionStore(web)
    anonymous = store.create()
    for _ in range(10):
        store.create(authenticated=True, user_id="u1")
    store.create(authenticated=True, user_id="u2")
    assert len(store.of_user("u1")) == 3 and len(store.of_user("u2")) == 1
    assert store.get(anonymous.id) is not None


def test_a_handle_with_unusual_characters_is_just_not_found():
    store = SessionStore(WebSettings(_env_file=None))
    store.create(authenticated=True, user_id="u1")
    assert store.destroy_handle("u1", "éèê") == 0


def test_a_request_is_refused_if_the_account_changed_while_its_body_was_arriving(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app):
        ctx = app.state.ctx
        add_user(app, "root", role="admin")
        user = add_user(app, "mgr", role="manager")
        session = ctx.sessions.create(authenticated=True, user_id=user.id, version=0)
        principal = ctx.accounts.principal_for(user.id)
        session.version = principal.version
        request = SimpleNamespace(state=SimpleNamespace(principal=principal, required=None))
        revalidate(request, ctx, session)
        assert request.state.principal.user_id == user.id
        ctx.accounts.update_user(user.id, status="disabled")
        with pytest.raises(HTTPException) as caught:
            revalidate(request, ctx, session)
        assert caught.value.status_code == 403 and ctx.sessions.get(session.id) is None


def test_losing_the_needed_permission_while_the_body_arrives_hides_the_page(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app):
        ctx = app.state.ctx
        add_user(app, "root", role="admin")
        ctx.accounts.save_role("Helper", "", ["candidates.view", "candidates.edit"])
        user = add_user(app, "helper", role="Helper")
        session = ctx.sessions.create(authenticated=True, user_id=user.id, version=0)
        principal = ctx.accounts.principal_for(user.id)
        session.version = principal.version
        request = SimpleNamespace(
            state=SimpleNamespace(principal=principal, required="candidates.edit")
        )
        revalidate(request, ctx, session)
        role = ctx.accounts.role_named("Helper")
        ctx.accounts.save_role("Helper", "", ["candidates.view"], identifier=role.id)
        with pytest.raises(HTTPException) as caught:
            revalidate(request, ctx, session)
        assert caught.value.status_code in (403, 404)


def test_other_peoples_failed_attempts_never_lock_out_the_real_user(tmp_path):
    app, _ = build_app(tmp_path, login_attempts=100, user_login_attempts=3)
    with open_client(app, "198.51.100.7") as attacker, open_client(app, "203.0.113.5") as home:
        add_user(app, "eva")
        bad = Browser(attacker)
        codes = [bad.sign_in("eva", "wrong password " + str(n)).status_code for n in range(5)]
        assert codes[:3] == [401, 401, 401] and codes[3:] == [429, 429]
        good = Browser(home)
        assert good.sign_in("eva").status_code == 303
        assert bad.sign_in("eva").status_code == 429


def test_a_success_does_not_reset_the_shared_address_budget(tmp_path):
    app, _ = build_app(tmp_path, login_attempts=4, user_login_attempts=50)
    with open_client(app, "198.51.100.7") as client:
        add_user(app, "eva")
        add_user(app, "own")
        browser = Browser(client)
        assert browser.sign_in("eva", "wrong one").status_code == 401
        assert browser.sign_in("eva", "wrong two").status_code == 401
        assert browser.sign_in("own").status_code == 303
        Browser(client).sign_in("eva", "wrong three")
        Browser(client).sign_in("eva", "wrong four")
        assert Browser(client).sign_in("eva", "wrong five").status_code == 429


def test_unknown_names_are_hashed_in_the_audit_log_but_known_ones_are_shown(tmp_path):
    app, _ = build_app(tmp_path, login_attempts=100)
    with open_client(app) as client:
        add_user(app, "eva")
        browser = Browser(client)
        browser.sign_in("my-secret-passphrase-typed-in-the-wrong-box", "x")
        browser.sign_in("eva", "wrong password")
        names = [entry.actor for entry in app.state.ctx.accounts.audit(action="login.fail")]
        assert "eva" in names
        assert all("secret" not in name for name in names)
        assert any(name.startswith("unknown-") and len(name) == 16 for name in names)


def test_only_people_who_may_edit_the_context_change_the_company_details(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        add_user(app, "vic", role="viewer")
        browser = Browser(client)
        token = browser.signed_in_token("vic", path="/settings")
        page = client.get("/settings").text
        assert 'name="company_name"' not in page
        browser.post(
            "/settings",
            {**SETTINGS_FORM, **CONTEXT_FORM, "company_name": "Hacked Ltd"},
            token=token,
        )
        assert app.state.ctx.store.memory("profile") in (None, {}) or (
            "Hacked" not in str(app.state.ctx.store.memory("profile"))
        )
    with open_client(app) as client:
        add_user(app, "ann", role="manager")
        browser = Browser(client)
        token = browser.signed_in_token("ann", path="/settings")
        assert 'name="company_name"' in client.get("/settings").text
        browser.post("/settings", {**SETTINGS_FORM, **CONTEXT_FORM}, token=token)
        assert "Acme" in str(app.state.ctx.store.memory("profile"))


def test_a_reset_link_is_not_voided_by_repeat_requests_inside_the_cooldown(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app):
        ctx = app.state.ctx
        user = add_user(app, "eva", email="eva@example.com")
        first = ctx.accounts.create_link(user.id, "reset", 3600, "self")
        assert ctx.accounts.recent_link(user.id, "reset", 120) is True
        assert ctx.accounts.recent_link(user.id, "login", 120) is False
        assert ctx.accounts.recent_link(user.id, "reset", 0) is False
        assert ctx.accounts.peek_link(first, "reset") is not None


def test_creating_an_api_key_secret_is_never_logged_with_the_wrong_password(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        add_user(app, "mgr", role="manager")
        browser = Browser(client)
        token = browser.signed_in_token("mgr")
        denied = browser.post(
            "/account/keys", {"name": "x", "current": PASSWORD + "!"}, token=token
        )
        assert denied.status_code == 400 and "hra_" not in denied.text


def test_a_role_without_report_access_cannot_reach_findings_through_side_doors(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as owner, open_client(app, "203.0.113.20") as other:
        ctx = app.state.ctx
        add_user(app, "eva", role="admin")
        ctx.accounts.save_role(
            "Side door",
            "",
            ["emails.view", "emails.send", "reports.export", "voice.use", "candidates.view"],
        )
        add_user(app, "sid", role="Side door")
        boss = Browser(owner)
        token = boss.signed_in_token("eva")
        location = start(boss, token)
        wait_for(boss, location)
        job_id = location.rsplit("/", 1)[1]
        assert owner.get(f"/jobs/{job_id}/download/md").status_code == 200
        guest = Browser(other)
        guest_token = guest.signed_in_token("sid", path="/emails")
        assert job_id not in other.get("/emails").text
        assert other.get(f"/jobs/{job_id}/download/md").status_code == 404
        assert other.get(f"/jobs/{job_id}/speech").status_code == 404
        preview = other.post(
            "/emails/preview",
            json={"template": "outreach-single", "job": job_id},
            headers={"X-CSRF-Token": guest_token, "origin": ORIGIN},
        )
        assert job_id not in preview.text


def test_exporting_a_candidate_needs_the_right_to_see_candidates(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        ctx = app.state.ctx
        add_user(app, "root", role="admin")
        person = ctx.candidates.create("Eva Svobodova", notes="private notes")
        ctx.accounts.save_role("Exporter", "", ["data.export"])
        add_user(app, "ex", role="Exporter")
        Browser(client).signed_in_token("ex")
        assert client.get(f"/candidates/{person.id}/export").status_code == 404


def test_a_hire_form_cannot_write_contacts_without_the_contact_permission(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        ctx = app.state.ctx
        add_user(app, "root", role="admin")
        person = ctx.candidates.create("Eva Svobodova")
        ctx.accounts.save_role("Hirer", "", ["candidates.view", "hires.manage"])
        add_user(app, "hirer", role="Hirer")
        browser = Browser(client)
        token = browser.signed_in_token("hirer", path=f"/candidates/{person.id}")
        sent = browser.post(
            f"/candidates/{person.id}/hires",
            {"role_title": "Engineer", "email": "eva@example.com", "phone": "+420 123"},
            token=token,
        )
        assert sent.status_code in {200, 303}
        assert ctx.candidates.hires(person.id) and ctx.candidates.contacts(person.id) == []


def test_every_write_through_the_api_is_audited_with_the_key(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        ctx = app.state.ctx
        user = add_user(app, "mgr", role="manager")
        _, secret = ctx.accounts.create_key(user.id, "script", None)
        headers = {"Authorization": f"Bearer {secret}"}
        made = client.post("/api/v1/candidates", json={"name": "Eva Svobodova"}, headers=headers)
        assert made.status_code in {200, 201}
        client.get("/api/v1/candidates", headers=headers)
        entries = ctx.accounts.audit(action="api.write")
        assert len(entries) == 1 and entries[0].target == "POST /candidates"
        assert entries[0].actor == "mgr" and entries[0].detail["key"]


def test_a_failed_inner_block_rolls_back_only_itself(tmp_path):
    database = Database(StoreTuning(path=tmp_path / "a.db"))
    database.run("CREATE TABLE marks (x INTEGER)")
    with database.transaction() as db:
        db.execute("INSERT INTO marks VALUES (1)")
        with pytest.raises(ValueError), database.transaction() as inner:
            inner.execute("INSERT INTO marks VALUES (2)")
            raise ValueError
        db.execute("INSERT INTO marks VALUES (3)")
    assert [row["x"] for row in database.query("SELECT x FROM marks ORDER BY x")] == [1, 3]
    with pytest.raises(ValueError), database.transaction() as db:
        db.execute("INSERT INTO marks VALUES (4)")
        with database.transaction() as inner:
            inner.execute("INSERT INTO marks VALUES (5)")
        raise ValueError
    assert [row["x"] for row in database.query("SELECT x FROM marks ORDER BY x")] == [1, 3]
    database.close()


def test_a_failing_commit_leaves_nothing_half_written_for_the_next_caller(tmp_path):
    class Flaky:
        def __init__(self, real):
            self.real = real
            self.fail = False

        def commit(self):
            if self.fail:
                self.fail = False
                raise sqlite3.OperationalError("disk full")
            self.real.commit()

        def __getattr__(self, name):
            return getattr(self.real, name)

    database = Database(StoreTuning(path=tmp_path / "b.db"))
    database.run("CREATE TABLE marks (x INTEGER)")
    flaky = Flaky(database._db)
    database._connection = flaky
    flaky.fail = True
    with pytest.raises(sqlite3.OperationalError), database.transaction() as db:
        db.execute("INSERT INTO marks VALUES (1)")
    database.run("INSERT INTO marks VALUES (2)")
    assert [row["x"] for row in database.query("SELECT x FROM marks")] == [2]
    database._connection = flaky.real
    database.close()


def test_administrators_can_see_and_revoke_the_keys_of_other_people(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        ctx = app.state.ctx
        add_user(app, "root", role="admin")
        member = add_user(app, "mgr", role="manager")
        key, _ = ctx.accounts.create_key(member.id, "reporting", None)
        browser = Browser(client)
        token = browser.signed_in_token("root", path=f"/admin/users/{member.id}")
        page = client.get(f"/admin/users/{member.id}").text
        assert "reporting" in page and key.prefix in page
        revoked = browser.post(f"/admin/users/{member.id}/keys/{key.id}/revoke", token=token)
        assert revoked.status_code == 200 and "revoked" in revoked.text
        assert ctx.accounts.keys(member.id)[0].revoked is not None
        again = browser.post(f"/admin/users/{member.id}/keys/{key.id}/revoke", token=token)
        assert again.status_code == 404
    with open_client(app) as other:
        add_user(app, "vic", role="viewer")
        guest = Browser(other)
        guest_token = guest.signed_in_token("vic")
        denied = guest.post(f"/admin/users/{member.id}/keys/{key.id}/revoke", token=guest_token)
        assert denied.status_code == 404


def test_session_devices_are_shown_as_a_browser_and_system_not_the_raw_agent_string():
    from agent.web.format import device

    chrome = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"
    assert device(chrome) == "Chrome on Windows"
    assert device("Mozilla/5.0 (Windows NT 10.0) Chrome/126 Edg/126") == "Edge on Windows"
    assert (
        device("Mozilla/5.0 (Linux; Android 14) Chrome/126 Mobile Safari/537.36")
        == "Chrome on Android"
    )
    assert device("curl/8.5") == "Unknown device"
