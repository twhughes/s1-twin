"""Tests for sequence data model and MIDI file I/O."""

import tempfile
from pathlib import Path

import mido
import pytest

from synth.sequence import Note, Sequence, _resolution_ticks, load_midi, save_midi

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


# ── device limits (G7) ───────────────────────────────────────
def test_poly_warning_over_four_notes_per_step():
    from synth.sequence import MAX_NOTES_PER_STEP

    seq = Sequence(steps=4)
    for pitch in (60, 64, 67, 71):
        seq.toggle_note(0, pitch)
    assert MAX_NOTES_PER_STEP == 4
    assert seq.poly_warnings() == []
    seq.toggle_note(0, 74)  # fifth note on step 0
    assert seq.poly_warnings() == [0]


def test_max_steps_constant():
    from synth.sequence import MAX_STEPS

    assert MAX_STEPS == 64


# ── C3: the .mid round-trip LAW (the interchange contract) ────
#
# "load_midi(save_midi(seq)) reproduces notes, bpm, and step_resolution
#  exactly" (chassis-spec C3). Pinned across a spread of sequences so the
# single most important interchange contract cannot silently regress. Grids
# are of the form 1/N — the only ones sequence.py's tick math is defined for
# (triplet scales live in the PRM layer, not the .mid seam).

def _notes_key(seq: Sequence) -> list[tuple[int, int, int, int]]:
    return sorted((n.step, n.pitch, n.velocity, n.duration) for n in seq.notes)


C3_LAW_SEQUENCES = {
    "empty": Sequence(),
    "single-note": Sequence(notes=[Note(step=0, pitch=60)], steps=16, bpm=120.0),
    "chord-and-melody": Sequence(
        notes=[
            Note(step=0, pitch=48, velocity=100, duration=2),
            Note(step=0, pitch=60, velocity=90, duration=1),
            Note(step=3, pitch=55, velocity=110, duration=4),
        ],
        steps=16, bpm=98.5, step_resolution="1/16",
    ),
    "coarse-grid": Sequence(
        notes=[Note(step=0, pitch=36, velocity=70, duration=1),
               Note(step=2, pitch=43, velocity=127, duration=3)],
        steps=8, bpm=90.0, step_resolution="1/8",
    ),
    "quarter-grid": Sequence(
        notes=[Note(step=1, pitch=72, velocity=64, duration=2)],
        steps=8, bpm=174.0, step_resolution="1/4",
    ),
    "fine-grid": Sequence(
        notes=[Note(step=5, pitch=90, velocity=1, duration=1),
               Note(step=31, pitch=21, velocity=127, duration=1)],
        steps=32, bpm=120.0, step_resolution="1/32",
    ),
    "extreme-pitch-and-vel": Sequence(
        notes=[Note(step=0, pitch=0, velocity=127, duration=1),
               Note(step=7, pitch=127, velocity=1, duration=8)],
        steps=16, bpm=60.0, step_resolution="1/16",
    ),
}


@pytest.mark.parametrize("seq", C3_LAW_SEQUENCES.values(), ids=list(C3_LAW_SEQUENCES))
def test_c3_roundtrip_law(seq, tmp_path):
    """load_midi(save_midi(seq)) reproduces notes, bpm and step_resolution."""
    path = tmp_path / "law.mid"
    save_midi(seq, path)
    loaded = load_midi(path, quantize=seq.step_resolution)

    assert loaded.step_resolution == seq.step_resolution
    assert abs(loaded.bpm - seq.bpm) < 0.1
    assert _notes_key(loaded) == _notes_key(seq)


@pytest.mark.parametrize("seq", C3_LAW_SEQUENCES.values(), ids=list(C3_LAW_SEQUENCES))
def test_c3_note_off_sorts_before_note_on_at_equal_ticks(seq, tmp_path):
    """C3 ordering rule: at any shared tick, every note_off precedes every
    note_on. Walk the saved track and assert no note_on ever appears before a
    note_off that lands on the same absolute tick."""
    path = tmp_path / "order.mid"
    save_midi(seq, path)
    mid = mido.MidiFile(str(path))

    abs_tick = 0
    seen_on_at: dict[int, bool] = {}
    for msg in mid.tracks[0]:
        abs_tick += msg.time
        if msg.type == "note_on" and msg.velocity > 0:
            seen_on_at[abs_tick] = True
        elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            assert not seen_on_at.get(abs_tick), (
                f"note_off at tick {abs_tick} came after a note_on at the same tick"
            )


