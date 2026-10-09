# tests/test_web_live.py
from tests.web_helpers import (
    Browser,
    build_app,
    clarification,
    open_client,
    ORIGIN,
    ScriptedResearcher,
    start,
    wait_for,
)
from agent.models import (
    Priority,
    Requirement,
    RequirementKind,
)
from agent.chat import (
    Profile,
    save_profile,
)
from tests.fakes import skill_report
from agent.errors import AgentError

import json
import re

SETTINGS = {
    "theme": "system",
    "density": "comfortable",
    "live_view": "chat",
    "goal": "hiring",
    "source": ["web"],
    "skill_limit": "5",
    "strictness": "balanced",
    "scepticism": "standard",
}
CONTEXT = {
    "company_name": "Acme",
    "company_website": "https://acme.example",
    "company_description": "We build payment software for shops",
    "sender_name": "Eva Svobodova",
}


def scripted(tmp_path, **overrides):
    app, stub = build_app(tmp_path, ScriptedResearcher(), **overrides)
    return app, stub


def finish(browser, token, text="Jan Novak in Brno"):
    location = start(browser, token, text)
    wait_for(browser, location)
    return location


def sse(text):
    events = []
    for block in text.split("\n\n"):
        lines = block.splitlines()
        data = [line[6:] for line in lines if line.startswith("data: ")]
        if data and data[0] != "{}":
            events.append((int(lines[0][4:]), json.loads(data[0])))
    return events


