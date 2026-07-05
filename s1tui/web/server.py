"""FastAPI backend for the s1tui sound-design web app.

Exposes device/MIDI discovery, target upload, the match lifecycle (start/pause/
resume/stop) with live progress over a WebSocket, audio clip retrieval for
listening, and patch-bank management. Launch with ``s1tui-web``.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..match.distance import Weights
from ..match.session import MatchConfig
from ..midi_backend import MidiBackend
from ..patches import (
    delete_patch,
    list_patches,
    load_patch,
    load_patch_metadata,
    save_patch,
)
from .serialize import progress_payload, spectrogram_payload, wav_bytes
from .state import STATE, MatchAlreadyRunning

HOST, PORT = "127.0.0.1", 8765
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# Errors caused by the client's request/setup (bad device, busy input, bad
# name) map to 400; anything else is a genuine server bug and stays a 500.
try:
    import sounddevice as _sd

    CLIENT_ERRORS: tuple[type[Exception], ...] = (RuntimeError, ValueError, _sd.PortAudioError)
except Exception:  # pragma: no cover - sounddevice always present with [studio]
    CLIENT_ERRORS = (RuntimeError, ValueError)


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
    # Stop the match thread and audio streams before PortAudio tears down,
    # so the process doesn't abort (SIGABRT) on exit with streams still open.
    try:
        STATE.stop()
    except Exception:  # noqa: BLE001
        pass
    try:
        STATE.stop_monitor()
    except Exception:  # noqa: BLE001
        pass


app = FastAPI(title="s1tui studio", lifespan=_lifespan)

STATIC_DIR = Path(__file__).parent / "static"

_ALLOWED_HOSTS = {f"{HOST}:{PORT}", f"localhost:{PORT}", HOST, "localhost"}
_ALLOWED_ORIGINS = {f"http://{h}" for h in _ALLOWED_HOSTS}


@app.middleware("http")
async def _origin_guard(request: Request, call_next):
    """Reject cross-origin/DNS-rebinding requests.

    The server drives MIDI hardware and writes files; any web page the user
    visits can otherwise fire requests at localhost:8765.
    """
    host = request.headers.get("host", "")
    origin = request.headers.get("origin")
    if host not in _ALLOWED_HOSTS:
        return JSONResponse({"detail": "forbidden host"}, status_code=403)
    if origin is not None and origin not in _ALLOWED_ORIGINS:
        return JSONResponse({"detail": "forbidden origin"}, status_code=403)
    return await call_next(request)


# ── request models ───────────────────────────────────────────
class ConnectReq(BaseModel):
    port: str
    channel: int = 3
    device: int | str | None = None


class TestReq(BaseModel):
    device: int | str | None = None


class MonitorReq(BaseModel):
    input: int | str
    output: int | str
    gain: float = 1.0


class RecordStopReq(BaseModel):
    name: str = "recording"
    as_target: bool = False


class StartReq(BaseModel):
    max_iters: int = Field(40, ge=1, le=1000)
    popsize: int | None = Field(None, ge=2, le=128)
    include_effects: bool = True
    optimizer: Literal["cma", "random"] = "cma"
    mode: Literal["auto", "interactive"] = "auto"
    calibrate: bool = True
    seed: int | None = None


class SaveReq(BaseModel):
    name: str
    overwrite: bool = False


# ── discovery / status ───────────────────────────────────────
@app.get("/api/devices")
def devices() -> list[dict]:
    from ..match.capture import list_input_devices

    return list_input_devices()


@app.get("/api/ports")
def ports() -> dict:
    return {"output": MidiBackend.list_output_ports(), "input": MidiBackend.list_input_ports()}


@app.get("/api/status")
def status() -> dict:
    return {
        "connected": STATE.connected,
        "port": STATE.midi.port_name,
        "channel": STATE.midi.channel + 1,
        "device": STATE.device,
        "running": STATE.running,
        "has_target": STATE.target_clip is not None,
    }


@app.get("/api/diagnostics")
def diagnostics() -> dict:
    return STATE.diagnostics()


def _as_device(v: int | str | None) -> int | str | None:
    return int(v) if isinstance(v, str) and v.isdigit() else v


@app.get("/api/level")
def level(device: int | str) -> dict:
    try:
        return STATE.read_level(_as_device(device))
    except CLIENT_ERRORS as e:
        raise HTTPException(400, str(e))


@app.post("/api/test/signal")
def test_signal(req: TestReq) -> dict:
    try:
        return STATE.test_signal(_as_device(req.device))
    except CLIENT_ERRORS as e:
        raise HTTPException(400, str(e))


@app.post("/api/monitor/start")
def monitor_start(req: MonitorReq) -> dict:
    try:
        STATE.start_monitor(_as_device(req.input), _as_device(req.output), req.gain)
    except CLIENT_ERRORS as e:
        raise HTTPException(400, f"could not start monitor: {e}")
    return STATE.monitor_status()


@app.post("/api/monitor/stop")
def monitor_stop() -> dict:
    STATE.stop_monitor()
    return STATE.monitor_status()


class GainReq(BaseModel):
    gain: float = 1.0


@app.post("/api/monitor/gain")
def monitor_gain(req: GainReq) -> dict:
    STATE.monitor.gain = max(0.0, min(20.0, req.gain))
    return {"gain": STATE.monitor.gain}


@app.get("/api/monitor/status")
def monitor_status() -> dict:
    return STATE.monitor_status()


@app.post("/api/monitor/record/start")
def record_start() -> dict:
    try:
        STATE.record_start()
    except CLIENT_ERRORS as e:
        raise HTTPException(400, str(e))
    return {"recording": True}


@app.post("/api/monitor/record/stop")
def record_stop(req: RecordStopReq) -> dict:
    try:
        out = STATE.record_stop(req.name, req.as_target)
    except CLIENT_ERRORS as e:
        raise HTTPException(400, str(e))
    if out.get("is_target"):
        out["spectrogram"] = spectrogram_payload(STATE.target_clip)
    return out


@app.post("/api/connect")
def connect(req: ConnectReq) -> dict:
    try:
        STATE.connect(req.port, req.channel, req.device)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"connect failed: {e}")
    return status()


# ── target ───────────────────────────────────────────────────
@app.post("/api/target")
async def upload_target(file: UploadFile) -> dict:
    chunks: list[bytes] = []
    size = 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "audio file too large (50 MB max)")
        chunks.append(chunk)
    data = b"".join(chunks)
    suffix = Path(file.filename or "target.wav").suffix or ".wav"
    try:
        clip = STATE.load_target_bytes(data, suffix)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"could not read audio: {e}")
    return {"duration": round(clip.duration, 3), "spectrogram": spectrogram_payload(clip)}


# ── match lifecycle ──────────────────────────────────────────
@app.post("/api/match/start")
def match_start(req: StartReq) -> dict:
    config = MatchConfig(
        max_iters=req.max_iters,
        popsize=req.popsize,
        include_effects=req.include_effects,
        optimizer=req.optimizer,
        mode=req.mode,
        weights=Weights(),
        seed=req.seed,
    )
    try:
        STATE.start_match(config, calibrate=req.calibrate)
    except MatchAlreadyRunning as e:
        raise HTTPException(409, str(e))
    except CLIENT_ERRORS as e:
        raise HTTPException(400, str(e))
    return {"running": True}


@app.post("/api/match/pause")
def match_pause() -> dict:
    STATE.pause()
    return {"paused": True}


@app.post("/api/match/resume")
def match_resume() -> dict:
    STATE.resume()
    return {"paused": False}


@app.post("/api/match/stop")
def match_stop() -> dict:
    STATE.stop()
    return {"running": False}


@app.get("/api/clip/{which}")
def clip(which: str) -> Response:
    clip = {"best": STATE.best_clip, "last": STATE.last_clip, "target": lambda: STATE.target_clip}.get(which)
    audio = clip() if clip else None
    if audio is None:
        raise HTTPException(404, f"no '{which}' clip available")
    return Response(content=wav_bytes(audio), media_type="audio/wav")


# ── patch bank ───────────────────────────────────────────────
@app.get("/api/patches")
def patches() -> list[dict]:
    out = []
    for p in list_patches():
        out.append({"name": p.stem, "metadata": load_patch_metadata(p)})
    return out


@app.post("/api/patches")
def save_current(req: SaveReq) -> dict:
    if STATE.session is None or not STATE.session.best_patch():
        raise HTTPException(400, "no match result to save")
    from ..patches import patch_path

    try:
        if patch_path(req.name).exists() and not req.overwrite:
            raise HTTPException(409, "patch exists; set overwrite=true")
        meta = {"closeness": round(STATE.session.best_closeness, 2), "source": "match"}
        path = save_patch(req.name, STATE.session.best_patch(), metadata=meta)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"saved": path.name, "metadata": meta}


@app.delete("/api/patches/{name}")
def remove_patch(name: str) -> dict:
    try:
        deleted = delete_patch(name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not deleted:
        raise HTTPException(404, "patch not found")
    return {"deleted": name}


@app.post("/api/patches/{name}/play")
def play_patch(name: str) -> dict:
    from ..patches import patch_path

    try:
        path = patch_path(name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not path.exists():
        raise HTTPException(404, "patch not found")
    if not STATE.connected:
        raise HTTPException(400, "not connected to MIDI")
    values = load_patch(path)

    def _play() -> None:
        for cc, v in values.items():
            STATE.midi.send_cc(cc, v)
        time.sleep(0.05)
        STATE.midi.send_note_on(48)
        time.sleep(1.0)
        STATE.midi.send_note_off(48)

    threading.Thread(target=_play, daemon=True).start()
    return {"playing": name}


# ── live progress ────────────────────────────────────────────
@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    # Per-match, not per-connection: the client keeps one socket across
    # matches, so reset whenever STATE.match_id changes or a second run's
    # target spectrogram never re-renders and best_spec stays gated on the
    # previous run's minimum loss.
    seen_match = -1
    sent_target = False
    last_best_loss = float("inf")
    try:
        while True:
            if STATE.match_id != seen_match:
                seen_match = STATE.match_id
                sent_target = False
                last_best_loss = float("inf")
            p = STATE.get_latest()
            paused = STATE.session.paused if STATE.session else False
            msg: dict = {"type": "tick", "running": STATE.running, "paused": paused}
            if p is not None:
                msg.update(progress_payload(p))
                if not sent_target and STATE.target_clip is not None:
                    msg["target_spec"] = spectrogram_payload(STATE.target_clip)
                    sent_target = True
                if p.best_loss < last_best_loss and STATE.best_clip() is not None:
                    msg["best_spec"] = spectrogram_payload(STATE.best_clip())
                    last_best_loss = p.best_loss
            await websocket.send_json(msg)
            await asyncio.sleep(0.1)
    except WebSocketDisconnect:
        return


# ── static frontend (mounted last so /api wins) ──────────────
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


def run(host: str | None = None, port: int | None = None, open_browser: bool = True) -> None:
    import uvicorn

    host = host or HOST
    port = port or PORT
    url = f"http://{host}:{port}"

    if open_browser:
        def _open() -> None:
            import webbrowser

            webbrowser.open(url)

        threading.Timer(1.2, _open).start()
    print(f"s1 cockpit → {url}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


def main() -> None:
    run()


if __name__ == "__main__":
    main()
