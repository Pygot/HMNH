# tests/test_portability.py
from agent.portability import (
    BY_NAME,
    DATASETS,
    FORMAT,
    Portability,
)
from agent.chat import (
    load_profile,
    Profile,
    save_profile,
)
from agent.config import (
    AuthTuning,
    Settings,
)
from agent.candidates import Candidates
from agent.errors import InvalidRequest
from agent.drafts import TemplateStore
from agent.finance import FinanceBook
from agent.accounts import Accounts
from agent.routines import Routines
from agent.mailer import Mailer
from agent.store import Store

import pytest
import json


class World:
    def __init__(self, root, name):
        self.settings = Settings(
            _env_file=None,
            store={"path": root / name / "agent.db"},
            email={"template_dir": root / name / "templates"},
        )
        self.store = Store(self.settings.store)
        self.candidates = Candidates(self.store, self.settings.candidates)
        self.finance = FinanceBook(self.store, self.settings.finance)
        self.routines = Routines(self.store, self.settings.routines)
        self.mailer = Mailer(self.settings, self.store)
        auth = AuthTuning()
        self.accounts = Accounts(self.store, auth)
        self.accounts.sync_roles(auth.roles)
        self.templates = TemplateStore(self.settings.email)
        self.port = Portability(
            self.store,
            self.candidates,
            self.finance,
            self.routines,
            lambda: self.mailer,
            self.accounts,
            lambda: self.templates,
            self.settings.portability,
        )

    def close(self):
        self.store.close()


@pytest.fixture
def make(tmp_path):
    opened = []

    def build(name="a"):
        world = World(tmp_path, name)
        opened.append(world)
        return world

    yield build
    for world in opened:
        world.close()


def everything(_):
    return True


def fill(world):
    person = world.candidates.create(
        "Eva Svobodova",
        headline="Rust developer",
        employer="Acme",
        location="Brno",
        skills=["rust", "sql"],
        tags=["referral"],
        links=["https://www.linkedin.com/in/eva"],
        notes="Met at a meetup",
    )
    world.candidates.add_contact(person.id, "email", "eva@example.com")
    hire = world.candidates.hire(person.id, "Engineer", department="Platform", started="2026-03-01")
    world.candidates.update_hire(
        person.id, hire.id, outcome="left", rating=4, review="Reliable", ended="2026-09-01"
    )
    world.candidates.create("Bob Beta", status="interviewing")
    world.finance.add_many(
        [
            ("2026-05-01", "revenue", "1200.50", "Contracts", "Invoice 1"),
            ("2026-05-02", "expense", "99", "Software", ""),
        ]
    )
    world.routines.create("Rust hunt", "Rust", "Brno", purpose_confirmed=True, min_rating=4.2)
    world.mailer.suppress("stop@example.com", "Asked to stop", "eva")
    world.templates.save(
        world.templates.get("outreach-single").model_copy(
            update={"id": "my-template", "name": "Mine"}
        )
    )
    save_profile(world.store, Profile(company_name="Acme", sender_name="Eva"))
    return person


def test_the_registry_describes_every_dataset_once():
    assert len({item.name for item in DATASETS}) == len(DATASETS)
    for item in DATASETS:
        assert item.view and (item.edit is None or item.edit)
    assert BY_NAME["users"].edit is None and BY_NAME["audit"].edit is None
    assert BY_NAME["candidates"].csv and not BY_NAME["templates"].csv


