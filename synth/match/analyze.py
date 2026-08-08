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


# ── multi-pitch detection (iterative harmonic salience) ──────────────────────
# For matching CHORDS (up to 4 notes) we need the note SET, not one pitch. This
# is deliberately simpler than YIN: a harmonic-sum salience over candidate MIDI
# fundamentals, greedy-peeled one note at a time (pick the strongest, subtract
# its harmonic partials from the spectrum, repeat).

# How many harmonics a candidate's salience sums, and the per-harmonic decay.
# The decay makes a true fundamental out-score its sub-octave (whose only energy
# sits in the *even* harmonic slots, each down-weighted), killing octave errors.
_SALIENCE_HARMONICS = 16
_SALIENCE_DECAY = 0.85
# Fractional half-width of the notch that suppresses a picked note's partials
# (±3 % ≈ a half-semitone), so the next peel sees the residual spectrum.
_SUPPRESS_BW = 0.03
# A candidate only competes if its OWN fundamental bin carries real energy — at
# least this fraction of the strongest fundamental. This is the octave-error
# guard: a sub-harmonic like C3 "explaining" a C4+G4 dyad has NO energy at 130 Hz,
# so it is rejected (missing-fundamental error). Suppression handles the octave
# *above* (a picked note zeros the partials its octave-up would sit on).
_FUND_GATE_REL = 0.1
# A peeled note counts only if its salience clears this fraction of the first
# (strongest) note's salience — the "relative threshold" that stops the peel.
_SALIENCE_REL_THRESHOLD = 0.1


def _harmonic_salience_notes(
    clip: AudioClip,
    max_notes: int = 4,
    fmin: float = DEFAULT_FMIN,
    fmax: float = DEFAULT_FMAX,
) -> list[tuple[int, float]]:
    """Peel up to ``max_notes`` MIDI notes by iterative harmonic salience.

    Returns ``(midi_note, salience)`` pairs in **selection order** (strongest
    first), with NO relative-threshold cut — always as many as it can peel up to
    ``max_notes`` (so a caller can inspect ranking / an extra candidate). Empty
    for silence. This is the raw ranked engine behind :func:`detect_notes`.
    """
    x = np.asarray(clip.samples, dtype=np.float64)
    sr = clip.samplerate
    if x.size == 0 or float(np.abs(x).max()) < SILENCE_FLOOR:
        return []

    seg_len = min(x.size, sr)  # up to ~1 s of the loudest body, for f0 resolution
    seg = _analysis_window(x, sr, seg_len)
    if seg.size < 2:
        return []
    seg = seg * np.hanning(seg.size)
    n_fft = 1 << int(np.ceil(np.log2(max(2, seg.size * 2))))
    spec = np.abs(np.fft.rfft(seg, n=n_fft))
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
    nyq = float(freqs[-1])

    lo_note = int(np.ceil(69.0 + 12.0 * np.log2(fmin / 440.0)))
    hi_note = int(np.floor(69.0 + 12.0 * np.log2(min(fmax, nyq) / 440.0)))
    if hi_note < lo_note:
        return []
    cand_notes = np.arange(lo_note, hi_note + 1)
    cand_freqs = 440.0 * 2.0 ** ((cand_notes - 69) / 12.0)
    # Reference for the fundamental-presence gate: the strongest candidate
    # fundamental in the ORIGINAL spectrum (before any peeling).
    fund_ref = float(np.max(np.interp(cand_freqs, freqs, spec))) if cand_freqs.size else 0.0
    fund_gate = _FUND_GATE_REL * fund_ref

    residual = spec.copy()
    out: list[tuple[int, float]] = []
    taken: set[int] = set()
    for _ in range(int(max(1, max_notes))):
        best_note, best_sal = None, 0.0
        for note, f in zip(cand_notes, cand_freqs):
            if int(note) in taken:
                continue
            # Octave-error guard: the candidate's own fundamental must carry energy.
            if float(np.interp(f, freqs, residual)) < fund_gate:
                continue
            harm_count = int(min(_SALIENCE_HARMONICS, nyq // f))
            if harm_count < 1:
                continue
            hs = np.arange(1, harm_count + 1) * f
            mags = np.interp(hs, freqs, residual)
            weights = _SALIENCE_DECAY ** np.arange(harm_count)
            sal = float(np.dot(weights, mags))
            if sal > best_sal:
                best_note, best_sal = int(note), sal
        if best_note is None or best_sal <= 1e-9:
            break
        out.append((best_note, best_sal))
        taken.add(best_note)
        # Subtract the picked note's harmonic partials so the next peel is honest.
        f = 440.0 * 2.0 ** ((best_note - 69) / 12.0)
        for h in range(1, _SALIENCE_HARMONICS + 1):
            fh = h * f
            if fh > nyq:
                break
            residual[(freqs >= fh * (1 - _SUPPRESS_BW)) & (freqs <= fh * (1 + _SUPPRESS_BW))] = 0.0
    return out


def detect_notes(clip: AudioClip, max_notes: int = 4) -> list[int]:
    """Detect the set of MIDI notes sounding in ``clip`` (chords up to ``max_notes``).

    Iterative harmonic salience: sum each candidate fundamental's harmonic
    magnitudes, pick the strongest, subtract its partials, repeat — stopping when
    a peeled note's salience falls below :data:`_SALIENCE_REL_THRESHOLD` of the
    first note's, or ``max_notes`` is reached. Returns MIDI notes **low→high**;
    a mono (single-pitch) input yields exactly one note, so this never regresses
    the single-note path.

    Honest scope: this is reliable on **clean, steady, harmonic** material — synth
    chords with clear fundamentals, exactly what the twin renders. It degrades on
    dense voicings, inharmonic timbres, reverb, and real polyphonic mixes, where
    shared partials and missing fundamentals confuse the greedy peel. It is NOT a
    general polyphonic transcriber. Falls back to :data:`C3_FALLBACK` for silence.
    """
    ranked = _harmonic_salience_notes(clip, max_notes)
    if not ranked:
        return [C3_FALLBACK]
    top = ranked[0][1]
    kept = [ranked[0][0]]
    for note, sal in ranked[1:]:
        if sal < _SALIENCE_REL_THRESHOLD * top:
            break
        kept.append(note)
    return sorted(set(kept))


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
