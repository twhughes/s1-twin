"""W-rec (docs/design/ROUND2.md §3): targets made for the Match view.

* ``POST /api/match/record-note`` (synth/web/match_ws.py): the S-1 plays one note of its
  current sound and the cockpit's audio monitor captures it. Driven through the real app
  with a fake MIDI world and a fake monitor: no MIDI port, no audio device, no real sleeps.
  The S-1's patch must never change (no CC goes out: ``calibrate()`` would send one).
* The browser's WAV writer (``core/wav.js``): what it writes, the server reads like any
  dropped file. Skips cleanly when ``node`` is not installed.
"""

from __future__ import annotations

import base64
import io
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

import synth.engine as engine_module
import synth.web.match_ws as match_ws
import synth.web.server as server_mod
from synth.engine import S1Engine
from synth.match import ANALYSIS_SECONDS, WORKING_SR
from synth.match import driver as driver_mod
from synth.match.capture import AudioClip
from tests.fakes import FakeMidiWorld

BASE_URL = "http://127.0.0.1:8766"
ROUTE = "/api/match/record-note"
STATIC = Path(__file__).resolve().parents[1] / "synth" / "web" / "static"


class FakeMonitor:
    """Quacks like a running AudioMonitor for probe(): hands back one fixed take."""

    muted, input, output, gain, peak_db, rms_db = False, 1, 2, 1.0, -12.0, -20.0   # status fields

    def __init__(self, samples: np.ndarray, running: bool = True) -> None:
        self.samples = np.asarray(samples, dtype=np.float32)
        self.running = running
        self.captures = 0
        self.capturing = False

    def begin_capture(self) -> None:
        self.capturing = True

    def end_capture(self) -> AudioClip:
        self.capturing = False
        self.captures += 1
        return AudioClip(self.samples.copy(), WORKING_SR)

    def stop(self) -> None:
        self.running = False


class FakeSequencer:
    playing = True

    def stop(self) -> None:
        self.playing = False


def _take(lead: float = 0.1, note: float = 1.5, freq: float = 130.81) -> np.ndarray:
    """What the monitor hears: a little silence, then a decaying saw-ish tone."""
    t = np.arange(int(note * WORKING_SR)) / WORKING_SR
    tone = 0.3 * sum(np.sin(2 * np.pi * h * freq * t) / h for h in range(1, 12)) * np.exp(-t / 0.8)
    return np.concatenate([np.zeros(int(lead * WORKING_SR)), tone])


@pytest.fixture
def world() -> FakeMidiWorld:
    return FakeMidiWorld()


@pytest.fixture
def monitor() -> FakeMonitor:
    return FakeMonitor(_take())


@pytest.fixture
def engine(world, monitor, monkeypatch):
    e = S1Engine(midi_module=world, monitor=monitor, audio_auto=False, poll_interval=999)
    monkeypatch.setattr(engine_module, "ENGINE", e)
    server_mod._match_holder.clear()
    yield e
    e.stop()


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    """The driver's hold and tail, recorded instead of slept."""
    slept: list[float] = []
    monkeypatch.setattr(driver_mod.time, "sleep", lambda s: slept.append(s))
    return slept


@pytest.fixture
def client(engine) -> TestClient:
    # No context manager: the lifespan (the real engine watcher) stays off.
    return TestClient(server_mod.app, base_url=BASE_URL)


def _connect_s1(engine: S1Engine, world: FakeMidiWorld):
    world.add_device(out_name="S-1 MIDI IN", in_name="S-1 MIDI OUT")
    engine._tick()
    assert engine.midi.connected
    return world.outputs["S-1 MIDI IN"]


# ── the happy path ─────────────────────────────────────────────────────────────
def test_plays_one_note_and_answers_the_take(client, engine, world, monitor, sleeps, monkeypatch):
    port = _connect_s1(engine, world)

    def never(*_a, **_k):
        raise AssertionError("calibrate() overwrites the S-1's patch; the route must never call it")

    monkeypatch.setattr(driver_mod.SynthDriver, "calibrate", never)
    r = client.post(ROUTE, json={"note": 52, "velocity": 77, "hold": 0.9, "tail": 0.4})

    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "audio/wav"
    audio, sr = sf.read(io.BytesIO(r.content), dtype="float32")
    assert sr == WORKING_SR and audio.ndim == 1
    assert len(audio) == int(ANALYSIS_SECONDS * WORKING_SR), "prepare(): a fixed 2 s window"
    assert np.abs(audio[: int(0.005 * sr)]).max() > 0.05, "trimmed to the onset: no lead silence"
    assert np.abs(audio).max() > 0.95, "peak-normalized like any probe"

    sent = [(m.type, getattr(m, "note", None), getattr(m, "velocity", None), getattr(m, "channel", None))
            for m in port.sent]
    assert sent == [("note_on", 52, 77, engine.midi.channel), ("note_off", 52, 0, engine.midi.channel)]
    assert not any(m.type == "control_change" for m in port.sent), "the S-1's patch is never touched"
    assert sleeps == [0.9, 0.4], "held for `hold`, then `tail` of release; no settle (no CCs applied)"
    assert monitor.captures == 1 and not monitor.capturing


