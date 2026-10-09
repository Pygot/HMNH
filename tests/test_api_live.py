# tests/test_api_live.py
from tests.test_api import (
    get,
    post,
    RESEARCH,
    SKILL_SEARCH,
    start,
    start_skill,
    wait_api,
)
from tests.web_helpers import (
    API_TOKEN,
    build_app,
    open_client,
    ScriptedResearcher,
)
from tests.fakes import skill_report
from agent.web.api import BASE

COMPANY = {"name": "Acme", "website": "https://acme.example", "description": "We build payments"}
WANTED = [
    {"kind": "language", "label": "English", "level": "C1"},
    {"kind": "skill", "label": "Python", "minimum_years": 5, "priority": "nice"},
]


def live_app(tmp_path, **overrides):
    return build_app(tmp_path, ScriptedResearcher(), api_token=API_TOKEN, **overrides)


def test_the_company_and_requirements_reach_the_request(tmp_path):
    app, stub = live_app(tmp_path)
    with open_client(app) as client:
        job_id = start(client, {**RESEARCH, "company": COMPANY, "requirements": WANTED})
        wait_api(client, job_id)
    request = stub.calls[0][1]
    assert request.company.name == "Acme" and request.company.description == "We build payments"
    assert [item.label for item in request.requirements] == ["English", "Python"]
    assert request.requirements[1].minimum_years == 5
    assert request.requirements[1].priority.value == "nice"


def test_the_report_contains_the_company_profile_and_the_assessment(tmp_path):
    app, _ = live_app(tmp_path)
    with open_client(app) as client:
        job_id = start(client, {**RESEARCH, "company": COMPANY, "requirements": WANTED})
        body = wait_api(client, job_id)
    report = body["report"]
    assert report["company"]["name"] == "Acme"
    assert report["requirements"]["coverage"] == 0.6
    assert [item["status"] for item in report["requirements"]["results"]] == [
        "met",
        "partial",
        "unknown",
    ]


def test_a_skill_search_accepts_a_company_and_requirements(tmp_path):
    app, stub = live_app(tmp_path)
    stub.skill_result = skill_report()
    with open_client(app) as client:
        job_id = start_skill(client, {**SKILL_SEARCH, "company": COMPANY, "requirements": WANTED})
        wait_api(client, job_id)
    request = stub.calls[0][1]
    assert request.company.name == "Acme" and len(request.requirements) == 2


def test_invalid_company_and_requirement_input_is_a_validation_error(tmp_path):
    app, stub = live_app(tmp_path)
    bad = [
        {"company": {"name": "Acme", "website": "ftp://acme.example"}},
        {"company": {"description": "no name"}},
        {"requirements": [{"kind": "hobby", "label": "Chess"}]},
        {"requirements": [{"kind": "skill", "label": "Rust", "minimum_years": 99}]},
        {"requirements": [{"kind": "skill", "label": "Rust", "priority": "vital"}]},
        {"requirements": [{"kind": "skill", "label": "Rust", "colour": "red"}]},
        {"requirements": [{"kind": "skill", "label": f"Skill {n}"} for n in range(21)]},
    ]
    with open_client(app) as client:
        for extra in bad:
            response = post(client, "/research", {**RESEARCH, **extra})
            assert response.status_code == 422, extra
            assert response.json()["error"]["code"] == "validation_error"
    assert stub.calls == []


def test_events_can_be_polled_in_pages(tmp_path):
    app, _ = live_app(tmp_path)
    with open_client(app) as client:
        job_id = start(client)
        wait_api(client, job_id)
        first = get(client, f"/jobs/{job_id}/events").json()
        later = get(client, f"/jobs/{job_id}/events?after={first['next'] - 3}").json()
        beyond = get(client, f"/jobs/{job_id}/events?after={first['next']}").json()
        junk = get(client, f"/jobs/{job_id}/events?after=-4").json()
    assert first["state"] == "done" and first["stage"] == "Done"
    assert first["next"] == len(first["events"])
    assert [event["seq"] for event in first["events"]] == list(range(first["next"]))
    assert first["events"][-1]["kind"] == "done"
    assert {"stage", "source", "finding", "rating"} <= {event["kind"] for event in first["events"]}
    assert [event["seq"] for event in later["events"]] == list(
        range(first["next"] - 3, first["next"])
    )
    assert beyond["events"] == [] and beyond["next"] == first["next"]
    assert junk["events"] == first["events"]


