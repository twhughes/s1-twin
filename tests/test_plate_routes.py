"""W-plate's routes (docs/design/BUILD.md §2.5): the raw monitor window that draws
the live plume, and the curves the browser twin should use. Fakes only: no MIDI
ports, no audio devices."""

from __future__ import annotations

import json
import sys

import numpy as np
import pytest
from fastapi.testclient import TestClient

import synth.engine as engine_module
import synth.web.plate_routes as plate_routes
import synth.web.server as server_mod
from synth.engine import S1Engine
from synth.web.server import app
from tests.fakes import FakeMidiWorld, FakeSounddevice

BASE_URL = "http://127.0.0.1:8766"


@pytest.fixture
def engine(monkeypatch):
    e = S1Engine(midi_module=FakeMidiWorld(), audio_auto=False, poll_interval=999)
    monkeypatch.setattr(engine_module, "ENGINE", e)
    server_mod._match_holder.clear()
    yield e
    e.stop()


@pytest.fixture
def client(engine):
    return TestClient(app, base_url=BASE_URL)


class FakeMonitor:
    """Quacks like AudioMonitor for the route: records the n it was asked for."""

    running = True
    samplerate = 44100

    def __init__(self, samples):
        self.samples = list(samples)
        self.asked: list[int] = []

    def scope_raw(self, n):
        self.asked.append(n)
        return self.samples[-n:]

    def stop(self):
        pass


# ── AudioMonitor.scope_raw: the last n samples of the ring, in order ──
class TestScopeRaw:
    @pytest.fixture
    def fake_sd(self, monkeypatch):
        fake = FakeSounddevice()
        monkeypatch.setitem(sys.modules, "sounddevice", fake)
        monkeypatch.setattr("synth.audio.PREFILL_SECONDS", 0.0)
        return fake

    @pytest.fixture
    def monitor(self):
        from synth.audio import AudioMonitor

        m = AudioMonitor()
        yield m
        m.stop()

    def test_empty_while_stopped(self, monitor):
        assert monitor.scope_raw(2048) == []

    def test_last_n_in_order(self, fake_sd, monitor):
        monitor.start(fake_sd.add_s1(), 1)
        ramp = np.arange(1000, dtype=np.float32) / 1000
        fake_sd.pump(ramp)
        got = monitor.scope_raw(256)
        assert len(got) == 256
        assert np.allclose(got, ramp[-256:])

    def test_never_more_than_arrived(self, fake_sd, monitor):
        monitor.start(fake_sd.add_s1(), 1)
        fake_sd.pump(np.full(300, 0.5, dtype=np.float32))
        assert len(monitor.scope_raw(5000)) == 300

    def test_reads_across_the_ring_wrap(self, fake_sd, monitor):
        monitor.start(fake_sd.add_s1(), 1)
        cap = len(monitor._buf)
        total = cap + 1234                       # the write head has wrapped
        signal = np.sin(np.arange(total) * 0.01).astype(np.float32)
        for i in range(0, total, 512):
            fake_sd.pump(signal[i:i + 512])
        got = monitor.scope_raw(2048)
        assert np.allclose(got, signal[-2048:], atol=1e-6)

    def test_empty_after_stop(self, fake_sd, monitor):
        monitor.start(fake_sd.add_s1(), 1)
        fake_sd.pump(np.ones(512, dtype=np.float32))
        monitor.stop()
        assert monitor.scope_raw(64) == []


# ── GET /api/monitor/raw ──────────────────────────────────────
class TestMonitorRaw:
    def test_stopped_monitor(self, client):
        r = client.get("/api/monitor/raw")
        assert r.status_code == 200
        assert r.json() == {"running": False, "sr": None, "samples": []}

    def test_samples_and_rate(self, client, engine):
        engine.monitor = FakeMonitor([0.1234567891, -0.5, 0.25])
        body = client.get("/api/monitor/raw?n=2048").json()
        assert body["running"] is True and body["sr"] == 44100
        assert body["samples"] == [0.123457, -0.5, 0.25]   # rounded to 6 places for the wire

    def test_n_is_clamped(self, client, engine):
        engine.monitor = mon = FakeMonitor([0.0] * 10)
        client.get("/api/monitor/raw?n=1")
        client.get("/api/monitor/raw?n=999999")
        client.get("/api/monitor/raw")
        assert mon.asked == [16, 8192, 2048]


# ── GET /api/twin/curves ──────────────────────────────────────
class TestTwinCurves:
    @pytest.fixture
    def paths(self, tmp_path, monkeypatch):
        cal = tmp_path / "home" / "twin" / "curves.calibrated.json"
        default = tmp_path / "static" / "twin" / "curves.json"
        monkeypatch.setattr(plate_routes, "CALIBRATED_CURVES", cal)
        monkeypatch.setattr(plate_routes, "DEFAULT_CURVES", default)
        return cal, default

    @staticmethod
    def write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data))

    def test_404_when_no_file(self, client, paths):
        assert client.get("/api/twin/curves").status_code == 404

    def test_defaults_when_uncalibrated(self, client, paths):
        _, default = paths
        self.write(default, {"cutoff": {"lo": 30.0, "hi": 12000.0}})
        r = client.get("/api/twin/curves")
        assert r.status_code == 200
        assert r.json() == {"cutoff": {"lo": 30.0, "hi": 12000.0}}
        assert r.headers["x-twin-curves"] == "default"

    def test_calibrated_wins(self, client, paths):
        cal, default = paths
        self.write(default, {"cutoff": {"lo": 30.0}})
        self.write(cal, {"cutoff": {"lo": 42.0}})
        r = client.get("/api/twin/curves")
        assert r.json() == {"cutoff": {"lo": 42.0}}
        assert r.headers["x-twin-curves"] == "calibrated"
        assert r.headers["cache-control"] == "no-store"

    def test_broken_calibrated_file_is_skipped(self, client, paths):
        cal, default = paths
        self.write(cal, "{ not json")
        self.write(default, {"cutoff": {"lo": 30.0}})
        r = client.get("/api/twin/curves")
        assert r.status_code == 200 and r.headers["x-twin-curves"] == "default"

    def test_non_object_json_is_skipped(self, client, paths):
        cal, _ = paths
        self.write(cal, [1, 2, 3])
        assert client.get("/api/twin/curves").status_code == 404
