"""Offline analysis of a target clip: pitch detection + envelope segmentation.

Phase A of the match-v3 rebuild (``docs/match-v3-spec.md``). Two jobs, both pure
numpy/scipy and hardware-free:

1. :func:`detect_f0` finds the target's fundamental with a YIN-style autocorrelation
   method, and :func:`probe_note` turns that into the MIDI note the driver should
   probe at — replacing the hardwired C3 (``driver.PROBE_NOTE = 48``) that made the
   matcher compare a C3 candidate against, say, a G4 target.
2. :func:`segment` splits the amplitude envelope into attack / sustain / release
   regions for later phases (ADSR estimation) to lean on.

Nothing here touches the S-1 or an audio device; callers pass an
:class:`~synth.match.capture.AudioClip` built from a file or a recording.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .capture import AudioClip

# MIDI note the driver falls back to when a target has no detectable pitch
# (noise/silence). 48 == C3 == ~130.81 Hz, the S-1's bass home. Kept here so the
# fallback lives with the detector that produces the alternative.
C3_FALLBACK = 48

# YIN search bounds. The S-1 is a bass/lead mono synth; 50-2000 Hz spans sub-bass
# up through the top of its useful single-note range.
DEFAULT_FMIN = 50.0
DEFAULT_FMAX = 2000.0

# CMNDF aperiodicity threshold. A frame is "voiced" only if the cumulative mean
# normalized difference dips below this for some lag; noise/silence never does.
YIN_THRESHOLD = 0.1

# Below this peak amplitude the clip is treated as silence (unvoiced).
SILENCE_FLOOR = 1e-4


# ── pitch detection (YIN) ────────────────────────────────────────────────────


def _analysis_window(x: np.ndarray, sr: int, seg_len: int) -> np.ndarray:
    """Return the loudest ``seg_len``-sample slice of ``x`` (mean-removed).

    Centering on peak energy keeps the estimate on the voiced body of a note
    rather than its onset transient or release tail.
    """
    if x.size <= seg_len:
        return np.pad(x, (0, seg_len - x.size)) - float(np.mean(x)) if x.size else x
    win = max(1, sr // 200)  # ~5 ms energy smoothing
    energy = np.convolve(x**2, np.ones(win) / win, mode="same")
    center = int(np.argmax(energy))
    start = min(max(0, center - seg_len // 2), x.size - seg_len)
    seg = x[start : start + seg_len]
    return seg - float(np.mean(seg))


def _difference(x: np.ndarray, w: int, tau_max: int) -> np.ndarray:
    """YIN difference function d(tau) over a ``w``-sample integration window."""
    diff = np.zeros(tau_max + 1)
    base = x[:w]
    for tau in range(1, tau_max + 1):
        delta = base - x[tau : tau + w]
        diff[tau] = np.dot(delta, delta)
    return diff


def _cmndf(diff: np.ndarray) -> np.ndarray:
    """Cumulative mean normalized difference (CMNDF), d'(0) = 1."""
    out = np.ones_like(diff)
    taus = np.arange(1, diff.size)
    running = np.cumsum(diff[1:])
    out[1:] = diff[1:] * taus / np.maximum(running, 1e-12)
    return out


def _parabolic(cmndf: np.ndarray, tau: int) -> float:
    """Sub-sample refine the lag of a CMNDF minimum by parabolic interpolation."""
    if tau <= 0 or tau + 1 >= cmndf.size:
        return float(tau)
    a, b, c = cmndf[tau - 1], cmndf[tau], cmndf[tau + 1]
    denom = a + c - 2.0 * b
    if abs(denom) < 1e-12:
        return float(tau)
    return tau + 0.5 * (a - c) / denom


def detect_f0(
    clip: AudioClip,
    fmin: float = DEFAULT_FMIN,
    fmax: float = DEFAULT_FMAX,
    threshold: float = YIN_THRESHOLD,
) -> float | None:
    """Estimate the fundamental frequency of ``clip`` in Hz, or ``None``.

    Uses the YIN method: difference function → cumulative mean normalized
    difference (CMNDF) → absolute-threshold lag pick → parabolic refinement.
    Returns ``None`` for silence, noise, or any clip with no lag whose CMNDF
    dips below ``threshold`` (i.e. nothing periodic enough to call a pitch).
    """
    x = np.asarray(clip.samples, dtype=np.float64)
    sr = clip.samplerate
    if x.size == 0 or float(np.abs(x).max()) < SILENCE_FLOOR:
        return None

    tau_min = max(1, int(sr / fmax))
    tau_max = int(sr / fmin)
    if tau_max <= tau_min:
        return None

    # Integrate over a few periods for a stable estimate, but don't outrun the clip.
    w = min(4 * tau_max, max(2 * tau_max, x.size - tau_max))
    if w < tau_max:
        return None
    seg = _analysis_window(x, sr, w + tau_max)

    cmndf = _cmndf(_difference(seg, w, tau_max))

    tau = tau_min
    best = None
    while tau <= tau_max:
        if cmndf[tau] < threshold:
            while tau + 1 <= tau_max and cmndf[tau + 1] < cmndf[tau]:
                tau += 1
            best = tau
            break
        tau += 1
    if best is None:
        return None

    refined = _parabolic(cmndf, best)
    if refined <= 0:
        return None
    return sr / refined


