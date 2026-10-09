# tests/test_api_v2.py
from tests.web_helpers import (
    add_user,
    API_TOKEN,
    bearer,
    build_app,
    open_client,
)
from tests.fakes import (
    hiring_report,
    rated_skill_report,
)
from agent.web.api_core import BASE

import xml.etree.ElementTree as ET
import time

PATH = BASE


def boot(tmp_path, **overrides):
    app, stub = build_app(tmp_path, api_requests=500, **overrides)
    stub.results = [hiring_report()]
    stub.skill_result = rated_skill_report(3.6)
    return app


def key_for(app, username, role, permissions=None):
    accounts = app.state.ctx.accounts
    base = accounts.role_named(role)
    if base is not None and "api.use" not in base.effective() and role != "Quiet":
        role = accounts.save_role(f"{role} API", "", sorted(base.effective() | {"api.use"})).name
    user = add_user(app, username, role=role)
    _, token = app.state.ctx.accounts.create_key(user.id, "test", permissions)
    return user, token


def call(client, method, path, token, **kwargs):
    return client.request(method, f"{PATH}{path}", headers=bearer(token), **kwargs)


def test_a_personal_key_acts_with_its_owners_permissions_only(tmp_path):
    app = boot(tmp_path)
    with open_client(app) as client:
        _, manager = key_for(app, "mgr", "manager")
        _, viewer = key_for(app, "vic", "viewer")
        me = call(client, "GET", "/me", manager).json()
        assert (
            me["role"] == "manager"
            and me["source"] == "key"
            and "finance.view" in me["permissions"]
        )
        assert "config.manage" not in me["permissions"]
        assert call(client, "GET", "/users", manager).status_code == 200
        denied = call(client, "GET", "/users", viewer)
        assert denied.status_code == 403 and denied.json()["error"]["code"] == "forbidden"
        assert call(client, "GET", "/audit", manager).status_code == 403
        assert call(client, "GET", "/roles", manager).status_code == 403
        assert call(client, "POST", "/research", viewer, json={"goal": "hiring"}).status_code == 403
        entries = app.state.ctx.accounts.audit(action="api.denied")
        assert len(entries) >= 3 and entries[0].outcome == "denied"


def test_a_key_can_be_narrowed_and_never_exceeds_its_owner(tmp_path):
    app = boot(tmp_path)
    with open_client(app) as client:
        _, narrow = key_for(app, "mgr", "manager", ["api.use", "candidates.view"])
        assert call(client, "GET", "/candidates", narrow).status_code == 200
        assert call(client, "GET", "/routines", narrow).status_code == 403
        user = app.state.ctx.accounts.user_named("mgr")
        _, wide = app.state.ctx.accounts.create_key(user.id, "wide", ["config.manage", "api.use"])
        assert call(client, "GET", "/me", wide).json()["permissions"] == ["api.use"]


def test_a_key_without_the_api_permission_or_after_revocation_is_refused(tmp_path):
    app = boot(tmp_path)
    ctx = app.state.ctx
    ctx.accounts.save_role("Quiet", "", ["chat.use", "candidates.view"])
    with open_client(app) as client:
        _, quiet = key_for(app, "quiet", "Quiet")
        refused = call(client, "GET", "/candidates", quiet)
        assert (
            refused.status_code == 403
            and "may not use the API" in refused.json()["error"]["message"]
        )
        key_for(app, "root", "admin")
        user, token = key_for(app, "mgr", "manager")
        assert call(client, "GET", "/me", token).status_code == 200
        (key,) = ctx.accounts.keys(user.id)
        ctx.accounts.revoke_key(key.id)
        assert call(client, "GET", "/me", token).status_code == 401
        ctx.accounts.update_user(user.id, status="disabled")
        _, other = key_for(app, "other", "manager")
        ctx.policy.update(allow_api_keys=False)
        assert call(client, "GET", "/me", other).status_code == 404


