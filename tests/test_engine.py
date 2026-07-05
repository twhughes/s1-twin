"""Tests for s1tui.engine — two-way sync, hot-plug, keyboard forwarding."""

from __future__ import annotations

import sys

import mido
import pytest

from s1tui.engine import CONNECTING, DISCONNECTED, SYNCED, S1Engine
from s1tui.schema import S1_PARAMS
from tests.fakes import FakeMidiWorld, FakeSounddevice


@pytest.fixture
def world():
    return FakeMidiWorld()


@pytest.fixture
def engine(world):
    e = S1Engine(midi_module=world, audio_auto=False, poll_interval=0.01)
    yield e
    e.stop()


@pytest.fixture
def events(engine):
    captured: list[dict] = []
    engine.subscribe(captured.append)
    return captured


def plug_s1(world):
    world.add_device(out_name="S-1 MIDI IN", in_name="S-1 MIDI OUT")
    return world.outputs["S-1 MIDI IN"], world.inputs["S-1 MIDI OUT"]


# ── G4: connect → push-all ───────────────────────────────────
class TestAutoConnect:
    def test_starts_disconnected(self, engine):
        assert engine.sync_state == DISCONNECTED

    def test_connects_and_pushes_all_when_s1_appears(self, engine, world, events):
        out, _ = plug_s1(world)
        engine._tick()
        assert engine.sync_state == SYNCED
        ccs_sent = [m for m in out.sent if m.type == "control_change"]
        assert {m.control for m in ccs_sent} == {p.cc for p in S1_PARAMS}
        # Push respects app state (schema defaults at startup).
        cutoff = next(m for m in ccs_sent if m.control == 74)
        assert cutoff.value == 127

    def test_sync_events_published(self, engine, world, events):
        plug_s1(world)
        engine._tick()
        states = [e["state"] for e in events if e["type"] == "sync"]
        assert states == [CONNECTING, SYNCED]

    def test_no_s1_no_connect(self, engine, world):
        world.add_device(out_name="KeyStep 32", in_name="KeyStep 32")
        engine._tick()
        assert engine.sync_state == DISCONNECTED
        assert not engine.midi.connected

    def test_push_uses_synth_channel_3(self, engine, world):
        out, _ = plug_s1(world)
        engine._tick()
        assert all(m.channel == 2 for m in out.sent if m.type == "control_change")


# ── G4: CC in → state → broadcast ────────────────────────────
class TestIncomingCC:
    def test_knob_twist_updates_state_and_broadcasts(self, engine, world, events):
        _, s1_in = plug_s1(world)
        engine._tick()
        events.clear()
        s1_in.pending.append(
            mido.Message("control_change", channel=2, control=74, value=42)
        )
        engine._tick()
        assert engine.params.get(74) == 42
        param_events = [e for e in events if e["type"] == "param"]
        assert param_events == [{"type": "param", "cc": 74, "value": 42, "source": "midi"}]

    def test_knob_twist_not_echoed_back(self, engine, world):
        out, s1_in = plug_s1(world)
        engine._tick()
        sent_before = len(out.sent)
        s1_in.pending.append(
            mido.Message("control_change", channel=2, control=71, value=99)
        )
        engine._tick()
        assert len(out.sent) == sent_before  # no CC echo to the device

    def test_unknown_cc_ignored(self, engine, world):
        _, s1_in = plug_s1(world)
        engine._tick()
        s1_in.pending.append(
            mido.Message("control_change", channel=2, control=9, value=1)
        )
        engine._tick()
        assert engine.params.get(9) == 0  # untouched default


