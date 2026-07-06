"""The S-1 cockpit server — the hardware synth, fully present in software.

One FastAPI app serves:

- **The cockpit API**: every schema parameter readable/writable, patch and
  sequence banks, transport, live notes, pattern select (Program Change),
  and .PRM export ("Save to S-1"). The REST/WS surface covers everything a
  human can do in the UI, so an agent driving the documented API has the
  whole synth. Interactive docs at ``/docs``.
- **A live WebSocket** (``/ws/state``): every state change (UI edits,
  physical knob twists, sync/monitor/transport events) is broadcast to all
  open browser views; clients can send notes and param changes back over it.
- **The match studio** (``/api/match/...``): the automated sound-design
  engine, available when the ``[studio]`` extras are installed.

Launch with ``s1`` (or ``synth-web``).
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import synth.engine as engine_module

from .. import patches as patch_bank
from .. import prm as prm_module
from .. import sequences as seq_bank
from ..engine import S1Engine
from ..schema import (
    PRM_PARAMS,
    AccessLevel,
    S1Param,
    param_by_cc,
    sections_for_access,
    settings_menu_params,
)
from ..sequence import MAX_STEPS
from .state import MatchAlreadyRunning, MatchState, studio_available

HOST, PORT = "127.0.0.1", 8765
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

CLIENT_ERRORS: tuple[type[Exception], ...] = (RuntimeError, ValueError)


def eng() -> S1Engine:
    """The current engine (module attribute so tests can swap in fakes)."""
    return engine_module.ENGINE


_match_holder: dict[str, MatchState] = {}


def match_state() -> MatchState:
    ms = _match_holder.get("state")
    if ms is None or ms.engine is not eng():
        ms = MatchState(eng())
        _match_holder["state"] = ms
    return ms


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    eng().start()
    yield
    # Stop the match thread and audio streams before PortAudio tears down,
    # so the process doesn't abort (SIGABRT) on exit with streams still open.
    for fn in (match_state().stop, eng().stop):
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass


app = FastAPI(
    title="s1 cockpit",
    description=(
        "The Roland S-1, fully present in software. Everything the browser "
        "UI can do is here: read/set any parameter, manage patches and "
        "sequences, drive the transport, play notes, switch device patterns, "
        "and export .PRM pattern files for the S-1's disk mode. Live state "
        "streams over the /ws/state WebSocket."
    ),
    lifespan=_lifespan,
)

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


# ══════════════════════════════════════════════════════════════
# Schema + state
# ══════════════════════════════════════════════════════════════

def _param_payload(p: S1Param, value: int | None = None) -> dict:
    return {
        "cc": p.cc,
        "name": p.name,
        "section": p.section,
        "access": p.access.value,
        "type": p.control_type.value,
        "min": p.min_val,
        "max": p.max_val,
        "default": p.default,
        "value": value if value is not None else eng().params.get(p.cc),
        "labels": {str(k): v for k, v in p.value_labels.items()},
        "format": p.display_format,
        "menu_item": p.menu_item,
        "description": p.description,
    }


@app.get("/api/schema", tags=["schema"], summary="The full parameter schema")
def get_schema() -> dict:
    """Every S-1 parameter, organized as the hardware is: faceplate sections
    (knobs + SHIFT combos), the settings menu in manual order, MIDI-only
    performance controls, and the PRM-only tier (disk-mode parameters with
    no CC — informational). Each CC param carries its current value; set one
    via PUT /api/params/{cc}."""
    sections = [
        {"name": name, "params": [_param_payload(p) for p in params]}
        for name, params in sections_for_access((AccessLevel.PANEL, AccessLevel.SHIFT))
    ]
    menu = [
        {"code": code, **_param_payload(p)}
        for code, p in settings_menu_params()
        if isinstance(p, S1Param) and p.access == AccessLevel.MENU
    ]
    midi = [
        _param_payload(p)
        for _, params in sections_for_access((AccessLevel.EXTERNAL,))
        for p in params
    ]
    prm_only = [
        {
            "key": p.key,
            "name": p.name,
            "section": p.section,
            "access": "prm",
            "type": p.control_type.value,
            "min": p.min_val,
            "max": p.max_val,
            "default": p.default,
            "labels": {str(k): v for k, v in p.value_labels.items()},
            "description": p.description,
        }
        for p in PRM_PARAMS
    ]
    return {"sections": sections, "menu": menu, "midi": midi, "prm": prm_only}


@app.get("/api/state", tags=["state"], summary="Full live state snapshot")
def get_state() -> dict:
    """Current value of every CC parameter plus sync/monitor/transport
    status — the same payload the WebSocket sends on connect."""
    return {
        "params": {str(cc): v for cc, v in eng().params.snapshot().items()},
        "status": eng().status(),
    }


@app.get("/api/status", tags=["state"], summary="Connection and service status")
def get_status() -> dict:
    """Sync chip state (disconnected/connecting/synced), S-1 port, detected
    keyboards, monitor and transport status, and studio availability."""
    return {**eng().status(), "studio": studio_available()}


class ParamValue(BaseModel):
    value: int = Field(description="CC value; clamped to the parameter's legal range")


@app.put("/api/params/{cc}", tags=["params"], summary="Set one parameter")
def set_param(cc: int, req: ParamValue) -> dict:
    """Set a parameter by CC number. Updates app state, sends the CC to the
    S-1 (when connected), and broadcasts to every open view. Discrete
    selectors take option indexes (see /api/schema labels)."""
    try:
        value = eng().set_param(cc, req.value, source="ui")
    except KeyError:
        raise HTTPException(404, f"no parameter with CC {cc}")
    return {"cc": cc, "value": value}


class BulkParams(BaseModel):
    values: dict[int, int] = Field(description="Mapping of CC number to value")


@app.post("/api/params", tags=["params"], summary="Set many parameters at once")
def set_params(req: BulkParams) -> dict:
    """Bulk parameter set (e.g. an agent applying a whole sound). Unknown
    CCs are reported back, known ones are applied in order."""
    unknown = [cc for cc in req.values if param_by_cc(cc) is None]
    applied = {}
    for cc, value in req.values.items():
        if param_by_cc(cc) is not None:
            applied[cc] = eng().set_param(cc, value, source="ui")
    return {"applied": applied, "unknown": unknown}


@app.get("/api/params/{cc}", tags=["params"], summary="Read one parameter")
def get_param(cc: int) -> dict:
    p = param_by_cc(cc)
    if p is None:
        raise HTTPException(404, f"no parameter with CC {cc}")
    return _param_payload(p)


# ══════════════════════════════════════════════════════════════
# Notes (QWERTY / agent playing)
# ══════════════════════════════════════════════════════════════

class NoteReq(BaseModel):
    note: int = Field(ge=0, le=127, description="MIDI note number")
    velocity: int = Field(100, ge=1, le=127)
    on: bool = Field(True, description="true = note-on, false = note-off")


@app.post("/api/notes", tags=["notes"], summary="Play or release a note on the S-1")
def play_note(req: NoteReq) -> dict:
    """Send a note-on/off to the S-1 on the synth channel. This is how the
    browser QWERTY keyboard and agents play the synth live."""
    sent = eng().note_on(req.note, req.velocity) if req.on else eng().note_off(req.note)
    return {"sent": sent, "note": req.note, "on": req.on}


@app.post("/api/notes/all-off", tags=["notes"], summary="Panic: all notes off")
def all_notes_off() -> dict:
    return {"sent": eng().all_notes_off()}


# ══════════════════════════════════════════════════════════════
# Device patterns (Program Change)
# ══════════════════════════════════════════════════════════════

class PatternReq(BaseModel):
    bank: int = Field(ge=1, le=4, description="Pattern bank 1-4")
    slot: int = Field(ge=1, le=16, description="Slot within the bank, 1-16")


@app.post("/api/device/pattern", tags=["device"],
          summary="Select one of the S-1's 64 internal patterns")
def select_pattern(req: PatternReq) -> dict:
    """Sends Program Change (bank-1)*16 + (slot-1) on the dedicated PC
    channel, switching the S-1's internal pattern live."""
    program = eng().select_pattern(req.bank, req.slot)
    return {"bank": req.bank, "slot": req.slot, "program": program}


