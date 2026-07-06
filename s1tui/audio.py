"""First-class audio monitoring — hear the S-1 out of the Mac, no DAW.

The S-1 is class-compliant USB audio: plugging in the data cable exposes a
2-channel "S-1" input device in CoreAudio. This module finds that device and
routes it to the default output.

Runs two independent PortAudio streams (input from the S-1, output to your
speakers/headphones) bridged by a lock-protected numpy ring buffer. Two streams
rather than one duplex stream because CoreAudio can't open a single duplex stream
across mismatched devices (AUHAL error -10851). A startup cushion + bounded ring
keep the passthrough glitch-free despite the two devices running on separate clocks.

The same input stream feeds clean frames to the recorder and to the matcher's
single-note probes, so there's no device contention — and you hear every candidate
as it's tried.
"""

from __future__ import annotations

import collections
import threading
import time

import numpy as np

from .match import WORKING_SR
from .match.capture import AudioClip

BLOCKSIZE = 512
RING_SECONDS = 0.5      # bounded buffer -> bounded latency
PREFILL_SECONDS = 0.15  # cushion so the output never starves at startup
SCOPE_BLOCKS = 4        # last ~46 ms (at 44.1k/512) feed the UI oscilloscope

# Substrings that identify the S-1's USB audio device in CoreAudio.
S1_DEVICE_MARKERS = ("s-1",)


def list_input_devices() -> list[dict]:
    """All audio input devices: [{index, name, channels, samplerate}]."""
    import sounddevice as sd

    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            out.append({
                "index": i,
                "name": d["name"],
                "channels": d["max_input_channels"],
                "samplerate": d["default_samplerate"],
            })
    return out


def list_output_devices() -> list[dict]:
    """All audio output devices: [{index, name, channels, samplerate}]."""
    import sounddevice as sd

    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] > 0:
            out.append({
                "index": i,
                "name": d["name"],
                "channels": d["max_output_channels"],
                "samplerate": d["default_samplerate"],
            })
    return out


def find_s1_input() -> int | None:
    """Index of the S-1's USB audio input device, or None if not plugged in."""
    for d in list_input_devices():
        name = d["name"].lower()
        if any(marker in name for marker in S1_DEVICE_MARKERS):
            return d["index"]
    return None


def default_output() -> int | None:
    """Index of the system default output device, or None."""
    import sounddevice as sd

    try:
        dev = sd.default.device[1]
    except Exception:
        return None
    return dev if isinstance(dev, int) and dev >= 0 else None


def rescan_devices() -> None:
    """Refresh PortAudio's device list so hot-plugged hardware appears.

    PortAudio snapshots devices at initialization; a reinitialize is the only
    way to see USB devices plugged in after startup. Only safe while no
    streams are open — callers must check that first.
    """
    import sounddevice as sd

    try:
        sd._terminate()
        sd._initialize()
    except Exception:
        pass


class _Ring:
    """Single-producer/single-consumer float32 ring buffer."""

    def __init__(self, capacity: int) -> None:
        self.buf = np.zeros(capacity, dtype=np.float32)
        self.cap = capacity
        self.w = self.r = self.count = 0
        self.lock = threading.Lock()

    def write(self, x: np.ndarray) -> None:
        n = len(x)
        with self.lock:
            if n >= self.cap:
                x = x[-self.cap:]
                n = self.cap
            end = self.w + n
            if end <= self.cap:
                self.buf[self.w:end] = x
            else:
                k = self.cap - self.w
                self.buf[self.w:] = x[:k]
                self.buf[: end - self.cap] = x[k:]
            self.w = end % self.cap
            self.count += n
            if self.count > self.cap:  # overran reader: drop oldest
                self.count = self.cap
                self.r = self.w

    def read(self, n: int) -> np.ndarray:
        out = np.zeros(n, dtype=np.float32)
        with self.lock:
            m = min(n, self.count)
            end = self.r + m
            if end <= self.cap:
                out[:m] = self.buf[self.r:end]
            else:
                k = self.cap - self.r
                out[:k] = self.buf[self.r:]
                out[k:m] = self.buf[: end - self.cap]
            self.r = (self.r + m) % self.cap
            self.count -= m
        return out

    @property
    def filled(self) -> int:
        return self.count


