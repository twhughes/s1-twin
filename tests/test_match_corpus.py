"""Tests for the Phase 0 measurement harness (synth/match/corpus.py).

Everything here runs OFFLINE: no S-1, no audio device, no file I/O beyond the
committed feature-only fixtures and a pytest tmp dir. The placeholder renderer is
pure numpy/scipy.
"""

from pathlib import Path

import numpy as np
import pytest

from synth.match import ANALYSIS_SECONDS, WORKING_SR
from synth.match.corpus import (
    DEFAULT_SPECS,
    Matcher,
    MatchResult,
    Target,
    benchmark,
    generate_corpus,
    load_corpus,
    midi_to_hz,
    render_placeholder,
    save_corpus,
)
from synth.match.features import Features, extract

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "corpus"


# ── committed fixtures ────────────────────────────────────────────────────────
def test_fixtures_present_and_load():
    corpus = load_corpus(FIXTURE_DIR)
    assert len(corpus) == len(DEFAULT_SPECS)
    names = {t.name for t in corpus}
    assert names == {s.name for s in DEFAULT_SPECS}
    for t in corpus:
        assert isinstance(t.feats(), Features)
        assert t.cc is not None and all(isinstance(k, int) for k in t.cc)
        assert t.f0 > 0


# ── Target round-trip ─────────────────────────────────────────────────────────
def test_target_roundtrips(tmp_path):
    clip = render_placeholder({20: 110, 74: 90}, midi_to_hz(48))
    original = Target(name="rt", f0=midi_to_hz(48), cc={20: 110, 74: 90}, features=extract(clip))
    path = original.save(tmp_path / "rt.npz")
    loaded = Target.load(path)

    assert loaded.name == original.name
    assert loaded.f0 == pytest.approx(original.f0)
    assert loaded.cc == original.cc  # int keys preserved through JSON
    np.testing.assert_array_equal(loaded.feats().logmel, original.feats().logmel)
    np.testing.assert_array_equal(loaded.feats().mfcc, original.feats().mfcc)


def test_target_cc_may_be_none(tmp_path):
    clip = render_placeholder({20: 100}, 220.0)
    t = Target(name="anon", f0=220.0, cc=None, features=extract(clip))
    loaded = Target.load(t.save(tmp_path / "anon.npz"))
    assert loaded.cc is None


def test_target_feats_extracts_from_clip():
    clip = render_placeholder({20: 100}, 220.0)
    t = Target(name="c", f0=220.0, clip=clip)  # no features yet
    assert t.features is None
    feats = t.feats()
    assert isinstance(feats, Features)
    assert t.features is feats  # memoized


def test_target_without_features_or_clip_raises():
    with pytest.raises(ValueError):
        Target(name="empty", f0=100.0).feats()


# ── placeholder renderer ──────────────────────────────────────────────────────
def test_render_placeholder_deterministic():
    cc = {20: 100, 23: 80, 74: 90}
    a = render_placeholder(cc, midi_to_hz(48))
    b = render_placeholder(cc, midi_to_hz(48))
    np.testing.assert_array_equal(a.samples, b.samples)


def test_render_placeholder_shape_and_range():
    clip = render_placeholder({20: 120, 74: 100}, 440.0)
    assert clip.samplerate == WORKING_SR
    assert len(clip.samples) == int(ANALYSIS_SECONDS * WORKING_SR)
    assert np.abs(clip.samples).max() <= 1.0 + 1e-6
    assert clip.samples.dtype == np.float32


def test_brighter_cutoff_is_brighter():
    # A higher cutoff CC should raise the spectral centroid (a sanity check on
    # the placeholder, not a perceptual claim).
    from synth.match.capture import prepare

    dark = extract(prepare(render_placeholder({20: 120, 74: 10}, 220.0)))
    bright = extract(prepare(render_placeholder({20: 120, 74: 120}, 220.0)))
    assert bright.centroid.mean() > dark.centroid.mean()


def test_midi_to_hz():
    assert midi_to_hz(69) == pytest.approx(440.0)
    assert midi_to_hz(57) == pytest.approx(220.0)
    assert midi_to_hz(48) == pytest.approx(130.81, abs=0.1)


# ── corpus generation ─────────────────────────────────────────────────────────
def test_generate_corpus_deterministic_and_caches(tmp_path):
    first = generate_corpus(cache_dir=tmp_path, force=True)
    assert len(first) == len(DEFAULT_SPECS)
    # Every npz got written to the cache dir.
    assert len(list(tmp_path.glob("*.npz"))) == len(DEFAULT_SPECS)

    # A second run loads from cache and yields identical features.
    second = generate_corpus(cache_dir=tmp_path, force=False)
    assert [t.name for t in first] == [t.name for t in second]
    for a, b in zip(first, second):
        np.testing.assert_array_equal(a.feats().logmel, b.feats().logmel)
        assert a.cc == b.cc


def test_save_and_load_roundtrip(tmp_path):
    targets = generate_corpus(cache_dir=tmp_path, force=True)
    other = tmp_path / "fixtures"
    save_corpus(targets, other)
    reloaded = load_corpus(other)
    assert [t.name for t in reloaded] == sorted(t.name for t in targets)


# ── benchmark ─────────────────────────────────────────────────────────────────
def _perfect_matcher(target: Target) -> MatchResult:
    """Trivial stub: returns the target's own features (closeness ~100)."""
    return MatchResult(features=target.feats(), probe_count=3, cache_hits=1)


def test_benchmark_returns_four_metrics():
    corpus = load_corpus(FIXTURE_DIR)
    result = benchmark(_perfect_matcher, corpus)
    assert set(result) == {"median_seconds", "mean_closeness", "probe_count", "cache_hits"}
    assert result["median_seconds"] >= 0.0
    assert result["mean_closeness"] == pytest.approx(100.0, abs=1e-3)
    assert result["probe_count"] == 3 * len(corpus)
    assert result["cache_hits"] == 1 * len(corpus)


def test_benchmark_scores_worse_match_lower():
    corpus = load_corpus(FIXTURE_DIR)

    def bad_matcher(target: Target) -> MatchResult:
        # A different corpus item's features -> lower closeness.
        wrong = next(t for t in corpus if t.name != target.name)
        return MatchResult(features=wrong.feats(), probe_count=0, cache_hits=0)

    good = benchmark(_perfect_matcher, corpus)["mean_closeness"]
    bad = benchmark(bad_matcher, corpus)["mean_closeness"]
    assert bad < good


def test_benchmark_empty_corpus():
    result = benchmark(_perfect_matcher, [])
    assert result == {"median_seconds": 0.0, "mean_closeness": 0.0, "probe_count": 0, "cache_hits": 0}


def test_matcher_protocol_accepts_callable():
    # A plain function satisfies the Matcher protocol structurally.
    m: Matcher = _perfect_matcher
    corpus = load_corpus(FIXTURE_DIR)
    assert m(corpus[0]).probe_count == 3
