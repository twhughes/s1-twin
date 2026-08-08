"""Tests for synth.match.analyze — pitch detection + envelope segmentation.

Fully offline on synthetic signals: pure sines at known pitches, white noise,
silence, and a hand-built ADSR envelope. No hardware, no audio device, no files.
"""

import numpy as np
import pytest

from synth.match import WORKING_SR
from synth.match.analyze import (
    C3_FALLBACK,
    Segments,
    detect_f0,
    detect_notes,
    hz_to_midi,
    midi_to_hz,
    probe_note,
    segment,
)
from synth.match.capture import AudioClip


def sine(freq: float, seconds: float = 2.0, sr: int = WORKING_SR) -> AudioClip:
    t = np.linspace(0, seconds, int(seconds * sr), endpoint=False)
    return AudioClip((0.9 * np.sin(2 * np.pi * freq * t)).astype(np.float32), sr)


def cents(f_est: float, f_true: float) -> float:
    return 1200.0 * np.log2(f_est / f_true)


# Known pitches: (name, Hz, MIDI note). C3 uses the driver's 48 == ~130.81 Hz.
PITCHES = [
    ("A4", 440.0, 69),
    ("C3", 130.8128, 48),
    ("G4", 392.0, 67),
    ("A2", 110.0, 45),
    ("E5", 659.255, 76),
]


# ── detect_f0 on pure tones ──────────────────────────────────────────────────
class TestDetectF0:
    @pytest.mark.parametrize("name,freq,note", PITCHES)
    def test_pitch_within_a_few_cents(self, name, freq, note):
        f0 = detect_f0(sine(freq))
        assert f0 is not None, f"{name}: expected a pitch, got None"
        err = abs(cents(f0, freq))
        assert err < 10.0, f"{name}: {err:.2f} cents off ({f0:.3f} vs {freq})"

    @pytest.mark.parametrize("name,freq,note", PITCHES)
    def test_pitch_maps_to_correct_midi_note(self, name, freq, note):
        f0 = detect_f0(sine(freq))
        assert hz_to_midi(f0) == note

    def test_white_noise_is_unvoiced(self):
        rng = np.random.default_rng(0)
        clip = AudioClip((0.5 * rng.standard_normal(2 * WORKING_SR)).astype(np.float32), WORKING_SR)
        assert detect_f0(clip) is None

    def test_silence_is_unvoiced(self):
        clip = AudioClip(np.zeros(2 * WORKING_SR, dtype=np.float32), WORKING_SR)
        assert detect_f0(clip) is None

    def test_empty_clip_is_unvoiced(self):
        assert detect_f0(AudioClip(np.zeros(0, dtype=np.float32), WORKING_SR)) is None


# ── Hz ↔ MIDI ────────────────────────────────────────────────────────────────
class TestHzMidi:
    def test_a4_is_69(self):
        assert hz_to_midi(440.0) == 69

    def test_c3_is_48(self):
        assert hz_to_midi(130.8128) == 48

    def test_rounds_to_nearest(self):
        # A quarter-tone above A4 still rounds to A4, a quarter-tone below to G#4.
        assert hz_to_midi(440.0 * 2 ** (0.49 / 12)) == 69
        assert hz_to_midi(440.0 * 2 ** (-0.49 / 12)) == 69

    def test_roundtrip(self):
        for note in (36, 45, 48, 60, 69, 76):
            assert hz_to_midi(midi_to_hz(note)) == note


# ── probe_note (the driver's replacement for hardwired 48) ───────────────────
class TestProbeNote:
    def test_voiced_returns_detected_note(self):
        assert probe_note(sine(392.0)) == 67  # G4, not C3

    def test_unvoiced_falls_back_to_c3(self):
        clip = AudioClip(np.zeros(2 * WORKING_SR, dtype=np.float32), WORKING_SR)
        assert probe_note(clip) == C3_FALLBACK == 48

    def test_noise_falls_back_to_c3(self):
        rng = np.random.default_rng(1)
        clip = AudioClip((0.5 * rng.standard_normal(2 * WORKING_SR)).astype(np.float32), WORKING_SR)
        assert probe_note(clip) == 48


# ── multi-pitch detection (detect_notes) ─────────────────────────────────────
def harmonic_note(midi: int, t: np.ndarray, n_harm: int = 6) -> np.ndarray:
    """One harmonic (steady, clean) tone at ``midi`` — a stack of ``n_harm`` sines
    with 1/h amplitudes, exactly the material detect_notes is honest about."""
    f = midi_to_hz(midi)
    return sum((1.0 / h) * np.sin(2 * np.pi * f * h * t) for h in range(1, n_harm + 1))


def chord_clip(midis: list[int], seconds: float = 2.0, sr: int = WORKING_SR) -> AudioClip:
    t = np.linspace(0, seconds, int(seconds * sr), endpoint=False)
    x = sum(harmonic_note(m, t) for m in midis)
    x = x / np.abs(x).max()
    return AudioClip(x.astype(np.float32), sr)


