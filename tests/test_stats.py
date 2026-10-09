# tests/test_stats.py
from agent.web.charts import (
    grouped_bars,
    line_plot,
    plot_svg,
    row_bars,
    rows_svg,
    thin,
)
from tests.web_helpers import (
    add_user,
    Browser,
    build_app,
    open_client,
    start,
    wait_for,
)
from agent.config import (
    CandidateTuning,
    FinanceTuning,
    RoutineTuning,
    StatsTuning,
    StoreTuning,
)
from agent.store import (
    JobRecord,
    Store,
)
from datetime import (
    datetime,
    UTC,
)
from agent.candidates import Candidates
from agent.finance import FinanceBook
from agent.config import ChartTuning
from agent.routines import Routines
from agent.stats import Stats

import xml.etree.ElementTree as ET
import pytest

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC).timestamp()
DAY = 86_400


@pytest.fixture
def world(tmp_path):
    store = Store(StoreTuning(path=tmp_path / "data" / "agent.db"), lambda: NOW)
    finance = FinanceBook(store, FinanceTuning(), lambda: NOW)
    yield {
        "store": store,
        "finance": finance,
        "stats": Stats(store, finance, StatsTuning(), lambda: NOW),
        "candidates": Candidates(store, CandidateTuning(), lambda: NOW),
        "routines": Routines(store, RoutineTuning(), lambda: NOW),
    }
    store.close()


def job(store, identifier, age_days, kind="person", state="done", rating=3.5):
    store.save_job(
        JobRecord(
            id=identifier,
            thread_id=None,
            owner="web",
            kind=kind,
            title=identifier,
            state=state,
            stage="Done",
            rating=rating,
            created=NOW - age_days * DAY,
            updated=NOW,
        )
    )


def test_an_empty_workspace_gives_complete_zeroes(world):
    stats = world["stats"]
    searches = stats.searches(7)
    assert searches["total"] == 0 and len(searches["per_day"]) == 7
    assert searches["average_rating"] is None
    assert stats.candidates()["total"] == 0 and len(stats.candidates()["ratings"]) == 5
    assert len(stats.hires(3)["per_month"]) == 3
    assert stats.routines(7)["runs"] == 0 and stats.emails(7)["sent"] == 0
    assert stats.team(7)["total"] == 0 and len(stats.finance(3)["per_month"]) == 3


def test_searches_are_counted_per_day_and_kind(world):
    store, stats = world["store"], world["stats"]
    job(store, "a", 0, rating=4.0)
    job(store, "b", 0, kind="skill", rating=2.0)
    job(store, "c", 2, state="failed", rating=None)
    job(store, "old", 40)
    data = stats.searches(7)
    assert data["total"] == 3 and dict(data["by_kind"]) == {"person": 2, "skill": 1}
    assert data["per_day"][-1] == ("2026-06-15", 2) and data["per_day"][-3] == ("2026-06-13", 1)
    assert data["average_rating"] == 3.0 and data["best_rating"] == 4.0
    assert dict(data["by_state"]) == {"done": 2, "failed": 1}
    assert stats.searches(90)["total"] == 4
    assert stats.searches(10_000)["days"] == StatsTuning().max_days
    assert stats.searches(0)["days"] == 1


def test_candidate_hire_routine_and_mail_figures(world):
    candidates, routines, stats = world["candidates"], world["routines"], world["stats"]
    first = candidates.create("Ann One")
    second = candidates.create("Bob Two")
    world["store"].run("UPDATE candidates SET rating = 4.2 WHERE id = ?", (first.id,))
    world["store"].run("UPDATE candidates SET rating = 2.6 WHERE id = ?", (second.id,))
    hire = candidates.hire(first.id, "Analyst")
    candidates.update_hire(first.id, hire.id, rating=5, outcome="left")
    data = stats.candidates()
    assert data["total"] == 2 and data["hired_before"] == 1 and data["average_rating"] == 3.4
    assert dict(data["ratings"]) == {"0-1": 0, "1-2": 0, "2-3": 1, "3-4": 0, "4-5": 1}
    hires = stats.hires(3)
    assert hires["total"] == 1 and hires["average_rating"] == 5.0 and hires["per_month"][-1][1] == 1
    assert dict(hires["by_outcome"]) == {"left": 1}
    made = routines.create("", "Rust", "Brno", purpose_confirmed=True)
    routines.begin(made.id, "job-1")
    assert stats.routines(7)["runs"] == 1 and dict(stats.routines(7)["by_status"]) == {"active": 1}
    world["store"].run(
        "INSERT INTO mail_log (ts, actor, to_address, subject, kind, purpose, status) "
        "VALUES (?, 'a', 'x@y.co', 's', 'outreach', 'outreach', 'sent')",
        (NOW - DAY,),
    )
    world["store"].run(
        "INSERT INTO suppressions (address, reason, created) VALUES ('z@y.co', '', ?)", (NOW,)
    )
    mail = stats.emails(30)
    assert (
        mail["sent"] == 1
        and mail["do_not_contact"] == 1
        and sum(c for _, c in mail["per_week"]) == 1
    )


