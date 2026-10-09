# tests/test_candidates.py
from agent.candidates import (
    Candidates,
    link_key,
    name_key,
)
from tests.fakes import (
    hiring_report,
    person_report,
    skill_report,
)
from agent.config import (
    CandidateTuning,
    StoreTuning,
)
from agent.errors import InvalidRequest
from agent.models import Identity
from agent.db import Database

import pytest


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now

    def tick(self, seconds=60):
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def make(tmp_path, clock):
    opened = []

    def build(**changes):
        database = Database(StoreTuning(path=tmp_path / "data" / "agent.db"), clock)
        opened.append(database)
        return Candidates(database, CandidateTuning(**changes), clock)

    yield build
    for database in opened:
        database.close()


@pytest.fixture
def candidates(make):
    return make()


def test_names_and_links_are_normalised_for_matching():
    assert name_key("  Jan   NOVÁK ") == "jan novak"
    assert (
        link_key("https://www.linkedin.com/in/Jan-Novak/?utm_source=x")
        == "linkedin.com/in/jan-novak"
    )
    assert link_key("http://example.com/a/") == link_key("https://example.com/a")


def test_creating_and_updating_a_candidate(candidates, clock):
    person = candidates.create(
        "  Jan Novak ",
        headline="Engineer",
        skills=["python", "python", " rust "],
        tags=["brno", "senior"],
        links=["https://github.com/jan"],
        actor="eva",
    )
    assert person.name == "Jan Novak" and person.status == "sourced"
    assert person.skills == ["python", "rust"] and person.tags == ["brno", "senior"]
    assert candidates.links(person.id)[0].url == "https://github.com/jan"
    clock.tick()
    changed = candidates.update(person.id, status="interviewing", notes="Strong portfolio")
    assert changed.status == "interviewing" and changed.updated > person.updated
    events = candidates.events(person.id)
    assert [event.kind for event in events] == ["status", "created"]
    with pytest.raises(InvalidRequest, match="status"):
        candidates.update(person.id, status="winning")
    with pytest.raises(InvalidRequest, match="name"):
        candidates.create("x")
    with pytest.raises(InvalidRequest, match="http"):
        candidates.add_link(person.id, "javascript:alert(1)")
    assert candidates.delete(person.id) is True
    assert candidates.get(person.id) is None
    assert candidates.events(person.id) == []


def test_the_candidate_limit_is_enforced(make):
    small = make(max_candidates=2)
    small.create("Ann One")
    small.create("Bob Two")
    with pytest.raises(InvalidRequest, match="too many"):
        small.create("Cy Three")


def test_a_research_report_creates_then_updates_the_same_candidate(candidates, clock):
    report = person_report()
    first = candidates.record(report, "job-1")[0]
    assert first.name == "Jan Novak" and first.employer == "Acme" and first.researched == 1
    assert first.rating == pytest.approx(report.rating.overall)
    clock.tick()
    again = candidates.record(hiring_report(), "job-2")[0]
    assert again.id == first.id and again.researched == 2
    assert candidates.count() == 1
    kinds = [event.kind for event in candidates.events(first.id)]
    assert kinds.count("research") == 2


def test_a_shared_profile_link_wins_over_a_different_name_spelling(candidates):
    base = candidates.create("Jan Novak", links=["https://www.linkedin.com/in/jan-novak"])
    other = Identity(name="Johnny Novak", links=["https://linkedin.com/in/jan-novak/"])
    assert candidates.find_match(other).id == base.id


def test_two_people_with_the_same_name_are_not_merged_when_it_is_unclear(candidates):
    candidates.create("Jan Novak", location="Brno")
    candidates.create("Jan Novak", location="Prague")
    assert candidates.find_match(Identity(name="Jan Novak")) is None
    assert candidates.find_match(Identity(name="Jan Novak", location="Brno")).location == "Brno"
    assert candidates.find_match(Identity(name="Jan Novak", location="Ostrava")) is None


def test_a_skill_search_records_every_ranked_candidate(candidates):
    found = candidates.record(skill_report(), "job-3")
    assert [item.name for item in found] == ["Jan Novak"]
    assert candidates.links(found[0].id)[0].url == "https://github.com/jan"


def test_contacts_are_validated_deduplicated_and_limited(make):
    store = make(max_contacts=3)
    person = store.create("Eva Svobodova")
    assert store.add_contact(person.id, "email", "Eva@Example.com") is True
    assert store.add_contact(person.id, "email", "eva@example.com") is False
    assert store.add_contact(person.id, "phone", "+420 601 234 567") is True
    with pytest.raises(InvalidRequest, match="email"):
        store.add_contact(person.id, "email", "nope")
    with pytest.raises(InvalidRequest, match="phone"):
        store.add_contact(person.id, "phone", "abc")
    with pytest.raises(InvalidRequest, match="http"):
        store.add_contact(person.id, "website", "ftp://x")
    with pytest.raises(InvalidRequest, match="kind"):
        store.add_contact(person.id, "fax", "1")
    assert store.add_contact(person.id, "other", "Twitter handle") is True
    with pytest.raises(InvalidRequest, match="too many"):
        store.add_contact(person.id, "address", "Somewhere 1")
    listed = store.contacts(person.id)
    assert [item.value for item in listed if item.kind == "email"] == ["eva@example.com"]
    assert store.remove_contact(person.id, listed[0].id) is True
    assert store.remove_contact(person.id, listed[0].id) is False


