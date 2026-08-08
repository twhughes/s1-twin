"""Streaming match server for the standalone soft synth (``soft/index.html``).

Drop an audio file on the page and this server matches it **on this synth** (the
one you play in the browser) by gradient-descending a *differentiable model of
this synth* to the target — and streams every optimization step so you can WATCH
it train: the knobs animate to the current guess and a loss curve falls. Targets
may be **chords of up to 4 notes**: the same patch is rendered on every note and
summed (``Twin.render_chord``), and the shared patch is fit to the whole chord.

Under the hood the differentiable model is the autograd DSP in
``synth/match/twin.py`` (``Twin.render`` / ``render_chord`` + ``spectral_loss``).
The match engine reuses it purely as the differentiable forward model; the page
never surfaces that name — in the UI it is "the differentiable model of this
synth" / the match engine.

Two ways to fix the note SET (the critical, infrequent step):

* **SEED** (recommended) — the user selects up to 4 keys on the on-screen
  keyboard; the client sends those notes, the server uses them EXACTLY and skips
  detection and the note-search-over-set entirely. The notes are known, so the
  run is just a thorough multi-restart patch search — sidestepping the flaky part.
* **COLD START** (fallback) — no seeded notes: ``analyze.detect_notes`` recovers
  the set by iterative harmonic salience, then a bounded note-search refines it.

The algorithm's phases:

1. **pitch** — the seeded set, or ``detect_notes`` for a cold start. The frame
   carries ``"seeded": bool``.
2. **gd** — Adam gradient-descent on the continuous params against the CHORD, with
   **multiple random restarts** (keep best). Quality favors accuracy over speed —
   a match may take minutes; the ``quick``/``thorough`` control picks the budget.
3. **note-search** (cold start only) — transpose the whole set ±12, drop the
   weakest voice (N−1), add the next-strongest candidate (N+1), re-optimize
   briefly at each, keep whichever lowers loss; then enumerate discrete ``s``.

The optimization is **silent + hardware-free by default**: each candidate is
rendered to a numpy array only to compute the loss — nothing plays. The page's
optional MONITOR toggle is a *client-side* concern (it replays the browser's own
Web-Audio synth at the detected notes on some frames); this server never opens an
audio device and never streams per-step audio. Only the final target-vs-match A/B
WAVs are rendered once, as base64, in the ``done`` frame. Match time scales ~N×
with the voice count.

WebSocket contract — ``GET /ws/match``. Query params (all optional):
``?throttle=<seconds>`` paces frames (MONITOR-on uses a larger value so candidates
are audible); ``?notes=60,64,67`` seeds the note set (absent → cold-start
detection); ``?quality=quick|thorough`` picks the search budget (default
thorough):

    client → one **binary** message: the raw audio-file bytes.
    server → one JSON frame PER step. Stable schema:

        {"phase": "pitch"|"gd"|"note-search"|"done",
         "notes": [int], "chord_name": str,          # the note SET + its label
         "note": int, "note_name": str,              # lowest/root, for back-compat
         "seeded": bool,                             # pitch frame: seeded vs cold
         "iter": int, "total": int, "restart": int,  # restart index on gd frames
         "loss": float, "best_loss": float,
         "params": {page-knob-key -> value in the page's P units}}

      note-search frames add "improved": bool. The final frame is:

        {"phase": "done", ...same keys...,
         "cc": {cc_number: value}, "closeness": float, "seconds": float,
         "steps": int, "match_wav_b64": str, "target_wav_b64": str}

      On a bad upload the server sends {"phase": "error", "detail": str} and closes.

Localhost only (127.0.0.1) with a host guard on both the HTTP routes and the
WebSocket: this process reads uploaded files and runs compute, so no cross-origin
/ DNS-rebinding caller may reach it. Port from ``SOFT_PORT`` (default **8767**).
"""

from __future__ import annotations

import base64
import contextlib
import io
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

import numpy as np
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

