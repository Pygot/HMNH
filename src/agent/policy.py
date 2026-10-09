# src/agent/policy.py
from pydantic import (
    ConfigDict,
    ValidationError,
)
from agent.config import PolicyTuning
from agent.store import Store

KEY = "policy"


class Policy(PolicyTuning):
    """The workspace policy switches, as defaults overlaid with stored changes.

    Unlike the tuning class it extends, instances may be changed, and stored keys
    that are no longer known are ignored.
    """

    model_config = ConfigDict(frozen=False, extra="ignore")


class PolicyBook:
    """A store backed holder of the current workspace policy.

    The policy is loaded lazily and cached, and every update is saved.
    """

    def __init__(self, store: Store, defaults: PolicyTuning):
        """Initialize the policy book.

        Args:
            store: the store that keeps changed policy values.
            defaults: the policy used for values that were never changed.
        """
        self._store = store
        self._defaults = defaults
        self._current: Policy | None = None

    def get(self) -> Policy:
        """Return the current policy, loading it on first use.

        Stored values override the defaults. Stored data that fails validation is
        ignored in favour of the defaults.

        Returns:
            The current policy.
        """
        if self._current is None:
            base = self._defaults.model_dump()
            stored = self._store.memory(KEY) or {}
            try:
                self._current = Policy.model_validate({**base, **stored})
            except ValidationError:
                self._current = Policy.model_validate(base)
        return self._current

    def update(self, **changes: bool) -> Policy:
        """Change policy switches and save the result.

        Args:
            **changes: the switches to change, by name, with their new boolean values.

        Returns:
            The updated policy.

        Raises:
            ValidationError: when a change does not fit the policy fields.
        """
        merged = {**self.get().model_dump(), **changes}
        updated = Policy.model_validate(merged)
        self._store.remember(KEY, updated.model_dump(mode="json"))
        self._current = updated
        return updated
