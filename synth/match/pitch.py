"""Voice-aware pitch: follow one pitch frame by frame, and say how steady and how sure it is.

Why (Tyler, 2026-09-28: "this has to work with recorded sounds like vocal sounds"): the chord
detector (:func:`analyze.detect_notes`) sums each note's partials over about a second of sound. A
sung note wavers (vibrato, drift, a scoop at the start), so over a second its partials smear across
the neighbouring semitones and the peel reads one sung note as a cluster ("C#3+D3+D#3"), which the
synth then plays as a chord. Here the pitch is followed in 10 ms steps, where a voice is one steady
harmonic series:

1. **The track.** YIN (difference function, then the cumulative mean normalized difference) on
   23 ms windows every 10 ms, from 50 to 2500 Hz (a YIN capped at 900 Hz halves a whistle), on the
   band from 30 Hz (so a sub-oscillator under 50 Hz still shows that the sound repeats more slowly
   than any pitch followed). The period is the first dip under 0.15, else the first dip within 0.1
   of the deepest one.
2. **The octave.** At each frame's pitch, a pitch-synchronous look at the harmonics: a Hann window
   of an even number of periods puts every other partial on a null, so each harmonic and each
   half-way point (an odd multiple of f0/2) is read on its own. If the harmonics off every 2nd (or
   3rd) carry almost nothing, YIN took two (three) periods for one and the pitch is that much
   higher; if the half-way points carry real partials, it is an octave lower.
3. **One series or more.** A frame is one note when it is periodic, its fundamental is there, and
   its harmonics hold most of its energy. The fundamental matters: a chord repeats at a common
   sub-octave (C3+G3 every C2 period), but nothing sounds at that sub-octave.
4. **The take.** A pitch is heard when such frames hold some of its loud energy, and the take is
   monophonic when they hold most of it. Frames an octave, two or a twelfth off their neighbours
   are mended. The pitch is the median of the frames (each within 10 dB of the loudest counts the
   same), its wobble their half-range; fitting the S-1's LFO waves to the track tells the part of
   the wobble no regular LFO follows, and a square LFO's trill (two pitches, unequal time on each,
   where the median lands on one side) is placed at the wave's centre.

:func:`detect` is the cold-start note set the matcher uses (``twin_session.detect``): one note for a
sound whose pitch it follows; otherwise the harmonic-salience chord detector, with neighbouring
semitones merged into the strongest (:func:`analyze.merge_neighbours`), so it never returns a cluster
of neighbours.

Pure numpy, hardware-free.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

FMIN = 50.0                # Hz: the lowest pitch followed (a low bass)
FMAX = 2500.0              # Hz: the highest (a whistle)
HOP_S = 0.010              # a frame every 10 ms
INTEGRATE_S = 0.023        # YIN's integration window (at least one period at FMIN)
BAND_HZ = (30.0, 5000.0)   # the band the pitch is followed in and the harmonics weighed in
MAX_HARMONICS = 40         # the harmonics read at each frame's pitch
LOOK_S = 0.030             # the pitch-synchronous window: an even number of periods, about this long,
LOOK_PERIODS = (4, 12)     # ...and at least 4, at most 12 (a gliding high pitch stays in the main lobe)
YIN_DIP = 0.15             # the period: the first dip of the CMNDF under this...
YIN_NEAR = 0.10            # ...else the first dip within this of the deepest one
VOICED_AP = 0.40           # a frame is periodic when its CMNDF at the period is under this
LOUD_DB = 30.0             # frames within this of the loudest one count
SPARSE = 0.12              # the harmonics off every 2nd (3rd) hold less than this share: 2 (3) x the pitch
HALF_MIN = 0.5             # the half-way points hold this much of the harmonics' power: an octave lower
H1_DB = 30.0               # the fundamental is there: within this of the strongest harmonic...
H1_OVER = 4.0              # ...and 4 times (6 dB) the power at the half-way points beside it
SERIES_MIN = 0.5           # a frame is one note when its harmonics hold at least half its power
MONO_MIN = 0.6             # a take is monophonic when such frames hold this share of its loud power
HEARD_MIN = 0.15           # a take whose one-note frames hold less than this: no pitch was heard
EVEN_DB = 10.0             # frames within this of the loudest count the same in the take's pitch
LFO_RATES = np.geomspace(0.3, 15.0, 56)   # Hz: the LFO rates the regular-wobble fit tries
OCTAVE_SLOP = 150.0        # cents: a frame this near a whole jump off its neighbours is a frame error
NEIGHBOURS = 3             # frames each side a frame's pitch is checked against
JUMPS = (1200.0, 2400.0, 1901.96)   # cents: an octave, two, a twelfth (2, 4 or 3 periods taken for one)
OUTLIER = 700.0            # cents: a frame this far from its neighbours, and no such jump, is left out
REGULAR_WOBBLE = 30.0      # cents: a trill: a wobble this wide that a square LFO follows...
REGULAR_SHARE = 0.35       # ...leaving less than this share of it
VIBRATO_MIN = 8.0          # cents: a wobble narrower than this has no vibrato rate worth saying
TRIANGLE, SQUARE, SAW = 0, 1, 2     # the LFO waves regular_fit tries (its returned wave)
PHANTOM_GAP = 11           # semitones: a "pitch" this far under all of a chord's notes is its sub-octave


@dataclass
class Track:
    """The pitch frame by frame: ``times`` (s, frame centres), ``f0`` (Hz, NaN where no period was
    found), ``aperiodicity`` (the CMNDF at the period: 0 is perfectly periodic), ``power`` (the
    frame's mean square in the band), ``series`` (the share of that power in the harmonics), and
    the frame flags ``voiced`` (periodic and loud), ``fundamental`` (a partial at f0) and ``mono``
    (one harmonic series: all three, and the harmonics hold most of the power); ``harmonics`` the
    power of each harmonic of a frame with a pitch (None without one)."""

    times: np.ndarray
    f0: np.ndarray
    aperiodicity: np.ndarray
    power: np.ndarray
    series: np.ndarray
    voiced: np.ndarray
    fundamental: np.ndarray
    mono: np.ndarray
    harmonics: list


@dataclass
class Pitch:
    """What the take's pitch is, and how sure and how steady.

    ``note`` the nearest MIDI note to the middle of the one-note frames (None when no pitch was
    heard); ``cents`` how far the middle sits from that note (-50..50); ``f0`` the middle in Hz;
    ``wobble`` the half-range of the pitch (cents: 5th to 95th percentile); ``irregular`` the
    half-range of what is left after the best regular LFO wave (the S-1's triangle, square or saw,
    any rate 0.3 to 15 Hz), with that wave's ``vibrato_hz``; ``confidence`` 0..1; ``monophonic``:
    one harmonic series holds most of the take's energy; ``voiced`` the share of the loud power in
    periodic frames, ``mono_share`` in one-note frames; ``breath_db`` the power outside the
    harmonics against the power in them, over the periodic frames (dB; None without any)."""

    note: int | None
    cents: float | None
    f0: float | None
    wobble: float | None
    irregular: float | None
    vibrato_hz: float | None
    confidence: float
    monophonic: bool
    voiced: float
    mono_share: float
    breath_db: float | None
    track: Track


# ── helpers ───────────────────────────────────────────────────────────────────
def band_limit(x: np.ndarray, sr: int, lo: float = BAND_HZ[0], hi: float = BAND_HZ[1]) -> np.ndarray:
    """``x`` with the band [lo, hi] kept (an FFT mask with 20% cosine skirts), mean removed."""
    x = np.asarray(x, dtype=np.float64)
    if x.size < 4:
        return x - (float(x.mean()) if x.size else 0.0)
    n = 1 << int(math.ceil(math.log2(x.size)))
    spec = np.fft.rfft(x - x.mean(), n)
    f = np.fft.rfftfreq(n, 1.0 / sr)
    gain = np.ones_like(f)
    lo_a, hi_a = lo * 0.8, min(hi * 1.2, sr / 2)
    gain[f < lo_a] = 0.0
    rise = (f >= lo_a) & (f < lo)
    gain[rise] = 0.5 - 0.5 * np.cos(np.pi * (f[rise] - lo_a) / max(lo - lo_a, 1e-9))
    fall = (f > hi) & (f <= hi_a)
    gain[fall] = 0.5 + 0.5 * np.cos(np.pi * (f[fall] - hi) / max(hi_a - hi, 1e-9))
    gain[f > hi_a] = 0.0
    return np.fft.irfft(spec * gain, n)[: x.size]


def _cmndf_frames(x: np.ndarray, sr: int, hop: int) -> tuple[np.ndarray, np.ndarray, int, int, int]:
    """YIN's CMNDF for every frame: (cmndf [frames, lags], frame starts [samples], the integration
    window w, tau_min, tau_max). A frame compares x[s:s+w] with x[s+tau:s+tau+w]. Vectorized: the
    frame energies by cumulative sums, the cross term by FFT."""
    tau_min = max(2, int(math.floor(sr / FMAX)))
    tau_max = int(math.ceil(sr / FMIN))
    w = max(int(round(INTEGRATE_S * sr)), tau_max)
    length = w + tau_max + 2
    if x.size < length:
        return np.zeros((0, tau_max + 2)), np.zeros(0, dtype=int), w, tau_min, tau_max
    starts = np.arange(0, x.size - length + 1, hop)
    frames = x[starts[:, None] + np.arange(length)[None, :]]
    cs = np.concatenate([np.zeros((len(starts), 1)), np.cumsum(frames * frames, axis=1)], axis=1)
    taus = np.arange(tau_max + 2)
    e0 = cs[:, w][:, None]
    e_tau = cs[:, taus + w] - cs[:, taus]
    nfft = 1 << int(math.ceil(math.log2(length)))
    a = np.fft.rfft(frames[:, :w], nfft)
    b = np.fft.rfft(frames, nfft)
    r = np.fft.irfft(np.conj(a) * b, nfft)[:, : tau_max + 2]
    d = np.maximum(e0 + e_tau - 2.0 * r, 0.0)
    d[:, 0] = 0.0
    run = np.cumsum(d[:, 1:], axis=1)
    cm = np.ones_like(d)
    cm[:, 1:] = d[:, 1:] * taus[1:][None, :] / np.maximum(run, 1e-20)
    return cm, starts, w, tau_min, tau_max


def _pick(cm: np.ndarray, tau_min: int, tau_max: int) -> tuple[float, float]:
    """One frame's period (fractional lag) and its CMNDF value: the first dip under YIN_DIP, else
    the first dip within YIN_NEAR of the deepest; parabolic refinement. (nan, 1.0) if none."""
    seg = cm[tau_min: tau_max + 1]
    if seg.size < 3 or not np.all(np.isfinite(seg)):
        return math.nan, 1.0
    under = np.flatnonzero(seg < YIN_DIP)
    if under.size:
        i = int(under[0])
        while i + 1 < seg.size and seg[i + 1] < seg[i]:
            i += 1
    else:
        inner = (seg[1:-1] <= seg[:-2]) & (seg[1:-1] <= seg[2:])
        dips = np.flatnonzero(inner) + 1
        if dips.size == 0:
            return math.nan, 1.0
        floor = float(seg[dips].min())
        i = int(dips[np.argmax(seg[dips] <= floor + YIN_NEAR)])
    tau = i + tau_min
    value = float(cm[tau])
    if 0 < tau < cm.size - 1:
        a, b, c = cm[tau - 1], cm[tau], cm[tau + 1]
        den = a + c - 2.0 * b
        if abs(den) > 1e-12:
            shift = 0.5 * (a - c) / den
            if abs(shift) < 1.0:
                return tau + shift, value
    return float(tau), value


def _look(x: np.ndarray, centre: int, f0: float, sr: int,
          hi: float) -> tuple[np.ndarray, np.ndarray, float] | None:
    """A pitch-synchronous look at ``f0`` around ``centre``: the power of each harmonic (1..H), of
    each half-way point ((h - 1/2) f0), and the window's mean square. The Hann window spans an even
    number of periods n (LOOK_PERIODS, about LOOK_S), so in its DFT harmonic h is bin h*n, half-way
    point h - 1/2 is bin (h - 1/2)*n, and every other one of them sits on a null of the window.
    None when the window does not fit in ``x``."""
    per = sr / f0
    n_per = max(LOOK_PERIODS[0], min(LOOK_PERIODS[1], 2 * int(round(LOOK_S * f0 / 2.0))))
    m = int(round(n_per * per))
    if m < 8 or m > x.size:
        return None
    a = min(max(0, centre - m // 2), x.size - m)
    seg = x[a:a + m]
    win = 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(m) / m)
    wsum = float(win.sum())
    h_count = int(max(1, min(MAX_HARMONICS, hi // f0, (m // 2) // n_per)))
    spec = np.fft.rfft(win * seg)
    hs = np.arange(1, h_count + 1)
    power = 0.5 * (2.0 * np.abs(spec) / wsum) ** 2
    total = float(np.dot(win, seg * seg) / wsum)
    return power[hs * n_per], power[(2 * hs - 1) * n_per // 2], total


def _octave(x: np.ndarray, centre: int, f0: float, sr: int, hi: float):
    """Settle the octave of a frame's pitch by its harmonics (see the module docs). Returns
    (f0, harmonic powers, half-way powers, mean square) or None."""
    tried: set[int] = set()
    looked = None
    for _ in range(3):
        tried.add(int(round(f0 * 1000)))
        looked = _look(x, centre, f0, sr, hi)
        if looked is None:
            return None
        harm, half, _total = looked
        whole = float(harm.sum())
        up = None
        for k in (2, 3):             # YIN took k periods for one: the harmonics off every k-th are empty
            off_k = whole - float(harm[k - 1::k].sum())
            if harm.size >= k and off_k < SPARSE * whole and f0 * k <= FMAX:
                up = f0 * k
                break
        if up is not None and int(round(up * 1000)) not in tried:
            f0 = up
            continue
        down = f0 / 2.0
        if (float(half.sum()) > HALF_MIN * float(harm.sum()) and down >= FMIN
                and int(round(down * 1000)) not in tried):
            f0 = down
            continue
        break
    harm, half, total = looked
    return f0, harm, half, total


# ── the track ─────────────────────────────────────────────────────────────────
def track(samples: np.ndarray, sr: int) -> Track:
    """The pitch of ``samples`` (mono) frame by frame (see the module docs)."""
    x = band_limit(samples, sr)
    hop = max(1, int(round(HOP_S * sr)))
    hi = min(BAND_HZ[1], 0.45 * sr)
    cm, starts, w, tau_min, tau_max = _cmndf_frames(x, sr, hop)
    n = len(starts)
    f0 = np.full(n, np.nan)
    ap = np.ones(n)
    power = np.zeros(n)
    series = np.zeros(n)
    fund = np.zeros(n, dtype=bool)
    harmonics: list = [None] * n
    centres = starts + (w + tau_max) / 2.0
    for i in range(n):
        lag, value = _pick(cm[i], tau_min, tau_max)
        span = tau_max if not math.isfinite(lag) else lag
        s0 = int(starts[i])
        seg = x[s0: s0 + w + int(round(span))]            # the samples this frame compares
        power[i] = float(np.mean(seg * seg)) if seg.size else 0.0
        if not math.isfinite(lag) or lag <= 0:
            continue
        centres[i] = s0 + (w + lag) / 2.0
        settled = _octave(x, int(round(centres[i])), sr / lag, sr, hi)
        if settled is None:
            continue
        f, harm, half, total = settled
        f0[i], ap[i], harmonics[i] = f, value, harm
        if abs(f * lag / sr - 1.0) > 0.01:        # the octave moved: read the CMNDF at the new period
            ap[i] = float(np.interp(sr / f, np.arange(cm.shape[1]), cm[i]))
        series[i] = min(1.0, float(harm.sum()) / total) if total > 0 else 0.0
        top = float(harm.max()) if harm.size else 0.0
        beside = float(np.mean(half[:2])) if half.size else 0.0
        fund[i] = bool(harm.size and harm[0] >= top * 10 ** (-H1_DB / 10) and harm[0] >= H1_OVER * beside)
    loud = (power > 0) & (power >= (power.max() if n else 0.0) * 10 ** (-LOUD_DB / 10))
    voiced = loud & np.isfinite(f0) & (ap < VOICED_AP)
    mono = voiced & fund & (series >= SERIES_MIN)
    return Track(times=centres / float(sr), f0=f0, aperiodicity=ap, power=power, series=series,
                 voiced=voiced, fundamental=fund, mono=mono, harmonics=harmonics)


def _lfo_shapes(phase: np.ndarray) -> list[np.ndarray]:
    """The S-1's regular LFO waves as the twin draws them (twin._lfo_wave: a triangle and a square
    of 9 odd harmonics, a saw of 11; the inverse saw is the saw with a negative depth), at ``phase``
    (radians; any shape, the waves are taken along it)."""
    sines = {h: np.sin(h * phase) for h in range(1, 18)}
    tri = sum(((-1.0) ** j / (h * h)) * sines[h] for j, h in enumerate(range(1, 18, 2)))
    sq = sum(sines[h] / h for h in range(1, 18, 2))
    saw = sum(((-1.0) ** (h + 1) / h) * sines[h] for h in range(1, 12))
    return [(8.0 / math.pi ** 2) * tri, (4.0 / math.pi) * sq, (2.0 / math.pi) * saw]


def smooth_track(times: np.ndarray, cents: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """A pitch track (cents) with its frame errors mended: a frame an octave, two octaves or a
    twelfth off the median of its NEIGHBOURS each side (a period taken twice or three times) is
    moved back; one otherwise more than OUTLIER off them is left out (NaN). A glide moves
    frame by frame, so it is left alone. Returns (times, cents) in time order."""
    order = np.argsort(times)
    t = np.asarray(times, dtype=np.float64)[order]
    c = np.asarray(cents, dtype=np.float64)[order].copy()
    out = c.copy()
    for i in range(c.size):
        near = np.concatenate([c[max(0, i - NEIGHBOURS):i], c[i + 1:i + 1 + NEIGHBOURS]])
        if near.size == 0:
            continue
        d = c[i] - float(np.median(near))
        if abs(d) <= OCTAVE_SLOP:
            continue
        for jump in JUMPS:
            if abs(abs(d) - jump) <= OCTAVE_SLOP:
                out[i] = c[i] - math.copysign(jump, d)
                break
        else:
            if abs(d) > OUTLIER:
                out[i] = math.nan
    return t, out


def weighted_quantile(values: np.ndarray, weights: np.ndarray, q: float | list[float]) -> np.ndarray:
    """Quantiles of ``values`` where each counts as much as its weight (the middle of each weight)."""
    v = np.asarray(values, dtype=np.float64)
    w = np.maximum(np.asarray(weights, dtype=np.float64), 0.0)
    order = np.argsort(v)
    v, w = v[order], w[order]
    total = float(w.sum())
    if v.size == 0:
        return np.full(np.shape(q), np.nan)
    if total <= 0:
        return np.quantile(v, q)
    at = (np.cumsum(w) - 0.5 * w) / total
    return np.interp(q, at, v)


def regular_fit(times: np.ndarray, cents: np.ndarray,
                weights: np.ndarray | None = None) -> tuple[float, float | None, float, int | None]:
    """How much of a pitch track a regular LFO can follow: one of the S-1's waves (triangle, square,
    saw) at one rate (LFO_RATES), one depth and any phase, around one pitch, fitted by (weighted)
    least squares to the frames between the 2.5th and 97.5th percentiles. Returns (the half-range of
    what is left, in cents: 5th to 95th percentile, never more than the track's own; the
    best wave's rate in Hz, or None when a steady pitch does as well; the fit's centre, in the
    track's cents; the wave: TRIANGLE, SQUARE or SAW, or None)."""
    t = np.asarray(times, dtype=np.float64)
    c = np.asarray(cents, dtype=np.float64)
    w = np.ones_like(c) if weights is None else np.maximum(np.asarray(weights, dtype=np.float64), 0.0)
    if c.size < 4 or float(w.sum()) <= 0:
        return 0.0, None, float(np.mean(c)) if c.size else math.nan, None
    # the few frames past the 2.5th and 97.5th percentiles (a stray frame) do not steer the fit
    lo, hi = weighted_quantile(c, w, [0.025, 0.975])
    inside = (c >= lo) & (c <= hi)
    if inside.sum() >= 4:
        t, c, w = t[inside], c[inside], w[inside]
    w = w / float(w.sum())
    mean = float(np.dot(w, c))
    base = c - mean

    def spread(res: np.ndarray) -> float:
        lo, hi = weighted_quantile(res, w, [0.05, 0.95])
        return float(hi - lo) / 2.0

    total = float(np.dot(w, base * base))
    best_err, best = total, None

    def trial(rates: np.ndarray, offsets: np.ndarray,
              kinds: tuple[int, ...] = (TRIANGLE, SQUARE, SAW)) -> None:
        """Try every wave in ``kinds`` at each rate and phase offset; keep the best fit so far."""
        nonlocal best_err, best
        for rate in rates:
            waves = _lfo_shapes(2.0 * np.pi * rate * t[None, :] + offsets[:, None])
            for kind in kinds:
                shape = waves[kind]
                s = shape - (shape @ w)[:, None]              # each offset's wave, centred
                ss = (s * s) @ w
                amp = np.where(ss > 1e-9, (s * base[None, :]) @ w / np.maximum(ss, 1e-9), 0.0)
                err = total - amp * amp * ss
                j = int(np.argmin(err))
                if err[j] < best_err - 1e-9:
                    best_err = float(err[j])
                    best = (float(rate), base - amp[j] * s[j], mean - amp[j] * float(shape[j] @ w), kind,
                            float(offsets[j]))

    step = 2.0 * np.pi / 16
    trial(LFO_RATES, np.arange(16) * step)
    if best is not None:                                  # then finer, around the best
        ratio = float(LFO_RATES[1] / LFO_RATES[0])
        trial(best[0] * ratio ** np.linspace(-1.0, 1.0, 13), best[4] + np.linspace(-step, step, 13),
              (best[3],))
    if best is None:
        return spread(base), None, mean, None
    rate, res, centre, kind, _offset = best
    return min(spread(res), spread(base)), rate, centre, kind


def analyze(samples: np.ndarray, sr: int) -> Pitch:
    """The take's pitch: its note and cents, wobble, confidence and whether it is one note."""
    tr = track(samples, sr)
    top = float(tr.power.max()) if tr.power.size else 0.0
    loud_power = float(tr.power[(tr.power > 0) & (tr.power >= top * 10 ** (-LOUD_DB / 10))].sum())

    def share(mask: np.ndarray) -> float:
        return float(tr.power[mask].sum()) / loud_power if loud_power > 0 else 0.0

    voiced, mono_share = share(tr.voiced), share(tr.mono)
    breath_db = None
    if tr.voiced.any():
        pw = tr.power[tr.voiced]
        harm = float(np.dot(pw, tr.series[tr.voiced]))
        rest = float(np.dot(pw, 1.0 - tr.series[tr.voiced]))
        breath_db = 10.0 * math.log10(max(rest, 1e-12) / max(harm, 1e-12))
    empty = Pitch(note=None, cents=None, f0=None, wobble=None, irregular=None, vibrato_hz=None,
                  confidence=0.0, monophonic=False, voiced=voiced, mono_share=mono_share,
                  breath_db=breath_db, track=tr)
    if mono_share < HEARD_MIN or not tr.mono.any():
        return empty
    t, c = smooth_track(tr.times[tr.mono], 6900.0 + 1200.0 * np.log2(tr.f0[tr.mono] / 440.0))
    keep = np.isfinite(c)
    t, c = t[keep], c[keep]
    # every frame within EVEN_DB of the loudest counts the same; quieter ones (a tail, a faint
    # pre-sound) count less
    pw = np.minimum(1.0, tr.power[tr.mono][keep] / (10 ** (-EVEN_DB / 10) * top))
    mid = float(weighted_quantile(c, pw, 0.5))
    p5, p95 = weighted_quantile(c, pw, [0.05, 0.95])
    wobble = float(p95 - p5) / 2.0
    irregular, rate, centre, shape = regular_fit(t, c, pw)
    # The middle: the weighted median, unless a square LFO explains the wobble (a trill between two
    # pitches, from the S-1's own square LFO: the median lands on one side; the note is the centre).
    trill = shape == SQUARE and wobble >= REGULAR_WOBBLE and irregular <= REGULAR_SHARE * wobble
    middle = centre if trill else mid
    note = int(round(middle / 100.0))
    clarity = float(np.dot(tr.power[tr.mono], 1.0 - tr.aperiodicity[tr.mono]) / tr.power[tr.mono].sum())
    confidence = float(np.clip(mono_share * clarity, 0.0, 1.0))
    return Pitch(note=note, cents=middle - 100.0 * note, f0=440.0 * 2.0 ** ((middle - 6900.0) / 1200.0),
                 wobble=wobble, irregular=irregular, vibrato_hz=rate if wobble >= VIBRATO_MIN else None,
                 confidence=confidence, monophonic=mono_share >= MONO_MIN, voiced=voiced,
                 mono_share=mono_share, breath_db=breath_db, track=tr)


# ── the note set the matcher starts from ──────────────────────────────────────
@dataclass
class Detection:
    """The cold-start note set: ``notes`` low to high; ``ranked`` the candidates the note-search
    draws from, strongest first; ``pitch`` what the pitch tracker heard (:class:`Pitch`)."""

    notes: list[int]
    ranked: list[int]
    pitch: Pitch


def detect(samples: np.ndarray, sr: int, max_notes: int = 4) -> Detection:
    """The notes in a take, for a matcher that starts cold.

    A sound whose pitch the tracker follows (a voice, a whistle, one synth note) is ONE note: the
    middle of its pitch, however it wavers; the note-search then tries it an octave up and down.
    That holds for a rough or breathy voice too, where the tracker is unsure (its ``confidence``
    says how unsure): its noise would give the chord detector notes of its own. Anything else (a
    chord, or no pitch followed at all) goes to the harmonic-salience chord detector
    (:func:`analyze.detect_notes`), with neighbouring semitones merged into the strongest; so does a
    "pitch" an octave or more under every note that detector finds (a clipped chord's difference
    tones at its common sub-octave)."""
    from .analyze import _harmonic_salience_notes, detect_notes, merge_neighbours
    from .capture import AudioClip

    x = np.asarray(samples, dtype=np.float64)
    heard = analyze(x, sr)
    clip = AudioClip(x.astype(np.float32), sr)
    notes = detect_notes(clip, max_notes=max_notes)
    # A chord repeats at its notes' common sub-octave. Nothing sounds there, so a chord is not one
    # note, unless distortion (a clipped take) puts difference tones there: then the chord detector
    # finds real notes, all an octave or more above the pitch followed, none near it.
    phantom = heard.note is not None and len(notes) >= 2 and min(notes) - heard.note >= PHANTOM_GAP
    if heard.note is not None and not phantom:
        return Detection([heard.note], [heard.note], heard)
    ranked = [n for n, _sal in merge_neighbours(_harmonic_salience_notes(clip, max_notes=max_notes + 1))]
    return Detection(sorted(notes), ranked or list(notes), heard)
