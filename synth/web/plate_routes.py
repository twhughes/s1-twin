"""Routes owned by W-plate (see docs/design/BUILD.md §1, §2.5). Mounted by server.py.

- ``GET /api/monitor/raw`` — the S-1's last *n* raw audio samples, for the plate's live
  plume (the Output well draws the real signal in bronze with the twin's prediction over it).
- ``GET /api/twin/curves`` — the curves the browser twin should use: the calibrated set once
  the hardware session has written one, else the exported defaults, else 404 (the browser
  then uses the curves bundled with ``twin/audio.js``). The ``X-Twin-Curves`` header says
  which one it is, so the UI can stop calling the twin uncalibrated when that is no longer true.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

import synth.engine as engine_module

from ..paths import DATA_DIR

router = APIRouter()

# The raw window: at least a few cycles of a low note, at most ~0.2 s at 44.1 kHz.
RAW_MIN, RAW_MAX = 16, 8192

# Where the curves live. Module globals so tests can point them at a tmp dir.
CALIBRATED_CURVES = DATA_DIR / "twin" / "curves.calibrated.json"
DEFAULT_CURVES = Path(__file__).parent / "static" / "twin" / "curves.json"


@router.get("/api/monitor/raw", tags=["monitor"],
            summary="The S-1's last n audio samples, unprocessed")
def monitor_raw(n: int = 2048) -> dict:
    """The newest ``n`` samples of the S-1's audio input (oldest first) at the
    device sample rate ``sr`` — what the Output well draws as the bronze plume.
    ``n`` is clamped to 16..8192. Empty ``samples`` while the monitor is off."""
    n = max(RAW_MIN, min(RAW_MAX, n))
    mon = engine_module.ENGINE.monitor
    samples = np.round(np.asarray(mon.scope_raw(n), dtype=np.float64), 6)
    return {"running": bool(mon.running), "sr": mon.samplerate, "samples": samples.tolist()}


def _load_curves(path: Path) -> dict | None:
    """The file's JSON object, or None if it is missing or not a JSON object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@router.get("/api/twin/curves", tags=["twin"],
            summary="The curves the browser twin should use")
def twin_curves() -> JSONResponse:
    """Calibrated curves (``~/.synth/twin/curves.calibrated.json``, written by
    the hardware session) win; else the exported defaults
    (``static/twin/curves.json``); else 404 and the browser twin keeps its own.
    A file that is not a JSON object is skipped, never served."""
    for path, kind in ((CALIBRATED_CURVES, "calibrated"), (DEFAULT_CURVES, "default")):
        data = _load_curves(path)
        if data is not None:
            return JSONResponse(data, headers={"X-Twin-Curves": kind, "Cache-Control": "no-store"})
    raise HTTPException(404, "no twin curves file; the browser twin uses its bundled curves")
