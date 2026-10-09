# src/agent/forms.py
from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    """An input of a settings form, with its label, hint and optional choices.

    A secret field holds a value such as a key or token that should not be shown.
    """

    key: str
    label: str
    hint: str = ""
    secret: bool = False
    choices: tuple[tuple[str, str], ...] = ()
    placeholder: str = ""


@dataclass(frozen=True)
class Section:
    """A titled group of settings fields with an introduction."""

    name: str
    title: str
    intro: str
    fields: tuple[Field, ...]


@dataclass(frozen=True)
class Topic:
    """A dashboard topic with the permission and optional feature switch it needs.

    The window names the kind of time range the topic accepts, such as days or
    months, or is None for topics without one.
    """

    name: str
    label: str
    permission: str
    module: str | None
    window: str | None


@dataclass(frozen=True)
class Dataset:
    """A set of records that can be exported and optionally imported.

    The view permission is needed to export it. The edit permission is needed to
    import it, and is None for datasets that are export only.
    """

    name: str
    label: str
    view: str
    edit: str | None
    module: str | None
    csv: bool
