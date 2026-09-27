"""Tests for ``synth-calibrate`` (``synth/match/calibrate_cli.py``).

Nothing here touches hardware: the dry run's pretend S-1 (``FakeRig``) plays every
probe. The two end-to-end tests are the contract: (1) plan -> probes -> stop -> resume
-> fit -> the exact curves JSON shape -> the report, with a clean patch restored and
nothing installed; (2) against a pretend S-1 whose curves are hidden but known, the fit
moves the curves to the right numbers and the held-out gap shrinks. Everything else pins
one piece: the plan's stratification, the preflight's fixes, the regression, and the raw
capture path staying identical to ``SynthDriver.probe``.
"""

from __future__ import annotations

import json
import math
from collections import Counter

import numpy as np
import pytest

import synth.match.calibrate_cli as C
from synth.match import WORKING_SR
from synth.match import driver as driver_mod
from synth.match.capture import AudioClip, prepare
from synth.match.driver import SynthDriver
from synth.match.twin import DEFAULT_CURVES, K_NAMES, K_PARAMS
from synth.schema import S1_PARAMS

FAST = ["--fit-sr", "8000", "--grid", "9", "--refine", "4", "--passes", "1", "--workers", "1", "--yes"]


# ── the plan ─────────────────────────────────────────────────────────────────
def test_default_plan_is_stratified():
    plan = C.make_plan()
    kinds = Counter(p.kind for p in plan)
    assert len(plan) == 300 and kinds == {"sweep": 162, "joint": 126, "repeat": 12}
    assert [p.i for p in plan] == list(range(300))
    assert plan[0].kind == plan[1].kind == "repeat"                 # warm-up + floor first
    every_cc = {p.cc for p in S1_PARAMS}
    k_cc = {kp.name: kp.cc for kp in K_PARAMS}
    for spec in C.SWEEPS:
        mine = [p for p in plan if p.curve == spec.curve]
        levels = sorted(p.level for p in mine)
        assert levels[0] == 0 and levels[-1] == 127 and len(set(levels)) == 9
        assert sum(p.holdout for p in mine) == 3
        for p in mine:
            assert p.cc[k_cc[spec.curve]] == p.level and p.note == spec.note
            for cc, v in spec.base.items():
                if cc != k_cc[spec.curve]:
                    assert p.cc[cc] == v
    for p in plan:
        assert set(p.cc) == every_cc                                   # every CC, every probe
    joints = [p for p in plan if p.kind == "joint"]
    assert Counter(p.note for p in joints) == {n: 18 for n in C.JOINT_NOTES}
    # Latin hypercube: each k param covers every tenth of its joint range.
    for kp in K_PARAMS:
        lo, hi = C.JOINT_RANGES[kp.name]
        if hi - lo < 0.2 or kp.name in ("saw_lvl", "square_lvl", "sub_lvl"):
            continue                                                   # audibility fixups move these
        u = [(p.cc[kp.cc] / 127.0 - lo) / (hi - lo) for p in joints]
        assert len({min(9, int(x * 10)) for x in u}) == 10, kp.name
    for p in joints:
        assert max(p.cc[20], p.cc[19], p.cc[21]) >= 45                 # always audible


def test_plan_is_deterministic_and_scales_down():
    a, b = C.make_plan(seed=3), C.make_plan(seed=3)
    assert [p.to_json() for p in a] == [p.to_json() for p in b]
    assert [p.to_json() for p in a] != [p.to_json() for p in C.make_plan(seed=4)]
    small = C.make_plan(24, curves=["cutoff", "attack"])
    assert Counter(p.kind for p in small) == {"sweep": 12, "joint": 10, "repeat": 2}
    assert {p.curve for p in small if p.kind == "sweep"} == {"cutoff", "attack"}
    with pytest.raises(ValueError, match="unknown curve"):
        C.make_plan(24, curves=["wobble"])


def test_probe_json_roundtrip():
    p = C.make_plan(24, curves=["cutoff"])[5]
    assert C.Probe.from_json(json.loads(json.dumps(p.to_json()))) == p


# ── the capture path ─────────────────────────────────────────────────────────
class _Midi:
    def __init__(self):
        self.events = []

    def send_cc(self, cc, value):
        self.events.append(("cc", cc, value))
        return True

    def send_note_on(self, note, velocity=100):
        self.events.append(("on", note))
        return True

    def send_note_off(self, note):
        self.events.append(("off", note))
        return True


