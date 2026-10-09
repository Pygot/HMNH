# tests/test_web_settings.py
from agent.models import (
    Goal,
    Network,
    Priority,
    Requirement,
    RequirementKind,
    ResearchOptions,
    ScepticismMode,
    Strictness,
)
from tests.web_helpers import (
    Browser,
    build_app,
    open_client,
    ORIGIN,
    say,
    start,
    wait_for,
)
from agent.chat import (
    load_profile,
    Profile,
    save_profile,
)
from agent.web.context import service_status
from tests.fakes import sceptical_report
from agent.errors import UpstreamError

import pytest
import re

SETTINGS_FORM = {
    "theme": "dark",
    "density": "compact",
    "goal": "sales",
    "source": ["web", "linkedin"],
    "skill_limit": "7",
    "strictness": "strict",
    "threshold": "0.8",
    "margin": "0.2",
    "max_web_pages": "4",
    "scepticism": "strict",
}
CONTEXT_FORM = {
    "company_name": "Acme",
    "company_website": "https://acme.example",
    "company_description": "We build payment software for shops",
    "sender_name": "Eva Svobodova",
}
ENGLISH = Requirement(
    kind=RequirementKind.LANGUAGE, label="English", level="C1", priority=Priority.MUST
)
COOKIE = "__Host-prefs"


def checked(html, name, value):
    return re.search(rf'name="{name}" value="{value}" checked', html) is not None


def field_value(html, name):
    match = re.search(rf'name="{name}"[^>]*value="([^"]*)"', html)
    assert match, name
    return match.group(1)


def prefs_cookies(response):
    return [item for item in response.headers.get_list("set-cookie") if item.startswith(COOKIE)]


def save(browser, token, **changes):
    return browser.post("/settings", {**SETTINGS_FORM, **changes}, token=token)


def test_the_settings_page_shows_every_section_and_the_service_status(tmp_path):
    app, stub = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        browser.logged_in_token()
        page = client.get("/settings")
    assert page.status_code == 200
    for expected in (
        "Appearance",
        "Research defaults",
        "Hiring context",
        "Matching and scepticism",
        "Your data",
        "Developer",
        "Account",
        "Connected services",
        "Reset display settings",
        "Balanced: threshold 0.7, margin 0.15",
        "2.00 USD used, 103.00 USD remaining",
        "Display choices stay in this browser",
    ):
        assert expected in page.text
    assert checked(page.text, "theme", "system")
    assert checked(page.text, "density", "comfortable")
    assert stub.status_calls == 1


def test_the_service_status_is_cached_between_requests(tmp_path):
    app, stub = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        browser.logged_in_token()
        for _ in range(3):
            client.get("/settings")
    assert stub.status_calls == 1


def test_an_unavailable_status_is_reported_instead_of_hiding_the_page(tmp_path):
    app, stub = build_app(tmp_path)
    stub.status_error = UpstreamError("Apify account timed out.")
    with open_client(app) as client:
        browser = Browser(client)
        browser.logged_in_token()
        page = client.get("/settings")
    assert page.status_code == 200
    assert "Status is not available: Apify account timed out." in page.text
    assert stub.status_calls == 1


