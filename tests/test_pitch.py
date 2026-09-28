"""Voice-aware pitch (synth/match/pitch.py) and the cold-start note set it gives the matcher.

Tyler's recorded takes (a sung C#3 a little flat, a wavering high tone, a rough voice) came out as
clusters of neighbouring semitones ("C3+C#3", "C#3+D3+D#3"), which the synth then played as chords.
These tests hold the fix on synthetic signals only: a sung vowel between two semitones, a whistle,
a real chord, a rough voice, the old chords. No recording of anyone is used.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("scipy")

from synth.match import WORKING_SR as SR  # noqa: E402
from synth.match import pitch  # noqa: E402
from synth.match import twin_session as ts  # noqa: E402
from synth.match.analyze import detect_notes, merge_neighbours  # noqa: E402
from synth.match.capture import AudioClip  # noqa: E402
from tests.voices import lowpass_saw, rough_voice, saws, vowel, whistle  # noqa: E402


# ── one sung note is one note ─────────────────────────────────────────────────
def test_a_sung_vowel_between_semitones_is_one_note() -> None:
    """C#3 sung 28 cents flat, with vibrato and drift: ONE note, C#3, and the cents say how flat."""
    x = vowel()
    found = pitch.detect(x, SR)
    assert found.notes == [49] and found.ranked == [49]
    p = found.pitch
    assert p.monophonic and p.confidence > 0.8
    assert abs(p.cents - (-28.0)) <= 10.0, p.cents
    assert 20.0 < p.wobble < 60.0, "it wavers: +-30 cents of vibrato and 15 of drift"
    assert p.vibrato_hz == pytest.approx(5.5, abs=0.6)
    assert ts.detect(x) == ([49], [49]), "the matcher's cold start is the same answer"


def test_the_old_detector_made_that_vowel_a_cluster() -> None:
    """Why: summed over the whole take, the vowel's partials smear across the semitones beside it.
    The chord detector alone no longer returns neighbours either (they merge into the strongest)."""
    from synth.match.analyze import _harmonic_salience_notes

    clip = AudioClip(vowel().astype(np.float32), SR)
    raw = [n for n, _s in _harmonic_salience_notes(clip)]
    assert any(abs(a - b) == 1 for a in raw for b in raw), f"the peel alone finds neighbours: {raw}"
    notes = detect_notes(clip)
    assert all(b - a > 1 for a, b in zip(notes, notes[1:])), notes


def test_a_whistle_is_its_note_not_an_octave_down() -> None:
    """1260 Hz (D#6 + 22 cents): a pitch range capped at 900 Hz would call it D#5."""
    found = pitch.detect(whistle(), SR)
    assert found.notes == [87]
    assert abs(found.pitch.cents - 22.0) <= 10.0 and found.pitch.confidence > 0.9


@pytest.mark.parametrize("notes", [[48, 55], [45, 52]], ids=["C3+G3", "A2+E3"])
def test_a_real_chord_a_fifth_apart_stays_two_notes(notes: list[int]) -> None:
    """Two saws a fifth apart repeat at a common sub-octave (C2 under C3+G3), but nothing sounds
    there: not one note. The chord detector gets them, and keeps both."""
    found = pitch.detect(saws(*notes), SR)
    assert found.notes == notes
    assert not found.pitch.monophonic and found.pitch.confidence < 0.2


def test_the_old_chords_still_come_out() -> None:
    triad = saws(60, 64, 67)
    assert pitch.detect(triad, SR).notes == [60, 64, 67]
    assert pitch.detect(saws(60, 64, 67, 70), SR).notes == [60, 64, 67, 70]


def test_a_clipped_chord_is_not_its_sub_octave() -> None:
    """Clipping puts difference tones at a triad's common sub-octave (C2 under C4+E4+G4), so the
    pitch there looks real; the chord detector's notes, all two octaves up, win."""
    clipped = np.clip(2.5 * saws(60, 64, 67), -0.8, 0.8)
    assert pitch.detect(clipped, SR).notes == [60, 64, 67]


def test_a_rough_voice_is_one_unsure_note() -> None:
    """More breath than tone, jitter and shimmer: the pitch is unsure (low confidence), and it is
    still ONE note (A3), not the chord its noise would give the chord detector."""
    found = pitch.detect(rough_voice(), SR)
    assert found.pitch.confidence < 0.5 and not found.pitch.monophonic
    assert found.notes == [57]


def test_silence_and_a_clean_synth_note() -> None:
    assert pitch.detect(np.zeros(SR), SR).notes == [48], "silence: the C3 fallback, as before"
    found = pitch.detect(lowpass_saw(), SR)
    assert found.notes == [48] and abs(found.pitch.cents) < 5 and found.pitch.wobble < 5
    assert found.pitch.vibrato_hz is None, "a steady note has no vibrato to name"


# ── the pieces ────────────────────────────────────────────────────────────────
def test_merge_neighbours_keeps_the_strongest_of_each_run() -> None:
    ranked = [(61, 9.0), (62, 7.0), (64, 5.0), (60, 4.0), (67, 3.0), (68, 1.0)]
    assert merge_neighbours(ranked) == [(61, 9.0), (64, 5.0), (67, 3.0)]
    assert merge_neighbours([(60, 1.0), (64, 0.5), (67, 0.4)]) == [(60, 1.0), (64, 0.5), (67, 0.4)]
    assert merge_neighbours([]) == []


def test_a_period_taken_twice_is_mended() -> None:
    """An octave, two octaves or a twelfth off its neighbours is a frame error; a glide is not."""
    t = np.arange(12) * 0.01
    c = np.full(12, 5700.0)
    c[3], c[6], c[9] = 4500.0, 3300.0, 5700.0 - 1901.96
    _t, mended = pitch.smooth_track(t, c)
    assert np.allclose(mended, 5700.0)
    glide = np.linspace(4900.0, 6400.0, 12)
    assert np.allclose(pitch.smooth_track(t, glide)[1], glide)


def test_a_square_lfo_trill_is_its_middle() -> None:
    """The S-1's own square LFO on the pitch spends unequal time on its two sides: the note is the
    wave's centre, not the side the median lands on."""
    t = np.arange(150) * 0.01
    square = 156.0 * pitch._lfo_shapes(2 * np.pi * 0.8 * t)[pitch.SQUARE]      # as the twin draws it
    irregular, rate, centre, wave = pitch.regular_fit(t, 5500.0 + square)
    assert wave == pitch.SQUARE and irregular < 10.0 and rate == pytest.approx(0.8, abs=0.05)
    assert abs(centre - 5500.0) < 5.0
    assert float(np.median(5500.0 + square)) > 5600.0, "the median lands on one side"


def test_the_track_follows_a_high_pitch() -> None:
    tr = pitch.track(whistle(1800.0), SR)
    f = tr.f0[tr.mono]
    assert f.size > 50 and np.all(np.abs(1200 * np.log2(f / 1800.0)) < 40), "no frame halved"
