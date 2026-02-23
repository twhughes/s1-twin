"""Tests for the sequencer engine."""

import time
from unittest.mock import MagicMock, call

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