class _Monitor:
    running = True

    def __init__(self, clip):
        self.clip = clip

    def begin_capture(self):
        pass

    def end_capture(self):
        return self.clip


def test_probe_driver_capture_is_probe_without_prepare(monkeypatch):
    """Drift test: ProbeDriver.capture() is SynthDriver.probe() minus prepare()."""
    monkeypatch.setattr(driver_mod.time, "sleep", lambda *_: None)
    rng = np.random.default_rng(0)
    raw = AudioClip((0.3 * rng.standard_normal(WORKING_SR * 2)).astype(np.float32), WORKING_SR)
    old = SynthDriver(_Midi(), device=None, monitor=_Monitor(raw), note=52)
    new = C.ProbeDriver(_Midi(), device=None, monitor=_Monitor(raw), note=52)
    old.latency_s = new.latency_s = 0.05
    probed = old.probe({74: 90})
    captured = new.capture({74: 90})
    assert captured.samples.size == raw.samples.size - int(0.05 * WORKING_SR)
    tail = raw.samples[int(0.05 * WORKING_SR):]
    assert np.abs(captured.samples).max() == pytest.approx(np.abs(tail).max())   # level intact
    assert np.array_equal(prepare(captured).samples, probed.samples)
    assert old.midi.events == new.midi.events


def test_hardware_rig_sends_only_changes_and_kills_the_tail(monkeypatch):
    monkeypatch.setattr(C.time, "sleep", lambda *_: None)
    sent: list[dict] = []

    class Driver:
        device, note = 3, 48

        def apply(self, params):
            sent.append(dict(params))

        def capture(self, params):
            return AudioClip(np.zeros(10, np.float32), WORKING_SR)

        def calibrate(self):
            sent.append({"calibrate": True})
            return 0.02

    class Midi:
        port_name = "S-1 MIDI IN"
        notes_off = 0

        def all_notes_off(self):
            self.notes_off += 1

        def disconnect(self):
            pass

    rig = C.HardwareRig(Driver(), Midi())
    base = dict(C.CALIBRATION_BASE)
    rig.capture(base, 36)
    assert sent[0] == base and sent[1] == {72: 0}                        # full first, then tail kill
    rig.capture({**base, 74: 10}, 48)
    assert sent[2] == {74: 10, 72: base[72]} and rig.driver.note == 48   # only what changed
    rig.capture({**base, 74: 10}, 48, full=True)
    assert sent[4] == {**base, 74: 10}                                   # a repeat re-sends it all
    assert rig.latency() == 0.02
    rig.capture(base, 48)
    assert sent[-2] == base                                              # after calibrate(): full again
    rig.restore()
    assert sent[-1] == C.CLEAN_PATCH and rig.midi.notes_off == 1


# ── preflight ────────────────────────────────────────────────────────────────
def test_preflight_moves_range_until_c3_is_c3():
    rig = C.FakeRig(kind="placeholder", range_neutral=3)                 # plays an octave low at 16'
    lines: list[str] = []
    pre = C.preflight(rig, lines.append)
    assert pre.range_cc14 == 3 and pre.overrides == {14: 3, 11: 127}
    assert abs(pre.cents) < 5 and pre.f0_hz == pytest.approx(C.midi_to_hz(48), rel=0.01)
    assert 0.01 < pre.latency_s < 0.03
    assert pre.floor_dbfs < -75 and pre.floor_samples is not None
    assert any("setting Range to 8'" in line for line in lines)
    assert "floor_samples" not in pre.to_json()


def test_preflight_stops_on_silence_and_on_ignored_ccs():
    class Silent(C.FakeRig):
        def _render(self, note):
            return np.zeros(int(C.RECORD_S * WORKING_SR))

    with pytest.raises(C.SetupError, match="made no sound"):
        C.preflight(Silent(), lambda _l: None)

    class Deaf(C.FakeRig):
        def _render(self, note):                                         # ignores every CC
            t = np.arange(int(C.RECORD_S * WORKING_SR)) / WORKING_SR
            return 0.3 * np.sin(2 * np.pi * C.midi_to_hz(note) * t)

    with pytest.raises(C.SetupError, match="not taking CCs"):
        C.preflight(Deaf(), lambda _l: None)