def test_a_full_export_can_be_imported_into_a_fresh_workspace(make):
    source, target = make("a"), make("b")
    fill(source)
    bundle = source.port.bundle(
        [
            "candidates",
            "contacts",
            "hires",
            "finance",
            "routines",
            "suppressions",
            "templates",
            "profile",
        ]
    )
    assert bundle["format"] == FORMAT and bundle["version"] == 1 and bundle["currency"] == "USD"
    raw = json.dumps(bundle).encode()
    records = target.port.parse_json(raw)
    result = target.port.apply(records, "skip", "importer", everything, False, True)
    assert result.ok and result.applied, [item.errors for item in result.outcomes]
    counts = {item.dataset: item.created for item in result.outcomes}
    assert counts["candidates"] == 2 and counts["contacts"] == 1 and counts["hires"] == 1
    assert counts["finance"] == 2 and counts["routines"] == 1 and counts["suppressions"] == 1
    person = target.candidates.page(query="Eva").items[0]
    assert (
        person.skills == ["rust", "sql"]
        and person.status == "alumni"
        and person.notes == "Met at a meetup"
    )
    assert target.candidates.links(person.id)[0].url.endswith("linkedin.com/in/eva")
    assert target.candidates.contacts(person.id)[0].value == "eva@example.com"
    hires = target.candidates.hires(person.id)
    assert hires[0].rating == 4 and hires[0].outcome == "left" and hires[0].review == "Reliable"
    assert [event.kind for event in target.candidates.events(person.id)][-1] == "created"
    assert target.finance.totals()["revenue"] == 120_050
    (routine,) = target.routines.all()
    assert routine.status == "paused" and routine.purpose_confirmed and routine.min_rating == 4.2
    assert target.mailer.suppressed("stop@example.com")
    assert target.templates.get("my-template") is not None
    assert load_profile(target.store).company_name == "Acme"


def test_importing_the_same_file_twice_adds_nothing_the_second_time(make):
    source, target = make("a"), make("b")
    fill(source)
    records = target.port.parse_json(
        json.dumps(
            source.port.bundle(
                ["candidates", "contacts", "hires", "finance", "suppressions", "routines"]
            )
        ).encode()
    )
    first = target.port.apply(records, "skip", "x", everything, False, True)
    second = target.port.apply(records, "skip", "x", everything, False, True)
    assert first.applied and second.applied
    assert {item.dataset: item.created for item in second.outcomes if item.created} == {}
    assert (
        target.candidates.count() == 2
        and target.finance.count() == 2
        and len(target.routines.all()) == 1
    )


def test_the_update_strategy_overwrites_only_what_the_file_states(make):
    source, target = make("a"), make("b")
    fill(source)
    existing = target.candidates.create(
        "Eva Svobodova", headline="Old headline", location="Brno", notes="keep me"
    )
    records = source.port.parse_json(json.dumps(source.port.bundle(["candidates"])).encode())
    eva = next(item for item in records["candidates"] if item["name"] == "Eva Svobodova")
    eva["notes"] = ""
    skip = target.port.apply(records, "skip", "x", everything, False)
    assert skip.applied and target.candidates.get(existing.id).headline == "Old headline"
    update = target.port.apply(records, "update", "x", everything, False)
    assert update.applied and update.outcomes[0].updated == 2
    changed = target.candidates.get(existing.id)
    assert changed.headline == "Rust developer" and changed.notes == "keep me"
    assert target.candidates.count() == 2


def test_a_dry_run_changes_nothing_but_reports_exactly(make):
    source, target = make("a"), make("b")
    fill(source)
    records = source.port.parse_json(
        json.dumps(source.port.bundle(["candidates", "finance"])).encode()
    )
    preview = target.port.apply(records, "skip", "x", everything, True)
    assert preview.ok and not preview.applied
    assert {item.dataset: item.created for item in preview.outcomes} == {
        "candidates": 2,
        "finance": 2,
    }
    assert target.candidates.count() == 0 and target.finance.count() == 0
    token, shown = target.port.preview(records, "skip", "me", "file.json", everything, "x", False)
    assert shown.ok and token
    assert target.port.take(token, "someone-else") is None
    token, _ = target.port.preview(records, "skip", "me", "file.json", everything, "x", False)
    stash = target.port.take(token, "me")
    assert stash is not None and stash.filename == "file.json"
    assert target.port.take(token, "me") is None