@app.get("/api/midi/ports", tags=["device"], summary="List MIDI ports")
def midi_ports() -> dict:
    return {"output": eng().midi.output_names(), "input": eng().midi.input_names()}


# ══════════════════════════════════════════════════════════════
# Patch bank (JSON, app-side)
# ══════════════════════════════════════════════════════════════

class SaveReq(BaseModel):
    name: str
    overwrite: bool = False


@app.get("/api/patches", tags=["patches"], summary="List saved patches")
def list_patches() -> list[dict]:
    return [
        {"name": p.stem, "metadata": patch_bank.load_patch_metadata(p)}
        for p in patch_bank.list_patches()
    ]


@app.post("/api/patches", tags=["patches"], summary="Save the current sound as a patch")
def save_patch(req: SaveReq) -> dict:
    """Snapshots every CC parameter's current value into the JSON bank."""
    try:
        if patch_bank.patch_path(req.name).exists() and not req.overwrite:
            raise HTTPException(409, "patch exists; set overwrite=true")
        path = patch_bank.save_patch(req.name, eng().params.snapshot())
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"saved": path.stem}


@app.get("/api/patches/{name}", tags=["patches"], summary="Read a patch's values")
def get_patch(name: str) -> dict:
    path = _existing_patch(name)
    return {
        "name": name,
        "values": {str(k): v for k, v in patch_bank.load_patch(path).items()},
        "metadata": patch_bank.load_patch_metadata(path),
    }