def test_preflight_lowers_expression_only_when_it_works():
    class Hot(C.FakeRig):
        def _render(self, note):
            return super()._render(note) * (self.state.get(11, 127) / 127.0) * 2.5

    pre = C.preflight(Hot(), lambda _l: None)
    assert pre.expression_cc11 < 127 and pre.peak_dbfs < -3
    hot_deaf = C.FakeRig(gain=0.6)                                       # CC11 changes nothing
    pre2 = C.preflight(hot_deaf, lambda _l: None)
    assert pre2.expression_cc11 == 127
    assert any("does not change the S-1's level" in w for w in pre2.warnings)


# ── the fit's building blocks ────────────────────────────────────────────────
def test_regress_curve_recovers_linear_and_exp_and_refuses_flat():
    lin = C.SWEEP_BY_CURVE["resonance"]
    pts = [{"k": k, "x": 0.2 + 3.0 * k, "weight": 1.0, "ok": True} for k in (0, 0.25, 0.5, 0.75, 1)]
    curve, info = C.regress_curve(lin, pts, (0.0, 3.8, "linear", "u"))
    assert curve[0] == pytest.approx(0.2) and curve[1] == pytest.approx(3.2) and curve[2] == "linear"
    assert info["r2"] == pytest.approx(1.0)
    expo = C.SWEEP_BY_CURVE["cutoff"]
    pts = [{"k": k, "x": 40.0 * (8000 / 40.0) ** k, "weight": 1.0, "ok": True} for k in (0.1, 0.4, 0.7)]
    pts.append({"k": 1.0, "x": 99999.0, "weight": 1.0, "ok": False})     # a censored point is ignored
    curve, info = C.regress_curve(expo, pts, (30.0, 12000.0, "exp", "Hz"))
    assert curve[0] == pytest.approx(40.0, rel=1e-6) and curve[1] == pytest.approx(8000.0, rel=1e-6)
    assert info["identified"] == 3 and info["points"] == 4
    flat = [{"k": k, "x": 1.0 + 0.001 * k, "weight": 1.0, "ok": True} for k in (0, 0.5, 1)]
    curve, info = C.regress_curve(lin, flat, (0.0, 3.8, "linear", "u"))
    assert curve is None and info["why"] == "no measurable response to this knob"
    curve, info = C.regress_curve(lin, pts[:1], (0.0, 3.8, "linear", "u"))
    assert curve is None and info["why"] == "too few measurable points"


def test_curves_payload_has_the_exact_shape_and_loads_back(tmp_path):
    payload = C.curves_payload(C.default_curve_tuples(), "synth-calibrate 2026-09-28T10-00-00")
    assert list(payload) == ["version", "calibrated", "source", "curves"]
    assert payload["version"] == 1 and payload["calibrated"] is True
    assert list(payload["curves"]) == list(K_NAMES)
    for name, c in payload["curves"].items():
        assert list(c) == ["lo", "hi", "kind", "unit"]
        assert isinstance(c["lo"], float) and isinstance(c["hi"], float)
        assert c["kind"] in ("linear", "exp") and c["unit"] == DEFAULT_CURVES[name].unit
    path = tmp_path / "curves.json"
    path.write_text(json.dumps(payload))
    mapping = C.load_curves(path)
    for name, cur in DEFAULT_CURVES.items():
        got = mapping.curve(name)
        assert got.lo == pytest.approx(cur.lo) and got.hi == pytest.approx(cur.hi) and got.kind == cur.kind
    dest = tmp_path / "twin" / "curves.calibrated.json"
    C.install_curves(payload, dest)
    C.install_curves({**payload, "source": "second"}, dest)
    assert json.loads(dest.read_text())["source"] == "second"
    previous = dest.with_name("curves.calibrated.previous.json")
    assert json.loads(previous.read_text())["source"] == payload["source"]


def test_time_estimates():
    assert C.estimate_seconds(300, 2.4) == pytest.approx(720.0)
    real = C.hardware_probe_seconds(0.03)
    assert 2.3 < real < 2.6                                            # settle + note + tail + I/O
    assert C._fmt_minutes(720) == "12 min" and C._fmt_minutes(40) == "40 s"


# ── the command, end to end, on a pretend S-1 ────────────────────────────────
@pytest.fixture
def rigs(monkeypatch):
    made: list[C.FakeRig] = []

    class Tracked(C.FakeRig):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            made.append(self)

    monkeypatch.setattr(C, "FakeRig", Tracked)
    return made


