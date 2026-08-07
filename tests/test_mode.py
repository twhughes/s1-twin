"""Tests for the cockpit mode switch (chassis-spec C8).

`logic` mode suppresses the three things Logic owns — MK3 note-forwarding,
the audio monitor, and sequencer clock-out — and `solo` restores them.
Everything else stays live in every mode.
"""

from __future__ import annotations

import sys
import time

import mido
import pytest

from synth.engine import LOGIC, SOLO, S1Engine
from tests.fakes import FakeMidiWorld, FakeSounddevice


@pytest.fixture
def world():
    return FakeMidiWorld()


@pytest.fixture
def engine(world):
    e = S1Engine(midi_module=world, audio_auto=False, poll_interval=0.01)
    yield e
    e.stop()


def plug_s1(world):
    world.add_device(out_name="S-1 MIDI IN", in_name="S-1 MIDI OUT")
    return world.outputs["S-1 MIDI IN"], world.inputs["S-1 MIDI OUT"]


# ── default ───────────────────────────────────────────────────
def test_default_mode_is_solo(engine):
    assert engine.mode == SOLO


def test_set_mode_rejects_unknown(engine):
    with pytest.raises(ValueError):
        engine.set_mode("couch")


def test_set_mode_idempotent_returns_mode(engine):
    assert engine.set_mode(SOLO) == SOLO
    assert engine.set_mode(LOGIC) == LOGIC
    assert engine.set_mode(LOGIC) == LOGIC  # no-op, still logic


def test_mode_in_status(engine):
    assert engine.status()["mode"] == SOLO
    engine.set_mode(LOGIC)
    assert engine.status()["mode"] == LOGIC


def test_mode_change_published(engine):
    events: list[dict] = []
    engine.subscribe(events.append)
    engine.set_mode(LOGIC)
    mode_events = [e for e in events if e["type"] == "mode"]
    assert mode_events and mode_events[-1]["mode"] == LOGIC


# ── keyboard forwarding gate ───────────────────────────────────
class TestForwardingGate:
    def test_callback_registered_in_solo(self, engine, world):
        plug_s1(world)
        world.add_device(in_name="KeyStep 32")
        engine._tick()
        kb = world.inputs["KeyStep 32"]
        # Bound methods are re-created per attribute access, so compare by value.
        assert kb.callback == engine._forward_keyboard

    def test_callback_absent_in_logic(self, engine, world):
        engine.set_mode(LOGIC)
        plug_s1(world)
        world.add_device(in_name="KeyStep 32")
        engine._tick()
        kb = world.inputs["KeyStep 32"]
        assert kb.callback is None

    def test_notes_forward_in_solo(self, engine, world):
        out, _ = plug_s1(world)
        world.add_device(in_name="KeyStep 32")
        engine._tick()
        world.inputs["KeyStep 32"].callback(
            mido.Message("note_on", channel=0, note=60, velocity=90))
        assert [m.type for m in out.sent if m.type == "note_on"] == ["note_on"]

    def test_notes_suppressed_in_logic(self, engine, world):
        out, _ = plug_s1(world)
        world.add_device(in_name="KeyStep 32")
        engine.set_mode(LOGIC)
        engine._tick()
        kb = world.inputs["KeyStep 32"]
        # Even a direct callback invocation forwards nothing (entry gate).
        engine._forward_keyboard(mido.Message("note_on", channel=0, note=60, velocity=90))
        # And messages arriving via iter_pending drain into nothing.
        kb.pending.append(mido.Message("note_on", channel=0, note=64, velocity=90))
        engine._tick()
        assert [m for m in out.sent if m.type in ("note_on", "note_off")] == []

    def test_switch_to_logic_disables_live_forwarding(self, engine, world):
        """A keyboard opened in solo stops forwarding the instant we go logic."""
        out, _ = plug_s1(world)
        world.add_device(in_name="KeyStep 32")
        engine._tick()
        engine.set_mode(LOGIC)
        # The port opened in solo held a callback; the entry gate blocks it.
        engine._forward_keyboard(mido.Message("note_on", channel=0, note=60, velocity=90))
        assert [m for m in out.sent if m.type == "note_on"] == []

    def test_switch_back_to_solo_restores_forwarding(self, engine, world):
        out, _ = plug_s1(world)
        world.add_device(in_name="KeyStep 32")
        engine.set_mode(LOGIC)
        engine._tick()
        engine.set_mode(SOLO)  # re-opens keyboards with the callback bound
        kb = world.inputs["KeyStep 32"]
        assert kb.callback == engine._forward_keyboard
        kb.callback(mido.Message("note_on", channel=0, note=62, velocity=90))
        assert [m.type for m in out.sent if m.type == "note_on"] == ["note_on"]

    def test_cc_sync_still_works_in_logic(self, engine, world):
        """CC sync (both directions) is never gated by mode — only notes are."""
        out, s1_in = plug_s1(world)
        engine._tick()
        engine.set_mode(LOGIC)
        # UI/agent → device still sends.
        engine.set_param(74, 55, source="ui")
        assert any(m.control == 74 and m.value == 55
                   for m in out.sent if m.type == "control_change")
        # Device → app still adopts the knob twist.
        s1_in.pending.append(mido.Message("control_change", channel=2, control=71, value=42))
        engine._tick()
        assert engine.params.get(71) == 42


