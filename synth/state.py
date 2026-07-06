"""Centralized parameter state store."""

from __future__ import annotations

from collections.abc import Callable

from .schema import S1_PARAMS


class ParamState:
    """Holds current values for all CCs with listener-based change notification."""

    def __init__(self) -> None:
        self._values: dict[int, int] = {p.cc: p.default for p in S1_PARAMS}
        self._listeners: list[Callable[[int, int, str], None]] = []

    def add_listener(self, callback: Callable[[int, int, str], None]) -> None:
        """Register a callback: callback(cc, value, source)."""
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[int, int, str], None]) -> None:
        """Unregister a callback."""
        self._listeners.remove(callback)

    def set(self, cc: int, value: int, source: str = "ui") -> None:
        """Set a CC value and notify listeners."""
        value = max(0, min(127, value))
        self._values[cc] = value
        for listener in self._listeners:
            listener(cc, value, source)

    def get(self, cc: int) -> int:
        """Get the current value for a CC."""
        return self._values.get(cc, 0)

    def snapshot(self) -> dict[int, int]:
        """Return a copy of all current values."""
        return dict(self._values)

    def load(self, values: dict[int, int], source: str = "patch") -> None:
        """Bulk-load values, notifying listeners for each."""
        for cc, val in values.items():
            self.set(cc, val, source=source)
