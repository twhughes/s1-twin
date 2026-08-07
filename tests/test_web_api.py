"""Tests for the cockpit API — schema-driven UI contract, params, banks,
sequencer, export, monitor, WebSocket. All against a fake MIDI world."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import synth.engine as engine_module
import synth.patches as patches_mod
import synth.prm as prm_mod
import synth.sequences as sequences_mod
import synth.web.server as server_mod
from synth.engine import S1Engine
from synth.prm import PrmFile
from synth.schema import S1_PARAMS
from synth.web.facade import EngineFacade
from synth.web.server import app
from tests.fakes import FakeMidiWorld

BASE_URL = "http://127.0.0.1:8766"


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
    # base_url must match the server's Host allowlist (anti-DNS-rebinding).
    # No context manager: the lifespan (real engine watcher) stays off; sync
    # ticks run manually via engine._tick().
    return TestClient(app, base_url=BASE_URL)


@pytest.fixture
def banks(tmp_path, monkeypatch):
    monkeypatch.setattr(patches_mod, "PATCH_DIR", tmp_path / "patches")
    monkeypatch.setattr(sequences_mod, "SEQUENCE_DIR", tmp_path / "sequences")
    return tmp_path


def connect_s1(engine, world):
    world.add_device(out_name="S-1 MIDI IN", in_name="S-1 MIDI OUT")
    engine._tick()
    return world.outputs["S-1 MIDI IN"]


# ── G3: the UI contract — every param, exactly one control ──
class TestSchemaContract:
    def test_every_param_exactly_once(self, client):
        """The UI is generated from this payload: one entry per schema param
        across faceplate sections + settings menu + MIDI — no dupes, none
        missing — means exactly one control per param in the served UI."""
        schema = client.get("/api/schema").json()
        ccs = [p["cc"] for s in schema["sections"] for p in s["params"]]
        ccs += [p["cc"] for p in schema["menu"]]
        ccs += [p["cc"] for p in schema["midi"]]
        assert len(ccs) == len(set(ccs)), "a param appears in two groups"
        assert set(ccs) == {p.cc for p in S1_PARAMS}

    def test_sections_are_faceplate_order(self, client):
        schema = client.get("/api/schema").json()
        assert [s["name"] for s in schema["sections"]] == [
            "LFO", "OSC", "FILTER", "AMP", "ENV", "EFX", "CONTROLLER",
        ]

    def test_params_carry_control_metadata(self, client):
        schema = client.get("/api/schema").json()
        by_cc = {p["cc"]: p for s in schema["sections"] for p in s["params"]}
        wf = by_cc[12]
        assert wf["type"] == "discrete"
        assert wf["labels"]["0"] == "Saw"
        assert wf["max"] == 5
        assert by_cc[74]["type"] == "continuous"
        assert by_cc[65]["type"] == "switch"

    def test_prm_tier_served(self, client):
        schema = client.get("/api/schema").json()
        keys = {p["key"] for p in schema["prm"]}
        assert "REVERB_TYPE" in keys and "OSC_CHOP_PWM" in keys

    def test_no_param_hand_coded_in_html(self, client):
        """The cockpit HTML must not hard-code any parameter control — a
        schema addition appears in the UI for free."""
        html = (server_mod.STATIC_DIR / "index.html").read_text()
        assert "data-cc" not in html
        for p in S1_PARAMS:
            assert f">{p.name}<" not in html, p.name

    def test_schema_values_track_live_state(self, client, engine):
        engine.set_param(74, 33)
        schema = client.get("/api/schema").json()
        cutoff = next(p for s in schema["sections"] for p in s["params"] if p["cc"] == 74)
        assert cutoff["value"] == 33


# ── G3: params — control change fires the CC ────────────────
class TestParams:
    def test_put_param_fires_cc(self, client, engine, world):
        out = connect_s1(engine, world)
        r = client.put("/api/params/74", json={"value": 90})
        assert r.status_code == 200
        cc = [m for m in out.sent if m.type == "control_change" and m.control == 74]
        assert cc[-1].value == 90

    def test_put_param_clamps_to_schema(self, client, engine):
        r = client.put("/api/params/12", json={"value": 500})
        assert r.json()["value"] == 5  # waveform: 6 options

    def test_unknown_cc_404(self, client):
        assert client.put("/api/params/9", json={"value": 1}).status_code == 404
        assert client.get("/api/params/9").status_code == 404

    def test_bulk_set(self, client, engine, world):
        out = connect_s1(engine, world)
        r = client.post("/api/params", json={"values": {"74": 10, "71": 20, "9": 5}})
        body = r.json()
        assert body["applied"] == {"74": 10, "71": 20}
        assert body["unknown"] == [9]
        sent = {m.control: m.value for m in out.sent if m.type == "control_change"}
        assert sent[74] == 10 and sent[71] == 20

    def test_state_snapshot(self, client, engine):
        engine.set_param(71, 55)
        r = client.get("/api/state").json()
        assert r["params"]["71"] == 55
        assert r["status"]["sync"] == "disconnected"

    def test_status_includes_studio_flag(self, client):
        assert "studio" in client.get("/api/status").json()

    def test_push_all_route_is_the_explicit_sync(self, client, engine, world):
        """POST /api/push-all is the only thing that pushes state at the S-1.

        Connect leaves the device untouched (listen-only); this route is how
        the cockpit's "Push to S-1" button makes the app win.
        """
        out = connect_s1(engine, world)
        assert client.get("/api/status").json()["sync"] == "listening"
        assert [m for m in out.sent if m.type == "control_change"] == []

        body = client.post("/api/push-all").json()
        assert body["pushed"] == len(S1_PARAMS)
        assert body["sync"] == "synced"
        sent = {m.control for m in out.sent if m.type == "control_change"}
        assert sent == {p.cc for p in S1_PARAMS}
        assert client.get("/api/status").json()["sync"] == "synced"


# ── G3: patch bank round-trip through the browser API ───────
class TestPatchBank:
    def test_save_load_roundtrip(self, client, engine, banks):
        client.put("/api/params/74", json={"value": 42})
        client.put("/api/params/71", json={"value": 99})
        assert client.post("/api/patches", json={"name": "dreamy"}).status_code == 200

        client.put("/api/params/74", json={"value": 0})
        client.put("/api/params/71", json={"value": 0})

        r = client.post("/api/patches/dreamy/load")
        assert r.status_code == 200
        state = client.get("/api/state").json()["params"]
        assert state["74"] == 42 and state["71"] == 99

    def test_saved_patch_readable(self, client, engine, banks):
        client.put("/api/params/74", json={"value": 42})
        client.post("/api/patches", json={"name": "p"})
        r = client.get("/api/patches/p").json()
        assert r["values"]["74"] == 42

    def test_overwrite_guard(self, client, engine, banks):
        client.post("/api/patches", json={"name": "p"})
        assert client.post("/api/patches", json={"name": "p"}).status_code == 409
        assert client.post("/api/patches", json={"name": "p", "overwrite": True}).status_code == 200

    def test_delete(self, client, engine, banks):
        client.post("/api/patches", json={"name": "p"})
        assert client.delete("/api/patches/p").status_code == 200
        assert client.delete("/api/patches/p").status_code == 404

    def test_path_traversal_rejected(self, client, engine, banks):
        assert client.post("/api/patches", json={"name": "../evil"}).status_code == 400

    def test_play_loads_patch(self, client, engine, world, banks):
        connect_s1(engine, world)
        client.put("/api/params/74", json={"value": 7})
        client.post("/api/patches", json={"name": "p"})
        client.put("/api/params/74", json={"value": 99})
        assert client.post("/api/patches/p/play").status_code == 200
        assert engine.params.get(74) == 7


# ── G7: sequence CRUD + transport round-trip ─────────────────
class TestSequencer:
    SEQ = {
        "steps": 8, "bpm": 140.0, "step_resolution": "1/16",
        "notes": [
            {"step": 0, "pitch": 60, "velocity": 100, "duration": 2},
            {"step": 4, "pitch": 67, "velocity": 90, "duration": 1},
        ],
    }

    def test_put_get_roundtrip(self, client, engine):
        r = client.put("/api/sequence", json=self.SEQ)
        assert r.status_code == 200
        got = client.get("/api/sequence").json()
        assert got["steps"] == 8 and got["bpm"] == 140.0
        assert len(got["notes"]) == 2
        assert got["poly_warnings"] == []

    def test_steps_over_64_rejected(self, client, engine):
        assert client.put("/api/sequence", json={**self.SEQ, "steps": 65}).status_code == 422

    def test_poly_warning_over_4_notes(self, client, engine):
        notes = [{"step": 0, "pitch": 50 + i, "velocity": 100, "duration": 1} for i in range(5)]
        r = client.put("/api/sequence", json={**self.SEQ, "notes": notes})
        assert r.json()["poly_warnings"] == [0]

    def test_transport_play_stop(self, client, engine, world):
        import time

        connect_s1(engine, world)
        out = world.outputs["S-1 MIDI IN"]
        client.put("/api/sequence", json={**self.SEQ, "bpm": 600.0})
        r = client.post("/api/transport", json={"action": "play"})
        assert r.json()["playing"] is True
        time.sleep(0.15)
        r = client.post("/api/transport", json={"action": "stop"})
        assert r.json()["playing"] is False
        types = {m.type for m in out.sent}
        assert "start" in types and "note_on" in types and "clock" in types and "stop" in types

    def test_transport_settings(self, client, engine):
        r = client.put("/api/transport", json={"bpm": 98.0, "gate": 0.5, "shuffle": 0.2,
                                               "probability": 0.9, "clock_enabled": False})
        body = r.json()
        assert body["bpm"] == 98.0
        assert body["gate"] == 0.5
        assert body["clock_enabled"] is False
        assert engine.sequencer.shuffle == 0.2

    def test_bank_roundtrip(self, client, engine, banks):
        client.put("/api/sequence", json=self.SEQ)
        assert client.post("/api/sequences", json={"name": "line"}).status_code == 200
        client.put("/api/sequence", json={**self.SEQ, "notes": []})
        r = client.post("/api/sequences/line/load")
        assert len(r.json()["notes"]) == 2
        assert [s["name"] for s in client.get("/api/sequences").json()] == ["line"]
        assert client.delete("/api/sequences/line").status_code == 200
        assert client.post("/api/sequences/line/load").status_code == 404

    def test_live_edit_does_not_stop_playback(self, client, engine, world):
        connect_s1(engine, world)
        client.put("/api/sequence", json={**self.SEQ, "bpm": 600.0})
        client.post("/api/transport", json={"action": "play"})
        client.put("/api/sequence", json={**self.SEQ, "bpm": 600.0,
                                          "notes": self.SEQ["notes"][:1]})
        assert engine.sequencer.playing
        client.post("/api/transport", json={"action": "stop"})


# ── G6/G7: notes + device pattern ────────────────────────────
class TestNotesAndPattern:
    def test_note_on_off(self, client, engine, world):
        out = connect_s1(engine, world)
        client.post("/api/notes", json={"note": 60, "velocity": 111, "on": True})
        client.post("/api/notes", json={"note": 60, "on": False})
        notes = [m for m in out.sent if m.type in ("note_on", "note_off")]
        assert [(m.type, m.note) for m in notes] == [("note_on", 60), ("note_off", 60)]
        assert notes[0].velocity == 111

    def test_all_notes_off(self, client, engine, world):
        out = connect_s1(engine, world)
        client.post("/api/notes/all-off")
        assert any(m.type == "control_change" and m.control == 123 for m in out.sent)

    def test_note_validation(self, client, engine):
        assert client.post("/api/notes", json={"note": 200}).status_code == 422

    def test_pattern_select(self, client, engine, world):
        out = connect_s1(engine, world)
        r = client.post("/api/device/pattern", json={"bank": 2, "slot": 5})
        assert r.json()["program"] == 20
        pc = [m for m in out.sent if m.type == "program_change"][-1]
        assert pc.program == 20 and pc.channel == 15

    def test_pattern_validation(self, client, engine):
        assert client.post("/api/device/pattern", json={"bank": 5, "slot": 1}).status_code == 422

    def test_midi_ports_listed(self, client, engine, world):
        world.add_device(out_name="S-1 MIDI IN", in_name="S-1 MIDI OUT")
        r = client.get("/api/midi/ports").json()
        assert "S-1 MIDI IN" in r["output"]


# ── G5: monitor endpoints ────────────────────────────────────
class TestMonitor:
    def test_mute_toggle(self, client, engine):
        r = client.post("/api/monitor/mute", json={"muted": True})
        assert r.json()["muted"] is True
        assert engine.monitor.muted is True
        r = client.post("/api/monitor/mute", json={"muted": False})
        assert r.json()["muted"] is False

    def test_stop_disables_auto(self, client, engine):
        client.post("/api/monitor/stop")
        assert engine.audio_auto is False

    def test_gain(self, client, engine):
        client.post("/api/monitor/gain", json={"gain": 3.5})
        assert engine.monitor.gain == 3.5

    def test_status(self, client, engine):
        r = client.get("/api/monitor").json()
        assert r["running"] is False

    def test_scope_zeros_while_stopped(self, client, engine):
        r = client.get("/api/monitor/scope").json()
        assert r["running"] is False
        assert r["points"] == [0.0] * 128

    def test_scope_points_clamped(self, client, engine):
        assert len(client.get("/api/monitor/scope?points=9999").json()["points"]) == 512
        assert len(client.get("/api/monitor/scope?points=1").json()["points"]) == 16


# ── G8: export endpoints ─────────────────────────────────────
class TestExport:
    def test_download_prm(self, client, engine):
        client.put("/api/params/74", json={"value": 88})
        client.put("/api/sequence", json=TestSequencer.SEQ)
        r = client.get("/api/export/prm?bank=2&slot=5")
        assert r.status_code == 200
        assert 'filename="S1_PTN2-05.PRM"' in r.headers["content-disposition"]
        prm = PrmFile.parse(r.content.decode("ascii"))
        assert prm.to_cc_values()[74] == 88
        assert prm.to_sequence().steps == 8

    def test_download_validation(self, client, engine):
        assert client.get("/api/export/prm?bank=9&slot=1").status_code == 400

    def test_device_status_unmounted(self, client, engine, tmp_path, monkeypatch):
        monkeypatch.setattr(prm_mod, "VOLUMES_DIR", tmp_path)
        r = client.get("/api/export/device").json()
        assert r["mounted"] is False

    def test_write_unmounted_409(self, client, engine, tmp_path, monkeypatch):
        monkeypatch.setattr(prm_mod, "VOLUMES_DIR", tmp_path)
        assert client.post("/api/export/device", json={"bank": 1, "slot": 1}).status_code == 409

    def test_write_to_mounted_volume(self, client, engine, tmp_path, monkeypatch):
        monkeypatch.setattr(prm_mod, "VOLUMES_DIR", tmp_path)
        (tmp_path / "S-1" / "RESTORE").mkdir(parents=True)
        client.put("/api/params/74", json={"value": 61})
        r = client.post("/api/export/device", json={"bank": 3, "slot": 12})
        assert r.status_code == 200
        written = tmp_path / "S-1" / "RESTORE" / "S1_PTN3-12.PRM"
        assert written.exists()
        assert PrmFile.load(written).to_cc_values()[74] == 61
        status = client.get("/api/export/device").json()
        assert status["mounted"] is True


# ── the librarian: .PRM import ───────────────────────────────
def make_pattern_file(directory, name="S1_PTN1-01.PRM", cutoff=90, bpm=140.0):
    """A device-plausible pattern: template dump + a tweak + one note."""
    prm = prm_mod.load_template()
    prm.set("VCF_CUTOFF", prm_mod.cc_to_prm(74, cutoff))
    prm.set("TEMPO", round(bpm * 100))
    step = prm_mod.PrmStep()
    step.notes[0], step.velocities[0], step.lengths[0] = 60, 101, 24
    prm.set_step(1, step)
    directory.mkdir(parents=True, exist_ok=True)
    return prm.save(directory / name)


class TestImport:
    @pytest.fixture(autouse=True)
    def dirs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(prm_mod, "VOLUMES_DIR", tmp_path / "volumes")
        monkeypatch.setattr(prm_mod, "BACKUPS_DIR", tmp_path / "backups")
        (tmp_path / "volumes").mkdir()
        return tmp_path

    def mount_s1(self, tmp_path):
        vol = tmp_path / "volumes" / "S-1"
        (vol / "RESTORE").mkdir(parents=True)
        (vol / "BACKUP").mkdir()
        return vol

    def test_list_local_backups(self, client, engine, dirs):
        make_pattern_file(dirs / "backups" / "july")
        r = client.get("/api/import/prm").json()
        assert r["device"]["mounted"] is False
        assert r["backups"] == [
            {"name": "july/S1_PTN1-01.PRM", "bank": 1, "slot": 1}
        ]

    def test_list_device_backup(self, client, engine, dirs):
        vol = self.mount_s1(dirs)
        make_pattern_file(vol / "BACKUP", name="S1_PTN3-07.PRM")
        r = client.get("/api/import/prm").json()
        assert r["device"]["mounted"] is True
        assert r["device"]["files"] == [
            {"name": "S1_PTN3-07.PRM", "bank": 3, "slot": 7}
        ]

    def test_import_applies_patch_and_sequence(self, client, engine, world, dirs):
        port = connect_s1(engine, world)
        make_pattern_file(dirs / "backups", cutoff=90, bpm=140.0)
        sent_before = len(port.sent)
        r = client.post("/api/import/prm", json={
            "source": "backups", "name": "S1_PTN1-01.PRM",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["params"] == len(prm_mod.load_template().to_cc_values())
        assert body["sequence"]["bpm"] == 140.0
        assert body["sequence"]["notes"] == 1
        # live state follows and the hardware heard the pushed CCs
        assert engine.params.get(74) == 90
        assert engine.sequencer.sequence.bpm == 140.0
        note = engine.sequencer.sequence.notes[0]
        assert (note.step, note.pitch, note.velocity) == (0, 60, 101)
        assert len(port.sent) > sent_before

    def test_import_patch_only_leaves_sequence(self, client, engine, dirs):
        make_pattern_file(dirs / "backups", bpm=175.0)
        before_bpm = engine.sequencer.sequence.bpm
        r = client.post("/api/import/prm", json={
            "source": "backups", "name": "S1_PTN1-01.PRM",
            "load_sequence": False,
        })
        assert r.status_code == 200
        assert "sequence" not in r.json()
        assert engine.sequencer.sequence.bpm == before_bpm
        assert engine.params.get(74) == 90

    def test_import_from_device(self, client, engine, dirs):
        vol = self.mount_s1(dirs)
        make_pattern_file(vol / "BACKUP", name="S1_PTN2-02.PRM", cutoff=33)
        r = client.post("/api/import/prm", json={
            "source": "device", "name": "S1_PTN2-02.PRM",
        })
        assert r.status_code == 200
        assert engine.params.get(74) == 33

    def test_import_device_unmounted_409(self, client, engine, dirs):
        assert client.post("/api/import/prm", json={
            "source": "device", "name": "S1_PTN1-01.PRM",
        }).status_code == 409

    def test_import_traversal_rejected(self, client, engine, dirs):
        evil = dirs / "evil.PRM"
        make_pattern_file(dirs, name="evil.PRM")
        assert evil.exists()
        r = client.post("/api/import/prm", json={
            "source": "backups", "name": "../evil.PRM",
        })
        assert r.status_code == 400

    def test_import_missing_404(self, client, engine, dirs):
        (dirs / "backups").mkdir()
        assert client.post("/api/import/prm", json={
            "source": "backups", "name": "S1_PTN1-01.PRM",
        }).status_code == 404

    def test_import_garbage_400(self, client, engine, dirs):
        backups = dirs / "backups"
        backups.mkdir()
        (backups / "junk.PRM").write_text("this is not a pattern")
        r = client.post("/api/import/prm", json={
            "source": "backups", "name": "junk.PRM",
        })
        assert r.status_code == 400

    def test_upload_prm(self, client, engine, dirs):
        path = make_pattern_file(dirs, cutoff=71)
        r = client.post(
            "/api/import/upload",
            files={"file": ("S1_PTN1-01.PRM", path.read_bytes())},
        )
        assert r.status_code == 200
        assert engine.params.get(74) == 71

    def test_upload_garbage_400(self, client, engine):
        r = client.post(
            "/api/import/upload", files={"file": ("x.PRM", b"\x00\x01nothing")}
        )
        assert r.status_code == 400


# ── G4: WebSocket state channel ──────────────────────────────
class TestStateWebSocket:
    def test_hello_carries_full_state(self, client, engine):
        engine.set_param(74, 21)
        with client.websocket_connect("/ws/state") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            assert hello["params"]["74"] == 21
            assert hello["status"]["sync"] == "disconnected"
            assert "sequence" in hello

    def test_knob_twist_broadcast(self, client, engine):
        with client.websocket_connect("/ws/state") as ws:
            ws.receive_json()  # hello
            engine.params.set(71, 77, source="midi")  # physical knob turn
            event = ws.receive_json()
            assert event == {"type": "param", "cc": 71, "value": 77, "source": "midi"}

    def test_client_param_message(self, client, engine, world):
        out = connect_s1(engine, world)
        with client.websocket_connect("/ws/state") as ws:
            ws.receive_json()
            ws.send_json({"type": "param", "cc": 74, "value": 44})
            event = ws.receive_json()  # broadcast back
            assert event["cc"] == 74 and event["value"] == 44
        assert engine.params.get(74) == 44
        assert any(m.type == "control_change" and m.control == 74 and m.value == 44
                   for m in out.sent)

    def test_client_note_message(self, client, engine, world):
        out = connect_s1(engine, world)
        with client.websocket_connect("/ws/state") as ws:
            ws.receive_json()
            ws.send_json({"type": "note", "note": 64, "velocity": 80, "on": True})
            ws.send_json({"type": "note", "note": 64, "on": False})
            ws.send_json({"type": "param", "cc": 74, "value": 1})  # fence: forces ordering
            ws.receive_json()
        notes = [m for m in out.sent if m.type in ("note_on", "note_off")]
        assert [(m.type, m.note) for m in notes] == [("note_on", 64), ("note_off", 64)]

    def test_sync_event_on_connect(self, client, engine, world):
        """Connect is listen-only: the chip stops at 'listening', not 'synced'."""
        with client.websocket_connect("/ws/state") as ws:
            ws.receive_json()
            connect_s1(engine, world)
            states = [ws.receive_json()["state"] for _ in range(2)]
            assert states == ["connecting", "listening"]


# ── C8: cockpit mode ─────────────────────────────────────────
class TestMode:
    def test_default_mode_is_solo(self, client):
        assert client.get("/api/mode").json() == {"mode": "solo"}

    def test_status_carries_mode(self, client):
        assert client.get("/api/status").json()["mode"] == "solo"

    def test_set_mode_logic_then_solo(self, client, engine):
        assert client.post("/api/mode", json={"mode": "logic"}).json() == {"mode": "logic"}
        assert engine.mode == "logic"
        assert client.get("/api/mode").json() == {"mode": "logic"}
        assert client.post("/api/mode", json={"mode": "solo"}).json() == {"mode": "solo"}
        assert engine.mode == "solo"

    def test_set_mode_rejects_unknown(self, client):
        assert client.post("/api/mode", json={"mode": "couch"}).status_code == 422

    def test_mode_broadcast_over_ws(self, client, engine):
        with client.websocket_connect("/ws/state") as ws:
            ws.receive_json()  # hello
            client.post("/api/mode", json={"mode": "logic"})
            event = ws.receive_json()
            assert event == {"type": "mode", "mode": "logic"}

    def test_hello_carries_mode(self, client, engine):
        engine.set_mode("logic")
        with client.websocket_connect("/ws/state") as ws:
            hello = ws.receive_json()
            assert hello["mode"] == "logic"
            assert hello["status"]["mode"] == "logic"


# ── security ─────────────────────────────────────────────────
class TestSecurity:
    def test_forbidden_host_rejected(self, engine):
        c = TestClient(app)  # default base_url "testserver" — not allowlisted
        assert c.get("/api/status").status_code == 403

    def test_forbidden_origin_rejected(self, client):
        r = client.get("/api/status", headers={"origin": "http://evil.example"})
        assert r.status_code == 403

    def test_allowed_origin_ok(self, client):
        r = client.get("/api/status", headers={"origin": "http://127.0.0.1:8766"})
        assert r.status_code == 200


# ── match studio surface ─────────────────────────────────────
class TestMatchEndpoints:
    def test_match_status(self, client, engine):
        r = client.get("/api/match/status").json()
        assert r["running"] is False
        assert "available" in r

    def test_match_start_without_target_400(self, client, engine):
        r = client.post("/api/match/start", json={"max_iters": 5})
        assert r.status_code in (400, 501)

    def test_clip_404(self, client, engine):
        assert client.get("/api/clip/best").status_code == 404

    def test_record_without_monitor_400(self, client, engine):
        assert client.post("/api/monitor/record/start").status_code == 400


# ── G4: the web↔engine seam is a pinned, faked-out contract ──
class TestEngineFacade:
    """The routes reach the engine through a typed surface, not an open object."""

    def test_real_engine_conforms(self, world):
        """The live S1Engine satisfies the EngineFacade the routes rely on."""
        e = S1Engine(midi_module=world, audio_auto=False, poll_interval=999)
        try:
            assert isinstance(e, EngineFacade)
        finally:
            e.stop()

    def test_facade_names_the_public_route_surface(self):
        """Guard the contract's key public methods against silent drift."""
        for name in (
            "status", "monitor_status", "transport_status", "set_param",
            "load_values", "push_all", "note_on", "note_off", "all_notes_off",
            "select_pattern", "set_mode", "subscribe", "unsubscribe",
            "publish", "start", "stop",
        ):
            assert hasattr(S1Engine, name), name


# ── FABLE debt: no private reaches from the web layer into the session ──
class TestNoPrivateSessionReach:
    def test_state_module_uses_only_public_session_api(self):
        """web/state.py must not touch MatchSession privates (session._foo) —
        Group 3 gave it public snapshot()/best_clip(); this pins the fix."""
        src = Path(server_mod.__file__).with_name("state.py").read_text()
        offenders = re.findall(r"session\._[A-Za-z]\w*", src)
        assert offenders == [], f"private session reach(es) remain: {offenders}"