def test_c3_note_off_before_note_on_touching_notes(tmp_path):
    """A note ending exactly where the next begins: the off must be written
    before the on so the device never hears a zero-length overlap."""
    seq = Sequence(
        notes=[Note(step=0, pitch=60, duration=1), Note(step=1, pitch=64, duration=1)],
        steps=16, step_resolution="1/16",
    )
    path = tmp_path / "touch.mid"
    save_midi(seq, path)
    mid = mido.MidiFile(str(path))

    order, abs_tick = [], 0
    for msg in mid.tracks[0]:
        abs_tick += msg.time
        if msg.type in ("note_on", "note_off"):
            order.append((abs_tick, msg.type, msg.note))
    # The off of pitch 60 and the on of pitch 64 both land on tick 120.
    off_60 = order.index((120, "note_off", 60))
    on_64 = order.index((120, "note_on", 64))
    assert off_60 < on_64


# ── C3 / M2: motion lanes export as CC events (save_midi seam) ─
def test_save_midi_places_cc_events_at_step_ticks(tmp_path):
    """cc_events become control_change messages at step-boundary ticks on the
    requested channel — the motion-lane half of C3, at the writer's seam."""
    seq = Sequence(notes=[Note(step=0, pitch=60, duration=1)], steps=16, step_resolution="1/16")
    path = tmp_path / "motion.mid"
    # cutoff sweep on CC 74 at steps 0 and 2, synth channel (0-indexed) 3.
    save_midi(seq, path, cc_events=[(0, 74, 100), (2, 74, 40)], channel=3)

    mid = mido.MidiFile(str(path))
    step_ticks = mid.ticks_per_beat // 4  # 1/16 at 480 ppqn = 120
    ccs, abs_tick = [], 0
    for msg in mid.tracks[0]:
        abs_tick += msg.time
        if msg.type == "control_change":
            ccs.append((abs_tick, msg.control, msg.value, msg.channel))
    assert ccs == [(0, 74, 100, 3), (2 * step_ticks, 74, 40, 3)]


# ── Note / Sequence validation at the interchange seam (C3) ───
class TestNoteValidation:
    def test_valid_edges_accepted(self):
        Note(step=0, pitch=0, velocity=0, duration=1)
        Note(step=63, pitch=127, velocity=127, duration=64)

    def test_pitch_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            Note(step=0, pitch=128)
        with pytest.raises(ValueError):
            Note(step=0, pitch=-1)

    def test_velocity_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            Note(step=0, pitch=60, velocity=200)
        with pytest.raises(ValueError):
            Note(step=0, pitch=60, velocity=-1)

    def test_negative_step_rejected(self):
        with pytest.raises(ValueError):
            Note(step=-1, pitch=60)

    def test_zero_duration_rejected(self):
        with pytest.raises(ValueError):
            Note(step=0, pitch=60, duration=0)

    def test_mutation_stays_unchecked(self):
        """Sequencer/piano-roll mutate in place — validation is a birth check,
        not a cage. A live note may be pushed out of range without raising."""
        n = Note(step=0, pitch=60)
        n.pitch = 200  # no raise: __post_init__ ran only at construction
        assert n.pitch == 200


class TestSequenceValidation:
    def test_default_sequence_valid(self):
        Sequence()  # must not raise

    def test_zero_steps_rejected(self):
        with pytest.raises(ValueError):
            Sequence(steps=0)

    def test_non_positive_bpm_rejected(self):
        with pytest.raises(ValueError):
            Sequence(bpm=0.0)
        with pytest.raises(ValueError):
            Sequence(bpm=-120.0)

    def test_negative_dropped_notes_rejected(self):
        with pytest.raises(ValueError):
            Sequence(dropped_notes=-1)

    def test_sequence_is_not_frozen(self):
        """Sequence MUST stay mutable — toggle_note/clear/the sequencer edit
        it in place. Pin that it is not accidentally frozen."""
        seq = Sequence()
        seq.toggle_note(0, 60)
        seq.steps = 32
        seq.bpm = 140.0
        seq.clear()
        assert seq.steps == 32
        assert seq.bpm == 140.0
        assert seq.notes == []


# ── No silent caps: overflow is always surfaced (chassis guardrail) ──
def test_poly_warnings_reports_every_offending_step():
    """More than 4 notes on a step is reported for EVERY such step, sorted —
    never a silent truncation."""
    seq = Sequence(steps=8)
    for pitch in range(60, 66):      # 6 notes on step 0
        seq.toggle_note(0, pitch)
    for pitch in range(40, 45):      # 5 notes on step 5
        seq.toggle_note(5, pitch)
    seq.toggle_note(2, 72)           # 1 note on step 2 (fine)
    assert seq.poly_warnings() == [0, 5]


def test_dropped_notes_field_defaults_zero_and_is_surfaced():
    """A hand-built sequence reports zero drops; the field is the channel by
    which importers surface the count (never swallowed)."""
    seq = Sequence(notes=[Note(step=0, pitch=60)], steps=16)
    assert seq.dropped_notes == 0
