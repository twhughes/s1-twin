"""Tests for the sound-matching engine (synth/match).

Uses synthetic audio and a fake driver — no hardware, no audio device, no file I/O.
sounddevice/soundfile are imported lazily inside capture/driver, so they are never
touched here.
"""

import numpy as np
import pytest

from synth.match import WORKING_SR
from synth.match.capture import AudioClip, find_onset, is_silent, prepare
from synth.match.distance import Weights, closeness, loss, reference_scales
from synth.match.features import extract
from synth.match.optimizer import RandomOptimizer, make_optimizer
from synth.match.session import SILENCE_PENALTY_LOSS, MatchConfig, MatchSession
from synth.match.space import ParamSpace
from synth.schema import ControlType


def tone(freq: float, seconds: float = 2.0, sr: int = WORKING_SR, harmonics: int = 1) -> AudioClip:
    t = np.linspace(0, seconds, int(seconds * sr), endpoint=False)
    sig = np.zeros_like(t)
    for h in range(1, harmonics + 1):
        sig += np.sin(2 * np.pi * freq * h * t) / h
    return AudioClip((sig / np.abs(sig).max()).astype(np.float32), sr)


# ── ParamSpace ────────────────────────────────────────────────
class TestParamSpace:
    def test_dim_matches_param_count(self):
        sp = ParamSpace(include_effects=True)
        assert sp.dim == len(sp.params) > 0

    def test_excludes_effects_when_asked(self):
        with_fx = ParamSpace(include_effects=True).dim
        without_fx = ParamSpace(include_effects=False).dim
        assert without_fx < with_fx

    def test_pitch_params_excluded(self):
        ccs = ParamSpace().ccs
        for cc in (76, 18, 27, 106):  # fine tune, bend sens x2, lfo sync
            assert cc not in ccs

    def test_decode_in_range(self):
        sp = ParamSpace()
        out = sp.decode(np.full(sp.dim, 0.7))
        assert all(0 <= v <= 127 for v in out.values())

    def test_switch_snaps_binary(self):
        # The timbre space holds no SWITCH params since the schema audit, so
        # build a space that includes one (CC 65: Portamento) explicitly.
        from synth.schema import param_by_cc

        sp = ParamSpace(params=ParamSpace().params + [param_by_cc(65)])
        switch_cc = 65
        hi = sp.decode(np.where(np.array(sp.ccs) == switch_cc, 0.9, 0.5))
        lo = sp.decode(np.where(np.array(sp.ccs) == switch_cc, 0.1, 0.5))
        assert hi[switch_cc] == 127
        assert lo[switch_cc] == 0

    def test_discrete_snaps_to_legal_values(self):
        sp = ParamSpace()
        disc = next(p for p in sp.params if p.control_type == ControlType.DISCRETE)
        out = sp.decode(np.full(sp.dim, 1.0))
        assert out[disc.cc] in disc.value_labels

    def test_encode_decode_roundtrip_continuous(self):
        sp = ParamSpace()
        vec = sp.default_vector()
        decoded = sp.decode(vec)
        re = sp.encode(decoded)
        np.testing.assert_allclose(vec, re, atol=1e-2)


# ── features / distance ───────────────────────────────────────
class TestFeaturesDistance:
    def test_feature_shapes_consistent(self):
        a = extract(prepare(tone(220)))
        b = extract(prepare(tone(440)))
        assert a.logmel.shape == b.logmel.shape
        assert a.mfcc.shape == b.mfcc.shape

    def test_identical_clips_zero_loss_full_closeness(self):
        f = extract(prepare(tone(330, harmonics=3)))
        l = loss(f, f)
        assert l == pytest.approx(0.0, abs=1e-6)
        assert closeness(l) == pytest.approx(100.0, abs=1e-3)

    def test_similar_closer_than_dissimilar(self):
        target = extract(prepare(tone(220, harmonics=4)))
        close = extract(prepare(tone(225, harmonics=4)))
        far = extract(prepare(tone(1500, harmonics=1)))
        assert loss(target, close) < loss(target, far)

    def test_closeness_monotonic_decreasing(self):
        assert closeness(0.0) > closeness(0.5) > closeness(2.0)
        assert 0.0 <= closeness(10.0) <= 100.0


