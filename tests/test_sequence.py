"""Tests for sequence data model and MIDI file I/O."""

import tempfile
from pathlib import Path

import mido
import pytest

from s1tui.sequence import Note, Sequence, load_midi, save_midi, _resolution_ticks


# ── Note / Sequence dataclass tests ──


def test_note_defaults():
    n = Note(step=0, pitch=60)
    assert n.velocity == 100
    assert n.duration == 1


def test_sequence_defaults():
    seq = Sequence()
    assert seq.steps == 16
    assert seq.bpm == 120.0
    assert seq.step_resolution == "1/16"
    assert seq.notes == []


def test_note_at():
    seq = Sequence(notes=[Note(step=3, pitch=60), Note(step=5, pitch=64)])
    assert seq.note_at(3, 60) is not None
    assert seq.note_at(3, 60).pitch == 60
    assert seq.note_at(0, 60) is None
    assert seq.note_at(3, 64) is None


def test_notes_at_step():
    seq = Sequence(notes=[
        Note(step=3, pitch=60),
        Note(step=3, pitch=64),
        Note(step=5, pitch=67),
    ])
    at_3 = seq.notes_at_step(3)
    assert len(at_3) == 2
    assert {n.pitch for n in at_3} == {60, 64}


def test_toggle_note_add():
    seq = Sequence()
    seq.toggle_note(0, 60)
    assert len(seq.notes) == 1
    assert seq.notes[0].step == 0
    assert seq.notes[0].pitch == 60


def test_toggle_note_remove():
    seq = Sequence(notes=[Note(step=0, pitch=60)])
    seq.toggle_note(0, 60)
    assert len(seq.notes) == 0


def test_clear():
    seq = Sequence(notes=[Note(step=0, pitch=60), Note(step=1, pitch=64)])
    seq.clear()
    assert len(seq.notes) == 0


# ── Resolution ticks ──


def test_resolution_ticks_16th():
    assert _resolution_ticks("1/16", 480) == 120


def test_resolution_ticks_8th():
    assert _resolution_ticks("1/8", 480) == 240


def test_resolution_ticks_quarter():
    assert _resolution_ticks("1/4", 480) == 480


# ── MIDI file round-trip ──


def _create_test_midi(path: Path, notes: list[tuple[int, int, int]], bpm: float = 120.0) -> None:
    """Helper: write a simple MIDI file with given (pitch, start_tick, duration_tick) notes."""
    mid = mido.MidiFile(ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm), time=0))

    events: list[tuple[int, str, int, int]] = []
    for pitch, start, dur in notes:
        events.append((start, "note_on", pitch, 100))
        events.append((start + dur, "note_off", pitch, 0))
    events.sort(key=lambda e: (e[0], 0 if e[1] == "note_off" else 1))

    prev = 0
    for tick, typ, pitch, vel in events:
        track.append(mido.Message(typ, note=pitch, velocity=vel, time=tick - prev))
        prev = tick
    track.append(mido.MetaMessage("end_of_track", time=0))
    mid.save(str(path))


def test_load_midi_basic():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.mid"
        # C4 at step 0, E4 at step 4 (1/16 = 120 ticks per step at 480 tpb)
        _create_test_midi(path, [
            (60, 0, 120),      # C4, step 0, duration 1
            (64, 480, 240),    # E4, step 4, duration 2
        ])
        seq = load_midi(path)
        assert seq.bpm == 120.0
        assert len(seq.notes) == 2
        # First note
        c4 = next(n for n in seq.notes if n.pitch == 60)
        assert c4.step == 0
        assert c4.duration == 1
        # Second note
        e4 = next(n for n in seq.notes if n.pitch == 64)
        assert e4.step == 4
        assert e4.duration == 2


def test_load_midi_tempo():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.mid"
        _create_test_midi(path, [(60, 0, 120)], bpm=140.0)
        seq = load_midi(path)
        assert abs(seq.bpm - 140.0) < 0.1


def test_load_midi_quantize():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.mid"
        # Note slightly off grid (at tick 130 instead of 120)
        _create_test_midi(path, [(60, 130, 120)])
        seq = load_midi(path, quantize="1/16")
        assert seq.notes[0].step == 1  # rounds to nearest step


def test_save_midi_roundtrip():
    seq = Sequence(
        notes=[
            Note(step=0, pitch=60, velocity=100, duration=1),
            Note(step=4, pitch=64, velocity=80, duration=2),
        ],
        steps=16,
        bpm=130.0,
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "out.mid"
        save_midi(seq, path)
        assert path.exists()
        # Reload and verify
        loaded = load_midi(path)
        assert abs(loaded.bpm - 130.0) < 0.1
        assert len(loaded.notes) == 2
        c4 = next(n for n in loaded.notes if n.pitch == 60)
        assert c4.step == 0
        assert c4.duration == 1
        e4 = next(n for n in loaded.notes if n.pitch == 64)
        assert e4.step == 4
        assert e4.duration == 2


def test_save_midi_empty_sequence():
    seq = Sequence()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "empty.mid"
        save_midi(seq, path)
        assert path.exists()
        loaded = load_midi(path)
        assert len(loaded.notes) == 0


def test_load_midi_max_steps():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.mid"
        # Note beyond max_steps
        _create_test_midi(path, [(60, 120 * 70, 120)])  # step 70
        seq = load_midi(path, max_steps=64)
        # Note at step 70 should be excluded
        assert len(seq.notes) == 0


def test_pattern_length_rounds_to_16():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.mid"
        # Notes up to step 5 → pattern should be 16
        _create_test_midi(path, [(60, 0, 120), (64, 120 * 5, 120)])
        seq = load_midi(path)
        assert seq.steps == 16


def test_pattern_length_extends_to_32():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.mid"
        # Note at step 17 → pattern should extend to 32
        _create_test_midi(path, [(60, 120 * 17, 120)])
        seq = load_midi(path)
        assert seq.steps == 32
