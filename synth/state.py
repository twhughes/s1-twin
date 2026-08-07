"""Centralized parameter state store."""

from __future__ import annotations

import threading
from collections.abc import Callable

from .schema import S1_PARAMS

# One listener: callback(cc, value, source).
ParamListener = Callable[[int, int, str], None]


class ParamState:
    """Holds current values for all CCs with listener-based change notification."""

    def __init__(self) -> None:
        self._values: dict[int, int] = {p.cc: p.default for p in S1_PARAMS}
        self._listeners: list[ParamListener] = []
        # Guards listener registration and iteration. set() may run on the MIDI
        # watcher thread while the web/UI thread (un)registers a listener, so we
        # snapshot the list under the lock and fire callbacks outside it — no
        # mutation-during-iteration, no callback running under the lock.
        self._listeners_lock = threading.Lock()

    def add_listener(self, callback: ParamListener) -> None:
        """Register a callback: callback(cc, value, source)."""
        with self._listeners_lock:
            self._listeners.append(callback)

    def remove_listener(self, callback: ParamListener) -> None:
        """Unregister a callback."""
        with self._listeners_lock:
            self._listeners.remove(callback)

    def set(self, cc: int, value: int, source: str = "ui") -> None:
        """Set a CC value and notify listeners."""
        value = max(0, min(127, value))
        self._values[cc] = value
        with self._listeners_lock:
            listeners = list(self._listeners)
        for listener in listeners:
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
