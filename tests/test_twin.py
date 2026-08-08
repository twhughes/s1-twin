"""Tests for the differentiable digital twin (``synth/match/twin.py``).

Everything here runs OFFLINE — no S-1, no audio device. The headline test is
:func:`test_gradcheck_matches_finite_differences`: the twin's
``cc-vector -> render -> differentiable-loss`` gradient (autograd) agrees with
central finite differences, which is the core correctness proof that the search
descends a real gradient.

The twin is calibrated only against SYNTHETIC / self-consistent targets here (no
hardware in this session), and the distance metric is NOT perceptually validated —
both are honest limitations the tests assert are LABELED, not hidden.
"""

from __future__ import annotations

import numpy as np
import pytest

from synth.backend_protocol import DifferentiableBackend, InstrumentBackend
from synth.match.capture import AudioClip, prepare
from synth.match.corpus import MatchResult, Target, benchmark
from synth.match.features import extract
from synth.match.twin import (
    _K_INDEX,
    K_PARAMS,
    S_PARAMS,
    CalibrationReport,
    Twin,
    TwinMatcher,
    _default_s,
    calibrate,
    spectral_loss,
)


def _feat(clip_samples: np.ndarray, sr: int):
    return extract(prepare(AudioClip(np.asarray(clip_samples, np.float32), sr)))


# ── render contract ───────────────────────────────────────────────────────────


def test_render_shape_dtype_and_samplerate():
    tw = Twin(sr=16000, seconds=0.5)
    k = np.full(tw.k_dim, 0.5)
    audio = np.asarray(tw.render(k, _default_s(), 57))
    assert audio.shape == (int(round(0.5 * 16000)),)
    assert np.isfinite(audio).all()
    assert audio.dtype == np.float64  # autograd works in float64


def test_k_dim_matches_param_schema():
    tw = Twin()
    assert tw.k_dim == len(K_PARAMS) == 18
    # every continuous param maps to a distinct S-1 CC
    ccs = [p.cc for p in K_PARAMS]
    assert len(set(ccs)) == len(ccs)


def test_conforms_to_differentiable_backend_protocol():
    tw = Twin()
    assert isinstance(tw, InstrumentBackend)
    assert isinstance(tw, DifferentiableBackend)
    # the five backend methods behave (structural tier-1 conformance)
    assert tw.send(74, 100) is False  # disconnected
    tw.connect("twin")
    assert tw.send(74, 100) is True
    tw.disconnect()
    assert tw.send(74, 100) is False


def test_s_choices_change_the_sound():
    tw = Twin(sr=16000, seconds=0.4)
    k = np.full(tw.k_dim, 0.4)
    base = np.asarray(tw.render(k, {"sub_octave": 2, "lfo_shape": 2, "amp_env_mode": 1}, 48))
    for changed in (
        {"sub_octave": 0, "lfo_shape": 2, "amp_env_mode": 1},
        {"sub_octave": 2, "lfo_shape": 3, "amp_env_mode": 1},
        {"sub_octave": 2, "lfo_shape": 2, "amp_env_mode": 0},
    ):
        other = np.asarray(tw.render(k, changed, 48))
        assert float(np.abs(base - other).mean()) > 1e-6


def test_cutoff_sweep_raises_brightness_monotonically():
    tw = Twin(sr=16000, seconds=0.5)
    k = np.full(tw.k_dim, 0.3)
    k[_K_INDEX["saw_lvl"]] = 0.8
    centroids = []
    for cv in (0.2, 0.4, 0.6, 0.8):
        k2 = k.copy()
        k2[_K_INDEX["cutoff"]] = cv
        centroids.append(float(_feat(tw.render(k2, _default_s(), 57), tw.sr).centroid.mean()))
    assert all(centroids[i] < centroids[i + 1] for i in range(len(centroids) - 1))


