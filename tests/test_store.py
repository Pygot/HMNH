# tests/test_store.py
from agent.store import (
    JobRecord,
    Store,
)
from agent.config import StoreTuning

import pytest


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def make(tmp_path):
    opened = []

    def build(clock=None, **settings):
        tuning = StoreTuning(path=tmp_path / "data" / "agent.db", **settings)
        store = Store(tuning, clock or Clock())
        opened.append(store)
        return store

    yield build
    for store in opened:
        store.close()


def record(identifier="job-1", **extra):
    values = {
        "id": identifier,
        "owner": "web",
        "kind": "person",
        "title": "Jan Novak",
        "state": "done",
        "stage": "Done",
        "created": 1000.0,
        "updated": 1000.0,
    }
    values.update(extra)
    return JobRecord(**values)


def test_the_database_file_and_its_folder_are_created(make, tmp_path):
    store = make()
    assert store.counts() == {"threads": 0, "messages": 0, "jobs": 0, "memory": 0}
    assert (tmp_path / "data" / "agent.db").is_file()
    store.close()


def test_threads_are_created_listed_newest_first_and_renamed(make, tmp_path):
    clock = Clock()
    store = make(clock)
    first = store.create_thread("research", "First")
    clock.now += 10
    second = store.create_thread("research", "Second", {"phase": "idle"})
    clock.now += 10
    other = store.create_thread("tie", "Tie")
    assert [item.title for item in store.threads("research")] == ["Second", "First"]
    assert [item.title for item in store.threads("tie")] == ["Tie"]
    assert second.state == {"phase": "idle"} and first.state == {}
    store.rename_thread(first.id, "Renamed")
    assert store.thread(first.id).title == "Renamed"
    assert store.thread("missing") is None
    assert other.kind == "tie"


def test_messages_keep_their_order_roles_and_data(make, tmp_path):
    store = make()
    thread = store.create_thread("research", "Chat")
    store.add_message(thread.id, "user", "text", "Look into Jan")
    reply = store.add_message(thread.id, "assistant", "proposal", "Shall I?", {"task": "x"})
    store.add_message(thread.id, "user", "text", "yes")
    found = store.messages(thread.id)
    assert [item.text for item in found] == ["Look into Jan", "Shall I?", "yes"]
    assert [item.role for item in found] == ["user", "assistant", "user"]
    assert found[1].data == {"task": "x"} and found[1].kind == "proposal"
    assert [item.id for item in store.messages(thread.id, after=reply.id)] == [found[2].id]
    assert store.message(reply.id).text == "Shall I?"
    assert store.message(9999) is None
    assert [item.text for item in store.recent_messages(thread.id, 2)] == ["Shall I?", "yes"]


def test_adding_a_message_touches_the_thread_and_trims_long_text(make, tmp_path):
    clock = Clock()
    store = make(clock, message_chars=100)
    thread = store.create_thread("research", "Chat")
    older = store.create_thread("research", "Older")
    clock.now += 50
    added = store.add_message(thread.id, "user", "text", "x" * 500)
    assert len(added.text) == 100
    assert store.threads("research")[0].id == thread.id
    assert store.thread(thread.id).updated > store.thread(older.id).updated


def test_old_messages_are_pruned_beyond_the_limit(make, tmp_path):
    store = make(max_messages=10)
    thread = store.create_thread("research", "Chat")
    for number in range(25):
        store.add_message(thread.id, "user", "text", f"m{number}")
    texts = [item.text for item in store.messages(thread.id)]
    assert len(texts) == 10 and texts[-1] == "m24" and texts[0] == "m15"


def test_old_threads_are_pruned_beyond_the_limit(make, tmp_path):
    clock = Clock()
    store = make(clock, max_threads=3)
    for number in range(5):
        clock.now += 1
        store.create_thread("research", f"t{number}")
    assert [item.title for item in store.threads("research")] == ["t4", "t3", "t2"]
    assert len(store.threads("research", limit=1)) == 1


def test_thread_state_round_trips_and_deleting_removes_the_messages(make, tmp_path):
    store = make()
    thread = store.create_thread("research", "Chat")
    store.add_message(thread.id, "user", "text", "hello")
    store.set_state(thread.id, {"phase": "proposed", "proposal": {"kind": "person"}})
    assert store.thread(thread.id).state["proposal"] == {"kind": "person"}
    assert store.delete_thread(thread.id) is True
    assert store.delete_thread(thread.id) is False
    assert store.messages(thread.id) == [] and store.counts()["messages"] == 0


