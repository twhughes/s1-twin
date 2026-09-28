"""End-to-end tests for the soft-synth streaming match server (``soft/server.py``).

The server matches a dropped sound **on this synth** by gradient-descending a
differentiable model of it, streaming one JSON frame per optimization step over a
WebSocket. These tests drive that stream with FastAPI's ``TestClient``: render a
known patch with the model, feed it as the target, and assert the stream is
self-consistent (right detected note, a falling best-loss, a note-search phase, a
complete ``done`` frame with decodable A/B audio). Plus guard tests for bad uploads
and the host guard.

``base_url`` is a localhost URL so the server's host guard admits the request
(it rejects the TestClient default ``testserver`` host).
"""

from __future__ import annotations

import base64
import importlib.util
import io
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

# soft/ is not an importable package name; load server.py by path.
_SERVER_PATH = Path(__file__).resolve().parent.parent / "soft" / "server.py"
_spec = importlib.util.spec_from_file_location("soft_server", _SERVER_PATH)
assert _spec is not None and _spec.loader is not None
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)

from synth.match.twin import Twin  # noqa: E402  (after server module load)

BASE_URL = f"http://127.0.0.1:{server.PORT}"
# TestClient hard-codes the WebSocket Host header to "testserver"; a real browser
# sends the true localhost host. Supply it so the server's host guard admits us.
WS_HEADERS = {"host": f"127.0.0.1:{server.PORT}"}


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(server.app, base_url=BASE_URL)


