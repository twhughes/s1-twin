"""Tests for the cockpit's twin-matcher WebSocket, ``/ws/match`` (synth/web/match_ws.py).

Drives the real cockpit app with FastAPI's TestClient: one binary upload in, one JSON
frame per step out, the candidate in CC space on every frame (BUILD.md §2.4). The
search budget is shrunk to a few steps so each run takes seconds. No hardware: the
route never touches the engine, and the lifespan (the engine's watcher) stays off.
"""

from __future__ import annotations

import io
import json

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

pytest.importorskip("autograd")
pytest.importorskip("scipy")

import synth.web.match_ws as match_ws  # noqa: E402
import synth.web.server as server_mod  # noqa: E402
from synth.match import twin_session as ts  # noqa: E402

TINY = {"gd_iters": 3, "restarts": 1, "neighbor_iters": 2}
HOST = f"127.0.0.1:{server_mod.PORT}"
WS_HEADERS = {"host": HOST}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setitem(ts.QUALITY_PRESETS, "quick", TINY)
    # No context manager: the lifespan (the real engine watcher) stays off.
    return TestClient(server_mod.app, base_url=f"http://{HOST}")


def _tone_wav(freq: float = 220.0, seconds: float = 0.6, sr: int = 22050) -> bytes:
    """A tiny synthetic target: a decaying band-limited saw."""
    t = np.arange(int(sr * seconds)) / sr
    saw = sum(np.sin(2 * np.pi * h * freq * t) * ((-1) ** (h + 1)) / h for h in range(1, 20))
    audio = 0.4 * saw * np.exp(-t / 0.4)
    buf = io.BytesIO()
    sf.write(buf, audio.astype(np.float32), sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def _stream(client: TestClient, payload: bytes, query: str) -> list[dict]:
    frames: list[dict] = []
    with client.websocket_connect(f"/ws/match?{query}", headers=WS_HEADERS) as ws:
        ws.send_bytes(payload)
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame["phase"] in ("done", "error"):
                break
    return frames


def test_happy_path_streams_cc_frames(client: TestClient) -> None:
    init = json.dumps({"74": 30, "71": 100, "28": 0})
    frames = _stream(client, _tone_wav(), f"throttle=0&quality=quick&notes=57&init={init}")

    phases = [f["phase"] for f in frames]
    assert phases == ["pitch"] + ["gd"] * TINY["gd_iters"] + ["done"]
    twin_ccs = {"20", "19", "21", "23", "15", "74", "71", "24", "25", "26",
                "73", "75", "30", "72", "3", "13", "17", "76", "22", "12", "28"}
    for f in frames:
        assert set(f["cc"]) == twin_ccs, "every frame carries the full CC candidate"
        assert "params" not in f
        assert f["notes"] == [57] and f["chord_name"] == "A3"
        assert len(f["wave"]["y"]) == ts.WAVE_SPC * ts.WAVE_CYCLES + 4

    pitch, done = frames[0], frames[-1]
    assert pitch["seeded"] is True
    assert (pitch["cc"]["74"], pitch["cc"]["71"], pitch["cc"]["28"]) == (30, 100, 0)  # warm start
    assert "target_wave" in pitch and "target_wave" in done
    for key in ("closeness", "seconds", "steps", "match_wav_b64", "target_wav_b64"):
        assert key in done


def test_cold_start_detects_the_note(client: TestClient) -> None:
    frames = _stream(client, _tone_wav(220.0), "throttle=0&quality=quick")
    assert frames[0]["phase"] == "pitch" and frames[0]["seeded"] is False
    assert frames[0]["notes"] == [57]
    assert any(f["phase"] == "note-search" for f in frames)
    assert frames[-1]["phase"] == "done"


def test_bad_upload_gets_an_error_frame(client: TestClient) -> None:
    frames = _stream(client, b"this is not an audio file", "throttle=0&quality=quick")
    assert frames == [{"phase": "error", "detail": frames[0]["detail"]}]
    assert "audio" in frames[0]["detail"].lower() and "WAV" in frames[0]["detail"]


def test_oversized_upload_gets_an_error_frame(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(match_ws, "MAX_UPLOAD_BYTES", 16)
    frames = _stream(client, b"\x00" * 64, "throttle=0")
    assert frames[0]["phase"] == "error" and "large" in frames[0]["detail"]


def test_bad_init_gets_an_error_frame(client: TestClient) -> None:
    frames = _stream(client, _tone_wav(), "throttle=0&quality=quick&notes=57&init=[1,2]")
    assert frames[0]["phase"] == "error" and "init" in frames[0]["detail"]


def test_missing_extras_says_how_to_install(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(match_ws, "matcher_available", lambda: False)
    frames = _stream(client, _tone_wav(), "throttle=0")
    assert frames[0]["phase"] == "error" and "pip install" in frames[0]["detail"]


@pytest.mark.parametrize("headers", [
    {"host": "evil.example"},
    {"host": HOST, "origin": "http://evil.example"},
])
def test_foreign_host_or_origin_is_refused(client: TestClient, headers: dict) -> None:
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/match", headers=headers) as ws:
            ws.receive_json()
    assert exc.value.code == 1008
