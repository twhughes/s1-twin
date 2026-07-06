"""The live engine — the S-1's software presence.

Owns app state and all hardware I/O, and brokers every live event:

- Watches MIDI ports continuously. When an S-1 appears it connects, pushes
  the full app state (the S-1 has no SysEx and cannot be queried — app state
  is truth at connect time), and marks the session SYNCED. Unplug/replug is
  handled without a restart.
- Streams incoming CCs (physical knob twists) into :class:`ParamState` and
  out to every subscriber (WebSocket clients).
- Auto-detects non-S-1 MIDI input devices and forwards their notes to the
  S-1, hot-plug aware.
- Auto-starts the audio monitor when the S-1's USB audio input appears, so
  sound comes out of the Mac with no DAW and no config.
- Drives the sequencer engine and live pattern selection (Program Change).

The web server is a thin API over this object. Everything is injectable for
tests: a mido-shaped ``midi_module`` and the audio monitor.
"""

from __future__ import annotations

import threading
import time

from .audio import AudioMonitor, default_output, find_s1_input, rescan_devices
from .midi_backend import MidiBackend
from .schema import param_by_cc
from .sequencer_engine import SequencerEngine
from .state import ParamState

# Sync chip states.
DISCONNECTED = "disconnected"
CONNECTING = "connecting"
SYNCED = "synced"

S1_PORT_MARKER = "s-1"

# The S-1 receives pattern-select Program Changes on a dedicated channel
# (device default: 16; synth channel default: 3). 0-indexed internally.
DEFAULT_SYNTH_CHANNEL = 2
DEFAULT_PC_CHANNEL = 15

POLL_INTERVAL = 0.5


def _is_s1(port_name: str) -> bool:
    return S1_PORT_MARKER in port_name.lower()


