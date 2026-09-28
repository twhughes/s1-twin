"""The twin matcher as a stream of frames — one per optimization step.

This is the session/phase logic that used to live inside ``soft/server.py``, pulled
out so two front ends share it (docs/design/BUILD.md §2.4):

* the cockpit's ``/ws/match`` (``synth/web/match_ws.py``) streams :attr:`Step.frame`
  as-is: the candidate in **CC space on every frame** (``"cc": {cc: value}``), so any
  knob in the UI can animate to it;
* the standalone soft page (``soft/server.py``) converts each step's ``k``/``s`` to its
  own page units, so its frames and tests are unchanged.

It is the gradient-descent matcher over the differentiable twin
(:mod:`synth.match.twin`): render a candidate patch on the target's notes, score it
with the twin's differentiable spectral loss, step Adam, repeat. Nothing here plays
audio or touches hardware; candidates are rendered to arrays for the loss only.

Phases (the ``"phase"`` of each frame):

1. ``pitch`` — the note SET: the user's seeded notes (``seeded: true``), or the cold-
   start harmonic-salience detection. One frame, at the starting patch.
2. ``gd`` — Adam on the shared patch against the whole chord, with random restarts
   (keep the best). ``restart`` says which start the frame belongs to. Round 4: each descent
   runs until it stops improving (capped), at a cosine learning rate; after each start the
   other switch settings are tried with a short re-descent (their frames carry ``trying``),
   a winner is kept, and a final polish runs from the overall best.
3. ``note-search`` — cold start only: transpose the set ±12, drop the weakest voice,
   add the next candidate; re-descend briefly at each; ``improved`` marks a win.
   Then (no frames) the discrete switches are enumerated at the best patch.
4. ``done`` — the best patch rendered at full resolution, scored, and returned with
   A/B WAVs.

Frame schema (JSON-ready; every key below is on every frame unless noted)::

    {"phase": "pitch"|"gd"|"note-search"|"done",
     "notes": [int], "chord_name": str,         # the note SET + its label ("G3+B3+D4")
     "note": int, "note_name": str,             # the lowest note (back-compat)
     "seeded": bool,                            # pitch + done frames
     "iter": int, "total": int,                 # progress within the phase
     "restart": int,                            # pitch, gd, done frames
     "improved": bool,                          # note-search frames
     "loss": float, "best_loss": float,         # the twin's loss (lower is closer)
     "cc": {"<cc>": int},                       # the candidate, all 21 twin CCs
     "wave": {"spc", "y", "level"},             # ~2 cycles of the candidate (a plume)
     "target_wave": {"spc", "y", "level"}}      # pitch + done frames: the target's cycles
    done adds: "closeness": float (0..100, the plain metric), "seconds": float,
               "steps": int, "match_wav_b64": str, "target_wav_b64": str
    optional (round 4; old readers ignore them):
      gd "trying": str      # a switch trial ("Volume shape: Gate") or "a final polish"
      gd "starts": int      # how many starts the run makes (a round-4 budget; ``total`` is then
                            # the most gd steps the budget allows, and the run usually stops sooner)
      done "finished": true # the run was told to finish early; the best so far
    optional (round 6):
      gd "trying": str      # also a start's words ("Square, filter open", "a random start") or a
                            # note length the run tries ("note held 0.4 s", "note held to the end")
      done "held": float    # the key-up time the match used, in seconds (the render's length when
                            # the note is held through it); its A/B audio uses it. Only when the
                            # budget scans the length or the plan gives it (``gate_s``)

``wave.y`` is ``WAVE_CYCLES`` cycles of the lowest note resampled to ``spc`` samples per
cycle, with 2 samples of margin at each end (what ``design/draw.js plumePts`` needs),
peak-normalized; ``level`` is the snippet's RMS over the whole render's RMS, so a
candidate that is still quiet at that moment draws a small loop. Candidates are taken
at the moment the target is loudest.

Recorded matches for the static page (BUILD.md §2.4) are these exact frames, written by
``python -m synth.match.twin_session record <audio> …`` (see :func:`record_match`).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import math
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterator

import numpy as np

from . import ANALYSIS_SECONDS, WORKING_SR

# Optimizer budget. Getting the patch right beats getting it fast. Each descent runs until it
# stops improving (the plateau rule: no relative gain above ``tol`` over ``patience`` steps, after
# ``min_iters``), capped at ``gd_iters``, with a cosine learning rate from LR down to ``lr_min``.
# After each start's descent every switch setting (sub octave, LFO wave, volume shape) is scored at
# its knobs, and the ``switch_top`` most promising ones (-1: all) are re-descended briefly
# (``switch_iters``); a winner is adopted and the descent continues under it
# (``continue_iters``). The LFO wave only shows at about the right rate, which a descent rarely finds
# on its own, so the same stage also scans LFO settings (each wave x rates x on the pitch or the
# filter; renders only) and re-descends the ``lfo_top`` best. A final low-step polish
# (``polish_iters``) starts from the overall best.
# QUICK is the watchable budget (about half a minute); THOROUGH the default (2-3 minutes); DEEP
# many starts and every switch setting (up to about 10 minutes). Time scales ~N x with the
# chord's voice count. A budget without the round-4 keys runs the old search exactly: a fixed
# ``gd_iters`` per start at a constant LR, and one silent switch sweep at the end (tests pin tiny
# budgets that way; tools/match_benchmark.py runs "old" that way).
QUALITY_PRESETS: dict[str, dict[str, Any]] = {
    "quick":    {"gd_iters": 90, "restarts": 1, "neighbor_iters": 14,
                 "patience": 15, "min_iters": 30, "tol": 1e-3, "lr_min": 0.008,
                 "switch_top": 1, "switch_iters": 24, "continue_iters": 40, "polish_iters": 20, "lfo_top": 1,
                 "plain_starts": 1, "prior": 0.02, "gate_scan": 3, "floor_match": 1},
    "thorough": {"gd_iters": 240, "restarts": 4, "neighbor_iters": 24,
                 "patience": 25, "min_iters": 60, "tol": 5e-4, "lr_min": 0.008,
                 "switch_top": 3, "switch_iters": 40, "continue_iters": 120, "polish_iters": 80,
                 "lfo_top": 1, "plain_starts": 4, "prior": 0.02, "gate_scan": 3, "floor_match": 1},
    "deep":     {"gd_iters": 360, "restarts": 8, "neighbor_iters": 32,
                 "patience": 35, "min_iters": 80, "tol": 3e-4, "lr_min": 0.008,
                 "switch_top": -1, "switch_iters": 30, "continue_iters": 160, "polish_iters": 150,
                 "lfo_top": 2, "plain_starts": 8, "prior": 0.02, "gate_scan": 3, "floor_match": 1},
}
# Round 6: where the starts begin. A start in the middle of the knob cube sets every extra half up
# (noise, sub, vibrato, the LFO amounts and depth), and the search often stays in such an "invented
# extras" basin. So the starts are PLAIN patches scored at the target first (renders only): a saw, a
# square, both, or either with a sub, the filter open or half open, with the filter envelope off or on,
# the volume held or short, every extra at 0. The best ``plain_starts`` (distinct in their oscillators
# and filter) are the starts; random starts fill any rest, with the extras near 0. ``prior``: a mild
# L1 penalty on the extras' levels (k space) in the search loss only; the done frame's closeness is
# the plain metric as before.
EXTRAS = ("noise_lvl", "sub_lvl", "lfo_to_pitch", "lfo_to_cutoff", "lfo_depth")
PLAIN_MIXES: tuple[tuple[str, dict[str, float]], ...] = (
    ("Saw", {"saw_lvl": 1.0}),
    ("Square", {"square_lvl": 1.0}),
    ("Saw and Square", {"saw_lvl": 0.8, "square_lvl": 0.8}),
    ("Saw and Sub", {"saw_lvl": 1.0, "sub_lvl": 0.6}),
    ("Square and Sub", {"square_lvl": 1.0, "sub_lvl": 0.6}),
)
PLAIN_FILTERS = (("open", 1.0), ("half open", 0.6))             # cutoff (k)
PLAIN_ENVELOPES = (0.0, 0.35)                                    # filter envelope amount (k)
PLAIN_VOLUMES = ({"decay": 0.45, "sustain": 0.6, "release": 0.35},    # held
                 {"decay": 0.35, "sustain": 0.2, "release": 0.25})    # short
PLAIN_REST = {"pulse_width": 0.0, "resonance": 0.1, "key_follow": 0.0, "fine_tune": 0.5, "lfo_rate": 0.45,
              "attack": 0.0}
# The note's length: where the key goes up. A recording's is unknown; the twin's own is 1.2 s (2.0 s x 0.6),
# the Match view's test note too. ``gate_scan`` (rounds): before the first descent, other key-up times
# around it are scored on the best few starts (renders only), stepping on while one wins; the winner is
# kept (both twins rebuilt, the starts re-scored under it). After the first start's descent the same scan
# runs at its knobs, and a winner there is kept with a brief re-descent. (Only the first scan finds a
# short note: a descent under the wrong length fakes it with the envelope, and at those knobs the length
# hardly shows.) A key-up past the render's end holds the note through the window (HELD).
GATE_PRE_STARTS = 6
GATE_DEFAULT = 1.2
GATE_FACTORS = (0.5, 0.7, 1.4, 2.0)
GATE_MIN = 0.1
HELD = 60.0
# The target's noise floor. A recording (even a 16-bit WAV of the twin) never falls silent: after the
# note its tail sits at a noise floor (-96 dB for 16-bit), while a render falls to true silence
# (-180 dB). On a log spectrum that gap outweighs the note (a short note's true settings scored 3.33,
# 0.03 without the WAV). ``floor_match``: quiet noise at the target's floor (its quietest 20 ms, capped
# FLOOR_CAP_DB under its peak) is added to every candidate in the search loss, so tails compare alike.
FLOOR_FRAME_S = 0.02
FLOOR_CAP_DB = -60.0
DEFAULT_QUALITY = "thorough"
LR = 0.08                      # Adam learning rate (the start of each cosine schedule)
# Adam's first step moves every knob by about the rate, so a descent that starts from good knobs
# starts gently or it throws them away (the LFO probes: 0.05 lost the scan's gain, 0.02 kept it).
LR_SWITCH = 0.03               # re-descending under another switch setting
LR_LFO = 0.02                  # re-descending from an LFO setting the scan found
LR_CONTINUE = 0.02             # continuing under a winner
LR_POLISH = 0.015              # the final polish: small steps from the overall best
LFO_WAVES = (2, 3, 0)          # the LFO waves the matcher tries (twin.py S_PARAMS lfo_shape)
WARMUP = 8                     # ...and ramps its rate up over its first steps (the probes: without it,
                               # a trial from the scan's LFO lost its start and stopped before recovering)
POLISH_WORDS = "a final polish"
MAX_NOTES = 4                  # a chord target is at most 4 notes
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
PHASES = ("pitch", "gd", "note-search", "done")

# Frame pacing (seconds slept between streamed frames), for the WebSocket front ends.
DEFAULT_THROTTLE = 0.02
MAX_THROTTLE = 0.4

# The cheaper search render that carries the many gradient evaluations (mirrors
# TwinMatcher); the final candidate is rendered at the full twin resolution.
SEARCH_SR = 16000
SEARCH_SECONDS = 1.5
S_DEFAULT: dict[str, int] = {"sub_octave": 2, "lfo_shape": 2, "amp_env_mode": 1}

# The plume snippet carried by every frame.
WAVE_SPC = 64
WAVE_CYCLES = 2

_NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


class UploadError(ValueError):
    """An uploaded file the matcher cannot use. ``str(exc)`` is user-facing."""


# ── note labels ──────────────────────────────────────────────────────────────
def note_name(note: int) -> str:
    return f"{_NOTE_NAMES[note % 12]}{note // 12 - 1}"


def chord_name(notes: list[int]) -> str:
    """A single note keeps its name; a chord joins names low→high with ``+``."""
    ordered = sorted(notes)
    if len(ordered) == 1:
        return note_name(ordered[0])
    return "+".join(note_name(n) for n in ordered)


def note_fields(notes: list[int]) -> dict[str, Any]:
    """The note-SET keys every frame carries (plus the lowest note, for back-compat)."""
    ordered = sorted(int(n) for n in notes)
    return {
        "notes": ordered,
        "chord_name": chord_name(ordered),
        "note": ordered[0],
        "note_name": note_name(ordered[0]),
    }


# ── request parsing (shared by both WebSocket front ends) ────────────────────
def parse_notes(raw: str | None) -> list[int]:
    """``"60,64,67"`` → sorted unique MIDI notes (0..127), at most :data:`MAX_NOTES`.
    Junk tokens are skipped; an empty result means "cold start"."""
    out: set[int] = set()
    for tok in (raw or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            v = int(tok)
        except ValueError:
            continue
        if 0 <= v <= 127:
            out.add(v)
    return sorted(out)[:MAX_NOTES]


def parse_quality(raw: str | None) -> str:
    return raw if raw in QUALITY_PRESETS else DEFAULT_QUALITY


def clamp_throttle(raw: Any, default: float = DEFAULT_THROTTLE) -> float:
    try:
        value = float(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return min(MAX_THROTTLE, max(0.0, value))


def cc_init(cc_map: dict[Any, Any]) -> tuple[np.ndarray, dict[str, int]]:
    """A CC map (the synth's current knobs) → a warm start ``(k, s)``.

    CCs the map lacks take the schema default (``Twin.cc_to_k``); switch CCs the map
    carries set the discrete choices, when in range. Non-numeric entries are ignored.
    """
    from ..schema import param_by_cc
    from .twin import S_PARAMS, Twin

    clean: dict[int, int] = {}
    for key, value in (cc_map or {}).items():
        try:
            clean[int(key)] = int(round(float(value)))
        except (TypeError, ValueError):
            continue
    k = Twin().cc_to_k(clean)
    s: dict[str, int] = {}
    for sp in S_PARAMS:
        if sp.cc in clean:
            p = param_by_cc(sp.cc)
            lo, hi = (p.min_val, p.max_val) if p else (0, 127)
            if lo <= clean[sp.cc] <= hi:
                s[sp.name] = clean[sp.cc]
    return k, s


def decode_upload(raw: bytes) -> np.ndarray:
    """Uploaded audio-file bytes → mono float64 at the twin's rate (``WORKING_SR``).

    Raises :class:`UploadError` (user-facing message) on empty, unreadable, or silent
    input. The caller checks the size cap first (``MAX_UPLOAD_BYTES``)."""
    from .capture import load_audio

    if not raw:
        raise UploadError("The file was empty. Choose an audio file (WAV, AIFF, or FLAC).")
    with tempfile.NamedTemporaryFile(suffix=".audio", delete=False) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name
    try:
        clip = load_audio(tmp_path)
    except Exception as exc:  # noqa: BLE001 - any decode failure is a client error
        raise UploadError(
            "Could not read that file as audio. Try a WAV, AIFF, or FLAC file."
        ) from exc
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
    samples = np.asarray(clip.samples, dtype=np.float64)
    if samples.size == 0 or not np.all(np.isfinite(samples)) or float(np.abs(samples).max()) < 1e-6:
        raise UploadError("That audio is silent. Choose a recording with a note in it.")
    return samples


def detect(samples: np.ndarray) -> tuple[list[int], list[int]]:
    """Cold start: the detected note set (low→high) and the salience-ranked candidate
    list (strongest first, one past the set) the note-search draws from."""
    from .analyze import _harmonic_salience_notes, detect_notes, probe_note
    from .capture import AudioClip

    clip = AudioClip(np.asarray(samples, dtype=np.float32), WORKING_SR)
    notes = detect_notes(clip, max_notes=MAX_NOTES)
    if not notes:  # unpitched material: fall back to the probe note (C3 when unvoiced)
        notes = [probe_note(clip)]
    ranked = [nb for nb, _sal in _harmonic_salience_notes(clip, max_notes=MAX_NOTES + 1)]
    return sorted(notes), ranked


@dataclass
class Plan:
    """Everything one match needs, parsed from a request."""

    samples: np.ndarray
    notes: list[int]
    seeded: bool
    quality: str = DEFAULT_QUALITY
    cold_candidates: list[int] | None = None
    init_k: np.ndarray | None = None
    init_s: dict[str, int] = field(default_factory=dict)
    gate_s: float | None = None                 # the key-up time: target_prep's guess (None: the twin's 1.2 s);
                                                # the run's note-length scan starts from it
    crop: tuple[float, float] | None = None     # where ``samples`` lie in the upload, in seconds


def plan(raw: bytes, notes: str | None = None, quality: str | None = None,
         crop: str | None = None) -> Plan:
    """Decode an upload, crop it to its main sound (:mod:`target_prep`: whatever silence, noise or
    clicks surround it; ``crop`` = "t0,t1" seconds overrides the found edges), and fix the note set:
    seeded notes are used exactly (the recommended path); none → cold-start detection on the crop.
    Raises :class:`UploadError`."""
    from .target_prep import prepare_upload

    _take, prep = prepare_upload(raw, crop)             # decode_upload, then prepare_target
    edges = {"gate_s": prep.gate_s, "crop": (prep.t0, prep.t1)}
    seeded_notes = parse_notes(notes)
    if seeded_notes:
        return Plan(prep.samples, seeded_notes, True, parse_quality(quality), **edges)
    found, ranked = detect(prep.samples)
    return Plan(prep.samples, found, False, parse_quality(quality), cold_candidates=ranked, **edges)


# ── audio snippets ────────────────────────────────────────────────────────────
def wav_b64(samples: Any, samplerate: int) -> str:
    """16-bit PCM WAV of ``samples`` at ``samplerate``, base64-encoded."""
    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, np.clip(np.asarray(samples, dtype=np.float64), -1.0, 1.0),
             int(samplerate), format="WAV", subtype="PCM_16")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def loudest_time(audio: np.ndarray, sr: int, lo: float = 0.03, hi: float = 1.3) -> float:
    """Seconds at which ``audio`` is loudest (10 ms RMS frames within [lo, hi])."""
    a = np.asarray(audio, dtype=np.float64)
    hop = max(1, int(sr * 0.01))
    start, stop = int(lo * sr), min(len(a) - hop, int(hi * sr))
    if stop <= start:
        return lo
    best_t, best_e = lo, -1.0
    for i in range(start, stop, hop):
        e = float(np.mean(a[i:i + hop] ** 2))
        if e > best_e:
            best_t, best_e = i / sr, e
    return best_t


def wave_snippet(audio: np.ndarray, sr: int, f0: float, t0: float) -> dict[str, Any]:
    """``WAVE_CYCLES`` cycles of ``audio`` at ``t0`` s, resampled to ``WAVE_SPC`` samples
    per cycle (+2 margin samples each side), peak-normalized, with its relative level."""
    a = np.asarray(audio, dtype=np.float64)
    period = sr / max(f0, 1e-6)
    step = period / WAVE_SPC
    n_out = WAVE_SPC * WAVE_CYCLES + 4
    t0 = min(max(t0, 0.0), max(0.0, (len(a) - period * (WAVE_CYCLES + 0.2)) / sr))
    idx = t0 * sr - 2 * step + np.arange(n_out) * step
    y = np.interp(idx, np.arange(len(a)), a, left=0.0, right=0.0)
    whole = float(np.sqrt(np.mean(a * a))) if a.size else 0.0
    rms = float(np.sqrt(np.mean(y * y)))
    peak = float(np.abs(y).max())
    y = y / peak if peak > 1e-12 else y
    level = rms / whole if whole > 1e-12 else 0.0
    return {"spc": WAVE_SPC, "y": [round(float(v), 4) for v in y], "level": round(level, 3)}


# ── the search's rules (pure) ────────────────────────────────────────────────
def cosine_lr(t: int, cap: int, lr_max: float, lr_min: float | None) -> float:
    """The learning rate at step ``t`` (1-based) of a descent capped at ``cap`` steps: a half cosine
    from ``lr_max`` down to ``lr_min`` over the cap (constant ``lr_max`` when ``lr_min`` is None)."""
    if lr_min is None or cap <= 1:
        return float(lr_max)
    frac = min(1.0, max(0.0, (t - 1) / (cap - 1)))
    return float(lr_min + 0.5 * (lr_max - lr_min) * (1.0 + math.cos(math.pi * frac)))


def plateaued(bests: list[float], patience: int | None, tol: float = 0.0, min_iters: int = 0) -> bool:
    """True when a descent should stop: after ``min_iters`` steps, its best loss has improved by no
    more than ``tol`` (relative) over the last ``patience`` steps. ``bests[i]`` is the best loss after
    step i+1. No ``patience`` (None or 0) means never: the descent runs to its cap."""
    if not patience or len(bests) < max(int(min_iters), int(patience) + 1):
        return False
    then, now = bests[-1 - int(patience)], bests[-1]
    if not math.isfinite(then):
        return False
    return (then - now) <= tol * max(abs(then), 1e-12)


# The switches in plain words, as the Match view names them (views/match.js STAGES; match.check.mjs
# holds the two to the same words).
SWITCH_WORDS: dict[str, tuple[str, dict[int, str]]] = {
    "sub_octave": ("Sub octave", {2: "\u22121", 1: "\u22122", 0: "\u22122 asym"}),
    "lfo_shape": ("LFO wave", {0: "Saw", 1: "Inverse saw", 2: "Triangle", 3: "Square",
                               4: "Random", 5: "Noise"}),
    "amp_env_mode": ("Volume shape", {0: "Gate", 1: "Envelope"}),
}


def switch_words(s: dict[str, int], base: dict[str, int]) -> str:
    """The switches in ``s`` that differ from ``base``, in words: "Volume shape: Gate" or
    "Sub octave: −2, LFO wave: Square" (in the order the view lists them)."""
    parts = []
    for name in ("sub_octave", "lfo_shape", "amp_env_mode"):
        if name in s and s.get(name) != base.get(name):
            label, words = SWITCH_WORDS[name]
            parts.append(f"{label}: {words.get(int(s[name]), s[name])}")
    return ", ".join(parts)


def switch_candidates(scored: list[tuple[dict[str, int], float]], current: dict[str, int],
                      current_loss: float, top: int) -> list[dict[str, int]]:
    """The most promising other switch settings, best first, from ``scored`` = [(setting, loss at
    the start's best knobs)]. A setting that renders exactly as the current one does (its switch
    does nothing at these knobs, e.g. the sub octave with Sub at 0) is left out, and settings that
    sound the same as each other count once (the one that changes fewest switches). ``top`` < 0
    keeps them all."""
    def rel_same(a: float, b: float) -> bool:
        return abs(a - b) <= 1e-9 * max(abs(a), abs(b), 1e-12)

    def changes(cfg: dict[str, int]) -> int:
        return sum(cfg.get(k) != current.get(k) for k in cfg)

    alts = [(cfg, loss) for cfg, loss in scored
            if cfg != current and math.isfinite(loss) and not rel_same(loss, current_loss)]
    alts.sort(key=lambda cl: (cl[1], changes(cl[0])))
    kept: list[tuple[dict[str, int], float]] = []
    for cfg, loss in alts:
        if not any(rel_same(loss, k_loss) for _, k_loss in kept):
            kept.append((cfg, loss))
    picked = [cfg for cfg, _ in kept]
    return picked if top < 0 else picked[:max(0, int(top))]


def resolve_budget(quality: str | None, budget: dict[str, Any] | None = None) -> dict[str, Any]:
    """The budget a run uses: ``budget`` as given (tests and tools), else the preset for
    ``quality``. Round-4 keys a budget leaves out default to the old search (see QUALITY_PRESETS)."""
    b = dict(budget or QUALITY_PRESETS.get(quality or "", QUALITY_PRESETS[DEFAULT_QUALITY]))
    b.setdefault("patience", None)
    b.setdefault("min_iters", 0)
    b.setdefault("tol", 0.0)
    b.setdefault("lr_min", None)
    b.setdefault("switch_top", 0)
    b.setdefault("switch_iters", 0)
    b.setdefault("continue_iters", 0)
    b.setdefault("polish_iters", 0)
    b.setdefault("lfo_top", 0)
    b.setdefault("plain_starts", 0)
    b.setdefault("prior", 0.0)
    b.setdefault("gate_scan", 0)
    b.setdefault("floor_match", 0)
    return b


def noise_floor(x: np.ndarray, sr: float) -> float:
    """The RMS of ``x``'s quietest 20 ms frame that is not digital silence, capped FLOOR_CAP_DB under
    its peak (a note that never falls quiet has no floor to match); 0 when there is none."""
    a = np.asarray(x, dtype=np.float64)
    frame = max(1, int(round(FLOOR_FRAME_S * sr)))
    n = a.size // frame
    if n < 1:
        return 0.0
    rms = np.sqrt(np.mean(a[: n * frame].reshape(n, frame) ** 2, axis=1))
    quiet = rms[rms > 0]
    if not quiet.size:
        return 0.0
    return float(min(quiet.min(), np.abs(a).max() * 10.0 ** (FLOOR_CAP_DB / 20.0)))


def gate_candidates(gate: float, window: float) -> list[float]:
    """The key-up times the note-length scan tries around ``gate`` (seconds): x0.5, x0.7, x1.4, x2 and held
    through; times at or past the search ``window`` all sound held there, so they count once (HELD), and
    none is shorter than GATE_MIN. ``gate`` itself is left out."""
    out: list[float] = []
    for g in [gate * f for f in GATE_FACTORS] + [HELD]:
        g = HELD if g >= window else max(GATE_MIN, round(g, 3))
        cur = HELD if gate >= window else gate
        if g != cur and g not in out:
            out.append(g)
    return out


def gate_words(gate: float, window: float) -> str:
    """A key-up time in words, for the frames that try it."""
    return "note held to the end" if gate >= window else f"note held {gate:.1f} s"


def plain_patches() -> list[tuple[np.ndarray, str, tuple[str, str]]]:
    """The plain starts to score: ``[(k, words, (mix, filter))]``, every extra at 0 (see EXTRAS)."""
    from .twin import K_NAMES

    at = {n: i for i, n in enumerate(K_NAMES)}
    out = []
    for mix, levels in PLAIN_MIXES:
        for filt, cutoff in PLAIN_FILTERS:
            for env in PLAIN_ENVELOPES:
                for volume in PLAIN_VOLUMES:
                    k = np.zeros(len(K_NAMES))
                    knobs = {**PLAIN_REST, **volume, **levels, "cutoff": cutoff, "env_to_cutoff": env}
                    for name, v in knobs.items():
                        k[at[name]] = v
                    out.append((k, f"{mix}, filter {filt}", (mix, filt)))
    return out


def pick_plain(scored: list[tuple[float, np.ndarray, str, tuple[str, str]]],
               n: int) -> list[tuple[np.ndarray, str]]:
    """The ``n`` best plain starts, best first, one per oscillator mix and filter (then the next best,
    if fewer kinds score than starts are wanted). ``scored``: ``[(loss, k, words, kind)]``."""
    order = sorted((row for row in scored if math.isfinite(row[0])), key=lambda row: row[0])
    picked: list[tuple[np.ndarray, str]] = []
    kinds: set[tuple[str, str]] = set()
    for loss, k, words, kind in order:
        if len(picked) < n and kind not in kinds:
            kinds.add(kind)
            picked.append((k, words))
    for loss, k, words, kind in order:
        if len(picked) >= n:
            break
        if not any(k is p for p, _ in picked):
            picked.append((k, words))
    return picked


def extras_mask() -> np.ndarray:
    """1 at the extras' places in k (EXTRAS), else 0: the prior is ``lam * k @ mask``."""
    from .twin import K_NAMES

    return np.array([1.0 if n in EXTRAS else 0.0 for n in K_NAMES])


def _rng(seed: int | None, stream: int) -> np.random.Generator:
    """The random draws for a start: seed None or 0 gives the round-4 draws exactly; other seeds differ."""
    return np.random.default_rng(stream if not seed else [int(seed), int(stream)])


def lfo_hypotheses(k: np.ndarray, s: dict[str, int],
                   rates: int = 9) -> list[tuple[np.ndarray, dict[str, int], str]]:
    """The LFO settings the switch stage scans at knobs ``k``: each wave the matcher tries, at
    ``rates`` rates across the range, on the pitch (a vibrato) or on the filter, with Mod wheel to
    LFO at half. Returns ``[(knobs, switches, words)]``; the words say what a trial tries."""
    from .twin import K_NAMES

    i_rate, i_pitch, i_cut, i_depth = (K_NAMES.index(n)
                                       for n in ("lfo_rate", "lfo_to_pitch", "lfo_to_cutoff", "lfo_depth"))
    label, waves = SWITCH_WORDS["lfo_shape"]
    out = []
    for wave in LFO_WAVES:
        s2 = {**s, "lfo_shape": wave}
        for r in np.linspace(0.1, 0.9, rates):
            for where, amount in (("pitch", 0.1), ("filter", 0.4)):
                k2 = np.array(k, dtype=np.float64)
                k2[i_rate], k2[i_depth] = r, 0.5
                k2[i_pitch] = amount if where == "pitch" else 0.0
                k2[i_cut] = amount if where == "filter" else 0.0
                out.append((k2, s2, f"{label}: {waves[wave]}, rate {int(round(r * 127))}, on the {where}"))
    return out


# ── the stream ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Step:
    """One streamed step: the public frame, plus the candidate as the twin's own
    ``k`` (continuous, [0,1], ``K_PARAMS`` order) and ``s`` (discrete choices)."""

    frame: dict[str, Any]
    k: np.ndarray
    s: dict[str, int]


def _adam(
    grad_fn: Callable[[np.ndarray], np.ndarray],
    render: Callable[[np.ndarray], np.ndarray],
    loss_of: Callable[[np.ndarray], float],
    x0: np.ndarray,
    iters: int,
    lr: float,
    *,
    lr_min: float | None = None,
    patience: int | None = None,
    min_iters: int = 0,
    tol: float = 0.0,
    warmup: int = 0,
    penalty: Callable[[np.ndarray], float] | None = None,
    stop: Any = None,
) -> Iterator[tuple[np.ndarray, float, np.ndarray]]:
    """Adam over x in [0,1], yielding ``(x, loss, audio)`` after EACH step, where
    ``audio`` is the step's forward render (so a frame can draw it for free).

    The rate follows :func:`cosine_lr` over ``iters`` (constant ``lr`` when ``lr_min`` is None),
    ramped up linearly over the first ``warmup`` steps (none by default). ``penalty(x)`` is added to
    each step's loss (round 6: the prior on the extras; ``grad_fn`` must include it too).
    The descent ends early once :func:`plateaued` says it stopped improving, or when ``stop`` (a
    ``threading.Event``: "finish now") is set, checked before each step."""
    x = np.clip(np.asarray(x0, dtype=np.float64), 0.0, 1.0)
    m = np.zeros_like(x)
    v = np.zeros_like(x)
    b1, b2, eps = 0.9, 0.999, 1e-8
    bests: list[float] = []
    best = math.inf
    for t in range(1, iters + 1):
        if stop is not None and stop.is_set():
            return
        gr = np.asarray(grad_fn(x), dtype=np.float64)
        gr = np.where(np.isfinite(gr), gr, 0.0)
        m = b1 * m + (1 - b1) * gr
        v = b2 * v + (1 - b2) * gr * gr
        mhat = m / (1 - b1 ** t)
        vhat = v / (1 - b2 ** t)
        rate = cosine_lr(t, iters, lr, lr_min) * (min(1.0, t / warmup) if warmup else 1.0)
        x = np.clip(x - rate * mhat / (np.sqrt(vhat) + eps), 0.0, 1.0)
        audio = np.asarray(render(x), dtype=np.float64)
        loss = float(loss_of(audio)) + (float(penalty(x)) if penalty is not None else 0.0)
        yield x.copy(), loss, audio
        best = min(best, loss)
        bests.append(best)
        if plateaued(bests, patience, tol, min_iters):
            return


def steps(
    target_audio: np.ndarray,
    notes: list[int],
    *,
    seeded: bool,
    quality: str = DEFAULT_QUALITY,
    cold_candidates: list[int] | None = None,
    init_k: np.ndarray | None = None,
    init_s: dict[str, int] | None = None,
    budget: dict[str, Any] | None = None,
    stop: Any = None,
    seed: int | None = None,
    gate_s: float | None = None,
) -> Iterator[Step]:
    """Run one polyphonic match and yield a :class:`Step` per optimization step.

    ``target_audio`` is mono at ``WORKING_SR``. ``notes`` is the note SET (1..4).
    ``seeded`` True means the user fixed the notes, so the note-search is skipped.
    ``quality`` picks the budget (``budget`` overrides it — tests use a tiny one).
    ``cold_candidates`` is the salience-ranked list for the cold-start note-search.
    ``init_k``/``init_s`` warm-start the FIRST restart (else a near-centre start).
    ``stop`` (a ``threading.Event``) set means "finish now": the search ends at the next step
    and the best so far is still rendered, scored and yielded as the done frame.
    ``seed`` varies the random starts only (None or 0: the round-4 draws); plain starts do not vary.
    ``gate_s``: the note's key-up time in seconds (None: 1.2 s, the twin's own; past the window: held).
    A round-6 budget scans it too (``gate_scan``), and the done frame then says what it used (``held``)."""
    import autograd.numpy as anp
    from autograd import grad

    from .capture import AudioClip, prepare
    from .twin import SEARCH_BAND, Twin, _closeness_vs, band_limit, midi_to_hz, spectral_loss

    b = resolve_budget(quality, budget)
    gd_iters, restarts, neighbor_iters = int(b["gd_iters"]), int(b["restarts"]), int(b["neighbor_iters"])
    patience, min_iters, tol, lr_min = b["patience"], int(b["min_iters"]), float(b["tol"]), b["lr_min"]
    switch_top, switch_iters = int(b["switch_top"]), int(b["switch_iters"])
    continue_iters, polish_iters = int(b["continue_iters"]), int(b["polish_iters"])
    lfo_top, plain_n, lam = int(b["lfo_top"]), int(b["plain_starts"]), float(b["prior"])
    mask = extras_mask()
    # Round 4 (a budget with its keys): plateau stops, cosine rates, switch re-descent, a polish.
    new_style = bool(patience or lr_min is not None or (switch_top and switch_iters) or polish_iters)
    gate_rounds = int(b["gate_scan"]) if new_style else 0
    floor_on = bool(b["floor_match"]) and new_style

    def finishing() -> bool:
        return stop is not None and stop.is_set()

    def rule(cap: int, floor: float | None = None, warm: bool = False) -> dict[str, Any]:
        """How a descent capped at ``cap`` runs: it always hears "finish"; a round-4 budget adds the
        cosine rate (down to ``floor``, else ``lr_min``; ``warm``: ramped up first, for a descent
        that starts from good knobs) and the plateau rule (the main descent's own patience, scaled
        down for the short switch trials and the polish)."""
        how: dict[str, Any] = {"stop": stop, "penalty": prior_of if lam else None}
        if new_style:
            how["lr_min"] = lr_min if floor is None else floor
            how["warmup"] = WARMUP if warm else 0
        if patience:
            how.update(patience=max(6, min(int(patience), cap // 3)),
                       min_iters=min(min_iters, max(8, cap // 3)), tol=tol)
        return how

    t_start = time.perf_counter()

    def twins(gate: float) -> tuple[Any, Any]:
        """The full twin and the search twin with the key up at ``gate`` seconds. The search render's
        note-off sits at the same absolute time, so the shorter render keeps the target's envelope
        shape (mirrors TwinMatcher); it renders the SAME model more cheaply: its caps come from the
        full rate (model_sr), and the loss compares only the band both renders hold (band_limit)."""
        f = Twin(gate_fraction=gate / ANALYSIS_SECONDS)
        return f, Twin(sr=SEARCH_SR, seconds=SEARCH_SECONDS,
                       gate_fraction=min(1.0, gate / SEARCH_SECONDS), model_sr=f.sr)

    gate = GATE_DEFAULT if gate_s is None else max(GATE_MIN, float(gate_s))
    full, search = twins(gate)
    fmax = SEARCH_BAND * search.sr

    target_full = np.asarray(target_audio, dtype=np.float64)
    tgt_lo = AudioClip(target_full.astype(np.float32), full.sr).resample(search.sr).samples.astype(np.float64)
    n = max(1, int(round(search.seconds * search.sr)))
    tgt_lo = np.concatenate([tgt_lo, np.zeros(n - tgt_lo.shape[0])]) if tgt_lo.shape[0] < n else tgt_lo[:n]
    tgt_lo = np.asarray(band_limit(tgt_lo, search.sr, fmax), dtype=np.float64)
    floor = noise_floor(tgt_lo, search.sr) if floor_on else 0.0
    hiss = floor * np.random.default_rng(7).standard_normal(n) if floor > 0 else None

    s0 = {**S_DEFAULT, **(init_s or {})}
    notes0 = sorted(int(x) for x in notes)
    n_configs = len(search.s_configs())
    per_start = gd_iters
    if new_style:
        tried = (n_configs - 1 if switch_top < 0 else min(switch_top, n_configs - 1)) + max(0, lfo_top)
        per_start += (tried * switch_iters + continue_iters) if switch_iters else 0
    # ``total``: every gd step the budget allows (the old search runs exactly this many; a round-4
    # search usually stops sooner, as its descents stop improving).
    total_gd = max(1, per_start * restarts + (polish_iters if new_style else 0))
    t0 = loudest_time(tgt_lo, search.sr)

    def f0_of(note_set: list[int]) -> float:
        return midi_to_hz(min(note_set))

    def renderer(note_set: list[int], s: dict[str, int]) -> Callable[[np.ndarray], np.ndarray]:
        return lambda k: search.render_chord(k, s, note_set)

    def loss_of(audio: np.ndarray) -> float:
        if hiss is not None:
            audio = audio + hiss
        return float(spectral_loss(band_limit(audio, search.sr, fmax), tgt_lo, search.sr))

    def prior_of(k: np.ndarray) -> float:
        """The mild prior on the extras (round 6), in the search loss only."""
        return float(lam * np.dot(np.asarray(k, dtype=np.float64), mask)) if lam else 0.0

    def score(k: np.ndarray, s: dict[str, int], note_set: list[int]) -> float:
        """The search loss of patch ``(k, s)`` on ``note_set``: renders only, no gradient."""
        return loss_of(np.asarray(search.render_chord(k, s, note_set), dtype=np.float64)) + prior_of(k)

    def grad_of(note_set: list[int], s: dict[str, int]) -> Callable[[np.ndarray], np.ndarray]:
        def spectral(k: Any) -> Any:
            audio = search.render_chord(k, s, note_set)
            if hiss is not None:
                audio = audio + hiss
            return spectral_loss(band_limit(audio, search.sr, fmax), tgt_lo, search.sr)
        if lam:
            return grad(lambda k: spectral(k) + lam * anp.dot(k, mask))
        return grad(spectral)

    def frame(phase: str, note_set: list[int], x: np.ndarray, s: dict[str, int],
              audio: np.ndarray, **extra: Any) -> dict[str, Any]:
        return {
            "phase": phase, **note_fields(note_set), **extra,
            "cc": {str(c): int(v) for c, v in full.k_to_cc(x, s).items()},
            "wave": wave_snippet(audio, search.sr, f0_of(note_set), t0),
        }

    n_steps = 0
    best_notes, best_s = list(notes0), dict(s0)

    # ── the starts: a warm start (the synth's knobs) first when given; then (round 6) the best
    # plain patches, scored at the target with renders only; random starts fill any rest ──
    plain: list[tuple[np.ndarray, str]] = []
    gate_given = gate
    n_plain = min(plain_n, restarts - (1 if init_k is not None else 0)) if new_style else 0
    scored_plain = []
    if n_plain > 0 or (gate_rounds and new_style):
        scored_plain = [(score(k, s0, notes0), k, words, kind) for k, words, kind in plain_patches()]
        n_steps += len(scored_plain)
    if gate_rounds:
        # the note's length, before any descent: on the best few starts (and the warm start)
        pool = [k for _l, k, _w, _kind in sorted(scored_plain, key=lambda row: row[0])[:GATE_PRE_STARTS]]
        if init_k is not None:
            pool.append(np.clip(np.asarray(init_k, dtype=np.float64), 0.0, 1.0))
        here = min((score(k, s0, notes0) for k in pool), default=math.inf)
        g_best = gate
        for _round in range(gate_rounds):
            tried = []
            for g in gate_candidates(g_best, SEARCH_SECONDS):
                srch = twins(g)[1]
                tried.append((min(loss_of(np.asarray(srch.render_chord(k, s0, notes0), dtype=np.float64))
                                  + prior_of(k) for k in pool), g))
            n_steps += len(tried) * len(pool)
            if not tried or not min(tried)[0] < here:
                break
            here, g_best = min(tried)
        if g_best != gate:
            gate = g_best
            full, search = twins(gate)
            scored_plain = [(score(k, s0, notes0), k, words, kind) for _l, k, words, kind in scored_plain]
            n_steps += len(scored_plain)
    if n_plain > 0:
        plain = pick_plain(scored_plain, n_plain)
        if gate != gate_given:                     # the first start says the length the scan found
            plain[0] = (plain[0][0], f"{plain[0][1]}, {gate_words(gate, SEARCH_SECONDS)}")

    def start_of(r: int) -> tuple[np.ndarray, str | None]:
        """Start ``r``: its knobs, and the words its descent's frames carry (None for none)."""
        if init_k is not None and r == 0:
            return np.clip(np.asarray(init_k, dtype=np.float64), 0.0, 1.0), None
        j = r - (1 if init_k is not None else 0)
        if j < len(plain):
            return plain[j][0].copy(), plain[j][1]
        if r == 0:
            return np.clip(0.5 + 0.05 * _rng(seed, 0).standard_normal(full.k_dim), 0.0, 1.0), None
        x = _rng(seed, 1000 + r).uniform(0.15, 0.85, full.k_dim)
        if plain_n > 0 and new_style:                   # round 6: a random start keeps its extras low
            x = np.where(mask > 0, _rng(seed, 2000 + r).uniform(0.0, 0.08, full.k_dim), x)
            return x, "a random start"
        return x, None

    # ── phase 1: pitch (seeded set, or the cold-start detection) ─────────────
    x0_first = start_of(0)[0]
    audio0 = np.asarray(search.render_chord(x0_first, s0, notes0), dtype=np.float64)
    best_k, best_loss = x0_first.copy(), loss_of(audio0) + prior_of(x0_first)
    n_steps += 1
    yield Step(frame(
        "pitch", notes0, x0_first, s0, audio0, seeded=seeded,
        iter=0, total=total_gd, restart=0, loss=best_loss, best_loss=best_loss,
        target_wave=wave_snippet(tgt_lo, search.sr, f0_of(notes0), t0),
    ), x0_first.copy(), dict(s0))

    # ── phase 2: gradient descent on the shared patch, multi-restart ─────────
    # Each start descends under the best switches so far (the defaults at first; the old search
    # keeps them throughout), then (round 4) tries the other switch settings at its best knobs.
    counter = 0
    extra_new = {"starts": restarts} if new_style else {}

    def descend(note_set: list[int], s: dict[str, int], x_start: np.ndarray, cap: int, lr: float,
                rule: dict[str, Any], state: dict[str, Any], **fields: Any) -> Iterator[Step]:
        """One descent from ``x_start`` under ``s``, one gd frame a step; ``state`` tracks this
        descent's best (``k``, ``loss``) and the run's best (the enclosing variables)."""
        nonlocal counter, n_steps, best_loss, best_k, best_s
        for x, loss, audio in _adam(grad_of(note_set, s), renderer(note_set, s), loss_of,
                                    x_start, cap, lr, **rule):
            counter += 1
            n_steps += 1
            if loss < state["loss"]:
                state["loss"], state["k"] = loss, x.copy()
            if loss < best_loss:
                best_loss, best_k, best_s = loss, x.copy(), dict(s)
            yield Step(frame(
                "gd", note_set, x, s, audio,
                iter=counter, total=total_gd, loss=loss, best_loss=best_loss, **fields,
            ), x.copy(), dict(s))

    def scan_gate(s: dict[str, int], mine: dict[str, Any]) -> Iterator[Step]:
        """The note-length scan (see GATE_DEFAULT): score other key-up times at this start's knobs,
        renders only, stepping on while one wins; keep the winner (both twins rebuilt, the run's best
        re-scored under it) and re-descend briefly under it."""
        nonlocal gate, full, search, best_loss, best_k, best_s, best_notes, n_steps
        here, g_best = mine["loss"], gate
        for _round in range(gate_rounds):
            tried = []
            for g in gate_candidates(g_best, SEARCH_SECONDS):
                srch = twins(g)[1]
                tried.append((loss_of(np.asarray(srch.render_chord(mine["k"], s, notes0), dtype=np.float64))
                              + prior_of(mine["k"]), g))
            n_steps += len(tried)
            if not tried or not min(tried)[0] < here:
                break
            here, g_best = min(tried)
        if g_best == gate:
            return
        gate = g_best
        full, search = twins(gate)
        # every loss so far was the old key-up's: the run's best, re-scored under the new one
        best_loss = score(best_k, best_s, best_notes)
        if here < best_loss:
            best_k, best_s, best_notes, best_loss = mine["k"].copy(), dict(s), list(notes0), here
        mine["loss"] = here
        after = {"k": mine["k"].copy(), "loss": here}
        cap = max(switch_iters, 24)
        yield from descend(notes0, s, mine["k"], cap, LR_SWITCH, rule(cap, warm=True), after, restart=0,
                           trying=gate_words(gate, SEARCH_SECONDS), **extra_new)
        mine.update(after)

    for r in range(restarts):
        if finishing():
            break
        # Restart 0 starts at x0_first; later restarts sample the cube widely so the
        # multi-restart actually escapes the first basin (keep best overall).
        x_start, words0 = (x0_first, start_of(0)[1]) if r == 0 else start_of(r)
        s_r = dict(best_s) if new_style else dict(s0)
        mine = {"k": x_start.copy(), "loss": math.inf}
        yield from descend(notes0, s_r, x_start, gd_iters, LR, rule(gd_iters), mine, restart=r,
                           **({"trying": words0} if words0 else {}), **extra_new)
        if r == 0 and gate_rounds and not finishing() and math.isfinite(mine["loss"]):
            yield from scan_gate(s_r, mine)
        trials = new_style and bool(switch_top and switch_iters)
        if not trials or finishing() or not math.isfinite(mine["loss"]):
            continue
        # ── the switches: score every setting at this start's knobs (renders only), re-descend
        # the most promising briefly, adopt a winner, and keep descending under it ──
        scored = [(cfg, score(mine["k"], cfg, notes0)) for cfg in search.s_configs()]
        n_steps += len(scored)
        tries = [(cfg, mine["k"], switch_words(cfg, s_r), LR_SWITCH)
                 for cfg in switch_candidates(scored, s_r, mine["loss"], switch_top)]
        if lfo_top > 0:
            # the LFO scan: its best settings (one each on the pitch and the filter, for Deep)
            hyps = sorted(((score(k2, s2, notes0), i, k2, s2, w)
                           for i, (k2, s2, w) in enumerate(lfo_hypotheses(mine["k"], s_r))),
                          key=lambda h: (h[0], h[1]))
            n_steps += len(hyps)
            places: set[str] = set()
            for loss, _i, k2, s2, words in hyps:
                place = words.rsplit(" ", 1)[-1]
                if math.isfinite(loss) and place not in places and len(places) < lfo_top:
                    places.add(place)
                    tries.append((s2, k2, words, LR_LFO))
        won: dict[str, Any] | None = None
        for cfg, k_try, words, lr_try in tries:
            if finishing():
                break
            trial = {"k": k_try.copy(), "loss": math.inf}
            yield from descend(notes0, cfg, k_try, switch_iters, lr_try, rule(switch_iters, warm=True),
                               trial, restart=r, trying=words, **extra_new)
            if trial["loss"] < mine["loss"] and (won is None or trial["loss"] < won["loss"]):
                won = {"s": dict(cfg), "k": trial["k"], "loss": trial["loss"]}
        if won is not None and continue_iters and not finishing():
            kept = {"k": won["k"].copy(), "loss": won["loss"]}
            yield from descend(notes0, won["s"], won["k"], continue_iters, LR_CONTINUE,
                               rule(continue_iters, warm=True), kept, restart=r, **extra_new)

    # ── phase 3a: note-search over the SET (cold start only) ─────────────────
    if not seeded and not finishing():
        ranked = list(cold_candidates or notes0)
        weakest = next((nb for nb in reversed(ranked) if nb in best_notes),
                       best_notes[-1] if best_notes else None)
        next_cand = next((nb for nb in ranked if nb not in best_notes), None)
        cand_sets: list[list[int]] = []
        for shift in (-12, 12):                               # transpose the whole set
            shifted = [nb + shift for nb in best_notes]
            if all(0 <= nb <= 127 for nb in shifted):
                cand_sets.append(sorted(shifted))
        if len(best_notes) > 1 and weakest is not None:       # drop the weakest (N-1)
            cand_sets.append(sorted(nb for nb in best_notes if nb != weakest))
        if next_cand is not None and len(best_notes) < MAX_NOTES:   # add one (N+1)
            cand_sets.append(sorted(best_notes + [next_cand]))
        seen = {tuple(best_notes)}
        cand_sets = [c for c in cand_sets if c and tuple(c) not in seen and not seen.add(tuple(c))]

        total_ns = max(1, len(cand_sets) * neighbor_iters)
        ns_counter = 0
        for cand in cand_sets:
            start_k = best_k.copy()
            for x, loss, audio in _adam(grad_of(cand, best_s), renderer(cand, best_s), loss_of,
                                        start_k, neighbor_iters, LR, stop=stop,
                                        penalty=prior_of if lam else None):
                ns_counter += 1
                n_steps += 1
                improved = loss < best_loss
                if improved:
                    best_loss, best_k, best_notes = loss, x.copy(), list(cand)
                yield Step(frame(
                    "note-search", cand, x, best_s, audio,
                    iter=ns_counter, total=total_ns, loss=loss, best_loss=best_loss, improved=improved,
                ), x.copy(), dict(best_s))

    # ── phase 3b: enumerate the discrete switches cheaply (no gradient, no frames) ──
    if not finishing():
        for s_cfg in search.s_configs():
            loss = score(best_k, s_cfg, best_notes)
            n_steps += 1
            if loss < best_loss:
                best_loss, best_s = loss, dict(s_cfg)

    # ── phase 3c (round 4): a final polish, small steps from the overall best ────
    if new_style and polish_iters and not finishing():
        polish = {"k": best_k.copy(), "loss": best_loss}
        yield from descend(best_notes, dict(best_s), best_k.copy(), polish_iters, LR_POLISH,
                           rule(polish_iters, LR_POLISH / 10, warm=True),
                           polish, restart=max(0, restarts - 1), trying=POLISH_WORDS, **extra_new)

    # ── done: render the best at FULL resolution, score, A/B WAVs ────────────
    audio = np.asarray(full.render_chord(best_k, best_s, best_notes), dtype=np.float64)
    closeness = float(_closeness_vs(audio.astype(np.float32), target_full, full.sr))
    target_clip = prepare(AudioClip(target_full.astype(np.float32), full.sr))
    f0 = f0_of(best_notes)
    done = {
        "phase": "done", **note_fields(best_notes), "seeded": seeded,
        "iter": counter if new_style else total_gd, "total": counter if new_style else total_gd,
        "restart": restarts,
        "loss": best_loss, "best_loss": best_loss,
        "cc": {str(c): int(v) for c, v in full.k_to_cc(best_k, best_s).items()},
        "wave": wave_snippet(audio, full.sr, f0, t0),
        "target_wave": wave_snippet(target_full, full.sr, f0, t0),
        "closeness": round(closeness, 2),
        "seconds": round(time.perf_counter() - t_start, 2),
        "steps": n_steps,
        "match_wav_b64": wav_b64(audio, full.sr),
        "target_wav_b64": wav_b64(target_clip.samples, target_clip.samplerate),
    }
    if gate_rounds or gate_s is not None:
        done["held"] = round(min(gate, full.seconds), 2)   # the key-up the match used (its A/B too)
    if finishing():
        done["finished"] = True                  # finished early, on request: the best so far
    yield Step(done, best_k.copy(), dict(best_s))


def run(p: Plan, budget: dict[str, Any] | None = None, stop: Any = None,
        seed: int | None = None) -> Iterator[Step]:
    """:func:`steps` for a parsed :class:`Plan` (``stop``, ``seed``: see :func:`steps`)."""
    return steps(p.samples, p.notes, seeded=p.seeded, quality=p.quality,
                 cold_candidates=p.cold_candidates, init_k=p.init_k, init_s=p.init_s,
                 budget=budget, stop=stop, seed=seed, gate_s=p.gate_s)


async def astream(p: Plan, throttle: float = 0.0,
                  budget: dict[str, Any] | None = None, stop: Any = None) -> AsyncIterator[Step]:
    """Yield :func:`run`'s steps on the event loop while each step computes on a
    worker thread (a gradient step blocks for tens of ms; the cockpit's other sockets
    must stay live). ``throttle`` seconds are slept between frames (not after done).
    Setting ``stop`` finishes the run early; its done frame still comes."""
    it = run(p, budget, stop)
    try:
        while True:
            step = await asyncio.to_thread(next, it, None)
            if step is None:
                return
            yield step
            if throttle and step.frame["phase"] != "done":
                await asyncio.sleep(throttle)
    finally:
        # A cancelled await can leave the generator mid-step on its thread; it is
        # then simply dropped (closing a running generator raises ValueError).
        with contextlib.suppress(ValueError):
            it.close()


# ── recorded matches (the static page replays these; BUILD.md §2.4) ─────────
def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "match"


def _engine_tag() -> str:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).resolve().parent,
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        sha = "unknown"
    return f"twin {sha or 'unknown'}"


def record_match(
    audio_path: str | Path, *, title: str, notes: str | None = None,
    quality: str = "quick", target_url: str | None = None,
    budget: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Run one match on a file and return it in the recorded-match format:
    ``{"title", "target_url", "notes", "frames", "recorded", "engine"}``."""
    p = plan(Path(audio_path).read_bytes(), notes, quality)
    frames = [step.frame for step in run(p, budget)]
    return {
        "title": title,
        "target_url": target_url,
        "notes": frames[-1]["notes"] if frames else p.notes,
        "frames": frames,
        "recorded": datetime.now(timezone.utc).date().isoformat(),
        "engine": _engine_tag(),
    }


def write_recording(rec: dict[str, Any], out_dir: str | Path, slug: str | None = None) -> Path:
    """Write ``<out_dir>/<slug>.json`` and add/replace its row in ``<out_dir>/index.json``
    (a JSON list of ``{"slug", "title", "notes", "recorded", "closeness"}``)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    slug = slugify(slug or rec["title"])
    path = out / f"{slug}.json"
    path.write_text(json.dumps(rec, separators=(",", ":")) + "\n")
    index_path = out / "index.json"
    try:
        index = json.loads(index_path.read_text())
        if not isinstance(index, list):
            index = []
    except (OSError, ValueError):
        index = []
    done = rec["frames"][-1] if rec.get("frames") else {}
    row = {"slug": slug, "title": rec["title"], "notes": rec.get("notes", []),
           "recorded": rec.get("recorded"), "closeness": done.get("closeness")}
    index = [r for r in index if isinstance(r, dict) and r.get("slug") != slug] + [row]
    index_path.write_text(json.dumps(index, indent=1) + "\n")
    return path


def main(argv: list[str] | None = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m synth.match.twin_session",
        description="Record a twin match for the static page (matches/<slug>.json + index.json).",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", help="match one audio file and write the recorded run")
    rec.add_argument("audio", help="the target sound (WAV, AIFF, FLAC)")
    rec.add_argument("--title", required=True, help="the name shown in the list of recorded runs")
    rec.add_argument("--notes", default="", help="seeded MIDI notes, e.g. 57 or 60,64,67 (default: detect)")
    rec.add_argument("--quality", default="quick", choices=sorted(QUALITY_PRESETS))
    rec.add_argument("--target-url", default=None, help="where the page can fetch the target audio")
    rec.add_argument("--out", required=True, help="the matches/ directory to write into")
    args = ap.parse_args(argv)

    data = record_match(args.audio, title=args.title, notes=args.notes,
                        quality=args.quality, target_url=args.target_url)
    path = write_recording(data, args.out)
    done = data["frames"][-1]
    print(f"{path}  ({len(data['frames'])} frames, closeness {done.get('closeness')}%, "
          f"{done.get('seconds')} s)")


if __name__ == "__main__":
    main()
