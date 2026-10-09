# tests/test_outbox_web.py
from tests.web_helpers import (
    add_user,
    Browser,
    build_app,
    open_client,
    start,
    wait_for,
)
from agent.errors import UpstreamError
from tests.fakes import hiring_report

import re


def enable_mail(app, **changes):
    ctx = app.state.ctx
    sent = []
    settings = ctx.settings.model_copy(
        update={"smtp_host": "mail.example.com", "smtp_from": "hiring@example.com", **changes}
    )
    ctx.settings = settings
    ctx.mailer.use(settings)
    ctx.mailer._transport = lambda config, message: sent.append(message)
    ctx.policy.update(email_sending_enabled=True)
    return sent


def boot(tmp_path):
    app, stub = build_app(tmp_path, submissions=50, chat_requests=500, max_jobs_per_session=9)
    stub.results = [hiring_report()]
    return app, stub


def test_sending_is_hidden_until_it_is_switched_on_and_connected(tmp_path):
    app, _ = boot(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        add_user(app, "rec", role="recruiter")
        browser.signed_in_token("rec")
        assert client.get("/emails/compose").status_code == 404
        assert "Text only: nothing is sent" in client.get("/emails").text
        app.state.ctx.policy.update(email_sending_enabled=True)
        assert client.get("/emails/compose").status_code == 200
        assert "Email is not connected yet" in client.get("/emails/compose").text
        enable_mail(app)
        assert "Nothing is sent unless you confirm" in client.get("/emails").text


def test_a_viewer_cannot_compose_or_send_but_a_recruiter_can(tmp_path):
    app, _ = boot(tmp_path)
    with open_client(app) as one, open_client(app) as two:
        sent = enable_mail(app)
        add_user(app, "viewer", role="viewer")
        add_user(app, "rec", role="recruiter")
        viewer, rec = Browser(one), Browser(two)
        view_token = viewer.signed_in_token("viewer")
        rec_token = rec.signed_in_token("rec")
        fields = {"to": "a@example.com", "subject": "Hi", "body": "Hello", "confirm": "yes"}
        assert one.get("/emails/compose").status_code == 404
        assert viewer.post("/emails/send", fields, token=view_token).status_code == 404
        assert (
            viewer.post(
                "/emails/suppress", {"address": "a@example.com"}, token=view_token
            ).status_code
            == 404
        )
        assert sent == []
        ok = rec.post("/emails/send", fields, token=rec_token)
        assert ok.status_code == 303 and ok.headers["location"].startswith("/emails/log?sent=")
        assert len(sent) == 1


def test_a_message_needs_confirmation_a_valid_address_and_gets_the_opt_out_line(tmp_path):
    app, _ = boot(tmp_path)
    with open_client(app) as client:
        sent = enable_mail(app)
        browser = Browser(client)
        add_user(app, "rec", role="recruiter")
        token = browser.signed_in_token("rec", path="/emails/compose")
        fields = {
            "to": "jan@example.com",
            "subject": "Python role",
            "body": "Hello Jan,\n\nA role.",
        }
        unconfirmed = browser.post("/emails/send", fields, token=token)
        assert unconfirmed.status_code == 400 and "Tick the box" in unconfirmed.text
        assert "Hello Jan" in unconfirmed.text and sent == []
        bad = browser.post("/emails/send", {**fields, "to": "nope", "confirm": "yes"}, token=token)
        assert bad.status_code == 502 and "does not look right" in bad.text
        injected = browser.post(
            "/emails/send",
            {**fields, "subject": "Hi\r\nBcc: spy@example.com", "confirm": "yes"},
            token=token,
        )
        assert injected.status_code == 303
        message = sent[0]
        assert message["Subject"] == "Hi Bcc: spy@example.com" and message["Bcc"] is None
        assert message["To"] == "jan@example.com" and message["From"] == "hiring@example.com"
        body = message.get_content()
        assert body.startswith("Hello Jan") and "reply and say so" in body
        log = client.get("/emails/log?sent=1")
        assert "The message was sent" in log.text and "jan@example.com" in log.text


def test_people_who_asked_to_stop_are_never_written_to(tmp_path):
    app, _ = boot(tmp_path)
    with open_client(app) as client:
        sent = enable_mail(app)
        browser = Browser(client)
        add_user(app, "rec", role="recruiter")
        token = browser.signed_in_token("rec", path="/emails/log")
        added = browser.post(
            "/emails/suppress",
            {"address": "Jan@Example.com", "reason": "Asked to stop"},
            token=token,
        )
        assert added.status_code == 303
        assert "jan@example.com" in client.get("/emails/log").text.lower()
        fields = {"to": "jan@example.com", "subject": "Hi", "body": "Hello", "confirm": "yes"}
        refused = browser.post("/emails/send", fields, token=token)
        assert refused.status_code == 502 and "asked not to be contacted" in refused.text
        assert sent == []
        status = app.state.ctx.mailer.entries()[0].status
        assert status == "blocked"
        bad = browser.post("/emails/suppress", {"address": "nope"}, token=token)
        assert bad.status_code == 400 and "does not look right" in bad.text
        browser.post("/emails/suppress/remove", {"address": "jan@example.com"}, token=token)
        assert browser.post("/emails/send", fields, token=token).status_code == 303


def test_the_hourly_limits_stop_a_flood(tmp_path):
    app, _ = boot(tmp_path)
    with open_client(app) as client:
        sent = enable_mail(app)
        ctx = app.state.ctx
        ctx.mail_limiter = type(ctx.mail_limiter)(2, 3600)
        browser = Browser(client)
        add_user(app, "rec", role="recruiter")
        token = browser.signed_in_token("rec", path="/emails/compose")
        fields = {"to": "a@example.com", "subject": "Hi", "body": "Hello", "confirm": "yes"}
        codes = [browser.post("/emails/send", fields, token=token).status_code for _ in range(3)]
        assert codes == [303, 303, 429] and len(sent) == 2
        assert "too fast" in browser.post("/emails/send", fields, token=token).text


def test_a_server_refusal_is_reported_and_logged_as_failed(tmp_path):
    app, _ = boot(tmp_path)
    with open_client(app) as client:
        enable_mail(app)

        def refuse(config, message):
            raise UpstreamError("The mail server refused the recipient address.")

        app.state.ctx.mailer._transport = refuse
        browser = Browser(client)
        add_user(app, "rec", role="recruiter")
        token = browser.signed_in_token("rec", path="/emails/compose")
        fields = {"to": "a@example.com", "subject": "Hi", "body": "Hello", "confirm": "yes"}
        failed = browser.post("/emails/send", fields, token=token)
        assert failed.status_code == 502 and "refused the recipient" in failed.text
        assert "Hello" in failed.text
        assert app.state.ctx.mailer.entries()[0].status == "failed"


def test_the_report_offers_a_prefilled_message_and_remembers_it_on_the_candidate(tmp_path):
    app, _ = boot(tmp_path)
    with open_client(app) as client:
        sent = enable_mail(app)
        browser = Browser(client)
        ctx = app.state.ctx
        token = browser.logged_in_token()
        location = start(browser, token)
        wait_for(browser, location)
        person = ctx.candidates.page().items[0]
        ctx.candidates.add_contact(person.id, "email", "jan@example.com")
        page = client.get(location).text
        link = re.search(r'href="(/emails/compose\?[^"]+)"', page)
        assert link, "Send by email"
        compose = client.get(link.group(1).replace("&amp;", "&"))
        assert compose.status_code == 200
        assert 'value="jan@example.com"' in compose.text and "Jan Novak" in compose.text
        fields = {
            "to": "jan@example.com",
            "subject": "Hello",
            "body": "Hi Jan",
            "confirm": "yes",
            "candidate_id": person.id,
            "job_id": location.removeprefix("/jobs/"),
        }
        assert browser.post("/emails/send", fields, token=token).status_code == 303
        assert len(sent) == 1
        summaries = [event.summary for event in ctx.candidates.events(person.id)]
        assert "Emailed: Hello" in summaries
        ctx.policy.update(email_sending_enabled=False)
        assert "Send by email" not in client.get(location).text


def test_someone_who_may_send_but_not_read_the_log_lands_back_on_the_drafts(tmp_path):
    app, _ = boot(tmp_path)
    with open_client(app) as client:
        enable_mail(app)
        app.state.ctx.accounts.save_role("Sender", "", ["emails.view", "emails.send"])
        add_user(app, "sender", role="Sender")
        browser = Browser(client)
        token = browser.signed_in_token("sender", path="/emails")
        fields = {"to": "a@example.com", "subject": "Hi", "body": "Hello", "confirm": "yes"}
        sent = browser.post("/emails/send", fields, token=token)
        assert sent.headers["location"] == "/emails?mailed=1"
        assert "The message was sent" in client.get("/emails?mailed=1").text
        assert client.get("/emails/log").status_code == 404
