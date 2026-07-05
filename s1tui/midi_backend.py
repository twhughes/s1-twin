"""MIDI I/O layer for the Roland S-1."""

from __future__ import annotations

import threading

import mido

ALL_NOTES_OFF_CC = 123


class MidiBackend:
    """Manages MIDI connection to the S-1.

    All sends are serialized through one lock — the UI thread, the sequencer
    thread, and worker threads share this backend. A send that fails (device
    unplugged) marks the backend disconnected instead of raising.
    """

    def __init__(self) -> None:
        self._output: mido.ports.BaseOutput | None = None
        self._input: mido.ports.BaseInput | None = None
        self._port_name: str | None = None
        self._channel: int = 0  # 0-indexed
        self._lock = threading.RLock()

    @staticmethod
    def list_output_ports() -> list[str]:
        return mido.get_output_names()

    @staticmethod
    def list_input_ports() -> list[str]:
        return mido.get_input_names()

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
            self._output = mido.open_output(port_name)
            self._port_name = port_name
            # Try to open matching input for receiving CC feedback
            # Output ports are "X MIDI IN", input ports are "X MIDI OUT"
            inputs = self.list_input_ports()
            # First try exact match
            if port_name in inputs:
                try:
                    self._input = mido.open_input(port_name)
                except Exception:
                    pass
            else:
                # Try to find corresponding input (e.g., "S-1 MIDI IN" -> "S-1 MIDI OUT")
                base_name = port_name.replace(" MIDI IN", "").replace(" IN", "")
                for inp in inputs:
                    if base_name in inp:
                        try:
                            self._input = mido.open_input(inp)
                            break
                        except Exception:
                            pass

    def disconnect(self) -> None:
        """Flush hung notes with all-notes-off, then close the ports."""
        self.all_notes_off()
        with self._lock:
            self._close_ports_locked()

    def close(self) -> None:
        """Explicit cleanup — call on app exit instead of relying on GC."""
        self.disconnect()

    def _close_ports_locked(self) -> None:
        """Close both ports without sending anything. Caller holds the lock."""
        if self._output:
            try:
                self._output.close()
            except Exception:
                pass
            self._output = None
        if self._input:
            try:
                self._input.close()
            except Exception:
                pass
            self._input = None
        self._port_name = None

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
            except Exception:
                self._close_ports_locked()
                return False

    def send_cc(self, cc: int, value: int) -> bool:
        """Send a Control Change message."""
        value = max(0, min(127, value))
        return self._send(
            mido.Message("control_change", channel=self._channel, control=cc, value=value)
        )

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

    def send_program_change(self, program: int) -> bool:
        return self._send(
            mido.Message(
                "program_change", channel=self._channel, program=max(0, min(127, program))
            )
        )

    def poll_input(self) -> list[mido.Message]:
        """Non-blocking read of any pending input messages."""
        if self._input is None:
            return []
        msgs = []
        for msg in self._input.iter_pending():
            msgs.append(msg)
        return msgs