# ── Hz ↔ MIDI ────────────────────────────────────────────────────────────────


def hz_to_midi(hz: float) -> int:
    """Nearest MIDI note number for a frequency in Hz (A4 = 69 = 440 Hz)."""
    return int(round(69.0 + 12.0 * np.log2(hz / 440.0)))


def midi_to_hz(note: int) -> float:
    """Frequency in Hz for a MIDI note number."""
    return 440.0 * 2.0 ** ((note - 69) / 12.0)


def probe_note(clip: AudioClip) -> int:
    """MIDI note the driver should probe at for ``clip``.

    Detected pitch when voiced, else :data:`C3_FALLBACK` (48). This is the value
    that replaces the hardwired ``PROBE_NOTE = 48`` in ``driver.py``.
    """
    f0 = detect_f0(clip)
    if f0 is None:
        return C3_FALLBACK
    return hz_to_midi(f0)


# ── envelope segmentation (ADSR regions) ─────────────────────────────────────


@dataclass
class Segment:
    """A half-open ``[start, end)`` range of sample indices into the clip."""

    start: int
    end: int

    def seconds(self, samplerate: int) -> tuple[float, float]:
        return self.start / samplerate, self.end / samplerate


@dataclass
class Segments:
    """Attack / sustain / release regions of a clip's amplitude envelope.

    Boundaries are sample indices and always satisfy
    ``attack.start <= attack.end == sustain.start <= sustain.end == release.start
    <= release.end``. ``sustain`` lumps the post-peak decay in with the sustain
    plateau; ``release`` is the final decay to the noise floor.
    """

    attack: Segment
    sustain: Segment
    release: Segment
    samplerate: int


def _rms_envelope(x: np.ndarray, sr: int, win_ms: float = 12.0) -> np.ndarray:
    """Sample-indexed moving-RMS amplitude envelope, peak-normalized."""
    win = max(1, int(sr * win_ms / 1000.0))
    kernel = np.ones(win) / win
    rms = np.sqrt(np.convolve(x**2, kernel, mode="same") + 1e-12)
    peak = float(rms.max())
    if peak > 1e-9:
        rms = rms / peak
    return rms


def segment(clip: AudioClip, floor: float = 0.1) -> Segments:
    """Split ``clip`` into attack / sustain / release sample ranges.

    - **attack**: onset (first crossing of ``floor`` relative to peak) → peak.
    - **sustain**: peak → start of the final decay (post-peak decay + plateau).
    - **release**: final decay ramp → offset (last crossing of ``floor``).

    Degenerate input (empty or silent) yields three empty segments at 0.
    """
    x = np.asarray(clip.samples, dtype=np.float64)
    sr = clip.samplerate
    n = x.size
    if n == 0 or float(np.abs(x).max()) < SILENCE_FLOOR:
        empty = Segment(0, 0)
        return Segments(empty, empty, empty, sr)

    env = _rms_envelope(x, sr)
    above = np.nonzero(env >= floor)[0]
    if above.size == 0:
        empty = Segment(0, 0)
        return Segments(empty, empty, empty, sr)

    onset = int(above[0])
    offset = int(above[-1]) + 1  # exclusive end
    peak_idx = int(np.argmax(env))
    peak_idx = min(max(peak_idx, onset), offset - 1)

    # Release start = end of the sustain plateau. The plateau level is the median
    # envelope over the post-peak body (dominated by the held sustain); the last
    # sample at/above 90% of it marks where the final decay begins. Robust to the
    # residual RMS ripple that a local monotonic walk trips over.
    body = env[peak_idx:offset]
    plateau = float(np.median(body)) if body.size else float(env[peak_idx])
    held = np.nonzero(body >= 0.9 * plateau)[0]
    release_start = peak_idx + int(held[-1]) + 1 if held.size else peak_idx
    release_start = min(max(release_start, peak_idx), offset - 1)

    attack = Segment(onset, peak_idx)
    sustain = Segment(peak_idx, release_start)
    release = Segment(release_start, offset)
    return Segments(attack, sustain, release, sr)