def test_keys_cannot_be_guessed_or_forged(tmp_path):
    app = boot(tmp_path, api_failures=3)
    with open_client(app) as client:
        _, token = key_for(app, "mgr", "manager")
        prefix, _, _ = token.rpartition("_")
        for bad in (prefix + "_wrong-secret-value", "hra_nope_secret", "hra_", "hra"):
            assert call(client, "GET", "/me", bad).status_code in {401, 429}
        assert call(client, "GET", "/me", token).status_code == 200
        assert call(client, "GET", "/me", "hra_nope_secret").status_code == 429
        assert API_TOKEN not in call(client, "GET", "/me", "hra_x").text


def test_the_openapi_document_and_the_api_page_show_only_what_may_be_used(tmp_path):
    app = boot(tmp_path, api_token=API_TOKEN)
    with open_client(app) as client:
        _, manager = key_for(app, "mgr", "manager")
        _, viewer = key_for(app, "vic", "viewer")
        document = call(client, "GET", "/openapi.json", viewer).json()
        paths = set(document["paths"])
        assert f"{PATH}/candidates" in paths and f"{PATH}/users" not in paths
        assert f"{PATH}/research" not in paths and f"{PATH}/finance/entries" not in paths
        operation = document["paths"][f"{PATH}/candidates"]["get"]
        assert operation["x-permission"] == "candidates.view" and "403" in operation["responses"]
        assert (
            document["paths"][f"{PATH}/candidates/{{candidate_id}}"]["get"]["parameters"][0]["name"]
            == "candidate_id"
        )
        wide = call(client, "GET", "/openapi.json", manager).json()
        assert f"{PATH}/users" in wide["paths"] and f"{PATH}/audit" not in wide["paths"]
        for name in ("ApiCandidate", "ApiRoutine", "ApiEntry"):
            assert name in wide["components"]["schemas"]
        shared = call(client, "GET", "/openapi.json", API_TOKEN).json()
        assert f"{PATH}/finance/entries" not in shared["paths"]


def test_candidates_through_the_api_follow_the_candidate_permissions(tmp_path):
    app = boot(tmp_path)
    with open_client(app) as client:
        ctx = app.state.ctx
        _, rec = key_for(app, "rec", "recruiter")
        _, viewer = key_for(app, "vic", "viewer")
        created = call(
            client,
            "POST",
            "/candidates",
            rec,
            json={
                "name": "Eva Svobodova",
                "headline": "Engineer",
                "skills": ["rust"],
                "links": ["https://github.com/eva"],
            },
        )
        assert created.status_code == 201
        identifier = created.json()["id"]
        assert (
            call(client, "POST", "/candidates", viewer, json={"name": "Nope Nobody"}).status_code
            == 403
        )
        bad = call(client, "POST", "/candidates", rec, json={"name": "x"})
        assert bad.status_code == 422
        unknown = call(client, "POST", "/candidates", rec, json={"name": "Eva Two", "surprise": 1})
        assert unknown.status_code == 422
        hired = call(
            client,
            "POST",
            f"/candidates/{identifier}/hires",
            rec,
            json={"role_title": "Engineer", "email": "eva@example.com", "phone": "+420601234567"},
        )
        assert hired.status_code == 201
        mine = call(client, "GET", f"/candidates/{identifier}", rec).json()
        assert mine["candidate"]["status"] == "hired" and len(mine["contacts"]) == 2
        theirs = call(client, "GET", f"/candidates/{identifier}", viewer).json()
        assert theirs["contacts"] is None and theirs["hires"][0]["role_title"] == "Engineer"
        patched = call(client, "PATCH", f"/candidates/{identifier}", rec, json={"notes": "Strong"})
        assert patched.status_code == 200 and patched.json()["notes"] == "Strong"
        assert patched.json()["headline"] == "Engineer"
        contact = call(
            client,
            "POST",
            f"/candidates/{identifier}/contacts",
            rec,
            json={"kind": "website", "value": "https://eva.dev"},
        )
        assert contact.status_code == 201
        assert (
            call(
                client,
                "POST",
                f"/candidates/{identifier}/contacts",
                viewer,
                json={"kind": "email", "value": "a@b.co"},
            ).status_code
            == 403
        )
        assert call(client, "GET", "/candidates/missing", rec).status_code == 404
        assert call(client, "DELETE", f"/candidates/{identifier}", rec).status_code == 403
        _, boss = key_for(app, "boss", "admin")
        assert call(client, "DELETE", f"/candidates/{identifier}", boss).status_code == 204
        assert ctx.candidates.get(identifier) is None
        ctx.policy.update(candidates_enabled=False)
        assert call(client, "GET", "/candidates", rec).status_code == 404


