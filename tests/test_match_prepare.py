"""``POST /api/match/prepare`` (synth/web/match_ws.py, W-rec2): what the matcher will get from an
upload, before a match: the main sound's edges, the key-up guess, the notes, warnings, the take's
outline and the crop to listen to. And the match itself (``/ws/match``) crops the same way, keeps
the crop and the key-up in its diagnosis, and takes the user's own edges (``?crop=``).

Drives the real cockpit app with FastAPI's TestClient; no hardware, the lifespan stays off.
"""

from __future__ import annotations

import base64
import io
import json
import time

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

pytest.importorskip("autograd")
pytest.importorskip("scipy")

import synth.web.match_ws as match_ws  # noqa: E402
import synth.web.server as server_mod  # noqa: E402
from synth.match import WORKING_SR  # noqa: E402
from synth.match import target_prep as tp  # noqa: E402
from synth.match import twin_session as ts  # noqa: E402

HOST = f"127.0.0.1:{server_mod.PORT}"
ROUTE = "/api/match/prepare"
TINY = {"gd_iters": 3, "restarts": 1, "neighbor_iters": 2}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setitem(ts.QUALITY_PRESETS, "quick", TINY)
    return TestClient(server_mod.app, base_url=f"http://{HOST}")


def _take(lead: float = 2.0, sr: int = 48000, noise: float = 0.003, clip: float = 1.0) -> bytes:
    """What the browser uploads: the whole take at its own rate, 16-bit. ``lead`` s of room, then C3
    (a saw) held 0.8 s with a 0.1 s release, then 2 s of room."""
    n = int(1.6 * sr)
    t = np.arange(n) / sr
    env = np.where(t < 0.8, np.minimum(1.0, t / 0.005) * (0.5 + 0.5 * np.exp(-t / 0.2)),
                   (0.5 + 0.5 * np.exp(-0.8 / 0.2)) * np.exp(-(t - 0.8) / 0.1))
    saw = sum(np.sin(2 * np.pi * h * 130.81 * t) / h for h in range(1, 30)) / 1.8
    x = np.concatenate([np.zeros(int(lead * sr)), 0.3 * clip * saw * env, np.zeros(2 * sr)])
    x += noise * np.random.default_rng(0).standard_normal(len(x))
    buf = io.BytesIO()
    sf.write(buf, np.clip(x, -1, 1).astype(np.float32), sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def _post(client: TestClient, body: bytes, query: str = "", **headers) -> object:
    headers = {"content-type": "application/octet-stream", **headers}
    return client.post(ROUTE + query, content=body, headers=headers)


def test_it_says_what_the_matcher_will_get(client: TestClient) -> None:
    r = _post(client, _take())
    assert r.status_code == 200, r.text
    got = r.json()
    assert set(got) == {"duration", "crop", "onset", "gate_s", "noise_db", "peak_db", "notes", "warnings",
                        "peaks", "sr", "wav_b64"}
    assert got["duration"] == pytest.approx(5.6, abs=0.01)
    t0, t1 = got["crop"]
    assert abs(got["onset"] - 2.0) < 0.005 and t0 == pytest.approx(got["onset"] - 0.005, abs=0.002)
    assert 2.9 < t1 < 3.6, "the release is kept, the room after it is not"
    assert abs(got["gate_s"] - 0.8) < 0.06
    assert got["notes"] == [48], "the note is found in the crop (C3)"
    assert got["warnings"] == [] and got["noise_db"] < -40 and got["peak_db"] > -20
    assert len(got["peaks"]) == match_ws.PEAK_COLUMNS and max(got["peaks"]) == 1.0
    assert got["sr"] == WORKING_SR
    audio, sr = sf.read(io.BytesIO(base64.b64decode(got["wav_b64"])), dtype="float32")
    assert sr == WORKING_SR and len(audio) / sr == pytest.approx(t1 - t0, abs=0.002), "the crop, to the ms"
    assert np.abs(audio).max() == pytest.approx(match_ws.PREVIEW_PEAK, abs=0.01), "the crop to listen to"


def test_the_users_edges_win(client: TestClient) -> None:
    got = _post(client, _take(), "?crop=1.5,3.2").json()
    assert got["crop"] == [1.5, 3.2] and abs(got["onset"] - 2.0) < 0.005


def test_a_clipped_take_is_said_from_its_own_samples(client: TestClient) -> None:
    got = _post(client, _take(clip=6.0)).json()
    assert tp.CLIPPED in got["warnings"], "48 kHz flat tops, judged before the resample to 22.05 kHz"


def test_no_clear_sound_is_a_plain_422(client: TestClient) -> None:
    buf = io.BytesIO()
    sf.write(buf, (0.05 * np.random.default_rng(1).standard_normal(3 * 48000)).astype(np.float32), 48000,
             format="WAV", subtype="PCM_16")
    r = _post(client, buf.getvalue())
    assert r.status_code == 422 and r.json()["detail"] == tp.NO_SOUND


def test_not_audio_is_a_plain_422(client: TestClient) -> None:
    r = _post(client, b"this is not an audio file")
    assert r.status_code == 422 and "WAV" in r.json()["detail"]


def test_too_large_is_413_before_it_is_read(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(match_ws, "MAX_UPLOAD_BYTES", 1000)
    r = _post(client, b"\x00" * 5000)
    assert r.status_code == 413 and "25 MB" in r.json()["detail"]


def test_missing_extras_say_how_to_install(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(match_ws, "matcher_available", lambda: False)
    r = _post(client, _take())
    assert r.status_code == 503 and "pip install" in r.json()["detail"]


def test_foreign_origin_is_refused(client: TestClient) -> None:
    r = _post(client, _take(), origin="http://evil.example")
    assert r.status_code == 403


def test_outline() -> None:
    assert match_ws.outline(np.array([0.0, 0.5, -1.0, 0.25]), 2) == [0.5, 1.0]
    assert match_ws.outline(np.zeros(0)) == []
    assert len(match_ws.outline(np.ones(100), 480)) == 100, "never more columns than samples"


# ── the match crops the same way, and keeps what it got ─────────────────────────
def _stream(client: TestClient, payload: bytes, query: str) -> list[dict]:
    frames: list[dict] = []
    with client.websocket_connect(f"/ws/match?{query}", headers={"host": HOST}) as ws:
        ws.send_bytes(payload)
        while True:
            frames.append(ws.receive_json())
            if frames[-1]["phase"] in ("done", "error"):
                return frames


def _meta() -> dict:
    deadline = time.monotonic() + 10          # the run is saved just after the done frame goes out
    while time.monotonic() < deadline:
        runs = sorted(match_ws.MATCH_DIR.glob("*/meta.json"))
        if runs:
            return json.loads(runs[-1].read_text())
        time.sleep(0.05)
    raise AssertionError("the run was not kept")


def test_the_match_gets_the_crop_and_keeps_it(client: TestClient) -> None:
    frames = _stream(client, _take(), "throttle=0&quality=quick")
    assert frames[0]["notes"] == [48], "cold start on the crop, not on two seconds of room"
    assert frames[-1]["phase"] == "done"
    meta = _meta()
    assert abs(meta["crop"][0] - 1.995) < 0.005 and abs(meta["gate_s"] - 0.8) < 0.06
    assert "crop" not in meta["query"]


def test_the_match_takes_the_users_edges(client: TestClient) -> None:
    frames = _stream(client, _take(), "throttle=0&quality=quick&notes=48&crop=1.8,3.3")
    assert frames[-1]["phase"] == "done"
    meta = _meta()
    assert meta["crop"] == [1.8, 3.3] and meta["query"]["crop"] == "1.8,3.3"


def test_save_match_keeps_the_crop(tmp_path) -> None:
    run = match_ws.save_match(b"RIFFxxxx", {}, [], None, "20260101-000000", root=tmp_path,
                              crop=(1.23456, 2.5), gate_s=0.81234)
    meta = json.loads((run / "meta.json").read_text())
    assert meta["crop"] == [1.235, 2.5] and meta["gate_s"] == 0.812
    old = match_ws.save_match(b"RIFFxxxx", {}, [], None, "20260101-000001", root=tmp_path)
    assert json.loads((old / "meta.json").read_text())["crop"] is None
