# tests/test_candidates_web.py
from tests.web_helpers import (
    add_user,
    Browser,
    build_app,
    open_client,
    start,
    wait_for,
)
from tests.fakes import (
    hiring_report,
    person_report,
)

import re


def boot(tmp_path):
    app, stub = build_app(tmp_path, submissions=50, chat_requests=500, max_jobs_per_session=9)
    return app, stub


def research(browser, token, text="Jan Novak in Brno"):
    location = start(browser, token, text)
    wait_for(browser, location)
    return location


def candidate_path(client):
    page = client.get("/candidates").text
    found = re.search(r'href="(/candidates/[A-Za-z0-9_-]{8,})"', page)
    assert found, page[:800]
    return found.group(1)


def test_a_finished_search_adds_the_person_and_the_next_one_says_seen_before(tmp_path):
    app, stub = boot(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        assert "No candidates yet" in client.get("/candidates").text
        stub.results = [person_report(), hiring_report()]
        first = research(browser, token)
        assert "Seen before" not in client.get(first).text
        page = client.get("/candidates").text
        assert "Jan Novak" in page and "1 person" in page
        second = research(browser, token)
        report = client.get(second).text
        assert "Seen before" in report and "researched 1 time before" in report
        detail = client.get(candidate_path(client)).text
        assert "Researched: " in detail and "Open the report" in detail
        assert app.state.ctx.candidates.count() == 1


def test_auto_recording_and_the_module_can_be_switched_off(tmp_path):
    app, _ = boot(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        ctx = app.state.ctx
        ctx.policy.update(candidates_auto_record=False)
        research(browser, token)
        assert ctx.candidates.count() == 0
        ctx.policy.update(candidates_auto_record=True, candidates_enabled=False)
        research(browser, token)
        assert ctx.candidates.count() == 0
        assert client.get("/candidates").status_code == 404
        assert 'href="/candidates"' not in client.get("/settings").text


def test_people_see_only_what_their_role_allows(tmp_path):
    app, _ = boot(tmp_path)
    with open_client(app) as boss, open_client(app) as viewer, open_client(app) as outsider:
        owner = Browser(boss)
        token = owner.logged_in_token()
        research(owner, token)
        ctx = app.state.ctx
        person = ctx.candidates.page().items[0]
        ctx.candidates.add_contact(person.id, "email", "jan@example.com")
        add_user(app, "viewer", role="viewer")
        add_user(app, "finance", role="finance")
        see = Browser(viewer)
        see_token = see.signed_in_token("viewer")
        assert viewer.get("/candidates").status_code == 200
        page = viewer.get(f"/candidates/{person.id}").text
        assert "jan@example.com" not in page and "Contact details" not in page
        assert "Record a hire" not in page and 'name="headline"' not in page
        for method, path in (
            ("post", f"/candidates/{person.id}"),
            ("post", f"/candidates/{person.id}/hires"),
            ("post", f"/candidates/{person.id}/contacts"),
            ("post", f"/candidates/{person.id}/delete"),
            ("get", f"/candidates/{person.id}/export"),
            ("get", "/candidates/new"),
        ):
            response = (
                see.post(path, {"name": "x"}, token=see_token)
                if method == "post"
                else viewer.get(path)
            )
            assert response.status_code == 404, path
        Browser(outsider).signed_in_token("finance")
        assert outsider.get("/candidates").status_code == 404
        assert outsider.get(f"/candidates/{person.id}").status_code == 404
        assert ctx.candidates.get(person.id) is not None


def test_a_recruiter_adds_edits_and_hires_with_contacts_imported(tmp_path):
    app, _ = boot(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        ctx = app.state.ctx
        add_user(app, "rec", role="recruiter")
        token = browser.signed_in_token("rec", path="/candidates/new")
        bad = browser.post("/candidates", {"name": "x"}, token=token)
        assert bad.status_code == 400 and "needs a name" in bad.text
        made = browser.post(
            "/candidates",
            {
                "name": "Eva Svobodova",
                "headline": "Data engineer",
                "skills": "python, sql",
                "tags": "referral",
                "status": "interviewing",
                "link": "https://www.linkedin.com/in/eva",
                "notes": "Met at a meetup",
            },
            token=token,
        )
        assert made.status_code == 303
        path = made.headers["location"].split("?")[0]
        person = ctx.candidates.page().items[0]
        assert person.status == "interviewing" and person.skills == ["python", "sql"]
        assert "Eva Svobodova" in client.get(path).text
        page_token = browser.csrf(path)
        edit = browser.post(
            path, {"name": "Eva S.", "status": "offer", "notes": "Offer sent"}, token=page_token
        )
        assert edit.status_code == 303 and ctx.candidates.get(person.id).status == "offer"
        wrong = browser.post(
            f"{path}/hires", {"role_title": "Analyst", "email": "bad"}, token=page_token
        )
        assert wrong.status_code == 400 and "email address" in wrong.text
        assert ctx.candidates.hires(person.id) == []
        hired = browser.post(
            f"{path}/hires",
            {
                "role_title": "Analyst",
                "department": "Data",
                "started": "2026-03-01",
                "email": "eva@example.com",
                "phone": "+420 601 234 567",
            },
            token=page_token,
        )
        assert hired.status_code == 303
        assert ctx.candidates.get(person.id).status == "hired"
        assert {item.kind for item in ctx.candidates.contacts(person.id)} == {"email", "phone"}
        detail = client.get(path).text
        assert "eva@example.com" in detail and "Hired as Analyst" in detail
        assert "Yes, 1 time" in detail
        hire = ctx.candidates.hires(person.id)[0]
        update = browser.post(
            f"{path}/hires/{hire.id}",
            {
                "role_title": "Analyst",
                "outcome": "left",
                "rating": "4",
                "review": "Solid",
                "ended": "2026-09-01",
            },
            token=page_token,
        )
        assert update.status_code == 303
        assert ctx.candidates.hires(person.id)[0].rating == 4
        assert ctx.candidates.get(person.id).status == "alumni"
        gone = browser.post(f"{path}/delete", token=page_token)
        assert gone.status_code == 404
        assert "candidate.create" in {entry.action for entry in ctx.accounts.audit()}


def test_deleting_and_exporting_need_their_own_permissions(tmp_path):
    app, _ = boot(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        ctx = app.state.ctx
        add_user(app, "boss", role="admin")
        person = ctx.candidates.create("Eva Svobodova")
        ctx.candidates.add_contact(person.id, "email", "eva@example.com")
        token = browser.signed_in_token("boss", path=f"/candidates/{person.id}")
        export = client.get(f"/candidates/{person.id}/export")
        assert export.status_code == 200
        payload = export.json()
        assert payload["candidate"]["name"] == "Eva Svobodova"
        assert payload["contacts"][0]["value"] == "eva@example.com"
        deleted = browser.post(f"/candidates/{person.id}/delete", token=token)
        assert deleted.status_code == 303 and ctx.candidates.get(person.id) is None
        assert client.get(f"/candidates/{person.id}").status_code == 404


def test_the_report_page_adds_the_person_to_the_candidates(tmp_path):
    app, _ = boot(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        ctx = app.state.ctx
        token = browser.logged_in_token()
        ctx.policy.update(candidates_auto_record=False)
        location = research(browser, token)
        assert "Add to candidates" in client.get(location).text
        added = browser.post(f"{location}/candidate", token=token)
        assert added.status_code == 303 and ctx.candidates.count() == 1
        missing = browser.post("/jobs/nothing/candidate", token=token)
        assert missing.status_code == 404


def test_the_list_filters_and_pages(tmp_path):
    app, _ = boot(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        ctx = app.state.ctx
        browser.logged_in_token()
        for index in range(30):
            ctx.candidates.create(f"Person {index:02d}", employer="Acme" if index % 2 else "Globex")
        first = client.get("/candidates?sort=name").text
        assert "Person 00" in first and "Person 29" not in first and "Page 1 of 2" in first
        second = client.get("/candidates?sort=name&page=2").text
        assert "Person 29" in second and "Page 2 of 2" in second
        filtered = client.get("/candidates?q=globex").text
        assert "Person 00" in filtered and "Person 01" not in filtered
        assert client.get("/candidates?page=abc&min=x&status=zzz&hired=maybe").status_code == 200
        assert "must be a number" in client.get("/candidates?min=x").text
