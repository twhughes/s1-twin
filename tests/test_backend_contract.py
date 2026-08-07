"""The instrument backend contract (music/CONTRACTS.md §6 / chassis-spec C9).

One parametrized suite runs against BOTH tier-1 backends:

- ``MidiBackend`` backed by a ``FakeMidiWorld`` (the real S-1 I/O layer, no hardware).
- ``FakeBackend`` (the pure in-memory recorder).

Each must behave identically through the five protocol methods
``connect / disconnect / push_all / send / on_incoming``. A ``_Case`` wraps the
backend with two observers — ``sent_ccs()`` (app -> device) and ``deliver()``
(device -> app) — so every assertion is backend-agnostic.
"""

from __future__ import annotations

import mido
import pytest

from synth.backend_protocol import DifferentiableBackend, InstrumentBackend
from synth.midi_backend import MidiBackend
from tests.fakes import FakeBackend, FakeMidiWorld

PORT = "S-1"


class _Case:
    def __init__(self, backend, port, deliver, sent_ccs):
        self.backend = backend
        self.port = port
        self.deliver = deliver      # deliver(param, value): simulate device -> app
        self.sent_ccs = sent_ccs    # () -> list[(cc, value)] the app pushed out


def _midi_case() -> _Case:
    world = FakeMidiWorld()
    world.add_device(out_name=PORT, in_name=PORT)
    mb = MidiBackend(midi_module=world)

    def deliver(param, value):
        # Incoming CC arrives on the input port; poll_input() drives on_incoming.
        world.inputs[PORT].pending.append(
            mido.Message("control_change", control=param, value=value)
        )
        mb.poll_input()

    def sent_ccs():
        return [
            (m.control, m.value)
            for m in world.outputs[PORT].sent
            if m.type == "control_change"
        ]

    return _Case(mb, PORT, deliver, sent_ccs)


def _fake_case() -> _Case:
    fb = FakeBackend()
    return _Case(fb, PORT, fb.feed, lambda: list(fb.sent))


@pytest.fixture(params=["midi", "fake"])
def case(request) -> _Case:
    return _midi_case() if request.param == "midi" else _fake_case()


class TestInstrumentBackendContract:
    def test_structural_conformance(self, case):
        assert isinstance(case.backend, InstrumentBackend)

    def test_connect_disconnect_state(self, case):
        assert case.backend.connected is False
        case.backend.connect(case.port)
        assert case.backend.connected is True
        case.backend.disconnect()
        assert case.backend.connected is False

    def test_send_returns_false_when_disconnected(self, case):
        assert case.backend.send(74, 100) is False

    def test_send_returns_true_when_connected(self, case):
        case.backend.connect(case.port)
        assert case.backend.send(74, 100) is True
        assert (74, 100) in case.sent_ccs()

    def test_send_after_disconnect_is_false(self, case):
        case.backend.connect(case.port)
        case.backend.disconnect()
        assert case.backend.send(74, 100) is False

    def test_push_all_applies_whole_patch(self, case):
        case.backend.connect(case.port)
        patch = {74: 10, 71: 20, 73: 30}
        case.backend.push_all(patch)
        sent = case.sent_ccs()
        for cc, value in patch.items():
            assert (cc, value) in sent

    def test_on_incoming_delivers_param_value(self, case):
        case.backend.connect(case.port)
        received: list[tuple[int, int]] = []
        case.backend.on_incoming(lambda p, v: received.append((p, v)))
        case.deliver(74, 99)
        assert received == [(74, 99)]

    def test_multiple_incoming_listeners_all_fire(self, case):
        case.backend.connect(case.port)
        a: list = []
        b: list = []
        case.backend.on_incoming(lambda p, v: a.append((p, v)))
        case.backend.on_incoming(lambda p, v: b.append((p, v)))
        case.deliver(71, 42)
        assert a == [(71, 42)] == b


class TestProtocolTiers:
    """Hardware conforms to InstrumentBackend but NOT the differentiable tier —
    its twin stands in for ``render`` (chassis-spec C9 / music §6)."""

    def test_midi_backend_is_instrument_backend(self):
        assert isinstance(MidiBackend(), InstrumentBackend)

    def test_midi_backend_is_not_differentiable(self):
        # No render() — the hardware S-1 has no forward model.
        assert not isinstance(MidiBackend(), DifferentiableBackend)
        assert not hasattr(MidiBackend(), "render")

    def test_fake_backend_is_instrument_backend(self):
        assert isinstance(FakeBackend(), InstrumentBackend)