# ── G4: unplug / replug ──────────────────────────────────────
class TestReconnect:
    def test_unplug_marks_disconnected(self, engine, world, events):
        plug_s1(world)
        engine._tick()
        world.remove_device("S-1 MIDI IN", "S-1 MIDI OUT")
        engine._tick()
        assert engine.sync_state == DISCONNECTED
        assert not engine.midi.connected

    def test_replug_reconnects_and_repushes(self, engine, world):
        out, _ = plug_s1(world)
        engine._tick()
        world.remove_device("S-1 MIDI IN", "S-1 MIDI OUT")
        engine._tick()

        out2, _ = plug_s1(world)
        engine._tick()
        assert engine.sync_state == SYNCED
        assert {m.control for m in out2.sent if m.type == "control_change"} == {
            p.cc for p in S1_PARAMS
        }

    def test_app_keeps_working_disconnected(self, engine, world):
        plug_s1(world)
        engine._tick()
        world.remove_device("S-1 MIDI IN", "S-1 MIDI OUT")
        engine._tick()
        engine.set_param(74, 10)  # must not raise
        assert engine.params.get(74) == 10

    def test_state_survives_reconnect(self, engine, world):
        plug_s1(world)
        engine._tick()
        engine.set_param(74, 33)
        world.remove_device("S-1 MIDI IN", "S-1 MIDI OUT")
        engine._tick()
        out2, _ = plug_s1(world)
        engine._tick()
        cutoff = [m for m in out2.sent if m.type == "control_change" and m.control == 74]
        assert cutoff[-1].value == 33  # app state is truth on reconnect


# ── param setting (UI / agent side) ──────────────────────────
class TestSetParam:
    def test_set_sends_cc_and_broadcasts(self, engine, world, events):
        out, _ = plug_s1(world)
        engine._tick()
        events.clear()
        engine.set_param(71, 88, source="ui")
        cc = [m for m in out.sent if m.type == "control_change" and m.control == 71]
        assert cc[-1].value == 88
        assert {"type": "param", "cc": 71, "value": 88, "source": "ui"} in events

    def test_set_clamps_to_schema_range(self, engine):
        assert engine.set_param(12, 99) == 5  # LFO waveform: 6 options max
        assert engine.set_param(102, 0) == 3  # draw multiply floor CC 3

    def test_unknown_cc_raises(self, engine):
        with pytest.raises(KeyError):
            engine.set_param(9, 1)

    def test_load_values_bulk(self, engine, world, events):
        out, _ = plug_s1(world)
        engine._tick()
        engine.load_values({74: 5, 71: 100}, source="patch")
        assert engine.params.get(74) == 5
        assert engine.params.get(71) == 100


# ── G6: external MIDI keyboards ──────────────────────────────
class TestKeyboardForwarding:
    def test_external_keyboard_detected(self, engine, world, events):
        plug_s1(world)
        world.add_device(in_name="KeyStep 32")
        engine._tick()
        assert engine.status()["keyboards"] == ["KeyStep 32"]
        kb_events = [e for e in events if e["type"] == "keyboards"]
        assert kb_events and kb_events[-1]["names"] == ["KeyStep 32"]

    def test_s1_own_input_not_a_keyboard(self, engine, world):
        plug_s1(world)
        engine._tick()
        assert engine.status()["keyboards"] == []

    def test_notes_forwarded_to_s1(self, engine, world):
        out, _ = plug_s1(world)
        world.add_device(in_name="KeyStep 32")
        engine._tick()
        kb = world.inputs["KeyStep 32"]
        kb.pending.append(mido.Message("note_on", channel=0, note=60, velocity=90))
        kb.pending.append(mido.Message("note_off", channel=0, note=60))
        engine._tick()
        notes = [m for m in out.sent if m.type in ("note_on", "note_off")]
        assert [(m.type, m.note) for m in notes] == [("note_on", 60), ("note_off", 60)]
        assert notes[0].velocity == 90
        assert all(m.channel == 2 for m in notes)  # re-addressed to the S-1 channel

    def test_velocity_zero_note_on_is_note_off(self, engine, world):
        out, _ = plug_s1(world)
        world.add_device(in_name="KeyStep 32")
        engine._tick()
        kb = world.inputs["KeyStep 32"]
        kb.pending.append(mido.Message("note_on", channel=0, note=64, velocity=0))
        engine._tick()
        assert [m.type for m in out.sent if m.type in ("note_on", "note_off")] == ["note_off"]

    def test_keyboard_hot_unplug(self, engine, world, events):
        plug_s1(world)
        world.add_device(in_name="KeyStep 32")
        engine._tick()
        world.remove_device("KeyStep 32")
        engine._tick()
        assert engine.status()["keyboards"] == []

    def test_keyboard_works_without_s1(self, engine, world):
        # Notes drop silently while disconnected — no crash.
        world.add_device(in_name="KeyStep 32")
        engine._tick()
        kb = world.inputs["KeyStep 32"]
        kb.pending.append(mido.Message("note_on", channel=0, note=60, velocity=90))
        engine._tick()


