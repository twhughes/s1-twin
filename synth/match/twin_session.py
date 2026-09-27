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
   (keep the best). ``restart`` says which start the frame belongs to.
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

from . import WORKING_SR

# Optimizer budget. Getting the patch right beats getting it fast: THOROUGH (the
# default) spends generous iterations across several random restarts (keep best);
# QUICK is the watchable budget. Time also scales ~N× with the chord's voice count.
QUALITY_PRESETS: dict[str, dict[str, int]] = {
    "quick":    {"gd_iters": 45,  "restarts": 1, "neighbor_iters": 14},
    "thorough": {"gd_iters": 160, "restarts": 4, "neighbor_iters": 24},
}
DEFAULT_QUALITY = "thorough"
LR = 0.08                      # Adam learning rate
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


def plan(raw: bytes, notes: str | None = None, quality: str | None = None) -> Plan:
    """Decode an upload and fix the note set: seeded notes are used exactly (the
    recommended path); none → cold-start detection. Raises :class:`UploadError`."""
    samples = decode_upload(raw)
    seeded_notes = parse_notes(notes)
    if seeded_notes:
        return Plan(samples, seeded_notes, True, parse_quality(quality))
    found, ranked = detect(samples)
    return Plan(samples, found, False, parse_quality(quality), cold_candidates=ranked)


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
) -> Iterator[tuple[np.ndarray, float, np.ndarray]]:
    """Adam over x in [0,1], yielding ``(x, loss, audio)`` after EACH step, where
    ``audio`` is the step's forward render (so a frame can draw it for free)."""
    x = np.clip(np.asarray(x0, dtype=np.float64), 0.0, 1.0)
    m = np.zeros_like(x)
    v = np.zeros_like(x)
    b1, b2, eps = 0.9, 0.999, 1e-8
    for t in range(1, iters + 1):
        gr = np.asarray(grad_fn(x), dtype=np.float64)
        gr = np.where(np.isfinite(gr), gr, 0.0)
        m = b1 * m + (1 - b1) * gr
        v = b2 * v + (1 - b2) * gr * gr
        mhat = m / (1 - b1 ** t)
        vhat = v / (1 - b2 ** t)
        x = np.clip(x - lr * mhat / (np.sqrt(vhat) + eps), 0.0, 1.0)
        audio = np.asarray(render(x), dtype=np.float64)
        yield x.copy(), float(loss_of(audio)), audio