HOST = "127.0.0.1"
PORT = int(os.environ.get("SOFT_PORT", "8767"))  # PORTS.md (claim 8767 later); SOFT_PORT overrides
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# Frame pacing (seconds slept between streamed frames). Small so the default run
# stays fast; the page passes a larger value when MONITOR is on so each candidate
# is audible in the browser. Clamped in the handler. Tests set it to 0.
DEFAULT_THROTTLE = float(os.environ.get("SOFT_MATCH_THROTTLE", "0.02"))
MAX_THROTTLE = 0.4

# Optimizer budget. Getting the patch right beats getting it fast — a match may
# run for minutes (silent + hardware-free, so that is fine). The page picks a
# preset via ?quality=; THOROUGH (default) spends generous iterations across
# several random restarts (keep best); QUICK is the watchable/CI budget. Note
# timing also scales ~N× with the chord's voice count.
QUALITY_PRESETS: dict[str, dict[str, int]] = {
    "quick":    {"gd_iters": 45,  "restarts": 1, "neighbor_iters": 14},
    "thorough": {"gd_iters": 160, "restarts": 4, "neighbor_iters": 24},
}
DEFAULT_QUALITY = "thorough"
LR = 0.08              # Adam learning rate

MAX_NOTES = 4          # a chord target is at most 4 notes

_HERE = Path(__file__).resolve().parent
_INDEX = _HERE / "index.html"

_NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

_ALLOWED_HOSTS = {f"{HOST}:{PORT}", f"localhost:{PORT}", HOST, "localhost"}
_ALLOWED_ORIGINS = {f"http://{h}" for h in _ALLOWED_HOSTS}

app = FastAPI(title="s1 soft — match", docs_url=None, redoc_url=None)


@app.middleware("http")
async def _origin_guard(request: Request, call_next):
    """Reject cross-origin / DNS-rebinding HTTP requests (this server reads files
    and runs compute, so any page the user visits must not reach it)."""
    host = request.headers.get("host", "")
    origin = request.headers.get("origin")
    if host not in _ALLOWED_HOSTS:
        return JSONResponse({"detail": "forbidden host"}, status_code=403)
    if origin is not None and origin not in _ALLOWED_ORIGINS:
        return JSONResponse({"detail": "forbidden origin"}, status_code=403)
    return await call_next(request)


@app.get("/")
def index() -> FileResponse:
    """Serve the playable soft synth (same origin, so the WebSocket connects)."""
    return FileResponse(str(_INDEX), media_type="text/html")


def _note_name(note: int) -> str:
    return f"{_NOTE_NAMES[note % 12]}{note // 12 - 1}"


def _chord_name(notes: list[int]) -> str:
    """Label for a note SET: a single note keeps its name; a chord joins the note
    names low→high with ``+`` (e.g. ``"G4+B4+D5"``)."""
    ordered = sorted(notes)
    if len(ordered) == 1:
        return _note_name(ordered[0])
    return "+".join(_note_name(n) for n in ordered)


def _note_fields(notes: list[int]) -> dict[str, Any]:
    """The note-SET keys every frame carries: the full set + label, plus the
    lowest note as ``note``/``note_name`` for back-compat with single-note frames."""
    ordered = sorted(int(n) for n in notes)
    return {
        "notes": ordered,
        "chord_name": _chord_name(ordered),
        "note": ordered[0],
        "note_name": _note_name(ordered[0]),
    }


def _wav_b64(samples: Any, samplerate: int) -> str:
    """16-bit PCM WAV of ``samples`` at ``samplerate``, base64-encoded."""
    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, np.asarray(samples), int(samplerate), format="WAV", subtype="PCM_16")
    return base64.b64encode(buf.getvalue()).decode("ascii")


