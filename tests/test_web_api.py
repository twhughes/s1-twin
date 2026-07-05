"""Tests for the FastAPI backend — upload, bank, security, and match lifecycle.

No hardware: MIDI is not connected, and target audio is generated in-memory.
"""

import io
import threading
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

import s1tui.patches as patches_mod
import s1tui.web.server as server_mod
import s1tui.web.state as state_mod
from s1tui.match.capture import AudioClip
from s1tui.match.session import Progress
from s1tui.patches import save_patch
from s1tui.web.server import app
from s1tui.web.state import STATE

BASE_URL = "http://127.0.0.1:8765"


@pytest.fixture(autouse=True)
def reset_state():
    """STATE is a module-global singleton — reset it so tests can't leak
    targets/sessions into each other (order-independence)."""
    yield
    STATE.midi.disconnect()
    STATE.session = None
    STATE._thread = None
    STATE.latest = None
    STATE.target_clip = None
    STATE.last_error = None
    STATE.match_id = 0
    STATE.device = None


@pytest.fixture
def client():
    # base_url must match the server's Host allowlist (anti-DNS-rebinding)
    return TestClient(app, base_url=BASE_URL)


@pytest.fixture
def bank(tmp_path, monkeypatch):
    monkeypatch.setattr(patches_mod, "PATCH_DIR", tmp_path)
    return tmp_path