def test_one_bad_row_rolls_the_whole_import_back(make):
    target = make("b")
    records = {
        "candidates": [{"name": "Good Person"}, {"name": "Bad Status", "status": "winning"}],
        "finance": [{"day": "2026-05-01", "kind": "revenue", "amount": "5"}],
    }
    result = target.port.apply(records, "skip", "x", everything, False)
    assert not result.ok and not result.applied
    assert "Row 2" in result.outcomes[0].errors[0] and "status" in result.outcomes[0].errors[0]
    assert target.candidates.count() == 0 and target.finance.count() == 0


def test_validation_errors_name_the_row_and_the_field(make):
    target = make("b")
    records = {
        "candidates": [
            {"name": ""},
            {"name": "Ok Person", "links": [{"url": "javascript:alert(1)"}]},
        ],
        "contacts": [{"candidate": "Nobody", "kind": "email", "value": "a@b.co"}],
        "finance": [{"day": "2026-05-01", "kind": "revenue", "amount": "abc"}],
    }
    result = target.port.apply(records, "skip", "x", everything, True)
    by_name = {item.dataset: item.errors for item in result.outcomes}
    assert "name" in by_name["candidates"][0] or "String" in by_name["candidates"][0]
    assert any("http" in message or "valid" in message for message in by_name["candidates"])
    assert "not found" in by_name["contacts"][0] and "number" in by_name["finance"][0]


def test_permissions_decide_which_datasets_may_be_imported(make):
    target = make("b")
    records = {
        "candidates": [{"name": "Eva Svobodova"}],
        "finance": [{"day": "2026-05-01", "kind": "revenue", "amount": "1"}],
    }
    allowed = {"candidates.edit"}
    result = target.port.apply(records, "skip", "x", lambda name: name in allowed, False)
    assert not result.applied
    assert result.outcomes[1].errors == ["You may not import this dataset."]
    assert target.candidates.count() == 0
    export_only = target.port.apply({"users": [{"username": "x"}]}, "skip", "x", everything, False)
    assert export_only.outcomes[0].errors == ["This dataset cannot be imported."]
    with pytest.raises(InvalidRequest, match="what already exists"):
        target.port.apply(records, "destroy", "x", everything, True)


def test_unknown_files_and_oversized_files_are_refused(make):
    world = make("a")
    for raw in (
        b"not json",
        b"[]",
        b'{"format": "other"}',
        json.dumps({"format": FORMAT, "version": 99}).encode(),
        json.dumps({"format": FORMAT, "version": 1}).encode(),
    ):
        with pytest.raises(InvalidRequest):
            world.port.parse_json(raw)
    with pytest.raises(InvalidRequest, match="too large"):
        world.port.parse_json(b" " * (world.settings.portability.max_bytes + 1))
    rows = [{"name": f"P{n}"} for n in range(world.settings.portability.max_rows + 1)]
    with pytest.raises(InvalidRequest, match="too many"):
        world.port.parse_json(
            json.dumps({"format": FORMAT, "version": 1, "datasets": {"candidates": rows}}).encode()
        )
    ignored = world.port.parse_json(
        json.dumps(
            {"format": FORMAT, "version": 1, "datasets": {"evil": [], "candidates": []}}
        ).encode()
    )
    assert list(ignored) == ["candidates"]