class AudioMonitor:
    def __init__(self) -> None:
        self._in = None
        self._out = None
        self.input: int | str | None = None
        self.output: int | str | None = None
        self.gain: float = 1.0
        self.muted: bool = False
        self.samplerate: int | None = None
        self.peak: float = 0.0
        self.rms: float = 0.0

        self._ring: _Ring | None = None
        # deque append/iteration is GIL-atomic — safe across the audio thread
        self._scope: collections.deque = collections.deque(maxlen=SCOPE_BLOCKS)
        self._capturing = False
        self._cap: list[np.ndarray] = []
        self._recording = False
        self._rec: list[np.ndarray] = []

    @property
    def running(self) -> bool:
        return self._in is not None

    # ── lifecycle ────────────────────────────────────────────
    def start(self, input_dev: int | str, output_dev: int | str, gain: float = 1.0) -> None:
        import sounddevice as sd

        self.stop()
        in_info = sd.query_devices(input_dev, "input")
        out_info = sd.query_devices(output_dev, "output")
        sr = int(in_info["default_samplerate"])
        out_ch = min(2, int(out_info["max_output_channels"])) or 1
        self.samplerate, self.input, self.output, self.gain = sr, input_dev, output_dev, gain
        self._ring = _Ring(int(RING_SECONDS * sr))

        def in_cb(indata, frames, t, status):  # noqa: ANN001
            x = indata[:, 0].copy()
            self.peak = float(np.abs(x).max()) if x.size else 0.0
            self.rms = float(np.sqrt(np.mean(x**2))) if x.size else 0.0
            self._ring.write(x)
            self._scope.append(x)
            if self._capturing:
                self._cap.append(x)
            if self._recording:
                self._rec.append(x)

        def out_cb(outdata, frames, t, status):  # noqa: ANN001
            buf = self._ring.read(frames) * (0.0 if self.muted else self.gain)
            np.clip(buf, -1.0, 1.0, out=buf)
            outdata[:] = buf[:, None]

        self._in = sd.InputStream(samplerate=sr, blocksize=BLOCKSIZE, dtype="float32",
                                  channels=1, device=input_dev, callback=in_cb)
        self._out = sd.OutputStream(samplerate=sr, blocksize=BLOCKSIZE, dtype="float32",
                                    channels=out_ch, device=output_dev, callback=out_cb)
        self._in.start()
        # Build a cushion before opening the output so it never starves on frame 1.
        prefill = int(PREFILL_SECONDS * sr)
        deadline = time.monotonic() + 0.5
        while self._ring.filled < prefill and time.monotonic() < deadline:
            time.sleep(0.005)
        self._out.start()

    def stop(self) -> None:
        for s in (self._in, self._out):
            if s is not None:
                try:
                    s.stop()
                    s.close()
                except Exception:
                    pass
        self._in = self._out = None
        self._ring = None
        self._scope.clear()
        self.peak = 0.0
        self.rms = 0.0
        self._capturing = self._recording = False

    @property
    def healthy(self) -> bool:
        """True while both streams report active (device still present)."""
        if self._in is None or self._out is None:
            return False
        try:
            return bool(self._in.active) and bool(self._out.active)
        except Exception:
            return False

    def scope(self, points: int = 128) -> list[float]:
        """The last ~50 ms of input as ``points`` signed peaks — the UI's
        live oscilloscope. All zeros while stopped or silent."""
        blocks = list(self._scope)
        if not self.running or not blocks:
            return [0.0] * points
        data = np.concatenate(blocks)
        if len(data) < points:
            data = np.pad(data, (points - len(data), 0))
        return [
            float(chunk[np.abs(chunk).argmax()]) if len(chunk) else 0.0
            for chunk in np.array_split(data, points)
        ]

    @property
    def peak_db(self) -> float:
        import math

        return round(max(20.0 * math.log10(max(self.peak, 1e-9)), -120.0), 1)

    @property
    def rms_db(self) -> float:
        import math

        return round(max(20.0 * math.log10(max(self.rms, 1e-9)), -120.0), 1)

    # ── probe capture (for the matcher) ──────────────────────
    def begin_capture(self) -> None:
        self._cap = []
        self._capturing = True

    def end_capture(self) -> AudioClip:
        self._capturing = False
        sr = self.samplerate or WORKING_SR
        data = np.concatenate(self._cap) if self._cap else np.zeros(1, dtype=np.float32)
        return AudioClip(data.astype(np.float32), sr).resample(WORKING_SR)

    # ── recording to disk ────────────────────────────────────
    def record_start(self) -> None:
        self._rec = []
        self._recording = True

    def record_stop(self) -> AudioClip | None:
        self._recording = False
        if not self._rec or self.samplerate is None:
            return None
        data = np.concatenate(self._rec).astype(np.float32)
        self._rec = []
        return AudioClip(data, self.samplerate)

    @property
    def recording(self) -> bool:
        return self._recording
