# tests/test_web_emails.py
from tests.web_helpers import (
    Browser,
    build_app,
    open_client,
    ORIGIN,
    ScriptedResearcher,
    start,
    wait_for,
)
from agent.web.emails import (
    candidate_names,
    slug,
)
from agent.drafts import DraftCategory
from agent.config import EmailTuning
from tests.fakes import skill_report

import json

FORM = {
    "template_id": "",
    "name": "Warm intro",
    "category": "single",
    "purpose": "outreach",
    "subject": "Hello {{ candidate.first_name }}",
    "body": "Hi {{ candidate.first_name }}, we are {{ sender.company }}.",
}


def session(tmp_path, **overrides):
    app, stub = build_app(tmp_path, ScriptedResearcher(), **overrides)
    return app, stub


def preview(client, token, body, origin=ORIGIN):
    headers = {"origin": origin, "x-csrf-token": token}
    return client.post("/emails/preview", json=body, headers=headers)


def guarded(token):
    return {"origin": ORIGIN, "x-csrf-token": token}


def draft_body(**extra):
    return {
        "category": "single",
        "purpose": "outreach",
        "subject": "{{ role.title }}",
        "body": "Hi {{ candidate.first_name }}",
        **extra,
    }


def finished_job(browser, token):
    location = start(browser, token)
    wait_for(browser, location)
    return location.removeprefix("/jobs/")


def test_slugs_are_lowercase_dashed_and_unique():
    assert slug("Warm Intro!", set()) == "warm-intro"
    assert slug("Warm Intro!", {"warm-intro"}) == "warm-intro-2"
    assert slug("Warm Intro!", {"warm-intro", "warm-intro-2"}) == "warm-intro-3"
    assert slug("!!!", set()) == "template"
    assert slug("ab", set()) == "ab-draft"
    assert len(slug("x" * 100, set())) <= 34


def test_candidate_names_follow_the_category():
    tuning = EmailTuning(max_batch=3)
    typed = ["A", "B", "C", "D"]
    assert candidate_names(DraftCategory.BATCH, None, typed, tuning) == ["A", "B", "C"]
    assert candidate_names(DraftCategory.SINGLE, None, typed, tuning) == ["A"]
    assert candidate_names(DraftCategory.TEAM, None, typed, tuning) == ["A"]
    assert candidate_names(DraftCategory.SINGLE, None, [], tuning) == ["Candidate"]
    shortlist = skill_report()
    assert candidate_names(DraftCategory.BATCH, shortlist, [], tuning) == ["Jan Novak"]
    assert candidate_names(DraftCategory.SINGLE, shortlist, [], tuning) == ["Jan Novak"]


def test_the_page_lists_builtin_templates_by_category(tmp_path):
    app, _ = session(tmp_path)
    with open_client(app) as client:
        Browser(client).logged_in_token()
        page = client.get("/emails")
        batch = client.get("/emails?category=batch")
    assert page.status_code == 200
    for expected in (
        "Reach out to one candidate",
        "Reach out to a group of candidates",
        "Share a candidate report with the team",
        "Text only",
    ):
        assert expected in page.text
    assert "Reach out to one candidate" not in batch.text
    assert "Reach out to a group of candidates" in batch.text


def test_the_page_never_offers_to_send_anything(tmp_path):
    app, _ = session(tmp_path)
    with open_client(app) as client:
        Browser(client).logged_in_token()
        text = client.get("/emails").text.lower()
    for forbidden in ("smtp", "send now", "mailto:", 'type="email"', "recipient address"):
        assert forbidden not in text


def test_the_emails_page_needs_a_login(tmp_path):
    app, _ = session(tmp_path)
    with open_client(app) as client:
        response = client.get("/emails")
    assert response.status_code == 303 and response.headers["location"] == "/login"


def test_editing_a_builtin_template_offers_a_copy(tmp_path):
    app, _ = session(tmp_path)
    with open_client(app) as client:
        Browser(client).logged_in_token()
        page = client.get("/emails?edit=outreach-single").text
    assert 'value="Reach out to one candidate (copy)"' in page
    assert '<input type="hidden" name="template_id" value="">' in page
    assert "Built-in templates are read only" in page