def test_jobs_are_saved_updated_listed_and_deleted(make, tmp_path):
    clock = Clock()
    store = make(clock)
    store.save_job(record("a", state="running", stage="Queued", created=10.0))
    store.save_job(
        record("b", rating=3.6, created=20.0, report={"mode": "person"}, events=[{"seq": 0}])
    )
    store.save_job(record("c", owner="api", created=30.0))
    store.save_job(record("a", state="done", stage="Done", rating=2.0, created=999.0, updated=50.0))
    listed = store.jobs("web")
    assert [item.id for item in listed] == ["b", "a"]
    assert listed[1].state == "done" and listed[1].rating == 2.0 and listed[1].created == 10.0
    assert listed[0].report is None and listed[0].events == []
    full = store.job("b")
    assert full.report == {"mode": "person"} and full.events == [{"seq": 0}]
    assert store.job("missing") is None
    assert store.delete_job("b") is True and store.delete_job("b") is False
    assert [item.id for item in store.jobs("api")] == ["c"]


def test_jobs_keep_their_thread_link_until_the_thread_is_deleted(make, tmp_path):
    store = make()
    thread = store.create_thread("research", "Chat")
    store.save_job(record("a", thread_id=thread.id))
    assert store.job("a").thread_id == thread.id
    store.delete_thread(thread.id)
    assert store.job("a") is not None and store.job("a").thread_id is None


def test_memory_is_saved_replaced_read_and_forgotten(make, tmp_path):
    store = make()
    assert store.memory("profile") is None
    store.remember("profile", {"company": "Acme"})
    store.remember("profile", {"company": "Globex", "requirements": [1, 2]})
    assert store.memory("profile") == {"company": "Globex", "requirements": [1, 2]}
    assert store.forget("profile") is True and store.forget("profile") is False
    assert store.memory("profile") is None


def test_oversized_memory_is_refused(make, tmp_path):
    store = make(memory_chars=200)
    with pytest.raises(ValueError, match="too large"):
        store.remember("profile", {"text": "x" * 500})
    assert store.memory("profile") is None


def test_everything_survives_closing_and_reopening(make, tmp_path):
    first = make()
    thread = first.create_thread("research", "Chat")
    first.add_message(thread.id, "user", "text", "hello")
    first.save_job(record("a", thread_id=thread.id, report={"mode": "person"}))
    first.remember("profile", {"company": "Acme"})
    first.close()
    second = make()
    assert second.thread(thread.id).title == "Chat"
    assert second.messages(thread.id)[0].text == "hello"
    assert second.job("a").report == {"mode": "person"}
    assert second.memory("profile") == {"company": "Acme"}


def test_the_store_connects_on_first_use_and_can_be_closed_and_used_again(make, tmp_path):
    store = make()
    assert not (tmp_path / "data").exists()
    store.close()
    thread = store.create_thread("research", "Chat")
    store.close()
    store.close()
    assert store.thread(thread.id).title == "Chat"


def test_purge_removes_only_what_is_older_than_the_retention(make, tmp_path):
    clock = Clock()
    store = make(clock, retention_days=10)
    old = store.create_thread("research", "Old")
    store.save_job(record("old", updated=clock.now))
    clock.now += 11 * 86_400
    fresh = store.create_thread("research", "Fresh")
    store.save_job(record("fresh", updated=clock.now))
    store.remember("profile", {"company": "Acme"})
    assert store.purge() == {"threads": 1, "jobs": 1}
    assert store.thread(old.id) is None and store.thread(fresh.id) is not None
    assert store.job("old") is None and store.job("fresh") is not None
    assert store.memory("profile") is not None


def test_wipe_deletes_everything_and_reports_what_it_removed(make, tmp_path):
    store = make()
    thread = store.create_thread("research", "Chat")
    store.add_message(thread.id, "user", "text", "hello")
    store.save_job(record("a"))
    store.remember("profile", {"company": "Acme"})
    assert store.wipe() == {"threads": 1, "messages": 1, "jobs": 1, "memory": 1}
    assert store.counts() == {"threads": 0, "messages": 0, "jobs": 0, "memory": 0}


def test_corrupt_json_columns_fall_back_to_empty_values(make, tmp_path):
    store = make()
    thread = store.create_thread("research", "Chat")
    store.add_message(thread.id, "user", "text", "hello")
    with store._lock, store._db:
        store._db.execute("UPDATE threads SET state = 'not json'")
        store._db.execute("UPDATE messages SET data = '{broken'")
        store._db.execute("INSERT INTO memory (key, value, updated) VALUES ('x', '[1]', 1)")
    assert store.thread(thread.id).state == {}
    assert store.messages(thread.id)[0].data == {}
    assert store.memory("x") is None
