# tests/test_data_web.py
from tests.web_helpers import (
    add_user,
    bearer,
    Browser,
    build_app,
    open_client,
)
from agent.web.api_core import BASE

import json
import re


def boot(tmp_path):
    app, _ = build_app(tmp_path)
    app.state.ctx.policy.update(finance_enabled=True)
    return app


def seed(app):
    ctx = app.state.ctx
    person = ctx.candidates.create("Eva Svobodova", headline="Rust developer", location="Brno")
    ctx.candidates.add_contact(person.id, "email", "eva@example.com")
    ctx.candidates.hire(person.id, "Engineer", started="2026-03-01")
    ctx.finance.add_many([("2026-05-01", "revenue", "100", "Contracts", "Invoice")])
    return person


def upload(browser, token, name, content, **fields):
    return browser.post(
        "/admin/import",
        {"strategy": "skip", **fields},
        token=token,
        files={"file": (name, content, "application/octet-stream")},
    )


def test_the_export_pages_open_only_to_people_with_the_export_permission(tmp_path):
    app = boot(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        add_user(app, "vic", role="viewer")
        browser.signed_in_token("vic")
        for path in (
            "/admin/data",
            "/admin/data/export.json?datasets=candidates",
            "/admin/data/candidates.csv",
            "/admin/import",
        ):
            assert client.get(path).status_code == 404, path


def test_each_role_exports_only_the_data_it_may_read(tmp_path):
    app = boot(tmp_path)
    seed(app)
    app.state.ctx.accounts.save_role("Candidate exporter", "", ["data.export", "candidates.view"])
    with open_client(app) as client:
        browser = Browser(client)
        add_user(app, "exp", role="Candidate exporter")
        browser.signed_in_token("exp", path="/admin/data")
        page = client.get("/admin/data").text
        assert "Candidates with their links" in page
        assert "Contact details" not in page and "Revenue and expenses" not in page
        bundle = client.get(
            "/admin/data/export.json?datasets=candidates&datasets=contacts&datasets=finance"
        )
        assert bundle.status_code == 200
        names = set(json.loads(bundle.text)["datasets"])
        assert names == {"candidates"}
        assert client.get("/admin/data/export.json?datasets=finance").status_code == 404
        assert client.get("/admin/data/contacts.csv").status_code == 404
        assert client.get("/admin/data/candidates.csv").status_code == 200
        assert client.get("/admin/data/profile.csv").status_code == 404
        assert "attachment" in bundle.headers["content-disposition"]
        actions = {entry.action for entry in app.state.ctx.accounts.audit()}
        assert "data.export" in actions


def test_an_administrator_exports_everything_without_secrets(tmp_path):
    app = boot(tmp_path)
    seed(app)
    with open_client(app) as client:
        browser = Browser(client)
        add_user(app, "root", role="admin", email="root@example.com")
        browser.signed_in_token("root", path="/admin/data")
        names = re.findall(r'name="datasets" value="(\w+)"', client.get("/admin/data").text)
        assert {"candidates", "contacts", "hires", "finance", "users", "roles", "audit"} <= set(
            names
        )
        query = "&".join(f"datasets={name}" for name in names)
        document = client.get(f"/admin/data/export.json?{query}").json()
        text = json.dumps(document)
        assert "scrypt" not in text and "password" not in text.lower() and "totp_secret" not in text
        assert document["datasets"]["contacts"][0]["value"] == "eva@example.com"
        assert document["datasets"]["finance"][0]["amount"] == "100.00"


def test_a_preview_then_a_confirmation_imports_an_export_into_another_workspace(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("STORE__PATH", str(tmp_path / "one" / "agent.db"))
    source = boot(tmp_path / "one")
    seed(source)
    with open_client(source) as client:
        browser = Browser(client)
        add_user(source, "root")
        browser.signed_in_token("root", path="/admin/data")
        exported = client.get(
            "/admin/data/export.json?datasets=candidates&datasets=contacts&datasets=hires&datasets=finance"
        ).content
    monkeypatch.setenv("STORE__PATH", str(tmp_path / "two" / "agent.db"))
    target = boot(tmp_path / "two")
    with open_client(target) as client:
        browser = Browser(client)
        add_user(target, "root")
        token = browser.signed_in_token("root", path="/admin/import")
        assert "Import data" in client.get("/admin/import").text
        preview = upload(browser, token, "export.json", exported)
        assert preview.status_code == 200 and "What would change" in preview.text
        assert target.state.ctx.candidates.count() == 0
        found = re.search(r'name="token" value="([^"]+)"', preview.text)
        assert found
        confirm = browser.post("/admin/import/confirm", {"token": found.group(1)}, token=token)
        assert confirm.status_code == 200 and "The import is done" in confirm.text
        ctx = target.state.ctx
        person = ctx.candidates.page().items[0]
        assert person.name == "Eva Svobodova" and person.status == "hired"
        assert ctx.candidates.contacts(person.id)[0].value == "eva@example.com"
        assert ctx.finance.totals()["revenue"] == 10_000
        again = browser.post("/admin/import/confirm", {"token": found.group(1)}, token=token)
        assert again.status_code == 400 and "expired" in again.text
        actions = [entry.action for entry in ctx.accounts.audit()]
        assert "data.import" in actions and "data.import_preview" in actions


def test_a_broken_file_changes_nothing_and_says_why(tmp_path):
    app = boot(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        add_user(app, "root")
        token = browser.signed_in_token("root", path="/admin/import")
        bundle = {
            "format": "hiring-workspace-export",
            "version": 1,
            "datasets": {
                "candidates": [{"name": "Fine Person"}, {"name": "Broken", "status": "nope"}]
            },
        }
        shown = upload(browser, token, "x.json", json.dumps(bundle).encode())
        assert shown.status_code == 200 and "needs fixing" in shown.text and "Row 2" in shown.text
        assert "Import it now" not in shown.text
        assert app.state.ctx.candidates.count() == 0
        for name, content in (("x.json", b"nonsense"), ("empty.json", b""), ("y.csv", b"a,b\n1,2")):
            bad = upload(browser, token, name, content, dataset="templates")
            assert bad.status_code == 400, name
        assert upload(browser, token, "z.json", b"{}", strategy="destroy").status_code == 400


def test_a_csv_file_can_be_imported_for_a_chosen_dataset(tmp_path):
    app = boot(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        add_user(app, "root")
        token = browser.signed_in_token("root", path="/admin/import")
        content = b"name,headline,skills\nEva Svobodova,Rust developer,rust; sql\nBob Beta,,\n"
        preview = upload(browser, token, "people.csv", content, dataset="candidates")
        found = re.search(r'name="token" value="([^"]+)"', preview.text)
        assert preview.status_code == 200 and found
        browser.post("/admin/import/confirm", {"token": found.group(1)}, token=token)
        assert {item.name for item in app.state.ctx.candidates.page().items} == {
            "Eva Svobodova",
            "Bob Beta",
        }


def test_a_preview_belongs_to_the_person_who_made_it(tmp_path):
    app = boot(tmp_path)
    with open_client(app) as one, open_client(app) as two:
        add_user(app, "one")
        add_user(app, "two")
        first, second = Browser(one), Browser(two)
        first_token = first.signed_in_token("one", path="/admin/import")
        second_token = second.signed_in_token("two", path="/admin/import")
        content = json.dumps(
            {
                "format": "hiring-workspace-export",
                "version": 1,
                "datasets": {"candidates": [{"name": "Eva Svobodova"}]},
            }
        ).encode()
        preview = upload(first, first_token, "x.json", content)
        stolen = re.search(r'name="token" value="([^"]+)"', preview.text).group(1)
        refused = second.post("/admin/import/confirm", {"token": stolen}, token=second_token)
        assert refused.status_code == 400 and app.state.ctx.candidates.count() == 0


def test_importing_needs_the_edit_permission_of_every_dataset_in_the_file(tmp_path):
    app = boot(tmp_path)
    app.state.ctx.accounts.save_role("Importer", "", ["data.import", "candidates.edit"])
    with open_client(app) as client:
        browser = Browser(client)
        add_user(app, "imp", role="Importer")
        token = browser.signed_in_token("imp", path="/admin/import")
        bundle = {
            "format": "hiring-workspace-export",
            "version": 1,
            "datasets": {
                "candidates": [{"name": "Eva Svobodova"}],
                "finance": [{"day": "2026-05-01", "kind": "revenue", "amount": "5"}],
            },
        }
        shown = upload(browser, token, "x.json", json.dumps(bundle).encode())
        assert "You may not import this dataset" in shown.text and "Import it now" not in shown.text
        only = dict(bundle, datasets={"candidates": bundle["datasets"]["candidates"]})
        ok = upload(browser, token, "x.json", json.dumps(only).encode())
        assert "Import it now" in ok.text


def test_the_api_exports_and_imports_with_the_same_rules(tmp_path):
    app = boot(tmp_path)
    seed(app)
    app.state.ctx.accounts.save_role(
        "Data API",
        "",
        [
            "api.use",
            "data.export",
            "data.import",
            "candidates.view",
            "candidates.edit",
            "hires.manage",
        ],
    )
    with open_client(app) as client:
        user = add_user(app, "robot", role="Data API")
        _, key = app.state.ctx.accounts.create_key(user.id, "robot", None)
        exported = client.get(f"{BASE}/export", headers=bearer(key))
        assert exported.status_code == 200 and set(exported.json()["datasets"]) == {
            "candidates",
            "hires",
        }
        assert client.get(f"{BASE}/export?datasets=finance", headers=bearer(key)).status_code == 403
        bundle = exported.json()
        dry = client.post(
            f"{BASE}/import?dry_run=1", content=json.dumps(bundle), headers=bearer(key)
        )
        assert dry.status_code == 200 and dry.json()["applied"] is False
        broken = dict(bundle, datasets={"candidates": [{"name": "x", "status": "nope"}]})
        refused = client.post(f"{BASE}/import", content=json.dumps(broken), headers=bearer(key))
        assert refused.status_code == 422 and refused.json()["ok"] is False
        not_a_bundle = client.post(f"{BASE}/import", content=b"[]", headers=bearer(key))
        assert not_a_bundle.status_code == 422
        before = app.state.ctx.candidates.count()
        real = client.post(
            f"{BASE}/import?strategy=skip", content=json.dumps(bundle), headers=bearer(key)
        )
        assert real.status_code == 200 and real.json()["applied"] is True
        assert app.state.ctx.candidates.count() == before
