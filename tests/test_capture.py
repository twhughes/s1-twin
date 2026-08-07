"""Device + file I/O tests for ``synth.match.capture``.

Uses the shared ``FakeSounddevice`` (extended here with ``rec``/``wait``) and a
small fake ``soundfile`` module, so ``load_audio``, ``record``,
``refresh_devices``, ``list_input_devices`` and ``list_output_devices`` are
exercised with zero hardware. Asserts real shapes, dtypes, and samplerates.
"""

from __future__ import annotations

import sys

import numpy as np
import pytest

from synth.match import WORKING_SR
from tests.fakes import FakeSounddevice


class RecSounddevice(FakeSounddevice):
    """FakeSounddevice plus the record surface (sd.rec / sd.wait)."""

    def __init__(self, *a, level: float = 0.3, **k):
        super().__init__(*a, **k)
        self.level = level
        self.rec_calls: list[dict] = []
        self.waited = 0

    def rec(self, frames, samplerate=None, channels=1, device=None, dtype="float32"):
        self.rec_calls.append(
            {"frames": frames, "samplerate": samplerate, "channels": channels,
             "device": device, "dtype": dtype}
        )
        return np.full((frames, channels), self.level, dtype=np.float32)

    def wait(self):
        self.waited += 1


class FakeSoundfile:
    """Minimal stand-in for the ``soundfile`` module: only ``read`` is used."""

    def __init__(self, data: np.ndarray, sr: int):
        self._data = data
        self._sr = sr
        self.read_paths: list[str] = []

    def read(self, path, always_2d=False):
        self.read_paths.append(path)
        return self._data, self._sr


@pytest.fixture
def fake_sd(monkeypatch):
    fake = RecSounddevice()
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    return fake


# ── device enumeration ───────────────────────────────────────
class TestDeviceLists:
    def test_list_input_devices_parses_fields(self, fake_sd):
        from synth.match.capture import list_input_devices

        fake_sd.add_s1()  # input device, 2 ch @ 44100
        devs = list_input_devices()
        names = [d["name"] for d in devs]
        assert "MacBook Pro Microphone" in names
        assert "S-1" in names
        assert "MacBook Pro Speakers" not in names  # output-only excluded
        s1 = next(d for d in devs if d["name"] == "S-1")
        assert s1["channels"] == 2
        assert s1["samplerate"] == 44100
        assert isinstance(s1["samplerate"], int)  # capture casts to int
        assert isinstance(s1["index"], int)

    def test_list_output_devices_parses_fields(self, fake_sd):
        from synth.match.capture import list_output_devices

        devs = list_output_devices()
        names = [d["name"] for d in devs]
        assert names == ["MacBook Pro Speakers"]  # only output-capable
        spk = devs[0]
        assert spk["channels"] == 2
        assert spk["samplerate"] == 48000
        assert isinstance(spk["samplerate"], int)

    def test_index_matches_device_table_position(self, fake_sd):
        from synth.match.capture import list_input_devices

        idx = fake_sd.add_s1()
        s1 = next(d for d in list_input_devices() if d["name"] == "S-1")
        assert s1["index"] == idx

    def test_refresh_devices_reinitializes(self, fake_sd):
        from synth.match.capture import refresh_devices

        refresh_devices()
        assert fake_sd.reinitialized == 1

    def test_refresh_devices_swallows_errors(self, fake_sd, monkeypatch):
        from synth.match.capture import refresh_devices

        def boom():
            raise RuntimeError("portaudio busy")

        monkeypatch.setattr(fake_sd, "_terminate", boom)
        refresh_devices()  # must not raise


# ── record ───────────────────────────────────────────────────
class TestRecord:
    def test_records_at_native_sr_then_resamples(self, fake_sd):
        from synth.match.capture import record

        s1 = fake_sd.add_s1()  # 44100 Hz native
        clip = record(s1, duration=0.5)
        # recorded 0.5 s at native 44100, delivered at WORKING_SR
        assert clip.samplerate == WORKING_SR
        assert clip.samples.dtype == np.float32
        assert clip.samples.ndim == 1  # mono
        assert clip.duration == pytest.approx(0.5, abs=0.02)
        # sd.rec was asked for native-rate frames, and sd.wait() blocked once
        call = fake_sd.rec_calls[-1]
        assert call["samplerate"] == 44100
        assert call["frames"] == int(0.5 * 44100)
        assert call["channels"] == 1
        assert fake_sd.waited == 1

    def test_record_preserves_signal_level(self, fake_sd):
        from synth.match.capture import record

        fake_sd.level = 0.42
        s1 = fake_sd.add_s1()
        clip = record(s1, duration=0.3)
        # interior mean (avoid resample-poly edge ringing) tracks the DC level
        interior = clip.samples[100:-100]
        assert interior.mean() == pytest.approx(0.42, abs=0.01)

    def test_record_no_resample_when_native_matches(self, fake_sd):
        from synth.match.capture import record

        # a device already at WORKING_SR: no resample, exact frame count
        idx = len(fake_sd.devices)
        fake_sd.devices.append(
            {"name": "Loopback", "max_input_channels": 1,
             "max_output_channels": 0, "default_samplerate": float(WORKING_SR)}
        )
        clip = record(idx, duration=0.2)
        assert clip.samplerate == WORKING_SR
        assert len(clip.samples) == int(0.2 * WORKING_SR)


# ── load_audio ───────────────────────────────────────────────
class TestLoadAudio:
    def test_loads_and_resamples_to_working_sr(self, monkeypatch):
        from synth.match.capture import load_audio

        data = np.full(44100, 0.5, dtype=np.float32)  # 1 s mono @ 44100
        fake_sf = FakeSoundfile(data, 44100)
        monkeypatch.setitem(sys.modules, "soundfile", fake_sf)
        clip = load_audio("target.wav")
        assert clip.samplerate == WORKING_SR
        assert clip.samples.dtype == np.float32
        assert clip.samples.ndim == 1
        assert clip.duration == pytest.approx(1.0, abs=0.02)
        assert fake_sf.read_paths == ["target.wav"]

    def test_stereo_is_downmixed_to_mono(self, monkeypatch):
        from synth.match.capture import load_audio

        left = np.full((22050, 1), 0.2, dtype=np.float32)
        right = np.full((22050, 1), 0.8, dtype=np.float32)
        stereo = np.hstack([left, right])  # shape (n, 2)
        fake_sf = FakeSoundfile(stereo, WORKING_SR)  # no resample needed
        monkeypatch.setitem(sys.modules, "soundfile", fake_sf)
        clip = load_audio("stereo.wav")
        assert clip.samples.ndim == 1
        assert clip.samplerate == WORKING_SR
        assert clip.samples[0] == pytest.approx(0.5)  # (0.2 + 0.8) / 2

    def test_respects_explicit_target_sr(self, monkeypatch):
        from synth.match.capture import load_audio

        data = np.full(16000, 0.1, dtype=np.float32)
        fake_sf = FakeSoundfile(data, 16000)
        monkeypatch.setitem(sys.modules, "soundfile", fake_sf)
        clip = load_audio("x.wav", sr=8000)
        assert clip.samplerate == 8000
        assert len(clip.samples) == pytest.approx(8000, abs=2)