# ── differentiable-model glue ────────────────────────────────────────────────
# The k-vector -> page-P mapping. The page's live synth exposes ONE LFO with a
# target selector and a single sub oscillator, so several model params collapse or
# drop (documented in the page's honest note): the LFO->pitch vs LFO->cutoff amounts
# pick a target, and sub_octave / amp_env_mode / fine_tune have no knob.
def _k_to_page(k: Any, s: dict[str, int]) -> dict[str, Any]:
    """Model k-vector (+ discrete s) -> the page's P knob values, so a streamed
    frame carries params the page can drop straight onto its knobs."""
    from synth.match.twin import K_PARAMS

    kv = {kp.name: float(k[i]) for i, kp in enumerate(K_PARAMS)}
    lfo_wave = {2: "tri", 3: "square", 0: "saw", 1: "saw"}.get(s.get("lfo_shape", 2), "tri")
    return {
        "saw": kv["saw_lvl"], "pulse": kv["square_lvl"], "sub": kv["sub_lvl"],
        "noise": kv["noise_lvl"],
        "pw": min(0.98, max(0.02, 0.05 + 0.45 * kv["pulse_width"])),  # model duty 0.05..0.5
        "cutoff": kv["cutoff"], "res": kv["resonance"],
        "envAmt": kv["env_to_cutoff"], "keytrack": kv["key_follow"],
        "atk": kv["attack"], "dec": kv["decay"], "sus": kv["sustain"], "rel": kv["release"],
        "lfoRate": kv["lfo_rate"], "lfoDepth": kv["lfo_depth"],
        "lfoTarget": "pitch" if kv["lfo_to_pitch"] > kv["lfo_to_cutoff"] else "filter",
        "lfoWave": lfo_wave,
    }


def _page_to_k(params: dict[str, Any]) -> np.ndarray:
    """Inverse of :func:`_k_to_page`: the page's P knob values -> a model k-vector,
    so the client's CURRENT knobs can WARM-START the gradient descent ("hand-dial a
    starting patch, then optimize from there"). Unknown params default to 0.5. The
    page's normalized knobs (saw/cutoff/…) map straight through; ``pw`` inverts the
    duty curve; ``lfoTarget`` picks which continuous LFO amount carries the LFO."""
    from synth.match.twin import K_PARAMS

    idx = {kp.name: i for i, kp in enumerate(K_PARAMS)}
    k = np.full(len(K_PARAMS), 0.5)
    ident = {
        "saw": "saw_lvl", "pulse": "square_lvl", "sub": "sub_lvl", "noise": "noise_lvl",
        "cutoff": "cutoff", "res": "resonance", "envAmt": "env_to_cutoff",
        "keytrack": "key_follow", "atk": "attack", "dec": "decay", "sus": "sustain",
        "rel": "release", "lfoRate": "lfo_rate", "lfoDepth": "lfo_depth",
    }
    for pkey, kname in ident.items():
        if pkey in params:
            try:
                k[idx[kname]] = min(1.0, max(0.0, float(params[pkey])))
            except (TypeError, ValueError):
                pass
    if "pw" in params:
        try:  # page pw = 0.05 + 0.45 * k_pulse_width
            k[idx["pulse_width"]] = min(1.0, max(0.0, (float(params["pw"]) - 0.05) / 0.45))
        except (TypeError, ValueError):
            pass
    target = params.get("lfoTarget")
    if target == "pitch":
        k[idx["lfo_to_pitch"]], k[idx["lfo_to_cutoff"]] = 0.5, 0.0
    elif target == "filter":
        k[idx["lfo_to_pitch"]], k[idx["lfo_to_cutoff"]] = 0.0, 0.5
    return k


def _adam_yield(
    objective: Any, x0: np.ndarray, iters: int, lr: float,
) -> Iterator[tuple[np.ndarray, float]]:
    """Adam over x in [0,1], yielding (x, loss) after EACH step (so a caller can
    stream it). Mirrors ``twin._adam_descend`` but surfaces every step."""
    from autograd import grad

    g = grad(objective)
    x = np.clip(np.asarray(x0, dtype=np.float64), 0.0, 1.0)
    m = np.zeros_like(x)
    v = np.zeros_like(x)
    b1, b2, eps = 0.9, 0.999, 1e-8
    for t in range(1, iters + 1):
        gr = g(x)
        gr = np.where(np.isfinite(gr), gr, 0.0)
        m = b1 * m + (1 - b1) * gr
        v = b2 * v + (1 - b2) * gr * gr
        mhat = m / (1 - b1 ** t)
        vhat = v / (1 - b2 ** t)
        x = np.clip(x - lr * mhat / (np.sqrt(vhat) + eps), 0.0, 1.0)
        yield x.copy(), float(objective(x))