def test_csv_export_is_safe_and_csv_import_understands_it(make):
    source, target = make("a"), make("b")
    fill(source)
    source.candidates.create("=SUM(A1)", notes="+cmd")
    text = source.port.csv_text("candidates")
    assert text.splitlines()[0].startswith("id,name,headline")
    assert "'=SUM(A1)" in text and "'+cmd" in text and "rust; sql" in text
    parsed = target.port.parse_csv("candidates", text.encode())
    result = target.port.apply(parsed, "skip", "x", everything, False)
    assert result.applied and target.candidates.count() == 3
    person = target.candidates.page(query="Eva").items[0]
    assert person.skills == ["rust", "sql"] and target.candidates.links(person.id)
    finance = target.port.parse_csv("finance", source.port.csv_text("finance").encode())
    assert target.port.apply(finance, "skip", "x", everything, False).applied
    assert target.finance.count() == 2
    contacts = target.port.parse_csv("contacts", source.port.csv_text("contacts").encode())
    assert target.port.apply(contacts, "skip", "x", everything, False).applied
    assert target.candidates.contacts(person.id)[0].value == "eva@example.com"
    hires = target.port.parse_csv("hires", source.port.csv_text("hires").encode())
    assert target.port.apply(hires, "skip", "x", everything, False).applied
    assert target.candidates.hires(person.id)[0].role_title == "Engineer"
    for name in ("users", "audit", "mail_log", "routines", "suppressions"):
        assert source.port.csv_text(name).splitlines()
    with pytest.raises(InvalidRequest):
        source.port.csv_text("profile")
    with pytest.raises(InvalidRequest, match="CSV"):
        target.port.parse_csv("templates", b"a,b")
    with pytest.raises(InvalidRequest, match="UTF-8"):
        target.port.parse_csv("candidates", b"\xff\xfe\x00bad")


def test_csv_rows_may_name_the_candidate_by_name_but_not_ambiguously(make):
    target = make("b")
    target.candidates.create("Twin One", location="Brno")
    target.candidates.create("Twin One", location="Prague")
    target.candidates.create("Solo Person")
    ambiguous = target.port.parse_csv("contacts", b"candidate,kind,value\nTwin One,email,a@b.co\n")
    result = target.port.apply(ambiguous, "skip", "x", everything, True)
    assert not result.ok and "ambiguous" in result.outcomes[0].errors[0]
    solo = target.port.parse_csv("contacts", b"candidate,kind,value\nSolo Person,email,a@b.co\n")
    assert target.port.apply(solo, "skip", "x", everything, False).applied


def test_routines_arrive_paused_and_need_a_confirmed_purpose(make):
    target = make("b")
    records = {"routines": [{"skill": "Go", "location": "Brno", "name": "Go hunt"}]}
    refused = target.port.apply(records, "skip", "x", everything, True, False)
    assert not refused.ok
    accepted = target.port.apply(records, "skip", "x", everything, False, True)
    assert accepted.applied and target.routines.all()[0].status == "paused"
    assert target.routines.due() == []


def test_the_export_of_accounts_never_contains_secrets(make):
    world = make("a")
    world.accounts.create_user(
        "eva",
        world.accounts.role_named("admin").id,
        password_hash="scrypt$secret$hash",
        email="eva@example.com",
    )
    text = json.dumps(world.port.bundle(["users", "roles"]))
    assert (
        "scrypt" not in text
        and "password" not in text
        and "totp" not in text.lower().replace("two_step", "")
    )
    assert "eva@example.com" in text and "users.manage" in text
    with pytest.raises(InvalidRequest):
        world.port.bundle([])
    with pytest.raises(InvalidRequest):
        world.port.bundle(["nothing"])


def test_the_stash_expires_and_is_bounded(make):
    world = make("a")
    world.port._clock = lambda: 1000.0
    records = {"candidates": [{"name": "Eva Svobodova"}]}
    tokens = [
        world.port.preview(records, "skip", "me", "f", everything, "x", False)[0] for _ in range(30)
    ]
    assert len(world.port._stash) <= world.settings.portability.stash_max
    world.port._clock = lambda: 1000.0 + 3600
    assert world.port.take(tokens[-1], "me") is None
    assert world.port.available(everything, lambda module: True)
    assert len(world.port.importable(lambda name: name == "finance.edit", lambda module: True)) == 1


def test_absurd_timestamps_numbers_and_constants_are_refused_at_the_door(make):
    world = make("a")
    for created in (1e300, -5, 5e9):
        result = world.port.apply(
            {"candidates": [{"name": "Eva", "created": created}]}, "skip", "x", everything, True
        )
        assert not result.ok
    event = {"name": "Eva", "events": [{"ts": 1e300}]}
    assert not world.port.apply({"candidates": [event]}, "skip", "x", everything, True).ok
    row = '{"name": "Eva", "created": Infinity}'
    raw = f'{{"format": "{FORMAT}", "version": 1, "datasets": {{"candidates": [{row}]}}}}'.encode()
    with pytest.raises(InvalidRequest, match="not valid JSON"):
        world.port.parse_json(raw)
    with pytest.raises(InvalidRequest, match="not valid JSON"):
        world.port.parse_json(b"[" * 100_000)