def test_hiring_saves_the_hire_the_status_and_the_contacts_together(candidates, clock):
    person = candidates.create("Eva Svobodova")
    clock.tick()
    hired = candidates.hire(
        person.id,
        "Data engineer",
        department="Platform",
        started="2026-03-01",
        contacts=[("email", "eva@example.com"), ("phone", "+420601234567"), ("email", "")],
        job_id="job-9",
        actor="boss",
    )
    assert hired.outcome == "active" and hired.job_id == "job-9" and hired.started == "2026-03-01"
    updated = candidates.get(person.id)
    assert updated.status == "hired" and updated.hired_before is True
    assert {(item.kind, item.source) for item in candidates.contacts(person.id)} == {
        ("email", "hire"),
        ("phone", "hire"),
    }
    assert (
        candidates.events(person.id)[0].summary == "Hired as Data engineer; 2 contact details saved"
    )
    with pytest.raises(InvalidRequest, match="role"):
        candidates.hire(person.id, "  ")
    with pytest.raises(InvalidRequest, match="Dates"):
        candidates.hire(person.id, "Role", started="01/03/2026")
    with pytest.raises(InvalidRequest, match="email"):
        candidates.hire(person.id, "Role", contacts=[("email", "bad")])
    assert len(candidates.hires(person.id)) == 1


def test_a_failed_hire_changes_nothing(candidates):
    person = candidates.create("Eva Svobodova")
    with pytest.raises(InvalidRequest):
        candidates.hire(person.id, "Role", contacts=[("email", "bad")])
    assert candidates.hires(person.id) == []
    assert candidates.get(person.id).status == "sourced"


def test_the_outcome_and_rating_of_a_hire_can_be_recorded_later(candidates):
    person = candidates.create("Eva Svobodova")
    hire = candidates.hire(person.id, "Analyst")
    done = candidates.update_hire(
        person.id, hire.id, outcome="left", rating=4, review="Reliable.", ended="2026-09-30"
    )
    assert done.outcome == "left" and done.rating == 4 and done.ended == "2026-09-30"
    assert candidates.get(person.id).status == "alumni"
    with pytest.raises(InvalidRequest, match="rating"):
        candidates.update_hire(person.id, hire.id, rating=9)
    with pytest.raises(InvalidRequest, match="outcome"):
        candidates.update_hire(person.id, hire.id, outcome="vanished")
    cleared = candidates.update_hire(person.id, hire.id, clear_rating=True)
    assert cleared.rating is None
    with pytest.raises(InvalidRequest, match="does not exist"):
        candidates.update_hire("other", hire.id, rating=3)
    assert candidates.delete_hire(person.id, hire.id) is True


def test_a_second_active_hire_keeps_the_candidate_hired(candidates):
    person = candidates.create("Eva Svobodova")
    first = candidates.hire(person.id, "Analyst")
    candidates.hire(person.id, "Lead")
    candidates.update_hire(person.id, first.id, outcome="left")
    assert candidates.get(person.id).status == "hired"


def test_the_list_filters_sorts_and_pages(candidates, clock):
    for index, (name, rating) in enumerate(
        [("Ann Alpha", 4.5), ("Bob Beta", 3.0), ("Cy Gamma", None)]
    ):
        clock.tick()
        person = candidates.create(name, employer="Acme" if index != 1 else "Globex")
        if rating is not None:
            candidates._db.run("UPDATE candidates SET rating = ? WHERE id = ?", (rating, person.id))
    hired = candidates.create("Dee Delta")
    candidates.hire(hired.id, "Role")
    assert [item.name for item in candidates.page(sort="name").items][:2] == [
        "Ann Alpha",
        "Bob Beta",
    ]
    assert [item.name for item in candidates.page(query="globex").items] == ["Bob Beta"]
    assert [item.name for item in candidates.page(minimum=4.0).items] == ["Ann Alpha"]
    assert [item.name for item in candidates.page(hired=True).items] == ["Dee Delta"]
    assert len(candidates.page(hired=False).items) == 3
    assert [item.name for item in candidates.page(status="hired").items] == ["Dee Delta"]
    assert candidates.page(sort="rating").items[0].name == "Ann Alpha"
    paged = candidates.page(size=2, offset=2, sort="name")
    assert paged.total == 4 and len(paged.items) == 2


def test_seen_before_reports_earlier_research_and_hires(candidates, clock):
    report = person_report()
    assert candidates.seen(report.identity, "job-1") is None
    candidates.record(report, "job-1")
    assert candidates.seen(report.identity, "job-1") is None
    clock.tick()
    candidates.record(report, "job-2")
    seen = candidates.seen(report.identity, "job-2")
    assert seen is not None and len(seen.ratings) == 1 and seen.hires == []
    person = candidates.find_match(report.identity)
    candidates.hire(person.id, "Engineer")
    again = candidates.seen(report.identity, "job-2")
    assert again.hires[0].role_title == "Engineer"


def test_old_candidates_are_purged_but_hired_people_are_kept(make, clock):
    store = make(retention_days=30)
    stale = store.create("Old Sourced")
    kept = store.create("Old Hired")
    store.hire(kept.id, "Role")
    clock.tick(31 * 86_400)
    fresh = store.create("New One")
    assert store.purge() == 1
    assert store.get(stale.id) is None and store.get(kept.id) is not None
    assert store.get(fresh.id) is not None
    assert make(retention_days=0).purge() == 0