# ── clock-out gate ─────────────────────────────────────────────
class TestClockGate:
    def test_clock_suppressed_in_logic(self, engine, world):
        out, _ = plug_s1(world)
        engine._tick()
        engine.set_mode(LOGIC)
        assert engine.sequencer.clock_enabled is False
        seq = engine.sequencer
        seq._next_clock = time.monotonic() - 1.0  # a tick is due
        seq._flush_clock(time.monotonic())
        assert [m for m in out.sent if m.type == "clock"] == []

    def test_clock_restored_in_solo(self, engine, world):
        out, _ = plug_s1(world)
        engine._tick()
        engine.set_mode(LOGIC)
        engine.set_mode(SOLO)
        assert engine.sequencer.clock_enabled is True
        seq = engine.sequencer
        seq._next_clock = time.monotonic() - 1.0
        seq._flush_clock(time.monotonic())
        assert [m for m in out.sent if m.type == "clock"]

    def test_user_clock_preference_preserved(self, engine, world):
        """A user who turned clock-out off keeps it off after a logic round-trip."""
        plug_s1(world)
        engine._tick()
        engine.sequencer.clock_enabled = False  # user's choice in solo
        engine.set_mode(LOGIC)
        engine.set_mode(SOLO)
        assert engine.sequencer.clock_enabled is False

    def test_clock_emits_in_solo_none_in_logic(self, engine, world):
        """Direct clock-out contract (C4): the sequencer emits clock in solo and
        emits none in logic — the single-master rule, checked at the seam."""
        out, _ = plug_s1(world)
        engine._tick()
        seq = engine.sequencer

        # solo (default): a due tick sounds.
        assert engine.mode == SOLO
        seq._next_clock = time.monotonic() - 1.0
        seq._flush_clock(time.monotonic())
        assert [m for m in out.sent if m.type == "clock"]

        # logic: Logic masters clock, so none may go out.
        out.sent.clear()
        engine.set_mode(LOGIC)
        seq._next_clock = time.monotonic() - 1.0
        seq._flush_clock(time.monotonic())
        assert [m for m in out.sent if m.type == "clock"] == []


# ── monitor gate ───────────────────────────────────────────────
class TestMonitorGate:
    @pytest.fixture
    def fake_sd(self, monkeypatch):
        fake = FakeSounddevice()
        monkeypatch.setitem(sys.modules, "sounddevice", fake)
        monkeypatch.setattr("synth.audio.PREFILL_SECONDS", 0.0)
        return fake

    @pytest.fixture
    def audio_engine(self, world, fake_sd):
        e = S1Engine(midi_module=world, audio_auto=True, poll_interval=0.01)
        yield e
        e.stop()

    def test_switch_to_logic_stops_monitor(self, audio_engine, fake_sd):
        fake_sd.add_s1()
        audio_engine._tick()
        assert audio_engine.monitor.running
        audio_engine.set_mode(LOGIC)
        assert not audio_engine.monitor.running

    def test_monitor_not_restarted_in_logic(self, audio_engine, fake_sd):
        audio_engine.set_mode(LOGIC)
        fake_sd.add_s1()
        audio_engine._tick()  # would auto-start in solo
        assert not audio_engine.monitor.running

    def test_switch_back_to_solo_restarts_monitor(self, audio_engine, fake_sd):
        fake_sd.add_s1()
        audio_engine._tick()
        audio_engine.set_mode(LOGIC)
        assert not audio_engine.monitor.running
        audio_engine.set_mode(SOLO)  # _apply_mode re-scans audio
        assert audio_engine.monitor.running

    def test_switch_to_logic_suppresses_all_three_atomically(self, audio_engine,
                                                              fake_sd, world):
        """One set_mode(LOGIC) call must, together, stop the monitor, drop MK3
        forwarding, and disable clock-out — the three things Logic owns (C8)."""
        fake_sd.add_s1()
        plug_s1(world)
        world.add_device(in_name="KeyStep 32")
        audio_engine._tick()
        # In solo all three are live.
        assert audio_engine.monitor.running
        assert world.inputs["KeyStep 32"].callback == audio_engine._forward_keyboard
        assert audio_engine.sequencer.clock_enabled is True

        audio_engine.set_mode(LOGIC)

        # After the single switch, all three are suppressed together.
        assert not audio_engine.monitor.running
        assert world.inputs["KeyStep 32"].callback is None
        assert audio_engine.sequencer.clock_enabled is False