def test_editing_a_custom_template_keeps_its_identity(tmp_path):
    app, _ = session(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        browser.post("/emails/templates", FORM, token=token)
        page = client.get("/emails?edit=warm-intro").text
    assert '<input type="hidden" name="template_id" value="warm-intro">' in page
    assert 'value="Warm intro"' in page


def test_an_unknown_template_to_edit_shows_the_blank_editor(tmp_path):
    app, _ = session(tmp_path)
    with open_client(app) as client:
        Browser(client).logged_in_token()
        assert client.get("/emails?edit=nothing-here").status_code == 200


def test_saving_creates_a_custom_template_and_redirects_to_it(tmp_path):
    app, _ = session(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        response = browser.post("/emails/templates", FORM, token=token)
        assert response.status_code == 303
        assert response.headers["location"] == "/emails?edit=warm-intro&saved=1"
        page = client.get(response.headers["location"])
        listing = client.get("/emails").text
    assert "Warm intro" in listing and "The template was saved." in page.text
    assert (tmp_path / "email_templates" / "warm-intro.json").is_file()


def test_saving_the_same_name_twice_makes_a_second_template(tmp_path):
    app, _ = session(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        browser.post("/emails/templates", FORM, token=token)
        second = browser.post("/emails/templates", FORM, token=token)
    assert second.headers["location"] == "/emails?edit=warm-intro-2&saved=1"


def test_saving_an_existing_custom_template_replaces_it(tmp_path):
    app, _ = session(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        browser.post("/emails/templates", FORM, token=token)
        changed = {**FORM, "template_id": "warm-intro", "name": "Warm intro v2"}
        response = browser.post("/emails/templates", changed, token=token)
        listing = client.get("/emails").text
    assert response.headers["location"] == "/emails?edit=warm-intro&saved=1"
    assert "Warm intro v2" in listing
    assert len(list((tmp_path / "email_templates").glob("*.json"))) == 1


def test_saving_a_copy_of_a_builtin_template_creates_a_new_one(tmp_path):
    app, _ = session(tmp_path)
    form = {**FORM, "template_id": "", "name": "Reach out to one candidate"}
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        response = browser.post("/emails/templates", form, token=token)
    assert response.headers["location"] == "/emails?edit=reach-out-to-one-candidate&saved=1"


def test_a_builtin_template_cannot_be_overwritten_by_id(tmp_path):
    app, _ = session(tmp_path)
    form = {**FORM, "template_id": "outreach-single"}
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        response = browser.post("/emails/templates", form, token=token)
    assert response.status_code == 303
    assert "outreach-single" not in response.headers["location"]
    assert not (tmp_path / "email_templates" / "outreach-single.json").exists()


def test_unsafe_templates_are_rejected_with_a_message_and_keep_the_text(tmp_path):
    app, _ = session(tmp_path)
    form = {**FORM, "body": "{% include 'secret' %} typed text"}
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        response = browser.post("/emails/templates", form, token=token)
    assert response.status_code == 400
    assert "may not use these tags" in response.text
    assert "typed text" in response.text
    assert not (tmp_path / "email_templates").exists()


def test_templates_with_unknown_placeholders_are_rejected(tmp_path):
    app, _ = session(tmp_path)
    form = {**FORM, "body": "Hi {{ candidate.nickname }}"}
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        response = browser.post("/emails/templates", form, token=token)
    assert response.status_code == 400 and "could not be rendered" in response.text


def test_a_missing_name_or_bad_choice_is_a_form_error(tmp_path):
    app, _ = session(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        for update in ({"name": ""}, {"category": "everyone"}, {"purpose": "spam"}):
            response = browser.post("/emails/templates", {**FORM, **update}, token=token)
            assert response.status_code == 400, update


def test_saving_needs_a_valid_form_token(tmp_path):
    app, _ = session(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        browser.logged_in_token()
        assert browser.post("/emails/templates", FORM).status_code == 403
        assert browser.post("/emails/templates", FORM, token="wrong").status_code == 403
        token = browser.csrf("/emails")
        cross = browser.post(
            "/emails/templates",
            FORM,
            token=token,
            origin=None,
            headers={"sec-fetch-site": "cross-site"},
        )
        assert cross.status_code == 403


def test_the_template_limit_is_reported(tmp_path):
    app, _ = build_app(tmp_path, ScriptedResearcher())
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        folder = tmp_path / "email_templates"
        folder.mkdir()
        for number in range(60):
            payload = {**FORM, "id": f"filler-{number:02d}", "name": "Filler"}
            payload.pop("template_id")
            (folder / f"filler-{number:02d}.json").write_text(json.dumps(payload))
        response = browser.post("/emails/templates", FORM, token=token)
    assert response.status_code == 400 and "limit" in response.text


def test_deleting_removes_a_custom_template(tmp_path):
    app, _ = session(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        browser.post("/emails/templates", FORM, token=token)
        response = browser.post("/emails/templates/warm-intro/delete", token=token)
        again = browser.post("/emails/templates/warm-intro/delete", token=token)
    assert response.status_code == 303 and response.headers["location"] == "/emails"
    assert again.status_code == 404
    assert not (tmp_path / "email_templates" / "warm-intro.json").exists()


def test_builtin_templates_cannot_be_deleted(tmp_path):
    app, _ = session(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        assert (
            browser.post("/emails/templates/outreach-single/delete", token=token).status_code == 404
        )
        assert browser.post("/emails/templates/x..y/delete", token=token).status_code == 404
        assert browser.post("/emails/templates/warm-intro/delete").status_code == 403


def test_the_preview_renders_drafts_without_saving(tmp_path):
    app, _ = session(tmp_path)
    body = draft_body(subject="Hello {{ candidate.first_name }}", names=["Jan Novak"])
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        response = preview(client, token, body)
    assert response.status_code == 200
    (draft,) = response.json()["drafts"]
    assert draft["recipient"] == "Jan Novak" and draft["subject"] == "Hello Jan"
    assert draft["body"].startswith("Hi Jan\n\n--\n")
    assert not (tmp_path / "email_templates").exists()


def test_a_batch_preview_drafts_every_typed_name(tmp_path):
    app, _ = session(tmp_path)
    body = draft_body(category="batch", names=["Jan Novak", "  Eva   Svobodova ", "", 7, "Petr"])
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        response = preview(client, token, body)
    drafts = response.json()["drafts"]
    assert [item["recipient"] for item in drafts] == ["Jan Novak", "Eva Svobodova", "Petr"]
    assert [item["body"].split("\n")[0] for item in drafts] == ["Hi Jan", "Hi Eva", "Hi Petr"]


def test_a_single_preview_only_drafts_the_first_name(tmp_path):
    app, _ = session(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        response = preview(client, token, draft_body(names=["Jan Novak", "Eva Svobodova"]))
    assert len(response.json()["drafts"]) == 1


def test_the_preview_uses_the_report_of_a_finished_search(tmp_path):
    app, _ = session(tmp_path)
    body = draft_body(
        category="team",
        purpose="report",
        subject="{{ candidate.name }}",
        body="Rating {{ report.overall }}, {{ report.findings_count }} findings",
    )
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        job_id = finished_job(browser, token)
        response = preview(client, token, {**body, "job": job_id})
        without = preview(client, token, body)
    (draft,) = response.json()["drafts"]
    assert draft["subject"] == "Jan Novak"
    assert draft["body"].startswith("Rating 3.6, 5 findings")
    assert "Rating 3.6" not in without.json()["drafts"][0]["body"]
    assert "Rating , 0 findings" in without.json()["drafts"][0]["body"]


def test_a_job_started_through_the_api_is_never_used_for_a_preview(tmp_path):
    app, _ = build_app(
        tmp_path, ScriptedResearcher(), api_token="api-token-for-the-test-suite-only"
    )
    body = draft_body(
        category="team",
        purpose="report",
        subject="s",
        body="Rating {{ report.overall }}",
    )
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        started = client.post(
            "/api/v1/research",
            json={"goal": "hiring", "name": "Eva Svobodova", "lawful_purpose": True},
            headers={"authorization": "Bearer api-token-for-the-test-suite-only"},
        ).json()
        for _ in range(200):
            state = client.get(
                f"/api/v1/jobs/{started['id']}",
                headers={"authorization": "Bearer api-token-for-the-test-suite-only"},
            ).json()["state"]
            if state == "done":
                break
        response = preview(client, token, {**body, "job": started["id"]})
    assert "3.6" not in response.json()["drafts"][0]["body"]


def test_the_preview_reports_invalid_input_as_json_errors(tmp_path):
    app, _ = session(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        not_json = client.post("/emails/preview", content=b"nope", headers=guarded(token))
        not_object = client.post("/emails/preview", content=b"[1]", headers=guarded(token))
        bad_category = preview(client, token, draft_body(category="everyone"))
        bad_purpose = preview(client, token, draft_body(purpose="spam"))
        forbidden = preview(client, token, draft_body(body="{% include 'x' %}"))
        unknown = preview(client, token, draft_body(body="{{ nothing.here }}"))
    for response in (not_json, not_object, bad_category, bad_purpose, forbidden, unknown):
        assert response.status_code == 422, response.text
        assert response.json()["error"]
    assert "may not use these tags" in forbidden.json()["error"]


def test_the_preview_is_protected_like_every_other_post(tmp_path):
    app, _ = session(tmp_path)
    with open_client(app) as client:
        anonymous = client.post("/emails/preview", json=draft_body(), headers={"origin": ORIGIN})
        browser = Browser(client)
        token = browser.logged_in_token()
        no_token = client.post("/emails/preview", json=draft_body(), headers={"origin": ORIGIN})
        wrong_origin = preview(client, token, draft_body(), origin="https://evil.example")
    assert anonymous.status_code == 303
    assert no_token.status_code == 403 and wrong_origin.status_code == 403


def test_the_sender_and_company_come_from_the_saved_settings(tmp_path):
    app, _ = session(tmp_path)
    settings = {
        "theme": "system",
        "density": "comfortable",
        "live_view": "chat",
        "goal": "hiring",
        "source": ["web"],
        "skill_limit": "5",
        "strictness": "balanced",
        "scepticism": "standard",
        "company_name": "Globex",
        "sender_name": "Eva Svobodova",
    }
    body = draft_body(subject="x", body="From {{ sender.name }} at {{ sender.company }}")
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        browser.post("/settings", settings, token=token)
        response = preview(client, token, body)
    assert response.json()["drafts"][0]["body"].startswith("From Eva Svobodova at Globex")


def test_the_template_list_shows_a_rendered_example_not_template_source(tmp_path):
    app, _ = session(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        browser.post(
            "/emails/templates",
            {**FORM, "subject": "Hello {{ candidate.first_name }}"},
            token=token,
        )
        page = client.get("/emails").text
    assert "<small>Senior Python engineer at Acme</small>" in page
    assert "<small>Hello Jan</small>" in page
    assert "{% if company.name %}" not in page.split('id="editor"')[0]
