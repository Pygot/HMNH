# tests/test_api.py
from tests.web_helpers import (
    API_TOKEN,
    bearer,
    Browser,
    build_app,
    clarification,
    LI_A,
    LI_B,
    open_client,
    say,
    TOKEN,
    wait_for,
)
from agent.models import (
    Network,
    ResearchOptions,
    ScepticismMode,
    Strictness,
)
from agent.web.api import (
    api_routes,
    BASE,
    ENDPOINTS,
)
from agent.errors import (
    ComplianceRefusal,
    UpstreamError,
)
from tests.fakes import sceptical_report
from agent.config import AuthTuning
from agent import __version__

import base64
import pytest
import time
import re

RESEARCH = {
    "goal": "hiring",
    "name": "Jan Novak",
    "city": "Brno",
    "employers": ["Acme"],
    "skills": ["python"],
    "lawful_purpose": True,
}
SKILL_SEARCH = {
    "goal": "hiring",
    "skill": "Rust",
    "location": "Brno",
    "limit": 3,
    "lawful_purpose": True,
}
FINISHED = ("done", "failed", "needs_input")


def api_app(tmp_path, **overrides):
    return build_app(tmp_path, api_token=API_TOKEN, **overrides)


def post(client, path, body, token=API_TOKEN):
    return client.post(f"{BASE}{path}", json=body, headers=bearer(token))


def get(client, path, token=API_TOKEN):
    return client.get(f"{BASE}{path}", headers=bearer(token))


def wait_api(client, job_id, states=FINISHED, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = get(client, f"/jobs/{job_id}").json()
        if body["state"] in states:
            return body
        time.sleep(0.02)
    raise AssertionError("job did not finish in time")


def start(client, body=RESEARCH):
    response = post(client, "/research", body)
    assert response.status_code == 202, response.text
    return response.json()["id"]


def start_skill(client, body):
    response = post(client, "/skill-search", body)
    assert response.status_code == 202, response.text
    return response.json()["id"]


def test_the_api_is_disabled_without_a_token_or_keys(tmp_path):
    app, _ = build_app(tmp_path)
    with open_client(app) as client:
        app.state.ctx.policy.update(allow_api_keys=False)
        for response in (
            get(client, "/status"),
            get(client, "/jobs"),
            get(client, "/openapi.json"),
            post(client, "/research", RESEARCH),
            client.get(f"{BASE}/status"),
        ):
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "api_disabled"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"authorization": "Bearer"},
        {"authorization": "Bearer "},
        {"authorization": "Basic dXNlcjpwYXNz"},
        {"authorization": f"Bearer {TOKEN}"},
        {"authorization": "Bearer wrong-token-wrong-token-wrong"},
        {"authorization": API_TOKEN},
    ],
)
def test_missing_or_wrong_credentials_are_rejected(tmp_path, headers):
    app, stub = api_app(tmp_path)
    with open_client(app) as client:
        response = client.post(f"{BASE}/research", json=RESEARCH, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "unauthorized"
    assert API_TOKEN not in response.text
    assert stub.calls == []


def test_the_scheme_name_is_case_insensitive(tmp_path):
    app, _ = api_app(tmp_path)
    with open_client(app) as client:
        for scheme in ("bearer", "BEARER", "Bearer"):
            response = client.get(
                f"{BASE}/status", headers={"authorization": f"{scheme} {API_TOKEN}"}
            )
            assert response.status_code == 200


def test_the_web_access_token_is_not_an_api_credential(tmp_path):
    app, _ = api_app(tmp_path)
    with open_client(app) as client:
        assert get(client, "/status", token=TOKEN).status_code == 401
        browser = Browser(client)
        browser.logged_in_token()
        assert client.get(f"{BASE}/status").status_code == 401
        assert get(client, "/status").status_code == 200


def test_repeated_failures_lock_the_address_out_but_never_the_right_token(tmp_path):
    app, _ = api_app(tmp_path, api_failures=3)
    with open_client(app) as client:
        statuses = [get(client, "/status", token="wrong-" * 6).status_code for _ in range(3)]
        assert statuses == [401, 401, 401]
        locked = get(client, "/status", token="wrong-" * 6)
        assert locked.status_code == 429 and locked.json()["error"]["code"] == "locked_out"
        assert get(client, "/status").status_code == 200
        again = get(client, "/status", token="wrong-" * 6)
        assert again.status_code == 429 and again.json()["error"]["code"] == "locked_out"


def test_successful_requests_do_not_count_as_failures(tmp_path):
    app, _ = api_app(tmp_path, api_failures=2)
    with open_client(app) as client:
        assert all(get(client, "/status").status_code == 200 for _ in range(6))


def test_requests_are_rate_limited(tmp_path):
    app, _ = api_app(tmp_path, api_requests=3)
    with open_client(app) as client:
        assert [get(client, "/status").status_code for _ in range(3)] == [200] * 3
        limited = get(client, "/status")
    assert limited.status_code == 429 and limited.json()["error"]["code"] == "rate_limited"


def test_the_status_endpoint_reports_providers_and_credit(tmp_path):
    app, stub = api_app(tmp_path)
    with open_client(app) as client:
        response = get(client, "/status")
        stub.status_error = UpstreamError("Apify account timed out.")
        cached = get(client, "/status")
    body = response.json()
    assert response.status_code == 200
    assert body["llm_provider"] == "anthropic" and body["version"] == __version__
    assert body["search_provider"] == "apify" and body["unavailable_networks"] == ["linkedin"]
    assert body["apify_remaining_usd"] == 103.0
    assert cached.status_code == 200 and stub.status_calls == 1


def test_an_unavailable_status_is_a_503(tmp_path):
    app, stub = api_app(tmp_path)
    stub.status_error = UpstreamError("Apify account timed out.")
    with open_client(app) as client:
        response = get(client, "/status")
    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "unavailable",
        "message": "Apify account timed out.",
    }