def test_messy_csv_never_crashes_the_importer(make):
    world = make("a")
    rows = ("name,rating,created,researched\nEva,abc," + "9" * 40 + ",\u0661\n").encode()
    parsed = world.port.parse_csv("candidates", rows)
    first = parsed["candidates"][0]
    assert first["rating"] is None and first["researched"] == 0
    assert first["created"] is None or first["created"] > 0
    assert world.port.apply(parsed, "skip", "x", everything, True) is not None
    with pytest.raises(InvalidRequest, match="not valid CSV"):
        world.port.parse_csv("candidates", b"name\n" + b"x" * 200_000)
    ragged = world.port.parse_csv("candidates", b"name,rating\nEva\n")
    assert ragged["candidates"][0]["name"] == "Eva"


def test_a_template_that_will_fail_stops_the_whole_import_before_anything_is_written(make):
    world = make("a")
    clash = world.templates.get("outreach-single").model_dump(mode="json")
    records = {"candidates": [{"name": "Good Person"}], "templates": [clash]}
    assert not world.port.apply(records, "skip", "x", everything, True).ok
    real = world.port.apply(records, "skip", "x", everything, False)
    assert not real.applied and not real.ok
    assert world.candidates.count() == 0


def test_templates_and_profile_follow_the_chosen_strategy(make):
    world = make("a")
    save_profile(world.store, Profile(company_name="Keep Ltd"))
    base = world.templates.get("outreach-single")
    mine = base.model_copy(update={"id": "mine-1", "name": "Mine"})
    world.templates.save(mine)
    changed = mine.model_copy(update={"name": "Renamed"}).model_dump(mode="json")
    records = {"templates": [changed], "profile": [{"company_name": "New Ltd"}]}
    skipped = world.port.apply(records, "skip", "x", everything, False)
    assert skipped.applied and load_profile(world.store).company_name == "Keep Ltd"
    assert world.templates.get("mine-1").name == "Mine"
    assert {item.dataset: item.skipped for item in skipped.outcomes} == {
        "templates": 1,
        "profile": 1,
    }
    updated = world.port.apply(records, "update", "x", everything, False)
    assert updated.applied and load_profile(world.store).company_name == "New Ltd"
    assert world.templates.get("mine-1").name == "Renamed"


def test_each_person_may_hold_only_a_few_previews(make):
    world = make("a")
    records = {"candidates": [{"name": "Eva Svobodova"}]}
    limit = world.settings.portability.stash_per_owner
    mine = [
        world.port.preview(records, "skip", "me", "f", everything, "x", False)[0]
        for _ in range(limit + 2)
    ]
    other = world.port.preview(records, "skip", "you", "f", everything, "x", False)[0]
    assert world.port.take(mine[0], "me") is None
    assert world.port.take(mine[-1], "me") is not None
    assert world.port.take(other, "you") is not None


def test_names_in_rows_are_matched_exactly_through_one_index(make):
    world = make("a")
    for name in ("Eva One", "Eva Two", "Eva Three", "Eva Four", "Eva Five", "Eva Six", "Eva"):
        world.candidates.create(name)
    records = {"contacts": [{"candidate": "eva", "kind": "email", "value": "e@example.com"}]}
    result = world.port.apply(records, "skip", "x", everything, False)
    assert result.applied and result.outcomes[0].created == 1
    phone = {"candidate": "FRESH FACE", "kind": "phone", "value": "+420 123"}
    made = {"candidates": [{"name": "Fresh Face"}], "contacts": [phone]}
    assert world.port.apply(made, "skip", "x", everything, False).applied
