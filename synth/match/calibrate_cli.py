"""``synth-calibrate`` — measure the real S-1 and fit the twin's curves to it.

FABLE: *"Budget real effort for calibration: measure the actual CC→Hz and CC→seconds
curves from hardware, fit per-module correction terms, and always report
twin-vs-hardware error on a held-out probe set."* This command is that session, end
to end. It is what makes the word "twin" true.

1. **Preflight.** Find the S-1 (MIDI port + USB audio input), measure note→sound
   latency with the driver's own :meth:`~synth.match.driver.SynthDriver.calibrate`,
   check the pitch (move Range by an octave if the S-1 plays an octave off) and the
   level (lower Expression if a plain saw leaves too little headroom).
2. **Probe.** Play a stratified plan through the existing capture path
   (:class:`~synth.match.driver.SynthDriver`, at each probe's own pitch):

   * *sweeps* — one knob at a time across its whole range, around a plain patch
     chosen so that knob is audible (the measurement);
   * *joint* probes — a Latin hypercube over the whole k-space at several pitches and
     every modeled switch setting (never used by the fit: the held-out test);
   * *repeats* — one fixed patch, again and again (the hardware's own noise floor).

   Each probe is saved the moment it lands (``probes/NNNN.wav`` + one line in
   ``probes.jsonl``), so a stopped run continues with ``--resume <dir>``.
3. **Fit.** For each sweep probe, find the physical value (Hz, seconds, level …) that
   makes the twin sound most like the capture — a 1-D search over a wide range — then
   regress those measured points onto the curve's shape (``linear`` or ``exp``). Curves
   are fitted in dependency stages (pulse width before square level, cutoff before key
   follow …) and the whole fit runs twice (``--passes``) so early curves see later
   corrections. A curve whose fit does not beat the default on its own held-out sweep
   probes is put back to the default.
4. **Report.** The held-out feature gap, default curves vs fitted curves, per module
   and on the joint probes, in the plain metric :func:`synth.match.twin.calibrate` uses
   (and that seam is called on the joint probes as a cross-check). Next to it: the
   repeat probes' own spread — the floor no twin can beat.
5. **Write** ``curves.calibrated.json`` (the browser twin reads it; exact shape in
   :func:`curves_payload`), ``report.json`` and ``report.md``; restore a clean patch
   (the schema's init values) on the S-1.

``--dry-run`` runs every step with no hardware against a pretend S-1:
``--fake placeholder`` renders with :func:`synth.match.corpus.render_placeholder` (a
deliberately different synth: most knobs do nothing, which exercises the "no
measurable response" path) and ``--fake twin`` renders with the twin under hidden,
known curves (:data:`FAKE_TRUE_CURVES`) — the fit should recover them. A dry run never
writes ``~/.synth/twin/curves.calibrated.json``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import numpy as np

from ..paths import data_dir
from ..schema import S1_PARAMS, param_by_cc
from . import WORKING_SR
from .capture import AudioClip, find_onset, prepare
from .driver import CALIBRATION_PATCH, SETTLE_S, SynthDriver

# ─────────────────────────────────────────────────────────────────────────────
# Timing: one probe = the driver's standard note (hold) + its release window (tail).
# The twin renders the same window: note-off at HOLD_S, total RECORD_S.
# ─────────────────────────────────────────────────────────────────────────────
HOLD_S = 1.2
TAIL_S = 1.0
RECORD_S = HOLD_S + TAIL_S
GATE_FRACTION = HOLD_S / RECORD_S
TAIL_KILL_S = 0.03          # after a probe: release -> 0, then this pause, so no tail bleeds on
PITCH_NOTE = 48             # C3: the pitch/level check note (the S-1 is a bass synth)
DEFAULT_PROBES = 300

SILENT_PEAK = 10.0 ** (-60.0 / 20.0)    # raw peak below -60 dBFS: the probe made no sound
CLIP_PEAK = 0.99                        # raw peak at or above this: the converter clipped
HEADROOM_PEAK = 10.0 ** (-9.0 / 20.0)   # a plain saw should peak below -9 dBFS (mixes get louder)
QUIET_PEAK = 10.0 ** (-40.0 / 20.0)     # below this the check says "turn it up"
EXPRESSION_STEPS = (127, 96, 72)        # CC11 values tried when the plain saw is too hot

RANGE_LABELS = ("64'", "32'", "16'", "8'", "4'", "2'")   # CC14 option index -> label

# The patch the S-1 is left on: every parameter at the schema's default (the init
# patch, including the sanctioned 64s for the chord-voice key shifts).
CLEAN_PATCH: dict[int, int] = {p.cc: int(p.default) for p in S1_PARAMS}

# The calibration base: a plain, bright, sustained saw with every feature the twin does
# NOT model switched off, and every one it does model at a neutral value. Each sweep
# overrides a few of these (see SWEEPS); joint probes override all the k/s CCs.
_BASE_OVERRIDES: dict[int, int] = {
    # oscillator: one saw, static pulse width (PWM source = Manual), white noise, no draw
    20: 127, 19: 0, 21: 0, 23: 0, 15: 64, 16: 1, 22: 2, 78: 1, 107: 0, 76: 64, 14: 2,
    # filter: open, no modulation, no key follow
    74: 127, 71: 0, 24: 0, 25: 0, 26: 0,
    # amp + envelope: envelope mode, instant attack, full sustain, short release
    28: 1, 29: 2, 73: 0, 75: 64, 30: 127, 72: 16,
    # LFO: triangle, normal range, no sync, restarts on every key (a repeatable phase),
    # full mod depth — it only reaches the sound where a sweep routes it (CC13 / CC25)
    3: 64, 12: 2, 79: 0, 106: 0, 105: 1, 13: 0, 17: 127,
    # voicing: one voice, no glide, no transpose
    80: 0, 65: 0, 31: 0, 77: 64,
    # effects off
    92: 0, 91: 0, 93: 0,
    # performance CCs neutral
    1: 0, 64: 0, 11: 127,
}
CALIBRATION_BASE: dict[int, int] = {**CLEAN_PATCH, **_BASE_OVERRIDES}

# The repeat probe: a plain saw with a little filter, at C3. Its spread is the floor.
REPEAT_PATCH: dict[int, int] = {**CALIBRATION_BASE, 74: 90, 30: 100}

MODULE_LABELS = {"osc": "Oscillator", "filter": "Filter", "env": "Envelope", "lfo": "LFO"}
CURVE_LABELS = {
    "saw_lvl": "Saw level", "square_lvl": "Square level", "sub_lvl": "Sub level",
    "noise_lvl": "Noise level", "pulse_width": "Pulse width", "fine_tune": "Fine tune",
    "cutoff": "Filter cutoff", "resonance": "Resonance", "key_follow": "Key follow",
    "env_to_cutoff": "Filter envelope depth", "lfo_to_cutoff": "Filter LFO depth",
    "attack": "Attack", "decay": "Decay", "sustain": "Sustain", "release": "Release",
    "lfo_rate": "LFO rate", "lfo_to_pitch": "Pitch LFO depth", "lfo_depth": "LFO mod depth",
}


# ─────────────────────────────────────────────────────────────────────────────
# What each sweep measures
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SweepSpec:
    """One curve's measurement: sweep its CC over 0..127 around ``base``.

    ``search`` is the wide physical range the per-probe inversion searches
    (``(lo, hi, kind)``); ``physical`` says whether each end of that range is a real
    bound (a level cannot go below 0, a sustain cannot pass 1) — an answer pinned to a
    NON-physical end means the true value lies outside the range, so that point is
    dropped. ``stage`` orders the fit: a stage may lean on curves from earlier stages.
    ``method`` is ``"twin"`` (1-D search through the twin), ``"pitch"`` (fine tune: read
    straight off the detected f0) or ``"rate"`` (LFO rate: the dominant modulation
    frequency of the brightness track). ``objective`` says what the twin search matches:
    ``"metric"`` (the plain feature distance), or — for LFO depths, where a time-aligned
    comparison would punish any LFO phase difference — a phase-free measurement taken
    the same way on the capture and on the twin: ``"vibrato"`` (pitch swing, semitones)
    or ``"swing"`` (brightness swing, octaves). ``fold`` maps duty d and 1-d together
    (their spectra are identical)."""

    curve: str
    module: str
    stage: int
    note: int
    base: dict[int, int]
    search: tuple[float, float, str]
    physical: tuple[bool, bool] = (False, False)
    method: str = "twin"
    fold: bool = False
    objective: str = "metric"


SWEEPS: tuple[SweepSpec, ...] = (
    # stage 1 — measurable on the plain saw alone
    SweepSpec("fine_tune", "osc", 1, 48, {}, (-300.0, 300.0, "linear"), method="pitch"),
    SweepSpec("pulse_width", "osc", 1, 48, {20: 0, 19: 127}, (0.01, 0.6, "linear"), fold=True),
    SweepSpec("cutoff", "filter", 1, 36, {}, (8.0, 24000.0, "exp")),
    SweepSpec("attack", "env", 1, 48, {}, (0.0003, 8.0, "exp")),
    SweepSpec("release", "env", 1, 48, {}, (0.001, 15.0, "exp")),
    # The rate is read from a vibrato (pitch is rendered per sample by both the S-1 and the
    # twin; the twin's filter follows an LFO only every 20 ms) with a long release, so the
    # measuring window runs to ~2 s and slow rates still show 1.2 cycles.
    SweepSpec("lfo_rate", "lfo", 1, 48, {13: 40, 72: 110}, (0.02, 80.0, "exp"), method="rate"),
    # stage 2 — lean on stage-1 curves (pulse width, cutoff, attack)
    SweepSpec("square_lvl", "osc", 2, 48, {20: 127, 15: 64}, (0.0, 2.5, "linear"), (True, False)),
    SweepSpec("sub_lvl", "osc", 2, 48, {20: 127}, (0.0, 2.5, "linear"), (True, False)),
    SweepSpec("noise_lvl", "osc", 2, 48, {20: 127}, (0.0, 1.5, "linear"), (True, False)),
    SweepSpec("resonance", "filter", 2, 36, {74: 64}, (0.0, 3.98, "linear"), (True, True)),
    SweepSpec("key_follow", "filter", 2, 36, {74: 90}, (0.0, 2.0, "linear"), (True, False)),
    SweepSpec("decay", "env", 2, 48, {30: 0}, (0.001, 15.0, "exp")),
    SweepSpec("sustain", "env", 2, 48, {75: 30}, (0.0, 1.0, "linear"), (True, True)),
    # stage 3 — lean on stage-2 curves (sub level, decay/sustain, LFO rate)
    SweepSpec("saw_lvl", "osc", 3, 48, {21: 127}, (0.0, 2.5, "linear"), (True, False)),
    SweepSpec("env_to_cutoff", "filter", 3, 36, {74: 20, 30: 0, 75: 64}, (0.0, 10.0, "linear"),
              (True, False)),
    SweepSpec("lfo_to_cutoff", "filter", 3, 36, {74: 50, 3: 80}, (0.0, 8.0, "linear"), (True, False),
              objective="swing"),
    SweepSpec("lfo_to_pitch", "lfo", 3, 67, {3: 80}, (0.0, 24.0, "linear"), (True, False),
              objective="vibrato"),
    # stage 4 — scales every LFO route, so it goes last
    SweepSpec("lfo_depth", "lfo", 4, 67, {13: 64, 3: 80}, (0.0, 2.5, "linear"), (True, False),
              objective="vibrato"),
)
SWEEP_BY_CURVE = {s.curve: s for s in SWEEPS}

# Joint probes sample each k param inside the part of its range real patches use, so
# they stay audible and musical; every modeled switch value and seven pitches appear.
JOINT_RANGES: dict[str, tuple[float, float]] = {
    "saw_lvl": (0.0, 1.0), "square_lvl": (0.0, 1.0), "sub_lvl": (0.0, 1.0), "noise_lvl": (0.0, 0.6),
    "pulse_width": (0.0, 1.0), "cutoff": (0.25, 1.0), "resonance": (0.0, 0.85),
    "env_to_cutoff": (0.0, 0.8), "lfo_to_cutoff": (0.0, 0.5), "key_follow": (0.0, 1.0),
    "attack": (0.0, 0.6), "decay": (0.1, 1.0), "sustain": (0.0, 1.0), "release": (0.0, 0.8),
    "lfo_rate": (0.1, 0.9), "lfo_to_pitch": (0.0, 0.25), "lfo_depth": (0.0, 1.0),
    "fine_tune": (0.3, 0.7),
}
JOINT_S: dict[str, tuple[int, ...]] = {
    "sub_octave": (0, 1, 2), "lfo_shape": (2, 3, 0, 1), "amp_env_mode": (0, 1),
}
JOINT_NOTES = (36, 40, 43, 48, 52, 55, 60)

# The pretend S-1 for ``--dry-run --fake twin``: the twin with these hidden curves.
# Each differs from DEFAULT_CURVES the way a real device might (a narrower cutoff
# range, slower envelopes, a reversed pulse-width knob …), so a fit that works must
# move every curve toward these numbers.
FAKE_TRUE_CURVES: dict[str, tuple[float, float, str, str]] = {
    "saw_lvl": (0.0, 1.0, "linear", "amp"),
    "square_lvl": (0.0, 0.8, "linear", "amp"),
    "sub_lvl": (0.0, 1.3, "linear", "amp"),
    "noise_lvl": (0.0, 0.35, "linear", "amp"),
    "pulse_width": (0.5, 0.08, "linear", "duty"),
    "cutoff": (45.0, 9000.0, "exp", "Hz"),
    "resonance": (0.0, 3.2, "linear", "ladder k (4==self-osc)"),
    "env_to_cutoff": (0.0, 5.0, "linear", "octaves"),
    "lfo_to_cutoff": (0.0, 3.0, "linear", "octaves"),
    "key_follow": (0.0, 0.8, "linear", "oct/oct"),
    "attack": (0.002, 3.0, "exp", "s"),
    "decay": (0.01, 6.0, "exp", "s"),
    "sustain": (0.0, 1.0, "linear", "level"),
    "release": (0.01, 3.0, "exp", "s"),
    "lfo_rate": (0.08, 20.0, "exp", "Hz"),
    "lfo_to_pitch": (0.0, 7.0, "linear", "semitones"),
    "lfo_depth": (0.0, 1.0, "linear", "amt"),
    "fine_tune": (-50.0, 50.0, "linear", "cents"),
}


class SetupError(RuntimeError):
    """Something outside the code is not ready (S-1 missing, silent, clipping …).
    The message says what happened and what to do."""


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────
def midi_to_hz(note: float) -> float:
    return 440.0 * 2.0 ** ((float(note) - 69.0) / 12.0)


def _cc_from_k(cc: int, k: float) -> int:
    """Normalized k -> integer CC value, with the same formula as ``Twin.k_to_cc``."""
    p = param_by_cc(cc)
    lo, hi = (p.min_val, p.max_val) if p else (0, 127)
    return int(round(lo + float(np.clip(k, 0.0, 1.0)) * (hi - lo)))


def _k_of(cc: int, value: int) -> float:
    """Integer CC value -> normalized k, with the same formula as ``Twin.cc_to_k``."""
    p = param_by_cc(cc)
    lo, hi = (p.min_val, p.max_val) if p else (0, 127)
    return float(np.clip((int(value) - lo) / max(1, hi - lo), 0.0, 1.0))


def s_from_cc(cc: dict[int, int]) -> dict[str, int]:
    """The twin's discrete s config read from a CC vector — exactly as
    :func:`synth.match.twin.calibrate` reads it."""
    from .twin import S_PARAMS, _default_s

    return {sp.name: int(cc.get(sp.cc, _default_s()[sp.name])) for sp in S_PARAMS}


def _dbfs(x: float) -> float:
    return 20.0 * math.log10(max(float(x), 1e-9))


def _fmt_minutes(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} s"
    return f"{seconds / 60.0:.0f} min"


def default_root() -> Path:
    return data_dir() / "calibration"


def default_curves_out() -> Path:
    return data_dir() / "twin" / "curves.calibrated.json"


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H-%M-%S")


# ─────────────────────────────────────────────────────────────────────────────
# The probe plan
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Probe:
    """One planned probe. ``cc`` is the full CC vector (the calibration base with this
    probe's overrides); preflight corrections (Range, Expression) ride on top of it."""

    i: int
    kind: str                       # "sweep" | "joint" | "repeat"
    note: int
    cc: dict[int, int]
    curve: str | None = None        # sweeps: the curve this probe measures
    level: int | None = None        # sweeps: the swept CC value
    holdout: bool = False           # sweeps: kept out of the fit, used to measure the gap

    def to_json(self) -> dict:
        d = asdict(self)
        d["cc"] = {str(k): int(v) for k, v in self.cc.items()}
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Probe":
        return cls(i=int(d["i"]), kind=d["kind"], note=int(d["note"]),
                   cc={int(k): int(v) for k, v in d["cc"].items()},
                   curve=d.get("curve"), level=d.get("level"), holdout=bool(d.get("holdout")))


def sweep_specs(curves: Iterable[str] | None = None) -> list[SweepSpec]:
    if curves is None:
        return list(SWEEPS)
    wanted = set(curves)
    unknown = wanted - set(SWEEP_BY_CURVE)
    if unknown:
        raise ValueError(f"unknown curve(s): {', '.join(sorted(unknown))}; "
                         f"choose from {', '.join(SWEEP_BY_CURVE)}")
    return [s for s in SWEEPS if s.curve in wanted]


def _latin_hypercube(n: int, d: int, rng: np.random.Generator) -> np.ndarray:
    """``n`` points in [0,1]^d with exactly one point per 1/n stratum in every dimension."""
    if n <= 0:
        return np.zeros((0, d))
    edges = np.arange(n, dtype=np.float64) / n
    pts = edges[:, None] + rng.uniform(size=(n, d)) / n
    for j in range(d):
        pts[:, j] = pts[rng.permutation(n), j]
    return pts


def plan_counts(n_probes: int, n_curves: int) -> tuple[int, int, int]:
    """(levels per sweep, joint probes, repeats) for a probe budget.

    About 54% of the budget measures (sweeps: 3..17 levels per curve), about 4% repeats
    the noise-floor patch (at least 2), and the rest are joint held-out probes."""
    n_repeat = max(2, n_probes // 25)
    levels = int(np.clip(round(0.54 * n_probes / max(1, n_curves)), 3, 17))
    n_joint = max(0, n_probes - n_repeat - levels * n_curves)
    return levels, n_joint, n_repeat


def make_plan(n_probes: int = DEFAULT_PROBES, seed: int = 0,
              curves: Iterable[str] | None = None) -> list[Probe]:
    """The stratified probe plan. Deterministic for a seed.

    Sweeps (per curve, 0..127 in even steps; every third level held out), joint probes
    (a Latin hypercube over every k param inside :data:`JOINT_RANGES`, switch values and
    pitches balanced), and repeats of :data:`REPEAT_PATCH` (two first, the rest spread
    evenly). Everything but the repeats is shuffled, so any prefix of the plan is itself
    a spread-out sample — a run stopped halfway can still be fitted (``--fit-only``)."""
    from .twin import K_PARAMS, S_PARAMS

    specs = sweep_specs(curves)
    rng = np.random.default_rng(seed)
    levels, n_joint, n_repeat = plan_counts(n_probes, len(specs))
    k_cc = {kp.name: kp.cc for kp in K_PARAMS}

    body: list[Probe] = []
    for spec in specs:
        values = np.unique(np.round(np.linspace(0, 127, levels)).astype(int))
        for j, v in enumerate(values):
            cc = {**CALIBRATION_BASE, **spec.base, k_cc[spec.curve]: int(v)}
            body.append(Probe(i=-1, kind="sweep", note=spec.note, cc=cc, curve=spec.curve,
                              level=int(v), holdout=(j % 3 == 1)))

    u = _latin_hypercube(n_joint, len(K_PARAMS), rng)
    s_orders = {name: rng.permutation(max(1, n_joint)) for name in JOINT_S}
    note_order = rng.permutation(max(1, n_joint))
    for r in range(n_joint):
        cc = dict(CALIBRATION_BASE)
        for d, kp in enumerate(K_PARAMS):
            lo, hi = JOINT_RANGES[kp.name]
            cc[kp.cc] = _cc_from_k(kp.cc, lo + (hi - lo) * u[r, d])
        oscs = (20, 19, 21)                         # saw, square, sub: keep one audible
        if max(cc[c] for c in oscs) < 45:
            cc[oscs[r % 3]] = int(rng.integers(64, 128))
        for sp in S_PARAMS:
            choices = JOINT_S[sp.name]
            cc[sp.cc] = int(choices[int(s_orders[sp.name][r]) % len(choices)])
        note = JOINT_NOTES[int(note_order[r]) % len(JOINT_NOTES)]
        body.append(Probe(i=-1, kind="joint", note=int(note), cc=cc))

    order = rng.permutation(len(body))
    body = [body[j] for j in order]

    def repeat() -> Probe:
        return Probe(i=-1, kind="repeat", note=PITCH_NOTE, cc=dict(REPEAT_PATCH))

    plan: list[Probe] = [repeat(), repeat()]
    rest = n_repeat - 2
    every = math.ceil(len(body) / (rest + 1)) if rest > 0 and body else 0
    placed = 2
    for j, p in enumerate(body, 1):
        plan.append(p)
        if every and placed < n_repeat and j % every == 0:
            plan.append(repeat())
            placed += 1
    while placed < n_repeat:
        plan.append(repeat())
        placed += 1
    for i, p in enumerate(plan):
        p.i = i
    return plan


# ─────────────────────────────────────────────────────────────────────────────
# The rigs: what the probe loop talks to (the real S-1, or a pretend one)
# ─────────────────────────────────────────────────────────────────────────────
class ProbeDriver(SynthDriver):
    """:class:`SynthDriver` that returns the RAW capture (level intact).

    :meth:`SynthDriver.probe` returns ``prepare(clip)``, which peak-normalizes — so a
    silent or clipped probe can no longer be told apart. :meth:`capture` is the same
    capture path (monitor or own stream, latency trim) minus the ``prepare``; the test
    suite asserts ``prepare(capture(p)) == probe(p)`` so the two cannot drift."""

    def capture(self, params: dict[int, int] | None = None) -> AudioClip:
        if params is not None:
            self.apply(params)
            time.sleep(SETTLE_S)
        if self._use_monitor:
            self.monitor.begin_capture()
            self._play_note()
            clip = self.monitor.end_capture()
        else:
            clip = self._record_window(self.latency_s + self.hold_s + self.tail_s, play=True)
        skip = int(self.latency_s * self.sr)
        if skip:
            clip = AudioClip(clip.samples[skip:], clip.samplerate)
        return clip


class HardwareRig:
    """The real S-1: a :class:`ProbeDriver` plus its MIDI connection.

    Sends only the CCs that changed since the last probe (a repeat probe re-sends the
    whole vector, which heals any dropped message), and after every capture drops
    Release to 0 so a long tail cannot bleed into the next probe."""

    fake: str | None = None

    def __init__(self, driver: ProbeDriver, midi: Any) -> None:
        self.driver = driver
        self.midi = midi
        self._sent: dict[int, int] = {}
        self.describe = f"MIDI {getattr(midi, 'port_name', '?')} · audio input {driver.device}"

    def _send(self, cc: dict[int, int], full: bool = False) -> None:
        diff = dict(cc) if (full or not self._sent) else {
            c: v for c, v in cc.items() if self._sent.get(c) != v}
        if diff:
            self.driver.apply(diff)
            self._sent.update(diff)

    def latency(self) -> float:
        lat = float(self.driver.calibrate())
        self._sent.clear()          # calibrate() applied its own patch; state unknown now
        return lat

    def capture(self, cc: dict[int, int], note: int, full: bool = False) -> AudioClip:
        self._send(cc, full=full)
        time.sleep(SETTLE_S)
        self.driver.note = int(note)
        clip = self.driver.capture(None)
        self.driver.apply({72: 0})
        self._sent[72] = 0
        time.sleep(TAIL_KILL_S)
        return clip

    def restore(self, patch: dict[int, int] = CLEAN_PATCH) -> None:
        try:
            self.midi.all_notes_off()
            self.driver.apply(dict(patch))
            self._sent = dict(patch)
        except Exception:  # noqa: BLE001 - the device may already be gone
            pass

    def close(self) -> None:
        try:
            import sounddevice as sd

            sd.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.midi.disconnect()
        except Exception:  # noqa: BLE001
            pass


class FakeRig:
    """A pretend S-1 for ``--dry-run``: renders each probe instead of recording it.

    It behaves like the hardware where it matters to the pipeline: a note→sound
    latency (measured the way :meth:`SynthDriver.calibrate` measures it), a Range knob
    that sets the octave (``range_neutral`` is the CC14 value that plays at pitch), a
    real-looking level (``gain``) and a noise floor. ``kind`` picks the sound:
    ``"placeholder"`` (corpus.render_placeholder) or ``"twin"`` (the twin under
    :data:`FAKE_TRUE_CURVES`)."""

    def __init__(self, kind: str = "placeholder", latency_s: float = 0.018, range_neutral: int = 2,
                 gain: float = 0.3, noise_dbfs: float = -84.0, seed: int = 0) -> None:
        if kind not in ("placeholder", "twin"):
            raise ValueError(f"unknown fake {kind!r}")
        self.fake = kind
        self.kind = kind
        self.true_latency_s = float(latency_s)
        self.range_neutral = int(range_neutral)
        self.gain = float(gain)
        self.noise = 10.0 ** (noise_dbfs / 20.0)
        self.seed = int(seed)
        self.state: dict[int, int] = {}
        self.captures = 0
        self.restored: dict[int, int] | None = None
        self.closed = False
        self._latency_s = 0.0
        self._twin = None
        self.describe = f"pretend S-1 ({kind})"

    def _render(self, note: int) -> np.ndarray:
        eff = int(note) + 12 * (int(self.state.get(14, 2)) - self.range_neutral)
        if self.kind == "placeholder":
            from .corpus import render_placeholder

            clip = render_placeholder(self.state, midi_to_hz(eff), seconds=RECORD_S, sr=WORKING_SR)
            return clip.samples.astype(np.float64) * self.gain
        if self._twin is None:
            from .twin import Curve, Mapping, Twin

            curves = {n: Curve(*v) for n, v in FAKE_TRUE_CURVES.items()}
            self._twin = Twin(mapping=Mapping(curves, name="pretend S-1 (hidden curves)"),
                              sr=WORKING_SR, seconds=RECORD_S, gate_fraction=GATE_FRACTION)
        tw = self._twin
        audio = np.asarray(tw.render(tw.cc_to_k(self.state), s_from_cc(self.state), eff))
        return audio * self.gain

    def _raw(self, note: int) -> np.ndarray:
        self.captures += 1
        rng = np.random.default_rng(self.seed * 100003 + self.captures)
        pad = np.zeros(int(round(self.true_latency_s * WORKING_SR)))
        x = np.concatenate([pad, self._render(note)])
        x = x + self.noise * rng.standard_normal(x.size)
        return np.clip(x, -1.0, 1.0).astype(np.float32)

    def latency(self) -> float:
        self.state.update(CALIBRATION_PATCH)
        raw = self._raw(PITCH_NOTE)
        self._latency_s = find_onset(raw, WORKING_SR) / WORKING_SR
        return self._latency_s

    def capture(self, cc: dict[int, int], note: int, full: bool = False) -> AudioClip:
        self.state.update(cc)
        raw = self._raw(note)
        skip = int(self._latency_s * WORKING_SR)
        return AudioClip(raw[skip:], WORKING_SR)

    def restore(self, patch: dict[int, int] = CLEAN_PATCH) -> None:
        self.state = dict(patch)
        self.restored = dict(patch)

    def close(self) -> None:
        self.closed = True


def open_hardware(port: str | None, channel: int, device: str | None) -> HardwareRig:
    """Connect to the real S-1 (MIDI out + its USB audio input). Raises SetupError with
    a plain instruction when something is missing."""
    from ..audio import find_s1_input
    from ..midi_backend import MidiBackend
    from .capture import refresh_devices
    from .cli import _auto_port

    name = _auto_port(port)
    if not name:
        raise SetupError(
            "No S-1 MIDI port found. Check that the S-1 is on and connected with its USB-C "
            "data cable (a charge-only cable carries no MIDI or sound).")
    midi = MidiBackend()
    midi.channel = max(1, min(16, int(channel))) - 1
    try:
        midi.connect(name)
    except Exception as e:  # noqa: BLE001
        raise SetupError(f"Could not open the MIDI port {name!r}: {e}") from e
    dev: int | str | None = device
    if isinstance(dev, str) and dev.isdigit():
        dev = int(dev)
    if dev is None:
        refresh_devices()
        dev = find_s1_input()
    if dev is None:
        midi.disconnect()
        raise SetupError(
            "The S-1's MIDI port is there but its USB audio input is not. Unplug the cable, "
            "plug it back in, and run the check again.")
    driver = ProbeDriver(midi, device=dev, hold_s=HOLD_S, tail_s=TAIL_S)
    return HardwareRig(driver, midi)


def cockpit_running(port: int | None = None) -> bool:
    """True if something listens on the cockpit's port (default ``SYNTH_PORT`` / 8766)."""
    port = port or int(os.environ.get("SYNTH_PORT", "8766"))
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Preflight: latency, pitch, level
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class PreflightResult:
    latency_s: float
    range_cc14: int
    expression_cc11: int
    f0_hz: float
    cents: float
    peak_dbfs: float
    probe_seconds: float
    floor_dbfs: float = -120.0
    warnings: list[str] = field(default_factory=list)
    # The S-1's hiss with every oscillator at 0 (raw, WORKING_SR). Saved as floor.wav;
    # the fit adds it to every twin render so both sides carry the same floor.
    floor_samples: np.ndarray | None = field(default=None, repr=False, compare=False)

    def to_json(self) -> dict:
        d = asdict(self)
        d.pop("floor_samples", None)
        return d

    @property
    def overrides(self) -> dict[int, int]:
        """The corrections every probe carries on top of its planned CC vector."""
        return {14: self.range_cc14, 11: self.expression_cc11}


def preflight(rig: Any, emit: Callable[[str], None] = print) -> PreflightResult:
    """Latency, then a plain bright saw at C3: is it audible, not clipping, at pitch?

    Moves Range (CC14) by whole octaves until C3 sounds as C3, and lowers Expression
    (CC11) while the saw leaves less than 9 dB of headroom. The last check's wall time
    is the real per-probe cost the time estimate uses."""
    from .analyze import detect_f0

    latency = rig.latency()
    emit(f"  Latency: {latency * 1000:.0f} ms from note to sound.")
    rng14 = CALIBRATION_BASE[14]
    expr_idx, expr_locked, hot_peak = 0, False, None
    warnings: list[str] = []
    for _attempt in range(8):
        expr = EXPRESSION_STEPS[expr_idx]
        cc = {**CALIBRATION_BASE, 74: 127, 14: rng14, 11: expr}
        t0 = time.perf_counter()
        raw = rig.capture(cc, PITCH_NOTE, full=True)
        seconds = time.perf_counter() - t0
        peak = float(np.abs(raw.samples).max()) if raw.samples.size else 0.0
        if peak < SILENT_PEAK:
            raise SetupError(
                "The S-1 made no sound for a test note. Check: the S-1 is on, its volume knob "
                "is up, it receives on MIDI channel 3 (or pass --channel), and no other app "
                "holds its audio input.")
        if hot_peak is not None:
            # Expression was just lowered. If the level did not drop, CC11 is not a level
            # control on this S-1: put it back and leave the headroom to the volume knob.
            if _dbfs(peak) > _dbfs(hot_peak) - 1.0:
                expr_idx, expr_locked, hot_peak = 0, True, None
                warnings.append("Expression (CC11) does not change the S-1's level, so it stays at "
                                "127. Turn the volume knob down a little for more headroom.")
                continue
            hot_peak = None
        if peak >= HEADROOM_PEAK and not expr_locked and expr_idx + 1 < len(EXPRESSION_STEPS):
            hot_peak = peak
            expr_idx += 1
            emit(f"  Level: peak {_dbfs(peak):.1f} dBFS is hot; lowering Expression to "
                 f"{EXPRESSION_STEPS[expr_idx]} for headroom.")
            continue
        if peak >= CLIP_PEAK:
            raise SetupError(
                "The S-1 is clipping on a plain saw. Turn its volume knob down a little and "
                "run the check again.")
        f0 = detect_f0(prepare(raw))
        if f0 is None:
            raise SetupError(
                "No clear pitch in the test note. Check that no pattern, arpeggio or other "
                "keyboard is playing the S-1, then run the check again.")
        semis = 12.0 * math.log2(f0 / midi_to_hz(PITCH_NOTE))
        octave = int(round(semis / 12.0))
        if octave != 0:
            new = rng14 - octave
            if not 0 <= new < len(RANGE_LABELS):
                raise SetupError(
                    f"C3 plays at {f0:.1f} Hz (expected {midi_to_hz(PITCH_NOTE):.1f} Hz) and "
                    "the Range knob cannot correct it. Check the S-1's transpose setting.")
            emit(f"  Pitch: C3 played at {f0:.1f} Hz, {octave:+d} octave(s) off; setting Range "
                 f"to {RANGE_LABELS[new]} and checking again.")
            rng14 = new
            continue
        cents = 100.0 * semis
        # The hiss floor, and proof that CCs arrive: every oscillator level at 0 must
        # silence the S-1. If it still sounds, the level CCs are not getting through.
        quiet = {**cc, 20: 0, 19: 0, 21: 0, 23: 0}
        floor = rig.capture(quiet, PITCH_NOTE, full=True)
        fs = np.asarray(floor.samples, dtype=np.float64)
        floor_peak = float(np.abs(fs).max()) if fs.size else 0.0
        if floor_peak >= QUIET_PEAK:
            raise SetupError(
                "The S-1 still sounded with every oscillator level at 0, so it is not taking CCs "
                "from this app. Check its MIDI channel (this app sends on 3; pass --channel) and "
                "run the check again.")
        floor_dbfs = _dbfs(float(np.sqrt(np.mean(fs ** 2))) if fs.size else 0.0)
        if peak < QUIET_PEAK:
            warnings.append(f"The level is low (peak {_dbfs(peak):.0f} dBFS). Turning the S-1's "
                            "volume up gives cleaner measurements.")
        if abs(cents) > 30.0:
            warnings.append(f"The S-1 is {cents:+.0f} cents off concert pitch at Fine tune 64 "
                            "(its master tune?). The fine-tune curve absorbs this.")
        emit(f"  Pitch: C3 plays at {f0:.1f} Hz ({cents:+.0f} cents) with Range {RANGE_LABELS[rng14]}.")
        emit(f"  Level: peak {_dbfs(peak):.1f} dBFS with Expression {expr}.")
        emit(f"  Hiss floor: {floor_dbfs:.0f} dBFS with the oscillators off (CCs arrive).")
        for w in warnings:
            emit(f"  Note: {w}")
        return PreflightResult(latency_s=float(latency), range_cc14=int(rng14),
                               expression_cc11=int(expr), f0_hz=float(f0), cents=float(cents),
                               peak_dbfs=_dbfs(peak), probe_seconds=float(seconds),
                               floor_dbfs=float(floor_dbfs), warnings=warnings,
                               floor_samples=np.asarray(floor.samples, dtype=np.float32))
    raise SetupError("The pitch and level check did not settle after eight tries. "
                     "Check the S-1's Range and volume, then run the check again.")


# ─────────────────────────────────────────────────────────────────────────────
# Run directory: plan.json, meta.json, probes.jsonl, probes/NNNN.wav
# ─────────────────────────────────────────────────────────────────────────────
def write_plan(run_dir: Path, plan: list[Probe], config: dict) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "config": config, "probes": [p.to_json() for p in plan]}
    _atomic_write_text(run_dir / "plan.json", json.dumps(payload, indent=1))


def read_plan(run_dir: Path) -> tuple[list[Probe], dict]:
    path = run_dir / "plan.json"
    if not path.is_file():
        raise SetupError(f"No plan.json in {run_dir} — is this a calibration run folder?")
    data = json.loads(path.read_text())
    return [Probe.from_json(d) for d in data["probes"]], data.get("config", {})


def read_rows(run_dir: Path) -> list[dict]:
    """Completed probes (one JSON object per line). A torn last line is ignored."""
    path = run_dir / "probes.jsonl"
    rows: list[dict] = []
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (run_dir / row.get("file", "")).is_file():
            rows.append(row)
    return rows


def _append_row(run_dir: Path, row: dict) -> None:
    with (run_dir / "probes.jsonl").open("a") as f:
        f.write(json.dumps(row) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _append_meta(run_dir: Path, entry: dict) -> None:
    path = run_dir / "meta.json"
    meta = json.loads(path.read_text()) if path.exists() else {"sessions": []}
    meta["sessions"].append(entry)
    _atomic_write_text(path, json.dumps(meta, indent=1))


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _save_wav(path: Path, samples: np.ndarray, sr: int) -> None:
    import soundfile as sf

    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.asarray(samples, dtype=np.float32), sr, subtype="FLOAT")


def _load_wav(path: Path) -> AudioClip:
    import soundfile as sf

    data, sr = sf.read(str(path), dtype="float32", always_2d=False)
    if data.ndim == 2:
        data = data.mean(axis=1)
    return AudioClip(np.asarray(data, dtype=np.float32), int(sr))


# ─────────────────────────────────────────────────────────────────────────────
# The probe loop
# ─────────────────────────────────────────────────────────────────────────────
def estimate_seconds(n_probes: int, per_probe_s: float) -> float:
    return float(n_probes) * float(per_probe_s)


def hardware_probe_seconds(latency_s: float = 0.03) -> float:
    """What one probe costs on the real S-1, from the driver's own timings: CC settle +
    latency + note + release window, plus about 0.1 s of stream start/stop and the tail
    kill. Used for the dry run's "on the real S-1" line."""
    return SETTLE_S + latency_s + HOLD_S + TAIL_S + TAIL_KILL_S + 0.1


def _probe_label(p: Probe) -> str:
    if p.kind == "sweep":
        return f"{CURVE_LABELS.get(p.curve or '', p.curve)} sweep"
    return {"joint": "joint probe", "repeat": "repeat probe"}.get(p.kind, p.kind)


def run_probes(rig: Any, plan: list[Probe], run_dir: Path, overrides: dict[int, int],
               emit: Callable[[str], None] = print, stop_after: int | None = None,
               live: bool | None = None) -> str:
    """Play every planned probe not yet on disk. Returns ``"done"`` or ``"stopped"``.

    Each capture is written (WAV + one ``probes.jsonl`` line, fsynced) before the next
    one starts. Progress shows the probe, its kind, the running seconds per probe and
    the time left; ``live`` rewrites one line in place (default: when stdout is a TTY)."""
    done = {int(r["i"]) for r in read_rows(run_dir)}
    todo = [p for p in plan if p.i not in done]
    total = len(plan)
    if live is None:
        live = sys.stdout.isatty()
    ema: float | None = None
    for count, p in enumerate(todo, 1):
        cc = {**p.cc, **overrides}
        t0 = time.perf_counter()
        raw = rig.capture(cc, p.note, full=(p.kind == "repeat"))
        dt = time.perf_counter() - t0
        samples = np.asarray(raw.samples, dtype=np.float32)
        peak = float(np.abs(samples).max()) if samples.size else 0.0
        rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) if samples.size else 0.0
        rel = f"probes/{p.i:04d}.wav"
        _save_wav(run_dir / rel, samples, raw.samplerate)
        row = {
            "i": p.i, "kind": p.kind, "curve": p.curve, "level": p.level, "note": p.note,
            "holdout": p.holdout, "file": rel, "sr": int(raw.samplerate),
            "cc": {str(k): int(v) for k, v in cc.items()},
            "peak_dbfs": round(_dbfs(peak), 2), "rms_dbfs": round(_dbfs(rms), 2),
            "silent": bool(peak < SILENT_PEAK), "clipped": bool(peak >= CLIP_PEAK),
            "seconds": round(dt, 4), "t": datetime.now().isoformat(timespec="seconds"),
        }
        _append_row(run_dir, row)
        ema = dt if ema is None else 0.85 * ema + 0.15 * dt
        n_done = len(done) + count
        left = (total - n_done) * ema
        line = (f"  probe {n_done:>3} of {total} · {_probe_label(p):<24} · {ema:4.2f} s each · "
                f"about {_fmt_minutes(left)} left")
        if row["silent"] or row["clipped"]:
            what = "no sound" if row["silent"] else "clipped"
            if live:
                sys.stdout.write("\r" + " " * 96 + "\r")
            emit(f"  probe {p.i}: {what} ({_probe_label(p)}); it is left out of the fit.")
        if live:
            sys.stdout.write("\r" + line)
            sys.stdout.flush()
        elif count == 1 or n_done == total or n_done % max(1, total // 10) == 0:
            emit(line)
        if stop_after is not None and count >= stop_after and n_done < total:
            if live:
                sys.stdout.write("\n")
            return "stopped"
    if live and todo:
        sys.stdout.write("\n")
    return "done"


# ─────────────────────────────────────────────────────────────────────────────
# The fit
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class FitConfig:
    sr: int = WORKING_SR        # the rate the inversions render at
    grid: int = 17              # coarse grid points per 1-D search
    refine: int = 8             # golden-section steps around the best grid point
    passes: int = 2             # repeat every stage this many times
    workers: int = 1            # processes for the inversions and evaluations
    revert_margin: float = 1.05  # keep the default when the fit is worse than this on held-out


CurveTuple = tuple[float, float, str, str]


def default_curve_tuples() -> dict[str, CurveTuple]:
    from .twin import DEFAULT_CURVES

    return {n: (float(c.lo), float(c.hi), c.kind, c.unit) for n, c in DEFAULT_CURVES.items()}


def _mapping(curves: dict[str, CurveTuple], name: str = "calibrated"):
    from .twin import Curve, Mapping

    return Mapping({n: Curve(*v) for n, v in curves.items()}, name=name)


def _features(samples: np.ndarray, sr: int):
    from .features import extract

    return extract(prepare(AudioClip(np.asarray(samples, dtype=np.float32), sr)))


def _feature_error(target_feat, cand_samples: np.ndarray, sr: int) -> float:
    """The plain-metric feature distance :func:`synth.match.twin.calibrate` reports:
    ``distance.loss(target, candidate)`` on prepared clips, target-scaled."""
    from .distance import Weights, reference_scales
    from .distance import loss as plain_loss

    cf = _features(cand_samples, sr)
    return float(plain_loss(target_feat, cf, Weights(), scales=reference_scales(target_feat)))


# ── Phase-free LFO measurements ──────────────────────────────────────────────
# All run on a prepared clip (t = 0 at the note's onset) over the held part of the note,
# where the LFO is the only thing moving.
SUSTAIN_WINDOW = (0.1, HOLD_S - 0.05)


def _audible_end(samples: np.ndarray, sr: int, floor_db: float = -40.0) -> float:
    """Seconds until the sound falls ``floor_db`` below its loudest 10 ms (the release end)."""
    x = np.asarray(samples, dtype=np.float64)
    hop = max(1, sr // 100)
    n = x.size // hop
    if n == 0:
        return 0.0
    rms = np.sqrt(np.mean(x[: n * hop].reshape(n, hop) ** 2, axis=1))
    loud = np.nonzero(rms >= rms.max() * 10.0 ** (floor_db / 20.0))[0]
    return float((loud[-1] + 1) * hop / sr) if loud.size else 0.0


def _centroid_track(samples: np.ndarray, sr: int, n_fft: int = 512, hop: int = 128,
                    window: tuple[float, float] = SUSTAIN_WINDOW) -> tuple[np.ndarray, np.ndarray]:
    """(times, spectral centroid in Hz) every ``hop`` samples over ``window`` (seconds)."""
    x = np.asarray(samples, dtype=np.float64)
    a, b = int(window[0] * sr), int(window[1] * sr)
    seg = x[a:b]
    if seg.size < n_fft + hop:
        return np.zeros(0), np.zeros(0)
    n = 1 + (seg.size - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n)[:, None]
    mag = np.abs(np.fft.rfft(seg[idx] * np.hanning(n_fft), axis=1))
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
    cent = (mag * freqs).sum(axis=1) / np.maximum(mag.sum(axis=1), 1e-12)
    times = window[0] + (np.arange(n) * hop + n_fft / 2) / sr
    return times, cent


def dominant_rate(samples: np.ndarray, sr: int) -> tuple[float | None, float, float]:
    """The LFO rate, read off the brightness track: the frequency of the best-fitting
    sinusoid (after removing a mean and a linear trend). Returns ``(hz, evr, phase)`` —
    ``evr`` is the variance explained (0..1), ``phase`` the sinusoid's phase at note-on in
    degrees (a sine that starts at 0 and rises is 0). ``hz`` is None when the track does
    not hold at least 1.2 clear cycles. The window runs from 0.1 s to the end of the
    audible sound (a long release keeps the LFO measurable after note-off)."""
    end = min(_audible_end(samples, sr), np.asarray(samples).size / sr) - 0.02
    times, track = _centroid_track(samples, sr, window=(SUSTAIN_WINDOW[0], max(end, SUSTAIN_WINDOW[1])))
    if track.size < 16:
        return None, 0.0, 0.0
    y = np.log2(np.maximum(track, 1.0))
    base = np.vstack([np.ones_like(times), times - times.mean()]).T
    y = y - base @ np.linalg.lstsq(base, y, rcond=None)[0]
    var = float(np.var(y))
    if var < 1e-10:
        return None, 0.0, 0.0
    span = float(times[-1] - times[0])
    frame_rate = 1.0 / float(times[1] - times[0])

    def fit(f: float) -> tuple[float, float]:
        m = np.vstack([np.sin(2 * np.pi * f * times), np.cos(2 * np.pi * f * times), base.T]).T
        coef, *_ = np.linalg.lstsq(m, y, rcond=None)
        resid = y - m @ coef
        return 1.0 - float(np.var(resid)) / var, math.degrees(math.atan2(coef[1], coef[0]))

    grid = np.geomspace(0.3, 0.35 * frame_rate, 480)
    scores = np.array([fit(f)[0] for f in grid])
    j = int(np.argmax(scores))
    lo, hi = math.log(grid[max(j - 1, 0)]), math.log(grid[min(j + 1, grid.size - 1)])
    golden = (math.sqrt(5.0) - 1.0) / 2.0
    for _ in range(24):
        c, d = hi - golden * (hi - lo), lo + golden * (hi - lo)
        if fit(math.exp(c))[0] > fit(math.exp(d))[0]:
            hi = d
        else:
            lo = c
    f = math.exp(0.5 * (lo + hi))
    evr, phase = fit(f)
    if evr < 0.5 or f * span < 1.2:
        return None, evr, phase
    return f, evr, phase


def _median3(x: np.ndarray) -> np.ndarray:
    if x.size < 3:
        return x
    return np.median(np.vstack([x[:-2], x[1:-1], x[2:]]), axis=0)


def vibrato_extent(samples: np.ndarray, sr: int, win_s: float = 0.03,
                   hop_s: float = 0.01) -> float | None:
    """Half the pitch swing in semitones over the sustain window (YIN on short windows).
    None when fewer than 80% of the windows hold a clear pitch."""
    from .analyze import detect_f0

    x = np.asarray(samples, dtype=np.float32)
    win, hop = int(win_s * sr), int(hop_s * sr)
    starts = range(int(SUSTAIN_WINDOW[0] * sr), int(SUSTAIN_WINDOW[1] * sr) - win, hop)
    f0s = [detect_f0(AudioClip(x[a:a + win], sr), fmin=90.0, fmax=2000.0) for a in starts]
    voiced = np.array([f for f in f0s if f is not None])
    if len(f0s) == 0 or voiced.size < 0.8 * len(f0s):
        return None
    semis = _median3(12.0 * np.log2(voiced / np.median(voiced)))
    return float((semis.max() - semis.min()) / 2.0)


def brightness_swing(samples: np.ndarray, sr: int) -> float | None:
    """Half the brightness swing in octaves (log2 spectral centroid) over the sustain window."""
    _times, track = _centroid_track(samples, sr)
    if track.size < 8:
        return None
    y = _median3(np.log2(np.maximum(track, 1.0)))
    return float((y.max() - y.min()) / 2.0)


OBJECTIVES: dict[str, Callable[[np.ndarray, int], float | None]] = {
    "vibrato": vibrato_extent, "swing": brightness_swing,
}


def _add_floor(audio: np.ndarray, floor: np.ndarray | None, cap_peak: float | None) -> np.ndarray:
    """Give a twin render the S-1's own hiss at the level it has in the capture it is
    compared with: capture = signal (peak ``cap_peak``) + floor, and ``prepare()``
    peak-normalizes both sides, so the floor rides at ``peak(render) / cap_peak``."""
    if floor is None or not cap_peak or cap_peak <= 0.0:
        return audio
    peak = float(np.abs(audio).max()) if audio.size else 0.0
    if peak <= 0.0:
        return audio
    f = np.resize(np.asarray(floor, dtype=np.float32), audio.size)
    return (audio + (peak / float(cap_peak)) * f).astype(np.float32)


def _render(curves: dict[str, CurveTuple], cc: dict[int, int], note: int, sr: int,
            k_override: tuple[int, float] | None = None) -> np.ndarray:
    from .twin import Twin

    tw = Twin(mapping=_mapping(curves), sr=sr, seconds=RECORD_S, gate_fraction=GATE_FRACTION)
    k = tw.cc_to_k(cc)
    if k_override is not None:
        k[k_override[0]] = k_override[1]
    return np.asarray(tw.render(k, s_from_cc(cc), int(note)), dtype=np.float32)


def _invert_task(task: dict) -> dict:
    """One sweep probe: the physical value of ``task["curve"]`` that makes the twin
    sound most like the capture. Top level so a process pool can run it.

    A coarse grid over the search range, then golden-section refinement around the
    best grid point. Also reports how sharp the answer is: ``spread`` (the loss range
    over the grid, relative — near 0 means this probe cannot see the knob) and
    ``width`` (the grid span within 5% of the best loss — the answer's uncertainty)."""
    from .twin import K_NAMES, Curve

    curve = task["curve"]
    lo, hi, kind = task["search"]
    search_curve = Curve(lo, hi, kind)
    curves = {n: tuple(v) for n, v in task["curves"].items()}
    unit = curves[curve][3]
    curves[curve] = (lo, hi, kind, unit)
    cc = {int(k): int(v) for k, v in task["cc"].items()}
    sr = int(task["sr"])
    idx = K_NAMES.index(curve)
    objective = task.get("objective", "metric")
    measure = OBJECTIVES.get(objective)
    target = np.asarray(task["target"], dtype=np.float32)
    target_feat = _features(target, sr) if measure is None else None
    target_val = measure(target, sr) if measure is not None else None
    cache: dict[float, float] = {}

    from .twin import Twin

    tw = Twin(mapping=_mapping(curves, "search"), sr=sr, seconds=RECORD_S, gate_fraction=GATE_FRACTION)
    k0 = tw.cc_to_k(cc)
    s = s_from_cc(cc)

    def loss_at(u: float) -> float:
        u = float(np.clip(u, 0.0, 1.0))
        key = round(u, 7)
        if key not in cache:
            kk = k0.copy()
            kk[idx] = u
            audio = np.asarray(tw.render(kk, s, int(task["note"])), dtype=np.float32)
            audio = _add_floor(audio, task.get("floor"), task.get("cap_peak"))
            if measure is None:
                cache[key] = _feature_error(target_feat, audio, sr)
            else:
                val = measure(prepare(AudioClip(audio, sr)).samples, sr)
                cache[key] = 1e3 if (val is None or target_val is None) else abs(val - target_val)
        return cache[key]

    grid = np.linspace(0.0, 1.0, int(task["grid"]))
    losses = np.array([loss_at(u) for u in grid])
    j = int(np.argmin(losses))
    a, b = float(grid[max(j - 1, 0)]), float(grid[min(j + 1, len(grid) - 1)])
    golden = (math.sqrt(5.0) - 1.0) / 2.0
    c, d = b - golden * (b - a), a + golden * (b - a)
    fc, fd = loss_at(c), loss_at(d)
    for _ in range(int(task["refine"])):
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - golden * (b - a)
            fc = loss_at(c)
        else:
            a, c, fc = c, d, fd
            d = a + golden * (b - a)
            fd = loss_at(d)
    best_u, best_l = min(((float(u), float(v)) for u, v in cache.items()), key=lambda t: t[1])
    lmin, lmax = float(losses.min()), float(losses.max())
    near = grid[losses <= min(lmin, best_l) * 1.05 + 0.01 * (lmax - lmin) + 1e-9]
    x = float(search_curve(best_u))
    if task.get("fold"):
        x = min(x, 1.0 - x)
    return {
        "i": task["i"], "curve": curve, "u": best_u, "x": x, "loss": best_l,
        "measured": target_val,
        "spread": (lmax - lmin) / (lmin + 1e-6),
        "width": float(near.max() - near.min()) if near.size else 0.0,
        "band": [float(near.min()), float(near.max())] if near.size else [best_u, best_u],
    }


def _eval_task(task: dict) -> float:
    """Feature error of the twin (under ``task["curves"]``) against one capture."""
    target = np.asarray(task["target"], dtype=np.float32)
    tf = _features(target, WORKING_SR)
    cc = {int(k): int(v) for k, v in task["cc"].items()}
    audio = _render({n: tuple(v) for n, v in task["curves"].items()}, cc, int(task["note"]), WORKING_SR)
    audio = _add_floor(audio, task.get("floor"), task.get("cap_peak"))
    return _feature_error(tf, audio, WORKING_SR)


class _Pool:
    """Runs task lists serially (workers <= 1) or on a spawn-based process pool."""

    def __init__(self, workers: int) -> None:
        self.workers = max(1, int(workers))
        self._ex = None

    def __enter__(self) -> "_Pool":
        if self.workers > 1:
            import multiprocessing as mp
            from concurrent.futures import ProcessPoolExecutor

            self._ex = ProcessPoolExecutor(max_workers=self.workers, mp_context=mp.get_context("spawn"))
        return self

    def __exit__(self, *exc: object) -> None:
        if self._ex is not None:
            self._ex.shutdown(wait=True, cancel_futures=True)

    def map(self, fn: Callable[[dict], Any], tasks: list[dict]) -> list[Any]:
        if not tasks:
            return []
        if self._ex is None:
            return [fn(t) for t in tasks]
        return list(self._ex.map(fn, tasks, chunksize=1))


MIN_SPREAD = 0.03       # a probe whose loss moves < 3% across the whole search cannot see the knob
NO_RESPONSE = 0.02      # a fitted slope under 2% of the search span = "the knob does nothing"


def regress_curve(spec: SweepSpec, points: list[dict], default: CurveTuple) -> tuple[CurveTuple | None, dict]:
    """Fit ``lo``/``hi`` of the curve's shape to measured points ``{k, x, weight}``.

    ``linear`` regresses x on k; ``exp`` regresses log x on k (the search range of an
    exp curve is log-spaced too). Weighted least squares: a sharp inversion counts more
    than a blurry one. Returns ``(None, info)`` when the curve keeps its default (too
    few measurable points, or no measurable response)."""
    kind = default[2]
    info: dict[str, Any] = {"points": len(points), "kind": kind}
    usable = [p for p in points if p.get("ok")]
    info["identified"] = len(usable)
    ks = np.array([p["k"] for p in usable], dtype=np.float64)
    if len(np.unique(np.round(ks, 4))) < 2:
        info.update(status="kept", why="too few measurable points")
        return None, info
    xs = np.array([p["x"] for p in usable], dtype=np.float64)
    ws = np.array([max(1e-6, p.get("weight", 1.0)) for p in usable], dtype=np.float64)
    if kind == "exp":
        if np.any(xs <= 0):
            info.update(status="kept", why="non-positive measurements on an exp curve")
            return None, info
        y = np.log(xs)
        span = math.log(spec.search[1] / spec.search[0])
    else:
        y = xs
        span = spec.search[1] - spec.search[0]
    sw = np.sqrt(ws)
    A = np.vstack([np.ones_like(ks), ks]).T
    (a, b), *_ = np.linalg.lstsq(A * sw[:, None], y * sw, rcond=None)
    resid = y - (a + b * ks)
    ybar = float(np.sum(ws * y) / np.sum(ws))
    ss_tot = float(np.sum(ws * (y - ybar) ** 2))
    info["r2"] = float(1.0 - np.sum(ws * resid ** 2) / ss_tot) if ss_tot > 1e-12 else None
    info["rms"] = float(np.sqrt(np.sum(ws * resid ** 2) / np.sum(ws)))
    if abs(b) < NO_RESPONSE * abs(span):
        info.update(status="kept", why="no measurable response to this knob")
        return None, info
    lo, hi = (math.exp(a), math.exp(a + b)) if kind == "exp" else (float(a), float(a + b))
    notes = []
    if kind == "linear" and spec.physical[0] and spec.curve != "fine_tune":
        if lo < 0.0:
            lo, notes = 0.0, notes + ["low end clamped to 0"]
        if hi < 0.0:
            hi, notes = 0.0, notes + ["high end clamped to 0"]
    if spec.fold:
        lo, hi = float(np.clip(lo, 0.001, 0.5)), float(np.clip(hi, 0.001, 0.5))
    if lo == hi:
        info.update(status="kept", why="the fit collapsed to one value")
        return None, info
    info.update(status="fitted", why="; ".join(notes))
    return (float(lo), float(hi), kind, default[3]), info


@dataclass
class LoadedProbe:
    row: dict
    probe: Probe
    raw: AudioClip          # latency-trimmed capture at its recorded rate
    usable: bool

    @property
    def cc(self) -> dict[int, int]:
        return {int(k): int(v) for k, v in self.row["cc"].items()}

    @property
    def peak(self) -> float:
        return float(np.abs(self.raw.samples).max()) if self.raw.samples.size else 0.0


def load_probes(run_dir: Path) -> list[LoadedProbe]:
    plan, _config = read_plan(run_dir)
    by_i = {p.i: p for p in plan}
    out = []
    for row in read_rows(run_dir):
        p = by_i.get(int(row["i"]))
        if p is None:
            continue
        clip = _load_wav(run_dir / row["file"])
        usable = not row.get("silent") and not row.get("clipped")
        out.append(LoadedProbe(row=row, probe=p, raw=clip, usable=usable))
    return out


def _at_sr(clip: AudioClip, sr: int) -> np.ndarray:
    return np.asarray(clip.resample(sr).samples if clip.samplerate != sr else clip.samples, np.float32)


def fit_curves(probes: list[LoadedProbe], cfg: FitConfig, pool: _Pool,
               emit: Callable[[str], None] = print,
               floor: np.ndarray | None = None) -> tuple[dict[str, CurveTuple], dict[str, dict]]:
    """Stage-by-stage, pass-by-pass curve fit from the sweep probes (fit split only)."""
    from .analyze import detect_f0
    from .twin import K_NAMES

    curves = default_curve_tuples()
    defaults = dict(curves)
    swept = sorted({lp.probe.curve for lp in probes if lp.probe.kind == "sweep" and lp.probe.curve},
                   key=lambda c: [s.curve for s in SWEEPS].index(c))
    fits: dict[str, dict] = {}
    fit_set = {c: [lp for lp in probes if lp.probe.kind == "sweep" and lp.probe.curve == c
                   and not lp.probe.holdout and lp.usable] for c in swept}
    at_fit_sr = {lp.probe.i: prepare(AudioClip(_at_sr(lp.raw, cfg.sr), cfg.sr)).samples
                 for c in swept for lp in fit_set[c]}
    floor_fit = _at_sr(AudioClip(floor, WORKING_SR), cfg.sr) if floor is not None else None

    pitch_points: dict[str, list[dict]] = {}
    for c in swept:
        spec = SWEEP_BY_CURVE[c]
        if spec.method == "rate":
            pts = []
            k_idx = K_NAMES.index(c)
            for lp in fit_set[c]:
                at_ws = prepare(AudioClip(_at_sr(lp.raw, WORKING_SR), WORKING_SR)).samples
                hz, evr, phase = dominant_rate(at_ws, WORKING_SR)
                k = _k_of(_k_cc(k_idx), lp.cc.get(_k_cc(k_idx), 64))
                pts.append({"i": lp.probe.i, "k": k, "x": hz if hz is not None else float("nan"),
                            "weight": max(evr, 1e-3), "ok": hz is not None, "evr": evr, "phase": phase})
            pitch_points[c] = pts
            continue
        if spec.method != "pitch":
            continue
        pts = []
        k_idx = K_NAMES.index(c)
        for lp in fit_set[c]:
            f0 = detect_f0(prepare(lp.raw))
            k = _k_of(_k_cc(k_idx), lp.cc.get(_k_cc(k_idx), 64))
            ok = f0 is not None
            cents = 1200.0 * math.log2(f0 / midi_to_hz(lp.probe.note)) if ok else float("nan")
            pts.append({"i": lp.probe.i, "k": k, "x": cents, "weight": 1.0, "ok": ok})
        pitch_points[c] = pts

    stages = sorted({SWEEP_BY_CURVE[c].stage for c in swept})
    for pass_no in range(1, cfg.passes + 1):
        for stage in stages:
            in_stage = [c for c in swept if SWEEP_BY_CURVE[c].stage == stage]
            tasks = []
            for c in in_stage:
                spec = SWEEP_BY_CURVE[c]
                if spec.method != "twin":
                    continue
                for lp in fit_set[c]:
                    tasks.append({
                        "i": lp.probe.i, "curve": c, "search": spec.search, "fold": spec.fold,
                        "curves": curves, "cc": lp.row["cc"], "note": lp.probe.note,
                        "target": at_fit_sr[lp.probe.i], "sr": cfg.sr,
                        "grid": cfg.grid, "refine": cfg.refine,
                        "floor": floor_fit, "cap_peak": lp.peak, "objective": spec.objective,
                    })
            results = {(r["curve"], r["i"]): r for r in pool.map(_invert_task, tasks)}
            for c in in_stage:
                spec = SWEEP_BY_CURVE[c]
                k_idx = K_NAMES.index(c)
                if spec.method in ("pitch", "rate"):
                    points = pitch_points[c]
                else:
                    points = []
                    for lp in fit_set[c]:
                        r = results[(c, lp.probe.i)]
                        u = r["u"]
                        # An answer pinned to (or a near-best band reaching) a non-physical
                        # end of the search is only a bound ("at least 10 kHz"): drop it.
                        band_lo, band_hi = r["band"]
                        edge = ((min(u, band_lo) <= 0.001 and not spec.physical[0])
                                or (max(u, band_hi) >= 0.999 and not spec.physical[1]))
                        ok = r["spread"] >= MIN_SPREAD and not edge
                        if spec.objective != "metric":
                            # a phase-free match must actually hit the measured value
                            ok = ok and r["measured"] is not None and r["loss"] < 0.25 * max(
                                abs(r["measured"]), 0.05)
                        if c == "cutoff" and cfg.sr != WORKING_SR:
                            # The twin's soft bound bends high cutoffs differently at the
                            # fit rate than at WORKING_SR; keep only points where it agrees.
                            ok = ok and abs(_ladder_bend(r["x"], cfg.sr)
                                            - _ladder_bend(r["x"], WORKING_SR)) < 0.02
                        k = _k_of(_k_cc(k_idx), lp.cc.get(_k_cc(k_idx), 0))
                        weight = 1.0 / (r["width"] + 0.5 / max(1, cfg.grid - 1)) ** 2
                        points.append({"i": lp.probe.i, "k": k, "x": r["x"], "weight": weight,
                                       "ok": ok, "spread": r["spread"], "edge": edge,
                                       "loss": r["loss"]})
                new, info = regress_curve(spec, points, defaults[c])
                info["pass"] = pass_no
                if spec.method == "rate":
                    phases = [p["phase"] for p in points if p["ok"]]
                    info["lfo_phase_deg"] = float(np.median(phases)) if phases else None
                info["measured"] = [{"k": round(p["k"], 4), "x": _round_sig(p["x"]), "ok": p["ok"]}
                                    for p in points]
                fits[c] = info
                curves[c] = new if new is not None else defaults[c]
        emit(f"  Fit pass {pass_no} of {cfg.passes} done.")
    return curves, fits


def _ladder_bend(hz: float, sr: int) -> float:
    """Octaves the twin's soft upper cutoff bound (0.45 * sr, via softplus in
    ``Twin.render``) pulls a cutoff of ``hz`` down. It depends on the sample rate, so a
    cutoff measured at a lower fit rate is only comparable where both bends agree."""
    d = math.log2(0.45 * sr / max(float(hz), 1e-9))
    return (max(d, 0.0) + math.log1p(math.exp(-abs(d)))) - d


def _k_cc(k_idx: int) -> int:
    from .twin import K_PARAMS

    return K_PARAMS[k_idx].cc


def _round_sig(x: float, sig: int = 6) -> float | None:
    if x is None or not np.isfinite(x):
        return None
    return float(f"{float(x):.{sig}g}")


def _eval_tasks(probes: list[LoadedProbe], curves: dict[str, CurveTuple],
                floor: np.ndarray | None = None) -> list[dict]:
    return [{"curves": curves, "cc": lp.row["cc"], "note": lp.probe.note,
             "target": _at_sr(lp.raw, WORKING_SR), "floor": floor, "cap_peak": lp.peak}
            for lp in probes]


def load_floor(run_dir: Path) -> np.ndarray | None:
    """The hiss floor captured by the latest preflight (``floor.wav``), if any."""
    path = run_dir / "floor.wav"
    if not path.is_file():
        return None
    clip = _load_wav(path)
    return _at_sr(clip, WORKING_SR)


def _mean(xs: Sequence[float]) -> float | None:
    return float(np.mean(xs)) if len(xs) else None


def _closeness(err: float | None) -> float | None:
    if err is None:
        return None
    from .distance import Weights, closeness

    return float(closeness(err, Weights()))


def fit_run(run_dir: Path, cfg: FitConfig, emit: Callable[[str], None] = print) -> dict:
    """Fit the curves from a run folder's probes and measure the held-out gap.

    Returns the report dict (also the content of ``report.json``); the fitted curves
    are in ``report["fitted_curves"]``."""
    from .distance import Weights, reference_scales
    from .distance import loss as plain_loss
    from .twin import K_NAMES, Twin
    from .twin import calibrate as twin_calibrate

    probes = load_probes(run_dir)
    floor = load_floor(run_dir)
    usable = [lp for lp in probes if lp.usable]
    repeats = [lp for lp in usable if lp.probe.kind == "repeat"]
    joints = [lp for lp in usable if lp.probe.kind == "joint"]
    held = [lp for lp in usable if lp.probe.kind == "sweep" and lp.probe.holdout]

    # The repeat spread: how far apart the S-1's own repeats of one patch land.
    floor_errs: list[float] = []
    if len(repeats) >= 2:
        ref = _features(prepare(repeats[0].raw).samples, WORKING_SR)
        scales = reference_scales(ref)
        for lp in repeats[1:]:
            cf = _features(prepare(lp.raw).samples, WORKING_SR)
            floor_errs.append(float(plain_loss(ref, cf, Weights(), scales=scales)))

    with _Pool(cfg.workers) as pool:
        curves, fits = fit_curves(probes, cfg, pool, emit, floor=floor)
        defaults = default_curve_tuples()

        # Guard: a curve whose fit is worse than the default on its own held-out
        # sweep probes goes back to the default.
        guard_tasks, guard_keys = [], []
        for c, info in fits.items():
            if info.get("status") != "fitted":
                continue
            hp = [lp for lp in held if lp.probe.curve == c]
            if not hp:
                continue
            without = {**curves, c: defaults[c]}
            for lp in hp:
                guard_tasks += _eval_tasks([lp], curves, floor) + _eval_tasks([lp], without, floor)
                guard_keys += [(c, "fitted"), (c, "default")]
        guard_vals = pool.map(_eval_task, guard_tasks)
        per: dict[str, dict[str, list[float]]] = {}
        for (c, which), v in zip(guard_keys, guard_vals):
            per.setdefault(c, {"fitted": [], "default": []})[which].append(v)
        for c, d in per.items():
            f, dflt = float(np.mean(d["fitted"])), float(np.mean(d["default"]))
            fits[c]["held_out"] = {"fitted": f, "default": dflt, "n": len(d["fitted"])}
            if f > dflt * cfg.revert_margin:
                fits[c]["status"] = "reverted"
                fits[c]["why"] = "the fit did worse than the default on held-out probes"
                curves[c] = defaults[c]

        # The held-out gap: default curves vs fitted curves.
        eval_set = held + joints
        before = pool.map(_eval_task, _eval_tasks(eval_set, defaults, floor))
        after = pool.map(_eval_task, _eval_tasks(eval_set, curves, floor))
        # The same joint gap with no hiss added — what twin.calibrate() computes.
        plain_after = pool.map(_eval_task, _eval_tasks(joints, curves)) if floor is not None else None
    err_before = {lp.probe.i: v for lp, v in zip(eval_set, before)}
    err_after = {lp.probe.i: v for lp, v in zip(eval_set, after)}

    def gap(group: list[LoadedProbe]) -> dict:
        b = [err_before[lp.probe.i] for lp in group]
        a = [err_after[lp.probe.i] for lp in group]
        return {"n": len(group), "before": _mean(b), "after": _mean(a),
                "closeness_before": _closeness(_mean(b)), "closeness_after": _closeness(_mean(a))}

    modules = {}
    for m in MODULE_LABELS:
        group = [lp for lp in held if SWEEP_BY_CURVE[lp.probe.curve].module == m]
        if group:
            modules[m] = gap(group)
    by_note = {}
    for note in sorted({lp.probe.note for lp in joints}):
        by_note[str(note)] = gap([lp for lp in joints if lp.probe.note == note])

    plain = ({lp.probe.i: v for lp, v in zip(joints, plain_after)} if plain_after is not None
             else {lp.probe.i: err_after[lp.probe.i] for lp in joints})

    # The seam, as a cross-check: twin.calibrate() on the joint probes, per pitch.
    seam = {}
    tw = Twin(mapping=_mapping(curves), sr=WORKING_SR, seconds=RECORD_S, gate_fraction=GATE_FRACTION)
    for note in sorted({lp.probe.note for lp in joints}):
        group = [lp for lp in joints if lp.probe.note == note]
        if len(group) < 2:
            continue
        rep = twin_calibrate([(lp.cc, lp.raw.resample(WORKING_SR).samples) for lp in group],
                             twin=tw, held_out_fraction=1.0, note=note)
        seam[str(note)] = {"n": rep.n_held_out, "held_out_feature_error": rep.held_out_feature_error,
                           "this_report": _mean([plain[lp.probe.i] for lp in group])}

    # Summary of every curve (swept or not).
    curve_report = {}
    for name in K_NAMES:
        info = fits.get(name, {"status": "not swept", "why": "not in this run's plan"})
        d, f = defaults[name], curves[name]
        curve_report[name] = {
            "label": CURVE_LABELS.get(name, name),
            "module": SWEEP_BY_CURVE[name].module if name in SWEEP_BY_CURVE else None,
            "default": {"lo": d[0], "hi": d[1], "kind": d[2], "unit": d[3]},
            "fitted": {"lo": _round_sig(f[0]), "hi": _round_sig(f[1]), "kind": f[2], "unit": f[3]},
            **{k: v for k, v in info.items()},
        }

    kinds = {k: sum(1 for lp in probes if lp.probe.kind == k) for k in ("sweep", "joint", "repeat")}
    secs = [float(lp.row.get("seconds", 0.0)) for lp in probes]
    return {
        "probes": {"recorded": len(probes), "usable": len(usable),
                   "silent": sum(1 for lp in probes if lp.row.get("silent")),
                   "clipped": sum(1 for lp in probes if lp.row.get("clipped")), "by_kind": kinds},
        "seconds_per_probe": float(np.median(secs)) if secs else None,
        "repeat_spread": {"n": len(floor_errs), "mean": _mean(floor_errs),
                          "max": float(max(floor_errs)) if floor_errs else None,
                          "closeness": _closeness(_mean(floor_errs))},
        "hiss": {"measured": floor is not None,
                 "rms_dbfs": _dbfs(float(np.sqrt(np.mean(floor.astype(np.float64) ** 2))))
                 if floor is not None and floor.size else None},
        "gap": {"joint": gap(joints), "held_out_sweeps": gap(held), "modules": modules, "by_note": by_note},
        "calibrate_seam": {
            "comment": ("synth.match.twin.calibrate() on the joint probes with the fitted curves, "
                        "per pitch. 'this_report' is this report's own number for the same probes "
                        "without the hiss floor added (twin.calibrate adds none); the two should "
                        "agree. Its label still says 'synthetic' until the requested flag lands, "
                        "so the label is not copied here."),
            "by_note": seam,
        },
        "curves": curve_report,
        "fitted_curves": {n: list(v) for n, v in curves.items()},
        "fit_config": asdict(cfg),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Outputs
# ─────────────────────────────────────────────────────────────────────────────
def curves_payload(curves: dict[str, Sequence], source: str, calibrated: bool = True) -> dict:
    """The file the browser twin reads, in its exact shape::

        {"version": 1, "calibrated": true, "source": "synth-calibrate <timestamp>",
         "curves": {name: {"lo": float, "hi": float, "kind": "linear"|"exp", "unit": str}}}

    All 18 k curves, in ``K_PARAMS`` order. ``calibrated`` is true only for a hardware
    run; a dry run writes false (and never to the installed path)."""
    from .twin import K_NAMES

    out = {}
    for name in K_NAMES:
        lo, hi, kind, unit = curves[name]
        out[name] = {"lo": float(_round_sig(lo)), "hi": float(_round_sig(hi)),
                     "kind": str(kind), "unit": str(unit)}
    return {"version": 1, "calibrated": bool(calibrated), "source": str(source), "curves": out}


def load_curves(path: str | Path):
    """Read a curves file (see :func:`curves_payload`) back into a twin ``Mapping``."""
    from .twin import K_NAMES, Curve, Mapping

    data = json.loads(Path(path).read_text())
    if data.get("version") != 1 or "curves" not in data:
        raise ValueError(f"{path}: not a version-1 curves file")
    curves = {}
    for name in K_NAMES:
        c = data["curves"][name]
        curves[name] = Curve(float(c["lo"]), float(c["hi"]), str(c["kind"]), str(c.get("unit", "")))
    label = data.get("source", "calibrated")
    return Mapping(curves, name=f"{label} ({'calibrated' if data.get('calibrated') else 'uncalibrated'})")


def _fmt_val(v: float | None, unit: str = "") -> str:
    if v is None:
        return "n/a"
    av = abs(v)
    if av >= 1000:
        s = f"{v:,.0f}"
    elif av >= 100:
        s = f"{v:.0f}"
    elif av >= 10:
        s = f"{v:.1f}"
    elif av >= 1:
        s = f"{v:.2f}"
    else:
        s = f"{v:.3g}"
    return f"{s} {unit}".strip()


def _fmt_curve(c: dict) -> str:
    unit = c.get("unit", "")
    short = {"amp": "", "duty": "", "level": "", "amt": "", "ladder k (4==self-osc)": "",
             "oct/oct": "oct/oct"}.get(unit, unit)
    return f"{_fmt_val(c['lo'], short)} to {_fmt_val(c['hi'], short)} ({c['kind']})"


def headline(report: dict) -> str:
    j = report["gap"]["joint"]
    fl = report["repeat_spread"]
    if not j["n"]:
        return "No joint probes were usable, so there is no held-out gap to report."
    s = (f"On {j['n']} held-out joint probes the twin's feature gap went from {j['before']:.3f} "
         f"to {j['after']:.3f} (closeness {j['closeness_before']:.0f} to {j['closeness_after']:.0f}).")
    if fl["mean"] is not None:
        s += f" The S-1's own repeat spread is {fl['mean']:.3f}: no twin can beat that floor."
    return s


def report_markdown(report: dict) -> str:
    """The short human report (report.md)."""
    lines = [f"# Twin calibration, {report['created'][:16].replace('T', ' ')}", ""]
    if report.get("dry_run"):
        lines += [f"**Dry run** against a pretend S-1 ({report.get('fake')}). These numbers test the "
                  "pipeline; they say nothing about the real S-1.", ""]
    lines += [f"**Result.** {headline(report)}", ""]
    if report.get("worse"):
        lines += ["**The fitted curves did worse than the defaults on the joint probes, so they were "
                  "not installed.** Look at the curve table and the notes below.", ""]
    pre = report.get("preflight") or []
    last = pre[-1] if pre else None
    pr = report["probes"]
    src = [f"Source: {report['source']}",
           f"{pr['recorded']} probes ({pr['by_kind']['sweep']} sweep, {pr['by_kind']['joint']} joint, "
           f"{pr['by_kind']['repeat']} repeat), {pr['usable']} usable"]
    if report.get("seconds_per_probe"):
        src.append(f"{report['seconds_per_probe']:.2f} s per probe")
    if last:
        src.append(f"latency {last['latency_s'] * 1000:.0f} ms, Range {RANGE_LABELS[last['range_cc14']]}, "
                   f"tuning {last['cents']:+.0f} cents, Expression {last['expression_cc11']}, "
                   f"hiss {last.get('floor_dbfs', -120):.0f} dBFS")
    lines += [" · ".join(src) + ".", ""]

    lines += ["## Held-out gap by module", "",
              "Feature gap between the twin and the S-1 on probes the fit never saw (lower is better).", "",
              "| Module | Probes | Default curves | Fitted curves | Closeness |",
              "|---|---:|---:|---:|---|"]
    for m, g in report["gap"]["modules"].items():
        lines.append(f"| {MODULE_LABELS[m]} sweeps | {g['n']} | {g['before']:.3f} | {g['after']:.3f} | "
                     f"{g['closeness_before']:.0f} to {g['closeness_after']:.0f} |")
    j = report["gap"]["joint"]
    if j["n"]:
        lines.append(f"| All knobs at once (joint) | {j['n']} | {j['before']:.3f} | {j['after']:.3f} | "
                     f"{j['closeness_before']:.0f} to {j['closeness_after']:.0f} |")
    fl = report["repeat_spread"]
    if fl["mean"] is not None:
        lines.append(f"| Repeat spread (the floor) | {fl['n']} | {fl['mean']:.3f} | | |")
    lines += ["", "## Curves", "",
              "| Knob | Default | Fitted | Fit quality | Notes |", "|---|---|---|---|---|"]
    for name, c in report["curves"].items():
        status = c.get("status")
        fallback = {"kept": "kept default", "reverted": "kept default", "not swept": "not measured"}
        fitted = _fmt_curve(c["fitted"]) if status == "fitted" else fallback.get(status, status)
        r2 = c.get("r2")
        quality = (f"R² {r2:.2f}, {c.get('identified', 0)} of {c.get('points', 0)} points"
                   if r2 is not None else (f"{c.get('identified', 0)} of {c.get('points', 0)} points"
                                           if c.get("points") else ""))
        lines.append(f"| {c['label']} | {_fmt_curve(c['default'])} | {fitted} | {quality} | "
                     f"{c.get('why') or ''} |")
    notes = report.get("notes") or []
    if notes:
        lines += ["", "## Notes", ""] + [f"- {n}" for n in notes]
    lines += ["", "Files: `report.json` (every number), `curves.calibrated.json` (the curves), "
              "`probes/` (every capture as a WAV), `plan.json`, `probes.jsonl`.", ""]
    return "\n".join(lines)


def _report_notes(report: dict) -> list[str]:
    notes = []
    kept = [c["label"] for c in report["curves"].values() if c.get("status") in ("kept", "reverted")]
    if kept:
        notes.append("Kept at the default (not measurable or not better): " + ", ".join(kept) + ". "
                     "On the real S-1 a knob with no measurable response usually means the CC did not "
                     "arrive, or the twin models that knob differently from the hardware.")
    for pre in report.get("preflight") or []:
        for w in pre.get("warnings", []):
            notes.append(w)
    pr = report["probes"]
    if pr["silent"] or pr["clipped"]:
        notes.append(f"{pr['silent']} silent and {pr['clipped']} clipped probes were left out.")
    phase = (report["curves"].get("lfo_rate") or {}).get("lfo_phase_deg")
    if phase is not None and abs(phase) > 45.0:
        notes.append(f"The S-1's LFO starts at phase {phase:+.0f} degrees on each key; the twin starts at 0 "
                     "(rising). The LFO fits here are phase-free, but a match with a strong LFO stays off "
                     "until the twin gets a start-phase setting.")
    seam = report["calibrate_seam"]["by_note"]
    if seam:
        diffs = [abs(v["held_out_feature_error"] - v["this_report"]) for v in seam.values()
                 if v["this_report"] is not None]
        if diffs and max(diffs) > 1e-6:
            notes.append("twin.calibrate() and this report disagree on the joint gap by up to "
                         f"{max(diffs):.2g}; they should agree exactly.")
    return notes


def install_curves(payload: dict, dest: Path) -> Path:
    """Write the curves where the cockpit looks, keeping the previous file beside it."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.copy2(dest, dest.with_name(dest.stem + ".previous.json"))
    _atomic_write_text(dest, json.dumps(payload, indent=1))
    return dest


# ─────────────────────────────────────────────────────────────────────────────
# The command
# ─────────────────────────────────────────────────────────────────────────────
SETUP_STEPS = (
    "Connect the S-1 to the Mac with its USB-C data cable. A charge-only cable gives power "
    "but no MIDI and no sound.",
    "Stop the cockpit if it runs (`synth off`), and let nothing else play the S-1: no "
    "keyboard, no pattern, no arpeggio.",
    "Do not touch the S-1's knobs until the run ends. Every probe sets each sound "
    "parameter over MIDI, and a knob turn would change what is measured. Their positions "
    "do not matter.",
    "Set the S-1's volume knob to about 3 o'clock. The check below says if the level is too "
    "low or too high.",
    "Room noise does not matter: the sound travels over USB, not through a microphone. If "
    "the test notes bother you, plug headphones into the S-1.",
    "Keep the Mac awake and plugged in. If anything stops the run, the resume command is "
    "printed; nothing recorded is lost.",
)


def print_setup(emit: Callable[[str], None] = print) -> None:
    emit("Before we start:")
    for n, step in enumerate(SETUP_STEPS, 1):
        emit(f"  {n}. {step}")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="synth-calibrate",
        description="Measure the real S-1 and fit the twin's curves to it (about 15 minutes "
                    "for 300 probes). Writes ~/.synth/twin/curves.calibrated.json and a report.")
    ap.add_argument("--check", action="store_true",
                    help="Only check the setup: ports, latency, pitch, level and hiss "
                         "(plays three short notes)")
    ap.add_argument("--probes", type=int, default=DEFAULT_PROBES,
                    help=f"Probe budget (default {DEFAULT_PROBES}, about 2.4 s each on the S-1)")
    ap.add_argument("--curves", default=None,
                    help="Only sweep these curves (comma-separated, e.g. cutoff,attack)")
    ap.add_argument("--seed", type=int, default=0, help="Plan seed (default 0)")
    ap.add_argument("--resume", metavar="DIR", default=None,
                    help="Continue a stopped run folder (or re-fit a finished one)")
    ap.add_argument("--fit-only", action="store_true",
                    help="With --resume: fit on the probes recorded so far, play nothing")
    ap.add_argument("--stop-after", type=int, default=None, metavar="N",
                    help="Stop after N probes this session (resume later with --resume)")
    ap.add_argument("--dry-run", action="store_true",
                    help="No hardware: a pretend S-1 plays the probes (writes only the run folder)")
    ap.add_argument("--fake", choices=("placeholder", "twin"), default="placeholder",
                    help="The pretend S-1 for --dry-run (default placeholder)")
    ap.add_argument("--port", "-p", default=None, help="MIDI port (substring ok); auto-detects the S-1")
    ap.add_argument("--channel", "-c", type=int, default=3, help="MIDI channel 1-16 (default 3)")
    ap.add_argument("--device", "-d", default=None,
                    help="Audio input device (index or name); default: the S-1's USB input")
    ap.add_argument("--root", default=None, help="Where run folders go (default ~/.synth/calibration)")
    ap.add_argument("--curves-out", default=None,
                    help="Where to install the curves (default ~/.synth/twin/curves.calibrated.json)")
    ap.add_argument("--no-install", action="store_true", help="Write the curves only into the run folder")
    ap.add_argument("--workers", type=int, default=max(1, min(8, (os.cpu_count() or 2) - 1)),
                    help="Processes for the fit (default: CPU count - 1, at most 8)")
    ap.add_argument("--fit-sr", type=int, default=WORKING_SR, help=argparse.SUPPRESS)
    ap.add_argument("--passes", type=int, default=2, help=argparse.SUPPRESS)
    ap.add_argument("--grid", type=int, default=17, help=argparse.SUPPRESS)
    ap.add_argument("--refine", type=int, default=8, help=argparse.SUPPRESS)
    ap.add_argument("--yes", "-y", action="store_true", help="Do not wait for Enter before probing")
    return ap


def _fit_seconds_estimate(plan: list[Probe], cfg: FitConfig) -> float:
    """Rough fit time: time a twin render + feature pass (and one vibrato measurement)
    on this machine, then scale by the plan's inversions and evaluations."""
    from .twin import Twin

    fit = [p for p in plan if p.kind == "sweep" and not p.holdout]
    n_vib = sum(1 for p in fit if SWEEP_BY_CURVE[p.curve].objective == "vibrato")
    n_eval = sum(1 for p in plan if p.kind == "joint" or (p.kind == "sweep" and p.holdout))
    tw = Twin(sr=cfg.sr, seconds=RECORD_S, gate_fraction=GATE_FRACTION)
    k = np.full(tw.k_dim, 0.5)
    t0 = time.perf_counter()
    for _ in range(2):
        audio = np.asarray(tw.render(k, None, 67), np.float32)
        _features(audio, cfg.sr)
    per = (time.perf_counter() - t0) / 2
    t0 = time.perf_counter()
    vibrato_extent(prepare(AudioClip(audio, cfg.sr)).samples, cfg.sr)
    per_vib = time.perf_counter() - t0
    per_probe = cfg.grid + cfg.refine + 2
    inversions = (len(fit) * per + n_vib * per_vib) * per_probe * cfg.passes
    per_full = per * (WORKING_SR / cfg.sr)
    return (inversions + 4 * n_eval * per_full) / max(1, cfg.workers) + 5.0


def _check(args: argparse.Namespace, emit: Callable[[str], None]) -> int:
    rig = FakeRig(kind=args.fake) if args.dry_run else None
    try:
        if rig is None:
            if cockpit_running():
                emit("Note: the cockpit is running. That is fine for this check; stop it (`synth off`) "
                     "before the full calibration run.")
            rig = open_hardware(args.port, args.channel, args.device)
        emit(f"Checking {rig.describe}:")
        pre = preflight(rig, emit)
        emit(f"  One probe takes about {pre.probe_seconds:.2f} s here.")
        emit("Ready for calibration.")
        return 0
    except SetupError as e:
        emit(f"Problem: {e}")
        return 2
    finally:
        if rig is not None:
            rig.restore(CLEAN_PATCH)
            rig.close()


def main(argv: list[str] | None = None, emit: Callable[[str], None] = print) -> int:
    args = build_parser().parse_args(argv)
    try:
        import autograd  # noqa: F401
    except ImportError:
        emit("The calibration fit needs the twin extra: .venv/bin/pip install -e '.[studio,twin]'")
        return 2
    if args.check:
        return _check(args, emit)

    curves_sel = [c.strip() for c in args.curves.split(",") if c.strip()] if args.curves else None
    cfg = FitConfig(sr=int(args.fit_sr), grid=int(args.grid), refine=int(args.refine),
                    passes=max(1, int(args.passes)), workers=max(1, int(args.workers)))
    try:
        if args.resume:
            run_dir = Path(args.resume).expanduser()
            plan, config = read_plan(run_dir)
            stamp = config.get("stamp", run_dir.name)
            dry = bool(config.get("dry_run", args.dry_run))
            fake = config.get("fake", args.fake) if dry else None
        else:
            stamp = _timestamp()
            root = Path(args.root).expanduser() if args.root else default_root()
            run_dir = root / (stamp + ("-dry-run" if args.dry_run else ""))
            dry, fake = bool(args.dry_run), (args.fake if args.dry_run else None)
            plan = make_plan(args.probes, args.seed, curves_sel)
            write_plan(run_dir, plan, {"stamp": stamp, "seed": args.seed, "probes": args.probes,
                                       "curves": curves_sel, "dry_run": dry, "fake": fake,
                                       "hold_s": HOLD_S, "tail_s": TAIL_S})
    except (SetupError, ValueError) as e:
        emit(f"Problem: {e}")
        return 2

    source = f"synth-calibrate {'--dry-run ' if dry else ''}{stamp}"
    levels = sum(1 for p in plan if p.kind == "sweep")
    joint = sum(1 for p in plan if p.kind == "joint")
    reps = sum(1 for p in plan if p.kind == "repeat")
    done = {int(r["i"]) for r in read_rows(run_dir)}
    remaining = [p for p in plan if p.i not in done]
    emit(f"Run folder: {run_dir}")
    emit(f"Plan: {len(plan)} probes ({levels} sweep, {joint} joint, {reps} repeat); "
         f"{len(done)} already recorded.")

    if remaining and not args.fit_only:
        if dry:
            emit(f"Dry run: a pretend S-1 ({fake}) plays every probe. Nothing is sent to hardware.")
        print_setup(emit)
        if not dry and not args.yes:
            try:
                input("Press Enter when the S-1 is ready (Ctrl-C to quit). ")
            except (KeyboardInterrupt, EOFError):
                emit("\nStopped before any probe. Nothing was sent.")
                return 130
        rig = None
        status = "stopped"
        try:
            if dry:
                rig = FakeRig(kind=fake or "placeholder", seed=args.seed)
            else:
                if cockpit_running():
                    emit("Note: the cockpit is still running. Stop it (`synth off`) if you play the "
                         "keyboard; a stray note would spoil a probe.")
                rig = open_hardware(args.port, args.channel, args.device)
            emit(f"Preflight on {rig.describe}:")
            pre = preflight(rig, emit)
            if pre.floor_samples is not None:
                _save_wav(run_dir / "floor.wav", pre.floor_samples, WORKING_SR)
            _append_meta(run_dir, {"started": datetime.now().isoformat(timespec="seconds"),
                                   "probes_before": len(done), "rig": rig.describe,
                                   "preflight": pre.to_json()})
            est = estimate_seconds(len(remaining), pre.probe_seconds)
            fit_est = _fit_seconds_estimate(plan, cfg)
            emit(f"Estimate: {len(remaining)} probes x {pre.probe_seconds:.2f} s = {_fmt_minutes(est)} "
                 f"of probing, then about {_fmt_minutes(fit_est)} of fitting (the S-1 is free by then).")
            if dry:
                real = hardware_probe_seconds(pre.latency_s)
                emit(f"On the real S-1: {len(remaining)} probes x {real:.2f} s = "
                     f"{_fmt_minutes(len(remaining) * real)} ({SETTLE_S:.2f} s settle + "
                     f"{HOLD_S:.1f} s note + {TAIL_S:.1f} s tail + latency and I/O).")
            if not dry and shutil.which("caffeinate"):
                subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            status = run_probes(rig, plan, run_dir, pre.overrides, emit, stop_after=args.stop_after)
        except SetupError as e:
            emit(f"Problem: {e}")
            return 2
        except KeyboardInterrupt:
            status = "stopped"
        finally:
            if rig is not None:
                rig.restore(CLEAN_PATCH)
                rig.close()
                emit("The S-1 is back on a clean patch (the init values).")
        if status != "done":
            n_now = len(read_rows(run_dir))
            emit(f"\nStopped after {n_now} of {len(plan)} probes. Nothing recorded is lost.")
            emit(f"Resume:  synth-calibrate --resume {run_dir}")
            emit(f"Or fit what is there:  synth-calibrate --resume {run_dir} --fit-only")
            return 130

    if not read_rows(run_dir):
        emit("No probes recorded yet, so there is nothing to fit.")
        return 2

    emit(f"Fitting ({cfg.workers} worker{'s' if cfg.workers != 1 else ''}, {cfg.passes} passes) …")
    t0 = time.perf_counter()
    report = fit_run(run_dir, cfg, emit)
    meta_path = run_dir / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {"sessions": []}
    j = report["gap"]["joint"]
    worse = bool(j["n"] and j["after"] is not None and j["before"] is not None and j["after"] > j["before"])
    report.update({
        "version": 1, "source": source, "created": datetime.now().isoformat(timespec="seconds"),
        "dry_run": dry, "fake": fake, "run_dir": str(run_dir),
        "planned": len(plan), "fit_seconds": round(time.perf_counter() - t0, 1),
        "preflight": [s["preflight"] for s in meta.get("sessions", []) if "preflight" in s],
        "worse": worse,
    })
    report["notes"] = _report_notes(report)
    fitted = {n: tuple(v) for n, v in report.pop("fitted_curves").items()}
    payload = curves_payload(fitted, source, calibrated=not dry)
    _atomic_write_text(run_dir / "curves.calibrated.json", json.dumps(payload, indent=1))
    installed = None
    if not dry and not args.no_install and not worse:
        dest = Path(args.curves_out).expanduser() if args.curves_out else default_curves_out()
        installed = str(install_curves(payload, dest))
    report["installed"] = installed
    _atomic_write_text(run_dir / "report.json", json.dumps(report, indent=1, default=_json_default))
    (run_dir / "report.md").write_text(report_markdown(report))

    emit("")
    emit(headline(report))
    for m, g in report["gap"]["modules"].items():
        emit(f"  {MODULE_LABELS[m]:<11} {g['before']:.3f} -> {g['after']:.3f}  "
             f"({g['n']} held-out sweep probes)")
    emit(f"Report: {run_dir / 'report.md'}")
    if installed:
        emit(f"Curves installed: {installed}")
    elif dry:
        emit(f"Curves (dry run, not installed): {run_dir / 'curves.calibrated.json'}")
    elif worse:
        emit("The fitted curves did worse than the defaults, so they were NOT installed. "
             "See the report.")
    return 0


def _json_default(o: Any) -> Any:
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


__all__ = [
    "CALIBRATION_BASE", "CLEAN_PATCH", "FAKE_TRUE_CURVES", "FakeRig",
    "FitConfig", "HardwareRig", "Probe", "ProbeDriver", "SWEEPS", "SetupError", "curves_payload",
    "fit_run", "load_curves", "main", "make_plan", "preflight", "run_probes",
]


if __name__ == "__main__":
    raise SystemExit(main())
