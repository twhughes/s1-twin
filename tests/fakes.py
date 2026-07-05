"""Shared hardware fakes: a PortAudio-shaped sounddevice and mido-shaped MIDI
ports, so the monitor, sync engine, and server are testable with zero hardware.
"""

from __future__ import annotations

import numpy as np

# ──────────────────────────────────────────────────────────────
# Fake sounddevice
# ──────────────────────────────────────────────────────────────

class FakeStream:
    """Stands in for sd.InputStream / sd.OutputStream.

    Callbacks fire only when the test pumps them, so tests control time.
    """

    def __init__(self, samplerate=None, blocksize=None, dtype=None,
                 channels=1, device=None, callback=None):
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.channels = channels
        self.device = device
        self.callback = callback
        self.active = False
        self.closed = False

    def start(self):
        self.active = True

    def stop(self):
        self.active = False

    def close(self):
        self.closed = True


class _Default:
    def __init__(self):
        self.device = (-1, -1)  # (input, output)


class FakeSounddevice:
    """Mimics the sounddevice module surface the app uses."""

    def __init__(self, devices: list[dict] | None = None):
        # Each device: {"name", "max_input_channels", "max_output_channels",
        #               "default_samplerate"}
        self.devices = devices if devices is not None else [
            {"name": "MacBook Pro Microphone", "max_input_channels": 1,
             "max_output_channels": 0, "default_samplerate": 48000.0},
            {"name": "MacBook Pro Speakers", "max_input_channels": 0,
             "max_output_channels": 2, "default_samplerate": 48000.0},
        ]
        self.default = _Default()
        self.default.device = (0, 1)
        self.input_streams: list[FakeStream] = []
        self.output_streams: list[FakeStream] = []
        self.reinitialized = 0

    # -- module surface ------------------------------------------------------
    def query_devices(self, device=None, kind=None):
        if device is None:
            return list(self.devices)
        if isinstance(device, str):
            for d in self.devices:
                if device.lower() in d["name"].lower():
                    return d
            raise ValueError(f"no device matching {device!r}")
        return self.devices[device]

    def InputStream(self, **kwargs):  # noqa: N802 — mimics sounddevice API
        s = FakeStream(**kwargs)
        self.input_streams.append(s)
        return s

    def OutputStream(self, **kwargs):  # noqa: N802 — mimics sounddevice API
        s = FakeStream(**kwargs)
        self.output_streams.append(s)
        return s

    def _terminate(self):
        self.reinitialized += 1

    def _initialize(self):
        pass

    # -- test helpers --------------------------------------------------------
    def add_s1(self):
        """Hot-plug the S-1's USB audio device."""
        self.devices.append({"name": "S-1", "max_input_channels": 2,
                             "max_output_channels": 0, "default_samplerate": 44100.0})
        return len(self.devices) - 1

    def remove_s1(self):
        self.devices = [d for d in self.devices if d["name"] != "S-1"]

    def pump(self, samples: np.ndarray, frames_out: int | None = None) -> np.ndarray:
        """Push samples through the live input stream, then drain the output.

        Returns what the output stream wrote (mono column stacked to shape
        (n, channels)).
        """
        ins = self.input_streams[-1]
        x = np.asarray(samples, dtype=np.float32).reshape(-1, 1)
        ins.callback(x, len(x), None, None)
        outs = self.output_streams[-1]
        n = frames_out if frames_out is not None else len(x)
        buf = np.zeros((n, outs.channels), dtype=np.float32)
        outs.callback(buf, n, None, None)
        return buf


# ──────────────────────────────────────────────────────────────
# Fake mido ports
# ──────────────────────────────────────────────────────────────

class FakeMidiPort:
    """Stands in for a mido input or output port."""

    def __init__(self, name: str):
        self.name = name
        self.sent: list = []       # messages sent out (for output ports)
        self.pending: list = []    # messages waiting to be read (for input ports)
        self.closed = False
        self.fail_next_send = False

    # output surface
    def send(self, msg):
        if self.fail_next_send or self.closed:
            raise OSError("port gone")
        self.sent.append(msg)

    # input surface
    def iter_pending(self):
        msgs, self.pending = self.pending, []
        yield from msgs

    def close(self):
        self.closed = True


class FakeMidiWorld:
    """A patchable stand-in for the mido module's port discovery/opening.

    Tests add and remove named ports; opening a missing port raises like the
    real backend does.
    """

    def __init__(self):
        self.outputs: dict[str, FakeMidiPort] = {}
        self.inputs: dict[str, FakeMidiPort] = {}

    def add_device(self, out_name: str | None = None, in_name: str | None = None):
        if out_name:
            self.outputs.setdefault(out_name, FakeMidiPort(out_name))
        if in_name:
            self.inputs.setdefault(in_name, FakeMidiPort(in_name))

    def remove_device(self, *names: str):
        for name in names:
            self.outputs.pop(name, None)
            self.inputs.pop(name, None)

    # mido-shaped surface
    def get_output_names(self):
        return list(self.outputs)

    def get_input_names(self):
        return list(self.inputs)

    def open_output(self, name):
        if name not in self.outputs:
            raise OSError(f"unknown port {name!r}")
        port = self.outputs[name]
        port.closed = False
        return port

    def open_input(self, name):
        if name not in self.inputs:
            raise OSError(f"unknown port {name!r}")
        port = self.inputs[name]
        port.closed = False
        return port
