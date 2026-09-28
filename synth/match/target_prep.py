"""Find the main sound in a take and crop to it: the target the matcher gets.

A take holds more than the sound. A recording starts, and two seconds later the note comes; room
noise runs under it; a click can come before it, and the click of Stop after it. The matcher
compares its candidate with the target from the target's first sample, so the target must start
where the sound starts and end where it ends. :func:`prepare_target` finds that from the audio
alone, whatever surrounds the sound:

1. **The level.** A 10 ms RMS envelope. The noise floor is its quietest 100 ms (a recorder's
   warm-up zeros left out: they are not the room). The threshold sits well above
   the floor, or at most 45 dB under the loudest frame for a clean file, and never within 10 dB of
   the loudest frame.
2. **The main sound.** The most energetic stretch above the threshold, with gaps shorter than
   150 ms merged (a tremolo dip, a stutter). A short blip set apart at either end (a click) is
   dropped. Another sound in the take is left out, with a warning. A take with no sound in it is
   refused in plain words: silence, only a click, or only the room (steady noise; or noise that
   swells, is not pitched, and has the room's own spectral shape). A steady pitched sound that
   fills the whole take is kept whole.
3. **The edges.** The onset is the first millisecond that clearly rises over the floor; 5 ms
   before it are kept (faded in). The end is the last frame over the threshold, plus the tail down
   to the floor (at most 300 ms), plus a 20 ms fade. At most 4 s are kept. A take that ends while
   the sound still falls keeps its end.
4. **The key-up.** After the sustain plateau, the time the envelope starts falling for good: a
   plateau-then-fall fit on the smoothed envelope. None when there is no plateau (a pluck that
   dies away before any release) or no fall (the take ends while the key is held).

Pure numpy, except :func:`native_take` (the upload's own samples, for the clipping check:
resampling to the twin's rate smooths a clipped top out of sight).
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .twin_session import UploadError

FRAME_S = 0.010          # the envelope's frames (10 ms RMS)
FLOOR_SPAN = 10          # the noise floor: the quietest stretch of this many frames (100 ms)
SILENT_RMS = 1e-6        # a frame under this is digital silence (-120 dBFS)
OVER_FLOOR_DB = 12.0     # the threshold: this far over the floor...
UNDER_TOP_DB = 45.0      # ...or at most this far under the loudest frame (a clean file)...
NEAR_TOP_DB = 10.0       # ...and never nearer the loudest frame than this
FOOT_DB = 6.0            # the sound's foot: the envelope this far over the floor
MERGE_FRAMES = 15        # gaps shorter than 150 ms inside one sound are merged
BLIP_FRAMES = 3          # a run of 30 ms or less, set apart by 30 ms or more, is a click
MIN_SOUND_FRAMES = 3     # a main sound shorter than this is only a click
OTHER_FRAMES = 5         # another sound worth a warning lasts 50 ms (or half the main one, if shorter)...
OTHER_DB = 10.0          # ...and comes within this of the main sound's loudest frame
PRE_S = 0.005            # kept before the onset
FADE_S = 0.020           # the fade at the end
MAX_LEAD_S = 0.300       # the onset walks back along the foot at most this far
MAX_TAIL_S = 0.300       # the tail past the last frame over the threshold: at most this
MAX_KEEP_S = 4.0         # at most this much sound is kept
CLEAN_FLOOR_DB = -90.0   # a floor under this is a clean file's (16-bit silence is -96 dB), not a room
MATCH_TAIL_S = 2.5       # the matcher's copy follows the tail down to the foot (the room's floor), past
                         # the display's MAX_TAIL_S, at most this far past the onset and never into the next
                         # sound: a clean release rings on long after its last loud frame, and cut there it
                         # ended in a cliff the twin never makes (the suite's "sub": 45% cut, 78% whole);
                         # a recording's tail meets its room within a frame or two, so it ends as before
NO_SOUND_DB = 6.0        # nothing stands out: the loudest frame is less than this over the floor
FAINT_DB = 15.0          # the sound barely stands out: less than this over the floor
QUIET_DBFS = -40.0       # a sound peaking under this is very quiet
TONAL = 0.5              # a steady take is a held tone when this share of its power sits in spectral peaks
NOISY_EVENT = 0.2        # a "sound" with less than this in peaks, shaped like the room, is the room
SAME_SHAPE_DB = 3.0      # two spectra this close in shape (mean third-octave difference) are the same noise
CLIP_LEVEL = 0.99        # clipping: flat runs at this much of full scale or more...
CLIP_SHARE = 0.001       # ...making up at least this share of the sound's samples
# the key-up fit (on the envelope smoothed over 50 ms)
SMOOTH_FRAMES = 5
FIT_FRAMES = 15          # each side of the hinge: 150 ms
PLATEAU_FRAMES = 10      # the plateau lasts at least 100 ms
PLATEAU_SLOPE = 12.0     # dB/s: the plateau falls no faster than this...
FALL_SLOPE = 15.0        # ...the fall at least this fast...
KINK = 10.0              # ...and at least this much faster than the plateau
FALL_DB = 8.0            # the fall goes at least this far under the plateau
RISE_BACK_DB = 1.5       # after the key-up, nothing climbs back over the plateau by more than this
PLATEAU_SPAN_DB = 40.0   # a sustain plateau is part of the note: within this of its peak (a 16-bit tail's
                         # hiss, -96 dB, then digital silence is a clean "plateau then fall" too)

NO_SOUND = ("No clear sound was found in that audio, only a click or steady noise. "
            "Record the sound again, louder or closer, or choose another file.")
QUIET = "The sound is very quiet. It can still be matched, but a louder take matches better."
CLIPPED = ("The sound is clipped: it was too loud when it was recorded. "
           "A take that is not clipped matches better.")
MORE = ("The take holds more than one sound, so the loudest one is used. "
        "Record one sound at a time to match another.")
FAINT = ("The sound barely stands out from the background noise, so the crop may be off. "
         "A quieter room or a closer take works better.")
LONG = f"The sound is longer than {MAX_KEEP_S:g} s, so its first {MAX_KEEP_S:g} s are used."


@dataclass
class Prepared:
    """The main sound of a take, cropped: what the matcher gets.

    ``samples`` the crop (faded in over the kept lead, faded out over the last 20 ms); ``t0``/``t1``
    where it lies in the take (seconds); ``onset`` where the sound starts (seconds in the take, about
    5 ms after ``t0``); ``gate_s`` how long the key was held, in seconds after the onset (None when
    it cannot be seen); ``noise_db`` the floor and ``peak_db`` the crop's peak (dBFS); ``warnings``
    plain sentences; ``duration`` the whole take (seconds)."""

    samples: np.ndarray
    t0: float
    t1: float
    gate_s: float | None
    noise_db: float
    peak_db: float
    warnings: list[str] = field(default_factory=list)
    onset: float = 0.0
    duration: float = 0.0


# ── the envelope ──────────────────────────────────────────────────────────────
def _db(power: Any) -> Any:
    return 10.0 * np.log10(np.maximum(power, 1e-14))


def frame_power(x: np.ndarray, hop: int) -> np.ndarray:
    """Mean square of each whole ``hop``-sample frame (a partial last frame is left out)."""
    n = len(x) // hop
    if n == 0:
        return np.zeros(0)
    fr = np.asarray(x[: n * hop], dtype=np.float64).reshape(n, hop)
    return np.mean(fr * fr, axis=1)


def noise_floor(power: np.ndarray, span: int = FLOOR_SPAN) -> tuple[float, int]:
    """The quietest ``span`` frames in a row: (mean power, first frame). A recorder warming up (exact
    zeros at the start, then the room: 100 ms of audio 20 dB or more under the loudest frame) is left
    out: its zeros are not the room. Digital silence before the sound itself is a clean file's
    floor. Fewer frames than ``span``: the quietest one."""
    alive = np.ones(len(power), dtype=bool)
    silent = power <= SILENT_RMS ** 2
    lead = int(np.argmin(silent)) if not silent.all() else len(power)
    after = power[lead:lead + span]
    if (0 < lead and after.size == span and not np.any(after <= SILENT_RMS ** 2)
            and float(_db(after.mean())) < float(_db(power.max())) - 20.0):
        alive[:lead] = False
    if len(power) >= span:
        full = np.convolve(alive.astype(np.float64), np.ones(span), mode="valid") >= span - 0.5
        avg = np.convolve(power, np.ones(span) / span, mode="valid")
        if full.any():
            i = int(np.argmin(np.where(full, avg, np.inf)))
            return float(avg[i]), i
    i = int(np.argmin(np.where(alive, power, np.inf)))
    return max(float(power[i]), SILENT_RMS ** 2), i


def threshold(floor_db: float, top_db: float) -> float:
    """The level a frame must pass to be part of a sound: ``OVER_FLOOR_DB`` over the floor, or at most
    ``UNDER_TOP_DB`` under the loudest frame (a clean file), never nearer the top than
    ``NEAR_TOP_DB``, and always at least halfway from the floor to the top (a faint sound)."""
    thr = min(max(floor_db + OVER_FLOOR_DB, top_db - UNDER_TOP_DB), top_db - NEAR_TOP_DB)
    if math.isfinite(floor_db):
        thr = max(thr, floor_db + min(OVER_FLOOR_DB, (top_db - floor_db) / 2))
    return thr


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous True stretches of ``mask`` as [start, stop) pairs."""
    m = np.concatenate([[False], np.asarray(mask, dtype=bool), [False]])
    edges = np.flatnonzero(np.diff(m.astype(np.int8)))
    return [(int(a), int(b)) for a, b in zip(edges[::2], edges[1::2])]