# ── QWERTY / API notes ───────────────────────────────────────
class TestApiNotes:
    def test_note_on_off(self, engine, world):
        out, _ = plug_s1(world)
        engine._tick()
        engine.note_on(72, 101)
        engine.note_off(72)
        notes = [m for m in out.sent if m.type in ("note_on", "note_off")]
        assert [(m.type, m.note) for m in notes] == [("note_on", 72), ("note_off", 72)]
        assert notes[0].velocity == 101


# ── G7: pattern select (Program Change) ──────────────────────
class TestPatternSelect:
    def test_bank_slot_to_program(self, engine, world, events):
        out, _ = plug_s1(world)
        engine._tick()
        assert engine.select_pattern(1, 1) == 0
        assert engine.select_pattern(4, 16) == 63
        assert engine.select_pattern(2, 3) == 18
        pcs = [m for m in out.sent if m.type == "program_change"]
        assert [m.program for m in pcs] == [0, 63, 18]

    def test_pc_goes_out_on_pc_channel_16(self, engine, world):
        out, _ = plug_s1(world)
        engine._tick()
        engine.select_pattern(1, 2)
        pc = [m for m in out.sent if m.type == "program_change"][-1]
        assert pc.channel == 15

    def test_invalid_bank_slot(self, engine):
        with pytest.raises(ValueError):
            engine.select_pattern(0, 1)
        with pytest.raises(ValueError):
            engine.select_pattern(5, 1)
        with pytest.raises(ValueError):
            engine.select_pattern(1, 0)
        with pytest.raises(ValueError):
            engine.select_pattern(1, 17)


# ── G5: audio auto-start ─────────────────────────────────────
class TestAudioAutoStart:
    @pytest.fixture
    def fake_sd(self, monkeypatch):
        fake = FakeSounddevice()
        monkeypatch.setitem(sys.modules, "sounddevice", fake)
        monkeypatch.setattr("s1tui.audio.PREFILL_SECONDS", 0.0)
        return fake

    @pytest.fixture
    def audio_engine(self, world, fake_sd):
        e = S1Engine(midi_module=world, audio_auto=True, poll_interval=0.01)
        yield e
        e.stop()

    def test_monitor_autostarts_when_s1_audio_appears(self, audio_engine, fake_sd):
        fake_sd.add_s1()
        audio_engine._tick()
        assert audio_engine.monitor.running
        assert audio_engine.monitor.input == 2   # the S-1 device index
        assert audio_engine.monitor.output == 1  # system default output

    def test_no_s1_audio_no_monitor(self, audio_engine, fake_sd):
        audio_engine._tick()
        assert not audio_engine.monitor.running

    def test_monitor_stops_when_stream_dies(self, audio_engine, fake_sd):
        fake_sd.add_s1()
        audio_engine._tick()
        fake_sd.input_streams[-1].active = False  # unplug
        fake_sd.remove_s1()
        audio_engine._tick()
        assert not audio_engine.monitor.running

    def test_monitor_restarts_on_replug(self, audio_engine, fake_sd):
        fake_sd.add_s1()
        audio_engine._tick()
        fake_sd.input_streams[-1].active = False
        fake_sd.remove_s1()
        audio_engine._tick()
        fake_sd.add_s1()
        audio_engine._tick()
        assert audio_engine.monitor.running

    def test_monitor_events_published(self, audio_engine, fake_sd):
        seen: list[dict] = []
        audio_engine.subscribe(seen.append)
        fake_sd.add_s1()
        audio_engine._tick()
        mon = [e for e in seen if e["type"] == "monitor"]
        assert mon and mon[-1]["running"] is True


# ── watcher thread ───────────────────────────────────────────
class TestWatcherThread:
    def test_start_stop(self, world):
        import time

        e = S1Engine(midi_module=world, audio_auto=False, poll_interval=0.01)
        e.start()
        plug_s1(world)
        deadline = time.monotonic() + 2.0
        while e.sync_state != SYNCED and time.monotonic() < deadline:
            time.sleep(0.01)
        assert e.sync_state == SYNCED
        e.stop()
        assert e._thread is None

    def test_start_idempotent(self, world):
        e = S1Engine(midi_module=world, audio_auto=False, poll_interval=0.01)
        e.start()
        t = e._thread
        e.start()
        assert e._thread is t
        e.stop()
