"""Shared server state — a single-user, single-session app, so a module-level
singleton is plenty. Holds the MIDI connection, audio device, and the running
match session/thread.
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

from ..match.capture import AudioClip
from ..match.driver import SynthDriver
from ..match.monitor import AudioMonitor
from ..match.session import MatchConfig, MatchSession, Progress
from ..midi_backend import MidiBackend

RECORDING_DIR = Path.home() / ".s1tui" / "recordings"


class MatchAlreadyRunning(RuntimeError):
    """Raised when a second match is started while one is running."""


def _db(amp: float) -> float:
    """Linear amplitude (0-1) to dBFS, floored at -120."""
    import math

    return round(max(20.0 * math.log10(max(amp, 1e-9)), -120.0), 1)


class AppState:
    def __init__(self) -> None:
        self.midi = MidiBackend()
        self.device: int | str | None = None
        self.monitor = AudioMonitor()
        self.session: MatchSession | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.latest: Progress | None = None
        self.target_clip: AudioClip | None = None
        self.last_error: str | None = None
        # Bumped on every start_match so WebSocket clients know to reset
        # their per-match state (target spectrogram, best-loss watermark).
        self.match_id = 0

    # ── connection ───────────────────────────────────────────
    def connect(self, port: str, channel: int, device: int | str | None) -> None:
        self.midi.channel = channel - 1
        self.midi.connect(port)
        self.device = device

    @property
    def connected(self) -> bool:
        return self.midi.connected

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── diagnostics / setup ──────────────────────────────────
    def diagnostics(self) -> dict:
        """Snapshot of the signal chain for the setup wizard."""
        from ..match.capture import list_input_devices, list_output_devices

        ports = MidiBackend.list_output_ports()
        s1 = next((p for p in ports if "s-1" in p.lower() or "s1" in p.lower()), None)
        inputs = list_input_devices()
        outputs = list_output_devices()
        blackhole = next((d for d in inputs if "blackhole" in d["name"].lower()), None)
        return {
            "midi": {"found": s1 is not None, "port": s1, "all": ports},
            "audio": {"blackhole": blackhole, "devices": inputs, "outputs": outputs},
            "connected": self.connected,
            "connected_port": self.midi.port_name,
            "running": self.running,
            "monitor": self.monitor_status(),
        }

    def read_level(self, device: int | str) -> dict:
        """Input level in dBFS for a live meter. Reads the monitor stream if it's
        running (it owns the input), otherwise takes a short standalone capture."""
        if self.monitor.running:
            return {"peak_db": self.monitor.peak_db, "rms_db": self.monitor.rms_db}
        if self.running:
            raise RuntimeError("input busy — start the monitor to meter during a match")
        import numpy as np
        import sounddevice as sd

        info = sd.query_devices(device, "input")
        sr = int(info["default_samplerate"])
        rec = sd.rec(int(0.15 * sr), samplerate=sr, channels=1, device=device, dtype="float32")
        sd.wait()
        sig = rec.reshape(-1)
        peak = float(np.abs(sig).max()) if sig.size else 0.0
        rms = float(np.sqrt(np.mean(sig ** 2))) if sig.size else 0.0
        return {"peak_db": _db(peak), "rms_db": _db(rms)}

    def test_signal(self, device: int | str | None = None) -> dict:
        """Send a probe note and listen: did the S-1's audio actually arrive?"""
        if not self.connected:
            raise RuntimeError("connect to the S-1 MIDI port first")
        if self.running:
            raise RuntimeError("a match is already running")
        import numpy as np

        from ..match.capture import find_onset

        if self.monitor.running:
            self.monitor.begin_capture()
            self.midi.send_note_on(48)
            time.sleep(1.0)
            self.midi.send_note_off(48)
            clip = self.monitor.end_capture()
            sig, sr = clip.samples, clip.samplerate
        else:
            dev = device if device is not None else self.device
            if dev is None:
                raise RuntimeError("choose an audio input device first")
            import sounddevice as sd

            info = sd.query_devices(dev, "input")
            sr = int(info["default_samplerate"])
            rec = sd.rec(int(1.0 * sr), samplerate=sr, channels=1, device=dev, dtype="float32")
            self.midi.send_note_on(48)
            sd.wait()
            self.midi.send_note_off(48)
            sig = rec.reshape(-1)

        peak = float(np.abs(sig).max()) if sig.size else 0.0
        detected = peak > 10 ** (-50.0 / 20.0)  # louder than -50 dBFS
        onset = find_onset(sig, sr) if detected else 0
        return {
            "detected": detected,
            "peak_db": _db(peak),
            "latency_ms": round(onset / sr * 1000.0, 1) if detected else None,
        }

    # ── live monitor ─────────────────────────────────────────
    def start_monitor(self, input_dev: int | str, output_dev: int | str, gain: float = 1.0) -> None:
        if self.running:
            raise RuntimeError("stop the match before changing the monitor")
        self.monitor.start(input_dev, output_dev, gain)
        self.device = input_dev  # the matcher captures from this input

    def stop_monitor(self) -> None:
        self.monitor.stop()

    def monitor_status(self) -> dict:
        return {
            "running": self.monitor.running,
            "input": self.monitor.input,
            "output": self.monitor.output,
            "recording": self.monitor.recording,
            "peak_db": self.monitor.peak_db,
        }

    def record_start(self) -> None:
        if not self.monitor.running:
            raise RuntimeError("start the monitor before recording")
        self.monitor.record_start()

    def record_stop(self, name: str, as_target: bool) -> dict:
        from ..patches import resolve_in_dir, sanitize_name

        name = sanitize_name(name)
        clip = self.monitor.record_stop()
        if clip is None or clip.samples.size == 0:
            raise RuntimeError("nothing was recorded")
        import soundfile as sf

        RECORDING_DIR.mkdir(parents=True, exist_ok=True)
        path = resolve_in_dir(RECORDING_DIR, f"{name}.wav")
        sf.write(str(path), clip.samples, clip.samplerate, subtype="PCM_16")
        result = {"path": str(path), "duration": round(clip.duration, 3)}
        if as_target:
            self.set_target_clip(clip)
            result["is_target"] = True
        return result

    def set_target_clip(self, clip: AudioClip) -> AudioClip:
        from ..match.capture import prepare

        self.target_clip = prepare(clip)
        return self.target_clip

    # ── target ───────────────────────────────────────────────
    def load_target_bytes(self, data: bytes, suffix: str) -> AudioClip:
        from ..match.capture import load_audio, prepare

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(data)
            tmp = Path(f.name)
        try:
            self.target_clip = prepare(load_audio(tmp))
        finally:
            tmp.unlink(missing_ok=True)
        return self.target_clip

    # ── match lifecycle ──────────────────────────────────────
    def start_match(self, config: MatchConfig, calibrate: bool) -> None:
        # The check-then-spawn must be atomic: FastAPI runs sync endpoints in
        # a threadpool, so two near-simultaneous starts can both pass an
        # unguarded running check.
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise MatchAlreadyRunning("a match is already running")
            if self.target_clip is None:
                raise RuntimeError("no target loaded")
            if not self.connected:
                raise RuntimeError("not connected to MIDI")

            driver = SynthDriver(self.midi, device=self.device, monitor=self.monitor)
            session = MatchSession(driver, config)
            session.set_target_clip(self.target_clip)
            self.session = session
            self.latest = None
            self.last_error = None
            self.match_id += 1

            def _run() -> None:
                # A raise here would otherwise die silently on the daemon
                # thread and leave the frontend on "running…" forever.
                try:
                    if calibrate:
                        session.calibrate()
                    session.run(on_progress=self._on_progress)
                except Exception as e:  # noqa: BLE001
                    self.last_error = str(e)
                    tick = session._snapshot(done=True)
                    tick.error = str(e)
                    self._on_progress(tick)

            self._thread = threading.Thread(target=_run, daemon=True)
            self._thread.start()

    def _on_progress(self, p: Progress) -> None:
        with self._lock:
            self.latest = p

    def get_latest(self) -> Progress | None:
        with self._lock:
            return self.latest

    def pause(self) -> None:
        if self.session:
            self.session.pause()

    def resume(self) -> None:
        if self.session:
            self.session.resume()

    def stop(self) -> None:
        if self.session:
            self.session.stop()

    def best_clip(self) -> AudioClip | None:
        with self._lock:
            return self.session._best_clip if self.session else None

    def last_clip(self) -> AudioClip | None:
        p = self.get_latest()
        return p.last_clip if p else None


STATE = AppState()