def _groups(runs: list[tuple[int, int]], gap: int) -> list[list[tuple[int, int]]]:
    """Runs whose gaps are shorter than ``gap`` frames, merged into one sound each."""
    out: list[list[tuple[int, int]]] = []
    for run in runs:
        if out and run[0] - out[-1][-1][1] < gap:
            out[-1].append(run)
        else:
            out.append([run])
    return out


def _strip_blips(group: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Drop short runs set apart at either end of a sound (a click before it, the click of Stop)."""
    g = list(group)
    while len(g) > 1 and g[0][1] - g[0][0] <= BLIP_FRAMES and g[1][0] - g[0][1] >= BLIP_FRAMES:
        g.pop(0)
    while len(g) > 1 and g[-1][1] - g[-1][0] <= BLIP_FRAMES and g[-1][0] - g[-2][1] >= BLIP_FRAMES:
        g.pop()
    return g


def _spectrum(x: np.ndarray, n_fft: int) -> np.ndarray:
    """The averaged power spectrum (Hann frames, 75% overlap; a take shorter than a frame is
    windowed whole, then zero-padded)."""
    x = np.asarray(x, dtype=np.float64)
    if len(x) < n_fft:
        return np.abs(np.fft.rfft(np.hanning(len(x)) * x, n_fft)) ** 2
    win = np.hanning(n_fft)
    frames = [win * x[i:i + n_fft] for i in range(0, len(x) - n_fft + 1, n_fft // 4)][:64]
    return np.mean([np.abs(np.fft.rfft(f)) ** 2 for f in frames], axis=0)


def tonal_share(x: np.ndarray, n_fft: int = 8192, span: int = 31) -> float:
    """The share of the power that sits in spectral peaks (10 dB over the local median): near 1 for
    a pitched tone, near 0 for noise of any color (white, pink, a room's rumble)."""
    from numpy.lib.stride_tricks import sliding_window_view

    p = _spectrum(x, n_fft)[4:]
    if p.size < span or p.sum() <= 0:
        return 0.0
    pad = span // 2
    med = np.median(sliding_window_view(np.pad(p, (pad, pad), mode="edge"), span), axis=1)
    return float(p[p > 10.0 * med].sum() / p.sum())


def band_shape(x: np.ndarray, sr: int, n_fft: int = 2048) -> np.ndarray:
    """The spectrum's shape without its level: third-octave band powers (60 Hz to 8 kHz) in dB,
    minus their mean."""
    p = _spectrum(x, n_fft)
    f = np.fft.rfftfreq(n_fft, 1.0 / sr)
    edges = 60.0 * 2.0 ** (np.arange(0, 22) / 3.0)
    bands = [p[(f >= lo) & (f < hi)].sum() for lo, hi in zip(edges[:-1], edges[1:]) if hi <= sr / 2]
    db = _db(np.asarray(bands) + 1e-20)
    return db - db.mean()


def clip_share(x: np.ndarray) -> float:
    """The share of samples in flat runs at full scale (two or more equal samples in a row at
    ``CLIP_LEVEL`` or more): what an input that clipped leaves. A peak-normalized sound touches
    full scale in single samples, which do not count."""
    x = np.asarray(x, dtype=np.float64)
    if len(x) < 2:
        return 0.0
    hot = np.abs(x) >= CLIP_LEVEL
    flat = hot[1:] & hot[:-1] & (np.abs(np.diff(x)) < 1e-6)
    return float(np.count_nonzero(flat)) / len(x)


# ── the key-up ────────────────────────────────────────────────────────────────
def _smooth_db(power: np.ndarray, frames: int = SMOOTH_FRAMES) -> np.ndarray:
    if len(power) >= frames > 1:
        pad = frames // 2
        padded = np.pad(power, (pad, pad), mode="edge")
        power = np.convolve(padded, np.ones(frames) / frames, mode="valid")
    return _db(power)


def _hinge(s: np.ndarray, k: float, lo: int, hi: int, fps: float) -> tuple[np.ndarray, float]:
    """Least-squares plateau-then-fall through s[lo:hi], bent at frame ``k``: (level, slope before,
    slope after) in dB and dB/s, and the mean squared residual."""
    t = (np.arange(lo, hi) - k) / fps
    a = np.column_stack([np.ones_like(t), np.minimum(t, 0.0), np.maximum(t, 0.0)])
    coef, *_ = np.linalg.lstsq(a, s[lo:hi], rcond=None)
    resid = s[lo:hi] - a @ coef
    return coef, float(np.mean(resid * resid))


def key_up(power: np.ndarray, fps: float, floor_db: float, until: float | None = None) -> float | None:
    """How long the key was held, in seconds from the first frame, or None.

    ``power`` is the frame power from the onset. After the sustain plateau (at least 100 ms, falling
    no faster than ``PLATEAU_SLOPE``), the envelope starts falling for good: much faster, at least
    ``FALL_DB`` down, and never back over the plateau. A plateau-then-fall line fitted around each
    candidate bend (150 ms each side) finds it, to a few milliseconds on a clean take. The plateau lies
    within PLATEAU_SPAN_DB of the note's peak, and the key goes up before ``until`` (seconds from the
    first frame: where the sound ends) when given."""
    s = _smooth_db(power)
    n = len(s)
    if n < PLATEAU_FRAMES + 4:
        return None
    top = float(s.max())
    first = int(np.argmax(s >= top - 6.0))             # the attack has arrived
    audible = np.flatnonzero(s > floor_db + 3.0)          # the fall, followed down to the floor
    end = int(audible[-1]) + 1 if audible.size else n

    def depth(level: float) -> float:
        # the fall goes FALL_DB down, or down to the floor's foot in a noisy take (3 dB at least)
        return max(3.0, min(FALL_DB, level - (floor_db + FOOT_DB) - 0.5))

    def window(k: int) -> tuple[int, int]:
        # 150 ms of plateau before the bend; after it, the fall only while it is a line in dB: until
        # it nears the floor, or 20 dB down (an exponential release is straight in dB until then)
        lo = max(first, k - FIT_FRAMES)
        level = float(np.median(s[lo:k + 1]))
        stop = max(level - 20.0, floor_db + FOOT_DB)
        hi = k + 1
        while hi < min(end, k + FIT_FRAMES + 1) and s[hi] > stop:
            hi += 1
        return lo, min(end, hi + 1)

    last = end - 2 if until is None else min(end - 2, int(until * fps))
    best: tuple[float, int] | None = None
    for k in range(first + PLATEAU_FRAMES, last):
        lo, hi = window(k)
        if hi - k < 3:
            continue
        (level, before, after), err = _hinge(s, k, lo, hi, fps)
        if level < top - PLATEAU_SPAN_DB:
            continue
        if before < -PLATEAU_SLOPE or after > -FALL_SLOPE or after > before - KINK:
            continue
        rest = s[k + 1:end]
        if rest.size == 0 or rest.max() > level + RISE_BACK_DB or rest.min() > level - depth(level):
            continue
        if best is None or err < best[0]:
            best = (err, k)
    if best is None:
        return None
    k0 = best[1]
    lo, hi = window(k0)
    fine = min(np.linspace(k0 - 1.0, k0 + 1.0, 41), key=lambda kf: _hinge(s, kf, lo, hi, fps)[1])
    return max(0.0, (float(fine) + 0.5) / fps)          # frame k covers [k, k+1) hops: its middle


# ── the crop ──────────────────────────────────────────────────────────────────
def _fine_onset(x: np.ndarray, lo: int, hi: int, level: float, sr: int) -> int:
    """The first sample in [lo, hi) where the trailing 1 ms RMS rises over ``level`` (else ``hi``)."""
    w = max(1, int(round(sr / 1000)))
    lo = max(0, lo)
    seg = np.asarray(x[max(0, lo - w + 1):hi], dtype=np.float64)
    if seg.size == 0:
        return hi
    c = np.concatenate([[0.0], np.cumsum(seg * seg)])
    idx = np.arange(w, len(c))
    rms = np.sqrt((c[idx] - c[idx - w]) / w)            # rms[j]: the window ending at seg[j + w - 1]
    over = np.flatnonzero(rms > level)
    if over.size == 0:
        return hi
    return max(0, lo - w + 1) + int(over[0]) + w - 1


def parse_crop(raw: Any) -> tuple[float, float] | None:
    """A crop the user chose: "2.1,3.4" or (2.1, 3.4) → (2.1, 3.4) seconds; junk → None."""
    if raw is None:
        return None
    parts = raw.split(",") if isinstance(raw, str) else list(raw) if isinstance(raw, (list, tuple)) else []
    try:
        t0, t1 = (float(p) for p in parts)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(t0) and math.isfinite(t1)) or t1 <= t0 or t1 <= 0:
        return None
    return max(0.0, t0), t1


def native_take(raw: bytes) -> tuple[np.ndarray, int] | None:
    """The upload's own samples (mono, at its own rate), or None if soundfile cannot read it."""
    try:
        import soundfile as sf

        data, sr = sf.read(io.BytesIO(raw), always_2d=True, dtype="float64")
    except Exception:  # noqa: BLE001 - the check is a courtesy; decode_upload has the final say
        return None
    return np.asarray(data, dtype=np.float64).mean(axis=1), int(sr)


@dataclass
class _Take:
    """A take's level, measured once: frame power and dB, the loudest frame, the floor."""

    x: np.ndarray
    sr: int
    hop: int
    power: np.ndarray
    env: np.ndarray
    top_db: float
    floor_db: float
    ends_sounding: bool     # the take ends while its sound still falls: its floor was never heard

    @property
    def n(self) -> int:
        return len(self.power)

    @property
    def fps(self) -> float:
        return self.sr / self.hop

    @property
    def heard_floor(self) -> float:
        return -math.inf if self.ends_sounding else self.floor_db


def _measure(samples: np.ndarray, sr: int) -> _Take:
    x = np.asarray(samples, dtype=np.float64)
    hop = max(1, int(round(sr * FRAME_S)))
    power = frame_power(x, hop)
    if power.size == 0 or not np.any(power > SILENT_RMS ** 2):
        raise UploadError(NO_SOUND)
    env = _db(power)
    floor_p, floor_at = noise_floor(power)
    n, span = len(power), FLOOR_SPAN
    # The quietest stretch is the take's end, and the level still falls into it: a sound cut off by
    # the end of the file (a truncated sample), not a room.
    drop = float(np.mean(env[n - 3 * span:n - 2 * span]) - np.mean(env[n - span:])) if n >= 3 * span else 0.0
    ends_sounding = floor_at >= n - span - 1 and drop >= 1.5
    return _Take(x, sr, hop, power, env, float(env.max()), float(_db(floor_p)), bool(ends_sounding))


def prepare_upload(raw: bytes, crop: Any = None) -> tuple[np.ndarray, Prepared]:
    """An upload's bytes → (the whole take at the twin's rate, its main sound as the matcher gets it).
    ``crop`` ("t0,t1" or a pair, seconds) is the user's own edges. :func:`twin_session.plan` and
    ``POST /api/match/prepare`` both come here, so the page shows exactly what the matcher gets.
    Raises :class:`UploadError`."""
    from . import WORKING_SR
    from .twin_session import decode_upload

    take = decode_upload(raw)
    return take, prepare_target(take, WORKING_SR, crop=parse_crop(crop), source=native_take(raw))


def prepare_target(samples: np.ndarray, sr: int, *, crop: tuple[float, float] | None = None,
                   source: tuple[np.ndarray, int] | None = None) -> Prepared:
    """Find the main sound in ``samples`` (mono, at ``sr``) and crop to it (see the module docs).

    ``crop`` (seconds in the take) overrides the found edges: the user moved them. ``source`` is the
    take at its own rate, before resampling, where clipping is judged (else ``samples`` are). Raises
    :class:`UploadError` when no sound stands out: silence, steady noise, or only a click."""
    take = _measure(samples, sr)
    warnings: list[str] = []
    if crop is not None:
        i0, i1, onset, lead, look, tail = _user_crop(take, crop)
    else:
        i0, i1, onset, lead, look, tail = _auto_crop(take, warnings)
    return _finish(take, i0, i1, onset, lead, look, tail, warnings, source)


def _user_crop(take: _Take, crop: tuple[float, float]) -> tuple[int, int, int, int, int, int]:
    """The user's edges (at least 50 ms); the onset is the first clear rise inside them."""
    x, sr, hop = take.x, take.sr, take.hop
    least = int(0.05 * sr)
    i0 = min(int(round(crop[0] * sr)), max(0, len(x) - least))
    i1 = min(len(x), max(int(round(crop[1] * sr)), i0 + least))
    foot = max(take.floor_db + FOOT_DB, take.top_db - 60.0)
    a = -(-i0 // hop)
    inside = np.flatnonzero(take.env[a:max(a + 1, i1 // hop)] > threshold(take.floor_db, take.top_db))
    onset = i0
    if inside.size:
        first = a + int(inside[0])
        early = math.sqrt(float(take.power[first:first + 3].max()))
        onset = max(i0, min(i1 - 1, _fine_onset(x, (first - 1) * hop, (first + 1) * hop,
                                                max(10 ** (foot / 20), 0.25 * early), sr)))
    return i0, i1, onset, min(int(0.002 * sr), i1 - i0), i1, i1   # a 2 ms fade-in: the cut never clicks


def _auto_crop(take: _Take, warnings: list[str]) -> tuple[int, int, int, int, int, int]:
    """Find the main sound's edges (see the module docs); add what to know to ``warnings``."""
    x, sr, hop, power, env, n, fps = take.x, take.sr, take.hop, take.power, take.env, take.n, take.fps
    top_db, floor_db = take.top_db, take.floor_db
    if top_db - floor_db < NO_SOUND_DB and not take.ends_sounding:
        # Nothing stands out. A steady pitched sound that fills the whole take is the sound; steady
        # noise (or a room with nothing in it) is not.
        if tonal_share(x) < TONAL or top_db < -60.0:
            raise UploadError(NO_SOUND)
        alive = np.flatnonzero(np.abs(x) > SILENT_RMS)
        i0 = int(alive[0]) if alive.size else 0
        return i0, len(x), i0, 0, len(x), len(x)

    thr_db = threshold(take.heard_floor, top_db)
    foot_db = max(take.heard_floor + FOOT_DB, top_db - 60.0)
    groups = _groups(_runs(env > thr_db), MERGE_FRAMES)
    energy = [sum(float(power[a:b].sum()) for a, b in g) for g in groups]
    span = [g[-1][1] - g[0][0] for g in groups]
    # the main sound: the most energetic group that is more than a click (a loud click never wins)
    pick = [i for i in range(len(groups)) if span[i] >= OTHER_FRAMES] or list(range(len(groups)))
    main = max(pick, key=lambda i: energy[i])
    group = _strip_blips(groups[main])
    first, last = group[0][0], group[-1][1]
    if last - first < MIN_SOUND_FRAMES:
        raise UploadError(NO_SOUND)
    # A room's noise can swell 10 dB or more in 10 ms frames (a rumble): a "sound" that is not
    # pitched and has the spectral shape of the room (the take between its sounds, or else all of
    # it but the main sound) is only the room.
    room = power > SILENT_RMS ** 2
    for g in groups:
        room[max(0, g[0][0] - 5):g[-1][1] + 5] = False
    if np.count_nonzero(room) < FLOOR_SPAN:
        room = power > SILENT_RMS ** 2
        room[max(0, first - 5):last + 5] = False
    if floor_db > -90.0 and np.count_nonzero(room) >= FLOOR_SPAN:
        event = x[first * hop:last * hop]
        rest = x[:n * hop].reshape(n, hop)[room].ravel()
        if (tonal_share(event) < NOISY_EVENT
                and float(np.mean(np.abs(band_shape(event, sr) - band_shape(rest, sr)))) < SAME_SHAPE_DB):
            raise UploadError(NO_SOUND)
    main_top = float(max(env[a:b].max() for a, b in group))
    other = max(MIN_SOUND_FRAMES, min(OTHER_FRAMES, (last - first) // 2))    # a click is not another sound
    if any(i != main and span[i] >= other
           and float(max(env[a:b].max() for a, b in groups[i])) >= main_top - OTHER_DB
           for i in range(len(groups))):
        warnings.append(MORE)
    if top_db - floor_db < FAINT_DB and not take.ends_sounding:
        warnings.append(FAINT)

    # the onset: walk back along the foot (not into a click or another sound), then to the millisecond:
    # the first 1 ms over the foot and over a quarter of the level the sound reaches in its first
    # frames (so a flicker of noise just before it does not count)
    before = max([b for g in groups for _a, b in g if b <= first] + [0])
    a = first
    stop = max(before, first - int(round(MAX_LEAD_S * fps)))
    while a - 1 >= stop and env[a - 1] > foot_db:
        a -= 1
    early = math.sqrt(float(power[first:first + 3].max()))
    onset = _fine_onset(x, (a - 1) * hop, (first + 1) * hop, max(10 ** (foot_db / 20), 0.25 * early), sr)
    # the end: the tail down to the foot (not into what comes next), plus the fade
    after = min([a2 for g in groups for a2, _b in g if a2 >= last] + [n])
    b = last
    stop = min(after, last + int(round(MAX_TAIL_S * fps)))
    while b < stop and env[b] > foot_db:
        b += 1
    to_end = b >= n or (take.ends_sounding and last >= n - 1)
    i1 = len(x) if to_end else min(len(x), b * hop + int(FADE_S * sr))
    i0 = max(0, onset - int(round(PRE_S * sr)))
    # the key-up fit may look past the crop, down into the floor, but not into what comes next
    look = max(i1, min(len(x), after * hop, i1 + int(round(MAX_TAIL_S * sr))))
    # the matcher's copy: a clean take's release rings on under any threshold into digital silence, and
    # the twin renders all of it, so all of it is kept (up to MATCH_TAIL_S past the onset, never into the
    # next sound); a recording's tail sinks into its room, so it goes on only while over the foot
    b2, stop2 = b, min(after, onset // hop + int(round(MATCH_TAIL_S * fps)))
    if take.heard_floor < CLEAN_FLOOR_DB:
        b2 = max(b2, stop2)
    while b2 < stop2 and env[b2] > foot_db:
        b2 += 1
    tail = len(x) if b2 >= n else min(len(x), b2 * hop + int(FADE_S * sr))
    return i0, i1, onset, onset - i0, look, max(i1, tail)


def _finish(take: _Take, i0: int, i1: int, onset: int, lead: int, look: int, tail: int, warnings: list[str],
            source: tuple[np.ndarray, int] | None) -> Prepared:
    """Cut [i0, tail): the heard sound [i0, i1) and its tail (MATCH_TAIL_S), at most MAX_KEEP_S from the
    onset; fade it in over ``lead`` samples and out
    over the last 20 ms, measure it, and say what to know. The key-up is read from the onset up to
    ``look``."""
    x, sr = take.x, take.sr
    keep = onset + int(round(MAX_KEEP_S * sr))
    if i1 > keep:
        i1 = keep
        warnings.append(LONG)
    look, tail = min(look, keep), min(max(i1, tail), keep)
    crop = np.array(x[i0:tail], dtype=np.float64)             # the tail too: see MATCH_TAIL_S
    if lead > 0:                                         # the kept lead rises from nothing
        crop[:lead] *= np.linspace(0.0, 1.0, lead, endpoint=False)
    fade = min(len(crop), int(round(FADE_S * sr)))
    if fade > 1:
        crop[-fade:] *= np.linspace(1.0, 0.0, fade)
    peak = float(np.abs(x[i0:i1]).max()) if i1 > i0 else 0.0
    peak_db = 20.0 * math.log10(max(peak, 1e-7))
    if peak_db < QUIET_DBFS:
        warnings.insert(0, QUIET)
    if source is not None:
        nat, nsr = source
        clipped = clip_share(nat[int(i0 / sr * nsr):int(i1 / sr * nsr)])
    else:
        clipped = clip_share(x[i0:i1])
    if clipped >= CLIP_SHARE:
        warnings.insert(0, CLIPPED)
    floor = take.heard_floor if math.isfinite(take.heard_floor) else -120.0
    gate = key_up(frame_power(x[onset:look], take.hop), take.fps, floor, until=max(0, i1 - onset) / sr)
    return Prepared(samples=crop, t0=i0 / sr, t1=i1 / sr, gate_s=gate,
                    noise_db=max(-120.0, take.floor_db), peak_db=max(-120.0, peak_db),
                    warnings=warnings, onset=onset / sr, duration=len(x) / sr)
