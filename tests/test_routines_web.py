# tests/test_routines_web.py
from tests.web_helpers import (
    add_user,
    Browser,
    build_app,
    open_client,
    start,
    wait_for,
)
from tests.fakes import rated_skill_report
from agent.config import RoutineTuning

import time

FORM = {
    "skill": "Rust",
    "location": "Brno",
    "min_rating": "4.0",
    "need_count": "1",
    "size": "5",
    "interval": "24",
    "max_runs": "5",
    "musts": "yes",
    "purpose": "yes",
    "notify": "team@example.com",
}


def boot(tmp_path):
    return build_app(tmp_path, submissions=50, chat_requests=500, max_jobs_per_session=9)


def wait_for_routine(ctx, identifier, states, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = ctx.routines.get(identifier)
        if found is not None and found.status in states and ctx.routines.running() == 0:
            return found
        time.sleep(0.02)
    raise AssertionError("the routine did not settle")


def tick(client, ctx):
    return client.portal.call(ctx.runner.tick)


def enable_mail(app):
    ctx = app.state.ctx
    sent = []
    settings = ctx.settings.model_copy(
        update={"smtp_host": "mail.example.com", "smtp_from": "hiring@example.com"}
    )
    ctx.settings = settings
    ctx.mailer.use(settings)
    ctx.mailer._transport = lambda config, message: sent.append(message)
    return sent


def test_roles_decide_who_sees_and_changes_routines(tmp_path):
    app, _ = boot(tmp_path)
    with open_client(app) as one, open_client(app) as two, open_client(app) as three:
        add_user(app, "viewer", role="viewer")
        add_user(app, "rec", role="recruiter")
        add_user(app, "fin", role="finance")
        viewer, rec, finance = Browser(one), Browser(two), Browser(three)
        view_token = viewer.signed_in_token("viewer")
        rec_token = rec.signed_in_token("rec")
        finance.signed_in_token("fin")
        assert one.get("/routines").status_code == 200
        assert "New routine" not in one.get("/routines").text
        assert viewer.post("/routines", FORM, token=view_token).status_code == 404
        assert three.get("/routines").status_code == 404
        made = rec.post("/routines", FORM, token=rec_token)
        assert made.status_code == 303 and made.headers["location"].startswith("/routines/")
        path = made.headers["location"].split("?")[0]
        assert one.get(path).status_code == 200 and "Run now" not in one.get(path).text
        for action in ("pause", "resume", "run", "delete"):
            assert viewer.post(f"{path}/{action}", token=view_token).status_code == 404
        assert three.get(path).status_code == 404


def test_the_module_can_be_switched_off(tmp_path):
    app, _ = boot(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        browser.logged_in_token()
        app.state.ctx.policy.update(routines_enabled=False)
        assert client.get("/routines").status_code == 404
        assert 'href="/routines"' not in client.get("/settings").text


def test_creating_a_routine_needs_a_lawful_purpose_and_sensible_numbers(tmp_path):
    app, _ = boot(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token("/routines")
        refused = browser.post("/routines", {**FORM, "purpose": ""}, token=token)
        assert refused.status_code == 400 and "lawful" in refused.text.lower()
        for field, value in (
            ("interval", "0"),
            ("max_runs", "0"),
            ("min_rating", "9"),
            ("need_count", "9"),
        ):
            assert browser.post("/routines", {**FORM, field: value}, token=token).status_code == 400
        assert (
            browser.post("/routines", {**FORM, "min_rating": "abc"}, token=token).status_code == 400
        )
        assert browser.post("/routines", {**FORM, "notify": "bad"}, token=token).status_code == 400
        assert app.state.ctx.routines.all() == []


def test_a_perfect_match_stops_the_routine_and_emails_the_team(tmp_path):
    app, stub = boot(tmp_path)
    stub.skill_result = rated_skill_report(4.7)
    with open_client(app) as client:
        sent = enable_mail(app)
        browser = Browser(client)
        ctx = app.state.ctx
        token = browser.logged_in_token("/routines")
        made = browser.post("/routines", FORM, token=token)
        identifier = made.headers["location"].split("?")[0].rsplit("/", 1)[1]
        assert tick(client, ctx) == 1
        done = wait_for_routine(ctx, identifier, {"satisfied"})
        assert done.runs == 1 and done.best_rating == 4.7 and done.next_run is None
        for _ in range(100):
            if sent:
                break
            time.sleep(0.02)
        assert len(sent) == 1 and sent[0]["To"] == "team@example.com"
        assert "found what you were looking for" in sent[0]["Subject"]
        assert "Jan Novak" in sent[0].get_content() and "/jobs/" in sent[0].get_content()
        page = client.get(f"/routines/{identifier}").text
        assert "satisfied" in page and "1 match" in page and "Open" in page
        assert ctx.candidates.count() == 1
        assert tick(client, ctx) == 0


def test_a_near_miss_is_tried_again_later_until_the_runs_are_used_up(tmp_path):
    app, stub = boot(tmp_path)
    stub.skill_result = rated_skill_report(3.6)
    with open_client(app) as client:
        browser = Browser(client)
        ctx = app.state.ctx
        token = browser.logged_in_token("/routines")
        made = browser.post("/routines", {**FORM, "max_runs": "2"}, token=token)
        identifier = made.headers["location"].split("?")[0].rsplit("/", 1)[1]
        tick(client, ctx)
        first = wait_for_routine(ctx, identifier, {"active"})
        assert first.runs == 1 and first.next_run > time.time() + 20 * 3600
        assert "No perfect match yet" in client.get(f"/routines/{identifier}").text
        assert tick(client, ctx) == 0
        queued = browser.post(f"/routines/{identifier}/run", token=token)
        assert queued.status_code == 303
        assert tick(client, ctx) == 1
        last = wait_for_routine(ctx, identifier, {"exhausted"})
        assert last.runs == 2 and last.next_run is None
        again = browser.post(f"/routines/{identifier}/resume", token=token)
        assert again.status_code == 303 and ctx.routines.get(identifier).runs == 0


def test_pausing_stops_it_and_deleting_removes_it(tmp_path):
    app, _ = boot(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        ctx = app.state.ctx
        token = browser.logged_in_token("/routines")
        made = browser.post("/routines", FORM, token=token)
        path = made.headers["location"].split("?")[0]
        identifier = path.rsplit("/", 1)[1]
        browser.post(f"{path}/pause", token=token)
        assert tick(client, ctx) == 0 and ctx.routines.get(identifier).status == "paused"
        blocked = browser.post(f"{path}/run", token=token)
        assert blocked.status_code == 400 and "Resume" in blocked.text
        edit = browser.post(
            path,
            {
                "name": "Renamed",
                "min_rating": "4.4",
                "need_count": "1",
                "max_runs": "3",
                "interval": "12",
                "notify": "",
            },
            token=token,
        )
        assert edit.status_code == 303 and ctx.routines.get(identifier).name == "Renamed"
        bad = browser.post(path, {"name": "x", "min_rating": "nope", "interval": "12"}, token=token)
        assert bad.status_code == 400
        gone = browser.post(f"{path}/delete", token=token)
        assert gone.status_code == 303 and client.get(path).status_code == 404


def test_the_daily_limit_and_the_policy_stop_the_runner(tmp_path):
    app, stub = boot(tmp_path)
    stub.skill_result = rated_skill_report(3.0)
    with open_client(app) as client:
        browser = Browser(client)
        ctx = app.state.ctx
        token = browser.logged_in_token("/routines")
        ctx.settings = ctx.settings.model_copy(
            update={"routines": RoutineTuning(max_runs_per_day=1)}
        )
        first = browser.post("/routines", FORM, token=token).headers["location"].split("?")[0]
        second = browser.post("/routines", {**FORM, "skill": "Go"}, token=token).headers["location"]
        ctx.policy.update(routines_enabled=False)
        assert tick(client, ctx) == 0
        ctx.policy.update(routines_enabled=True)
        assert tick(client, ctx) == 1
        wait_for_routine(ctx, first.rsplit("/", 1)[1], {"active"})
        assert tick(client, ctx) == 0
        assert ctx.routines.runs_today() == 1
        assert second


def test_keep_looking_on_a_skill_report_creates_the_routine(tmp_path):
    app, stub = boot(tmp_path)
    stub.skill_result = rated_skill_report(3.2)
    with open_client(app) as client:
        browser = Browser(client)
        ctx = app.state.ctx
        token = browser.logged_in_token()
        location = start(browser, token, "find Rust in Brno")
        wait_for(browser, location)
        page = client.get(location).text
        assert "Keep looking" in page
        made = browser.post(
            f"{location}/routine", {"min_rating": "4.2", "interval": "12"}, token=token
        )
        assert made.status_code == 303
        (routine,) = ctx.routines.all()
        assert (routine.skill, routine.location, routine.min_rating) == ("Rust", "Brno", 4.2)
        assert routine.interval_hours == 12 and routine.purpose_confirmed is True
        assert browser.post("/jobs/none/routine", token=token).status_code == 404
        person = start(browser, token)
        wait_for(browser, person)
        assert "Keep looking" not in client.get(person).text
        assert browser.post(f"{person}/routine", token=token).status_code == 404


def test_a_restart_gives_interrupted_runs_another_chance(tmp_path):
    app, _ = boot(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        ctx = app.state.ctx
        token = browser.logged_in_token("/routines")
        made = browser.post("/routines", FORM, token=token)
        identifier = made.headers["location"].split("?")[0].rsplit("/", 1)[1]
        ctx.routines.begin(identifier, "lost-job")
        assert ctx.routines.recover() == 1
        assert ctx.routines.get(identifier).runs == 0