@app.post("/api/patches/{name}/load", tags=["patches"],
          summary="Load a patch into the live state")
def load_patch(name: str) -> dict:
    """Applies the patch to app state, pushes every CC to the S-1, and
    broadcasts — the loaded sound is immediately live."""
    values = patch_bank.load_patch(_existing_patch(name))
    eng().load_values(values, source="patch")
    return {"loaded": name, "params": len(values)}


@app.post("/api/patches/{name}/play", tags=["patches"],
          summary="Load a patch and audition it")
def play_patch(name: str) -> dict:
    """Loads the patch (app state is truth) and triggers a one-second note."""
    values = patch_bank.load_patch(_existing_patch(name))
    eng().load_values(values, source="patch")
    match_state().play_patch_preview()
    return {"playing": name}


@app.delete("/api/patches/{name}", tags=["patches"], summary="Delete a patch")
def remove_patch(name: str) -> dict:
    try:
        deleted = patch_bank.delete_patch(name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not deleted:
        raise HTTPException(404, "patch not found")
    return {"deleted": name}


def _existing_patch(name: str) -> Path:
    try:
        path = patch_bank.patch_path(name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not path.exists():
        raise HTTPException(404, "patch not found")
    return path


# ══════════════════════════════════════════════════════════════
# Sequencer: live sequence, transport, bank
# ══════════════════════════════════════════════════════════════

class NotePayload(BaseModel):
    step: int = Field(ge=0, lt=MAX_STEPS)
    pitch: int = Field(ge=0, le=127)
    velocity: int = Field(100, ge=1, le=127)
    duration: int = Field(1, ge=1, le=MAX_STEPS)


class SequencePayload(BaseModel):
    steps: int = Field(16, ge=1, le=MAX_STEPS, description=f"Pattern length (max {MAX_STEPS})")
    bpm: float = Field(120.0, gt=0, le=999)
    step_resolution: str = Field("1/16", description="Grid: 1/4 1/8 1/16 1/32 1/64 8t 16t 32t")
    notes: list[NotePayload] = Field(default_factory=list)


def _replace_live_sequence(new) -> None:
    """Swap the live sequence's contents in place — playback (if running)
    follows immediately without stopping."""
    live = eng().sequencer.sequence
    live.notes[:] = new.notes
    live.steps = new.steps
    live.bpm = new.bpm
    live.step_resolution = new.step_resolution
    live.dropped_notes = new.dropped_notes
    eng().publish({"type": "sequence"})


def _sequence_response() -> dict:
    seq = eng().sequencer.sequence
    return {
        **seq_bank.sequence_to_dict(seq),
        "poly_warnings": seq.poly_warnings(),
        "dropped_notes": seq.dropped_notes,
        "transport": eng().transport_status(),
    }


@app.get("/api/sequence", tags=["sequencer"], summary="The live sequence")
def get_sequence() -> dict:
    """The pattern currently loaded in the sequencer, plus device-limit
    warnings (steps holding more than the S-1's 4-note-per-step ceiling)."""
    return _sequence_response()


@app.put("/api/sequence", tags=["sequencer"], summary="Replace the live sequence")
def put_sequence(req: SequencePayload) -> dict:
    """Replaces notes/length/tempo in place — playback (if running) follows
    immediately without stopping. Device limits are enforced: max 64 steps;
    notes past the end are dropped and reported."""
    _replace_live_sequence(seq_bank.sequence_from_dict(req.model_dump()))
    return _sequence_response()


class TransportAction(BaseModel):
    action: Literal["play", "stop", "pause"]


@app.post("/api/transport", tags=["sequencer"], summary="Transport control")
def transport(req: TransportAction) -> dict:
    """play / stop / pause. Play emits MIDI Start + clock so the S-1's
    delay and LFO tempo-sync follow the app."""
    seq = eng().sequencer
    if req.action == "play":
        seq.play()
    elif req.action == "stop":
        seq.stop()
    else:
        seq.pause()
    status = eng().transport_status()
    eng().publish({"type": "transport", **status})
    return status


class TransportSettings(BaseModel):
    bpm: float | None = Field(None, gt=0, le=999)
    gate: float | None = Field(None, gt=0, le=1)
    shuffle: float | None = Field(None, ge=0, le=1)
    probability: float | None = Field(None, ge=0, le=1)
    last_step: int | None = Field(None, ge=1, le=MAX_STEPS)
    clock_enabled: bool | None = None


@app.put("/api/transport", tags=["sequencer"], summary="Performance settings")
def transport_settings(req: TransportSettings) -> dict:
    """Live performance controls: tempo, gate length, shuffle, note
    probability, pattern truncation, MIDI clock on/off."""
    seq = eng().sequencer
    if req.bpm is not None:
        seq.sequence.bpm = req.bpm
    if req.gate is not None:
        seq.gate = req.gate
    if req.shuffle is not None:
        seq.shuffle = req.shuffle
    if req.probability is not None:
        seq.probability = req.probability
    if req.last_step is not None:
        seq.last_step = req.last_step
    if req.clock_enabled is not None:
        seq.clock_enabled = req.clock_enabled
    status = eng().transport_status()
    eng().publish({"type": "transport", **status})
    return {
        **status,
        "gate": seq.gate,
        "shuffle": seq.shuffle,
        "probability": seq.probability,
        "last_step": seq.last_step,
        "clock_enabled": seq.clock_enabled,
    }


@app.get("/api/sequences", tags=["sequencer"], summary="List saved sequences")
def list_sequences() -> list[dict]:
    return [
        {"name": p.stem, "metadata": seq_bank.load_sequence_metadata(p)}
        for p in seq_bank.list_sequences()
    ]


@app.post("/api/sequences", tags=["sequencer"], summary="Save the live sequence")
def save_sequence(req: SaveReq) -> dict:
    try:
        if seq_bank.sequence_path(req.name).exists() and not req.overwrite:
            raise HTTPException(409, "sequence exists; set overwrite=true")
        path = seq_bank.save_sequence(req.name, eng().sequencer.sequence)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"saved": path.stem}


@app.post("/api/sequences/{name}/load", tags=["sequencer"],
          summary="Load a sequence from the bank")
def load_sequence(name: str) -> dict:
    try:
        path = seq_bank.sequence_path(name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not path.exists():
        raise HTTPException(404, "sequence not found")
    _replace_live_sequence(seq_bank.load_sequence(path))
    return _sequence_response()


@app.delete("/api/sequences/{name}", tags=["sequencer"], summary="Delete a sequence")
def delete_sequence(name: str) -> dict:
    try:
        deleted = seq_bank.delete_sequence(name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not deleted:
        raise HTTPException(404, "sequence not found")
    return {"deleted": name}


# ══════════════════════════════════════════════════════════════
# Audio monitor
# ══════════════════════════════════════════════════════════════

@app.get("/api/monitor", tags=["monitor"], summary="Audio monitor status")
def monitor_status() -> dict:
    return eng().monitor_status()


@app.get("/api/monitor/scope", tags=["monitor"],
         summary="Recent waveform for the live oscilloscope")
def monitor_scope(points: int = 128) -> dict:
    """The last ~50 ms of the S-1's audio, downsampled to signed peaks —
    poll this to render a live scope. All zeros while the monitor is off."""
    points = max(16, min(512, points))
    mon = eng().monitor
    return {"running": mon.running, "peak_db": mon.peak_db, "points": mon.scope(points)}


class MuteReq(BaseModel):
    muted: bool


@app.post("/api/monitor/mute", tags=["monitor"], summary="Mute/unmute monitoring")
def monitor_mute(req: MuteReq) -> dict:
    """Silences the S-1 passthrough without closing the streams (the level
    meter keeps running)."""
    eng().monitor.muted = req.muted
    eng().publish({"type": "monitor", **eng().monitor_status()})
    return eng().monitor_status()


class GainReq(BaseModel):
    gain: float = Field(1.0, ge=0.0, le=20.0)


@app.post("/api/monitor/gain", tags=["monitor"], summary="Set monitor gain")
def monitor_gain(req: GainReq) -> dict:
    eng().monitor.gain = req.gain
    return eng().monitor_status()


@app.post("/api/monitor/stop", tags=["monitor"], summary="Stop monitoring")
def monitor_stop() -> dict:
    """Stops the passthrough and disables auto-start until re-enabled."""
    eng().audio_auto = False
    eng().monitor.stop()
    eng().publish({"type": "monitor", **eng().monitor_status()})
    return eng().monitor_status()


@app.post("/api/monitor/start", tags=["monitor"], summary="(Re)start monitoring")
def monitor_start() -> dict:
    """Re-enables auto-start; the engine connects the S-1 audio input to the
    default output on its next scan."""
    eng().audio_auto = True
    eng()._tick_audio()
    return eng().monitor_status()


@app.post("/api/monitor/record/start", tags=["monitor"], summary="Start recording")
def record_start() -> dict:
    try:
        match_state().record_start()
    except CLIENT_ERRORS as e:
        raise HTTPException(400, str(e))
    return {"recording": True}


class RecordStopReq(BaseModel):
    name: str = "recording"
    as_target: bool = False


@app.post("/api/monitor/record/stop", tags=["monitor"], summary="Stop recording")
def record_stop(req: RecordStopReq) -> dict:
    """Writes the take to ~/.synth/recordings; optionally sets it as the
    match target (requires the studio extras)."""
    if req.as_target:
        _require_studio()
    try:
        out = match_state().record_stop(req.name, req.as_target)
    except CLIENT_ERRORS as e:
        raise HTTPException(400, str(e))
    if out.get("is_target"):
        from .serialize import spectrogram_payload

        out["spectrogram"] = spectrogram_payload(match_state().target_clip)
    return out


# ══════════════════════════════════════════════════════════════
# Save to S-1 (.PRM export)
# ══════════════════════════════════════════════════════════════

@app.get("/api/export/device", tags=["export"],
         summary="Is the S-1 mounted in disk mode?")
def export_device_status() -> dict:
    """Checks /Volumes for the S-1 disk-mode drive (hold [PLAY] while
    powering on; takes 1-2 minutes to mount)."""
    vol = prm_module.find_s1_volume()
    return {"mounted": vol is not None, "volume": str(vol) if vol else None}


@app.get("/api/export/prm", tags=["export"],
         summary="Download the current patch+sequence as a .PRM file")
def export_prm(bank: int = 1, slot: int = 1) -> Response:
    """Builds a device-ready S1_PTN<bank>-<slot>.PRM from the live patch and
    sequence. Copy it into the mounted S-1's RESTORE/ folder and press
    [HOLD] to write it into the hardware."""
    try:
        filename = prm_module.pattern_filename(bank, slot)
    except ValueError as e:
        raise HTTPException(400, str(e))
    prm = _build_current_prm()
    return Response(
        content=prm.serialize().encode("ascii"),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/export/device", tags=["export"],
          summary="Write the current patch+sequence into the mounted S-1")
def export_to_device(req: PatternReq) -> dict:
    """Copies the built .PRM straight into the S-1's RESTORE/ folder. The
    device must be mounted in disk mode; press [HOLD] on the S-1 afterwards
    and wait for "dOnE"."""
    path = prm_module.write_to_device(_build_current_prm(), req.bank, req.slot)
    if path is None:
        raise HTTPException(409, "S-1 not mounted — hold [PLAY] while powering on, wait for the drive")
    return {"written": str(path), "next": 'press [HOLD] on the S-1 and wait for "dOnE"'}


def _build_current_prm() -> prm_module.PrmFile:
    return prm_module.build_pattern(
        eng().params.snapshot(), eng().sequencer.sequence
    )


# ══════════════════════════════════════════════════════════════
# Load from S-1 (.PRM import — the librarian)
# ══════════════════════════════════════════════════════════════

def _prm_entry(root: Path, path: Path) -> dict:
    bank_slot = prm_module.parse_pattern_filename(path.name)
    return {
        "name": str(path.relative_to(root)),
        "bank": bank_slot[0] if bank_slot else None,
        "slot": bank_slot[1] if bank_slot else None,
    }


@app.get("/api/import/prm", tags=["import"],
         summary="List importable .PRM patterns")
def import_sources() -> dict:
    """Patterns readable back into the app: the mounted S-1's BACKUP/ folder
    (when the disk-mode ritual has been performed) and any device dumps kept
    in ~/.synth/backups. Load one with POST /api/import/prm."""
    vol = prm_module.find_s1_volume()
    device_files = []
    if vol is not None:
        backup = vol / "BACKUP"
        device_files = [_prm_entry(backup, p) for p in prm_module.list_prm_files(backup)]
    local = [
        _prm_entry(prm_module.BACKUPS_DIR, p)
        for p in prm_module.list_prm_files(prm_module.BACKUPS_DIR)
    ]
    return {
        "device": {"mounted": vol is not None, "files": device_files},
        "backups": local,
    }


class ImportReq(BaseModel):
    source: Literal["device", "backups"]
    name: str = Field(description="File name as listed by GET /api/import/prm")
    load_patch: bool = Field(True, description="Apply the pattern's synth parameters")
    load_sequence: bool = Field(True, description="Load the pattern's step sequence")


def _apply_prm(prm: prm_module.PrmFile, load_patch: bool, load_sequence: bool) -> dict:
    result: dict = {}
    if load_patch:
        values = prm.to_cc_values()
        eng().load_values(values, source="import")
        result["params"] = len(values)
    if load_sequence:
        seq = prm.to_sequence()
        _replace_live_sequence(seq)
        result["sequence"] = {
            "steps": seq.steps,
            "bpm": seq.bpm,
            "notes": len(seq.notes),
            "resolution": seq.step_resolution,
        }
    return result


@app.post("/api/import/prm", tags=["import"],
          summary="Load a listed .PRM pattern into the live state")
def import_pattern(req: ImportReq) -> dict:
    """Reads a pattern from the mounted S-1 (or the local backups folder)
    and makes it live: parameters are applied and pushed to the synth, and
    the sequence replaces the piano roll — the hardware's own patterns,
    editable in the app."""
    if req.source == "device":
        vol = prm_module.find_s1_volume()
        if vol is None:
            raise HTTPException(
                409, "S-1 not mounted — hold [PLAY] while powering on, wait for the drive"
            )
        root = vol / "BACKUP"
    else:
        root = prm_module.BACKUPS_DIR
    root = root.resolve()
    path = (root / req.name).resolve()
    if not path.is_relative_to(root) or path.suffix.upper() != ".PRM":
        raise HTTPException(400, "invalid pattern name")
    if not path.is_file():
        raise HTTPException(404, "pattern not found")
    try:
        prm = prm_module.PrmFile.load(path)
    except prm_module.PrmParseError as e:
        raise HTTPException(400, f"not an S-1 pattern file: {e}")
    return {"loaded": req.name, **_apply_prm(prm, req.load_patch, req.load_sequence)}


@app.post("/api/import/upload", tags=["import"],
          summary="Upload a .PRM file and load it into the live state")
async def import_upload(
    file: UploadFile, load_patch: bool = True, load_sequence: bool = True
) -> dict:
    """Same as POST /api/import/prm but for a pattern file from anywhere —
    drag one out of an old backup and the app plays it."""
    data = await file.read(prm_module.MAX_FILE_BYTES + 1)
    try:
        prm = prm_module.PrmFile.parse(data.decode("ascii", errors="replace"))
    except prm_module.PrmParseError as e:
        raise HTTPException(400, f"not an S-1 pattern file: {e}")
    name = file.filename or "upload.PRM"
    return {"loaded": name, **_apply_prm(prm, load_patch, load_sequence)}


def _require_studio() -> None:
    if not studio_available():
        raise HTTPException(
            501, 'the match studio needs the studio extras: pip install "synth[studio]"'
        )


# ══════════════════════════════════════════════════════════════
# Match studio (behind [studio] extras)
# ══════════════════════════════════════════════════════════════

class StartReq(BaseModel):
    max_iters: int = Field(40, ge=1, le=1000)
    popsize: int | None = Field(None, ge=2, le=128)
    include_effects: bool = True
    optimizer: Literal["cma", "random"] = "cma"
    mode: Literal["auto", "interactive"] = "auto"
    calibrate: bool = True
    seed: int | None = None


@app.get("/api/match/status", tags=["match"], summary="Match session status")
def match_status() -> dict:
    ms = match_state()
    return {
        "available": studio_available(),
        "running": ms.running,
        "has_target": ms.target_clip is not None,
    }


@app.post("/api/target", tags=["match"], summary="Upload a target sound")
async def upload_target(file: UploadFile) -> dict:
    _require_studio()
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
        clip = match_state().load_target_bytes(data, suffix)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"could not read audio: {e}")
    from .serialize import spectrogram_payload

    return {"duration": round(clip.duration, 3), "spectrogram": spectrogram_payload(clip)}


@app.post("/api/match/start", tags=["match"], summary="Start a match run")
def match_start(req: StartReq) -> dict:
    _require_studio()
    from ..match.distance import Weights
    from ..match.session import MatchConfig

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
        match_state().start_match(config, calibrate=req.calibrate)
    except MatchAlreadyRunning as e:
        raise HTTPException(409, str(e))
    except CLIENT_ERRORS as e:
        raise HTTPException(400, str(e))
    return {"running": True}


@app.post("/api/match/pause", tags=["match"])
def match_pause() -> dict:
    match_state().pause()
    return {"paused": True}


@app.post("/api/match/resume", tags=["match"])
def match_resume() -> dict:
    match_state().resume()
    return {"paused": False}


@app.post("/api/match/stop", tags=["match"])
def match_stop() -> dict:
    match_state().stop()
    return {"running": False}


@app.get("/api/clip/{which}", tags=["match"], summary="Listen to a clip")
def clip(which: str) -> Response:
    ms = match_state()
    source = {"best": ms.best_clip, "last": ms.last_clip, "target": lambda: ms.target_clip}.get(which)
    audio = source() if source else None
    if audio is None:
        raise HTTPException(404, f"no '{which}' clip available")
    from .serialize import wav_bytes

    return Response(content=wav_bytes(audio), media_type="audio/wav")


@app.post("/api/match/save", tags=["match"], summary="Save the match's best patch")
def match_save(req: SaveReq) -> dict:
    ms = match_state()
    if ms.session is None or not ms.session.best_patch():
        raise HTTPException(400, "no match result to save")
    try:
        if patch_bank.patch_path(req.name).exists() and not req.overwrite:
            raise HTTPException(409, "patch exists; set overwrite=true")
        meta = {"closeness": round(ms.session.best_closeness, 2), "source": "match"}
        path = patch_bank.save_patch(req.name, ms.session.best_patch(), metadata=meta)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"saved": path.stem, "metadata": meta}


# ── match progress WebSocket (per-match spectrograms/curve) ──
@app.websocket("/ws")
async def ws_match(websocket: WebSocket) -> None:
    await websocket.accept()
    from .serialize import progress_payload, spectrogram_payload

    ms = match_state()
    seen_match = -1
    sent_target = False
    last_best_loss = float("inf")
    try:
        while True:
            if ms.match_id != seen_match:
                seen_match = ms.match_id
                sent_target = False
                last_best_loss = float("inf")
            p = ms.get_latest()
            paused = ms.session.paused if ms.session else False
            msg: dict = {"type": "tick", "running": ms.running, "paused": paused}
            if p is not None:
                msg.update(progress_payload(p))
                if not sent_target and ms.target_clip is not None:
                    msg["target_spec"] = spectrogram_payload(ms.target_clip)
                    sent_target = True
                if p.best_loss < last_best_loss and ms.best_clip() is not None:
                    msg["best_spec"] = spectrogram_payload(ms.best_clip())
                    last_best_loss = p.best_loss
            await websocket.send_json(msg)
            await asyncio.sleep(0.1)
    except WebSocketDisconnect:
        return


# ══════════════════════════════════════════════════════════════
# Live state WebSocket
# ══════════════════════════════════════════════════════════════

@app.websocket("/ws/state")
async def ws_state(websocket: WebSocket) -> None:
    """Streams every live event: param changes (any source, including
    physical knob twists), sync chip changes, monitor levels, transport,
    sequencer position, keyboards. Accepts client messages:
    {"type": "param", "cc": 74, "value": 90} and
    {"type": "note", "note": 60, "velocity": 100, "on": true}."""
    await websocket.accept()
    engine = eng()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=512)

    def push(event: dict) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception:  # queue full or loop gone — drop the event
            pass

    engine.subscribe(push)
    try:
        await websocket.send_json({
            "type": "hello",
            "params": {str(cc): v for cc, v in engine.params.snapshot().items()},
            "status": engine.status(),
            "sequence": seq_bank.sequence_to_dict(engine.sequencer.sequence),
        })

        async def sender() -> None:
            while True:
                event = await queue.get()
                await websocket.send_json(event)

        async def receiver() -> None:
            while True:
                msg = await websocket.receive_json()
                kind = msg.get("type")
                if kind == "param":
                    try:
                        engine.set_param(int(msg["cc"]), int(msg["value"]), source="ui")
                    except (KeyError, ValueError, TypeError):
                        pass
                elif kind == "note":
                    try:
                        if msg.get("on", True):
                            engine.note_on(int(msg["note"]), int(msg.get("velocity", 100)))
                        else:
                            engine.note_off(int(msg["note"]))
                    except (ValueError, TypeError):
                        pass

        send_task = asyncio.create_task(sender())
        recv_task = asyncio.create_task(receiver())
        try:
            done, pending = await asyncio.wait(
                {send_task, recv_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
        finally:
            for task in (send_task, recv_task):
                task.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        engine.unsubscribe(push)


# ── static frontend (mounted last so /api wins) ──────────────
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


def run(host: str | None = None, port: int | None = None, open_browser: bool = True) -> None:
    import uvicorn

    host = host or HOST
    port = port or PORT
    _ALLOWED_HOSTS.update({f"{host}:{port}", host})
    _ALLOWED_ORIGINS.update({f"http://{host}:{port}", f"http://{host}"})
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
