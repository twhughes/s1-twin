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
* ``quality`` — ``quick`` or ``thorough`` (default ``thorough``);
* ``init`` — a JSON CC map, e.g. ``{"74": 90, "22": 1}``: the synth's current knobs,
  used as the first start of the descent.

A bad request gets ``{"phase": "error", "detail": <what happened and what to do>}``
and a close. Localhost only: the same host + origin guard as the cockpit's HTTP
routes (a browser WebSocket skips the HTTP middleware, so it is checked here).
Each optimization step runs on a worker thread, so the cockpit's other sockets stay
live during a match.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

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

    stream = session.astream(plan, throttle)
    try:
        async for step in stream:
            await websocket.send_json(step.frame)
    except (WebSocketDisconnect, RuntimeError):
        return                                   # the client left; the finally below ends the run
    finally:
        await stream.aclose()
    with contextlib.suppress(Exception):
        await websocket.close()
