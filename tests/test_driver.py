"""Direct unit tests for the real :class:`~synth.match.driver.SynthDriver`.

The driver is the only hardware-touching piece of the match engine, and the audit
found its sub-functions untested. These tests exercise them with a fake MIDI
backend and a fake audio monitor (and a fake ``sounddevice`` module for the
own-stream path) — no hardware, no audio device, no real sleeps.
"""

from __future__ import annotations

import numpy as np
import pytest

from synth.match import WORKING_SR
from synth.match import driver as driver_mod
from synth.match.capture import AudioClip
from synth.match.driver import CALIBRATION_PATCH, SynthDriver


# ── fakes ─────────────────────────────────────────────────────
class FakeMidi:
    """Records every CC/note event the driver sends, in order."""

    def __init__(self) -> None:
        self.events: list[tuple] = []

    def send_cc(self, cc: int, value: int) -> bool:
        self.events.append(("cc", cc, value))
        return True

    def send_note_on(self, note: int, velocity: int = 100) -> bool:
        self.events.append(("on", note))
        return True

    def send_note_off(self, note: int) -> bool:
        self.events.append(("off", note))
        return True


class FakeMonitor:
    """Stands in for a running AudioMonitor: hands back a fixed capture clip."""

    def __init__(self, clip: AudioClip, running: bool = True) -> None:
        self._clip = clip
        self.running = running
        self.began = False
        self.ended = False

    def begin_capture(self) -> None:
        self.began = True

    def end_capture(self) -> AudioClip:
        self.ended = True
        return self._clip


class FakeSD:
    """Minimal fake of the sounddevice module surface ``_record_window`` uses."""

    def __init__(self, rec_sr: float = 48000.0) -> None:
        self.rec_sr = rec_sr
        self.rec_calls: list[int] = []
        self.waited = False

    def query_devices(self, device=None, kind=None):
        return {"default_samplerate": self.rec_sr}

    def rec(self, frames, samplerate, channels, device, dtype):
        self.rec_calls.append(int(frames))
        return np.zeros((int(frames), int(channels)), dtype=np.float32)

    def wait(self):
        self.waited = True


def make_clip(samples: np.ndarray, sr: int = WORKING_SR) -> AudioClip:
    return AudioClip(np.asarray(samples, dtype=np.float32), sr)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Neutralize the driver's real time.sleep so tests run instantly."""
    monkeypatch.setattr(driver_mod.time, "sleep", lambda *a, **k: None)


def _driver(midi=None, monitor=None, **kw):
    kw.setdefault("hold_s", 0.0)
    kw.setdefault("tail_s", 0.0)
    return SynthDriver(midi or FakeMidi(), device=None, monitor=monitor, **kw)


# ── apply ─────────────────────────────────────────────────────
def test_apply_sends_every_cc():
    midi = FakeMidi()
    d = _driver(midi)
    d.apply({74: 127, 20: 0, 30: 64})
    assert midi.events == [("cc", 74, 127), ("cc", 20, 0), ("cc", 30, 64)]


# ── _use_monitor ──────────────────────────────────────────────
def test_use_monitor_property():
    assert _driver(monitor=None)._use_monitor is False
    assert _driver(monitor=FakeMonitor(make_clip(np.zeros(10)), running=False))._use_monitor is False
    assert _driver(monitor=FakeMonitor(make_clip(np.zeros(10)), running=True))._use_monitor is True


# ── _play_note ────────────────────────────────────────────────
def test_play_note_sends_on_then_off():
    midi = FakeMidi()
    d = _driver(midi, note=53)
    d._play_note()
    assert midi.events == [("on", 53), ("off", 53)]


# ── probe (monitor path + latency skip-trim) ──────────────────
def test_probe_uses_monitor_and_applies_params(monkeypatch):
    # Identity-prepare so we observe the raw (skip-trimmed) clip.
    monkeypatch.setattr(driver_mod, "prepare", lambda c: c)
    midi = FakeMidi()
    mon = FakeMonitor(make_clip(np.ones(WORKING_SR)))
    d = _driver(midi, monitor=mon, note=60)
    d.probe({74: 100})
    assert mon.began and mon.ended
    # params applied before the note is played
    assert ("cc", 74, 100) in midi.events
    assert midi.events.index(("cc", 74, 100)) < midi.events.index(("on", 60))


def test_probe_trims_calibrated_latency(monkeypatch):
    monkeypatch.setattr(driver_mod, "prepare", lambda c: c)
    mon = FakeMonitor(make_clip(np.ones(WORKING_SR)))  # 1.0 s at WORKING_SR
    d = _driver(monitor=mon)
    d.latency_s = 0.5  # trim the first 0.5 s (== WORKING_SR // 2 samples)
    clip = d.probe()
    assert clip.samples.size == WORKING_SR - int(0.5 * WORKING_SR)


def test_probe_no_latency_keeps_full_clip(monkeypatch):
    monkeypatch.setattr(driver_mod, "prepare", lambda c: c)
    mon = FakeMonitor(make_clip(np.ones(WORKING_SR)))
    d = _driver(monitor=mon)  # latency_s defaults to 0.0
    clip = d.probe()
    assert clip.samples.size == WORKING_SR


# ── _record_window (own-stream path, fake sounddevice) ────────
def test_record_window_play_path(monkeypatch):
    fake_sd = FakeSD(rec_sr=48000.0)
    monkeypatch.setitem(__import__("sys").modules, "sounddevice", fake_sd)
    midi = FakeMidi()
    d = _driver(midi, note=48)
    clip = d._record_window(1.0, play=True)
    assert fake_sd.rec_calls == [48000]  # 1.0 s * 48 kHz
    assert fake_sd.waited
    assert clip.samplerate == d.sr  # resampled to WORKING_SR
    # play=True fires a full note (on + off)
    assert midi.events == [("on", 48), ("off", 48)]


def test_record_window_no_play_holds_note_across_capture(monkeypatch):
    fake_sd = FakeSD(rec_sr=48000.0)
    monkeypatch.setitem(__import__("sys").modules, "sounddevice", fake_sd)
    midi = FakeMidi()
    d = _driver(midi, note=48)
    d._record_window(1.0, play=False)
    # note-on before the wait, note-off after — the note is held while recording
    assert midi.events == [("on", 48), ("off", 48)]
    assert fake_sd.waited


# ── calibrate (onset path, monitor) ───────────────────────────
def test_calibrate_measures_onset_latency():
    # Silence for the first half second, then a burst -> onset ~ 0.5 s.
    sr = WORKING_SR
    samples = np.concatenate([np.zeros(sr // 2), np.ones(sr // 2)])
    midi = FakeMidi()
    mon = FakeMonitor(make_clip(samples, sr))
    d = _driver(midi, monitor=mon)
    latency = d.calibrate()
    assert latency == pytest.approx(0.5, abs=0.05)
    assert d.latency_s == latency
    # the full bright calibration patch went out before measuring
    for cc, value in CALIBRATION_PATCH.items():
        assert ("cc", cc, value) in midi.events


def test_calibrate_silence_yields_zero_latency():
    mon = FakeMonitor(make_clip(np.zeros(WORKING_SR)))
    d = _driver(monitor=mon)
    assert d.calibrate() == 0.0
