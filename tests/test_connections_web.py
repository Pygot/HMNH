# tests/test_connections_web.py
from tests.web_helpers import (
    add_user,
    Browser,
    build_app,
    open_client,
    PASSWORD,
    StubServices,
)
from agent.connections import (
    Check,
    ConnectionChecker,
)
from tests.helpers import (
    json_response,
    mock_client,
)
from agent.config import ENV_FILE_VARIABLE
from contextlib import asynccontextmanager
from agent.web.admin import POLICY_FIELDS
from agent.errors import ConfigError

import os

SECRET = "tok-SECRET-value-123"
CONFIRM = {"confirm_password": PASSWORD}


async def fine(target, settings):
    return Check(target=target, ok=True, message="Works")


def boot(tmp_path, monkeypatch):
    env = tmp_path / "private.env"
    monkeypatch.setenv(ENV_FILE_VARIABLE, str(env))
    app, stub = build_app(tmp_path)
    return app, stub, env


def test_only_people_with_the_permission_see_the_connections(tmp_path, monkeypatch):
    app, _, _ = boot(tmp_path, monkeypatch)
    with open_client(app) as client:
        browser = Browser(client)
        add_user(app, "viewer", role="viewer")
        token = browser.signed_in_token("viewer")
        assert client.get("/admin/connections").status_code == 404
        sent = browser.post(
            "/admin/connections/apify", {**CONFIRM, "APIFY_TOKEN": SECRET}, token=token
        )
        assert sent.status_code == 404


def test_saving_a_token_writes_the_file_applies_it_and_never_echoes_it(tmp_path, monkeypatch):
    app, _, env = boot(tmp_path, monkeypatch)
    with open_client(app) as client:
        browser = Browser(client)
        ctx = app.state.ctx
        add_user(app, "root")
        token = browser.signed_in_token("root", path="/admin/connections")
        before = ctx.services
        ctx.checker = ConnectionChecker(
            lambda settings: mock_client(
                lambda r: json_response(
                    {
                        "data": {
                            "current": {"monthlyUsageUsd": 0.5},
                            "limits": {"maxMonthlyUsageUsd": 10, "maxConcurrentActorJobs": 2},
                        }
                    }
                )
            )
        )
        saved = browser.post(
            "/admin/connections/apify", {**CONFIRM, "APIFY_TOKEN": SECRET}, token=token
        )
        assert saved.status_code == 200
        assert "Saved and applied" in saved.text and "Works" in saved.text
        assert SECRET not in saved.text
        assert SECRET in env.read_text(encoding="utf-8")
        assert ctx.settings.apify_token.get_secret_value() == SECRET
        assert ctx.services is not before and ctx.services is not None
        assert SECRET not in client.get("/admin/connections").text
        assert "saved" in client.get("/admin/connections").text
        entry = ctx.accounts.audit(action="connections.save")[0]
        assert entry.detail == {"section": "apify", "keys": ["APIFY_TOKEN"]}
        assert SECRET not in str(entry)
        again = browser.post(
            "/admin/connections/apify", {**CONFIRM, "APIFY_TOKEN": ""}, token=token
        )
        assert ctx.settings.apify_token.get_secret_value() == SECRET and again.status_code == 200
        cleared = browser.post(
            "/admin/connections/apify",
            {**CONFIRM, "APIFY_TOKEN": "", "clear_APIFY_TOKEN": "yes"},
            token=token,
        )
        assert cleared.status_code == 200 and ctx.settings.apify_token is None
        assert "APIFY_TOKEN" not in env.read_text(encoding="utf-8")


def test_invalid_values_are_rejected_without_touching_the_file_or_services(tmp_path, monkeypatch):
    app, _, env = boot(tmp_path, monkeypatch)
    with open_client(app) as client:
        browser = Browser(client)
        ctx = app.state.ctx
        add_user(app, "root")
        token = browser.signed_in_token("root", path="/admin/connections")
        before = ctx.services
        bad = browser.post(
            "/admin/connections/mail", {**CONFIRM, "SMTP_PORT": "99999"}, token=token
        )
        assert bad.status_code == 400 and "smtp_port" in bad.text
        both = browser.post(
            "/admin/connections/llm",
            {**CONFIRM, "LLM_API_KEY": "sk-x", "LLM_OLLAMA_HOST": "localhost"},
            token=token,
        )
        assert both.status_code == 400 and "Both" in both.text
        assert not env.exists() and ctx.services is before
        assert (
            browser.post(
                "/admin/connections/nothing",
                {
                    **CONFIRM,
                },
                token=token,
            ).status_code
            == 404
        )


