"""Tests for state.py — centralized parameter state store."""

from synth.schema import S1_PARAMS
from synth.state import ParamState


class TestParamState:
    def test_initial_values_are_defaults(self):
        state = ParamState()
        for p in S1_PARAMS:
            assert state.get(p.cc) == p.default, (
                f"{p.name} (CC {p.cc}) expected {p.default}, got {state.get(p.cc)}"
            )

    def test_set_and_get(self):
        state = ParamState()
        state.set(74, 100)
        assert state.get(74) == 100

    def test_set_clamps_high(self):
        state = ParamState()
        state.set(74, 200)
        assert state.get(74) == 127

    def test_set_clamps_low(self):
        state = ParamState()
        state.set(74, -10)
        assert state.get(74) == 0

    def test_get_unknown_cc(self):
        state = ParamState()
        assert state.get(999) == 0

    def test_snapshot_returns_copy(self):
        state = ParamState()
        snap = state.snapshot()
        assert isinstance(snap, dict)
        assert len(snap) == 54
        # Modifying snapshot doesn't affect state
        snap[74] = 999
        assert state.get(74) != 999

    def test_listener_notified(self):
        state = ParamState()
        changes = []
        state.add_listener(lambda cc, val, src: changes.append((cc, val, src)))
        state.set(74, 100, "ui")
        assert len(changes) == 1
        assert changes[0] == (74, 100, "ui")

    def test_multiple_listeners(self):
        state = ParamState()
        a, b = [], []
        state.add_listener(lambda cc, val, src: a.append(cc))
        state.add_listener(lambda cc, val, src: b.append(cc))
        state.set(74, 50)
        assert len(a) == 1
        assert len(b) == 1

    def test_remove_listener(self):
        state = ParamState()
        changes = []
        def cb(cc, val, src):
            changes.append(cc)

        state.add_listener(cb)
        state.set(74, 50)
        assert len(changes) == 1
        state.remove_listener(cb)
        state.set(74, 60)
        assert len(changes) == 1  # No new notification

    def test_load_bulk(self):
        state = ParamState()
        values = {74: 100, 73: 50, 71: 80}
        changes = []
        state.add_listener(lambda cc, val, src: changes.append((cc, val, src)))
        state.load(values)
        assert state.get(74) == 100
        assert state.get(73) == 50
        assert state.get(71) == 80
        assert len(changes) == 3
        assert all(src == "patch" for _, _, src in changes)

    def test_source_passed_through(self):
        state = ParamState()
        sources = []
        state.add_listener(lambda cc, val, src: sources.append(src))
        state.set(74, 50, "ui")
        state.set(74, 60, "midi")
        state.set(74, 70, "patch")
        assert sources == ["ui", "midi", "patch"]

    def test_listener_may_unregister_during_notify(self):
        """A listener that removes another listener mid-notify must not raise —
        set() iterates a snapshot taken under the lock, not the live list."""
        state = ParamState()
        calls = []

        def second(cc, val, src):
            calls.append("second")

        def first(cc, val, src):
            calls.append("first")
            if second in state._listeners:  # mutate during iteration, once
                state.remove_listener(second)

        state.add_listener(first)
        state.add_listener(second)
        state.set(74, 50)  # must not raise; snapshot keeps this pass stable
        # second was still in the snapshot for this set, so both fired once.
        assert calls == ["first", "second"]
        calls.clear()
        state.set(74, 60)  # second now gone
        assert calls == ["first"]

    def test_concurrent_add_listener_during_set(self):
        """Registering a listener from another thread while set() fires must not
        corrupt the list or raise (lock-guarded registration + iteration)."""
        import threading

        state = ParamState()
        stop = threading.Event()

        def noop(cc, val, src):
            pass

        def adder():
            while not stop.is_set():
                state.add_listener(noop)
                state.remove_listener(noop)

        state.add_listener(lambda cc, val, src: None)
        t = threading.Thread(target=adder, daemon=True)
        t.start()
        try:
            for i in range(2000):
                state.set(74, i % 128)  # must never raise mid-iteration
        finally:
            stop.set()
            t.join(timeout=2.0)