class S1Engine:
    """Single place where app state, the device, and subscribers meet."""

    def __init__(
        self,
        midi_module=None,
        monitor: AudioMonitor | None = None,
        poll_interval: float = POLL_INTERVAL,
        audio_auto: bool = True,
    ) -> None:
        self._mido = midi_module
        self.params = ParamState()
        self.midi = MidiBackend(midi_module)
        self.midi.channel = DEFAULT_SYNTH_CHANNEL
        self.pc_channel = DEFAULT_PC_CHANNEL
        self.monitor = monitor if monitor is not None else AudioMonitor()
        self.sequencer = SequencerEngine(self.midi)
        self.sequencer.set_position_callback(
            lambda step: self.publish({"type": "position", "step": step})
        )
        self.sync_state = DISCONNECTED
        self.audio_auto = audio_auto

        self._poll_interval = poll_interval
        self._listeners: list = []
        self._listeners_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_rescan = 0.0
        # External (non-S-1) keyboard inputs: name -> open port.
        self._keyboards: dict[str, object] = {}

        self.params.add_listener(self._on_param_change)

    # ── events ───────────────────────────────────────────────
    def subscribe(self, callback) -> None:
        """Register callback(event: dict). Fired from engine/worker threads."""
        with self._listeners_lock:
            self._listeners.append(callback)

    def unsubscribe(self, callback) -> None:
        with self._listeners_lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def publish(self, event: dict) -> None:
        with self._listeners_lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(event)
            except Exception:
                pass

    # ── param flow ───────────────────────────────────────────
    def _on_param_change(self, cc: int, value: int, source: str) -> None:
        # Knob twists already reflect the device; everything else pushes out.
        if source != "midi":
            self.midi.send_cc(cc, value)
        self.publish({"type": "param", "cc": cc, "value": value, "source": source})

    def set_param(self, cc: int, value: int, source: str = "ui") -> int:
        """Validate against the schema, update state, send, broadcast."""
        param = param_by_cc(cc)
        if param is None:
            raise KeyError(f"unknown CC {cc}")
        value = max(param.min_val, min(param.max_val, int(value)))
        self.params.set(cc, value, source=source)
        return value

    def load_values(self, values: dict[int, int], source: str = "patch") -> None:
        """Bulk-set (e.g. a patch load) — pushes to the device and broadcasts."""
        for cc, value in values.items():
            if param_by_cc(cc) is not None:
                self.set_param(cc, value, source=source)

    def push_all(self) -> None:
        """Send the entire app state to the device (sync-on-connect)."""
        for cc, value in self.params.snapshot().items():
            self.midi.send_cc(cc, value)

    # ── notes / patterns ─────────────────────────────────────
    def note_on(self, note: int, velocity: int = 100) -> bool:
        return self.midi.send_note_on(note, velocity)

    def note_off(self, note: int) -> bool:
        return self.midi.send_note_off(note)

    def all_notes_off(self) -> bool:
        return self.midi.all_notes_off()

    def select_pattern(self, bank: int, slot: int) -> int:
        """Switch the S-1's internal pattern: bank 1-4 x slot 1-16 -> PC 0-63,
        sent on the dedicated PC channel."""
        if not 1 <= bank <= 4:
            raise ValueError("bank must be 1-4")
        if not 1 <= slot <= 16:
            raise ValueError("slot must be 1-16")
        program = (bank - 1) * 16 + (slot - 1)
        self.midi.send_program_change(program, channel=self.pc_channel)
        self.publish({"type": "pattern", "bank": bank, "slot": slot, "program": program})
        return program

    # ── lifecycle ────────────────────────────────────────────
    def start(self) -> None:
        """Start the watcher thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self.sequencer.stop()
        for port in self._keyboards.values():
            try:
                port.close()
            except Exception:
                pass
        self._keyboards.clear()
        self.midi.close()
        self.monitor.stop()

    # ── status ───────────────────────────────────────────────
    def status(self) -> dict:
        return {
            "sync": self.sync_state,
            "port": self.midi.port_name,
            "channel": self.midi.channel + 1,
            "pc_channel": self.pc_channel + 1,
            "keyboards": sorted(self._keyboards),
            "monitor": self.monitor_status(),
            "transport": self.transport_status(),
        }

    def monitor_status(self) -> dict:
        return {
            "running": self.monitor.running,
            "muted": self.monitor.muted,
            "input": self.monitor.input,
            "output": self.monitor.output,
            "gain": self.monitor.gain,
            "peak_db": self.monitor.peak_db,
            "rms_db": self.monitor.rms_db,
        }

    def transport_status(self) -> dict:
        seq = self.sequencer
        return {
            "playing": seq.playing,
            "paused": seq.paused,
            "position": seq.position,
            "bpm": seq.sequence.bpm,
            "steps": seq.sequence.steps,
        }

    # ── the watcher ──────────────────────────────────────────
    def _set_sync(self, state: str) -> None:
        if state != self.sync_state:
            self.sync_state = state
            self.publish({"type": "sync", "state": state, "port": self.midi.port_name})

    def _tick(self) -> None:
        """One pass of the watch loop; separated for tests."""
        self._tick_s1_midi()
        self._tick_incoming_cc()
        self._tick_keyboards()
        self._tick_audio()

    def _tick_s1_midi(self) -> None:
        try:
            outputs = self.midi.output_names()
        except Exception:
            outputs = []
        s1_port = next((p for p in outputs if _is_s1(p)), None)

        if self.midi.connected:
            if s1_port is None:
                # Unplugged. Close without the goodbye CC (port is gone).
                self.midi.drop_ports()
                self._set_sync(DISCONNECTED)
            return

        if s1_port is None:
            self._set_sync(DISCONNECTED)
            return

        # An S-1 appeared: connect and push the whole app state.
        self._set_sync(CONNECTING)
        try:
            self.midi.connect(s1_port)
            self.push_all()
        except Exception:
            self.midi.drop_ports()
            self._set_sync(DISCONNECTED)
            return
        self._set_sync(SYNCED)

    def _tick_incoming_cc(self) -> None:
        if not self.midi.connected:
            return
        for msg in self.midi.poll_input():
            if msg.type == "control_change" and param_by_cc(msg.control) is not None:
                # Physical knob moved: state follows the hardware; no echo.
                self.params.set(msg.control, msg.value, source="midi")

    def _tick_keyboards(self) -> None:
        if self._mido is None:
            import mido as midi_module
        else:
            midi_module = self._mido
        try:
            names = [n for n in midi_module.get_input_names() if not _is_s1(n)]
        except Exception:
            names = []

        changed = False
        for name in names:
            if name not in self._keyboards:
                try:
                    self._keyboards[name] = midi_module.open_input(name)
                    changed = True
                except Exception:
                    pass
        for name in list(self._keyboards):
            if name not in names:
                try:
                    self._keyboards[name].close()
                except Exception:
                    pass
                del self._keyboards[name]
                changed = True
        if changed:
            self.publish({"type": "keyboards", "names": sorted(self._keyboards)})

        # Forward notes from every external keyboard to the S-1.
        for port in self._keyboards.values():
            try:
                pending = list(port.iter_pending())
            except Exception:
                continue
            for msg in pending:
                if msg.type == "note_on" and msg.velocity > 0:
                    self.note_on(msg.note, msg.velocity)
                elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                    self.note_off(msg.note)

    def _tick_audio(self) -> None:
        if not self.audio_auto:
            return
        if self.monitor.running:
            if not self.monitor.healthy:
                # Device vanished mid-stream; tear down and rediscover.
                self.monitor.stop()
                self.publish({"type": "monitor", **self.monitor_status()})
            else:
                self.publish({"type": "monitor", **self.monitor_status()})
            return
        # Not running: look for the S-1's USB audio input. PortAudio only
        # sees hot-plugged devices after a reinitialize, which is expensive —
        # throttle the rescan (and it is only safe while no streams are open).
        now = time.monotonic()
        if now - self._last_rescan >= 2.0:
            self._last_rescan = now
            rescan_devices()
        try:
            s1_in = find_s1_input()
        except Exception:
            s1_in = None
        if s1_in is None:
            return
        out = default_output()
        if out is None:
            return
        try:
            self.monitor.start(s1_in, out)
        except Exception:
            return
        self.publish({"type": "monitor", **self.monitor_status()})

    def _watch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                pass
            self._stop.wait(self._poll_interval)


# The app-wide engine instance (single-user app; the web server serves this).
ENGINE = S1Engine()
