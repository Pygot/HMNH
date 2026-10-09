# src/agent/events.py
from pydantic import (
    BaseModel,
    Field,
)
from collections.abc import Callable
from enum import StrEnum
from typing import Any

Sink = Callable[["Kind", "Task | None", str, dict[str, Any]], None]
StageCallback = Callable[[str], None]


class Task(StrEnum):
    """A named stage of the research pipeline that progress events refer to."""

    PARSE = "parse"
    COMPANY = "company"
    SEARCH = "search"
    LINKEDIN = "linkedin"
    FACEBOOK = "facebook"
    INSTAGRAM = "instagram"
    WEB = "web"
    SUMMARISE = "summarise"
    MERGE = "merge"
    REQUIREMENTS = "requirements"
    RATE = "rate"
    SCEPTICISM = "scepticism"
    EXPORT = "export"
    WAIT = "wait"


class Kind(StrEnum):
    """The type of a progress event, such as a stage change, a finding or a failure."""

    STAGE = "stage"
    STEP = "step"
    NOTE = "note"
    COMPANY = "company"
    CANDIDATE = "candidate"
    SOURCE = "source"
    CLAIM = "claim"
    FINDING = "finding"
    REQUIREMENT = "requirement"
    RATING = "rating"
    SCEPTICISM = "scepticism"
    QUESTION = "question"
    DONE = "done"
    FAILED = "failed"


class Event(BaseModel):
    """One numbered progress event emitted while a job runs."""

    seq: int
    kind: Kind
    task: Task | None = None
    text: str
    data: dict[str, Any] = Field(default_factory=dict)


class Reporter:
    """A callable that forwards progress events to an optional sink.

    Without a sink every event is silently dropped.
    """

    def __init__(self, sink: Sink | None = None):
        """Initialize the reporter.

        Args:
            sink: function receiving the kind, task, text and extra data of each event, or
                None to discard events.
        """
        self._sink = sink

    def __call__(self, stage: str, task: Task | None = None) -> None:
        """Report a change of stage.

        Args:
            stage: the text describing the new stage.
            task: the pipeline task the stage belongs to, if any.
        """
        self.emit(Kind.STAGE, stage, task)

    def emit(self, kind: Kind, text: str, task: Task | None = None, **data: Any) -> None:
        """Send an event to the sink.

        Args:
            kind: the type of the event.
            text: the human readable message.
            task: the pipeline task the event belongs to, if any.
            **data: extra structured data passed to the sink.
        """
        if self._sink is not None:
            self._sink(kind, task, text, data)


class Notes(list[str]):
    """A list of note strings that also reports each added note as an event."""

    def __init__(self, reporter: Reporter):
        """Initialize an empty list of notes.

        Args:
            reporter: the reporter that receives a note event for every added note.
        """
        super().__init__()
        self._reporter = reporter

    def append(self, text: str) -> None:
        """Add a note and report it.

        Args:
            text: the note to add.
        """
        super().append(text)
        self._reporter.emit(Kind.NOTE, text)

    def extend(self, texts: Any) -> None:
        """Add several notes, reporting each one.

        Args:
            texts: an iterable of note strings.
        """
        for text in texts:
            self.append(text)


def as_reporter(progress: Reporter | StageCallback | None) -> Reporter:
    """Return a reporter for any supported progress argument.

    Args:
        progress: a reporter, a plain function taking stage text, or None.

    Returns:
        The same reporter if one was given, a reporter that relays only stage events
        to the function, or a reporter that discards events for None.
    """
    if isinstance(progress, Reporter):
        return progress
    if progress is None:
        return Reporter()

    def relay(kind: Kind, _task: Task | None, text: str, _data: dict[str, Any]) -> None:
        """Pass stage events to the plain progress function and ignore the rest.

        Args:
            kind: the type of the event.
            _task: the pipeline task, unused.
            text: the event message.
            _data: extra event data, unused.
        """
        if kind is Kind.STAGE:
            progress(text)

    return Reporter(relay)
