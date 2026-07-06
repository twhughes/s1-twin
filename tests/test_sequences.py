"""Tests for the sequence bank (synth/sequences.py)."""

from __future__ import annotations

import pytest

from synth import sequences
from synth.sequence import Note, Sequence


@pytest.fixture(autouse=True)
def tmp_bank(tmp_path, monkeypatch):
    monkeypatch.setattr(sequences, "SEQUENCE_DIR", tmp_path)
    return tmp_path


def make_seq() -> Sequence:
    return Sequence(
        notes=[Note(step=0, pitch=60, velocity=90, duration=2),
               Note(step=3, pitch=67, velocity=110, duration=1)],
        steps=16,
        bpm=118.0,
        step_resolution="1/16",
    )


class TestRoundTrip:
    def test_save_load(self):
        path = sequences.save_sequence("groove", make_seq())
        assert path.name == "groove.json"
        loaded = sequences.load_sequence(path)
        assert loaded.steps == 16
        assert loaded.bpm == 118.0
        assert [(n.step, n.pitch, n.velocity, n.duration) for n in loaded.notes] == [
            (0, 60, 90, 2), (3, 67, 110, 1),
        ]

    def test_metadata(self):
        path = sequences.save_sequence("m", make_seq(), metadata={"source": "agent"})
        assert sequences.load_sequence_metadata(path) == {"source": "agent"}

    def test_list_and_delete(self):
        sequences.save_sequence("a", make_seq())
        sequences.save_sequence("b", make_seq())
        names = [p.stem for p in sequences.list_sequences()]
        assert names == ["a", "b"]
        assert sequences.delete_sequence("a") is True
        assert sequences.delete_sequence("a") is False
        assert [p.stem for p in sequences.list_sequences()] == ["b"]

    def test_bad_name_rejected(self):
        with pytest.raises(ValueError):
            sequences.save_sequence("../evil", make_seq())


class TestDeviceLimits:
    def test_steps_clamped_to_64(self):
        seq = sequences.sequence_from_dict({"steps": 200, "notes": []})
        assert seq.steps == 64

    def test_notes_past_end_dropped_and_counted(self):
        seq = sequences.sequence_from_dict({
            "steps": 16,
            "notes": [{"step": 0, "pitch": 60}, {"step": 40, "pitch": 60}],
        })
        assert len(seq.notes) == 1
        assert seq.dropped_notes == 1

    def test_pitch_velocity_clamped(self):
        seq = sequences.sequence_from_dict({
            "steps": 16,
            "notes": [{"step": 0, "pitch": 300, "velocity": 500, "duration": 0}],
        })
        n = seq.notes[0]
        assert n.pitch == 127
        assert n.velocity == 127
        assert n.duration == 1

    def test_defaults(self):
        seq = sequences.sequence_from_dict({})
        assert seq.steps == 16
        assert seq.bpm == 120.0
        assert seq.step_resolution == "1/16"