class TestDetectNotes:
    def test_three_note_major_triad(self):
        # C major triad: C4 + E4 + G4 (MIDI 60, 64, 67).
        assert detect_notes(chord_clip([60, 64, 67])) == [60, 64, 67]

    def test_four_note_seventh_chord(self):
        # C dominant-7th: C4 + E4 + G4 + Bb4 (MIDI 60, 64, 67, 70).
        assert detect_notes(chord_clip([60, 64, 67, 70])) == [60, 64, 67, 70]

    def test_returns_notes_low_to_high(self):
        got = detect_notes(chord_clip([67, 60, 64]))  # unsorted input notes
        assert got == sorted(got) == [60, 64, 67]

    def test_single_sine_is_exactly_one_note(self):
        # A pure single pitch must yield exactly one note (no polyphony regression).
        assert detect_notes(sine(392.0)) == [67]  # G4

    def test_single_harmonic_note_is_one_note(self):
        assert detect_notes(chord_clip([57])) == [57]  # A3, harmonic-rich but mono

    def test_respects_max_notes_cap(self):
        got = detect_notes(chord_clip([60, 64, 67, 70]), max_notes=2)
        assert len(got) <= 2

    def test_silence_falls_back(self):
        clip = AudioClip(np.zeros(WORKING_SR, dtype=np.float32), WORKING_SR)
        assert detect_notes(clip) == [C3_FALLBACK]


# ── segmentation ─────────────────────────────────────────────────────────────
def adsr_clip(
    carrier: float = 220.0,
    attack_s: float = 0.10,
    decay_s: float = 0.10,
    sustain_s: float = 1.0,
    release_s: float = 0.30,
    sustain_level: float = 0.6,
    sr: int = WORKING_SR,
) -> tuple[AudioClip, dict]:
    a, d, s, r = (int(x * sr) for x in (attack_s, decay_s, sustain_s, release_s))
    env = np.concatenate(
        [
            np.linspace(0.0, 1.0, a, endpoint=False),
            np.linspace(1.0, sustain_level, d, endpoint=False),
            np.full(s, sustain_level),
            np.linspace(sustain_level, 0.0, r),
        ]
    )
    t = np.arange(env.size) / sr
    sig = env * np.sin(2 * np.pi * carrier * t)
    bounds = {"attack": a, "decay": d, "sustain": s, "release": r, "total": env.size}
    return AudioClip(sig.astype(np.float32), sr), bounds


class TestSegment:
    def test_returns_segments_type(self):
        clip, _ = adsr_clip()
        assert isinstance(segment(clip), Segments)

    def test_ordering_attack_sustain_release(self):
        clip, _ = adsr_clip()
        seg = segment(clip)
        assert seg.attack.start < seg.attack.end
        assert seg.attack.end == seg.sustain.start
        assert seg.sustain.start < seg.sustain.end
        assert seg.sustain.end == seg.release.start
        assert seg.release.start < seg.release.end

    def test_attack_ends_near_peak(self):
        clip, b = adsr_clip()
        seg = segment(clip)
        peak = b["attack"]  # samples: envelope peaks at end of the attack ramp
        # Attack end lands within ~20 ms of the true peak.
        assert abs(seg.attack.end - peak) < 0.02 * WORKING_SR

    def test_release_starts_near_note_off(self):
        clip, b = adsr_clip()
        seg = segment(clip)
        note_off = b["attack"] + b["decay"] + b["sustain"]
        # Release begins within ~50 ms of where the sustain plateau ends (the
        # detector fires a little into the ramp, once the level leaves the plateau).
        assert abs(seg.release.start - note_off) < 0.05 * WORKING_SR

    def test_release_ends_in_the_final_decay(self):
        clip, b = adsr_clip()
        seg = segment(clip)
        note_off = b["attack"] + b["decay"] + b["sustain"]
        # Release ends inside the final decay ramp: past note-off, at/before the
        # last sample, and within the last ~0.1 s (the tail drops below the audible
        # floor before the mathematical zero, so the end precedes total).
        assert note_off < seg.release.end <= b["total"]
        assert seg.release.end > b["total"] - 0.1 * WORKING_SR

    def test_covers_full_voiced_span(self):
        clip, _ = adsr_clip()
        seg = segment(clip)
        assert seg.attack.start >= 0
        assert seg.release.end <= clip.samples.size

    def test_silence_yields_empty_segments(self):
        clip = AudioClip(np.zeros(WORKING_SR, dtype=np.float32), WORKING_SR)
        seg = segment(clip)
        assert seg.attack == seg.sustain == seg.release
        assert seg.attack.start == seg.attack.end == 0

    def test_segment_seconds_helper(self):
        clip, _ = adsr_clip()
        seg = segment(clip)
        lo, hi = seg.attack.seconds(WORKING_SR)
        assert lo == pytest.approx(seg.attack.start / WORKING_SR)
        assert hi == pytest.approx(seg.attack.end / WORKING_SR)