def _wav_bytes(freq=440.0, sr=22050, seconds=1.0) -> bytes:
    import soundfile as sf

    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
    sig = (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    buf = io.BytesIO()
    sf.write(buf, sig, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def _clip(sr=22050, seconds=0.5) -> AudioClip:
    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
    return AudioClip((0.5 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), sr)


def _progress(**kw) -> Progress:
    base = dict(
        iteration=0, evals=0, max_iters=1, best_closeness=0.0,
        best_loss=float("inf"), best_params={},
    )
    base.update(kw)
    return Progress(**base)


def test_status_disconnected(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    assert r.json()["connected"] is False


def test_ports_and_devices_listable(client):
    assert client.get("/api/ports").status_code == 200
    assert client.get("/api/devices").status_code == 200


def test_upload_target_returns_spectrogram(client):
    files = {"file": ("tone.wav", _wav_bytes(), "audio/wav")}
    r = client.post("/api/target", files=files)
    assert r.status_code == 200
    body = r.json()
    assert body["duration"] > 0
    spec = body["spectrogram"]
    assert spec["h"] == 64 and spec["w"] > 0 and spec["data"]
    assert STATE.target_clip is not None


def test_target_clip_download_after_upload(client):
    client.post("/api/target", files={"file": ("t.wav", _wav_bytes(), "audio/wav")})
    r = client.get("/api/clip/target")
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert r.content[:4] == b"RIFF"


def test_bank_list_save_delete(client, bank):
    assert client.get("/api/patches").json() == []

    save_patch("preset-a", {74: 100, 71: 40}, metadata={"closeness": 88.0})
    listing = client.get("/api/patches").json()
    assert len(listing) == 1
    assert listing[0]["name"] == "preset-a"
    assert listing[0]["metadata"]["closeness"] == 88.0

    r = client.delete("/api/patches/preset-a")
    assert r.status_code == 200
    assert client.get("/api/patches").json() == []


def test_delete_missing_patch_404(client, bank):
    assert client.delete("/api/patches/nope").status_code == 404


def test_save_current_without_match_400(client, bank):
    STATE.session = None
    assert client.post("/api/patches", json={"name": "x"}).status_code == 400


def test_play_patch_requires_connection(client, bank):
    save_patch("p", {74: 64})
    # MIDI not connected in tests -> 400
    assert client.post("/api/patches/p/play").status_code == 400


def test_start_match_without_target_or_midi_400(client):
    STATE.target_clip = None
    r = client.post("/api/match/start", json={"max_iters": 2})
    assert r.status_code == 400


# ── security ─────────────────────────────────────────────────


def test_forbidden_origin_rejected(client):
    r = client.get("/api/status", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_forbidden_host_rejected():
    # Default TestClient host is "testserver" — not in the allowlist
    r = TestClient(app).get("/api/status")
    assert r.status_code == 403


@pytest.mark.parametrize("name", ["../escape", "a/b", "..", "", ".hidden", "a\\b"])
def test_save_patch_rejects_traversal_names(client, bank, name, tmp_path):
    class FakeSession:
        best_closeness = 50.0

        def best_patch(self):
            return {74: 64}

    STATE.session = FakeSession()
    r = client.post("/api/patches", json={"name": name, "overwrite": True})
    assert r.status_code in (400, 422)
    # Nothing may have been written outside (or inside) the bank dir
    assert list(tmp_path.rglob("*.json")) == []
    assert not (tmp_path.parent / "escape.json").exists()


def test_record_stop_rejects_traversal_names(client):
    r = client.post(
        "/api/monitor/record/stop",
        json={"name": "../../evil", "as_target": False},
    )
    assert r.status_code == 400


def test_delete_patch_rejects_traversal_names(client, bank):
    # Encoded slashes are collapsed by the router before the handler; a
    # single-segment dotted name exercises the sanitizer itself.
    assert client.delete("/api/patches/.hidden").status_code == 400
    assert client.delete("/api/patches/..").status_code in (400, 404, 405)


def test_upload_size_cap(client, monkeypatch):
    monkeypatch.setattr(server_mod, "MAX_UPLOAD_BYTES", 1000)
    files = {"file": ("big.wav", _wav_bytes(seconds=1.0), "audio/wav")}
    r = client.post("/api/target", files=files)
    assert r.status_code == 413


def test_upload_corrupt_audio_400(client):
    files = {"file": ("bad.wav", b"not audio at all", "audio/wav")}
    assert client.post("/api/target", files=files).status_code == 400


# ── match lifecycle ──────────────────────────────────────────


def test_start_match_invalid_optimizer_422(client):
    STATE.target_clip = _clip()
    r = client.post("/api/match/start", json={"optimizer": "bogus"})
    assert r.status_code == 422


def test_start_match_invalid_mode_422(client):
    r = client.post("/api/match/start", json={"mode": "sideways"})
    assert r.status_code == 422


def test_start_match_while_running_409(client):
    # Simulate a live match thread; the guard must win before target/MIDI checks
    t = threading.Thread(target=time.sleep, args=(0.5,), daemon=True)
    t.start()
    STATE._thread = t
    r = client.post("/api/match/start", json={"max_iters": 2})
    assert r.status_code == 409


def test_match_thread_error_surfaces(client, monkeypatch):
    """A raising session must produce a done tick with the error, not hang."""

    class FakeSession:
        paused = False
        _best_clip = None

        def __init__(self, driver, config):
            pass

        def set_target_clip(self, clip):
            pass

        def calibrate(self):
            raise RuntimeError("boom")

        def _snapshot(self, done=False):
            return _progress(done=done)

    monkeypatch.setattr(state_mod, "SynthDriver", lambda *a, **k: object())
    monkeypatch.setattr(state_mod, "MatchSession", FakeSession)
    STATE.target_clip = _clip()
    from unittest.mock import MagicMock

    STATE.midi._output = MagicMock()  # pretend connected

    r = client.post("/api/match/start", json={"max_iters": 2, "calibrate": True})
    assert r.status_code == 200
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        p = STATE.get_latest()
        if p is not None and p.error:
            break
        time.sleep(0.02)
    p = STATE.get_latest()
    assert p is not None and p.done and p.error == "boom"
    assert STATE.last_error == "boom"
    STATE._thread.join(timeout=2.0)
    assert not STATE.running


def test_pause_resume_stop_endpoints(client):
    assert client.post("/api/match/pause").json() == {"paused": True}
    assert client.post("/api/match/resume").json() == {"paused": False}
    assert client.post("/api/match/stop").json() == {"running": False}


def test_clip_404_when_missing(client):
    STATE.session = None
    STATE.latest = None
    assert client.get("/api/clip/best").status_code == 404
    assert client.get("/api/clip/last").status_code == 404


def test_save_overwrite_conflict_409(client, bank):
    class FakeSession:
        best_closeness = 50.0

        def best_patch(self):
            return {74: 64}

    STATE.session = FakeSession()
    assert client.post("/api/patches", json={"name": "dup"}).status_code == 200
    assert client.post("/api/patches", json={"name": "dup"}).status_code == 409
    assert (
        client.post("/api/patches", json={"name": "dup", "overwrite": True}).status_code
        == 200
    )


# ── websocket ────────────────────────────────────────────────


def _recv_until(ws, pred, tries=30):
    for _ in range(tries):
        msg = ws.receive_json()
        if pred(msg):
            return msg
    raise AssertionError("condition not met within tick budget")


def test_ws_ticks_and_second_match_resends_target(client):
    STATE.target_clip = _clip()
    STATE._on_progress(_progress(best_loss=1.0))
    with client.websocket_connect("/ws") as ws:
        m = _recv_until(ws, lambda m: "target_spec" in m)
        assert m["type"] == "tick"
        # target_spec must not repeat within the same match
        m2 = ws.receive_json()
        assert "target_spec" not in m2
        # A new match (match_id bump) must re-send the target spectrogram
        STATE.match_id += 1
        _recv_until(ws, lambda m: "target_spec" in m)
