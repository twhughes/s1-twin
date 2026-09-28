"""Synthetic voices and synth sounds for the pitch and reach tests (tests/test_pitch.py,
tests/test_reach.py, tests/test_match_prepare.py): a sung vowel, a rough voice, a whistle, saws.

Synthetic only, on purpose: no recording of anyone's voice goes into this repository.
"""

from __future__ import annotations

import io
import math

import numpy as np
from scipy.signal import butter, lfilter, sosfilt

from synth.match import WORKING_SR as SR
from synth.match.analyze import midi_to_hz


def envelope(n: int, attack: float = 0.04, release: float = 0.12) -> np.ndarray:
    t = np.arange(n) / SR
    return np.minimum(1.0, t / attack) * np.clip((n / SR - t) / release, 0.0, 1.0)


def pitch_track(f0: float, n: int, *, vib_hz: float = 5.5, vib_cents: float = 30.0,
                drift_cents: float = 15.0, seed: int = 0) -> np.ndarray:
    """``f0`` (Hz) moved sample by sample by a vibrato and a slow random drift (a new aim every
    ~90 ms, joined by straight lines, scaled to +-``drift_cents``)."""
    rng = np.random.default_rng(seed)
    t = np.arange(n) / SR
    knots = rng.standard_normal(n // 2048 + 2)
    drift = np.interp(np.arange(n) / 2048.0, np.arange(knots.size), knots)
    drift = drift - drift.mean()
    drift = drift_cents * drift / max(float(np.abs(drift).max()), 1e-9)
    return f0 * 2.0 ** ((vib_cents * np.sin(2 * np.pi * vib_hz * t) + drift) / 1200.0)


def glottal(freq: np.ndarray, *, jitter: float = 0.0, shimmer: float = 0.0, seed: int = 1) -> np.ndarray:
    """A glottal pulse train as the lips radiate it: every harmonic, falling 6 dB an octave, on one
    phase accumulator (so a vibrato moves every partial). ``jitter`` roughens the period and
    ``shimmer`` the level, every few milliseconds."""
    rng = np.random.default_rng(seed)
    n = freq.size

    def wander(step: int) -> np.ndarray:
        return np.interp(np.arange(n) / step, np.arange(n // step + 2), rng.standard_normal(n // step + 2))

    if jitter:
        freq = freq * (1.0 + jitter * wander(64))
    phase = 2 * np.pi * np.cumsum(freq) / SR
    top = 0.45 * SR
    out = np.zeros(n)
    for h in range(1, int(top // float(freq.min())) + 1):
        out += np.where(freq * h < top, 1.0 / h, 0.0) * np.sin(h * phase)
    if shimmer:
        out *= 1.0 + shimmer * wander(128)
    return out / np.std(out)


def formant(x: np.ndarray, hz: float, bw: float) -> np.ndarray:
    """A two-pole resonance at ``hz`` (``bw`` wide), unity gain at DC: one formant of a vocal tract."""
    r = math.exp(-math.pi * bw / SR)
    c = 2 * r * math.cos(2 * math.pi * hz / SR)
    return lfilter([1.0 - c + r * r], [1.0, -c, r * r], x)


def vowel(f0: float = midi_to_hz(49) * 2 ** (-28 / 1200), seconds: float = 1.2,
          formants: tuple = ((500, 80), (1500, 110)), breath_db: float = -25.0, seed: int = 0,
          **track: float) -> np.ndarray:
    """A sung vowel: glottal pulses through two formants, with vibrato, drift and a little breath.
    By default C#3 sung 28 cents flat (136.4 Hz), between C3 and C#3."""
    n = int(seconds * SR)
    src = glottal(pitch_track(f0, n, seed=seed, **track), seed=seed + 1)
    src = src + 10 ** (breath_db / 20) * np.random.default_rng(seed + 2).standard_normal(n)
    for hz, bw in formants:
        src = formant(src, hz, bw)
    y = src * envelope(n)
    return 0.8 * y / np.abs(y).max()


def rough_voice(f0: float = 220.0, seed: int = 3) -> np.ndarray:
    """A rough, breathy voice: 3% jitter, 50% shimmer, a wide drift, and more breath than tone."""
    n = int(1.2 * SR)
    src = glottal(pitch_track(f0, n, vib_hz=6.0, vib_cents=25.0, drift_cents=40.0, seed=seed),
                  jitter=0.03, shimmer=0.5, seed=seed)
    src = src + 10 ** (3.0 / 20) * np.random.default_rng(seed + 1).standard_normal(n)
    y = formant(formant(src, 700, 130), 1200, 150) * envelope(n)
    return 0.8 * y / np.abs(y).max()


def whistle(hz: float = 1260.0) -> np.ndarray:
    """A whistle: a sine with a slight vibrato, a trace of its octave, a breath of noise."""
    n = int(1.2 * SR)
    ph = 2 * np.pi * np.cumsum(pitch_track(hz, n, vib_hz=5.0, vib_cents=10.0, drift_cents=5.0)) / SR
    y = np.sin(ph) + 0.05 * np.sin(2 * ph) + 10 ** (-35 / 20) * np.random.default_rng(0).standard_normal(n)
    y = y * envelope(n, 0.05, 0.1)
    return 0.8 * y / np.abs(y).max()


def saw(hz: float, n: int) -> np.ndarray:
    ph = 2 * np.pi * hz * np.arange(n) / SR
    return sum(np.sin(h * ph) / h for h in range(1, int(0.45 * SR // hz) + 1))


def saws(*notes: int) -> np.ndarray:
    """Band-limited saws on ``notes``, summed: a synth chord."""
    n = int(1.2 * SR)
    y = sum(saw(midi_to_hz(m), n) for m in notes) * envelope(n, 0.01, 0.1)
    return 0.8 * y / np.abs(y).max()


def lowpass_saw(note: int = 48, cutoff: float = 900.0) -> np.ndarray:
    """A clean saw through a lowpass with no resonance."""
    n = int(1.2 * SR)
    y = sosfilt(butter(4, cutoff, "lowpass", fs=SR, output="sos"), saw(midi_to_hz(note), n))
    y = y * envelope(n, 0.01, 0.1)
    return 0.8 * y / np.abs(y).max()


def take(x: np.ndarray, lead: float = 1.0, tail: float = 1.0, rate: int = 48000, seed: int = 0) -> bytes:
    """What the browser uploads: ``x`` (at SR) at a recorder's rate, 16-bit, with ``lead`` s of a
    quiet room before it and ``tail`` s after."""
    import soundfile as sf
    from scipy.signal import resample_poly

    g = math.gcd(rate, SR)
    y = resample_poly(np.asarray(x, dtype=np.float64), rate // g, SR // g)
    y = np.concatenate([np.zeros(int(lead * rate)), 0.4 * y, np.zeros(int(tail * rate))])
    y = y + 1e-3 * np.random.default_rng(seed).standard_normal(y.size)
    buf = io.BytesIO()
    sf.write(buf, y.astype(np.float32), rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()