def test_listings_are_paged_and_filterable(tmp_path):
    app = boot(tmp_path)
    with open_client(app) as client:
        _, rec = key_for(app, "rec", "recruiter")
        for index in range(7):
            app.state.ctx.candidates.create(
                f"Person {index:02d}", employer="Acme" if index % 2 else "Globex"
            )
        first = call(client, "GET", "/candidates?size=3&sort=name", rec).json()
        assert first["total"] == 7 and len(first["items"]) == 3 and first["page"] == 1
        assert first["next"] == f"{PATH}/candidates?page=2&size=3"
        last = call(client, "GET", "/candidates?size=3&page=3&sort=name", rec).json()
        assert len(last["items"]) == 1 and last["next"] is None
        found = call(client, "GET", "/candidates?q=globex&sort=name", rec).json()["items"]
        assert found[0]["name"] == "Person 00" and len(found) == 4
        capped = call(client, "GET", "/candidates?size=100000", rec).json()
        assert capped["size"] == 100
        assert call(client, "GET", "/candidates?page=abc&min=x&status=zzz", rec).status_code == 200


def test_routines_and_finance_through_the_api(tmp_path):
    app = boot(tmp_path)
    app.state.ctx.policy.update(finance_enabled=True)
    with open_client(app) as client:
        _, boss = key_for(app, "boss", "admin")
        _, viewer = key_for(app, "vic", "viewer")
        refused = call(
            client,
            "POST",
            "/routines",
            boss,
            json={"skill": "Rust", "location": "Brno", "lawful_purpose": False},
        )
        assert (
            refused.status_code == 403 and refused.json()["error"]["code"] == "compliance_refused"
        )
        made = call(
            client,
            "POST",
            "/routines",
            boss,
            json={"skill": "Rust", "location": "Brno", "lawful_purpose": True, "min_rating": 4.1},
        )
        assert made.status_code == 201
        identifier = made.json()["id"]
        assert (
            call(client, "POST", f"/routines/{identifier}/pause", boss).json()["status"] == "paused"
        )
        assert call(client, "POST", f"/routines/{identifier}/run", boss).status_code == 422
        assert (
            call(client, "POST", f"/routines/{identifier}/resume", boss).json()["status"]
            == "active"
        )
        assert call(client, "POST", f"/routines/{identifier}/run", boss).status_code == 200
        detail = call(client, "GET", f"/routines/{identifier}", viewer).json()
        assert detail["routine"]["min_rating"] == 4.1 and detail["runs"] == []
        assert call(client, "POST", f"/routines/{identifier}/pause", viewer).status_code == 403
        assert call(client, "DELETE", f"/routines/{identifier}", boss).status_code == 204
        assert call(client, "GET", f"/routines/{identifier}", boss).status_code == 404
        entry = call(
            client,
            "POST",
            "/finance/entries",
            boss,
            json={
                "day": time.strftime("%Y-%m-%d"),
                "kind": "revenue",
                "amount": "1250.50",
                "category": "Contracts",
            },
        )
        assert entry.status_code == 201 and entry.json()["amount"] == "1250.50"
        again = call(
            client,
            "POST",
            "/finance/entries",
            boss,
            json={
                "day": time.strftime("%Y-%m-%d"),
                "kind": "revenue",
                "amount": "10",
                "category": "contracts",
            },
        )
        assert again.status_code == 201
        assert len(call(client, "GET", "/finance/categories", boss).json()["categories"]) == 1
        listing = call(client, "GET", "/finance/entries", boss).json()
        assert listing["total"] == 2 and listing["currency"] == "USD"
        assert call(client, "GET", "/finance/entries", viewer).status_code == 403
        assert (
            call(
                client,
                "POST",
                "/finance/entries",
                boss,
                json={"day": "x", "kind": "revenue", "amount": "1"},
            ).status_code
            == 422
        )


