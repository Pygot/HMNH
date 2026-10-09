# tests/test_events.py
from agent.events import (
    as_reporter,
    Event,
    Kind,
    Notes,
    Reporter,
    Task,
)

import pytest


class Recorder:
    def __init__(self):
        self.items = []

    def __call__(self, kind, task, text, data):
        self.items.append((kind, task, text, data))


def test_a_reporter_without_a_sink_is_silent():
    reporter = Reporter()
    reporter("Searching", Task.SEARCH)
    reporter.emit(Kind.NOTE, "ignored", None, anything=1)


def test_calling_a_reporter_emits_a_stage_with_its_task():
    recorder = Recorder()
    reporter = Reporter(recorder)
    reporter("Searching", Task.SEARCH)
    reporter("Plain")
    assert recorder.items == [
        (Kind.STAGE, Task.SEARCH, "Searching", {}),
        (Kind.STAGE, None, "Plain", {}),
    ]


def test_emit_passes_the_extra_data_through():
    recorder = Recorder()
    Reporter(recorder).emit(Kind.FINDING, "A fact", Task.MERGE, id="F1", urls=["https://a.example"])
    assert recorder.items == [
        (Kind.FINDING, Task.MERGE, "A fact", {"id": "F1", "urls": ["https://a.example"]})
    ]


def test_the_text_keyword_is_reserved_for_the_event_text():
    with pytest.raises(TypeError):
        Reporter(Recorder()).emit(Kind.NOTE, "note", None, text="clash")


def test_notes_behave_like_a_list_and_report_each_entry():
    recorder = Recorder()
    notes = Notes(Reporter(recorder))
    notes.append("first")
    notes.extend(["second", "third"])
    assert notes == ["first", "second", "third"]
    assert [item[2] for item in recorder.items] == ["first", "second", "third"]
    assert {item[0] for item in recorder.items} == {Kind.NOTE}
    assert all(item[1] is None for item in recorder.items)


def test_notes_without_a_listener_still_collect():
    notes = Notes(Reporter())
    notes.extend(iter(["a", "b"]))
    assert list(notes) == ["a", "b"]


def test_a_reporter_passes_through_unchanged():
    reporter = Reporter()
    assert as_reporter(reporter) is reporter


def test_no_callback_becomes_a_silent_reporter():
    reporter = as_reporter(None)
    assert isinstance(reporter, Reporter)
    reporter("quiet", Task.WEB)
    reporter.emit(Kind.NOTE, "quiet")


def test_a_legacy_callback_only_sees_the_stage_text():
    seen = []
    reporter = as_reporter(seen.append)
    reporter("Stage one", Task.PARSE)
    reporter.emit(Kind.NOTE, "not a stage")
    reporter.emit(Kind.FINDING, "also not", Task.MERGE, id="F1")
    reporter("Stage two")
    assert seen == ["Stage one", "Stage two"]


def test_events_have_defaults_and_validate_their_kind():
    event = Event(seq=0, kind=Kind.NOTE, text="hello")
    assert event.task is None and event.data == {}
    assert Event.model_validate_json(event.model_dump_json()) == event
    with pytest.raises(ValueError):
        Event(seq=1, kind="nonsense", text="x")


def test_event_data_defaults_are_not_shared():
    first = Event(seq=0, kind=Kind.NOTE, text="a")
    second = Event(seq=1, kind=Kind.NOTE, text="b")
    first.data["key"] = "value"
    assert second.data == {}


def test_every_task_and_kind_has_a_unique_lowercase_value():
    for enum in (Task, Kind):
        values = [member.value for member in enum]
        assert len(values) == len(set(values))
        assert all(value == value.lower() and value.isalpha() for value in values)


def test_terminal_kinds_exist_for_the_stream_protocol():
    assert {Kind.DONE, Kind.FAILED, Kind.QUESTION} <= set(Kind)
    assert Task.WAIT in Task and Task.EXPORT in Task