def test_the_theme_is_chosen_in_the_settings_only(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        landing = client.get("/").text
        browser = Browser(client)
        token = browser.logged_in_token()
        pages = [client.get(path).text for path in ("/", "/emails", "/settings")]
        old = browser.post("/theme", {"return_to": "/"}, token=token)
    for page in [landing, *pages[:2]]:
        assert 'name="theme"' not in page and "Switch theme" not in page and "/theme" not in page
    assert 'name="theme"' in pages[2]
    assert old.status_code == 404


def test_saving_settings_sets_a_hardened_cookie_and_applies_them(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token("/settings")
        response = save(browser, token)
        assert response.status_code == 303 and response.headers["location"] == "/settings?saved=1"
        cookie = prefs_cookies(response)[0].lower()
        for flag in ("httponly", "secure", "samesite=strict", "path=/", "max-age=31536000"):
            assert flag in cookie
        assert "domain" not in cookie
        saved = client.get("/settings?saved=1")
        home = client.get("/")
    assert "Saved." in saved.text
    assert 'data-theme="dark" data-density="compact"' in home.text
    assert checked(saved.text, "theme", "dark") and checked(saved.text, "density", "compact")
    assert checked(saved.text, "goal", "sales")
    assert checked(saved.text, "strictness", "strict")
    assert checked(saved.text, "scepticism", "strict")
    assert checked(saved.text, "source", "web") and checked(saved.text, "source", "linkedin")
    assert not checked(saved.text, "source", "facebook")
    assert field_value(saved.text, "threshold") == "0.8"
    assert field_value(saved.text, "margin") == "0.2"
    assert field_value(saved.text, "max_web_pages") == "4"
    assert field_value(saved.text, "skill_limit") == "7"


def test_saved_settings_become_the_defaults_of_a_new_search(tmp_path):
    app, stub = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token("/settings")
        save(browser, token)
        wait_for(browser, start(browser, browser.csrf("/")))
    request = stub.calls[0][1]
    assert request.goal is Goal.SALES
    assert request.options == ResearchOptions(
        networks=[Network.WEB, Network.LINKEDIN],
        strictness=Strictness.STRICT,
        threshold=0.8,
        margin=0.2,
        max_web_pages=4,
        scepticism=ScepticismMode.STRICT,
    )


def test_the_skill_search_uses_the_saved_sources_and_candidate_count(tmp_path):
    app, stub = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token("/settings")
        save(browser, token, source=["web"], skill_limit="3")
        wait_for(browser, start(browser, browser.csrf("/"), "find Rust in Brno"))
    request = stub.calls[0][1]
    assert request.options.networks == [Network.WEB]
    assert request.options.scepticism is ScepticismMode.STRICT
    assert request.limit == 3


def test_the_optional_overrides_can_be_left_empty(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token("/settings")
        response = save(browser, token, threshold="", margin="", max_web_pages="", skill_limit="")
        assert response.status_code == 303
        page = client.get("/settings")
    assert field_value(page.text, "threshold") == ""
    assert field_value(page.text, "max_web_pages") == ""


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"theme": "neon"}, "Theme is not a valid choice."),
        ({"density": "huge"}, "Density is not a valid choice."),
        ({"goal": "stalking"}, "Goal is not a valid choice."),
        ({"strictness": "extreme"}, "Matching is not a valid choice."),
        ({"scepticism": "off"}, "Scepticism is not a valid choice."),
        ({"threshold": "abc"}, "Match threshold must be a number."),
        ({"margin": "x"}, "Match margin must be a number."),
        ({"max_web_pages": "1.5"}, "Web pages must be a number."),
        ({"skill_limit": "many"}, "Candidates must be a number."),
        ({"source": ["myspace"]}, "Source is not a valid choice."),
        ({"threshold": "1.5"}, "Please check this"),
        ({"max_web_pages": "99"}, "Please check this"),
        ({"skill_limit": "0"}, "Please check this"),
        ({"skill_limit": "11"}, "Please check this"),
        ({"source": []}, "Please check this"),
        ({"company_website": "https://acme.example"}, "Enter the company name"),
        ({"company_name": "Acme", "company_website": "ftp://acme.example"}, "Please check this"),
    ],
)
def test_invalid_settings_are_explained_and_not_saved(tmp_path, changes, message):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token("/settings")
        response = save(browser, token, **changes)
        assert response.status_code == 400
        assert message in response.text
        assert prefs_cookies(response) == []
        assert 'data-theme="system"' in client.get("/").text
        assert load_profile(app.state.ctx.store) == Profile()


