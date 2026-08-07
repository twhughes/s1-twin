"""The live engine — the S-1's software presence.

Owns app state and all hardware I/O, and brokers every live event:

- Watches MIDI ports continuously. When an S-1 appears it connects
  **listen-only** and marks the session LISTENING — it does *not* push app
  state at the hardware. The S-1 has no SysEx and cannot be queried, so an
  auto-push stomps the device's live patch with app state on every reconnect
  or power cycle (audible wobble/chop — found live 2026-07-28). Sending the
  full state is an explicit act: :meth:`S1Engine.push_all`, reached from
  POST /api/push-all or the cockpit's "Push to S-1" button, which is what
  marks the session SYNCED. Until then app state adopts the hardware via knob
  twists. Unplug/replug is handled without a restart.
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
from collections.abc import Callable

from .audio import AudioMonitor, default_output, find_s1_input, rescan_devices
from .midi_backend import MidiBackend
from .schema import param_by_cc
from .sequencer_engine import SequencerEngine, SequencerLike
from .state import ParamState

# One subscriber to the live event stream: called with the event dict that
# publish() broadcasts (WebSocket clients, tests). See subscribe()/publish().
EventCb = Callable[[dict], None]

# Sync chip states.
DISCONNECTED = "disconnected"
CONNECTING = "connecting"
LISTENING = "listening"   # connected, hardware untouched — app state not pushed
SYNCED = "synced"         # push_all has run; app state and device agree

S1_PORT_MARKER = "s-1"

# The S-1 receives pattern-select Program Changes on a dedicated channel
# (device default: 16; synth channel default: 3). 0-indexed internally.
DEFAULT_SYNTH_CHANNEL = 2
DEFAULT_PC_CHANNEL = 15

POLL_INTERVAL = 0.5

# Keyboard CCs forwarded to the S-1: Mod Wheel (1) and Damper Pedal (64) —
# the device's 'external' performance tier. Everything else is blocked so a
# controller's knobs/faders can't silently rewrite patch parameters.
PERFORMANCE_CCS = frozenset({1, 64})

# Cockpit modes (chassis-spec C8). Exactly one at a time.
#   solo  — the cockpit is the hub: it forwards MK3 notes, monitors the S-1's
#           audio, and masters MIDI clock. This is the default (current app).
#   logic — Logic Pro is the hub and owns notes, clock, and audio, so the
#           cockpit MUST NOT double-drive them: MK3 forwarding, the audio
#           monitor, and sequencer clock-out are all suppressed. Everything
#           else — CC sync both ways, librarian, patch bank, push-all — stays
#           live in every mode.
SOLO = "solo"
LOGIC = "logic"
MODES = (SOLO, LOGIC)


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
        self.sequencer: SequencerLike = SequencerEngine(self.midi)
        self.sequencer.set_position_callback(
            lambda step: self.publish({"type": "position", "step": step})
        )
        self.sync_state = DISCONNECTED
        self.audio_auto = audio_auto
        # Cockpit mode (C8). Default: solo (current behavior). `_solo_clock`
        # remembers the sequencer's clock-out preference so logic mode can
        # suppress it and solo mode restore exactly what the user chose.
        self.mode = SOLO
        self._solo_clock = self.sequencer.clock_enabled

        self._poll_interval = poll_interval
        self._listeners: list[EventCb] = []
        self._listeners_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_rescan = 0.0
        # External (non-S-1) keyboard inputs: name -> open port.
        self._keyboards: dict[str, object] = {}

        self.params.add_listener(self._on_param_change)

    # ── events ───────────────────────────────────────────────
    def subscribe(self, callback: EventCb) -> None:
        """Register callback(event: dict). Fired from engine/worker threads."""
        with self._listeners_lock:
            self._listeners.append(callback)

    def unsubscribe(self, callback: EventCb) -> None:
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
        """Send the entire app state to the device, one CC each.

        EXPLICIT only — never called on connect: it overwrites whatever patch
        the hardware is currently holding. Marks the session SYNCED.
        """
        for cc, value in self.params.snapshot().items():
            self.midi.send_cc(cc, value)
        if self.midi.connected:
            self._set_sync(SYNCED)

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

    # ── mode (C8: hub exclusivity) ───────────────────────────
    def set_mode(self, mode: str) -> str:
        """Switch the cockpit mode, applying the suppression/restoration atomically.

        `logic` stops MK3 forwarding, stops the audio monitor, and stops
        sequencer clock-out (Logic owns those). `solo` restores all three.
        Idempotent; returns the resulting mode. Broadcasts a ``mode`` event.
        """
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
        if mode == self.mode:
            return self.mode
        self.mode = mode
        self._apply_mode()
        self.publish({"type": "mode", "mode": self.mode})
        return self.mode

    def _apply_mode(self) -> None:
        """Apply the current mode's ownership split (C8), atomically.

        CC sync (both directions), the librarian, the patch bank, and
        push-all are never touched — they stay live in every mode.
        """
        if self.mode == LOGIC:
            # Logic masters clock, so silence sequencer clock-out. Remember the
            # user's preference to restore it verbatim on the way back to solo.
            self._solo_clock = self.sequencer.clock_enabled
            self.sequencer.clock_enabled = False
            # Logic owns the audio return; stop the cockpit's own monitor. The
            # mode gate in _tick_audio keeps it from auto-restarting.
            was_running = self.monitor.running
            self.monitor.stop()
            if was_running:
                self.publish({"type": "monitor", **self.monitor_status()})
        else:
            self.sequencer.clock_enabled = self._solo_clock
        # Re-open external keyboards so the note-forwarding callback matches the
        # mode: registered in solo, absent in logic.
        self._reopen_keyboards()
        if self.mode == SOLO:
            # Bring the monitor back if the S-1's audio input is present.
            self._tick_audio()

    def _reopen_keyboards(self) -> None:
        """Close and re-detect external keyboards so their input callback is
        (re)bound for the current mode — solo forwards, logic does not."""
        for port in self._keyboards.values():
            try:
                port.close()
            except Exception:
                pass
        self._keyboards.clear()
        self._tick_keyboards()

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
            "mode": self.mode,
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

        # An S-1 appeared: connect LISTEN-ONLY. No CC is sent — auto-pushing
        # app state stomped the hardware's live patch on every reconnect and
        # power cycle. Pushing is explicit (POST /api/push-all / cockpit
        # button); until then app state adopts the hardware's knob twists.
        self._set_sync(CONNECTING)
        try:
            self.midi.connect(s1_port)
        except Exception:
            self.midi.drop_ports()
            self._set_sync(DISCONNECTED)
            return
        self._set_sync(LISTENING)

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

        # In logic mode Logic owns the notes, so the port opens without the
        # forwarding callback (the entry gate in _forward_keyboard backs this up).
        callback = self._forward_keyboard if self.mode == SOLO else None
        changed = False
        for name in names:
            if name not in self._keyboards:
                try:
                    self._keyboards[name] = midi_module.open_input(
                        name, callback=callback)
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

        # Drain any port delivering via iter_pending (no callback support).
        # With a live callback the queue stays empty and this is a no-op.
        for port in self._keyboards.values():
            try:
                pending = list(port.iter_pending())
            except Exception:
                continue
            for msg in pending:
                self._forward_keyboard(msg)

    def _forward_keyboard(self, msg) -> None:
        """Forward one external-keyboard message to the S-1.

        Fires on the MIDI driver's callback thread the moment a key moves —
        never on the 0.5 s watch tick, which added up to half a second of
        latency and collapsed quick taps into inaudible zero-length notes.
        The backend serializes all sends through its lock, so this is safe.

        Suppressed in logic mode: Logic owns the notes there, so the cockpit
        must not double-drive the S-1 (C8).
        """
        if self.mode == LOGIC:
            return
        if msg.type == "note_on" and msg.velocity > 0:
            self.note_on(msg.note, msg.velocity)
        elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            self.note_off(msg.note)
        elif msg.type == "pitchwheel":
            self.midi.send_pitchwheel(msg.pitch)
        elif msg.type == "control_change" and msg.control in PERFORMANCE_CCS:
            self.midi.send_cc(msg.control, msg.value)

    def _tick_audio(self) -> None:
        # Logic owns the audio return in logic mode; never auto-start the
        # cockpit monitor there (C8).
        if self.mode == LOGIC:
            return
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