def test_defaults_are_the_twins_note_timing(client, engine, world, sleeps):
    port = _connect_s1(engine, world)
    assert client.post(ROUTE, json={}).status_code == 200
    assert [(m.type, m.note, m.velocity) for m in port.sent][0] == ("note_on", 48, 100)
    assert sleeps == [1.2, 1.0], "note-off at 1.2 s, as twin.py renders (2.0 s x 0.6)"


# ── when the S-1 cannot do it: 409 and plain words ─────────────────────────────
def _refused(client, words: str) -> None:
    r = client.post(ROUTE, json={"note": 48})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert words in detail and detail.endswith("then try again."), detail


def test_refuses_when_the_s1_is_not_connected(client, sleeps, monitor):
    _refused(client, "not connected")
    assert monitor.captures == 0 and sleeps == []


def test_refuses_when_the_monitor_is_off(client, engine, world, monitor, sleeps):
    port = _connect_s1(engine, world)
    monitor.running = False
    _refused(client, "audio monitor is off")
    assert port.sent == [] and sleeps == []


def test_refuses_in_logic_mode_and_says_why(client, engine, world):
    _connect_s1(engine, world)
    engine.set_mode("logic")                  # logic stops the cockpit's monitor
    _refused(client, "Logic")


def test_refuses_while_the_sequencer_plays(client, engine, world, sleeps, monkeypatch):
    port = _connect_s1(engine, world)
    monkeypatch.setattr(engine, "sequencer", FakeSequencer())
    _refused(client, "sequencer is playing")
    assert port.sent == []


def test_refuses_while_another_match_uses_the_s1(client, engine, world, monkeypatch):
    _connect_s1(engine, world)

    class Busy:
        running = True

    monkeypatch.setattr(server_mod, "match_state", lambda: Busy())
    _refused(client, "Another match")


def test_one_test_note_at_a_time(client, engine, world, sleeps):
    port = _connect_s1(engine, world)
    assert match_ws._note_lock.acquire(blocking=False)
    try:
        r = client.post(ROUTE, json={})
    finally:
        match_ws._note_lock.release()
    assert r.status_code == 409 and "already playing a test note" in r.json()["detail"]
    assert port.sent == []
    assert client.post(ROUTE, json={}).status_code == 200, "the lock is released after a refusal"


def test_a_silent_take_is_refused(client, engine, world, monitor, sleeps):
    _connect_s1(engine, world)
    monitor.samples = (1e-5 * np.random.default_rng(0).standard_normal(WORKING_SR)).astype(np.float32)
    r = client.post(ROUTE, json={})
    assert r.status_code == 409 and "made no sound" in r.json()["detail"], \
        "the raw take's peak decides: prepare() would normalize a noise floor to full scale"


# ── the request itself ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("body", [{"note": 200}, {"velocity": 0}, {"hold": 0}, {"tail": 9}])
def test_out_of_range_requests_are_rejected(client, engine, world, body, sleeps):
    port = _connect_s1(engine, world)
    assert client.post(ROUTE, json=body).status_code == 422
    assert port.sent == []


def test_foreign_origin_is_refused(client, engine, world):
    port = _connect_s1(engine, world)
    r = client.post(ROUTE, json={}, headers={"origin": "http://evil.example"})
    assert r.status_code == 403 and port.sent == []


# ── the browser's WAV reads like a dropped file ────────────────────────────────
NODE = shutil.which("node")

_ENCODE = """
import { encodeWav, trimToOnset } from %s;
const sr = 44100, lead = 0.3, n = Math.round(1.5 * sr), x = new Float32Array(n);
for (let i = 0; i < n; i++) {
  const t = i / sr - lead;
  if (t >= 0) x[i] = 0.4 * Math.sin(2 * Math.PI * 110 * t) * Math.exp(-t / 0.6);
}
process.stdout.write(Buffer.from(encodeWav(trimToOnset(x, sr), sr)).toString("base64"));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_browser_wav_is_read_like_a_dropped_file() -> None:
    from synth.match import twin_session as ts

    module = json.dumps((STATIC / "core" / "wav.js").as_uri())
    out = subprocess.run([NODE, "--input-type=module", "-e", _ENCODE % module],
                         capture_output=True, text=True, timeout=60, check=True).stdout
    raw = base64.b64decode(out)
    samples = ts.decode_upload(raw)                  # the /ws/match path for any upload
    assert abs(len(samples) / WORKING_SR - 1.205) < 0.01, "trimmed to ~5 ms before the note"
    assert np.abs(samples[: int(0.002 * WORKING_SR)]).max() < 1e-3, "the 5 ms kept before the onset"
    assert np.abs(samples[int(0.01 * WORKING_SR): int(0.05 * WORKING_SR)]).max() > 0.2, "then the note"
    plan = ts.plan(raw, "45", "quick")
    assert plan.seeded and plan.notes == [45]