def test_noise_raises_spectral_flatness():
    tw = Twin(sr=16000, seconds=0.5)
    k = np.full(tw.k_dim, 0.2)
    k[_K_INDEX["saw_lvl"]] = 0.6
    k[_K_INDEX["cutoff"]] = 0.9  # open filter so noise is audible
    flats = []
    for nv in (0.0, 0.4, 0.8):
        k2 = k.copy()
        k2[_K_INDEX["noise_lvl"]] = nv
        flats.append(float(_feat(tw.render(k2, _default_s(), 57), tw.sr).flatness.mean()))
    assert flats[0] < flats[1] < flats[2]


def test_cc_k_roundtrips():
    tw = Twin()
    k = np.full(tw.k_dim, 0.5)
    cc = tw.k_to_cc(k, _default_s())
    # every continuous + discrete CC is present
    for p in K_PARAMS:
        assert p.cc in cc
    for sp in S_PARAMS:
        assert cc[sp.cc] == _default_s()[sp.name]
    k2 = tw.cc_to_k(cc)
    assert float(np.abs(k - k2).max()) < 0.02  # within CC quantization


# ── the core correctness proof ────────────────────────────────────────────────


def test_gradcheck_matches_finite_differences():
    """Autograd gradient of ``render -> spectral_loss`` matches central FD.

    Uses a MODULATED config (filter-env + LFO on) so the harder time-varying
    filter path and the pitch/LFO gradients are all exercised. FD is Richardson-
    extrapolated (O(eps^4)) because the pitch params make the loss oscillatory —
    a plain central difference is FD-limited there, not gradient-limited."""
    from autograd import grad

    tw = Twin(sr=6000, seconds=0.3)
    target = np.asarray(tw.render(np.full(tw.k_dim, 0.4), None, 48), dtype=np.float64)

    def obj(k: np.ndarray) -> float:
        return spectral_loss(tw.render(k, None, 48), target, tw.sr)

    k = np.full(tw.k_dim, 0.5)
    for name, val in (("env_to_cutoff", 0.3), ("lfo_depth", 0.25),
                      ("lfo_to_cutoff", 0.2), ("lfo_to_pitch", 0.15), ("lfo_rate", 0.35)):
        k[_K_INDEX[name]] = val

    g = grad(obj)(k)

    def central(eps: float) -> np.ndarray:
        fd = np.zeros_like(k)
        for i in range(len(k)):
            kp, km = k.copy(), k.copy()
            kp[i] += eps
            km[i] -= eps
            fd[i] = (obj(kp) - obj(km)) / (2 * eps)
        return fd

    fd = (4.0 * central(1e-6) - central(2e-6)) / 3.0  # Richardson
    rel = np.abs(g - fd) / (np.maximum(np.abs(fd), np.abs(g)) + 1e-6)
    assert rel.max() < 1e-3, f"max rel-err {rel.max():.2e}"


def test_spectral_loss_zero_for_identical_positive_for_different():
    tw = Twin(sr=16000, seconds=0.4)
    a = np.asarray(tw.render(np.full(tw.k_dim, 0.4), None, 57), dtype=np.float64)
    b = np.asarray(tw.render(np.full(tw.k_dim, 0.7), None, 57), dtype=np.float64)
    assert float(spectral_loss(a, a, tw.sr)) == pytest.approx(0.0, abs=1e-9)
    assert float(spectral_loss(a, b, tw.sr)) > 1.0


# ── the search machinery, proven without hardware ─────────────────────────────


def test_self_consistency_search_recovers_the_patch():
    """A target rendered BY the twin at a known cc* is recovered by the gradient
    search to a high closeness — end-to-end proof of the search WITHOUT hardware."""
    tw = Twin(sr=11025, seconds=0.6)
    k_star = np.full(tw.k_dim, 0.12)
    for name, val in (("saw_lvl", 0.8), ("sub_lvl", 0.4), ("cutoff", 0.55),
                      ("resonance", 0.25), ("env_to_cutoff", 0.35), ("decay", 0.4),
                      ("sustain", 0.6), ("release", 0.3)):
        k_star[_K_INDEX[name]] = val
    note = 50
    target = np.asarray(tw.render(k_star, _default_s(), note), dtype=np.float64)

    matcher = TwinMatcher(twin=tw, iters=90, s_sweep=(),
                          search_sr=tw.sr, search_seconds=tw.seconds)
    result = matcher.match(target, note)
    assert result.closeness > 60.0, f"closeness {result.closeness:.1f}"
    assert result.evals > 0