def test_a_research_run_can_be_started_read_downloaded_and_deleted(tmp_path):
    app, stub = api_app(tmp_path)
    with open_client(app) as client:
        started = post(client, "/research", RESEARCH)
        assert started.status_code == 202
        job = started.json()
        assert job["kind"] == "person" and job["state"] == "running" and job["error"] is None
        assert job["links"] == {"self": f"{BASE}/jobs/{job['id']}"}
        assert len(job["id"]) >= 24
        finished = wait_api(client, job["id"])
        assert finished["state"] == "done" and finished["title"] == "Jan Novak"
        assert finished["links"]["report_md"].endswith("/report.md")
        assert finished["report"]["mode"] == "person"
        assert finished["report"]["findings"][0]["source_urls"] == ["https://jan.dev/about"]
        assert finished["clarification"] is None
        listing = get(client, "/jobs").json()["jobs"]
        assert [item["id"] for item in listing] == [job["id"]]
        assert "report" not in listing[0]
        markdown = get(client, f"/jobs/{job['id']}/report.md")
        data = get(client, f"/jobs/{job['id']}/report.json")
        assert markdown.status_code == 200
        assert markdown.headers["content-type"].startswith("text/markdown")
        assert "# Research report: Jan Novak" in markdown.text
        assert data.headers["content-type"] == "application/json"
        assert data.json()["mode"] == "person"
        assert len(list((tmp_path / "out").iterdir())) == 2
        deleted = client.delete(f"{BASE}/jobs/{job['id']}", headers=bearer())
        assert deleted.status_code == 204 and deleted.content == b""
        assert list((tmp_path / "out").iterdir()) == []
        for path in ("", "/report.md", "/report.json"):
            assert get(client, f"/jobs/{job['id']}{path}").status_code == 404
        assert get(client, "/jobs").json() == {"jobs": []}
    request = stub.calls[0][1]
    assert request.identity.name == "Jan Novak" and request.identity.location == "Brno"
    assert request.identity.employers == ["Acme"] and request.identity.skills == ["python"]
    assert request.purpose_confirmed is True


