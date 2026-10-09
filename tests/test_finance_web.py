# tests/test_finance_web.py
from tests.web_helpers import (
    add_user,
    Browser,
    build_app,
    open_client,
)

import re


def boot(tmp_path):
    app, _ = build_app(tmp_path)
    return app


def switch_on(app):
    app.state.ctx.policy.update(finance_enabled=True)


def test_the_module_is_off_until_an_administrator_enables_it(tmp_path):
    app = boot(tmp_path)
    with open_client(app) as client:
        browser = Browser(client)
        browser.logged_in_token()
        assert client.get("/finance").status_code == 404
        assert 'href="/finance"' not in client.get("/settings").text
        switch_on(app)
        assert client.get("/finance").status_code == 200
        assert 'href="/finance"' in client.get("/settings").text


def test_only_the_finance_permissions_open_the_pages(tmp_path):
    app = boot(tmp_path)
    switch_on(app)
    with open_client(app) as one, open_client(app) as two, open_client(app) as three:
        add_user(app, "fin", role="finance")
        add_user(app, "viewer", role="viewer")
        add_user(app, "mgr", role="manager")
        fin, viewer, manager = Browser(one), Browser(two), Browser(three)
        fin_token = fin.signed_in_token("fin")
        viewer.signed_in_token("viewer")
        manager_token = manager.signed_in_token("mgr")
        assert one.get("/finance").status_code == 200
        assert two.get("/finance").status_code == 404
        assert two.get("/finance/export.csv").status_code == 404
        assert three.get("/finance").status_code == 200
        assert "Add an entry" not in three.get("/finance").text
        assert (
            manager.post("/finance/entries", {"day": "2026-01-01"}, token=manager_token).status_code
            == 404
        )
        assert three.get("/finance/export.csv").status_code == 200
        assert (
            fin.post(
                "/finance/entries",
                {"day": "2026-01-01", "kind": "revenue", "amount": "5"},
                token=fin_token,
            ).status_code
            == 303
        )


def test_entries_categories_and_charts_work_together(tmp_path):
    app = boot(tmp_path)
    switch_on(app)
    with open_client(app) as client:
        browser = Browser(client)
        ctx = app.state.ctx
        add_user(app, "fin", role="finance")
        token = browser.signed_in_token("fin", path="/finance")
        made = browser.post(
            "/finance/categories", {"name": "Subscriptions", "kind": "expense"}, token=token
        )
        assert made.status_code == 303
        category = ctx.finance.categories()[0]
        today = ctx.finance.today().isoformat()
        bad = browser.post(
            "/finance/entries", {"day": today, "kind": "revenue", "amount": "abc"}, token=token
        )
        assert bad.status_code == 400 and "must be a number" in bad.text and today in bad.text
        mismatch = browser.post(
            "/finance/entries",
            {"day": today, "kind": "revenue", "amount": "5", "category": category.id},
            token=token,
        )
        assert mismatch.status_code == 400 and "same kind" in mismatch.text
        for fields in (
            {"kind": "revenue", "amount": "12000", "note": "Contract"},
            {
                "kind": "expense",
                "amount": "399.90",
                "category": category.id,
                "note": "Hosting bill",
            },
        ):
            assert (
                browser.post("/finance/entries", {"day": today, **fields}, token=token).status_code
                == 303
            )
        page = client.get("/finance").text
        assert "12,000.00 USD" in page and "399.90 USD" in page and 'class="plot"' in page
        assert "Subscriptions" in page and "plot__bar--1" in page
        assert "11,600.10 USD" in page
        entry = ctx.finance.page(kind="expense").items[0]
        updated = browser.post(
            f"/finance/entries/{entry.id}",
            {
                "day": today,
                "kind": "expense",
                "amount": "100",
                "category": category.id,
                "note": "x",
            },
            token=token,
        )
        assert updated.status_code == 303 and ctx.finance.entry(entry.id).amount == 10_000
        filtered = client.get(f"/finance?kind=revenue&month={today[:7]}").text
        assert "Contract" in filtered and "Hosting bill" not in filtered
        assert browser.post(f"/finance/entries/{entry.id}/delete", token=token).status_code == 303
        assert ctx.finance.entry(entry.id) is None
        assert (
            browser.post(
                f"/finance/categories/{category.id}", {"name": "Software"}, token=token
            ).status_code
            == 303
        )
        assert (
            browser.post("/finance/currency", {"currency": "eur"}, token=token).status_code == 303
        )
        assert "EUR" in client.get("/finance").text
        assert (
            browser.post("/finance/currency", {"currency": "dollars"}, token=token).status_code
            == 400
        )
        assert (
            browser.post(f"/finance/categories/{category.id}/delete", token=token).status_code
            == 303
        )


def test_the_export_is_csv_safe_and_needs_the_export_permission(tmp_path):
    app = boot(tmp_path)
    switch_on(app)
    with open_client(app) as client:
        browser = Browser(client)
        ctx = app.state.ctx
        ctx.finance.add_entry("2026-06-01", "revenue", "10", note="=HYPERLINK(1)")
        add_user(app, "fin", role="finance")
        browser.signed_in_token("fin")
        export = client.get("/finance/export.csv")
        assert export.status_code == 200 and export.headers["content-type"].startswith("text/csv")
        assert "'=HYPERLINK(1)" in export.text and "2026-06-01,revenue,10.00,USD" in export.text
        ctx.accounts.save_role("Reader", "", ["finance.view"])
        add_user(app, "reader", role="Reader")
    with open_client(app) as other:
        browser = Browser(other)
        browser.signed_in_token("reader")
        assert other.get("/finance").status_code == 200
        assert other.get("/finance/export.csv").status_code == 404
        assert re.search("Download CSV", other.get("/finance").text) is None


def test_pagination_and_odd_parameters_are_harmless(tmp_path):
    app = boot(tmp_path)
    switch_on(app)
    with open_client(app) as client:
        browser = Browser(client)
        browser.logged_in_token()
        for index in range(35):
            app.state.ctx.finance.add_entry("2026-06-01", "revenue", str(index + 1))
        assert "Page 1 of 2" in client.get("/finance").text
        assert "Page 2 of 2" in client.get("/finance?page=2").text
        assert client.get("/finance?page=zzz&kind=x&month=nope&category=%00").status_code == 200
