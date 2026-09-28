"""What in a sound the S-1 cannot make: measured, so the Match view can say why a match falls short.

Tyler, 2026-09-28: "this has to work with recorded sounds like vocal sounds. it doesnt yet. and i
need to know why". The S-1 is oscillators through ONE resonant lowpass, one envelope and one LFO:
it can match a pitch (with an even vibrato), an envelope, some noise and one resonance. A voice
has two or more resonances (the formants: they make the vowel), a pitch that drifts unevenly, and
breath. :func:`reach` measures, on the take the matcher gets:

* ``resonances``: formants by linear prediction. At each one-note frame of the pitch track
  (:mod:`synth.match.pitch`), 30 ms of the sound at 11 kHz, tilted up (pre-emphasis), gets an
  order-12 all-pole fit; each pole pair is a resonance at its angle, as wide as its distance from
  the unit circle says. The narrow ones (MAX_BW) above the fundamental, within STRONG_DB of the
  frame's strongest (read on the frame's harmonics, tilted up as the fit sees them: an all-pole
  peak's own height says more about its width than its loudness), are the frame's resonances;
  those that come back in SUPPORT of the frames are the sound's (``db``: under the strongest listed).
  None for a pitch above MAX_F0 (the harmonics are too far apart to show a resonance between
  them), and none for a narrow pulse wave (its spectrum has gaps at every N-th harmonic, N >= 3,
  and humps between them: the pulse's shape, not resonances; the S-1 makes those).
* ``vowel_like``: in at least VOWEL_SHARE of the frames, two resonances about an octave or more
  apart (VOWEL_RATIO): what one resonant filter cannot make. Frame by frame, so a vowel whose
  resonances move (a glide, "wow") still counts; its list is then the middle of each frame's pair.
* ``breath_db``: the power outside the harmonics against the power in them (dB).
* ``irregular``: the pitch wobble, in cents, that no regular LFO wave follows; ``wobble`` all of it.

Pure numpy. The plain sentences are the view's (views/match.js reachLines), which also knows the
patch the match found.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .pitch import EVEN_DB, Pitch, analyze, band_limit

LPC_SR_DIV = 2         # the fit runs at half the rate (11 kHz at the twin's 22.05 kHz)
LPC_HI = 4500.0        # Hz: the band the fit sees
LPC_ORDER = 12         # pole pairs enough for four resonances and the tilt
LPC_S = 0.030          # seconds of sound per fit
PRE_EMPHASIS = 0.94    # the tilt: a voice's source falls about 6 dB an octave
MAX_BW = 300.0         # Hz: a resonance is at most this wide (a lowpass knee or a speaker is wider)
LO_HZ = 150.0          # Hz: no resonance under this...
MIN_F0_RATIO = 1.5     # ...nor under this many times the pitch (the fundamental is not a resonance)
SUPPORT = 0.35         # a resonance comes back in at least this share of the frames
VOWEL_SHARE = 0.5      # like a vowel: two resonances VOWEL_RATIO apart in this share of the frames
CLUSTER_OCT = 1.0 / 8  # candidates this near (octaves) are the same resonance
STRONG_DB = 30.0       # a resonance within this of the strongest counts
MAX_F0 = 400.0         # Hz: above this pitch no resonance is measured
VOWEL_RATIO = 1.4      # two resonances this far apart (half an octave: "ah", "oh") or more: like a vowel
MAX_RESONANCES = 3
HOLE_DB = 15.0         # a harmonic this far under both neighbours is a gap (a square's even ones)...
PULSE_GAP_DB = 10.0    # ...and this far, at every N-th harmonic, a narrow pulse's gap
TILT_DB = 6.0          # dB an octave: strengths are read on the harmonics tilted up as the fit sees them


def _lpc(frame: np.ndarray, order: int) -> np.ndarray | None:
    """All-pole coefficients a[0..order] (a[0] = 1) by the autocorrelation method (Levinson-Durbin),
    or None for a silent or unstable frame."""
    n = frame.size
    r = np.array([float(np.dot(frame[:n - k], frame[k:])) for k in range(order + 1)])
    if r[0] <= 1e-12:
        return None
    r[0] *= 1.0001                                       # a whisper of white noise: always stable
    a = np.zeros(order + 1)
    a[0] = 1.0
    err = r[0]
    for i in range(1, order + 1):
        k = -(r[i] + float(np.dot(a[1:i], r[i - 1:0:-1]))) / err
        a[1:i + 1] = a[1:i + 1] + k * np.concatenate([a[i - 1:0:-1], [1.0]])
        err *= 1.0 - k * k
        if err <= 0:
            return None
    return a


def _gaps(levels: np.ndarray, depth: float = HOLE_DB) -> np.ndarray:
    """The harmonics more than ``depth`` dB under both neighbours (a square's missing even ones)."""
    gap = np.zeros(levels.size, dtype=bool)
    if levels.size >= 3:
        gap[1:-1] = levels[1:-1] < np.minimum(levels[:-2], levels[2:]) - depth
    return gap


def pulse_like(pitch: Pitch) -> bool:
    """A narrow pulse wave: over the one-note frames, the harmonics' average levels have gaps at
    every N-th harmonic (N >= 3; a square's N = 2 leaves one smooth slope over the odd harmonics)."""
    tr = pitch.track
    rows = [np.asarray(tr.harmonics[i]) for i in np.flatnonzero(tr.mono)
            if tr.harmonics[i] is not None]
    if not rows:
        return False
    size = min(len(r) for r in rows)
    if size < 6:
        return False
    levels = np.mean([10.0 * np.log10(r[:size] + 1e-20) for r in rows], axis=0)
    holes = np.flatnonzero(_gaps(levels, PULSE_GAP_DB)) + 1    # harmonic numbers
    if holes.size < 2 or holes[0] < 3:
        return False
    return bool(np.all(np.abs(holes / holes[0] - np.round(holes / holes[0])) < 0.15))


def frame_resonances(samples: np.ndarray, sr: int,
                     pitch: Pitch) -> list[tuple[float, list[tuple[float, float]]]]:
    """Each one-note frame's narrow resonances: [(the frame's weight, [(Hz, level dB)] low to high)],
    ``level`` the frame's harmonics there (tilted up as the fit sees them) under its strongest
    resonance's. Empty when they cannot be measured (see the module docs)."""
    if pitch.note is None or pitch.f0 is None or pitch.f0 > MAX_F0 or pulse_like(pitch):
        return []
    tr = pitch.track
    x = band_limit(samples, sr, 30.0, LPC_HI)[::LPC_SR_DIV]
    fs = sr / LPC_SR_DIV
    m = int(round(LPC_S * fs))
    win = np.hamming(m)
    lo = max(LO_HZ, MIN_F0_RATIO * pitch.f0)
    top = float(tr.power.max())
    out = []
    for i in np.flatnonzero(tr.mono):
        c = int(round(tr.times[i] * fs))
        seg = x[max(0, c - m // 2): max(0, c - m // 2) + m]
        if seg.size < m:
            continue
        seg = np.concatenate([seg[:1], seg[1:] - PRE_EMPHASIS * seg[:-1]]) * win
        a = _lpc(seg, LPC_ORDER)
        if a is None:
            continue
        harm = tr.harmonics[i]
        if harm is None or len(harm) < 2:
            continue
        levels = 10.0 * np.log10(np.asarray(harm) + 1e-20)
        keep = ~_gaps(levels)
        f_h = np.log2(tr.f0[i] * np.arange(1, len(harm) + 1))[keep]
        l_h = levels[keep] + TILT_DB * f_h                  # the harmonics, tilted as the fit sees them
        found = []
        for z in np.roots(a):
            if z.imag <= 0:
                continue
            hz = math.atan2(z.imag, z.real) * fs / (2.0 * math.pi)
            bw = -math.log(max(abs(z), 1e-12)) * fs / math.pi
            if bw <= MAX_BW and lo <= hz <= LPC_HI and math.log2(hz) <= f_h[-1]:
                found.append((hz, float(np.interp(math.log2(hz), f_h, l_h))))
        if found:
            best = max(level for _hz, level in found)
            found = sorted((hz, level - best) for hz, level in found if level >= best - STRONG_DB)
        # every frame within EVEN_DB of the loudest counts the same, as in the pitch
        w = min(1.0, float(tr.power[i]) / (10 ** (-EVEN_DB / 10) * top)) if top > 0 else 1.0
        out.append((w, found))
    return out


def _pair(found: list[tuple[float, float]]) -> tuple[tuple[float, float], ...] | None:
    """A frame's two resonances like a vowel's: its lowest, and the strongest one VOWEL_RATIO or
    more above it (None without such a pair)."""
    if not found:
        return None
    low = found[0]
    above = [r for r in found if r[0] >= VOWEL_RATIO * low[0]]
    return (low, max(above, key=lambda r: r[1])) if above else None


def resonances(samples: np.ndarray, sr: int, pitch: Pitch) -> tuple[list[dict[str, float]], bool]:
    """The sound's resonances, low to high ([{"hz", "db"}], ``db`` under the strongest listed; at
    most MAX_RESONANCES: the ones that come back in at least SUPPORT of the frames), and whether it
    is like a vowel: in at least VOWEL_SHARE of the frames, two resonances VOWEL_RATIO or more
    apart. A vowel's resonances move (a glide, a diphthong): then the list is the middle of each
    frame's pair."""
    frames = frame_resonances(samples, sr, pitch)
    total = sum(w for w, _f in frames)
    if total <= 0:
        return [], False
    pairs = [(w, _pair(found)) for w, found in frames]
    vowel = sum(w for w, pr in pairs if pr is not None) / total >= VOWEL_SHARE
    cands = [(math.log2(hz), level, w) for w, found in frames for hz, level in found]
    listed: list[dict[str, float]] = []
    if cands:
        lf = np.array([c[0] for c in cands])
        lv = np.array([c[1] for c in cands])
        wf = np.array([c[2] for c in cands])
        grid = np.arange(float(lf.min()), float(lf.max()) + 1e-9, 1.0 / 48)
        support = np.array([wf[np.abs(lf - g) <= CLUSTER_OCT / 2].sum() for g in grid]) / total
        for j in np.argsort(-support):
            if support[j] < SUPPORT or len(listed) >= MAX_RESONANCES:
                break
            near = np.abs(lf - grid[j]) <= CLUSTER_OCT
            hz = float(2.0 ** np.average(lf[near], weights=wf[near]))
            if all(abs(math.log2(hz / r["hz"])) >= CLUSTER_OCT for r in listed):
                listed.append({"hz": hz, "db": float(np.median(lv[near]))})
    if vowel and not vowel_like(listed):
        both = [pr for _w, pr in pairs if pr is not None]
        listed = [{"hz": float(np.median([pr[k][0] for pr in both])),
                   "db": float(np.median([pr[k][1] for pr in both]))} for k in (0, 1)]
    if not listed:
        return [], vowel
    best = max(r["db"] for r in listed)
    listed = sorted(({"hz": round(r["hz"], 1), "db": round(r["db"] - best, 1)} for r in listed),
                    key=lambda r: r["hz"])
    return listed, vowel


def vowel_like(res: list[dict[str, float]]) -> bool:
    """Two resonances about an octave or more apart."""
    hz = sorted(r["hz"] for r in res)
    return len(hz) >= 2 and hz[-1] / hz[0] >= VOWEL_RATIO


def reach(samples: np.ndarray, sr: int, pitch: Pitch | None = None) -> dict[str, Any]:
    """What the S-1 cannot make in ``samples`` (see the module docs), JSON-ready:
    ``{"resonances": [{"hz", "db"}] (low to high, at most 3), "vowel_like": bool,
    "breath_db": float | None, "wobble": float | None, "irregular": float | None,
    "vibrato_hz": float | None}``."""
    x = np.asarray(samples, dtype=np.float64)
    p = pitch if pitch is not None else analyze(x, sr)
    res, vowel = resonances(x, sr, p)

    def rnd(v: float | None, nd: int = 1) -> float | None:
        return None if v is None or not math.isfinite(v) else round(float(v), nd)

    return {
        "resonances": res,
        "vowel_like": vowel,
        "breath_db": rnd(p.breath_db),
        "wobble": rnd(p.wobble),
        "irregular": rnd(p.irregular),
        "vibrato_hz": rnd(p.vibrato_hz),
    }
