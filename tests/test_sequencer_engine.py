"""Tests for the sequencer engine."""

import time
from unittest.mock import MagicMock

import pytest

from s1tui.sequence import Note, Sequence
from s1tui.sequencer_engine import SequencerEngine


@pytest.fixture
def mock_midi():
    midi = MagicMock()
    midi.connected = True
    return midi


@pytest.fixture
def simple_sequence():
    return Sequence(
        notes=[
            Note(step=0, pitch=60, velocity=100, duration=1),
            Note(step=2, pitch=64, velocity=80, duration=1),
        ],
        steps=4,
        bpm=600.0,  # Very fast for testing (10 steps/sec at 1/16)
        step_resolution="1/16",
    )


def test_engine_init(mock_midi):
    engine = SequencerEngine(mock_midi)
    assert not engine.playing
    assert engine.position == 0


def test_engine_play_stop(mock_midi, simple_sequence):
    engine = SequencerEngine(mock_midi, simple_sequence)
    engine.play()
    assert engine.playing
    time.sleep(0.1)
    engine.stop()
    assert not engine.playing
    assert engine.position == 0
    mock_midi.send_start.assert_called_once()
    mock_midi.send_stop.assert_called()


def test_engine_sends_notes(mock_midi, simple_sequence):
    engine = SequencerEngine(mock_midi, simple_sequence)
    engine.play()
    # Let it play through at least one full cycle
    time.sleep(0.3)
    engine.stop()
    # Should have sent note_on for pitch 60 and 64
    note_on_calls = mock_midi.send_note_on.call_args_list
    pitches_played = {c[0][0] for c in note_on_calls}
    assert 60 in pitches_played
    assert 64 in pitches_played


def test_engine_pause_resume(mock_midi, simple_sequence):
    engine = SequencerEngine(mock_midi, simple_sequence)
    engine.play()
    time.sleep(0.05)
    engine.pause()
    assert engine.paused
    assert not engine.playing
    # Wait for pause to settle, then check position is stable
    time.sleep(0.05)
    pos = engine.position
    time.sleep(0.1)
    # Position should not advance while paused
    assert engine.position == pos
    # Resume
    engine.play()
    assert engine.playing
    assert not engine.paused
    mock_midi.send_continue.assert_called_once()
    time.sleep(0.05)
    engine.stop()


def test_engine_empty_sequence(mock_midi):
    engine = SequencerEngine(mock_midi, Sequence())
    engine.play()
    time.sleep(0.05)
    # Should stop immediately with empty sequence
    assert not engine.playing


def test_engine_sequence_swap(mock_midi, simple_sequence):
    engine = SequencerEngine(mock_midi, simple_sequence)
    new_seq = Sequence(notes=[Note(step=0, pitch=72)], steps=8, bpm=120.0)
    engine.sequence = new_seq
    assert engine.sequence is new_seq
    assert engine.position == 0


def test_engine_position_callback(mock_midi, simple_sequence):
    positions = []
    engine = SequencerEngine(mock_midi, simple_sequence)
    engine.set_position_callback(lambda step: positions.append(step))
    engine.play()
    time.sleep(0.2)
    engine.stop()
    assert len(positions) > 0
    assert 0 in positions


def test_engine_all_notes_off_on_stop(mock_midi):
    seq = Sequence(
        notes=[Note(step=0, pitch=60, velocity=100, duration=4)],
        steps=4,
        bpm=600.0,
    )
    engine = SequencerEngine(mock_midi, seq)
    engine.play()
    time.sleep(0.05)
    engine.stop()
    # note_off should have been called for any active notes
    mock_midi.send_note_off.assert_called()


def test_step_duration_calculation(mock_midi):
    seq = Sequence(bpm=120.0, step_resolution="1/16")
    engine = SequencerEngine(mock_midi, seq)
    # At 120 BPM, 1/16 = 0.125 seconds per step
    dur = engine._step_duration_seconds()
    assert abs(dur - 0.125) < 0.001


def test_step_duration_eighth_notes(mock_midi):
    seq = Sequence(bpm=120.0, step_resolution="1/8")
    engine = SequencerEngine(mock_midi, seq)
    # At 120 BPM, 1/8 = 0.25 seconds per step
    dur = engine._step_duration_seconds()
    assert abs(dur - 0.25) < 0.001


# ── thread-safety & failure handling ──


def test_concurrent_edits_during_playback(mock_midi):
    """Toggling notes from the UI thread while the engine iterates must not
    kill the playback loop (the engine snapshots the note list per step)."""
    seq = Sequence(
        notes=[Note(step=s, pitch=40 + s, duration=2) for s in range(8)],
        steps=8,
        bpm=600.0,
    )
    engine = SequencerEngine(mock_midi, seq)
    engine.play()
    for i in range(500):
        seq.toggle_note(i % 8, 60 + (i % 12))
    time.sleep(0.15)
    assert engine.playing  # loop survived the edit storm
    engine.stop()
    assert not engine.playing


def test_raising_backend_stops_playback_cleanly(mock_midi, simple_sequence):
    mock_midi.send_note_on.side_effect = OSError("port died")
    engine = SequencerEngine(mock_midi, simple_sequence)
    engine.play()
    time.sleep(0.2)
    # Thread must have exited without an unhandled exception, clearing state
    assert not engine.playing