def test_changes_wait_while_searches_are_running(tmp_path, monkeypatch):
    app, _, _ = boot(tmp_path, monkeypatch)
    with open_client(app) as client:
        browser = Browser(client)
        ctx = app.state.ctx
        add_user(app, "root")
        token = browser.signed_in_token("root", path="/admin/connections")
        monkeypatch.setattr(ctx.jobs, "busy", lambda: True)
        blocked = browser.post(
            "/admin/connections/apify", {**CONFIRM, "APIFY_TOKEN": SECRET}, token=token
        )
        assert blocked.status_code == 400 and "Searches are running" in blocked.text
        assert ctx.jobs.ready is True


def test_a_failing_restart_puts_the_previous_services_back(tmp_path, monkeypatch):
    app, stub, env = boot(tmp_path, monkeypatch)
    with open_client(app) as client:
        browser = Browser(client)
        ctx = app.state.ctx
        add_user(app, "root")
        token = browser.signed_in_token("root", path="/admin/connections")
        calls = []

        @asynccontextmanager
        async def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise ConfigError("the new connection cannot start")
            yield StubServices(stub)

        ctx.host._factory = flaky
        failed = browser.post(
            "/admin/connections/apify", {**CONFIRM, "APIFY_TOKEN": SECRET}, token=token
        )
        assert failed.status_code == 400 and "cannot start" in failed.text
        assert ctx.services is not None and ctx.jobs.ready
        assert ctx.settings.apify_token is None
        assert not env.exists() or SECRET not in env.read_text(encoding="utf-8")


def test_a_test_message_goes_through_the_logged_mailer(tmp_path, monkeypatch):
    app, _, _ = boot(tmp_path, monkeypatch)
    with open_client(app) as client:
        browser = Browser(client)
        ctx = app.state.ctx
        add_user(app, "root", email="root@example.com")
        sent = []
        ctx.mailer._transport = lambda settings, message: sent.append(message)
        token = browser.signed_in_token("root", path="/admin/connections")
        form = {
            "SMTP_HOST": "mail.example.com",
            "SMTP_PORT": "587",
            "SMTP_SECURITY": "starttls",
            "SMTP_FROM": "hiring@example.com",
            "SMTP_PASSWORD": "hunter2 with space",
            "test_to": "root@example.com",
            **CONFIRM,
        }
        response = browser.post("/admin/connections/mail", form, token=token)
        assert response.status_code == 200 and "test message was sent" in response.text
        assert len(sent) == 1 and sent[0]["To"] == "root@example.com"
        assert "hunter2" not in response.text
        assert ctx.mailer.configured()
        elsewhere = browser.post(
            "/admin/connections/test", {"target": "mail", "test_to": "not-an-address"}, token=token
        )
        assert "does not look right" in elsewhere.text
        assert (
            browser.post("/admin/connections/test", {"target": "bogus"}, token=token).status_code
            == 404
        )


def test_the_check_button_reports_a_failure_in_plain_words(tmp_path, monkeypatch):
    app, _, _ = boot(tmp_path, monkeypatch)
    with open_client(app) as client:
        browser = Browser(client)
        ctx = app.state.ctx
        add_user(app, "root")
        token = browser.signed_in_token("root", path="/admin/connections")
        browser.post("/admin/connections/apify", {**CONFIRM, "APIFY_TOKEN": SECRET}, token=token)
        ctx.checker = ConnectionChecker(
            lambda s: mock_client(lambda r: json_response({"e": 1}, 401))
        )
        result = browser.post("/admin/connections/test", {"target": "llm"}, token=token)
        assert "Did not work" in result.text and "inside the Apify platform" in result.text
        assert os.environ.get("APIFY_TOKEN") is None
        assert PASSWORD not in result.text


def test_saving_needs_the_account_password_again(tmp_path, monkeypatch):
    app, _, env = boot(tmp_path, monkeypatch)
    with open_client(app) as client:
        browser = Browser(client)
        app.state.ctx.checker.check = fine
        add_user(app, "root")
        token = browser.signed_in_token("root", path="/admin/connections")
        refused = browser.post("/admin/connections/apify", {"APIFY_TOKEN": SECRET}, token=token)
        assert refused.status_code == 400 and "not correct" in refused.text
        wrong = browser.post(
            "/admin/connections/apify",
            {"APIFY_TOKEN": SECRET, "confirm_password": PASSWORD + "x"},
            token=token,
        )
        assert wrong.status_code == 400 and not env.exists()
        done = browser.post(
            "/admin/connections/apify", {"APIFY_TOKEN": SECRET, **CONFIRM}, token=token
        )
        assert done.status_code == 200 and SECRET in env.read_text(encoding="utf-8")