def test_twin_matcher_conforms_to_matcher_protocol():
    """``TwinMatcher(target) -> MatchResult`` with a CC vector and ZERO probes —
    so it plugs straight into ``benchmark()``."""
    tw = Twin(sr=11025, seconds=0.6)
    k_star = np.full(tw.k_dim, 0.2)
    k_star[_K_INDEX["saw_lvl"]] = 0.8
    k_star[_K_INDEX["cutoff"]] = 0.5
    note = 48
    clip = prepare(AudioClip(np.asarray(tw.render(k_star, _default_s(), note), np.float32), tw.sr))
    target = Target(name="twin_native", f0=440.0 * 2 ** ((note - 69) / 12), clip=clip)

    matcher = TwinMatcher(twin=tw, iters=60, s_sweep=(),
                          search_sr=tw.sr, search_seconds=tw.seconds)
    result = matcher(target)
    assert isinstance(result, MatchResult)
    assert result.probe_count == 0  # the twin touches NO hardware
    assert result.cc is not None and 74 in result.cc


def test_benchmark_returns_the_four_metrics():
    """``benchmark(twin_matcher, corpus)`` runs and returns the four Phase-0
    metrics. Corpus here is TWIN-generated (the twin's own targets), so closeness
    is high — this is the twin's self-consistent benchmark, not a hardware claim."""
    tw = Twin(sr=11025, seconds=0.6)
    corpus = []
    for i, (note, saw, cut) in enumerate([(48, 0.8, 0.5), (55, 0.5, 0.7)]):
        k = np.full(tw.k_dim, 0.15)
        k[_K_INDEX["saw_lvl"]] = saw
        k[_K_INDEX["cutoff"]] = cut
        k[_K_INDEX["sustain"]] = 0.6
        clip = prepare(AudioClip(np.asarray(tw.render(k, _default_s(), note), np.float32), tw.sr))
        corpus.append(Target(name=f"twin_{i}", f0=440.0 * 2 ** ((note - 69) / 12), clip=clip))

    matcher = TwinMatcher(twin=tw, iters=70, s_sweep=(),
                          search_sr=tw.sr, search_seconds=tw.seconds)
    metrics = benchmark(matcher, corpus)
    assert set(metrics) == {"median_seconds", "mean_closeness", "probe_count", "cache_hits"}
    assert metrics["probe_count"] == 0
    assert metrics["median_seconds"] > 0.0
    assert metrics["mean_closeness"] > 50.0  # twin matches its own targets well


# ── the calibration seam (synthetic only — no real S-1 this session) ───────────


def test_calibrate_seam_reports_synthetic_gap():
    """``calibrate`` fits against SYNTHETIC probes and reports a held-out gap that
    it LABELS as synthetic (FABLE: report the gap; real S-1 probes replace these)."""
    tw = Twin(sr=11025, seconds=0.6)
    rng = np.random.default_rng(1)
    probes = []
    for _ in range(6):
        k = np.clip(0.5 + 0.2 * rng.standard_normal(tw.k_dim), 0.05, 0.95)
        cc = tw.k_to_cc(k)
        audio = np.asarray(tw.render(k, None, 48), dtype=np.float32)
        probes.append((cc, audio))

    report = calibrate(probes, tw, note=48)
    assert isinstance(report, CalibrationReport)
    assert report.is_synthetic is True
    assert "no real S-1" in report.note
    assert report.n_held_out >= 1
    # twin-vs-twin at the same note: the held-out feature error is near zero
    assert report.held_out_feature_error < 0.2


def test_calibrate_rejects_empty_probes():
    with pytest.raises(ValueError):
        calibrate([], Twin())
