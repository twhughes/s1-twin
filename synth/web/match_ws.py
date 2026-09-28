"""Routes owned by W-match (see docs/design/BUILD.md §1). Mounted by server.py.

``GET /ws/match`` — the twin matcher on the cockpit server (BUILD.md §2.4).

The client sends ONE binary message (the audio file's bytes); the server streams one
JSON frame per optimization step, then a ``done`` frame, and closes. The frames are
:mod:`synth.match.twin_session`'s frames exactly: the phases ``pitch → gd →
note-search → done``, and the candidate in **CC space on every frame**
(``"cc": {"<cc>": value}``) so any knob in the UI can animate to it. The done frame
adds ``closeness``, ``seconds``, ``steps`` and the A/B WAVs.

Query parameters (all optional):

* ``throttle`` — seconds between frames (default 0.02, clamped to 0..0.4);
* ``notes`` — seeded MIDI notes, e.g. ``60,64,67`` (absent → cold-start detection);
* ``quality`` — ``quick``, ``thorough`` or ``deep`` (default ``thorough``);
* ``init`` — a JSON CC map, e.g. ``{"74": 90, "22": 1}``: the synth's current knobs,
  used as the first start of the descent.

While the match runs, the client may send the text message ``"finish"``: the search ends at
the next step and the done frame still comes, with the best patch so far rendered and scored
(``"finished": true``). Closing the socket ends the search too, with no done frame.

A bad request gets ``{"phase": "error", "detail": <what happened and what to do>}``
and a close. Localhost only: the same host + origin guard as the cockpit's HTTP
routes (a browser WebSocket skips the HTTP middleware, so it is checked here).
Each optimization step runs on a worker thread, so the cockpit's other sockets stay
live during a match.

``POST /api/match/record-note`` (W-rec, docs/design/ROUND2.md §3) — the S-1 plays one
note of its current sound, captured by the cockpit's running audio monitor, and the
answer is that take as ``audio/wav``: the target for "Match the synth's current sound"
when the S-1 is the sound source. It is :meth:`SynthDriver.probe` with no parameters
(the S-1's patch is never touched: never ``calibrate()``, which overwrites it), so the
take is onset-trimmed, 2 s long and peak-normalized at the twin's rate (``prepare()``).
It answers ``409`` with a plain ``detail`` when the S-1 port is closed, the monitor is
not running, the sequencer is playing, another match is using the S-1, a note is
already being recorded, or the take is silent.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading

import numpy as np
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel, Field

import synth.engine as engine_module

from ..match import twin_session as session

router = APIRouter()

TOO_LARGE = "That audio file is too large (25 MB max). Trim it to a few seconds of the sound."
MISSING_EXTRAS = (
    "The matcher needs the twin extras on this computer. "
    'Install them with: pip install -e ".[studio,twin]", then restart the app.'
)
MAX_UPLOAD_BYTES = session.MAX_UPLOAD_BYTES


def matcher_available() -> bool:
    """True when the twin matcher's numerics are installed (autograd + scipy)."""
    try:
        import autograd  # noqa: F401
        import scipy  # noqa: F401
    except ImportError:
        return False
    return True


def _guard_ok(websocket: WebSocket) -> bool:
    """The cockpit's localhost guard, read live (``server.run`` widens the sets)."""
    from . import server

    host = websocket.headers.get("host", "")
    origin = websocket.headers.get("origin")
    if host not in server._ALLOWED_HOSTS:
        return False
    return origin is None or origin in server._ALLOWED_ORIGINS


async def _fail(websocket: WebSocket, detail: str) -> None:
    with contextlib.suppress(Exception):
        await websocket.send_json({"phase": "error", "detail": detail})
    with contextlib.suppress(Exception):
        await websocket.close()