# ── capture helpers ───────────────────────────────────────────
class TestCapture:
    def test_find_onset_detects_delayed_start(self):
        sr = WORKING_SR
        sig = np.concatenate([np.zeros(sr // 2), np.ones(sr)]).astype(np.float32)
        onset = find_onset(sig, sr)
        assert abs(onset - sr // 2) < sr * 0.05

    def test_prepare_fixes_length_and_normalizes(self):
        clip = prepare(tone(440, seconds=5.0))
        from synth.match import ANALYSIS_SECONDS

        assert len(clip.samples) == int(ANALYSIS_SECONDS * clip.samplerate)
        assert np.abs(clip.samples).max() == pytest.approx(1.0, abs=1e-3)


# ── optimizer ─────────────────────────────────────────────────
class TestOptimizer:
    def test_random_optimizer_improves(self):
        opt = RandomOptimizer(dim=5, popsize=10, max_iters=20, seed=0)
        target = np.full(5, 0.5)
        while not opt.done:
            sols = opt.ask()
            losses = [float(np.sum((s - target) ** 2)) for s in sols]
            opt.tell(sols, losses)
        best_x, best_f = opt.best
        assert best_f < 0.5

    def test_make_optimizer_random(self):
        opt = make_optimizer("random", dim=3, popsize=4, max_iters=2)
        assert isinstance(opt, RandomOptimizer)

    def test_cma_optimizer_converges(self):
        cma = pytest.importorskip("cma")  # noqa: F841
        from synth.match.optimizer import CMAESOptimizer

        opt = CMAESOptimizer(dim=4, x0=np.full(4, 0.5), max_iters=30, seed=1)
        target = np.array([0.2, 0.8, 0.5, 0.1])
        while not opt.done:
            sols = opt.ask()
            losses = [float(np.sum((s - target) ** 2)) for s in sols]
            opt.tell(sols, losses)
        best_x, best_f = opt.best
        assert best_f < 0.05


# ── session (fake driver) ─────────────────────────────────────
class FakeDriver:
    """A pretend S-1: a chosen 'true' param vector renders to a tone whose pitch
    depends on the filter CC, so the optimizer has a real gradient to follow."""

    def __init__(self, space: ParamSpace, true_vec: np.ndarray):
        self.space = space
        self.true = true_vec
        self.applied: dict[int, int] | None = None

    def _vec_for(self, params: dict[int, int]) -> np.ndarray:
        return self.space.encode(params)

    def probe(self, params=None) -> AudioClip:
        v = self._vec_for(params)
        dist = float(np.mean(np.abs(v - self.true)))
        freq = 220.0 + 880.0 * dist  # closer params -> closer pitch
        return prepare(tone(freq, harmonics=3))

    def apply(self, params) -> None:
        self.applied = dict(params)

    def calibrate(self) -> float:
        return 0.0


class TestSession:
    def test_run_improves_closeness(self):
        space = ParamSpace(include_effects=False)
        true_vec = space.default_vector().copy()
        true_vec[0] = min(1.0, true_vec[0] + 0.4)
        driver = FakeDriver(space, true_vec)

        session = MatchSession(
            driver,
            MatchConfig(max_iters=8, popsize=6, optimizer="random", seed=0, include_effects=False),
        )
        session.set_target_clip(driver.probe(space.decode(true_vec)))

        progresses = []
        best = session.run(on_progress=lambda p: progresses.append(p))

        assert isinstance(best, dict) and best
        assert session.best_closeness > 0
        assert driver.applied == best  # apply_best() ran
        assert progresses[-1].done

    def test_stop_halts_run(self):
        space = ParamSpace(include_effects=False)
        driver = FakeDriver(space, space.default_vector())
        session = MatchSession(
            driver,
            MatchConfig(max_iters=1000, popsize=4, optimizer="random", include_effects=False),
        )
        session.set_target_clip(driver.probe(space.decode(space.default_vector())))

        def cb(p):
            if p.evals >= 4:
                session.stop()

        session.run(on_progress=cb)
        assert session._evals < 50

    def test_run_without_target_raises(self):
        space = ParamSpace()
        driver = FakeDriver(space, space.default_vector())
        with pytest.raises(RuntimeError):
            MatchSession(driver).run()

    def test_stop_mid_generation_with_cma_exits_cleanly(self):
        """A generation cut short must not call opt.tell() with a partial
        population — CMA-ES raises on anything but the full sample set."""
        pytest.importorskip("cma")
        space = ParamSpace(include_effects=False)
        driver = FakeDriver(space, space.default_vector())
        session = MatchSession(
            driver,
            MatchConfig(max_iters=100, popsize=6, optimizer="cma", seed=1, include_effects=False),
        )
        session.set_target_clip(driver.probe(space.decode(space.default_vector())))

        def cb(p):
            # Stop after the 2nd eval — mid-generation (popsize=6)
            if p.evals >= 2:
                session.stop()

        best = session.run(on_progress=cb)  # must not raise
        assert isinstance(best, dict)
        assert session._evals < 12


# ── Phase 3: silence penalty, cache, retry, calibration, normalization ──


def silence(seconds: float = 2.0, sr: int = WORKING_SR) -> AudioClip:
    return AudioClip(np.zeros(int(seconds * sr), dtype=np.float32), sr)


class TestSilenceGuard:
    def test_is_silent(self):
        assert is_silent(silence())
        assert not is_silent(tone(220.0))
        assert is_silent(AudioClip(np.zeros(0, dtype=np.float32), WORKING_SR))

    def test_silent_probes_never_become_best(self):
        class SilentDriver:
            def probe(self, params=None):
                return prepare(silence())

            def apply(self, params):
                pass

            def calibrate(self):
                return 0.0

        session = MatchSession(
            SilentDriver(),
            MatchConfig(max_iters=2, popsize=4, optimizer="random", seed=0, include_effects=False),
        )
        session.set_target_clip(tone(220.0))
        best = session.run()
        assert best == {}
        assert session._best_loss == float("inf")
        assert session.best_closeness == 0.0


class TestEvalCache:
    def _session(self, driver):
        s = MatchSession(driver, MatchConfig(optimizer="random", include_effects=False))
        s.set_target_clip(tone(220.0))
        return s

    def test_identical_params_probe_once(self):
        space = ParamSpace(include_effects=False)
        driver = FakeDriver(space, space.default_vector())
        probes = []
        orig = driver.probe
        driver.probe = lambda params=None: (probes.append(1), orig(params))[1]
        session = self._session(driver)
        params = space.decode(space.default_vector())
        l1, clip1 = session._evaluate(params)
        l2, clip2 = session._evaluate(params)
        assert len(probes) == 1
        assert l1 == l2
        assert clip1 is not None and clip2 is None  # cache hit skips hardware
        assert session._cache_hits == 1

    def test_failed_probe_penalized_but_not_cached(self):
        space = ParamSpace(include_effects=False)
        calls = []

        class FlakyDriver:
            def probe(self, params=None):
                calls.append(1)
                if len(calls) <= 2:
                    raise OSError("device hiccup")
                return prepare(tone(220.0))

            def apply(self, params):
                pass

        session = self._session(FlakyDriver())
        params = space.decode(space.default_vector())
        # Both attempts fail -> penalty, not cached
        l1, clip1 = session._evaluate(params)
        assert l1 == SILENCE_PENALTY_LOSS and clip1 is None
        # Device recovered -> real evaluation happens (no stale penalty)
        l2, clip2 = session._evaluate(params)
        assert l2 < SILENCE_PENALTY_LOSS and clip2 is not None

    def test_retry_once_on_transient_error(self):
        space = ParamSpace(include_effects=False)
        calls = []

        class OnceFlaky:
            def probe(self, params=None):
                calls.append(1)
                if len(calls) == 1:
                    raise OSError("transient")
                return prepare(tone(220.0))

            def apply(self, params):
                pass

        session = self._session(OnceFlaky())
        l, clip = session._evaluate(space.decode(space.default_vector()))
        assert clip is not None and l < SILENCE_PENALTY_LOSS
        assert len(calls) == 2


class TestDistanceNormalization:
    def test_target_vs_target_terms_zero(self):
        feat = extract(prepare(tone(220.0, harmonics=3)))
        scales = reference_scales(feat)
        assert loss(feat, feat, Weights(), scales=scales) == pytest.approx(0.0, abs=1e-6)

    def test_target_vs_silence_terms_are_one(self):
        feat = extract(prepare(tone(220.0, harmonics=3)))
        scales = reference_scales(feat)
        sil = extract(silence())
        w = Weights()
        expected = w.logmel + w.mfcc + w.env + w.centroid + w.flatness
        assert loss(feat, sil, w, scales=scales) == pytest.approx(expected, rel=1e-3)

    def test_weights_now_express_relative_importance(self):
        feat = extract(prepare(tone(220.0, harmonics=3)))
        scales = reference_scales(feat)
        sil = extract(silence())
        only_env = Weights(logmel=0, mfcc=0, env=1.0, centroid=0, flatness=0)
        assert loss(feat, sil, only_env, scales=scales) == pytest.approx(1.0, rel=1e-3)


class TestCalibration:
    def test_calibrate_applies_bright_patch_before_note(self):
        from unittest.mock import MagicMock

        from synth.match.driver import CALIBRATION_PATCH, SynthDriver

        events = []
        midi = MagicMock()
        midi.send_cc.side_effect = lambda cc, v: events.append(("cc", cc, v))
        driver = SynthDriver(midi, device=None)

        def fake_record(duration, play):
            events.append(("record",))
            return prepare(tone(440.0))

        driver._record_window = fake_record
        driver.calibrate()
        assert ("cc", 74, 127) in events  # filter open
        assert ("cc", 73, 0) in events    # instant attack
        # the full calibration patch goes out before the note is recorded
        record_at = events.index(("record",))
        assert record_at >= len(CALIBRATION_PATCH)
        assert all(e[0] == "cc" for e in events[:record_at])


class TestCliGuards:
    def test_cli_refuses_empty_best(self, monkeypatch, capsys):
        from synth.match import cli

        class FakeSession:
            best_closeness = 0.0

            def __init__(self, driver, config):
                pass

            def load_target(self, path):
                pass

            def calibrate(self):
                return 0.0

            def run(self, on_progress=None):
                return {}

            def best_patch(self):
                return {}

            def stop(self):
                pass

        monkeypatch.setattr(
            cli.MidiBackend, "list_output_ports", staticmethod(lambda: ["S-1 MIDI IN"])
        )
        monkeypatch.setattr(cli.MidiBackend, "connect", lambda self, port: None)
        monkeypatch.setattr(cli, "MatchSession", FakeSession)
        monkeypatch.setattr(cli, "SynthDriver", lambda *a, **k: object())

        def no_save(*a, **k):
            raise AssertionError("must not save an empty patch")

        monkeypatch.setattr(cli, "save_patch", no_save)
        rc = cli.main(["target.wav", "--no-calibrate"])
        assert rc == 1
        assert "nothing to save" in capsys.readouterr().err