def test_api_responses_carry_the_security_headers(tmp_path):
    app, _ = api_app(tmp_path)
    with open_client(app) as client:
        responses = [get(client, "/status"), get(client, "/status", token="nope" * 8)]
    for response in responses:
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
        assert "script-src 'nonce-" in response.headers["content-security-policy"]
        assert "server" not in response.headers


def test_options_in_the_body_reach_the_pipeline(tmp_path):
    app, stub = api_app(tmp_path)
    body = {
        **RESEARCH,
        "focus": "senior backend",
        "aliases": ["Honza"],
        "roles": ["Engineer"],
        "options": {
            "networks": ["web", "linkedin"],
            "strictness": "strict",
            "threshold": 0.75,
            "margin": 0.2,
            "max_web_pages": 3,
            "scepticism": "strict",
        },
    }
    with open_client(app) as client:
        wait_api(client, start(client, body))
    request = stub.calls[0][1]
    assert request.options == ResearchOptions(
        networks=[Network.WEB, Network.LINKEDIN],
        strictness=Strictness.STRICT,
        threshold=0.75,
        margin=0.2,
        max_web_pages=3,
        scepticism=ScepticismMode.STRICT,
    )
    assert request.focus == "senior backend"
    assert request.identity.aliases == ["Honza"] and request.identity.roles == ["Engineer"]


def test_a_missing_options_object_means_the_defaults(tmp_path):
    app, stub = api_app(tmp_path)
    with open_client(app) as client:
        wait_api(client, start(client))
    assert stub.calls[0][1].options == ResearchOptions()


def test_a_suspicious_profile_is_visible_in_the_json(tmp_path):
    app, stub = api_app(tmp_path)
    stub.results = [sceptical_report()]
    with open_client(app) as client:
        body = wait_api(client, start(client))
    rating = body["report"]["rating"]
    assert rating["raw_overall"] == 4.4 and rating["overall"] == 2.5
    assert rating["scepticism"]["level"] == "high"
    assert rating["scepticism"]["flags"][0]["code"] == "overlapping_roles"


def test_refused_requests_end_as_failed_jobs_with_the_reason(tmp_path):
    app, stub = api_app(tmp_path)
    stub.results = [ComplianceRefusal("Refused: the request looks like an attempt to stalk.")]
    with open_client(app) as client:
        body = wait_api(client, start(client))
    assert body["state"] == "failed"
    assert body["error"].startswith("Refused:")
    assert body["report"] is None and "report_md" not in body["links"]


def test_unexpected_failures_do_not_leak_details(tmp_path):
    app, stub = api_app(tmp_path)
    stub.results = [RuntimeError("database password is hunter2")]
    with open_client(app) as client:
        body = wait_api(client, start(client))
    assert body["state"] == "failed" and "hunter2" not in str(body)


@pytest.mark.parametrize(
    ("changes", "status", "code"),
    [
        ({"lawful_purpose": False}, 403, "compliance_refused"),
        ({"lawful_purpose": None}, 422, "validation_error"),
        ({"goal": "stalking"}, 422, "validation_error"),
        ({"goal": None}, 422, "validation_error"),
        ({"unknown": 1}, 422, "validation_error"),
        ({"city": "Main Street 5"}, 422, "validation_error"),
        ({"name": ""}, 422, "validation_error"),
        ({"employers": "Acme"}, 422, "validation_error"),
        ({"options": {"networks": []}}, 422, "validation_error"),
        ({"options": {"threshold": 5}}, 422, "validation_error"),
        ({"options": {"scepticism": "off"}}, 422, "validation_error"),
        ({"options": {"unknown": 1}}, 422, "validation_error"),
        ({"name": None}, 422, "invalid_request"),
        ({"cv": {"filename": "cv.pdf", "content_base64": "JVBERg=="}}, 422, "invalid_request"),
        (
            {"cv": {"filename": "", "content_base64": "JVBERg=="}, "name": None},
            422,
            "validation_error",
        ),
    ],
)
def test_invalid_research_bodies_are_rejected_before_anything_starts(
    tmp_path, changes, status, code
):
    app, stub = api_app(tmp_path)
    body = {key: value for key, value in {**RESEARCH, **changes}.items() if value is not None}
    with open_client(app) as client:
        response = post(client, "/research", body)
        assert get(client, "/jobs").json() == {"jobs": []}
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert stub.calls == []