def test_the_event_stream_replays_everything_and_ends(tmp_path):
    app, _ = scripted(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = finish(browser, token)
        response = client.get(f"{location}/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert response.text.endswith("event: end\ndata: {}\n\n")
    events = sse(response.text)
    assert [number for number, _ in events] == list(range(len(events)))
    assert [item["seq"] for _, item in events] == list(range(len(events)))
    kinds = {item["kind"] for _, item in events}
    assert {"company", "source", "finding", "requirement", "rating", "done"} <= kinds
    assert events[-1][1]["kind"] == "done"


def test_the_stream_can_resume_after_an_event(tmp_path):
    app, _ = scripted(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = finish(browser, token)
        everything = sse(client.get(f"{location}/events").text)
        by_header = client.get(f"{location}/events", headers={"last-event-id": "4"})
        by_query = client.get(f"{location}/events?after=7")
        junk = client.get(f"{location}/events?after=-1", headers={"last-event-id": "abc"})
    assert [number for number, _ in sse(by_header.text)] == list(range(5, len(everything)))
    assert [number for number, _ in sse(by_query.text)] == list(range(7, len(everything)))
    assert len(sse(junk.text)) == len(everything)


def test_the_header_wins_over_the_query_when_both_are_given(tmp_path):
    app, _ = scripted(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = finish(browser, token)
        response = client.get(f"{location}/events?after=2", headers={"last-event-id": "9"})
    assert sse(response.text)[0][0] == 10


def test_the_event_stream_needs_a_session_and_is_shared_by_every_signed_in_one(tmp_path):
    app, _ = scripted(tmp_path)
    with open_client(app) as owner, open_client(app) as other, open_client(app) as anonymous:
        browser = Browser(owner)
        location = finish(browser, browser.logged_in_token())
        Browser(other).logged_in_token()
        shared = other.get(f"{location}/events")
        response = anonymous.get(f"{location}/events")
    assert shared.status_code == 200 and shared.text.endswith("event: end\ndata: {}\n\n")
    assert response.status_code == 303 and response.headers["location"] == "/login"


def test_unknown_jobs_have_no_stream(tmp_path):
    app, _ = scripted(tmp_path)
    with open_client(app) as client:
        Browser(client).logged_in_token()
        assert client.get("/jobs/nope/events").status_code == 404


def test_a_failed_run_ends_its_stream_with_the_failure(tmp_path):
    app, stub = scripted(tmp_path)
    stub.results = [AgentError("The search provider refused the request.")]
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = start(browser, token)
        wait_for(browser, location)
        events = sse(client.get(f"{location}/events").text)
        page = client.get(location)
    assert events[-1][1]["kind"] == "failed"
    assert events[-1][1]["text"] == "The search provider refused the request."
    assert "The search stopped" in page.text
    assert 'data-live="' in page.text and 'data-running=""' in page.text


def test_a_question_ends_its_stream_with_a_question_event(tmp_path):
    app, stub = scripted(tmp_path)
    stub.results = [clarification()]
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = start(browser, token)
        wait_for(browser, location)
        events = sse(client.get(f"{location}/events").text)
    assert events[-1][1]["kind"] == "question"
    assert events[-1][1]["data"] == {"networks": ["linkedin"]}


def test_a_running_job_shows_the_live_card_with_its_stream_and_view(tmp_path):
    app, stub = scripted(tmp_path)
    gate = stub.hold()
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = start(browser, token)
        job_id = location.removeprefix("/jobs/")
        page = client.get(location)
        status = client.get(f"{location}/status").json()
        gate.set()
        wait_for(browser, location)
    assert f'data-live="/jobs/{job_id}/events"' in page.text
    assert 'data-running="1"' in page.text and 'data-reload="1"' in page.text
    assert 'data-view="chat"' in page.text and 'data-title="Jan Novak"' in page.text
    assert set(status) == {"state", "stage", "task"} and status["state"] == "running"
    assert 'data-task="' in page.text


def test_the_status_reports_the_current_task_of_a_finished_run(tmp_path):
    app, _ = scripted(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = finish(browser, token)
        status = client.get(f"{location}/status").json()
    assert status == {"state": "done", "stage": "Done", "task": "export"}


def test_the_finished_report_shows_company_requirements_and_replays_the_activity(tmp_path):
    app, _ = scripted(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = finish(browser, token)
        text = client.get(location).text
    assert 'id="company"' in text and "Hiring company" in text
    assert "Payments software" in text and "Acme builds card payment terminals" in text
    assert "Provided by you" in text
    assert 'id="requirements"' in text and "60% covered" in text
    assert "English (C1)" in text and "mark--full" in text and "mark--dash" in text
    assert "Must-have requirements without evidence" not in text
    assert 'data-panel="activity"' in text
    assert re.search(r'data-running="" data-reload="" data-view="graph"', text)


def test_the_saved_view_decides_how_a_running_search_is_shown(tmp_path):
    app, stub = scripted(tmp_path)
    gate = stub.hold()
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = start(browser, token)
        log = client.get(location).text
        home = client.get("/").text
        client.post(
            "/live-view",
            json={"view": "graph"},
            headers={"origin": ORIGIN, "x-csrf-token": token},
        )
        graph = client.get(location).text
        graph_home = client.get("/").text
        gate.set()
        wait_for(browser, location)
    assert 'data-view="chat"' in log and 'data-view="graph"' in graph
    assert re.search(r'<section class="chat" data-chat [^>]*data-view="chat"', home)
    assert re.search(r'<section class="chat" data-chat [^>]*data-view="graph"', graph_home)


def test_the_live_view_choice_is_validated_and_protected(tmp_path):
    app, _ = scripted(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        good = {"origin": ORIGIN, "x-csrf-token": token}
        assert client.post("/live-view", json={"view": "chat"}, headers=good).status_code == 204
        assert client.post("/live-view", json={"view": "hologram"}, headers=good).status_code == 400
        assert client.post("/live-view", json={"other": 1}, headers=good).status_code == 400
        assert client.post("/live-view", content=b"not json", headers=good).status_code == 400
        assert client.post("/live-view", content=b"[1]", headers=good).status_code == 400
        no_token = {"origin": ORIGIN}
        assert client.post("/live-view", json={"view": "chat"}, headers=no_token).status_code == 403
        wrong = {"origin": "https://evil.example", "x-csrf-token": token}
        assert client.post("/live-view", json={"view": "chat"}, headers=wrong).status_code == 403


def test_the_live_view_choice_needs_a_login(tmp_path):
    app, _ = scripted(tmp_path)
    with open_client(app) as client:
        response = client.post("/live-view", json={"view": "chat"}, headers={"origin": ORIGIN})
    assert response.status_code == 303 and response.headers["location"] == "/login"


def test_the_choice_keeps_the_other_preferences_and_the_remembered_context(tmp_path):
    app, _ = scripted(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        settings = {
            **SETTINGS,
            "theme": "dark",
            "density": "compact",
            "goal": "sales",
            "skill_limit": "4",
            "strictness": "strict",
            "scepticism": "strict",
            **CONTEXT,
        }
        assert browser.post("/settings", settings, token=token).status_code == 303
        client.post(
            "/live-view",
            json={"view": "graph"},
            headers={"origin": ORIGIN, "x-csrf-token": token},
        )
        page = client.get("/settings").text
    assert re.search(r'name="live_view" value="graph" checked', page.replace("\n", " "))
    assert 'value="Acme"' in page and 'value="Eva Svobodova"' in page
    assert 'value="strict" checked' in page


def test_the_forms_of_the_old_interface_are_gone(tmp_path):
    app, _ = scripted(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        home = client.get("/").text
        old = [
            browser.post(path, {"name": "Jan Novak"}, token=token)
            for path in ("/research", "/skill-search", "/jobs/abc/answer")
        ]
        skills = client.get("/skills")
    for marker in ("company_name", "req_label", "data-requirements", 'name="lawful"', "Advanced"):
        assert marker not in home
    assert [response.status_code for response in old] == [404, 404, 404]
    assert skills.status_code == 404


def test_a_search_without_company_or_requirements_sends_neither(tmp_path):
    app, stub = scripted(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        finish(browser, token)
    request = stub.calls[0][1]
    assert request.company is None and request.requirements == []


def test_the_remembered_company_and_requirements_reach_every_kind_of_search(tmp_path):
    app, stub = scripted(tmp_path)
    english = Requirement(
        kind=RequirementKind.LANGUAGE, label="English", level="C1", priority=Priority.MUST
    )
    python = Requirement(
        kind=RequirementKind.SKILL, label="Python", minimum_years=5, priority=Priority.NICE
    )
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        browser.post("/settings", {**SETTINGS, **CONTEXT}, token=token)
        save_profile(
            app.state.ctx.store,
            Profile(**CONTEXT, requirements=[english, python]),
        )
        finish(browser, token)
        finish(browser, token, "find Rust in Brno")
    for _, request in stub.calls:
        assert request.company.name == "Acme" and request.company.website == "https://acme.example"
        assert request.company.description == "We build payment software for shops"
        first, second = request.requirements
        assert (first.kind, first.label, first.level) == (RequirementKind.LANGUAGE, "English", "C1")
        assert first.minimum_years is None and first.priority is Priority.MUST
        assert (second.kind, second.label, second.minimum_years) == (
            RequirementKind.SKILL,
            "Python",
            5,
        )
        assert second.priority is Priority.NICE


def test_the_email_drafts_of_a_person_report_are_shown_and_never_sent(tmp_path):
    app, _ = scripted(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        browser.post("/settings", {**SETTINGS, **CONTEXT}, token=token)
        location = finish(browser, token)
        page = client.get(location).text
    assert 'id="drafts"' in page and "Email drafts" in page
    assert "Text only. Nothing is sent" in page
    assert "Hi Jan," in page and "Eva Svobodova at Acme" in page
    assert "Reply STOP and we will not write to you again." in page
    assert "Share a candidate report with the team" in page
    assert "Decision support only." in page
    assert "data-draft=" in page and "Copy all" in page
    assert re.search(r'href="/emails\?job=', page)
    lowered = page.lower()
    assert "smtp" not in lowered and "mailto:" not in lowered and 'type="email"' not in lowered


def test_the_drafts_of_a_skill_search_name_every_candidate(tmp_path):
    app, stub = scripted(tmp_path)
    stub.skill_result = skill_report()
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = finish(browser, token, "find Rust in Brno")
        page = client.get(location).text
    assert 'id="drafts"' in page and "Reach out to a group of candidates" in page
    assert "Share a candidate shortlist with the team" in page
    assert "1. Jan Novak - 3.4 / 5" in page


def test_a_running_job_has_no_drafts_yet(tmp_path):
    app, stub = scripted(tmp_path)
    gate = stub.hold()
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = start(browser, token)
        page = client.get(location).text
        gate.set()
        wait_for(browser, location)
    assert 'id="drafts"' not in page


def test_exported_reports_include_the_company_and_requirements(tmp_path):
    app, _ = scripted(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = finish(browser, token)
        markdown = client.get(f"{location}/download/md").text
        data = json.loads(client.get(f"{location}/download/json").text)
    assert "Hiring company" in markdown and "Payments software" in markdown
    assert "Requirements" in markdown and "English (C1)" in markdown
    assert data["company"]["name"] == "Acme"
    assert data["requirements"]["coverage"] == 0.6