def test_statistics_are_listed_described_and_drawn_per_permission(tmp_path):
    app = boot(tmp_path)
    with open_client(app) as client:
        _, manager = key_for(app, "mgr", "manager")
        _, fin = key_for(app, "fin", "finance")
        index = call(client, "GET", "/stats", manager).json()["categories"]
        assert {item["name"] for item in index} >= {
            "searches",
            "candidates",
            "hires",
            "emails",
            "team",
        }
        assert "finance" not in {item["name"] for item in index}
        app.state.ctx.policy.update(finance_enabled=True)
        assert {
            item["name"] for item in call(client, "GET", "/stats", fin).json()["categories"]
        } == {"finance"}
        detail = call(client, "GET", "/stats/candidates", manager).json()
        assert detail["category"] == "candidates" and detail["charts"][0]["svg"].endswith(
            "/charts/0.svg"
        )
        assert {fact["label"] for fact in detail["facts"]} >= {"Candidates"}
        svg = call(client, "GET", "/stats/candidates/charts/1.svg", manager)
        assert svg.status_code == 200 and svg.headers["content-type"] == "image/svg+xml"
        ET.fromstring(svg.text)
        assert call(client, "GET", "/stats/candidates", fin).status_code == 404
        assert call(client, "GET", "/stats/nothing", manager).status_code == 404
        assert call(client, "GET", "/stats/candidates/charts/9.svg", manager).status_code == 404
        assert call(client, "GET", "/stats/candidates/charts/x.svg", manager).status_code == 404
        assert call(client, "GET", "/stats?window=1", manager).status_code == 200
        searches = call(client, "GET", "/stats/searches?window=7", manager).json()
        assert searches["data"]["days"] == 7


def test_keys_start_jobs_in_the_shared_workspace_but_the_shared_token_stays_private(tmp_path):
    app = boot(tmp_path, api_token=API_TOKEN)
    with open_client(app) as client:
        _, rec = key_for(app, "rec", "recruiter")
        body = {"goal": "hiring", "name": "Jan Novak", "city": "Brno", "lawful_purpose": True}
        started = call(client, "POST", "/research", rec, json=body)
        assert started.status_code == 202, started.text
        job_id = started.json()["id"]
        for _ in range(100):
            if call(client, "GET", f"/jobs/{job_id}", rec).json()["state"] == "done":
                break
            time.sleep(0.02)
        assert call(client, "GET", f"/jobs/{job_id}", rec).json()["state"] == "done"
        assert [item["id"] for item in call(client, "GET", "/jobs", rec).json()["jobs"]] == [job_id]
        assert call(client, "GET", f"/jobs/{job_id}", API_TOKEN).status_code == 404
        assert call(client, "GET", f"/jobs/{job_id}/report.json", rec).status_code == 200
        assert call(client, "DELETE", f"/jobs/{job_id}", rec).status_code == 403


def test_announcements_and_the_administration_listings(tmp_path):
    app = boot(tmp_path)
    with open_client(app) as client:
        ctx = app.state.ctx
        admin, boss = key_for(app, "boss", "admin")
        _, rec = key_for(app, "rec", "recruiter")
        ctx.messenger.announce(admin.id, "Hello", "Everyone")
        assert [
            item["title"]
            for item in call(client, "GET", "/announcements", rec).json()["announcements"]
        ] == ["Hello"]
        ctx.policy.update(messaging_enabled=False)
        assert call(client, "GET", "/announcements", rec).status_code == 404
        users = call(client, "GET", "/users", boss).json()["users"]
        assert {item["username"] for item in users} == {"boss", "rec"}
        assert all("password" not in str(item) for item in users)
        roles = call(client, "GET", "/roles", boss).json()["roles"]
        assert any(
            item["name"] == "admin" and "users.manage" in item["permissions"] for item in roles
        )
        entries = call(client, "GET", "/audit?action=api&size=5", boss).json()["entries"]
        assert isinstance(entries, list)