def test_dry_run_plan_probe_stop_resume_fit_report(tmp_path, monkeypatch, rigs):
    installed = tmp_path / "home-twin" / "curves.calibrated.json"
    monkeypatch.setattr(C, "default_curves_out", lambda: installed)
    root = tmp_path / "cal"
    args = ["--dry-run", "--curves", "cutoff,resonance", "--probes", "24", "--root", str(root), *FAST]

    lines: list[str] = []
    assert C.main([*args, "--stop-after", "5"], emit=lines.append) == 130
    (run_dir,) = list(root.iterdir())
    assert run_dir.name.endswith("-dry-run")
    assert len(C.read_rows(run_dir)) == 5
    assert rigs[0].restored == C.CLEAN_PATCH and rigs[0].closed           # clean patch on a stop too
    assert any(f"--resume {run_dir}" in line for line in lines)
    assert any(line.startswith("Estimate: 24 probes x ") for line in lines)
    assert any(line.startswith("On the real S-1: 24 probes x ") for line in lines)
    assert any(line.startswith("Before we start:") for line in lines)

    lines.clear()
    assert C.main(["--resume", str(run_dir), *FAST], emit=lines.append) == 0
    rows = C.read_rows(run_dir)
    assert sorted(r["i"] for r in rows) == list(range(24))               # nothing twice, nothing lost
    assert rigs[1].captures == 19 + 3                                     # the 19 left + preflight
    assert rigs[1].restored == C.CLEAN_PATCH
    for name in ("plan.json", "meta.json", "probes.jsonl", "floor.wav", "report.json", "report.md",
                 "curves.calibrated.json"):
        assert (run_dir / name).is_file(), name
    assert len(list((run_dir / "probes").glob("*.wav"))) == 24
    assert len(json.loads((run_dir / "meta.json").read_text())["sessions"]) == 2

    curves = json.loads((run_dir / "curves.calibrated.json").read_text())
    assert list(curves) == ["version", "calibrated", "source", "curves"]
    assert curves["version"] == 1 and curves["calibrated"] is False       # a dry run never claims it
    assert curves["source"].startswith("synth-calibrate --dry-run ")
    assert list(curves["curves"]) == list(K_NAMES)
    for c in curves["curves"].values():
        assert list(c) == ["lo", "hi", "kind", "unit"] and c["kind"] in ("linear", "exp")
    assert not installed.exists()                                         # nothing installed

    report = json.loads((run_dir / "report.json").read_text())
    assert report["dry_run"] is True and report["fake"] == "placeholder" and report["installed"] is None
    assert report["probes"]["recorded"] == 24
    assert report["curves"]["resonance"]["status"] == "kept"              # the placeholder has none
    assert report["curves"]["resonance"]["why"] == "no measurable response to this knob"
    assert report["curves"]["attack"]["status"] == "not swept"
    assert report["gap"]["joint"]["n"] > 0 and report["repeat_spread"]["n"] == 1
    assert report["hiss"]["measured"] is True
    for v in report["calibrate_seam"]["by_note"].values():               # the seam agrees with us
        assert v["held_out_feature_error"] == pytest.approx(v["this_report"], abs=1e-9)
    md = (run_dir / "report.md").read_text()
    assert "**Dry run**" in md and "## Held-out gap by module" in md and "Filter cutoff" in md
    assert any(line.startswith("On ") and "held-out joint probes" in line for line in lines)


def test_fit_recovers_the_hidden_curves_of_a_pretend_s1(tmp_path, rigs):
    root = tmp_path / "cal"
    args = ["--dry-run", "--fake", "twin", "--curves", "cutoff,attack", "--probes", "30",
            "--root", str(root), "--fit-sr", "11025", "--grid", "13", "--refine", "6",
            "--passes", "1", "--workers", "1", "--yes"]
    assert C.main(args, emit=lambda _l: None) == 0
    (run_dir,) = list(root.iterdir())
    report = json.loads((run_dir / "report.json").read_text())
    truth = C.FAKE_TRUE_CURVES
    cut = report["curves"]["cutoff"]
    att = report["curves"]["attack"]
    assert cut["status"] == "fitted" and att["status"] == "fitted"
    assert cut["fitted"]["lo"] == pytest.approx(truth["cutoff"][0], rel=0.2)
    assert cut["fitted"]["hi"] == pytest.approx(truth["cutoff"][1], rel=0.3)
    assert att["fitted"]["lo"] == pytest.approx(truth["attack"][0], rel=0.35)
    assert att["fitted"]["hi"] == pytest.approx(truth["attack"][1], rel=0.25)
    for module in ("filter", "env"):
        g = report["gap"]["modules"][module]
        assert g["after"] < g["before"], (module, g)
    # the defaults really were wrong for this pretend S-1, so there was something to fix
    assert not math.isclose(DEFAULT_CURVES["cutoff"].hi, truth["cutoff"][1], rel_tol=0.2)