def match_frames(
    target_audio: np.ndarray,
    notes0: list[int],
    seeded: bool,
    quality: str = DEFAULT_QUALITY,
    cold_candidates: list[int] | None = None,
    init_k: np.ndarray | None = None,
) -> Iterator[dict]:
    """Run the polyphonic match and yield one frame dict per step (see module
    docstring for the schema). Pure numpy/autograd — renders candidates to arrays
    for the loss only; no audio device is ever touched.

    ``notes0`` is the note SET (1..4 notes). ``seeded`` True means the user fixed
    those notes on the keyboard, so the note-search-over-set is skipped. ``quality``
    picks the iteration/restart budget. ``cold_candidates`` is the salience-ranked
    note list (strongest first) used by the cold-start note-search to pick the
    weakest voice + the next candidate; ignored when ``seeded``. ``init_k`` warm-
    starts the FIRST restart from the user's current knobs (else a random start)."""
    from synth.match.capture import AudioClip, prepare
    from synth.match.twin import Twin, _closeness_vs, spectral_loss

    budget = QUALITY_PRESETS.get(quality, QUALITY_PRESETS[DEFAULT_QUALITY])
    gd_iters, restarts, neighbor_iters = (
        budget["gd_iters"], budget["restarts"], budget["neighbor_iters"])

    t_start = time.perf_counter()
    full = Twin()
    # A cheaper search model carries the many gradient evals; align its note-off to
    # the same absolute time as the full model's so a shorter render keeps the
    # target's envelope shape (mirrors TwinMatcher).
    note_off_s = full.seconds * full.gate_fraction
    search_seconds = 1.5
    search = Twin(sr=16000, seconds=search_seconds,
                  gate_fraction=min(1.0, note_off_s / search_seconds))

    tgt_full = AudioClip(np.asarray(target_audio, dtype=np.float32), full.sr)
    tgt_lo = tgt_full.resample(search.sr).samples.astype(np.float64)
    n = max(1, int(round(search.seconds * search.sr)))
    if tgt_lo.shape[0] < n:
        tgt_lo = np.concatenate([tgt_lo, np.zeros(n - tgt_lo.shape[0])])
    else:
        tgt_lo = tgt_lo[:n]

    def make_obj(notes: list[int], s: dict[str, int]):
        def obj(k: np.ndarray) -> float:
            return spectral_loss(search.render_chord(k, s, notes), tgt_lo, search.sr)
        return obj

    s_default = {"sub_octave": 2, "lfo_shape": 2, "amp_env_mode": 1}
    notes0 = sorted(int(x) for x in notes0)
    total_gd = max(1, gd_iters * restarts)

    steps = 0
    best_notes = list(notes0)
    best_s = dict(s_default)

    # ── phase 1: pitch (seeded set, or the cold-start detection) ─────────────
    # Warm-start restart 0 from the user's current knobs when supplied; else the
    # usual near-center random init. Later restarts always sample randomly.
    if init_k is not None:
        x0_first = np.clip(np.asarray(init_k, dtype=np.float64), 0.0, 1.0)
    else:
        x0_first = np.clip(0.5 + 0.05 * np.random.default_rng(0).standard_normal(full.k_dim), 0.0, 1.0)
    best_k = x0_first.copy()
    best_loss = float(make_obj(notes0, s_default)(x0_first))
    steps += 1
    yield {
        "phase": "pitch", **_note_fields(notes0), "seeded": seeded,
        "iter": 0, "total": total_gd, "restart": 0,
        "loss": best_loss, "best_loss": best_loss,
        "params": _k_to_page(x0_first, s_default),
    }

    # ── phase 2: gradient descent on the shared patch, multi-restart ─────────
    obj0 = make_obj(notes0, s_default)
    counter = 0
    for r in range(restarts):
        # First restart starts centered; later restarts sample the cube widely so
        # the multi-restart actually escapes the first basin (keep best overall).
        rng_r = np.random.default_rng(1000 + r)
        x0 = x0_first if r == 0 else np.clip(rng_r.uniform(0.15, 0.85, full.k_dim), 0.0, 1.0)
        for _t, (x, loss) in enumerate(_adam_yield(obj0, x0, gd_iters, LR), start=1):
            counter += 1
            steps += 1
            if loss < best_loss:
                best_loss, best_k, best_s = loss, x.copy(), dict(s_default)
            yield {
                "phase": "gd", **_note_fields(notes0),
                "iter": counter, "total": total_gd, "restart": r,
                "loss": loss, "best_loss": best_loss,
                "params": _k_to_page(x, s_default),
            }

    # ── phase 3a: note-search over the SET (cold start only) ─────────────────
    # Seeded runs skip this: the notes are known, so only the patch is unknown.
    if not seeded:
        ranked = list(cold_candidates or notes0)
        # weakest voice = lowest-salience note that is in the current set
        weakest = next((nb for nb in reversed(ranked) if nb in best_notes),
                       best_notes[-1] if best_notes else None)
        # next candidate = strongest ranked note NOT already in the set
        next_cand = next((nb for nb in ranked if nb not in best_notes), None)

        cand_sets: list[list[int]] = []
        for shift in (-12, 12):                       # transpose the whole set
            shifted = [nb + shift for nb in best_notes]
            if all(0 <= nb <= 127 for nb in shifted):
                cand_sets.append(sorted(shifted))
        if len(best_notes) > 1 and weakest is not None:   # drop the weakest (N-1)
            cand_sets.append(sorted(nb for nb in best_notes if nb != weakest))
        if next_cand is not None and len(best_notes) < MAX_NOTES:  # add one (N+1)
            cand_sets.append(sorted(best_notes + [next_cand]))
        # de-dup, drop the current set / empties
        seen = {tuple(best_notes)}
        cand_sets = [c for c in cand_sets if c and tuple(c) not in seen and not seen.add(tuple(c))]

        total_ns = max(1, len(cand_sets) * neighbor_iters)
        ns_counter = 0
        for cand in cand_sets:
            obj_c = make_obj(cand, best_s)
            for _t, (x, loss) in enumerate(_adam_yield(obj_c, best_k, neighbor_iters, LR), start=1):
                ns_counter += 1
                steps += 1
                improved = loss < best_loss
                if improved:
                    best_loss, best_k, best_notes = loss, x.copy(), list(cand)
                yield {
                    "phase": "note-search", **_note_fields(cand),
                    "iter": ns_counter, "total": total_ns, "loss": loss,
                    "best_loss": best_loss, "improved": improved,
                    "params": _k_to_page(x, best_s),
                }

    # ── phase 3b: enumerate discrete s configs cheaply (no gradient) ─────────
    for s_cfg in search.s_configs():
        loss = float(spectral_loss(search.render_chord(best_k, s_cfg, best_notes), tgt_lo, search.sr))
        steps += 1
        if loss < best_loss:
            best_loss, best_s = loss, dict(s_cfg)

    # ── done: render the best at FULL resolution, score, A/B WAVs ────────────
    audio = np.asarray(full.render_chord(best_k, best_s, best_notes), dtype=np.float32)
    closeness = float(_closeness_vs(audio, np.asarray(target_audio, np.float64), full.sr))
    cc = full.k_to_cc(best_k, best_s)
    target_clip = prepare(AudioClip(np.asarray(target_audio, np.float32), full.sr))
    seconds = time.perf_counter() - t_start
    yield {
        "phase": "done", **_note_fields(best_notes), "seeded": seeded,
        "iter": total_gd, "total": total_gd, "restart": restarts,
        "loss": best_loss, "best_loss": best_loss,
        "params": _k_to_page(best_k, best_s),
        "cc": {str(num): int(val) for num, val in cc.items()},
        "closeness": round(closeness, 2), "seconds": round(seconds, 2), "steps": steps,
        "match_wav_b64": _wav_b64(audio, full.sr),
        "target_wav_b64": _wav_b64(target_clip.samples, target_clip.samplerate),
    }


