"""Streaming match server for the standalone soft synth (``soft/index.html``).

Drop an audio file on the page and this server matches it **on this synth** (the
one you play in the browser) by gradient-descending a *differentiable model of
this synth* to the target — and streams every optimization step so you can WATCH
it train: the knobs animate to the current guess and a loss curve falls. Targets
may be **chords of up to 4 notes**: the same patch is rendered on every note and
summed (``Twin.render_chord``), and the shared patch is fit to the whole chord.

The session itself — phases, budgets, seeding, restarts, note-search, the A/B WAVs —
lives in :mod:`synth.match.twin_session`, shared with the cockpit's ``/ws/match``.
The session speaks the S-1's CC space; this module converts each step to the page's
own knob units (``params``), so the page and its frames are unchanged. The page never
surfaces the model's name — in the UI it is "the differentiable model of this synth".

Two ways to fix the note SET (the critical, infrequent step):

* **SEED** (recommended) — the user selects up to 4 keys on the on-screen
  keyboard; the client sends those notes, the server uses them EXACTLY and skips
  detection and the note-search-over-set entirely.
* **COLD START** (fallback) — no seeded notes: ``analyze.detect_notes`` recovers
  the set by iterative harmonic salience, then a bounded note-search refines it.

The phases: **pitch** (the seeded set or the detection; ``"seeded": bool``) →
**gd** (Adam on the shared patch, multi-restart, keep best) → **note-search** (cold
start only: transpose ±12, drop the weakest voice, add the next candidate) → the
discrete switches are enumerated → **done**.

The optimization is **silent + hardware-free**: each candidate is rendered to a
numpy array only to compute the loss — nothing plays. The page's optional MONITOR
toggle is a *client-side* concern. Only the final target-vs-match A/B WAVs are
rendered once, as base64, in the ``done`` frame. Match time scales ~N× with the
voice count.

WebSocket contract — ``GET /ws/match``. Query params (all optional):
``?throttle=<seconds>`` paces frames (MONITOR-on uses a larger value so candidates
are audible); ``?notes=60,64,67`` seeds the note set (absent → cold-start
detection); ``?quality=quick|thorough`` picks the search budget (default
thorough); ``?init=<json of page knob values>`` warm-starts the first restart:

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
/ DNS-rebinding caller may reach it. Port from ``SOFT_PORT`` (default **8816**).
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any, Iterator

import numpy as np
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from synth.match import twin_session as session
from synth.match.twin_session import (  # noqa: F401  (re-exported: this module's old names)
    DEFAULT_QUALITY,
    LR,
    MAX_NOTES,
    MAX_THROTTLE,
    QUALITY_PRESETS,
    UploadError,
)

HOST = "127.0.0.1"
PORT = int(os.environ.get("SOFT_PORT", "8816"))  # PORTS.md 8816 (synth soft matcher); SOFT_PORT overrides
MAX_UPLOAD_BYTES = session.MAX_UPLOAD_BYTES

# Frame pacing (seconds slept between streamed frames). Small so the default run
# stays fast; the page passes a larger value when MONITOR is on so each candidate
# is audible in the browser. Clamped in the handler. Tests set it to 0.
DEFAULT_THROTTLE = float(os.environ.get("SOFT_MATCH_THROTTLE", str(session.DEFAULT_THROTTLE)))

_HERE = Path(__file__).resolve().parent
_INDEX = _HERE / "index.html"

_ALLOWED_HOSTS = {f"{HOST}:{PORT}", f"localhost:{PORT}", HOST, "localhost"}
_ALLOWED_ORIGINS = {f"http://{h}" for h in _ALLOWED_HOSTS}

TOO_LARGE = "That audio file is too large (25 MB max). Trim it to a few seconds of the sound."

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


# ── the page's knob units ────────────────────────────────────────────────────
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


# Session-frame keys the page's schema does not carry ("cc" returns on the done frame).
_SESSION_ONLY = ("cc", "wave", "target_wave")


def page_frame(step: session.Step) -> dict[str, Any]:
    """A session step -> this page's frame: page-unit ``params`` on every frame, and
    the CC map on the done frame only (the documented soft schema)."""
    frame = {k: v for k, v in step.frame.items() if k not in _SESSION_ONLY}
    frame["params"] = _k_to_page(step.k, step.s)
    if step.frame["phase"] == "done":
        frame["cc"] = step.frame["cc"]
    return frame


def match_frames(
    target_audio: np.ndarray,
    notes0: list[int],
    seeded: bool,
    quality: str = DEFAULT_QUALITY,
    cold_candidates: list[int] | None = None,
    init_k: np.ndarray | None = None,
) -> Iterator[dict]:
    """Run the polyphonic match and yield one page frame per step (see the module
    docstring). Kept for callers of the old API; the work is :func:`session.steps`."""
    for step in session.steps(target_audio, notes0, seeded=seeded, quality=quality,
                              cold_candidates=cold_candidates, init_k=init_k):
        yield page_frame(step)


def _decode_upload(raw: bytes) -> np.ndarray:
    """Old name for :func:`session.decode_upload` (raises ValueError on bad input)."""
    return session.decode_upload(raw)


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
    throttle = session.clamp_throttle(websocket.query_params.get("throttle"), DEFAULT_THROTTLE)

    try:
        raw = await websocket.receive_bytes()
    except (WebSocketDisconnect, KeyError, RuntimeError):
        with contextlib.suppress(Exception):
            await websocket.close()
        return

    if len(raw) > MAX_UPLOAD_BYTES:
        await websocket.send_json({"phase": "error", "detail": TOO_LARGE})
        await websocket.close()
        return

    try:
        plan = await asyncio.to_thread(
            session.plan, raw, websocket.query_params.get("notes"), websocket.query_params.get("quality"))
    except UploadError as exc:
        await websocket.send_json({"phase": "error", "detail": str(exc)})
        await websocket.close()
        return

    # Optional warm start: the client's current knob values (?init=<json>).
    init_raw = websocket.query_params.get("init")
    if init_raw:
        try:
            init_params = json.loads(init_raw)
            if isinstance(init_params, dict):
                plan.init_k = _page_to_k(init_params)
        except (ValueError, TypeError):
            plan.init_k = None

    stream = session.astream(plan, throttle)
    try:
        async for step in stream:
            await websocket.send_json(page_frame(step))
    except (WebSocketDisconnect, RuntimeError):
        return
    finally:
        await stream.aclose()
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
