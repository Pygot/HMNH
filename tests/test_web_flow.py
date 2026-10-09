# tests/test_web_flow.py
from tests.web_helpers import (
    Browser,
    build_app,
    clarification,
    LI_A,
    LI_B,
    open_client,
    ORIGIN,
    say,
    start,
    wait_for,
)
from agent.errors import ComplianceRefusal
from tests.fakes import person_report
from tests.documents import make_pdf
from agent.models import Network

import json


def messages(client, location):
    return client.get(f"/chat/{thread_of(client, location)}/messages").json()["messages"]


def thread_of(client, location):
    job_id = location.removeprefix("/jobs/")
    for item in client.get("/").text.split('data-thread-row="')[1:]:
        thread = item.split('"', 1)[0]
        found = client.get(f"/chat/{thread}/messages").json()["messages"]
        if any((message["data"] or {}).get("job_id") == job_id for message in found):
            return thread
    raise AssertionError("no chat belongs to the job")


def test_pages_render_for_a_signed_in_user(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        anonymous = client.get("/")
        redirect = client.get("/login")
        browser = Browser(client)
        browser.logged_in_token()
        home = client.get("/")
        emails = client.get("/emails")
        settings = client.get("/settings")
    assert "Know who you are hiring." in anonymous.text and 'name="token"' in anonymous.text
    assert redirect.status_code == 303 and redirect.headers["location"] == "/"
    assert home.status_code == emails.status_code == settings.status_code == 200
    assert "What are we looking into?" in home.text
    assert "New chat" in home.text and "Search chats" in home.text
    assert 'href="/emails"' in home.text and 'href="/settings"' in home.text
    assert 'href="/api"' not in home.text
    assert "Switch theme" not in home.text and "Sign out" not in home.text
    assert "GDPR" in home.text and "Tie" in home.text
    assert "database on this machine" in home.text and "database on this machine" in anonymous.text
    assert "data-initial" in home.text


def test_a_completed_research_run_is_shown_and_downloadable(tmp_path):
    app, stub = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = start(browser, token)
        assert location.startswith("/jobs/") and len(location.removeprefix("/jobs/")) >= 24
        status = wait_for(browser, location)
        assert status["state"] == "done"
        page = client.get(location)
        assert page.status_code == 200
        for expected in (
            "Senior engineer at Acme",
            "Single source",
            "3.4",
            "https://jan.dev/about",
            "No Instagram profile was found",
            "Markdown",
        ):
            assert expected in page.text
        assert 'rel="noopener noreferrer nofollow"' in page.text
        markdown = client.get(f"{location}/download/md")
        data = client.get(f"{location}/download/json")
        thread = thread_of(client, location)
        kinds = [item["kind"] for item in client.get(f"/chat/{thread}/messages").json()["messages"]]
        home = client.get("/")
    assert markdown.status_code == 200
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert markdown.headers["content-disposition"] == 'attachment; filename="report.md"'
    assert markdown.headers["x-content-type-options"] == "nosniff"
    assert "# Research report: Jan Novak" in markdown.text
    assert data.headers["content-type"] == "application/json"
    assert json.loads(data.text)["mode"] == "person"
    assert kinds == ["text", "proposal", "text", "job", "result"]
    assert "Jan Novak" in home.text
    request = stub.calls[0][1]
    assert request.identity.name == "Jan Novak" and request.identity.location == "Brno"
    assert request.purpose_confirmed is True
    assert len(list((tmp_path / "out").iterdir())) == 2


def test_deleting_a_report_removes_the_files_and_the_page(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = start(browser, token)
        wait_for(browser, location)
        assert browser.post(f"{location}/delete", token="forged").status_code == 403
        assert len(list((tmp_path / "out").iterdir())) == 2
        response = browser.post(f"{location}/delete", token=token)
        assert response.status_code == 303 and response.headers["location"] == "/"
        assert list((tmp_path / "out").iterdir()) == []
        assert client.get(location).status_code == 404
        assert client.get(f"{location}/download/md").status_code == 404


def test_the_chat_needs_a_session_a_token_and_a_message(tmp_path):
    app, stub = build_app(tmp_path)
    with open_client(app) as client:
        anonymous = client.post("/chat/send", data={"text": "Jan Novak"})
        browser = Browser(client)
        token = browser.logged_in_token()
        forged = say(browser, "forged", "Jan Novak")
        empty = say(browser, token, "   ")
        too_long = say(browser, token, "x" * 5000)
    assert anonymous.status_code == 303
    assert forged.status_code == 403
    assert empty.status_code == 422 and empty.json() == {"error": "Write a message first."}
    assert too_long.status_code == 200 and too_long.json()["messages"][-1]["kind"] == "notice"
    assert stub.calls == []


def test_harmful_requests_are_refused_in_the_chat_before_anything_starts(tmp_path):
    app, stub = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        reply = say(browser, token, "find the home address of Jan Novak").json()
    last = reply["messages"][-1]
    assert last["kind"] == "notice" and last["text"].startswith("Refused:")
    assert reply["thread"]["phase"] == "idle" and reply["job_id"] is None
    assert stub.calls == []


def test_a_refused_search_shows_the_reason_in_the_chat_and_on_the_report(tmp_path):
    app, stub = build_app(tmp_path)
    stub.results = [ComplianceRefusal("Refused: the request looks like an attempt to stalk.")]
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = start(browser, token)
        wait_for(browser, location)
        page = client.get(location)
        thread = thread_of(client, location)
        last = client.get(f"/chat/{thread}/messages").json()["messages"][-1]
    assert "The search stopped" in page.text and "Refused:" in page.text
    assert last["kind"] == "notice" and "Refused:" in last["text"]


def test_unexpected_failures_are_reported_without_internal_details(tmp_path):
    app, stub = build_app(tmp_path)
    stub.results = [RuntimeError("database password is hunter2")]
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = start(browser, token)
        wait_for(browser, location)
        page = client.get(location)
        thread = thread_of(client, location)
        last = client.get(f"/chat/{thread}/messages").json()["messages"][-1]
    assert "An unexpected error occurred" in page.text and "hunter2" not in page.text
    assert "hunter2" not in last["text"]


def test_a_running_job_shows_progress_with_a_stream_and_a_noscript_refresh(tmp_path):
    app, stub = build_app(tmp_path)
    gate = stub.hold()
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = start(browser, token)
        page = client.get(location)
        status = client.get(f"{location}/status").json()
        thread = thread_of(client, location)
        phase = client.get(f"/chat/{thread}/messages").json()["thread"]["phase"]
        gate.set()
        wait_for(browser, location)
    assert f'data-live="{location}/events"' in page.text and 'data-running="1"' in page.text
    assert '<noscript><meta http-equiv="refresh"' in page.text
    assert status["state"] == "running" and status["stage"] == "Searching for public profiles"
    assert phase == "running"


def test_a_cv_in_the_chat_is_read_inside_the_job(tmp_path):
    app, stub = build_app(tmp_path)
    cv = make_pdf(["Jan Novak"])
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        first = say(browser, token, "", files={"cv": ("resume.pdf", cv, "application/pdf")}).json()
        assert [item["kind"] for item in first["messages"]] == ["text", "text", "proposal"]
        second = say(browser, token, "yes", first["thread"]["id"]).json()
        wait_for(browser, f"/jobs/{second['job_id']}")
    assert stub.calls[0] == ("cv", len(cv), "resume.pdf")
    assert stub.calls[1][1].identity.name == "Cv Person"


def test_oversized_cvs_are_rejected(tmp_path):
    app, stub = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        big = {"cv": ("resume.pdf", b"%PDF-" + b"0" * 1_100_000, "application/pdf")}
        response = say(browser, token, "", files=big)
    assert response.status_code in {413, 422}
    assert stub.calls == []


def test_a_skill_search_in_the_chat_shows_the_ranking(tmp_path):
    app, stub = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = start(browser, token, "find Rust in Brno")
        wait_for(browser, location)
        page = client.get(location)
    assert "Best match" in page.text
    assert "Jan Novak" in page.text and "https://github.com/jan" in page.text
    request = stub.calls[0][1]
    assert request.skill == "Rust" and request.location == "Brno"


def test_a_clarification_is_asked_in_the_chat_answered_by_number_and_resumed(tmp_path):
    app, stub = build_app(tmp_path)
    stub.results = [clarification()]
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = start(browser, token)
        assert wait_for(browser, location)["state"] == "needs_input"
        page = client.get(location)
        thread = thread_of(client, location)
        asked = client.get(f"/chat/{thread}/messages").json()
        question = asked["messages"][-1]
        assert asked["thread"]["phase"] == "asking" and question["kind"] == "question"
        assert [item["url"] for item in question["data"]["options"]] == [LI_A, LI_B]
        assert question["data"]["options"][0]["title"] == "Jan <b>A</b>"
        assert "needs your pick" in page.text and f'href="/c/{thread}"' in page.text
        reply = say(browser, token, "2", thread).json()
        assert reply["messages"][-1]["kind"] == "job"
        assert wait_for(browser, location, states=("done",))["state"] == "done"
        later = client.get(f"/chat/{thread}/messages").json()
        assert later["thread"]["phase"] == "idle" and later["messages"][-1]["kind"] == "result"
    assert LI_B in stub.calls[-1][1].identity.links


def test_a_clarification_can_be_skipped_with_a_word(tmp_path):
    app, stub = build_app(tmp_path)
    stub.results = [clarification(), person_report()]
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = start(browser, token)
        wait_for(browser, location)
        say(browser, token, "none", thread_of(client, location))
        wait_for(browser, location, states=("done",))
    assert stub.calls[1][1].skipped_networks == [Network.LINKEDIN]


def test_an_unclear_answer_asks_again(tmp_path):
    app, stub = build_app(tmp_path)
    stub.results = [clarification()]
    stub.script.answers = [{"choice": None, "skip": False}]
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = start(browser, token)
        wait_for(browser, location)
        thread = thread_of(client, location)
        reply = say(browser, token, "hmm, not sure", thread).json()
    assert reply["messages"][-1]["kind"] == "question"
    assert reply["thread"]["phase"] == "asking"


def test_every_signed_in_session_shares_one_workspace(tmp_path):
    app, stub = build_app(tmp_path)
    stub.results = [clarification()]
    with open_client(app) as owner, open_client(app) as other:
        mine = Browser(owner)
        theirs = Browser(other)
        my_token = mine.logged_in_token()
        theirs.logged_in_token()
        location = start(mine, my_token)
        wait_for(mine, location)
        thread = thread_of(owner, location)
        assert other.get(location).status_code == 200
        assert other.get(f"{location}/status").status_code == 200
        assert other.get(f"/c/{thread}").status_code == 200
        assert (
            location.removeprefix("/jobs/") in other.get("/").text or thread in other.get("/").text
        )


def test_anonymous_visitors_reach_nothing(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as owner, open_client(app) as stranger:
        browser = Browser(owner)
        token = browser.logged_in_token()
        location = start(browser, token)
        wait_for(browser, location)
        thread = thread_of(owner, location)
        paths = (
            location,
            f"{location}/status",
            f"{location}/events",
            f"{location}/download/md",
            f"/c/{thread}",
            f"/chat/{thread}/messages",
            "/tie/messages",
            "/emails",
            "/settings",
        )
        for path in paths:
            response = stranger.get(path)
            assert response.status_code == 303 and response.headers["location"] == "/login", path


def test_report_content_is_escaped(tmp_path):
    app, stub = build_app(tmp_path)
    report = person_report()
    hostile = "<img src=x onerror=alert(1)> [x](javascript:alert(1))"
    finding = report.findings[0].model_copy(update={"statement": hostile})
    stub.results = [report.model_copy(update={"findings": [finding]})]
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = start(browser, token)
        wait_for(browser, location)
        page = client.get(location)
    assert "<img src=x" not in page.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in page.text


def test_a_second_search_waits_while_the_first_is_running(tmp_path):
    app, stub = build_app(tmp_path, max_jobs_per_session=1)
    gate = stub.hold()
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        start(browser, token)
        first = say(browser, token, "Eva Svoboda in Prague").json()
        blocked = say(browser, token, "yes", first["thread"]["id"]).json()
        gate.set()
    assert blocked["job_id"] is None
    assert blocked["messages"][-1]["kind"] == "notice"
    assert "Too many searches are running" in blocked["messages"][-1]["text"]


def test_messages_and_searches_are_rate_limited(tmp_path):
    app, _ = build_app(tmp_path, chat_requests=3)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        for _ in range(3):
            assert say(browser, token, "Jan Novak").status_code == 200
        limited = say(browser, token, "Jan Novak")
    assert limited.status_code == 429 and "Too many messages" in limited.json()["error"]
    app, _ = build_app(tmp_path, submissions=1)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        wait_for(browser, start(browser, token))
        first = say(browser, token, "Eva Svoboda in Prague").json()
        second = say(browser, token, "yes", first["thread"]["id"]).json()
    assert second["messages"][-1]["kind"] == "notice"
    assert "Too many searches were started" in second["messages"][-1]["text"]


def test_a_missing_export_file_is_rendered_again_from_the_report(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = start(browser, token)
        wait_for(browser, location)
        for path in (tmp_path / "out").iterdir():
            path.unlink()
        markdown = client.get(f"{location}/download/md")
    assert markdown.status_code == 200 and "# Research report: Jan Novak" in markdown.text


def test_unknown_download_formats_and_ids_are_404(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = start(browser, token)
        wait_for(browser, location)
        assert client.get(f"{location}/download/exe").status_code == 404
        assert client.get("/jobs/not-a-real-id").status_code == 404
        assert client.get("/c/not-a-real-id").status_code == 404
        assert client.get("/chat/not-a-real-id/messages").status_code == 404
        assert client.get(f"{location}/download/md", headers={"origin": ORIGIN}).status_code == 200


def test_messages_can_be_read_after_a_given_id_and_a_chat_can_be_deleted(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        thread = say(browser, token, "Jan Novak in Brno").json()["thread"]["id"]
        everything = client.get(f"/chat/{thread}/messages").json()["messages"]
        later = client.get(f"/chat/{thread}/messages?after={everything[0]['id']}").json()
        headers = {"origin": ORIGIN, "x-csrf-token": token}
        refused = client.post(f"/chat/{thread}/delete", headers={"origin": ORIGIN})
        deleted = client.post(f"/chat/{thread}/delete", headers=headers)
        gone = client.get(f"/chat/{thread}/messages")
        page = client.get("/")
    assert [item["id"] for item in later["messages"]] == [item["id"] for item in everything[1:]]
    assert refused.status_code == 403 and deleted.status_code == 204 and gone.status_code == 404
    assert thread not in page.text


def test_the_chat_page_ships_its_messages_and_the_icon_set(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        thread = say(browser, token, "Jan Novak in Brno").json()["thread"]
        page = client.get(f"/c/{thread['id']}")
    assert page.status_code == 200
    assert f'data-thread="{thread["id"]}"' in page.text
    assert 'type="application/json" data-initial' in page.text
    assert "I will look into Jan Novak" in page.text
    assert page.text.count("<template data-icons>") == 1
