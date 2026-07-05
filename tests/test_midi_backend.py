"""Tests for midi_backend.py — MIDI I/O layer."""

import threading
from unittest.mock import MagicMock, patch

import mido

from s1tui.midi_backend import ALL_NOTES_OFF_CC, MidiBackend


class TestMidiBackendInit:
    def test_not_connected_initially(self):
        mb = MidiBackend()
        assert not mb.connected
        assert mb.port_name is None

    def test_default_channel(self):
        mb = MidiBackend()
        assert mb.channel == 0

    def test_channel_setter(self):
        mb = MidiBackend()
        mb.channel = 5
        assert mb.channel == 5

    def test_channel_clamp_high(self):
        mb = MidiBackend()
        mb.channel = 20
        assert mb.channel == 15

    def test_channel_clamp_low(self):
        mb = MidiBackend()
        mb.channel = -5
        assert mb.channel == 0


class TestMidiBackendSend:
    def test_send_cc_when_not_connected(self):
        mb = MidiBackend()
        # Should not raise
        mb.send_cc(74, 100)

    def test_send_cc_clamps_value(self):
        mb = MidiBackend()
        mb._output = MagicMock()
        mb.send_cc(74, 200)
        msg = mb._output.send.call_args[0][0]
        assert msg.value == 127

    def test_send_cc_clamps_negative(self):
        mb = MidiBackend()
        mb._output = MagicMock()
        mb.send_cc(74, -10)
        msg = mb._output.send.call_args[0][0]
        assert msg.value == 0

    def test_send_cc_correct_channel(self):
        mb = MidiBackend()
        mb._output = MagicMock()
        mb._channel = 2
        mb.send_cc(74, 100)
        msg = mb._output.send.call_args[0][0]
        assert msg.channel == 2
        assert msg.control == 74
        assert msg.value == 100

    def test_send_note_on(self):
        mb = MidiBackend()
        mb._output = MagicMock()
        mb.send_note_on(60, 100)
        msg = mb._output.send.call_args[0][0]
        assert msg.type == "note_on"
        assert msg.note == 60
        assert msg.velocity == 100

    def test_send_note_off(self):
        mb = MidiBackend()
        mb._output = MagicMock()
        mb.send_note_off(60)
        msg = mb._output.send.call_args[0][0]
        assert msg.type == "note_off"
        assert msg.note == 60

    def test_send_note_on_when_not_connected(self):
        mb = MidiBackend()
        mb.send_note_on(60, 100)  # Should not raise

    def test_send_program_change(self):
        mb = MidiBackend()
        mb._output = MagicMock()
        mb.send_program_change(42)
        msg = mb._output.send.call_args[0][0]
        assert msg.type == "program_change"
        assert msg.program == 42

    def test_send_program_change_clamps(self):
        mb = MidiBackend()
        mb._output = MagicMock()
        mb.send_program_change(200)
        msg = mb._output.send.call_args[0][0]
        assert msg.program == 127


class TestMidiBackendPollInput:
    def test_poll_when_not_connected(self):
        mb = MidiBackend()
        assert mb.poll_input() == []

    def test_poll_returns_messages(self):
        mb = MidiBackend()
        mock_input = MagicMock()
        msg1 = mido.Message("control_change", control=74, value=50)
        msg2 = mido.Message("control_change", control=73, value=60)
        mock_input.iter_pending.return_value = [msg1, msg2]
        mb._input = mock_input
        msgs = mb.poll_input()
        assert len(msgs) == 2
        assert msgs[0].control == 74
        assert msgs[1].control == 73


class TestMidiBackendConnect:
    def test_disconnect_clears_state(self):
        mb = MidiBackend()
        mb._output = MagicMock()
        mb._input = MagicMock()
        mb._port_name = "Test Port"
        mb.disconnect()
        assert not mb.connected
        assert mb.port_name is None

    def test_disconnect_sends_all_notes_off_before_closing(self):
        mb = MidiBackend()
        out = MagicMock()
        mb._output = out
        mb.disconnect()
        msg = out.send.call_args[0][0]
        assert msg.type == "control_change"
        assert msg.control == ALL_NOTES_OFF_CC
        out.close.assert_called_once()

    def test_close_is_explicit_disconnect(self):
        mb = MidiBackend()
        out = MagicMock()
        mb._output = out
        mb.close()
        assert not mb.connected
        out.close.assert_called_once()


class TestMidiBackendThreadSafety:
    def test_concurrent_sends_are_serialized(self):
        """Sends from many threads must all pass through the lock — a port
        that detects overlapping calls proves serialization."""
        mb = MidiBackend()
        in_send = threading.Semaphore(1)
        overlaps = []

        class SlowPort:
            def send(self, msg):
                if not in_send.acquire(blocking=False):
                    overlaps.append(msg)
                    return
                try:
                    for _ in range(50):
                        pass
                finally:
                    in_send.release()

            def close(self):
                pass

        mb._output = SlowPort()

        def worker(i):
            for _ in range(100):
                mb.send_cc(74, i)
                mb.send_note_on(60 + i)
                mb.send_note_off(60 + i)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert overlaps == []

    def test_send_failure_marks_disconnected(self):
        mb = MidiBackend()
        out = MagicMock()
        out.send.side_effect = OSError("device unplugged")
        mb._output = out
        mb._port_name = "S-1"
        assert mb.send_cc(74, 64) is False  # no raise
        assert not mb.connected
        assert mb.port_name is None
        # Subsequent sends are silent no-ops
        assert mb.send_note_on(60) is False

    def test_send_returns_true_on_success(self):
        mb = MidiBackend()
        mb._output = MagicMock()
        assert mb.send_cc(74, 64) is True
        assert mb.send_note_on(60) is True

    def test_list_ports_static(self):
        with patch("mido.get_output_names", return_value=["Port A", "Port B"]):
            ports = MidiBackend.list_output_ports()
            assert ports == ["Port A", "Port B"]

        with patch("mido.get_input_names", return_value=["Port C"]):
            ports = MidiBackend.list_input_ports()
            assert ports == ["Port C"]