def _decode_upload(raw: bytes) -> np.ndarray:
    """Decode uploaded audio bytes to a mono float64 array at the model's SR.
    Raises ValueError on empty / silent / undecodable input."""
    from synth.match.capture import load_audio

    if not raw:
        raise ValueError("empty upload")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name
    try:
        clip = load_audio(tmp_path)
    except Exception as exc:  # noqa: BLE001 — any decode failure is a client error
        raise ValueError(f"could not read audio: {exc}") from exc
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
    samples = np.asarray(clip.samples, dtype=np.float64)
    if samples.size == 0 or float(np.abs(samples).max()) < 1e-6:
        raise ValueError("audio is empty or silent")
    return samples


@app.websocket("/ws/match")
async def ws_match(websocket: WebSocket) -> None:
    """Stream a match. Client sends one binary message (the audio bytes); the
    server pushes one JSON frame per optimization step, then a ``done`` frame."""
    import asyncio

    host = websocket.headers.get("host", "")
    if host not in _ALLOWED_HOSTS:
        await websocket.close(code=1008)
        return
    origin = websocket.headers.get("origin")
    if origin is not None and origin not in _ALLOWED_ORIGINS:
        await websocket.close(code=1008)
        return

    await websocket.accept()

    try:
        throttle = float(websocket.query_params.get("throttle", DEFAULT_THROTTLE))
    except (TypeError, ValueError):
        throttle = DEFAULT_THROTTLE
    throttle = min(MAX_THROTTLE, max(0.0, throttle))

    try:
        raw = await websocket.receive_bytes()
    except (WebSocketDisconnect, KeyError, RuntimeError):
        with contextlib.suppress(Exception):
            await websocket.close()
        return

    if len(raw) > MAX_UPLOAD_BYTES:
        await websocket.send_json({"phase": "error", "detail": "audio file too large (25 MB max)"})
        await websocket.close()
        return

    from synth.match.analyze import _harmonic_salience_notes, detect_notes
    from synth.match.capture import AudioClip

    try:
        samples = _decode_upload(raw)
    except ValueError as exc:
        await websocket.send_json({"phase": "error", "detail": str(exc)})
        await websocket.close()
        return

    quality = websocket.query_params.get("quality", DEFAULT_QUALITY)
    if quality not in QUALITY_PRESETS:
        quality = DEFAULT_QUALITY

    # Seeded notes (?notes=60,64,67) fix the SET exactly — the recommended path:
    # skip detection and note-search. Absent → cold-start harmonic-salience detect.
    seeded_raw = websocket.query_params.get("notes", "")
    seeded_notes: list[int] = []
    for tok in seeded_raw.split(","):
        tok = tok.strip()
        if tok:
            try:
                v = int(tok)
            except ValueError:
                continue
            if 0 <= v <= 127:
                seeded_notes.append(v)
    seeded_notes = sorted(set(seeded_notes))[:MAX_NOTES]

    clip = AudioClip(samples.astype(np.float32), 22050)
    if seeded_notes:
        notes0, seeded, cold_candidates = seeded_notes, True, None
    else:
        notes0 = detect_notes(clip, max_notes=MAX_NOTES)
        seeded = False
        # salience-ranked (strongest-first) list, one past the set, so the
        # note-search can pick the weakest voice + the next candidate.
        cold_candidates = [nb for nb, _sal in
                           _harmonic_salience_notes(clip, max_notes=MAX_NOTES + 1)]

    # Optional warm start: the client's current knob values (?init=<json>).
    import json
    init_k = None
    init_raw = websocket.query_params.get("init")
    if init_raw:
        try:
            init_params = json.loads(init_raw)
            if isinstance(init_params, dict):
                init_k = _page_to_k(init_params)
        except (ValueError, TypeError):
            init_k = None

    try:
        for frame in match_frames(samples, notes0, seeded, quality, cold_candidates, init_k):
            await websocket.send_json(frame)
            if throttle and frame["phase"] != "done":
                await asyncio.sleep(throttle)
    except WebSocketDisconnect:
        return
    with contextlib.suppress(Exception):
        await websocket.close()


def run(host: str | None = None, port: int | None = None) -> None:
    import uvicorn

    host = host or HOST
    port = port or PORT
    _ALLOWED_HOSTS.update({f"{host}:{port}", host})
    _ALLOWED_ORIGINS.update({f"http://{host}:{port}", f"http://{host}"})
    print(f"s1 soft (match) → http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


def main() -> None:
    run()


if __name__ == "__main__":
    main()