def test_a_saved_secret_does_not_follow_a_changed_service_address(tmp_path, monkeypatch):
    app, _, env = boot(tmp_path, monkeypatch)
    with open_client(app) as client:
        browser = Browser(client)
        ctx = app.state.ctx
        app.state.ctx.checker.check = fine
        add_user(app, "root")
        token = browser.signed_in_token("root", path="/admin/connections")
        first = {
            "SMTP_HOST": "mail.example.com",
            "SMTP_PORT": "587",
            "SMTP_SECURITY": "starttls",
            "SMTP_FROM": "hiring@example.com",
            "SMTP_USERNAME": "robot",
            "SMTP_PASSWORD": "hunter2 with space",
            **CONFIRM,
        }
        assert browser.post("/admin/connections/mail", first, token=token).status_code == 200
        assert ctx.settings.smtp_password.get_secret_value() == "hunter2 with space"
        same = {key: value for key, value in first.items() if key != "SMTP_PASSWORD"}
        kept = browser.post("/admin/connections/mail", same, token=token)
        assert kept.status_code == 200 and ctx.settings.smtp_password is not None
        moved = {**same, "SMTP_HOST": "attacker.example.net"}
        result = browser.post("/admin/connections/mail", moved, token=token)
        assert result.status_code == 200 and "was removed because" in result.text
        assert ctx.settings.smtp_password is None and "hunter2" not in env.read_text("utf-8")
        again = {**moved, "SMTP_PASSWORD": "fresh password 1"}
        assert browser.post("/admin/connections/mail", again, token=token).status_code == 200
        assert ctx.settings.smtp_password.get_secret_value() == "fresh password 1"
        llm = {"LLM_PROVIDER": "openai", "LLM_API_KEY": "sk-secret-key-value", **CONFIRM}
        assert browser.post("/admin/connections/llm", llm, token=token).status_code == 200
        redirected = {
            "LLM_PROVIDER": "openai",
            "LLM_BASE_URL": "https://evil.example/v1",
            **CONFIRM,
        }
        result = browser.post("/admin/connections/llm", redirected, token=token)
        assert result.status_code == 400
        assert ctx.settings.llm_api_key is not None and ctx.settings.llm_base_url is None
        assert "evil.example" not in env.read_text("utf-8")


def test_the_mail_server_and_the_sign_in_rules_belong_to_full_administrators(tmp_path, monkeypatch):
    app, _, env = boot(tmp_path, monkeypatch)
    with open_client(app) as client:
        browser = Browser(client)
        ctx = app.state.ctx
        app.state.ctx.checker.check = fine
        add_user(app, "root")
        ctx.accounts.save_role("Config only", "", ["config.manage", "users.view"])
        add_user(app, "cfg", role="Config only")
        token = browser.signed_in_token("cfg", path="/admin/connections")
        mail = {
            "SMTP_HOST": "mail.attacker.example",
            "SMTP_FROM": "boss@example.com",
            "SMTP_SECURITY": "starttls",
            **CONFIRM,
        }
        refused = browser.post("/admin/connections/mail", mail, token=token)
        assert refused.status_code == 403 and "manage everything" in refused.text
        assert not env.exists()
        apify = browser.post(
            "/admin/connections/apify", {"APIFY_TOKEN": SECRET, **CONFIRM}, token=token
        )
        assert apify.status_code == 200
        security = {name: "yes" for name, *_ in POLICY_FIELDS if name != "require_totp"}
        token = browser.csrf("/admin/security")
        denied = browser.post("/admin/security", security, token=token)
        assert denied.status_code == 403 and "manage everything" in denied.text
        harmless = {
            name: ("yes" if getattr(ctx.policy.get(), name) else "no") for name, *_ in POLICY_FIELDS
        }
        harmless["candidates_auto_record"] = "no"
        allowed = browser.post("/admin/security", harmless, token=token)
        assert allowed.status_code == 303
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.signed_in_token("root", path="/admin/security")
        assert (
            browser.post(
                "/admin/security",
                {"allow_password_login": "yes", "email_code_login": "yes"},
                token=token,
            ).status_code
            == 400
        )