def test_events_of_other_jobs_and_unknown_jobs_are_not_found(tmp_path):
    app, _ = live_app(tmp_path)
    with open_client(app) as client:
        response = get(client, "/jobs/unknown/events")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_events_need_a_valid_token(tmp_path):
    app, _ = live_app(tmp_path)
    with open_client(app) as client:
        job_id = start(client)
        wait_api(client, job_id)
        assert get(client, f"/jobs/{job_id}/events", token="wrong").status_code == 401
        assert client.get(f"{BASE}/jobs/{job_id}/events").status_code == 401


def test_the_template_list_has_builtin_and_text_only_templates(tmp_path):
    app, _ = live_app(tmp_path)
    with open_client(app) as client:
        response = get(client, "/email/templates")
    templates = response.json()["templates"]
    identifiers = {item["id"] for item in templates}
    assert {"outreach-single", "outreach-batch", "report-team", "shortlist-team"} <= identifiers
    assert {item["category"] for item in templates} == {"single", "batch", "team"}
    assert all(item["builtin"] for item in templates)
    assert all({"subject", "body", "purpose", "name"} <= set(item) for item in templates)


def test_a_single_draft_uses_the_names_and_the_sender(tmp_path):
    app, _ = live_app(tmp_path)
    body = {
        "template": "outreach-single",
        "names": ["Jan Novak", "Petr Svoboda"],
        "sender_name": "Eva Svobodova",
        "company_name": "Acme",
    }
    with open_client(app) as client:
        drafts = post(client, "/email/drafts", body).json()["drafts"]
    assert len(drafts) == 1
    assert drafts[0]["recipient"] == "Jan Novak"
    assert drafts[0]["body"].startswith("Hi Jan,")
    assert "Eva Svobodova at Acme" in drafts[0]["body"]
    assert "Reply STOP" in drafts[0]["body"]


def test_a_batch_makes_one_draft_per_name_up_to_the_configured_limit(tmp_path):
    app, _ = live_app(tmp_path)
    names = [f"Person {number}" for number in range(60)]
    with open_client(app) as client:
        drafts = post(client, "/email/drafts", {"template": "outreach-batch", "names": names})
    items = drafts.json()["drafts"]
    assert len(items) == 50
    assert [item["recipient"] for item in items[:3]] == ["Person 0", "Person 1", "Person 2"]
    assert items[1]["body"].startswith("Hi Person,")


def test_team_drafts_summarise_the_report_of_a_job(tmp_path):
    app, _ = live_app(tmp_path)
    with open_client(app) as client:
        job_id = start(client, {**RESEARCH, "company": COMPANY, "requirements": WANTED})
        wait_api(client, job_id)
        response = post(
            client,
            "/email/drafts",
            {"template": "report-team", "job": job_id, "company_name": "Acme"},
        )
    (draft,) = response.json()["drafts"]
    assert draft["recipient"] == "Jan Novak"
    assert "Overall rating: 3.6 / 5" in draft["body"]
    assert "Requirements covered: 60%." in draft["body"]
    assert "- English (C1): met" in draft["body"]
    assert "Senior Python engineer at Acme since 2021" in draft["body"]
    assert draft["body"].endswith("Decision support only.")


def test_drafts_for_an_unfinished_or_unknown_job(tmp_path):
    app, _ = live_app(tmp_path)
    with open_client(app) as client:
        missing = post(client, "/email/drafts", {"template": "report-team", "job": "nope"})
        no_template = post(client, "/email/drafts", {"template": "does-not-exist"})
    assert missing.status_code == 404 and no_template.status_code == 404


def test_draft_requests_are_validated(tmp_path):
    app, _ = live_app(tmp_path)
    with open_client(app) as client:
        for body in (
            {},
            {"template": "ab"},
            {"template": "outreach-single", "names": "Jan"},
            {"template": "outreach-single", "extra": 1},
            {"template": "outreach-single", "names": ["x"] * 101},
        ):
            assert post(client, "/email/drafts", body).status_code == 422, body


def test_nothing_in_the_api_can_send_an_email(tmp_path):
    app, _ = live_app(tmp_path)
    with open_client(app) as client:
        document = get(client, "/openapi.json").json()
    paths = " ".join(document["paths"])
    assert "/email/drafts" in paths and "/email/templates" in paths
    assert "send" not in paths and "smtp" not in paths.lower()
    summary = document["paths"][f"{BASE}/email/templates"]["get"]["summary"]
    assert "nothing is sent" in summary


def test_the_documented_schemas_include_the_new_request_fields(tmp_path):
    app, _ = live_app(tmp_path)
    with open_client(app) as client:
        schemas = get(client, "/openapi.json").json()["components"]["schemas"]
    assert {"company", "requirements"} <= set(schemas["ApiResearch"]["properties"])
    assert {"company", "requirements"} <= set(schemas["ApiSkillSearch"]["properties"])
    assert {"template", "names", "job"} <= set(schemas["ApiDrafts"]["properties"])