def test_a_refusal_names_the_missing_confirmation(tmp_path):
    app, _ = api_app(tmp_path)
    with open_client(app) as client:
        response = post(client, "/research", {**RESEARCH, "lawful_purpose": False})
    assert "lawful purpose" in response.json()["error"]["message"]


def test_the_body_must_be_json(tmp_path):
    app, stub = api_app(tmp_path)
    with open_client(app) as client:
        text = client.post(
            f"{BASE}/research",
            content=b"name=Jan",
            headers={**bearer(), "content-type": "application/x-www-form-urlencoded"},
        )
        broken = client.post(
            f"{BASE}/research",
            content=b"{not json",
            headers={**bearer(), "content-type": "application/json"},
        )
        listed = client.post(
            f"{BASE}/research",
            content=b"[1, 2]",
            headers={**bearer(), "content-type": "application/json; charset=utf-8"},
        )
    assert text.status_code == 422 and "application/json" in text.json()["error"]["message"]
    assert broken.status_code == 422 and "not valid JSON" in broken.json()["error"]["message"]
    assert listed.status_code == 422 and listed.json()["error"]["code"] == "validation_error"
    assert stub.calls == []


def test_a_cv_can_be_sent_as_base64(tmp_path):
    app, stub = api_app(tmp_path)
    cv = b"%PDF-1.4 pretend content"
    body = {
        "goal": "sales",
        "lawful_purpose": True,
        "cv": {"filename": "resume.pdf", "content_base64": base64.b64encode(cv).decode()},
    }
    with open_client(app) as client:
        job = wait_api(client, start(client, body))
    assert job["state"] == "done" and job["title"] == "Cv Person"
    assert stub.calls[0] == ("cv", len(cv), "resume.pdf")
    assert stub.calls[1][1].goal.value == "sales"


def test_invalid_cv_payloads_are_rejected(tmp_path):
    app, stub = api_app(tmp_path)
    good = base64.b64encode(b"%PDF-1.4").decode()
    cv = {"filename": "resume.pdf", "content_base64": good}
    cases = {
        "not base64": ({"cv": {**cv, "content_base64": "***not base64***"}}, "not valid base64"),
        "with a name": ({"name": "Jan Novak", "cv": cv}, "exactly one"),
        "with fields": ({"city": "Brno", "cv": cv}, "cannot be combined"),
    }
    base = {"goal": "hiring", "lawful_purpose": True}
    with open_client(app) as client:
        for body, message in cases.values():
            response = post(client, "/research", {**base, **body})
            assert response.status_code == 422
            assert message in response.json()["error"]["message"]
    assert stub.calls == []


