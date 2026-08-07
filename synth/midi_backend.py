"""MIDI I/O layer for the Roland S-1."""

from __future__ import annotations

import logging
import threading
from typing import Callable

import mido

logger = logging.getLogger(__name__)

ALL_NOTES_OFF_CC = 123

# A port that has gone away raises OSError (mido's IOError is an alias). That is
# the *expected* failure — a device unplug — and we handle it quietly. Anything
# else is an unexpected bug and gets logged instead of vanishing (FABLE: no
# silent swallow).
_PORT_GONE = OSError


class MidiBackend:
    """Manages MIDI connection to the S-1.

    All sends are serialized through one lock — the UI thread, the sequencer
    thread, and worker threads share this backend. A send that fails (device
    unplugged) marks the backend disconnected instead of raising.

    ``midi_module`` lets tests inject a fake mido-shaped module.
    """

    def __init__(self, midi_module=None) -> None:
        self._mido = midi_module or mido
        self._output: mido.ports.BaseOutput | None = None
        self._input: mido.ports.BaseInput | None = None
        self._port_name: str | None = None
        self._channel: int = 0  # 0-indexed
        self._lock = threading.RLock()
        # Callbacks registered via on_incoming(); poll_input() drives them.
        self._incoming_listeners: list[Callable[[int, int], None]] = []

    @staticmethod
    def list_output_ports() -> list[str]:
        return mido.get_output_names()

    @staticmethod
    def list_input_ports() -> list[str]:
        return mido.get_input_names()

    def output_names(self) -> list[str]:
        return self._mido.get_output_names()

    def input_names(self) -> list[str]:
        return self._mido.get_input_names()

    @property
    def connected(self) -> bool:
        return self._output is not None

    @property
    def port_name(self) -> str | None:
        return self._port_name

    @property
    def channel(self) -> int:
        return self._channel

    @channel.setter
    def channel(self, ch: int) -> None:
        self._channel = max(0, min(15, ch))

    def connect(self, port_name: str) -> None:
        """Open an output (and optionally input) port."""
        self.disconnect()
        with self._lock:
            self._output = self._mido.open_output(port_name)
            self._port_name = port_name
            # Try to open matching input for receiving CC feedback
            # Output ports are "X MIDI IN", input ports are "X MIDI OUT"
            inputs = self.input_names()
            # First try exact match
            if port_name in inputs:
                self._input = self._try_open_input(port_name)
            else:
                # Try to find corresponding input (e.g., "S-1 MIDI IN" -> "S-1 MIDI OUT")
                base_name = port_name.replace(" MIDI IN", "").replace(" IN", "")
                for inp in inputs:
                    if base_name in inp:
                        self._input = self._try_open_input(inp)
                        if self._input is not None:
                            break

    def _try_open_input(self, name: str):
        """Best-effort open of the CC-feedback input port.

        Input feedback is optional — the output is already open, so a failure
        here never fails the connect. A port that is simply gone (OSError) is
        expected and stays quiet; any *other* error is a real bug and gets
        logged rather than silently swallowed.
        """
        try:
            return self._mido.open_input(name)
        except _PORT_GONE:
            return None
        except Exception:
            logger.exception("unexpected error opening input port %r", name)
            return None

    def disconnect(self) -> None:
        """Flush hung notes with all-notes-off, then close the ports."""
        self.all_notes_off()
        with self._lock:
            self._close_ports_locked()

    def close(self) -> None:
        """Explicit cleanup — call on app exit instead of relying on GC."""
        self.disconnect()

    def drop_ports(self) -> None:
        """Close ports without sending anything (the device is already gone)."""
        with self._lock:
            self._close_ports_locked()

    def _close_ports_locked(self) -> None:
        """Close both ports without sending anything. Caller holds the lock.

        We always drop the references (the port is going regardless), but a
        close that fails for an *unexpected* reason is logged — only the
        expected "port already gone" (OSError) stays quiet.
        """
        if self._output:
            self._close_one(self._output, "output")
            self._output = None
        if self._input:
            self._close_one(self._input, "input")
            self._input = None
        self._port_name = None

    @staticmethod
    def _close_one(port, kind: str) -> None:
        try:
            port.close()
        except _PORT_GONE:
            pass  # device already gone — closing a dead port is a no-op
        except Exception:
            logger.exception("unexpected error closing %s port", kind)

    def _send(self, msg: mido.Message) -> bool:
        """Send one message under the lock.

        Returns True if the message went out. On a port error the backend
        marks itself disconnected and returns False rather than raising, so
        callers on any thread survive a device unplug.
        """
        with self._lock:
            if self._output is None:
                return False
            try:
                self._output.send(msg)
                return True
            except _PORT_GONE:
                # Expected: the device was unplugged mid-send. Drop the ports
                # and report failure quietly — every thread survives the unplug.
                self._close_ports_locked()
                return False
            except Exception:
                # Unexpected: a real bug in the send path. Preserve resilience
                # (disconnect + return False) but make the bug visible, never
                # a silent pass.
                logger.exception("unexpected error sending %s message", msg.type)
                self._close_ports_locked()
                return False

    def send_cc(self, cc: int, value: int) -> bool:
        """Send a Control Change message."""
        value = max(0, min(127, value))
        return self._send(
            mido.Message("control_change", channel=self._channel, control=cc, value=value)
        )

    def send_pitchwheel(self, pitch: int) -> bool:
        """Send a Pitch Bend message (pitch -8192..8191)."""
        pitch = max(-8192, min(8191, pitch))
        return self._send(
            mido.Message("pitchwheel", channel=self._channel, pitch=pitch)
        )

    def send_sysex(self, data: bytes) -> bool:
        """Send a System Exclusive message.

        ``data`` is the full sysex including the 0xF0 start and 0xF7 end bytes;
        mido frames the payload itself, so we hand it the bytes between the
        markers. Like every other send, a port error marks the backend
        disconnected and returns False rather than raising.
        """
        if len(data) < 2 or data[0] != 0xF0 or data[-1] != 0xF7:
            raise ValueError("sysex must start with 0xF0 and end with 0xF7")
        return self._send(mido.Message("sysex", data=list(data[1:-1])))

    def send_note_on(self, note: int, velocity: int = 100) -> bool:
        return self._send(
            mido.Message("note_on", channel=self._channel, note=note, velocity=velocity)
        )

    def send_note_off(self, note: int) -> bool:
        return self._send(
            mido.Message("note_off", channel=self._channel, note=note, velocity=0)
        )

    def all_notes_off(self) -> bool:
        """Panic: CC 123 (All Notes Off) on the active channel."""
        return self.send_cc(ALL_NOTES_OFF_CC, 0)

    def send_start(self) -> bool:
        """Send MIDI Start message (system real-time, no channel)."""
        return self._send(mido.Message("start"))

    def send_stop(self) -> bool:
        """Send MIDI Stop message (system real-time, no channel)."""
        return self._send(mido.Message("stop"))

    def send_continue(self) -> bool:
        """Send MIDI Continue message (system real-time, no channel)."""
        return self._send(mido.Message("continue"))

    def send_clock(self) -> bool:
        """Send one MIDI timing-clock tick (24 per quarter note)."""
        return self._send(mido.Message("clock"))

    def send_program_change(self, program: int, channel: int | None = None) -> bool:
        """Program Change; ``channel`` overrides the synth channel (the S-1
        listens for pattern-select PCs on a dedicated channel, default 16)."""
        ch = self._channel if channel is None else max(0, min(15, channel))
        return self._send(
            mido.Message(
                "program_change", channel=ch, program=max(0, min(127, program))
            )
        )

    def poll_input(self) -> list[mido.Message]:
        """Non-blocking read of any pending input messages.

        This is also the poll loop that drives :meth:`on_incoming` callbacks:
        every incoming Control Change is dispatched to registered listeners as
        ``(param, value)``. With no listeners registered (the default) this is
        pure passthrough, so existing callers see no behavior change.
        """
        if self._input is None:
            return []
        msgs = list(self._input.iter_pending())
        if self._incoming_listeners:
            for msg in msgs:
                if msg.type == "control_change":
                    self._dispatch_incoming(msg.control, msg.value)
        return msgs

    # ── InstrumentBackend conformance (music/CONTRACTS.md §6) ────────────────
    # These thin methods pin MidiBackend to the canonical instrument backend
    # protocol (synth/backend_protocol.py). They are additive: connect/disconnect
    # already match, and the rest wrap existing behavior so engine.py, match/,
    # and logic.py keep importing MidiBackend with its full CC/note/transport API
    # unchanged.

    def send(self, param: int, value: int) -> bool:
        """Protocol ``send``: one parameter change (one CC). Wraps
        :meth:`send_cc`; returns ``True`` if it went out, ``False`` if the
        backend is disconnected or the port vanished."""
        return self.send_cc(param, value)

    def on_incoming(self, cb: Callable[[int, int], None]) -> None:
        """Protocol ``on_incoming``: register ``cb`` for incoming
        ``(param, value)`` CC changes. :meth:`poll_input` invokes it when the
        device reports a knob move."""
        with self._lock:
            self._incoming_listeners.append(cb)

    def _dispatch_incoming(self, param: int, value: int) -> None:
        with self._lock:
            listeners = list(self._incoming_listeners)
        for cb in listeners:
            try:
                cb(param, value)
            except Exception:
                logger.exception("incoming CC listener raised")

    def push_all(self, patch: dict[int, int]) -> None:
        """Protocol ``push_all``: push a whole patch (CC -> value) to the
        device, one CC each.

        NOTE: the authoritative full-state push for the S-1 lives on the engine
        (:meth:`synth.engine.S1Engine.push_all`), which owns app state and marks
        the session SYNCED. This is the thin transport half — a backend-level
        way to blast a patch dict when you already hold one.
        """
        for cc, value in patch.items():
            self.send_cc(cc, value)
