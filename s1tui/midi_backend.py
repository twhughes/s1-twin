"""MIDI I/O layer for the Roland S-1."""

from __future__ import annotations

import mido


class MidiBackend:
    """Manages MIDI connection to the S-1."""

    def __init__(self) -> None:
        self._output: mido.ports.BaseOutput | None = None
        self._input: mido.ports.BaseInput | None = None
        self._port_name: str | None = None
        self._channel: int = 0  # 0-indexed

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
        if self._output:
            self._output.close()
            self._output = None
        if self._input:
            self._input.close()
            self._input = None
        self._port_name = None

    def send_cc(self, cc: int, value: int) -> None:
        """Send a Control Change message."""
        if self._output is None:
            return
        value = max(0, min(127, value))
        msg = mido.Message("control_change", channel=self._channel, control=cc, value=value)
        self._output.send(msg)

    def send_note_on(self, note: int, velocity: int = 100) -> None:
        if self._output is None:
            return
        msg = mido.Message("note_on", channel=self._channel, note=note, velocity=velocity)
        self._output.send(msg)

    def send_note_off(self, note: int) -> None:
        if self._output is None:
            return
        msg = mido.Message("note_off", channel=self._channel, note=note, velocity=0)
        self._output.send(msg)

    def send_start(self) -> None:
        """Send MIDI Start message (system real-time, no channel)."""
        if self._output is None:
            return
        self._output.send(mido.Message("start"))

    def send_stop(self) -> None:
        """Send MIDI Stop message (system real-time, no channel)."""
        if self._output is None:
            return
        self._output.send(mido.Message("stop"))

    def send_continue(self) -> None:
        """Send MIDI Continue message (system real-time, no channel)."""
        if self._output is None:
            return
        self._output.send(mido.Message("continue"))

    def send_program_change(self, program: int) -> None:
        if self._output is None:
            return
        msg = mido.Message("program_change", channel=self._channel, program=max(0, min(127, program)))
        self._output.send(msg)

    def poll_input(self) -> list[mido.Message]:
        """Non-blocking read of any pending input messages."""
        if self._input is None:
            return []
        msgs = []
        for msg in self._input.iter_pending():
            msgs.append(msg)
        return msgs

    def __del__(self) -> None:
        self.disconnect()