def test_check_without_an_s1_says_what_to_do(monkeypatch):
    import synth.match.cli as match_cli

    monkeypatch.setattr(match_cli, "_auto_port", lambda _p: None)
    monkeypatch.setattr(C, "cockpit_running", lambda port=None: False)
    lines: list[str] = []
    assert C.main(["--check"], emit=lines.append) == 2
    assert any(line.startswith("Problem: No S-1 MIDI port found") and "data cable" in line for line in lines)


def test_check_on_the_pretend_s1_passes(rigs):
    lines: list[str] = []
    assert C.main(["--check", "--dry-run"], emit=lines.append) == 0
    assert lines[-1] == "Ready for calibration."
    assert rigs[0].restored == C.CLEAN_PATCH


# ── phase-free LFO measurements ──────────────────────────────────────────────
def _tone(seconds: float = C.RECORD_S, sr: int = WORKING_SR):
    return np.arange(int(seconds * sr)) / sr, sr


def test_dominant_rate_reads_the_brightness_wobble_and_its_phase():
    t, sr = _tone()
    for rate in (2.0, 7.5):
        bright = 1.0 + 0.8 * np.sin(2 * np.pi * rate * t)                 # starts at 0, rising
        x = np.sin(2 * np.pi * 220 * t) + 0.5 * bright * np.sin(2 * np.pi * 2200 * t)
        hz, evr, phase = C.dominant_rate(x, sr)
        assert hz == pytest.approx(rate, rel=0.03) and evr > 0.8
        assert abs(phase) < 25.0
    slow = np.sin(2 * np.pi * 220 * t) * (1.0 + 0.5 * np.sin(2 * np.pi * 0.4 * t))
    assert C.dominant_rate(slow, sr)[0] is None                           # under 1.2 cycles: no answer


def test_vibrato_extent_and_brightness_swing():
    t, sr = _tone()
    semis = 2.0 * np.sin(2 * np.pi * 3.0 * t)
    phase = 2 * np.pi * np.cumsum(392.0 * 2.0 ** (semis / 12.0)) / sr
    assert C.vibrato_extent(np.sin(phase), sr) == pytest.approx(2.0, abs=0.2)
    assert C.vibrato_extent(np.sin(2 * np.pi * 392.0 * t), sr) == pytest.approx(0.0, abs=0.05)
    steady = np.sin(2 * np.pi * 220 * t) + 0.5 * np.sin(2 * np.pi * 2200 * t)
    wobble = (np.sin(2 * np.pi * 220 * t)
              + 0.5 * (1 + 0.9 * np.sin(2 * np.pi * 3 * t)) * np.sin(2 * np.pi * 2200 * t))
    assert C.brightness_swing(steady, sr) < 0.02 < C.brightness_swing(wobble, sr)


def test_lfo_rate_curve_is_measured_directly(tmp_path, rigs):
    root = tmp_path / "cal"
    assert C.main(["--dry-run", "--fake", "twin", "--curves", "lfo_rate", "--probes", "20",
                   "--root", str(root), *FAST], emit=lambda _l: None) == 0
    (run_dir,) = list(root.iterdir())
    rate = json.loads((run_dir / "report.json").read_text())["curves"]["lfo_rate"]
    lo, hi = C.FAKE_TRUE_CURVES["lfo_rate"][:2]
    assert rate["status"] == "fitted"
    assert rate["fitted"]["lo"] == pytest.approx(lo, rel=0.25)
    assert rate["fitted"]["hi"] == pytest.approx(hi, rel=0.15)
    assert abs(rate["lfo_phase_deg"]) < 30.0                             # the twin starts at 0, rising