@router.websocket("/ws/match")
async def ws_match(websocket: WebSocket) -> None:
    """Stream one twin match (see the module docstring for the contract)."""
    if not _guard_ok(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    q = websocket.query_params
    throttle = session.clamp_throttle(q.get("throttle"))

    try:
        raw = await websocket.receive_bytes()
    except (WebSocketDisconnect, KeyError, RuntimeError):
        with contextlib.suppress(Exception):
            await websocket.close()
        return

    if not matcher_available():
        await _fail(websocket, MISSING_EXTRAS)
        return
    if len(raw) > MAX_UPLOAD_BYTES:
        await _fail(websocket, TOO_LARGE)
        return

    init_k, init_s = None, {}
    init_raw = q.get("init")
    if init_raw:
        try:
            init_map = json.loads(init_raw)
        except ValueError:
            init_map = None
        if not isinstance(init_map, dict):
            await _fail(websocket, 'The starting knobs ("init") must be a JSON map of CC to value.')
            return
        init_k, init_s = session.cc_init(init_map)

    try:
        plan = await asyncio.to_thread(session.plan, raw, q.get("notes"), q.get("quality"))
    except session.UploadError as exc:
        await _fail(websocket, str(exc))
        return
    plan.init_k, plan.init_s = init_k, init_s

    # "finish" from the client (or a closed socket) ends the search at the next step.
    stop = threading.Event()

    async def listen() -> None:
        while not stop.is_set():
            try:
                msg = await websocket.receive()
            except Exception:  # noqa: BLE001 - the socket is gone
                stop.set()
                return
            if msg.get("type") == "websocket.disconnect":
                stop.set()
                return
            if msg.get("text") == "finish":
                stop.set()

    reader = asyncio.create_task(listen())
    stream = session.astream(plan, throttle, stop=stop)
    try:
        async for step in stream:
            await websocket.send_json(step.frame)
    except (WebSocketDisconnect, RuntimeError):
        return                                   # the client left; the finally below ends the run
    finally:
        stop.set()
        reader.cancel()
        await stream.aclose()
    with contextlib.suppress(Exception):
        await websocket.close()


# ── one note of the S-1's current sound (W-rec) ──────────────────────────────
NOT_CONNECTED = "The S-1 is not connected. Plug it in with its USB cable, then try again."
NO_AUDIO = ("No sound from the S-1 is reaching this app: its audio monitor is off. Plug the S-1 in "
            "with its USB cable (the cable carries its audio too), then try again.")
NO_AUDIO_LOGIC = ("In Logic mode, Logic Pro has the S-1's audio, so this app cannot record it. "
                  "Switch to Solo in Settings, then try again.")
SEQUENCER_PLAYING = "The sequencer is playing. Stop it, then try again."
MATCH_RUNNING = "Another match is using the S-1. Stop it, then try again."
NOTE_BUSY = "The S-1 is already playing a test note. Wait for it to finish, then try again."
SILENT_TAKE = ("The S-1 made no sound for the test note. Check its volume and that the patch "
               "makes a sound, then try again.")
SILENCE_PEAK = 1e-3            # -60 dBFS: the raw take's peak below this is silence

_note_lock = threading.Lock()  # one test note at a time: probes share the monitor's capture


class RecordNoteReq(BaseModel):
    note: int = Field(48, ge=0, le=127, description="MIDI note (default C3, the S-1's probe note)")
    velocity: int = Field(100, ge=1, le=127)
    hold: float = Field(1.2, gt=0, le=4.0, description="seconds the key is held")
    tail: float = Field(1.0, ge=0, le=4.0, description="seconds recorded after the key is let go")


class _AtVelocity:
    """The engine's MIDI backend, playing note-ons at one velocity (the driver uses the
    backend's default); every other call passes through."""

    def __init__(self, midi, velocity: int) -> None:
        self._midi = midi
        self._velocity = velocity

    def send_note_on(self, note: int, velocity: int | None = None) -> bool:
        return self._midi.send_note_on(note, self._velocity)

    def __getattr__(self, name: str):
        return getattr(self._midi, name)


class _Tap:
    """The running monitor, remembering the raw take's peak: ``probe()`` normalizes the
    take, which would turn a silent input's noise floor into a full-scale target."""

    def __init__(self, monitor) -> None:
        self._monitor = monitor
        self.peak = 0.0

    @property
    def running(self) -> bool:
        return bool(self._monitor.running)

    def begin_capture(self) -> None:
        self._monitor.begin_capture()

    def end_capture(self):
        clip = self._monitor.end_capture()
        samples = np.asarray(clip.samples)
        self.peak = float(np.abs(samples).max()) if samples.size else 0.0
        return clip


def _busy_reason(engine) -> str | None:
    """Why the S-1 cannot play a test note right now, in plain words (None: it can)."""
    from . import server

    if not engine.midi.connected:
        return NOT_CONNECTED
    if not engine.monitor.running:
        return NO_AUDIO_LOGIC if getattr(engine, "mode", "solo") == "logic" else NO_AUDIO
    if engine.sequencer.playing:
        return SEQUENCER_PLAYING
    if server.match_state().running:
        return MATCH_RUNNING
    return None


@router.post("/api/match/record-note", tags=["match"], response_class=Response,
             summary="Record one note of the S-1's current sound (audio/wav)")
def record_note(req: RecordNoteReq) -> Response:
    """Play ``note`` on the S-1 (held ``hold`` s, then ``tail`` s of release) with its
    current patch untouched, capture it from the running audio monitor, and return the
    take as a 16-bit mono WAV at the twin's rate, trimmed to the onset. ``409`` + a plain
    ``detail`` when the S-1 cannot do it now (see the module docstring)."""
    from ..match.driver import SynthDriver
    from .serialize import wav_bytes

    engine = engine_module.ENGINE
    reason = _busy_reason(engine)
    if reason:
        raise HTTPException(409, reason)
    if not _note_lock.acquire(blocking=False):
        raise HTTPException(409, NOTE_BUSY)
    try:
        tap = _Tap(engine.monitor)
        driver = SynthDriver(_AtVelocity(engine.midi, req.velocity), device=None, note=req.note,
                             hold_s=req.hold, tail_s=req.tail, monitor=tap)
        clip = driver.probe(None)           # None: play only, never apply or calibrate
    finally:
        _note_lock.release()
    if tap.peak < SILENCE_PEAK:
        raise HTTPException(409, SILENT_TAKE)
    return Response(content=wav_bytes(clip), media_type="audio/wav",
                    headers={"Cache-Control": "no-store"})