def test_finance_and_team_figures(world):
    finance, stats = world["finance"], world["stats"]
    finance.add_entry("2026-06-01", "revenue", "100")
    finance.add_entry("2026-05-01", "expense", "40")
    data = stats.finance(2)
    assert data["totals"] == {"revenue": 10_000, "expense": 4000, "net": 6000}
    assert data["per_month"][0]["expense"] == 4000 and data["currency"] == "USD"
    assert data["revenue_by_category"] == [("Uncategorised", 10_000)]


def test_standalone_svgs_are_valid_xml_and_escape_their_text():
    tuning = ChartTuning()
    plot = grouped_bars(
        ["<b>x</b>", "y"], [("A&B", [3, 1]), ("C", [1, 2])], str, tuning, 'Say "hi" <now>'
    )
    document = plot_svg(plot, tuning)
    root = ET.fromstring(document)
    assert root.tag.endswith("svg") and "&lt;b&gt;" in document and "A&amp;B" in document
    assert "<script" not in document and "style=" not in document
    ET.fromstring(
        plot_svg(line_plot(["a", "b"], [("S", [1, 2]), ("T", [2, 1])], str, tuning, "L"), tuning)
    )
    ET.fromstring(rows_svg(row_bars([("One & two", 5)], str, tuning, "R"), tuning))
    assert thin(list("abcdefghij"), 5) == ["a", "", "c", "", "e", "", "g", "", "i", ""]
    assert thin(["a", "b"], 5) == ["a", "b"]


def switch(browser, username):
    browser.post("/logout", token=browser.csrf("/account"))
    return browser.signed_in_token(username)


def test_the_dashboard_shows_only_the_parts_a_role_may_see(tmp_path):
    app, _ = build_app(tmp_path, submissions=50, chat_requests=500, max_jobs_per_session=9)
    with open_client(app) as client:
        browser = Browser(client)
        add_user(app, "mgr", role="manager")
        add_user(app, "viewer", role="viewer")
        app.state.ctx.accounts.save_role("Nothing", "", ["chat.use"])
        add_user(app, "none", role="Nothing")
        token = browser.logged_in_token()
        location = start(browser, token)
        wait_for(browser, location)
        switch(browser, "mgr")
        page = client.get("/dashboard").text
        for name in ("searches", "candidates", "hires", "emails", "team"):
            assert f'id="{name}"' in page, name
        assert 'id="finance"' not in page
        assert 'class="plot"' in page and "Save as SVG" in page
        app.state.ctx.policy.update(finance_enabled=True)
        assert 'id="finance"' in client.get("/dashboard").text
        svg = client.get("/dashboard/searches/0.svg")
        assert svg.status_code == 200 and svg.headers["content-type"] == "image/svg+xml"
        ET.fromstring(svg.text)
        assert client.get("/dashboard/team/0.svg").status_code == 200
        assert client.get("/dashboard/searches/9.svg").status_code == 404
        assert client.get("/dashboard/searches/x.svg").status_code == 404
        assert client.get("/dashboard/nothing/0.svg").status_code == 404
        switch(browser, "viewer")
        assert 'id="team"' not in client.get("/dashboard").text
        assert 'id="searches"' in client.get("/dashboard?window=7").text
        assert client.get("/dashboard/finance/0.svg").status_code == 404
        switch(browser, "none")
        assert client.get("/dashboard").status_code == 404
