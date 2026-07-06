"""Tests for s1tui.audio — the first-class monitor, with fake streams."""

from __future__ import annotations

import sys

import numpy as np
import pytest

from tests.fakes import FakeSounddevice


@pytest.fixture
def fake_sd(monkeypatch):
    fake = FakeSounddevice()
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    # No live callbacks in tests — skip the output-prefill wait.
    monkeypatch.setattr("s1tui.audio.PREFILL_SECONDS", 0.0)
    return fake


@pytest.fixture
def monitor():
    from s1tui.audio import AudioMonitor

    m = AudioMonitor()
    yield m
    m.stop()


# ── device discovery ─────────────────────────────────────────
class TestDiscovery:
    def test_find_s1_absent(self, fake_sd):
        from s1tui.audio import find_s1_input

        assert find_s1_input() is None

    def test_find_s1_present(self, fake_sd):
        from s1tui.audio import find_s1_input

        idx = fake_sd.add_s1()
        assert find_s1_input() == idx

    def test_find_s1_requires_input_channels(self, fake_sd):
        from s1tui.audio import find_s1_input

        fake_sd.devices.append({"name": "S-1", "max_input_channels": 0,
                                "max_output_channels": 2, "default_samplerate": 44100.0})
        assert find_s1_input() is None

    def test_default_output(self, fake_sd):
        from s1tui.audio import default_output

        assert default_output() == 1

    def test_default_output_none_when_unset(self, fake_sd):
        from s1tui.audio import default_output

        fake_sd.default.device = (0, -1)
        assert default_output() is None

    def test_list_devices(self, fake_sd):
        from s1tui.audio import list_input_devices, list_output_devices

        fake_sd.add_s1()
        names = [d["name"] for d in list_input_devices()]
        assert "S-1" in names
        assert all(d["channels"] > 0 for d in list_output_devices())

    def test_rescan_reinitializes_portaudio(self, fake_sd):
        from s1tui.audio import rescan_devices

        rescan_devices()
        assert fake_sd.reinitialized == 1


# ── monitor lifecycle ────────────────────────────────────────
class TestMonitorLifecycle:
    def test_start_opens_both_streams(self, fake_sd, monitor):
        s1 = fake_sd.add_s1()
        monitor.start(s1, 1)
        assert monitor.running
        assert monitor.healthy
        assert fake_sd.input_streams[-1].device == s1
        assert fake_sd.output_streams[-1].device == 1

    def test_stop_closes_streams(self, fake_sd, monitor):
        s1 = fake_sd.add_s1()
        monitor.start(s1, 1)
        monitor.stop()
        assert not monitor.running
        assert fake_sd.input_streams[-1].closed
        assert fake_sd.output_streams[-1].closed

    def test_unhealthy_when_stream_dies(self, fake_sd, monitor):
        s1 = fake_sd.add_s1()
        monitor.start(s1, 1)
        fake_sd.input_streams[-1].active = False  # device vanished
        assert not monitor.healthy

    def test_not_running_initially(self, monitor):
        assert not monitor.running
        assert not monitor.healthy


# ── audio path ───────────────────────────────────────────────
class TestAudioPath:
    def test_passthrough_applies_gain(self, fake_sd, monitor):
        s1 = fake_sd.add_s1()
        monitor.start(s1, 1, gain=2.0)
        out = fake_sd.pump(np.full(512, 0.25, dtype=np.float32))
        assert out.max() == pytest.approx(0.5, abs=1e-6)

    def test_meter_tracks_peak_and_rms(self, fake_sd, monitor):
        s1 = fake_sd.add_s1()
        monitor.start(s1, 1)
        fake_sd.pump(np.full(512, 0.5, dtype=np.float32))
        assert monitor.peak == pytest.approx(0.5)
        assert monitor.rms == pytest.approx(0.5)
        assert monitor.peak_db == pytest.approx(-6.0, abs=0.1)

    def test_mute_silences_output_but_meters(self, fake_sd, monitor):
        s1 = fake_sd.add_s1()
        monitor.start(s1, 1, gain=1.0)
        monitor.muted = True
        out = fake_sd.pump(np.full(512, 0.5, dtype=np.float32))
        assert out.max() == 0.0
        assert monitor.peak == pytest.approx(0.5)  # meter still live

    def test_unmute_restores_output(self, fake_sd, monitor):
        s1 = fake_sd.add_s1()
        monitor.start(s1, 1, gain=1.0)
        monitor.muted = True
        fake_sd.pump(np.full(512, 0.5, dtype=np.float32))
        monitor.muted = False
        out = fake_sd.pump(np.full(512, 0.5, dtype=np.float32))
        assert out.max() == pytest.approx(0.5, abs=1e-6)

    def test_output_clipped(self, fake_sd, monitor):
        s1 = fake_sd.add_s1()
        monitor.start(s1, 1, gain=10.0)
        out = fake_sd.pump(np.full(512, 0.9, dtype=np.float32))
        assert out.max() <= 1.0