@pytest.fixture(autouse=True)
def _round3_quick_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests check the soft page's stream end to end, not the search's quality (that is
    tests/test_match_search.py). A budget without the round-4 keys runs the round-3 search exactly,
    so pin "quick" to it: the stream stays the same shape at a third of the time."""
    from synth.match import twin_session

    monkeypatch.setitem(twin_session.QUALITY_PRESETS, "quick",
                        {"gd_iters": 45, "restarts": 1, "neighbor_iters": 14})


def _model_target_wav(note: int = 60) -> bytes:
    """Render a bright, known patch with the differentiable model and return WAV
    bytes. No sub / noise so YIN locks the true octave (a strong sub otherwise
    pulls the estimate an octave down)."""
    twin = Twin()
    cc = {20: 122, 19: 55, 21: 0, 23: 0, 74: 100, 71: 20,
          73: 3, 75: 45, 30: 105, 72: 20}
    k_star = twin.cc_to_k(cc)
    audio = np.asarray(twin.render(k_star, None, note), dtype=np.float32)
    buf = io.BytesIO()
    sf.write(buf, audio, twin.sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def _model_chord_wav(notes: list[int]) -> bytes:
    """Render a bright, known patch as a CHORD (same patch on every note, summed)
    with the differentiable model. No sub / noise so the fundamentals stay clean
    for cold-start detection."""
    twin = Twin()
    cc = {20: 122, 19: 55, 21: 0, 23: 0, 74: 100, 71: 20,
          73: 3, 75: 45, 30: 105, 72: 20}
    k_star = twin.cc_to_k(cc)
    audio = np.asarray(twin.render_chord(k_star, None, notes), dtype=np.float32)
    buf = io.BytesIO()
    sf.write(buf, audio, twin.sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def _decode_wav_b64(b64: str) -> tuple[np.ndarray, int]:
    raw = base64.b64decode(b64)
    data, sr = sf.read(io.BytesIO(raw), always_2d=False)
    return np.asarray(data), int(sr)


def _stream_match(client: TestClient, wav: bytes, query: str = "throttle=0") -> list[dict]:
    """Open the match WebSocket, send the audio, and collect every frame."""
    frames: list[dict] = []
    with client.websocket_connect(f"/ws/match?{query}", headers=WS_HEADERS) as ws:
        ws.send_bytes(wav)
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame["phase"] in ("done", "error"):
                break
    return frames


def test_index_served(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "S‑1 SOFT" in r.text or "S-1 SOFT" in r.text
    assert "/ws/match" in r.text  # the page wires up the streaming endpoint


def test_stream_self_consistent(client: TestClient) -> None:
    wav = _model_target_wav(note=60)
    frames = _stream_match(client, wav, "throttle=0&quality=quick")

    phases = [f["phase"] for f in frames]
    assert phases[-1] == "done"

    # phase 1: a pitch frame with the correct detected note (C4 == MIDI 60).
    pitch = [f for f in frames if f["phase"] == "pitch"]
    assert len(pitch) == 1
    assert pitch[0]["note"] == 60
    assert pitch[0]["note_name"] == "C4"

    # phase 2: multiple gd frames with a non-increasing best_loss.
    gd = [f for f in frames if f["phase"] == "gd"]
    assert len(gd) >= 5
    best = [f["best_loss"] for f in gd]
    assert all(b <= a + 1e-9 for a, b in zip(best, best[1:])), "best_loss must not rise"
    assert best[-1] < best[0]  # it actually descended

    # phase 3: at least one note-search frame.
    ns = [f for f in frames if f["phase"] == "note-search"]
    assert len(ns) >= 1

    # every streamed frame carries the page-unit params for the knobs.
    knob_keys = {"saw", "pulse", "sub", "noise", "pw", "cutoff", "res", "envAmt",
                 "keytrack", "atk", "dec", "sus", "rel", "lfoRate", "lfoDepth",
                 "lfoTarget", "lfoWave"}
    for f in frames:
        assert knob_keys <= set(f["params"])

    # done frame: full contract.
    done = frames[-1]
    for key in ("note", "note_name", "iter", "total", "loss", "best_loss",
                "params", "cc", "closeness", "seconds", "steps",
                "match_wav_b64", "target_wav_b64"):
        assert key in done, f"missing key {key!r}"

    assert done["cc"] and all(0 <= int(v) <= 127 for v in done["cc"].values())
    assert done["closeness"] > 20.0  # self-consistent target lands well above chance

    # both A/B WAVs decode to non-empty, non-silent audio.
    match_audio, match_sr = _decode_wav_b64(done["match_wav_b64"])
    target_audio, target_sr = _decode_wav_b64(done["target_wav_b64"])
    assert match_audio.size > 0 and target_audio.size > 0
    assert match_sr > 0 and target_sr > 0
    assert float(np.abs(match_audio).max()) > 0.0
    assert float(np.abs(target_audio).max()) > 0.0


def test_pitch_frame_carries_note_set(client: TestClient) -> None:
    """Every match now carries the note SET: a mono target's pitch frame lists a
    single-note ``notes`` + ``chord_name`` that agrees with the legacy note fields."""
    frames = _stream_match(client, _model_target_wav(note=60), "throttle=0&quality=quick")
    pitch = next(f for f in frames if f["phase"] == "pitch")
    assert pitch["notes"] == [60]
    assert pitch["chord_name"] == "C4" == pitch["note_name"]
    assert pitch["note"] == 60
    assert pitch["seeded"] is False


def test_chord_seeded_self_consistent(client: TestClient) -> None:
    """SEED path: render a 3-note chord on a known patch, feed it as the target with
    the notes seeded, and assert the stream matches it. Seeded notes are used
    exactly (no detection, no note-search)."""
    notes = [55, 59, 62]  # G3 + B3 + D4
    wav = _model_chord_wav(notes)
    q = "throttle=0&quality=quick&notes=" + ",".join(str(n) for n in notes)
    frames = _stream_match(client, wav, q)

    assert frames[-1]["phase"] == "done"

    # pitch frame: the seeded set, flagged seeded, with the right chord label.
    pitch = next(f for f in frames if f["phase"] == "pitch")
    assert pitch["notes"] == notes
    assert pitch["seeded"] is True
    assert pitch["chord_name"] == "G3+B3+D4"
    assert pitch["note"] == 55 and pitch["note_name"] == "G3"

    # seeded runs skip the note-search-over-set (the notes are known).
    assert not [f for f in frames if f["phase"] == "note-search"]

    # gd frames descend, and carry the note set + restart index.
    gd = [f for f in frames if f["phase"] == "gd"]
    assert len(gd) >= 5
    best = [f["best_loss"] for f in gd]
    assert all(b <= a + 1e-9 for a, b in zip(best, best[1:])), "best_loss must not rise"
    assert best[-1] < best[0]
    assert all(f["notes"] == notes for f in gd)
    assert all("restart" in f for f in gd)

    # done frame: the note set + a decent self-consistent closeness.
    done = frames[-1]
    assert done["notes"] == notes
    assert done["chord_name"] == "G3+B3+D4"
    assert done["seeded"] is True
    assert done["closeness"] > 20.0
    match_audio, _ = _decode_wav_b64(done["match_wav_b64"])
    assert match_audio.size > 0 and float(np.abs(match_audio).max()) > 0.0


def test_chord_cold_start_detects_notes(client: TestClient) -> None:
    """COLD-START path: no seeded notes — the server auto-detects the chord by
    harmonic salience. On a clean model-rendered triad it recovers the set."""
    notes = [60, 64, 67]  # C major triad
    frames = _stream_match(client, _model_chord_wav(notes), "throttle=0&quality=quick")

    pitch = next(f for f in frames if f["phase"] == "pitch")
    assert pitch["seeded"] is False
    assert pitch["notes"] == notes
    assert pitch["chord_name"] == "C4+E4+G4"

    done = frames[-1]
    assert done["phase"] == "done"
    assert "notes" in done and "chord_name" in done
    assert done["closeness"] > 20.0


def test_warm_start_from_init_params(client: TestClient) -> None:
    """The optimizer can warm-start from the user's current knobs: ?init=<json>
    seeds Adam's first restart, so the pitch frame's start params reflect the
    supplied knobs (identity roundtrip through _page_to_k / _k_to_page)."""
    import json

    notes = [60, 64, 67]
    wav = _model_chord_wav(notes)
    init = {"saw": 0.9, "cutoff": 0.3, "res": 0.7, "sub": 0.1, "atk": 0.15}
    q = ("throttle=0&quality=quick&notes=" + ",".join(map(str, notes))
         + "&init=" + json.dumps(init))
    frames = _stream_match(client, wav, q)

    pitch = next(f for f in frames if f["phase"] == "pitch")
    # start knobs match the warm-start we sent (the first restart begins here).
    assert pitch["params"]["saw"] == pytest.approx(0.9, abs=1e-6)
    assert pitch["params"]["cutoff"] == pytest.approx(0.3, abs=1e-6)
    assert pitch["params"]["res"] == pytest.approx(0.7, abs=1e-6)
    assert frames[-1]["phase"] == "done"


def test_rejects_non_audio(client: TestClient) -> None:
    with client.websocket_connect("/ws/match?throttle=0", headers=WS_HEADERS) as ws:
        ws.send_bytes(b"this is definitely not an audio file")
        frame = ws.receive_json()
    assert frame["phase"] == "error"
    assert "audio" in frame["detail"].lower()


def test_rejects_oversized_upload(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # Shrink the cap so the guard trips without shipping 25 MB through the socket.
    monkeypatch.setattr(server, "MAX_UPLOAD_BYTES", 16)
    with client.websocket_connect("/ws/match?throttle=0", headers=WS_HEADERS) as ws:
        ws.send_bytes(b"\x00" * 64)
        frame = ws.receive_json()
    assert frame["phase"] == "error"
    assert "large" in frame["detail"].lower()


def test_host_guard_rejects_foreign_host() -> None:
    # A non-localhost Host header is refused before any handler runs.
    foreign = TestClient(server.app, base_url="http://evil.example")
    r = foreign.get("/")
    assert r.status_code == 403
