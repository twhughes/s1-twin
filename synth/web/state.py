"""Match-studio state: the running match session and its target clip.

The cockpit itself (params, sync, monitor, sequencer) lives in
:mod:`synth.engine`; this object only manages sound-match sessions, borrowing
the engine's MIDI connection and audio monitor so there is exactly one of
each in the process. The match engine's heavy numerics (scipy, cma) stay
behind the ``[studio]`` extra — endpoints check :func:`studio_available`.
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # heavy studio types, imported lazily at runtime
    from ..match.capture import AudioClip
    from ..match.session import MatchConfig, MatchSession, Progress

from ..engine import S1Engine
from ..paths import data_dir

RECORDING_DIR = data_dir() / "recordings"


class MatchAlreadyRunning(RuntimeError):
    """Raised when a second match is started while one is running."""


def studio_available() -> bool:
    """True when the [studio] extras (scipy, cma) are installed."""
    try:
        import cma  # noqa: F401
        import scipy  # noqa: F401
    except ImportError:
        return False
    return True


class MatchState:
    def __init__(self, engine: S1Engine) -> None:
        self.engine = engine
        self.session: MatchSession | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.latest: Progress | None = None
        self.target_clip: AudioClip | None = None
        self.last_error: str | None = None
        # Bumped on every start_match so WebSocket clients know to reset
        # their per-match state (target spectrogram, best-loss watermark).
        self.match_id = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── recording (uses the engine's monitor) ────────────────
    def record_start(self) -> None:
        if not self.engine.monitor.running:
            raise RuntimeError("the audio monitor isn't running — plug in the S-1")
        self.engine.monitor.record_start()

    def record_stop(self, name: str, as_target: bool) -> dict:
        from ..patches import resolve_in_dir, sanitize_name

        name = sanitize_name(name)
        clip = self.engine.monitor.record_stop()
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
        from ..match.driver import SynthDriver
        from ..match.session import MatchSession

        # The check-then-spawn must be atomic: FastAPI runs sync endpoints in
        # a threadpool, so two near-simultaneous starts can both pass an
        # unguarded running check.
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise MatchAlreadyRunning("a match is already running")
            if self.target_clip is None:
                raise RuntimeError("no target loaded")
            if not self.engine.midi.connected:
                raise RuntimeError("the S-1 isn't connected")
            if not self.engine.monitor.running:
                raise RuntimeError("the audio monitor isn't running — plug in the S-1")

            driver = SynthDriver(
                self.engine.midi,
                device=self.engine.monitor.input,
                monitor=self.engine.monitor,
            )
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

    def play_patch_preview(self, note: int = 48, hold: float = 1.0) -> None:
        """Trigger a short note so the current state can be heard."""
        engine = self.engine

        def _play() -> None:
            time.sleep(0.05)
            engine.note_on(note)
            time.sleep(hold)
            engine.note_off(note)

        threading.Thread(target=_play, daemon=True).start()
