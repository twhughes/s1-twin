"""Phase 0 measurement harness — a fixed corpus + a benchmark to score matchers.

Nothing in the match-v3 rebuild improves without a number to improve. This module
is that number's home: a set of known targets and a :func:`benchmark` that every
later phase (pitch-aware probing, analytic estimation, the digital twin) calls to
report median wall-clock, mean closeness, probe count, and cache hits.

It runs entirely OFFLINE. There is no S-1 and no digital twin yet, so corpus audio
is produced by :func:`render_placeholder` — a deliberately crude saw/square/sub/noise
→ lowpass → ADSR renderer. **That audio is a PLACEHOLDER, not the Roland S-1.** Its
only jobs are to be deterministic and to exercise the real feature/closeness math.
When real S-1 captures (or twin renders) exist, they replace the placeholder as the
corpus source; the harness around them does not change.

One thing this harness explicitly does NOT do: validate that the closeness number
matches Tyler's ears. Per ``FABLE.md``, the distance metric is unvalidated, and
confirming it perceptually is a separate HUMAN ceremony (play pairs, ask which is
closer). This code only *measures* closeness with the existing metric; it makes no
claim that the metric is perceptually correct.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

import numpy as np

from . import ANALYSIS_SECONDS, WORKING_SR
from .capture import AudioClip, prepare
from .distance import Weights, closeness, loss, reference_scales
from .features import Features, extract

# ── CC numbers the placeholder renderer reads ────────────────────────────────
# These mirror ``schema.py`` (the source of truth); the corpus is keyed by CC
# number, so a vector produced here decodes identically on the real device.
_CC_SAW = 20      # Saw Level
_CC_SQUARE = 19   # Square Level
_CC_SUB = 21      # Sub Level
_CC_NOISE = 23    # Noise Level
_CC_CUTOFF = 74   # Filter Frequency / cutoff
_CC_ATTACK = 73   # Env Attack
_CC_DECAY = 75    # Env Decay
_CC_SUSTAIN = 30  # Env Sustain
_CC_RELEASE = 72  # Env Release


def midi_to_hz(note: float) -> float:
    """MIDI note number → frequency in Hz (A4 = 69 = 440 Hz)."""
    return 440.0 * 2.0 ** ((note - 69.0) / 12.0)


# ──────────────────────────────────────────────────────────────────────────────
# Target record
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class Target:
    """One corpus item: a sound to match, plus what we know is true about it.

    ``features`` is the analysis-ready feature bundle used for scoring. A
    committed fixture carries ``features`` (and no ``clip``) so CI can run the
    scoring math with no audio; a freshly generated target carries both.
    ``cc`` is the ground-truth CC vector when known (it is, for synthetic
    targets — it is what we rendered), or ``None`` for a real captured sound.
    """

    name: str
    f0: float                                   # true fundamental, Hz
    cc: dict[int, int] | None = None            # ground-truth CC vector, if known
    clip: AudioClip | None = None               # analysis-ready audio, if present
    features: Features | None = None            # extracted features (fixtures ship this)

    def feats(self) -> Features:
        """Return the feature bundle, extracting from the clip on demand."""
        if self.features is not None:
            return self.features
        if self.clip is not None:
            self.features = extract(self.clip)
            return self.features
        raise ValueError(f"Target {self.name!r} has neither features nor a clip")

    # ── round-trip (feature-only; no large WAVs) ─────────────────────────────
    def save(self, path: str | Path) -> Path:
        """Write this target as a compact ``.npz`` (features + metadata, no clip).

        Audio samples are intentionally dropped: fixtures store the extracted
        features, which are an order of magnitude smaller than the WAV and are
        exactly what the scoring math consumes.
        """
        feats = self.feats()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            path,
            name=self.name,
            f0=np.float64(self.f0),
            cc_json=json.dumps(self.cc),
            logmel=feats.logmel,
            mfcc=feats.mfcc,
            centroid=feats.centroid,
            flatness=feats.flatness,
            env=feats.env,
        )
        # np.savez appends .npz if missing — normalize the returned path.
        return path if path.suffix == ".npz" else path.with_suffix(".npz")

    @classmethod
    def load(cls, path: str | Path) -> "Target":
        """Load a target saved by :meth:`save` (feature-only)."""
        with np.load(path, allow_pickle=False) as data:
            cc_raw = json.loads(str(data["cc_json"]))
            cc = {int(k): int(v) for k, v in cc_raw.items()} if cc_raw is not None else None
            feats = Features(
                logmel=data["logmel"],
                mfcc=data["mfcc"],
                centroid=data["centroid"],
                flatness=data["flatness"],
                env=data["env"],
            )
            return cls(name=str(data["name"]), f0=float(data["f0"]), cc=cc, features=feats)


# ──────────────────────────────────────────────────────────────────────────────
# Placeholder synthesis  (NOT the real S-1 — see module docstring)
# ──────────────────────────────────────────────────────────────────────────────
def _adsr_envelope(n: int, sr: int, a: float, d: float, s: float, r: float, hold: float) -> np.ndarray:
    """A simple linear ADSR gate over ``n`` samples. Placeholder shaping only."""
    env = np.zeros(n, dtype=np.float64)
    a_n = max(int(a * sr), 1)
    d_n = max(int(d * sr), 1)
    hold_n = int(hold * sr)
    t = np.arange(n)
    # Attack: 0 → 1
    env[:a_n] = np.linspace(0.0, 1.0, a_n, endpoint=False)
    # Decay: 1 → sustain
    dec_end = min(a_n + d_n, n)
    env[a_n:dec_end] = np.linspace(1.0, s, dec_end - a_n, endpoint=False)
    # Sustain: hold at s until note-off
    off = min(a_n + d_n + hold_n, n)
    env[dec_end:off] = s
    # Release: s → 0
    r_n = max(int(r * sr), 1)
    rel_end = min(off + r_n, n)
    env[off:rel_end] = np.linspace(s, 0.0, rel_end - off, endpoint=False)
    env[rel_end:] = 0.0
    _ = t  # keep the index handy for future tweaks; silence linters
    return env


def render_placeholder(
    cc: dict[int, int],
    f0: float,
    seconds: float = ANALYSIS_SECONDS,
    sr: int = WORKING_SR,
    seed: int | None = None,
) -> AudioClip:
    """Deterministically render a crude tone from a CC vector at pitch ``f0``.

    PLACEHOLDER ONLY. This is a stand-in for real S-1 output so the corpus and
    benchmark can run with no hardware. It reads oscillator levels, filter
    cutoff, and ADSR from ``cc`` and mixes band-limited-ish saw/square/sub plus
    noise through a lowpass and an amplitude envelope. It is NOT calibrated to
    the S-1 and must be replaced by real captures (or twin renders) before any
    match result is trusted.
    """
    from scipy.signal import butter, lfilter, sawtooth, square

    n = int(seconds * sr)
    t = np.arange(n) / sr

    def lvl(code: int) -> float:
        return cc.get(code, 0) / 127.0

    saw = sawtooth(2.0 * np.pi * f0 * t) * lvl(_CC_SAW)
    sqr = square(2.0 * np.pi * f0 * t) * lvl(_CC_SQUARE)
    sub = square(2.0 * np.pi * (f0 / 2.0) * t) * lvl(_CC_SUB)

    # Deterministic noise seeded from the patch so re-renders are identical.
    if seed is None:
        seed = (int(round(f0)) * 131 + sum(cc.values())) & 0x7FFFFFFF
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(n) * lvl(_CC_NOISE)

    mix = saw + sqr + sub + noise
    peak = np.abs(mix).max()
    if peak > 1e-9:
        mix = mix / peak

    # Lowpass whose cutoff tracks CC74 (log-mapped 40 Hz → ~8 kHz). Placeholder.
    cutoff_hz = 40.0 * (8000.0 / 40.0) ** lvl(_CC_CUTOFF)
    wn = np.clip(cutoff_hz / (sr / 2.0), 1e-3, 0.99)
    b, a = butter(2, wn)
    mix = lfilter(b, a, mix)

    env = _adsr_envelope(
        n, sr,
        a=lvl(_CC_ATTACK) * 0.5,
        d=lvl(_CC_DECAY) * 0.5,
        s=max(lvl(_CC_SUSTAIN), 0.05),
        r=lvl(_CC_RELEASE) * 0.5,
        hold=seconds * 0.5,
    )
    out = (mix * env).astype(np.float32)
    peak = np.abs(out).max()
    if peak > 1e-9:
        out = out / peak
    return AudioClip(out, sr)


# ──────────────────────────────────────────────────────────────────────────────
# Corpus specs + generation
# ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class CorpusSpec:
    """A recipe for one corpus target: a name, a pitch, and a CC vector."""

    name: str
    midi_note: int
    cc: dict[int, int]


def _patch(overrides: dict[int, int] | None = None) -> dict[int, int]:
    """A minimal, legal CC vector: a plain saw at mid cutoff, plus overrides."""
    base = {
        _CC_SAW: 100, _CC_SQUARE: 0, _CC_SUB: 0, _CC_NOISE: 0,
        _CC_CUTOFF: 80, _CC_ATTACK: 0, _CC_DECAY: 40, _CC_SUSTAIN: 110, _CC_RELEASE: 20,
    }
    base.update(overrides or {})
    return base


# A small, deliberately varied set: different pitches (the C3 vs. not-C3 axis the
# whole rebuild turns on) and different timbres (bright saw, dark square, sub-heavy,
# noisy). Ground-truth CCs are known because we render them.
DEFAULT_SPECS: tuple[CorpusSpec, ...] = (
    CorpusSpec("bright_saw_c3", 48, _patch({_CC_SAW: 120, _CC_CUTOFF: 110})),
    CorpusSpec("dark_square_g4", 67, _patch({_CC_SAW: 0, _CC_SQUARE: 120, _CC_CUTOFF: 45})),
    CorpusSpec("sub_heavy_c2", 36, _patch({_CC_SAW: 60, _CC_SUB: 120, _CC_CUTOFF: 70})),
    CorpusSpec("noisy_a3", 57, _patch({_CC_SAW: 70, _CC_NOISE: 90, _CC_CUTOFF: 95, _CC_ATTACK: 30})),
)


def _default_cache_dir() -> Path:
    from ..paths import data_dir

    return data_dir() / "corpus"


def generate_corpus(
    specs: tuple[CorpusSpec, ...] = DEFAULT_SPECS,
    cache_dir: str | Path | None = None,
    force: bool = False,
) -> list[Target]:
    """Render (or load from cache) the corpus as a list of :class:`Target`.

    Clips are cached under ``cache_dir`` (default ``~/.synth/corpus/``) so later
    runs score against fixed audio with no hardware. Rendering is deterministic,
    so a cache miss and a cache hit yield identical features. Pass ``force=True``
    to re-render and overwrite the cache.
    """
    out_dir = Path(cache_dir) if cache_dir is not None else _default_cache_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    targets: list[Target] = []
    for spec in specs:
        f0 = midi_to_hz(spec.midi_note)
        path = out_dir / f"{spec.name}.npz"
        if path.exists() and not force:
            targets.append(Target.load(path))
            continue
        clip = prepare(render_placeholder(spec.cc, f0))
        target = Target(name=spec.name, f0=f0, cc=dict(spec.cc), clip=clip, features=extract(clip))
        target.save(path)
        targets.append(target)
    return targets


def save_corpus(targets: list[Target], out_dir: str | Path) -> list[Path]:
    """Write every target as a feature-only ``.npz`` under ``out_dir``."""
    out_dir = Path(out_dir)
    return [t.save(out_dir / f"{t.name}.npz") for t in targets]


def load_corpus(in_dir: str | Path) -> list[Target]:
    """Load all ``*.npz`` targets in ``in_dir``, sorted by name."""
    in_dir = Path(in_dir)
    return [Target.load(p) for p in sorted(in_dir.glob("*.npz"))]


# ──────────────────────────────────────────────────────────────────────────────
# The benchmark every later phase calls
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class MatchResult:
    """What a matcher returns for one target: a candidate + its cost counters.

    ``features`` is the candidate sound the matcher settled on, scored against
    the target by the harness (so every matcher is judged by the same metric).
    ``probe_count`` and ``cache_hits`` are the matcher's own tallies of hardware
    probes and memoized re-evaluations.
    """

    features: Features
    probe_count: int = 0
    cache_hits: int = 0
    cc: dict[int, int] | None = None
    extra: dict = field(default_factory=dict)


class Matcher(Protocol):
    """Minimal matcher interface: map a target to a :class:`MatchResult`.

    This is intentionally decoupled from :class:`~synth.match.session.MatchSession`.
    Later phases adapt their real matcher (session-driven, twin-driven, …) to this
    single call so the benchmark can score any of them the same way.
    """

    def __call__(self, target: Target) -> MatchResult:  # pragma: no cover - protocol
        ...


# A plain callable is just as acceptable as a Protocol implementer.
MatcherFn = Callable[[Target], MatchResult]


def benchmark(matcher: MatcherFn, corpus: list[Target], weights: Weights | None = None) -> dict:
    """Score ``matcher`` over ``corpus`` and return the four Phase-0 metrics.

    Returns ``{median_seconds, mean_closeness, probe_count, cache_hits}``:

    - ``median_seconds`` — median wall-clock per target of the matcher call.
    - ``mean_closeness`` — mean 0-100 closeness of the matcher's candidate to the
      target, using the existing (unvalidated) distance metric.
    - ``probe_count`` — total hardware probes the matcher reported.
    - ``cache_hits`` — total memoized re-evaluations the matcher reported.

    An empty corpus returns zeros. The closeness is a *measurement*, not a
    perceptual guarantee (see the module docstring).
    """
    weights = weights or Weights()
    if not corpus:
        return {"median_seconds": 0.0, "mean_closeness": 0.0, "probe_count": 0, "cache_hits": 0}

    seconds: list[float] = []
    closenesses: list[float] = []
    probe_count = 0
    cache_hits = 0

    for target in corpus:
        target_feat = target.feats()
        scales = reference_scales(target_feat)

        start = time.perf_counter()
        result = matcher(target)
        seconds.append(time.perf_counter() - start)

        cand_loss = loss(target_feat, result.features, weights, scales=scales)
        closenesses.append(closeness(cand_loss, weights))
        probe_count += int(result.probe_count)
        cache_hits += int(result.cache_hits)

    return {
        "median_seconds": float(np.median(seconds)),
        "mean_closeness": float(np.mean(closenesses)),
        "probe_count": int(probe_count),
        "cache_hits": int(cache_hits),
    }