def test_saving_settings_needs_a_valid_csrf_token_and_a_same_origin_request(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token("/settings")
        for path in (
            "/settings",
            "/settings/reset",
            "/settings/requirements/remove",
            "/settings/data/delete",
        ):
            assert browser.post(path, SETTINGS_FORM).status_code == 403, path
            assert browser.post(path, SETTINGS_FORM, token="forged").status_code == 403, path
            foreign = browser.post(path, SETTINGS_FORM, token=token, origin="https://evil.example")
            assert foreign.status_code == 403, path
        assert client.get("/").text.count('data-theme="system"') == 1


def test_reset_removes_the_saved_display_settings_but_not_the_memory(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token("/settings")
        save(browser, token, **CONTEXT_FORM)
        assert 'data-theme="dark"' in client.get("/").text
        response = browser.post("/settings/reset", token=token)
        assert response.status_code == 303 and response.headers["location"] == "/settings"
        assert "max-age=0" in prefs_cookies(response)[0].lower()
        assert 'data-theme="system"' in client.get("/").text
        assert load_profile(app.state.ctx.store).company_name == "Acme"


def test_the_hiring_context_is_optional_and_lives_in_the_database(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token("/settings")
        blank = save(browser, token)
        assert blank.status_code == 303
        assert load_profile(app.state.ctx.store) == Profile()
        filled = save(browser, token, **CONTEXT_FORM)
        page = client.get("/settings")
        profile = load_profile(app.state.ctx.store)
        cookie = prefs_cookies(filled)[0]
    assert filled.status_code == 303
    assert profile.company_name == "Acme" and profile.sender_name == "Eva Svobodova"
    assert field_value(page.text, "company_name") == "Acme"
    assert field_value(page.text, "company_website") == "https://acme.example"
    assert field_value(page.text, "sender_name") == "Eva Svobodova"
    assert "Acme" not in cookie and "Eva" not in cookie


def test_the_saved_company_reaches_the_next_search_and_the_email_drafts(tmp_path):
    app, stub = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token("/settings")
        save(browser, token, **CONTEXT_FORM)
        location = start(browser, browser.csrf("/"))
        wait_for(browser, location)
        page = client.get(location)
    company = stub.calls[0][1].company
    assert company.name == "Acme" and company.website == "https://acme.example"
    assert company.description == "We build payment software for shops"
    assert "Eva Svobodova" in page.text


def test_clearing_the_company_name_clears_the_company(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token("/settings")
        save(browser, token, **CONTEXT_FORM)
        cleared = save(
            browser,
            token,
            company_name="",
            company_website="",
            company_description="",
            sender_name="",
        )
        profile = load_profile(app.state.ctx.store)
    assert cleared.status_code == 303 and profile == Profile()


def test_standing_requirements_are_listed_and_can_be_removed_one_by_one(tmp_path):
    app, _ = build_app(tmp_path)
    store = app.state.ctx.store
    german = Requirement(kind=RequirementKind.LANGUAGE, label="German", priority=Priority.NICE)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token("/settings")
        save_profile(store, Profile(requirements=[ENGLISH, german]))
        page = client.get("/settings").text
        ignored = browser.post("/settings/requirements/remove", {"index": "9"}, token=token)
        junk = browser.post("/settings/requirements/remove", {"index": "x"}, token=token)
        removed = browser.post("/settings/requirements/remove", {"index": "0"}, token=token)
        after = client.get("/settings").text
        remaining = [item.label for item in load_profile(store).requirements]
    assert "English (C1)" in page and "German" in page and "nice to have" in page
    assert ignored.status_code == junk.status_code == 303
    assert removed.status_code == 303 and removed.headers["location"] == "/settings?saved=1#context"
    assert remaining == ["German"]
    assert "English (C1)" not in after and "German" in after


def test_what_the_chat_learned_is_visible_and_used_in_every_later_search(tmp_path):
    app, stub = build_app(tmp_path)
    stub.script.understood["We are a fintech startup hiring Python engineers"] = {
        "intent": "update",
        "company": "Acme",
        "about": "Payments software for shops",
        "requirements": [{"kind": "language", "label": "English", "level": "C1"}],
    }
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        thread = say(browser, token, "We are a fintech startup hiring Python engineers").json()
        page = client.get("/settings").text
        wait_for(browser, start(browser, token))
    assert thread["messages"][-1]["text"].startswith("Noted.")
    assert field_value(page, "company_name") == "Acme" and "English (C1)" in page
    request = stub.calls[0][1]
    assert request.company.name == "Acme"
    assert [item.label for item in request.requirements] == ["English"]


def test_the_data_section_counts_what_is_stored_and_explains_where(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        wait_for(browser, start(browser, token))
        page = client.get("/settings").text
    assert "stored in a database on this machine" in page and "agent.db" in page
    assert "Nothing is uploaded." in page and "after 90 days" in page
    assert re.search(r"<dt>Chats</dt><dd class=\"num\">1</dd>", page)
    assert re.search(r"<dt>Reports</dt><dd class=\"num\">1</dd>", page)


def test_the_history_and_the_memory_can_be_deleted_separately_or_together(tmp_path):
    app, _ = build_app(tmp_path)
    store = app.state.ctx.store
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        save(browser, token, **CONTEXT_FORM)
        location = start(browser, token)
        wait_for(browser, location)
        say(browser, token, "hello")
        assert list((tmp_path / "out").iterdir())
        history = browser.post("/settings/data/delete", {"what": "history"}, token=token)
        assert history.status_code == 303 and history.headers["location"].endswith("#data")
        assert store.threads("research") == [] and store.jobs("web") == []
        assert list((tmp_path / "out").iterdir()) == []
        assert client.get(location).status_code == 404
        assert load_profile(store).company_name == "Acme"
        memory = browser.post("/settings/data/delete", {"what": "memory"}, token=token)
        assert memory.status_code == 303 and load_profile(store) == Profile()
        wait_for(browser, start(browser, token))
        save(browser, token, **CONTEXT_FORM)
        everything = browser.post("/settings/data/delete", {"what": "all"}, token=token)
        unknown = browser.post("/settings/data/delete", {"what": "the moon"}, token=token)
        counts = store.counts()
    assert everything.status_code == 303 and unknown.status_code == 400
    assert counts == {"threads": 0, "messages": 0, "jobs": 0, "memory": 0}


def test_the_api_page_is_hidden_until_it_is_switched_on_in_the_settings(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        rail = client.get("/").text
        hidden = client.get("/api")
        settings = client.get("/settings").text
        save(browser, token, show_api="yes")
        shown_rail = client.get("/").text
        page = client.get("/api")
        save(browser, token)
        again = client.get("/api")
    assert 'href="/api"' not in rail and 'name="show_api"' in settings
    assert hidden.status_code == 303 and hidden.headers["location"] == "/settings#developer"
    assert 'href="/api"' in shown_rail
    assert page.status_code == 200
    assert "Create a personal API key" in page.text
    assert "The shared token from WEB_API_TOKEN" not in page.text
    for path in ("/api/v1/research", "/api/v1/skill-search", "/api/v1/jobs/{job_id}/answers"):
        assert path in page.text
    assert again.status_code == 303


def test_the_api_page_shows_the_state_of_the_api_without_its_secret(tmp_path):
    app, _ = build_app(tmp_path, api_token="y" * 24)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        save(browser, token, show_api="yes")
        page = client.get("/api")
    assert "The shared token from WEB_API_TOKEN also works" in page.text
    assert "y" * 24 not in page.text


def test_unreadable_preference_cookies_are_reset_with_a_notice(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        browser.logged_in_token()
        client.cookies.set(COOKIE, "!!!!")
        page = client.get("/")
        client.cookies.set(COOKIE, "A" * 5000)
        oversized = client.get("/")
    for response in (page, oversized):
        assert response.status_code == 200
        assert "could not be read and were reset" in response.text
        assert 'data-theme="system"' in response.text


def test_the_preference_cookie_cannot_inject_markup(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        browser.logged_in_token()
        evil = "eyJ0aGVtZSI6ICJcIj48c2NyaXB0PmFsZXJ0KDEpPC9zY3JpcHQ-In0"
        client.cookies.set(COOKIE, evil)
        page = client.get("/")
    assert "<script>alert(1)" not in page.text
    assert 'data-theme="system"' in page.text


def test_settings_pages_require_a_session(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        for path in ("/settings", "/api"):
            response = client.get(path)
            assert response.status_code == 303 and response.headers["location"] == "/login"
        for path in ("/settings", "/settings/reset"):
            response = client.post(path, data={}, headers={"origin": ORIGIN})
            assert response.status_code == 303 and response.headers["location"] == "/login"


def test_a_suspicious_profile_is_explained_on_the_report_page_and_in_the_chat(tmp_path):
    app, stub = build_app(tmp_path)
    stub.results = [sceptical_report()]
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = start(browser, token)
        wait_for(browser, location)
        page = client.get(location)
        thread = re.search(r'href="/c/([^"]+)"', client.get("/").text).group(1)
        last = client.get(f"/chat/{thread}/messages").json()["messages"][-1]
    for expected in (
        "Scepticism review: high",
        "The rating was reduced from 4.4 to 2.5.",
        "Roles overlap.",
        "What to do next",
        "Check dates.",
        "Capped from 4.4",
        "1 warning",
    ):
        assert expected in page.text
    assert last["kind"] == "result" and last["data"]["rating"] == 2.5


def test_a_clean_profile_shows_no_warning(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        location = start(browser, token)
        wait_for(browser, location)
        page = client.get(location)
    assert "No warning signs (standard mode)" in page.text
    assert 'id="scepticism"' not in page.text
    assert "Capped from" not in page.text


async def test_the_status_is_unavailable_before_the_services_have_started(tmp_path):
    app, _ = build_app(tmp_path)
    assert await service_status(app.state.ctx) == (None, "The service is still starting.")