def steps(
    target_audio: np.ndarray,
    notes: list[int],
    *,
    seeded: bool,
    quality: str = DEFAULT_QUALITY,
    cold_candidates: list[int] | None = None,
    init_k: np.ndarray | None = None,
    init_s: dict[str, int] | None = None,
    budget: dict[str, int] | None = None,
) -> Iterator[Step]:
    """Run one polyphonic match and yield a :class:`Step` per optimization step.

    ``target_audio`` is mono at ``WORKING_SR``. ``notes`` is the note SET (1..4).
    ``seeded`` True means the user fixed the notes, so the note-search is skipped.
    ``quality`` picks the budget (``budget`` overrides it — tests use a tiny one).
    ``cold_candidates`` is the salience-ranked list for the cold-start note-search.
    ``init_k``/``init_s`` warm-start the FIRST restart (else a near-centre start)."""
    from autograd import grad

    from .capture import AudioClip, prepare
    from .twin import Twin, _closeness_vs, midi_to_hz, spectral_loss

    b = dict(budget or QUALITY_PRESETS.get(quality, QUALITY_PRESETS[DEFAULT_QUALITY]))
    gd_iters, restarts, neighbor_iters = int(b["gd_iters"]), int(b["restarts"]), int(b["neighbor_iters"])

    t_start = time.perf_counter()
    full = Twin()
    # Align the search render's note-off to the full render's absolute time, so the
    # shorter render keeps the target's envelope shape (mirrors TwinMatcher).
    note_off_s = full.seconds * full.gate_fraction
    search = Twin(sr=SEARCH_SR, seconds=SEARCH_SECONDS,
                  gate_fraction=min(1.0, note_off_s / SEARCH_SECONDS))

    target_full = np.asarray(target_audio, dtype=np.float64)
    tgt_lo = AudioClip(target_full.astype(np.float32), full.sr).resample(search.sr).samples.astype(np.float64)
    n = max(1, int(round(search.seconds * search.sr)))
    tgt_lo = np.concatenate([tgt_lo, np.zeros(n - tgt_lo.shape[0])]) if tgt_lo.shape[0] < n else tgt_lo[:n]

    s0 = {**S_DEFAULT, **(init_s or {})}
    notes0 = sorted(int(x) for x in notes)
    total_gd = max(1, gd_iters * restarts)
    t0 = loudest_time(tgt_lo, search.sr)

    def f0_of(note_set: list[int]) -> float:
        return midi_to_hz(min(note_set))

    def renderer(note_set: list[int], s: dict[str, int]) -> Callable[[np.ndarray], np.ndarray]:
        return lambda k: search.render_chord(k, s, note_set)

    def loss_of(audio: np.ndarray) -> float:
        return float(spectral_loss(audio, tgt_lo, search.sr))

    def grad_of(note_set: list[int], s: dict[str, int]) -> Callable[[np.ndarray], np.ndarray]:
        return grad(lambda k: spectral_loss(search.render_chord(k, s, note_set), tgt_lo, search.sr))

    def frame(phase: str, note_set: list[int], x: np.ndarray, s: dict[str, int],
              audio: np.ndarray, **extra: Any) -> dict[str, Any]:
        return {
            "phase": phase, **note_fields(note_set), **extra,
            "cc": {str(c): int(v) for c, v in full.k_to_cc(x, s).items()},
            "wave": wave_snippet(audio, search.sr, f0_of(note_set), t0),
        }

    n_steps = 0
    best_notes, best_s = list(notes0), dict(s0)

    # ── phase 1: pitch (seeded set, or the cold-start detection) ─────────────
    if init_k is not None:
        x0_first = np.clip(np.asarray(init_k, dtype=np.float64), 0.0, 1.0)
    else:
        x0_first = np.clip(0.5 + 0.05 * np.random.default_rng(0).standard_normal(full.k_dim), 0.0, 1.0)
    audio0 = np.asarray(search.render_chord(x0_first, s0, notes0), dtype=np.float64)
    best_k, best_loss = x0_first.copy(), loss_of(audio0)
    n_steps += 1
    yield Step(frame(
        "pitch", notes0, x0_first, s0, audio0, seeded=seeded,
        iter=0, total=total_gd, restart=0, loss=best_loss, best_loss=best_loss,
        target_wave=wave_snippet(tgt_lo, search.sr, f0_of(notes0), t0),
    ), x0_first.copy(), dict(s0))

    # ── phase 2: gradient descent on the shared patch, multi-restart ─────────
    g0, r0 = grad_of(notes0, s0), renderer(notes0, s0)
    counter = 0
    for r in range(restarts):
        # Restart 0 starts at x0_first; later restarts sample the cube widely so the
        # multi-restart actually escapes the first basin (keep best overall).
        x_start = x0_first if r == 0 else np.random.default_rng(1000 + r).uniform(0.15, 0.85, full.k_dim)
        for x, loss, audio in _adam(g0, r0, loss_of, x_start, gd_iters, LR):
            counter += 1
            n_steps += 1
            if loss < best_loss:
                best_loss, best_k, best_s = loss, x.copy(), dict(s0)
            yield Step(frame(
                "gd", notes0, x, s0, audio,
                iter=counter, total=total_gd, restart=r, loss=loss, best_loss=best_loss,
            ), x.copy(), dict(s0))

    # ── phase 3a: note-search over the SET (cold start only) ─────────────────
    if not seeded:
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
                                        start_k, neighbor_iters, LR):
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
    for s_cfg in search.s_configs():
        loss = loss_of(np.asarray(search.render_chord(best_k, s_cfg, best_notes), dtype=np.float64))
        n_steps += 1
        if loss < best_loss:
            best_loss, best_s = loss, dict(s_cfg)

    # ── done: render the best at FULL resolution, score, A/B WAVs ────────────
    audio = np.asarray(full.render_chord(best_k, best_s, best_notes), dtype=np.float64)
    closeness = float(_closeness_vs(audio.astype(np.float32), target_full, full.sr))
    target_clip = prepare(AudioClip(target_full.astype(np.float32), full.sr))
    f0 = f0_of(best_notes)
    done = {
        "phase": "done", **note_fields(best_notes), "seeded": seeded,
        "iter": total_gd, "total": total_gd, "restart": restarts,
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
    yield Step(done, best_k.copy(), dict(best_s))


def run(p: Plan, budget: dict[str, int] | None = None) -> Iterator[Step]:
    """:func:`steps` for a parsed :class:`Plan`."""
    return steps(p.samples, p.notes, seeded=p.seeded, quality=p.quality,
                 cold_candidates=p.cold_candidates, init_k=p.init_k, init_s=p.init_s,
                 budget=budget)


async def astream(p: Plan, throttle: float = 0.0,
                  budget: dict[str, int] | None = None) -> AsyncIterator[Step]:
    """Yield :func:`run`'s steps on the event loop while each step computes on a
    worker thread (a gradient step blocks for tens of ms; the cockpit's other sockets
    must stay live). ``throttle`` seconds are slept between frames (not after done)."""
    it = run(p, budget)
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
