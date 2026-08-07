"""Tests for M4 — headless Logic transport over MMC on the HQ Clock bus.

The contract is the exact MMC byte sequence for each transport verb, pinned
against a fake mido world (no hardware). See chassis-spec C4/C5/M4.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import synth.engine as engine_module
import synth.web.server as server_mod
from synth import logic
from synth.engine import S1Engine
from synth.midi_backend import MidiBackend
from synth.web.server import app
from tests.fakes import FakeMidiWorld

BASE_URL = "http://127.0.0.1:8766"

# The pinned contract: full MMC sysex, F0 ... F7, broadcast device 0x7F.
EXPECTED = {
    "stop": [0xF0, 0x7F, 0x7F, 0x06, 0x01, 0xF7],
    "play": [0xF0, 0x7F, 0x7F, 0x06, 0x03, 0xF7],
    "record": [0xF0, 0x7F, 0x7F, 0x06, 0x06, 0xF7],
}


# ── the MMC bytes (the real contract) ───────────────────────────
class TestMmcBytes:
    def test_constants_are_pinned(self):
        assert list(logic.MMC_STOP) == EXPECTED["stop"]
        assert list(logic.MMC_PLAY) == EXPECTED["play"]
        assert list(logic.MMC_RECORD) == EXPECTED["record"]

    @pytest.mark.parametrize("action", ["play", "stop", "record"])
    def test_send_transport_sends_exact_bytes(self, action):
        world = FakeMidiWorld()
        world.add_device(out_name="HQ Clock")
        data = logic.send_transport(action, midi_module=world)
        assert list(data) == EXPECTED[action]

        sent = world.outputs["HQ Clock"].sent
        assert len(sent) == 1
        assert sent[0].type == "sysex"
        # mido reframes the payload — .bytes() gives the full F0..F7 back.
        assert list(sent[0].bytes()) == EXPECTED[action]

    def test_play_stop_record_helpers(self):
        world = FakeMidiWorld()
        world.add_device(out_name="HQ Clock")
        assert list(logic.play(midi_module=world)) == EXPECTED["play"]
        assert list(logic.stop(midi_module=world)) == EXPECTED["stop"]
        assert list(logic.record(midi_module=world)) == EXPECTED["record"]

    def test_custom_port(self):
        world = FakeMidiWorld()
        world.add_device(out_name="Some Other Bus")
        logic.send_transport("play", port_name="Some Other Bus", midi_module=world)
        assert len(world.outputs["Some Other Bus"].sent) == 1

    def test_no_note_flush_on_the_clock_bus(self):
        """Closing the transport must not leak an all-notes-off CC onto the bus."""
        world = FakeMidiWorld()
        world.add_device(out_name="HQ Clock")
        logic.send_transport("stop", midi_module=world)
        sent = world.outputs["HQ Clock"].sent
        assert all(m.type == "sysex" for m in sent)


# ── missing bus fails loud (C5), never silent ───────────────────
class TestMissingBus:
    def test_missing_port_raises_with_pointer(self):
        world = FakeMidiWorld()  # no HQ Clock
        with pytest.raises(logic.LogicTransportError) as exc:
            logic.send_transport("play", midi_module=world)
        msg = str(exc.value)
        assert "HQ Clock" in msg
        assert "Audio MIDI Setup" in msg  # the one-line setup pointer

    def test_unknown_action_raises(self):
        world = FakeMidiWorld()
        world.add_device(out_name="HQ Clock")
        with pytest.raises(logic.LogicTransportError):
            logic.send_transport("rewind", midi_module=world)


# ── send_sysex on the backend (framing + swallow-on-error) ──────
class TestSendSysex:
    def test_framing_validated(self):
        mb = MidiBackend()
        with pytest.raises(ValueError):
            mb.send_sysex(bytes([0x7F, 0x06, 0x01]))  # no F0/F7 framing

    def test_swallows_when_disconnected(self):
        mb = MidiBackend()
        assert mb.send_sysex(logic.MMC_PLAY) is False  # no port, no raise

    def test_sends_framed_payload(self):
        world = FakeMidiWorld()
        world.add_device(out_name="HQ Clock")
        mb = MidiBackend(midi_module=world)
        mb.connect("HQ Clock")
        assert mb.send_sysex(logic.MMC_PLAY) is True
        sent = world.outputs["HQ Clock"].sent
        assert list(sent[-1].bytes()) == EXPECTED["play"]


# ── the REST surface ────────────────────────────────────────────
@pytest.fixture
def world():
    return FakeMidiWorld()


@pytest.fixture
def engine(world, monkeypatch):
    e = S1Engine(midi_module=world, audio_auto=False, poll_interval=999)
    monkeypatch.setattr(engine_module, "ENGINE", e)
    server_mod._match_holder.clear()
    yield e
    e.stop()


@pytest.fixture
def client(engine):
    return TestClient(app, base_url=BASE_URL)


class TestLogicRoute:
    @pytest.mark.parametrize("action", ["play", "stop", "record"])
    def test_transport_route_sends_bytes(self, client, world, action):
        world.add_device(out_name="HQ Clock")
        r = client.post("/api/logic/transport", json={"action": action})
        assert r.status_code == 200
        body = r.json()
        assert body["action"] == action
        assert body["port"] == "HQ Clock"
        assert body["sent"] == EXPECTED[action]
        assert list(world.outputs["HQ Clock"].sent[0].bytes()) == EXPECTED[action]

    def test_missing_bus_is_503_not_silent(self, client, world):
        r = client.post("/api/logic/transport", json={"action": "play"})
        assert r.status_code == 503
        assert "HQ Clock" in r.json()["detail"]

    def test_bad_action_rejected(self, client, world):
        world.add_device(out_name="HQ Clock")
        r = client.post("/api/logic/transport", json={"action": "rewind"})
        assert r.status_code == 422  # Literal validation
