"""Contract tests for the match-engine seams (``synth/match/protocols.py``).

Three things are pinned here:

1. The :class:`Driver` protocol is honoured by BOTH the real ``SynthDriver`` (wired
   to a fake MIDI backend + fake monitor) and the test ``FakeDriver`` — one contract,
   two implementations.
2. The :class:`Matcher` contract + ``benchmark`` are **reused from corpus.py**, not
   duplicated: ``protocols.Matcher is corpus.Matcher``, and a plain callable matcher
   scores through ``benchmark``.
3. Phase A is wired: a non-C3 target makes the session point the driver at the
   detected pitch instead of the hardwired C3 (48).
"""

from __future__ import annotations

import numpy as np
import pytest

from synth.match import WORKING_SR, corpus, protocols
from synth.match import distance as distance_mod
from synth.match import driver as driver_mod
from synth.match.capture import AudioClip
from synth.match.corpus import MatchResult, Target, benchmark, generate_corpus
from synth.match.driver import SynthDriver
from synth.match.features import Features, extract
from synth.match.protocols import DistanceMetric, Driver, FeatureExtractor
from synth.match.session import MatchConfig, MatchSession
from synth.match.space import ParamSpace

# Reused fakes / helpers from the sibling test modules (all mine).
from tests.test_driver import FakeMidi, FakeMonitor
from tests.test_match_engine import FakeDriver, silence, tone


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Neutralize the driver's real time.sleep (calibrate holds a 1 s note)."""
    monkeypatch.setattr(driver_mod.time, "sleep", lambda *a, **k: None)


# ── Driver protocol: both implementations conform ─────────────
def _make_fake_driver() -> FakeDriver:
    space = ParamSpace(include_effects=False)
    return FakeDriver(space, space.default_vector())


def _make_synth_driver() -> SynthDriver:
    # A voiced clip so calibrate's onset path returns a real (small) latency.
    burst = np.concatenate([np.zeros(WORKING_SR // 2), np.ones(WORKING_SR // 2)])
    mon = FakeMonitor(AudioClip(burst.astype(np.float32), WORKING_SR))
    return SynthDriver(FakeMidi(), device=None, note=48, hold_s=0.0, tail_s=0.0, monitor=mon)


@pytest.mark.parametrize("factory", [_make_fake_driver, _make_synth_driver], ids=["fake", "synth"])
def test_driver_contract(factory):
    d = factory()
    assert isinstance(d, Driver)
    assert d.apply({74: 100}) is None
    clip = d.probe({74: 100})
    assert isinstance(clip, AudioClip)
    latency = d.calibrate()
    assert isinstance(latency, float)


# ── FeatureExtractor / DistanceMetric seams ───────────────────
def test_feature_extractor_conforms():
    assert isinstance(extract, FeatureExtractor)
    feat = extract(tone(220.0))
    assert isinstance(feat, Features)


def test_distance_module_conforms_to_metric():
    # The distance module itself is the default DistanceMetric implementation.
    assert isinstance(distance_mod, DistanceMetric)


# ── Matcher contract is corpus's, not a duplicate ─────────────
def test_matcher_contract_reexported_from_corpus():
    # protocols.py must re-export corpus's definitions, not shadow them.
    assert protocols.Matcher is corpus.Matcher
    assert protocols.MatchResult is corpus.MatchResult
    assert protocols.Target is corpus.Target


def test_matcher_scores_through_corpus_benchmark(tmp_path):
    corp = generate_corpus(cache_dir=tmp_path)

    class EchoMatcher:
        """A trivial Matcher: return the target's own features (perfect score)."""

        def __call__(self, target: Target) -> MatchResult:
            return MatchResult(features=target.feats(), probe_count=1)

    # corpus.Matcher is a plain (non-runtime_checkable) Protocol, so conformance
    # is proven structurally: benchmark() accepts and scores the matcher.
    matcher = EchoMatcher()
    result = benchmark(matcher, corp)
    assert set(result) == {"median_seconds", "mean_closeness", "probe_count", "cache_hits"}
    assert result["mean_closeness"] == pytest.approx(100.0, abs=1e-3)
    assert result["probe_count"] == len(corp)


# ── Phase A: session probes at the target's detected pitch ────
def test_session_probes_at_detected_pitch():
    driver = SynthDriver(FakeMidi(), device=None, note=48, monitor=None)
    session = MatchSession(driver, MatchConfig(include_effects=False))
    # A clearly-voiced 220 Hz tone -> A3 (MIDI 57), not the C3 (48) default.
    session.set_target_clip(tone(220.0, harmonics=3))
    assert driver.note != 48
    assert driver.note == 57


def test_session_unvoiced_target_falls_back_to_c3():
    driver = SynthDriver(FakeMidi(), device=None, note=99, monitor=None)
    session = MatchSession(driver, MatchConfig(include_effects=False))
    session.set_target_clip(silence())  # no detectable pitch -> C3 fallback
    assert driver.note == 48