def test_oversized_cvs_are_rejected_by_the_handler_or_the_body_limit(tmp_path):
    app, stub = api_app(tmp_path)
    limit = app.state.ctx.settings.cv.max_bytes
    base = {"goal": "hiring", "lawful_purpose": True}

    def body(size):
        content = base64.b64encode(b"%PDF" + b"0" * (size - 4)).decode()
        return {**base, "cv": {"filename": "cv.pdf", "content_base64": content}}

    with open_client(app) as client:
        slightly = post(client, "/research", body(limit + 1))
        huge = client.post(f"{BASE}/research", json=body(limit + limit // 2), headers=bearer())
    assert slightly.status_code == 422 and "at most" in slightly.json()["error"]["message"]
    assert huge.status_code == 413
    assert stub.calls == []


def test_a_skill_search_returns_a_ranked_report(tmp_path):
    app, stub = api_app(tmp_path)
    body = {**SKILL_SEARCH, "options": {"networks": ["web"], "scepticism": "strict"}}
    with open_client(app) as client:
        started = post(client, "/skill-search", body)
        assert started.status_code == 202 and started.json()["kind"] == "skill"
        job = wait_api(client, started.json()["id"])
    assert job["state"] == "done" and job["title"] == "Rust in Brno"
    assert job["report"]["candidates"][0]["best"] is True
    request = stub.calls[0][1]
    assert request.limit == 3 and request.options.networks == [Network.WEB]
    assert request.options.scepticism is ScepticismMode.STRICT


@pytest.mark.parametrize(
    ("changes", "status", "code"),
    [
        ({"lawful_purpose": False}, 403, "compliance_refused"),
        ({"limit": 11}, 422, "validation_error"),
        ({"limit": 0}, 422, "validation_error"),
        ({"skill": ""}, 422, "validation_error"),
        ({"location": "Main Street 5"}, 422, "validation_error"),
        ({"cv": {}}, 422, "validation_error"),
    ],
)
def test_invalid_skill_searches_are_rejected(tmp_path, changes, status, code):
    app, stub = api_app(tmp_path)
    with open_client(app) as client:
        response = post(client, "/skill-search", {**SKILL_SEARCH, **changes})
    assert response.status_code == status and response.json()["error"]["code"] == code
    assert stub.calls == []


def test_the_default_skill_search_limit_is_five(tmp_path):
    app, stub = api_app(tmp_path)
    body = {key: value for key, value in SKILL_SEARCH.items() if key != "limit"}
    with open_client(app) as client:
        wait_api(client, start_skill(client, body))
    assert stub.calls[0][1].limit == 5


def test_a_clarification_can_be_read_and_answered(tmp_path):
    app, stub = api_app(tmp_path)
    stub.results = [clarification()]
    with open_client(app) as client:
        job_id = start(client)
        waiting = wait_api(client, job_id)
        assert waiting["state"] == "needs_input"
        assert waiting["links"]["answers"] == f"{BASE}/jobs/{job_id}/answers"
        question = waiting["clarification"]["questions"][0]
        assert question["network"] == "linkedin"
        assert [option["url"] for option in question["options"]] == [LI_A, LI_B]
        empty = post(client, f"/jobs/{job_id}/answers", {"answers": []})
        assert empty.status_code == 422
        foreign = post(
            client,
            f"/jobs/{job_id}/answers",
            {"answers": [{"network": "linkedin", "chosen_url": "https://x.example/in/a"}]},
        )
        assert foreign.status_code == 400
        assert foreign.json()["error"]["code"] == "request_failed"
        assert "offered options" in foreign.json()["error"]["message"]
        accepted = post(
            client,
            f"/jobs/{job_id}/answers",
            {"answers": [{"network": "linkedin", "chosen_url": LI_B}]},
        )
        assert accepted.status_code == 202 and accepted.json()["state"] == "running"
        assert wait_api(client, job_id, ("done",))["state"] == "done"
        again = post(
            client,
            f"/jobs/{job_id}/answers",
            {"answers": [{"network": "linkedin", "chosen_url": LI_B}]},
        )
        assert again.status_code == 400 and "not waiting" in again.json()["error"]["message"]
    assert LI_B in stub.calls[-1][1].identity.links


def test_answers_can_skip_a_network_or_narrow_the_search(tmp_path):
    app, stub = api_app(tmp_path)
    stub.results = [clarification(), sceptical_report(), clarification(), sceptical_report()]
    with open_client(app) as client:
        first = start(client)
        wait_api(client, first)
        post(client, f"/jobs/{first}/answers", {"answers": [{"network": "linkedin"}]})
        wait_api(client, first, ("done",))
        second = start(client)
        wait_api(client, second)
        narrowed = {"answers": [{"network": "linkedin", "employer": "Acme", "location": "Brno"}]}
        assert post(client, f"/jobs/{second}/answers", narrowed).status_code == 202
        wait_api(client, second, ("done",))
    assert stub.calls[1][1].skipped_networks == [Network.LINKEDIN]
    assert stub.calls[3][1].identity.employers[0] == "Acme"


def test_unknown_jobs_are_not_found(tmp_path):
    app, _ = api_app(tmp_path)
    with open_client(app) as client:
        for path in ("", "/report.md", "/report.json"):
            response = get(client, f"/jobs/missing-id{path}")
            assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"
        assert post(client, "/jobs/missing-id/answers", {"answers": []}).status_code == 404
        assert client.delete(f"{BASE}/jobs/missing-id", headers=bearer()).status_code == 404


def test_reports_are_unavailable_until_the_job_is_done(tmp_path):
    app, stub = api_app(tmp_path)
    gate = stub.hold()
    with open_client(app) as client:
        job_id = start(client)
        running = get(client, f"/jobs/{job_id}/report.md")
        assert running.status_code == 404 and running.json()["error"]["code"] == "no_report"
        assert get(client, f"/jobs/{job_id}").json()["state"] == "running"
        gate.set()
        wait_api(client, job_id)


def test_the_running_job_limit_is_enforced(tmp_path):
    app, stub = api_app(tmp_path, max_jobs_per_session=1)
    gate = stub.hold()
    with open_client(app) as client:
        start(client)
        blocked = post(client, "/research", RESEARCH)
        skill = post(client, "/skill-search", SKILL_SEARCH)
        gate.set()
    for response in (blocked, skill):
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "too_many_jobs"


def test_api_jobs_and_web_jobs_are_invisible_to_each_other(tmp_path):
    app, _ = api_app(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        token = browser.logged_in_token()
        api_id = start(client)
        wait_api(client, api_id)
        first = say(browser, token, "Eva Svobodova").json()
        web_id = say(browser, token, "yes", first["thread"]["id"]).json()["job_id"]
        web_path = f"/jobs/{web_id}"
        wait_for(browser, web_path)
        assert client.get(f"/jobs/{api_id}").status_code == 404
        assert get(client, f"/jobs/{web_id}").status_code == 404
        assert [item["id"] for item in get(client, "/jobs").json()["jobs"]] == [api_id]
        assert api_id not in client.get("/").text


def test_the_openapi_document_describes_every_endpoint(tmp_path):
    app, _ = api_app(tmp_path)
    with open_client(app) as client:
        response = get(client, "/openapi.json")
    document = response.json()
    assert response.status_code == 200 and document["openapi"] == "3.1.0"
    assert document["info"]["version"] == __version__
    documented = {
        (method.upper(), path) for path, item in document["paths"].items() for method in item
    }
    service = set(AuthTuning().roles["service"].permissions)
    allowed = {
        (item["method"], item["path"])
        for item in ENDPOINTS
        if item["permission"] in service and item["module"] in (None, "candidates_enabled")
    }
    assert documented == allowed and len(documented) >= 15
    assert (f"{BASE}/finance/entries") not in document["paths"]
    assert document["components"]["securitySchemes"]["bearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
    }
    schemas = document["components"]["schemas"]
    for name in ("ApiResearch", "ApiSkillSearch", "ApiAnswers", "PersonReport", "ResearchOptions"):
        assert name in schemas
    references = set(re.findall(r'"#/components/schemas/(\w+)"', response.text))
    assert references and references <= set(schemas)
    assert API_TOKEN not in response.text and TOKEN not in response.text
    research = document["paths"][f"{BASE}/research"]["post"]
    assert research["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ApiResearch"
    }
    assert research["security"] == [{"bearerAuth": []}]


def test_the_documented_endpoints_match_the_registered_routes():
    registered = {
        (method, route.path) for route in api_routes() for method in route.methods - {"HEAD"}
    }
    assert registered == {(item["method"], item["path"]) for item in ENDPOINTS}


def test_the_request_models_reject_unknown_fields_and_bound_the_inputs(tmp_path):
    app, _ = api_app(tmp_path)
    with open_client(app) as client:
        schema = get(client, "/openapi.json").json()["components"]["schemas"]
    assert schema["ApiResearch"]["additionalProperties"] is False
    assert schema["ApiSkillSearch"]["properties"]["limit"]["maximum"] == 10
    assert schema["ApiAnswers"]["properties"]["answers"]["minItems"] == 1
