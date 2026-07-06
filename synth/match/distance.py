"""Closeness metric between a target sound and a candidate.

Combines a few complementary feature distances into a single scalar loss, then
maps that to a 0-100 ``closeness`` percentage. Identical clips score ~100; wildly
different ones approach 0.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .features import Features


@dataclass(frozen=True)
class Weights:
    logmel: float = 1.0   # overall spectral shape (the workhorse)
    mfcc: float = 0.5     # broad timbral colour
    env: float = 0.8      # ADSR / amplitude shape over time
    centroid: float = 0.3  # brightness
    flatness: float = 0.2  # noisiness vs. tonality

    # Steepness of loss -> closeness mapping. With reference-normalized terms
    # (each ~1 at target-vs-silence), a plausible match sits around 0.1-0.4
    # total loss; 1.5 maps that band to roughly 55-86% closeness, matching how
    # the old unnormalized metric (decay 2.5) read in practice.
    decay: float = 1.5


_TERMS = ("logmel", "mfcc", "env", "centroid", "flatness")


def _align(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Trim a pair of feature arrays to a common length along the last axis."""
    n = min(a.shape[-1], b.shape[-1])
    return a[..., :n], b[..., :n]


def _raw_terms(target: Features, cand: Features) -> dict[str, float]:
    """The five unweighted term distances (each 0 for identical clips)."""
    tm, cm = _align(target.logmel, cand.logmel)
    tf, cf = _align(target.mfcc, cand.mfcc)
    te, ce = _align(target.env, cand.env)
    tc, cc = _align(target.centroid, cand.centroid)
    tfl, cfl = _align(target.flatness, cand.flatness)
    return {
        "logmel": float(np.abs(tm - cm).mean()),
        # Skip the 0th coefficient (overall energy) — already loudness-normalized.
        "mfcc": float(np.abs(tf[1:] - cf[1:]).mean()),
        "env": float(np.sqrt(((te - ce) ** 2).mean())),
        "centroid": float(np.abs(tc - cc).mean()),
        "flatness": float(np.abs(tfl - cfl).mean()),
    }


def reference_scales(target: Features) -> dict[str, float]:
    """Per-term normalizers: each term measured between the target and pure
    silence.

    The raw terms mix L1 means and RMS over features with very different
    natural ranges, so without this the effective weighting is whatever the
    raw magnitudes happen to be. Dividing by these scales makes every term
    ~1.0 at "maximally wrong" (silence), so :class:`Weights` express real
    relative importance.
    """
    from . import ANALYSIS_SECONDS, WORKING_SR
    from .capture import AudioClip
    from .features import extract

    n = int(ANALYSIS_SECONDS * WORKING_SR)
    silence = extract(AudioClip(np.zeros(n, dtype=np.float32), WORKING_SR))
    raw = _raw_terms(target, silence)
    return {k: max(v, 1e-6) for k, v in raw.items()}


def loss(
    target: Features,
    cand: Features,
    w: Weights = Weights(),
    scales: dict[str, float] | None = None,
) -> float:
    """Weighted distance between two feature bundles (0 = identical).

    Pass ``scales`` from :func:`reference_scales` to normalize each term
    before weighting; without it the raw (incommensurable) magnitudes apply.
    """
    raw = _raw_terms(target, cand)
    if scales:
        raw = {k: v / scales.get(k, 1.0) for k, v in raw.items()}
    return sum(getattr(w, k) * raw[k] for k in _TERMS)


def closeness(loss_value: float, w: Weights = Weights()) -> float:
    """Map a loss to a 0-100 closeness percentage (monotonic, bounded)."""
    return float(100.0 * np.exp(-w.decay * loss_value))