# ── recording ────────────────────────────────────────────────
class TestRecording:
    def test_record_roundtrip(self, fake_sd, monitor):
        s1 = fake_sd.add_s1()
        monitor.start(s1, 1)
        monitor.record_start()
        assert monitor.recording
        fake_sd.pump(np.full(1000, 0.3, dtype=np.float32))
        clip = monitor.record_stop()
        assert not monitor.recording
        assert clip is not None
        assert clip.samplerate == 44100
        assert len(clip.samples) == 1000

    def test_record_stop_without_frames(self, fake_sd, monitor):
        s1 = fake_sd.add_s1()
        monitor.start(s1, 1)
        monitor.record_start()
        assert monitor.record_stop() is None


# ── ring buffer ──────────────────────────────────────────────
class TestRing:
    def test_write_read_roundtrip(self):
        from s1tui.audio import _Ring

        r = _Ring(16)
        r.write(np.arange(8, dtype=np.float32))
        out = r.read(8)
        np.testing.assert_array_equal(out, np.arange(8, dtype=np.float32))

    def test_read_underrun_pads_zeros(self):
        from s1tui.audio import _Ring

        r = _Ring(16)
        r.write(np.ones(4, dtype=np.float32))
        out = r.read(8)
        assert out[:4].sum() == 4.0
        assert out[4:].sum() == 0.0

    def test_wraparound(self):
        from s1tui.audio import _Ring

        r = _Ring(8)
        r.write(np.ones(6, dtype=np.float32))
        r.read(6)
        r.write(np.full(6, 2.0, dtype=np.float32))  # wraps
        np.testing.assert_array_equal(r.read(6), np.full(6, 2.0, dtype=np.float32))

    def test_overrun_drops_oldest(self):
        from s1tui.audio import _Ring

        r = _Ring(8)
        r.write(np.arange(12, dtype=np.float32))
        out = r.read(8)
        np.testing.assert_array_equal(out, np.arange(4, 12, dtype=np.float32))


# ── the live scope ───────────────────────────────────────────
class TestScope:
    def test_zeros_when_stopped(self, fake_sd, monitor):
        assert monitor.scope(64) == [0.0] * 64

    def test_zeros_before_any_audio(self, fake_sd, monitor):
        idx = fake_sd.add_s1()
        monitor.start(idx, 1)
        assert monitor.scope(32) == [0.0] * 32

    def test_reflects_recent_input(self, fake_sd, monitor):
        idx = fake_sd.add_s1()
        monitor.start(idx, 1)
        t = np.linspace(0, 1, 2048, endpoint=False)
        fake_sd.pump(0.5 * np.sin(2 * np.pi * 200 * t).astype(np.float32))
        points = monitor.scope(128)
        assert len(points) == 128
        assert max(abs(p) for p in points) == pytest.approx(0.5, abs=0.02)
        assert min(points) < -0.3  # signed peaks keep the waveform's shape

    def test_window_is_recent_blocks_only(self, fake_sd, monitor):
        from s1tui.audio import BLOCKSIZE, SCOPE_BLOCKS

        idx = fake_sd.add_s1()
        monitor.start(idx, 1)
        fake_sd.pump(np.full(BLOCKSIZE * (SCOPE_BLOCKS + 2), 0.9, dtype=np.float32))
        for _ in range(SCOPE_BLOCKS):
            fake_sd.pump(np.zeros(BLOCKSIZE, dtype=np.float32))
        assert max(abs(p) for p in monitor.scope(64)) == 0.0

    def test_cleared_on_stop(self, fake_sd, monitor):
        idx = fake_sd.add_s1()
        monitor.start(idx, 1)
        fake_sd.pump(np.full(1024, 0.7, dtype=np.float32))
        monitor.stop()
        assert monitor.scope(16) == [0.0] * 16