def test_device_disconnect_mid_play_stops(mock_midi, simple_sequence):
    def die(*a, **k):
        mock_midi.connected = False
        return False

    mock_midi.send_note_on.side_effect = die
    engine = SequencerEngine(mock_midi, simple_sequence)
    engine.play()
    time.sleep(0.3)
    assert not engine.playing


def test_no_stuck_notes_after_stop(mock_midi):
    """Every sounding pitch gets a note-off by the time stop() returns."""
    seq = Sequence(
        notes=[Note(step=s, pitch=30 + s, duration=8) for s in range(4)],
        steps=8,
        bpm=600.0,
    )
    engine = SequencerEngine(mock_midi, seq)
    engine.play()
    time.sleep(0.25)
    engine.stop()
    on_pitches = {c[0][0] for c in mock_midi.send_note_on.call_args_list}
    off_pitches = {c[0][0] for c in mock_midi.send_note_off.call_args_list}
    assert on_pitches <= off_pitches


# ── musicality: live tempo, drift, gate, shuffle, last step, probability ──


def test_live_tempo_change_applies_mid_playback(mock_midi):
    hits = []
    seq = Sequence(notes=[Note(step=0, pitch=60)], steps=4, bpm=30.0, step_resolution="1/16")
    engine = SequencerEngine(mock_midi, seq)
    engine.set_position_callback(lambda s: hits.append(s))
    engine.play()
    time.sleep(0.2)  # at 30 BPM a step is 0.5 s — we're still early in step 1
    seq.bpm = 1200.0  # 12.5 ms steps from the next step onward
    time.sleep(1.0)
    engine.stop()
    # At a fixed 30 BPM only ~3 steps fit in 1.2 s; live tempo blows past that
    assert len(hits) > 10


def test_absolute_clock_no_cumulative_drift(mock_midi):
    hits = []

    def slow_cb(step):
        hits.append(step)
        time.sleep(0.03)  # simulate per-step processing overhead

    seq = Sequence(notes=[Note(step=0, pitch=60)], steps=8, bpm=300.0)  # 50 ms steps
    engine = SequencerEngine(mock_midi, seq)
    engine.set_position_callback(slow_cb)
    engine.play()
    time.sleep(1.0)
    engine.stop()
    # Naive wait-after-work pacing gives 80 ms/step (~12 steps); the absolute
    # clock absorbs the overhead and stays at ~20
    assert len(hits) >= 16


def test_same_pitch_overlap_not_cut(mock_midi):
    """A pitch still held by a later overlapping note must not be turned off
    when the earlier note's hold expires."""
    engine = SequencerEngine(mock_midi)
    now = time.monotonic()
    engine._active = [(60, now - 0.01), (60, now + 10.0)]
    engine._flush_note_offs(time.monotonic())
    mock_midi.send_note_off.assert_not_called()
    # Once the sustaining note expires, exactly one off goes out
    engine._active = [(60, now - 0.01)]
    engine._flush_note_offs(time.monotonic())
    mock_midi.send_note_off.assert_called_once_with(60)


def test_gate_scales_note_off_time(mock_midi):
    ons, offs = [], []
    mock_midi.send_note_on.side_effect = lambda *a, **k: ons.append(time.monotonic())
    mock_midi.send_note_off.side_effect = lambda *a, **k: offs.append(time.monotonic())
    # 150 BPM at 1/16 -> 100 ms steps; duration 2 * gate 0.5 -> ~100 ms hold
    seq = Sequence(notes=[Note(step=0, pitch=60, duration=2)], steps=8, bpm=150.0)
    engine = SequencerEngine(mock_midi, seq)
    engine.gate = 0.5
    engine.play()
    time.sleep(0.45)
    engine.stop()
    assert ons and offs
    held = offs[0] - ons[0]
    assert 0.05 < held < 0.16  # ~one step, not the full two


def test_shuffle_delays_offbeats(mock_midi):
    times = {}
    seq = Sequence(notes=[Note(step=0, pitch=60)], steps=4, bpm=150.0)  # 100 ms steps
    engine = SequencerEngine(mock_midi, seq)
    engine.shuffle = 0.5  # even-numbered (1-indexed) steps land half a step late
    engine.set_position_callback(lambda s: times.setdefault(s, time.monotonic()))
    engine.play()
    time.sleep(0.45)
    engine.stop()
    assert {0, 1, 2} <= set(times)
    swung = times[1] - times[0]      # ~150 ms (delayed offbeat)
    recovered = times[2] - times[1]  # ~50 ms (back on the grid)
    assert swung > recovered


def test_last_step_truncates_pattern(mock_midi):
    positions = []
    seq = Sequence(notes=[Note(step=0, pitch=60)], steps=8, bpm=600.0)
    engine = SequencerEngine(mock_midi, seq)
    engine.last_step = 2
    engine.set_position_callback(lambda s: positions.append(s))
    engine.play()
    time.sleep(0.3)
    engine.stop()
    assert positions and set(positions) <= {0, 1}


def test_probability_zero_skips_all_notes(mock_midi):
    import random

    seq = Sequence(notes=[Note(step=s, pitch=60) for s in range(4)], steps=4, bpm=600.0)
    engine = SequencerEngine(mock_midi, seq, rng=random.Random(1))
    engine.probability = 0.0
    engine.play()
    time.sleep(0.2)
    engine.stop()
    mock_midi.send_note_on.assert_not_called()
