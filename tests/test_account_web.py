# tests/test_account_web.py
from tests.web_helpers import (
    add_user,
    Browser,
    build_app,
    open_client,
    ORIGIN,
    PASSWORD,
    TOKEN,
)
from agent.totp import (
    code_at,
    counter_for,
)
from agent.config import AuthTuning
from agent.errors import AgentError

import pytest
import time
import re

OTHER = "another-long-passphrase-77"
CODE_FIELD = re.compile(r'class="num secretkey">([A-Z2-7]+)<')
LINK_BOX = re.compile(r"(https?://[^\s<]+/(?:invite|reset|l)/[A-Za-z0-9_-]+)")
RECOVERY = re.compile(r'<li class="num">((?:[0-9a-f]{5}-){3}[0-9a-f]{5})</li>')


def make(tmp_path, **overrides):
    app, _ = build_app(tmp_path, **overrides)
    return app


def totp_now(secret, offset=0, at=None):
    tuning = AuthTuning()
    return code_at(secret, counter_for(at or time.time(), tuning) + offset, tuning.totp_digits)


def test_setup_creates_the_first_administrator_once(tmp_path):
    app = make(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        ctx = app.state.ctx
        assert ctx.setup_code
        wrong = client.get("/setup?code=nope")
        assert "setup code was not accepted" in wrong.text
        token = browser.csrf(f"/setup?code={ctx.setup_code}")
        fields = {
            "code": ctx.setup_code,
            "username": "eva",
            "password": PASSWORD,
            "confirm": "different-one-entirely-1",
        }
        mismatch = browser.post("/setup", fields, token=token)
        assert mismatch.status_code == 400 and "not the same" in mismatch.text
        created = browser.post("/setup", {**fields, "confirm": PASSWORD}, token=token)
        assert created.status_code == 303
        assert ctx.accounts.user_named("eva").role_name == "admin"
        assert ctx.setup_code is None
        again = browser.post("/setup", {**fields, "confirm": PASSWORD}, token=token)
        assert again.status_code in {303, 403}
        assert ctx.accounts.count_users() == 1


def test_password_sign_in_hides_which_part_was_wrong(tmp_path):
    app = make(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        add_user(app, "eva")
        unknown = browser.sign_in("nobody")
        wrong = browser.sign_in("eva", "not-the-password-at-all")
        assert unknown.status_code == wrong.status_code == 401
        assert "were not accepted" in unknown.text and "were not accepted" in wrong.text
        ok = browser.sign_in("eva")
        assert ok.status_code == 303
        assert client.get("/account").status_code == 200
        entries = app.state.ctx.accounts.audit(action="login")
        assert {entry.outcome for entry in entries} == {"denied", "ok"}


def test_sign_in_is_rate_limited_per_username(tmp_path):
    app = make(tmp_path, user_login_attempts=3, login_attempts=50)
    with open_client(app) as client:
        browser = Browser(client)
        add_user(app, "eva")
        codes = [
            browser.sign_in("eva", f"guess-number-{index}-here").status_code for index in range(5)
        ]
        assert codes[:3] == [401, 401, 401] and codes[3:] == [429, 429]
        assert browser.sign_in("eva").status_code == 429


def test_a_disabled_account_cannot_sign_in_and_loses_its_sessions(tmp_path):
    app = make(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        user = add_user(app, "eva")
        assert browser.sign_in("eva").status_code == 303
        assert client.get("/account").status_code == 200
        add_user(app, "boss")
        app.state.ctx.accounts.update_user(user.id, status="disabled")
        assert client.get("/account").status_code == 303
        assert browser.sign_in("eva").status_code == 401


def test_changing_the_password_signs_other_devices_out(tmp_path):
    app = make(tmp_path)
    with open_client(app) as first, open_client(app) as second:
        add_user(app, "eva")
        one, two = Browser(first), Browser(second)
        token = one.signed_in_token("eva")
        two.signed_in_token("eva")
        assert second.get("/account").status_code == 200
        bad = one.post(
            "/account/password",
            {"current": "wrong-wrong-wrong", "password": OTHER, "confirm": OTHER},
            token=token,
        )
        assert bad.status_code == 400 and "not correct" in bad.text
        weak = one.post(
            "/account/password",
            {"current": PASSWORD, "password": "short", "confirm": "short"},
            token=token,
        )
        assert weak.status_code == 400
        good = one.post(
            "/account/password",
            {"current": PASSWORD, "password": OTHER, "confirm": OTHER},
            token=token,
        )
        assert good.status_code == 303
        assert first.get("/account").status_code == 200
        assert second.get("/account").status_code == 303
        assert Browser(second).sign_in("eva", OTHER).status_code == 303


def test_two_step_sign_in_with_an_authenticator_and_recovery_codes(tmp_path):
    app = make(tmp_path)
    now = time.time()
    with open_client(app) as client:
        browser = Browser(client)
        add_user(app, "eva")
        token = browser.signed_in_token("eva", path="/account/2fa")
        page = client.get("/account/2fa")
        secret = CODE_FIELD.search(page.text).group(1)
        assert "<svg" in page.text
        bad = browser.post("/account/2fa/enable", {"code": "000000"}, token=token)
        assert bad.status_code == 400
        enabled = browser.post(
            "/account/2fa/enable", {"code": totp_now(secret, at=now)}, token=token
        )
        assert enabled.status_code == 200
        codes = RECOVERY.findall(enabled.text)
        assert len(codes) == AuthTuning().recovery_codes
        assert client.get("/account").status_code == 200
        out = browser.post("/logout", token=browser.csrf("/account"))
        assert out.status_code == 303
        step = browser.sign_in("eva")
        assert step.headers["location"] == "/login/verify"
        assert client.get("/account").status_code == 303
        verify_token = browser.csrf("/login/verify")
        replay = browser.post(
            "/login/verify", {"code": totp_now(secret, at=now)}, token=verify_token
        )
        assert replay.status_code == 401
        good = browser.post("/login/verify", {"code": totp_now(secret, 1, now)}, token=verify_token)
        assert good.status_code == 303
        assert client.get("/account").status_code == 200
        browser.post("/logout", token=browser.csrf("/account"))
        browser.sign_in("eva")
        used = browser.post(
            "/login/verify", {"code": codes[0]}, token=browser.csrf("/login/verify")
        )
        assert used.status_code == 303
        browser.post("/logout", token=browser.csrf("/account"))
        browser.sign_in("eva")
        again = browser.post(
            "/login/verify", {"code": codes[0]}, token=browser.csrf("/login/verify")
        )
        assert again.status_code == 401


def test_the_policy_can_require_an_authenticator_for_everyone(tmp_path):
    app = make(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        add_user(app, "eva")
        app.state.ctx.policy.update(require_totp=True)
        response = browser.sign_in("eva")
        assert response.headers["location"] == "/account/2fa"
        assert client.get("/").headers["location"] == "/account/2fa"
        assert client.get("/settings").headers["location"] == "/account/2fa"
        assert client.get("/account/2fa").status_code == 200
        assert "requires an authenticator" in client.get("/account/2fa").text


def test_an_invitation_link_creates_the_password_and_works_once(tmp_path):
    app = make(tmp_path)
    with open_client(app) as admin, open_client(app) as guest:
        add_user(app, "boss")
        boss = Browser(admin)
        token = boss.signed_in_token("boss", path="/admin/users")
        created = boss.post(
            "/admin/users",
            {
                "username": "newbie",
                "display_name": "New Person",
                "role": app.state.ctx.accounts.role_named("recruiter").id,
            },
            token=token,
        )
        assert created.status_code == 200
        link = LINK_BOX.search(created.text).group(1)
        path = "/" + link.split("/", 3)[3]
        visitor = Browser(guest)
        page = guest.get(path)
        assert page.status_code == 200 and "newbie" in page.text
        form = visitor.csrf(path)
        weak = visitor.post(path, {"password": "short", "confirm": "short"}, token=form)
        assert weak.status_code == 400
        done = visitor.post(path, {"password": OTHER, "confirm": OTHER}, token=form)
        assert done.status_code == 303
        assert guest.get("/account").status_code == 200
        assert app.state.ctx.accounts.user_named("newbie").status == "active"
        with open_client(app) as late:
            assert late.get(path).status_code == 404


def test_every_page_is_hidden_from_a_role_without_its_permission(tmp_path):
    app = make(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        add_user(app, "viewer", role="viewer")
        browser.signed_in_token("viewer")
        for path in (
            "/admin/users",
            "/admin/roles",
            "/admin/audit",
            "/admin/security",
            "/emails",
            "/api",
        ):
            assert client.get(path).status_code == 404, path
        assert client.get("/account").status_code == 200
        home = client.get("/account").text
        assert 'href="/admin"' not in home and 'href="/emails"' not in home
        denied = app.state.ctx.accounts.audit(action="access.denied")
        assert len(denied) >= 4


def test_a_role_cannot_grant_what_its_holder_does_not_have(tmp_path):
    app = make(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        ctx = app.state.ctx
        ctx.accounts.save_role("Roles only", "", ["roles.manage", "users.view", "users.manage"])
        add_user(app, "keeper", role="Roles only")
        add_user(app, "root", role="admin")
        token = browser.signed_in_token("keeper", path="/admin/roles")
        sneaky = browser.post(
            "/admin/roles",
            {"name": "Sneaky", "permission": ["roles.manage", "config.manage"]},
            token=token,
        )
        assert sneaky.status_code == 400 and "only give permissions" in sneaky.text
        assert ctx.accounts.role_named("Sneaky") is None
        admin_role = ctx.accounts.role_named("admin")
        edit = browser.post(
            "/admin/roles",
            {"id": admin_role.id, "name": "admin", "permission": ["chat.use"]},
            token=token,
        )
        assert edit.status_code == 404
        users_token = browser.csrf("/admin/users")
        promote = browser.post(
            "/admin/users",
            {"username": "extra", "role": admin_role.id},
            token=users_token,
        )
        assert promote.status_code == 400
        root = ctx.accounts.user_named("root")
        assert client.get(f"/admin/users/{root.id}").status_code == 200
        hit = browser.post(f"/admin/users/{root.id}/delete", token=users_token)
        assert hit.status_code == 404
        assert ctx.accounts.user_named("root") is not None


def test_the_last_administrator_cannot_be_removed_or_demoted(tmp_path):
    app = make(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        ctx = app.state.ctx
        add_user(app, "root")
        token = browser.signed_in_token("root", path="/admin/users")
        me = ctx.accounts.user_named("root")
        removed = browser.post(f"/admin/users/{me.id}/delete", token=token)
        assert removed.status_code == 400
        demote = browser.post(
            f"/admin/users/{me.id}",
            {"role": ctx.accounts.role_named("viewer").id, "status": "active"},
            token=token,
        )
        assert demote.status_code == 400
        assert ctx.accounts.user_named("root").role_name == "admin"


def test_the_audit_log_filters_and_exports_without_formula_injection(tmp_path):
    app = make(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        add_user(app, "root")
        browser.signed_in_token("root")
        app.state.ctx.accounts.log("note.add", actor_name="=cmd|' /C calc'!A0", target="+1")
        page = client.get("/admin/audit?action=note")
        assert page.status_code == 200 and "note.add" in page.text and "login.ok" not in page.text
        export = client.get("/admin/audit.csv?action=note")
        assert export.headers["content-type"].startswith("text/csv")
        assert "'=cmd" in export.text and ",'+1," in export.text


def test_session_listing_shows_handles_never_ids_and_can_revoke_one(tmp_path):
    app = make(tmp_path)
    with open_client(app) as first, open_client(app) as second:
        add_user(app, "eva")
        one, two = Browser(first), Browser(second)
        token = one.signed_in_token("eva")
        two.signed_in_token("eva")
        page = first.get("/account").text
        assert page.count("this one") == 1
        handle = re.findall(r'name="handle" value="([0-9a-f]{16})"', page)[0]
        session_ids = list(app.state.ctx.sessions._sessions)
        assert not any(identifier in page for identifier in session_ids)
        gone = one.post("/account/sessions/revoke", {"handle": handle}, token=token)
        assert gone.status_code == 303
        assert second.get("/account").status_code == 303
        assert first.get("/account").status_code == 200


def test_api_keys_are_shown_once_and_limited_to_held_permissions(tmp_path):
    app = make(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        ctx = app.state.ctx
        add_user(app, "mgr", role="manager")
        token = browser.signed_in_token("mgr")
        sneaky = browser.post(
            "/account/keys",
            {"name": "x", "current": PASSWORD, "permission": ["config.manage"]},
            token=token,
        )
        assert sneaky.status_code == 400
        unproven = browser.post(
            "/account/keys", {"name": "x", "permission": ["reports.view"]}, token=token
        )
        assert unproven.status_code == 400 and "not correct" in unproven.text
        made = browser.post(
            "/account/keys",
            {"name": "reports", "days": "30", "current": PASSWORD, "permission": ["reports.view"]},
            token=token,
        )
        assert made.status_code == 200 and "hra_" in made.text
        assert "hra_" not in client.get("/account").text
        user = ctx.accounts.user_named("mgr")
        (key,) = ctx.accounts.keys(user.id)
        assert key.permissions == ["reports.view"]
        bad_days = browser.post(
            "/account/keys", {"name": "y", "days": "9999", "current": PASSWORD}, token=token
        )
        assert bad_days.status_code == 400
        revoked = browser.post(f"/account/keys/{key.id}/revoke", token=token)
        assert revoked.status_code == 303
        assert ctx.accounts.keys(user.id)[0].revoked is not None


def test_the_access_token_still_signs_in_as_the_owner(tmp_path):
    app = make(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        assert browser.login(TOKEN).status_code == 303
        assert client.get("/admin/users").status_code == 200
        page = client.get("/account")
        assert page.status_code == 200 and "shared access token" in page.text
        assert ORIGIN


def test_api_keys_die_with_the_password_the_second_factor_and_a_global_sign_out(tmp_path):
    app = make(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        ctx = app.state.ctx
        add_user(app, "mgr", role="manager")
        token = browser.signed_in_token("mgr")
        user = ctx.accounts.user_named("mgr")
        for _ in range(3):
            ctx.accounts.create_key(user.id, "k", None)
        assert len([key for key in ctx.accounts.keys(user.id) if key.revoked is None]) == 3
        browser.post("/account/sessions/revoke", {}, token=token)
        assert all(key.revoked is not None for key in ctx.accounts.keys(user.id))
        ctx.accounts.create_key(user.id, "k2", None)
        ctx.accounts.set_password(user.id, ctx.hasher.hash("another-long-password-1"))
        assert all(key.revoked is not None for key in ctx.accounts.keys(user.id))
        ctx.accounts.create_key(user.id, "k3", None)
        ctx.accounts.disable_totp(user.id)
        assert all(key.revoked is not None for key in ctx.accounts.keys(user.id))
        ctx.accounts.create_key(user.id, "k4", None)
        ctx.accounts.bump_version(user.id)
        assert all(key.revoked is not None for key in ctx.accounts.keys(user.id))


def test_a_display_name_cannot_pose_as_another_account_or_hide_in_invisible_characters(tmp_path):
    app = make(tmp_path)
    with open_client(app):
        ctx = app.state.ctx
        add_user(app, "alice")
        eve = add_user(app, "eve", role="manager")
        with pytest.raises(AgentError, match="belongs to another account"):
            ctx.accounts.update_user(eve.id, display_name="Alice")
        cleaned = ctx.accounts.update_user(eve.id, display_name="E\u200bv\u202ee  Jones")
        assert cleaned.display_name == "Eve Jones"
        principal = ctx.accounts.principal_for(eve.id)
        assert principal.username == "eve"
        ctx.accounts.log("data.export", actor=principal)
        assert ctx.accounts.audit(limit=1)[0].actor == "eve"


def test_the_account_page_hides_the_rail_content_while_enrolling_in_two_step(tmp_path):
    app = make(tmp_path)
    ctx = app.state.ctx
    ctx.policy.update(require_totp=True)
    with open_client(app) as client:
        browser = Browser(client)
        add_user(app, "eva", role="admin")
        ctx.store.create_thread("research", "Secret candidate name")
        browser.signed_in_token("eva", path="/account/2fa")
        page = client.get("/account/2fa")
        assert page.status_code == 200 and "Secret candidate name" not in page.text
