"""G9 — the agent door.

A scripted "agent session" that operates the synth exclusively through the
public HTTP/WS API (the same surface /docs documents): discover the schema,
sculpt a dreamy patch, build a short sequence, play it, listen for the
result on the (fake) device, and save everything — plus a checklist that
every UI capability has a documented endpoint.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import synth.engine as engine_module
import synth.patches as patches_mod
import synth.sequences as sequences_mod
import synth.web.server as server_mod
from synth.engine import S1Engine
from synth.prm import PrmFile
from synth.web.server import app
from tests.fakes import FakeMidiWorld

BASE_URL = "http://127.0.0.1:8766"


@pytest.fixture
def world():
    return FakeMidiWorld()


@pytest.fixture
def engine(world, monkeypatch, tmp_path):
    e = S1Engine(midi_module=world, audio_auto=False, poll_interval=999)
    monkeypatch.setattr(engine_module, "ENGINE", e)
    monkeypatch.setattr(patches_mod, "PATCH_DIR", tmp_path / "patches")
    monkeypatch.setattr(sequences_mod, "SEQUENCE_DIR", tmp_path / "sequences")
    server_mod._match_holder.clear()
    yield e
    e.stop()


@pytest.fixture
def agent(engine):
    """The agent's only tool: an HTTP client against the documented API."""
    return TestClient(app, base_url=BASE_URL)


def s1_plugged_in(engine, world):
    world.add_device(out_name="S-1 MIDI IN", in_name="S-1 MIDI OUT")
    engine._tick()
    return world.outputs["S-1 MIDI IN"]


class TestAgentSession:
    def test_agent_builds_dreamy_patch_and_sequence(self, agent, engine, world):
        out = s1_plugged_in(engine, world)

        # 1. The agent reads the schema to learn what it can control.
        schema = agent.get("/api/schema").json()
        by_name = {
            (s["name"], p["name"]): p
            for s in schema["sections"] for p in s["params"]
        }
        attack = by_name[("ENV", "Attack")]
        release = by_name[("ENV", "Release")]
        reverb_time = by_name[("EFX", "Reverb Time")]
        reverb_level = by_name[("EFX", "Reverb Level")]
        delay_level = by_name[("EFX", "Delay Level")]
        cutoff = by_name[("FILTER", "Frequency")]

        # 2. A dreamy patch: slow attack, long release, long wet reverb/delay,
        #    softened filter. Bulk-set through the documented endpoint.
        dreamy = {
            str(attack["cc"]): 96,
            str(release["cc"]): 110,
            str(reverb_time["cc"]): 120,
            str(reverb_level["cc"]): 100,
            str(delay_level["cc"]): 80,
            str(cutoff["cc"]): 70,
        }
        r = agent.post("/api/params", json={"values": dreamy})
        assert r.status_code == 200
        assert r.json()["unknown"] == []

        # The hardware received every CC.
        sent = {m.control: m.value for m in out.sent if m.type == "control_change"}
        for cc_str, value in dreamy.items():
            assert sent[int(cc_str)] == value

        # 3. A short rising line, fast enough to hear in a test run.
        line = {
            "steps": 8,
            "bpm": 600.0,
            "step_resolution": "1/16",
            "notes": [
                {"step": 0, "pitch": 48, "velocity": 100, "duration": 2},
                {"step": 2, "pitch": 55, "velocity": 90, "duration": 2},
                {"step": 4, "pitch": 60, "velocity": 95, "duration": 2},
                {"step": 6, "pitch": 67, "velocity": 105, "duration": 2},
            ],
        }
        r = agent.put("/api/sequence", json=line)
        assert r.status_code == 200
        assert r.json()["poly_warnings"] == []

        # 4. Play it and confirm the device heard notes + MIDI clock.
        assert agent.post("/api/transport", json={"action": "play"}).json()["playing"]
        time.sleep(0.25)
        agent.post("/api/transport", json={"action": "stop"})
        played = [m.note for m in out.sent if m.type == "note_on"]
        assert set(played) >= {48, 55, 60}
        assert any(m.type == "clock" for m in out.sent)
        assert any(m.type == "start" for m in out.sent)

        # 5. Play a note directly (the agent can improvise, too).
        agent.post("/api/notes", json={"note": 72, "velocity": 90, "on": True})
        agent.post("/api/notes", json={"note": 72, "on": False})

        # 6. Save the patch and the sequence into the banks.
        assert agent.post("/api/patches", json={"name": "dreamscape"}).status_code == 200
        assert agent.post("/api/sequences", json={"name": "rising-line"}).status_code == 200
        assert [p["name"] for p in agent.get("/api/patches").json()] == ["dreamscape"]
        assert [s["name"] for s in agent.get("/api/sequences").json()] == ["rising-line"]

        # 7. Export the whole thing as a device-ready .PRM file.
        r = agent.get("/api/export/prm?bank=1&slot=3")
        assert r.status_code == 200
        prm = PrmFile.parse(r.content.decode("ascii"))
        assert prm.to_cc_values()[attack["cc"]] == 96
        exported = prm.to_sequence()
        assert exported.steps == 8
        assert {n.pitch for n in exported.notes} == {48, 55, 60, 67}

        # 8. Switch the S-1 to a device pattern slot, live.
        r = agent.post("/api/device/pattern", json={"bank": 1, "slot": 3})
        assert r.json()["program"] == 2

    def test_agent_can_read_everything_it_can_write(self, agent, engine, world):
        s1_plugged_in(engine, world)
        agent.put("/api/params/73", json={"value": 96})
        assert agent.get("/api/params/73").json()["value"] == 96
        assert agent.get("/api/state").json()["params"]["73"] == 96
        status = agent.get("/api/status").json()
        assert status["sync"] == "synced"

    def test_agent_hears_knob_twists_over_ws(self, agent, engine, world):
        """Two-way: the agent subscribes and sees a physical knob move."""
        s1_plugged_in(engine, world)
        with agent.websocket_connect("/ws/state") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            engine.params.set(74, 31, source="midi")  # human twists the knob
            event = ws.receive_json()
            assert (event["cc"], event["value"], event["source"]) == (74, 31, "midi")


class TestApiCompleteness:
    """Every UI capability has a documented endpoint (the G9 checklist)."""

    # capability -> (method, path) as it appears in the OpenAPI spec
    CHECKLIST = {
        "read full schema": ("get", "/api/schema"),
        "read full state": ("get", "/api/state"),
        "read status": ("get", "/api/status"),
        "read one param": ("get", "/api/params/{cc}"),
        "set one param": ("put", "/api/params/{cc}"),
        "set many params": ("post", "/api/params"),
        "play notes": ("post", "/api/notes"),
        "panic": ("post", "/api/notes/all-off"),
        "select device pattern": ("post", "/api/device/pattern"),
        "list midi ports": ("get", "/api/midi/ports"),
        "list patches": ("get", "/api/patches"),
        "save patch": ("post", "/api/patches"),
        "read patch": ("get", "/api/patches/{name}"),
        "load patch": ("post", "/api/patches/{name}/load"),
        "audition patch": ("post", "/api/patches/{name}/play"),
        "delete patch": ("delete", "/api/patches/{name}"),
        "read sequence": ("get", "/api/sequence"),
        "write sequence": ("put", "/api/sequence"),
        "transport actions": ("post", "/api/transport"),
        "performance settings": ("put", "/api/transport"),
        "list sequences": ("get", "/api/sequences"),
        "save sequence": ("post", "/api/sequences"),
        "load sequence": ("post", "/api/sequences/{name}/load"),
        "delete sequence": ("delete", "/api/sequences/{name}"),
        "monitor status": ("get", "/api/monitor"),
        "live waveform scope": ("get", "/api/monitor/scope"),
        "monitor mute": ("post", "/api/monitor/mute"),
        "monitor gain": ("post", "/api/monitor/gain"),
        "monitor stop": ("post", "/api/monitor/stop"),
        "monitor start": ("post", "/api/monitor/start"),
        "record take": ("post", "/api/monitor/record/start"),
        "finish take": ("post", "/api/monitor/record/stop"),
        "download .PRM": ("get", "/api/export/prm"),
        "disk-mode status": ("get", "/api/export/device"),
        "write to device": ("post", "/api/export/device"),
        "list importable patterns": ("get", "/api/import/prm"),
        "import a device pattern": ("post", "/api/import/prm"),
        "import an uploaded .PRM": ("post", "/api/import/upload"),
        "match status": ("get", "/api/match/status"),
        "upload match target": ("post", "/api/target"),
        "start match": ("post", "/api/match/start"),
        "save match result": ("post", "/api/match/save"),
    }

    def test_every_capability_documented(self, agent):
        spec = agent.get("/openapi.json").json()
        paths = spec["paths"]
        for capability, (method, path) in self.CHECKLIST.items():
            assert path in paths, f"{capability}: {path} missing from OpenAPI"
            assert method in paths[path], f"{capability}: {method.upper()} {path} missing"

    def test_endpoints_have_descriptions(self, agent):
        """An agent given only the OpenAPI spec needs prose, not just paths."""
        spec = agent.get("/openapi.json").json()
        for capability, (method, path) in self.CHECKLIST.items():
            op = spec["paths"][path][method]
            text = (op.get("description") or "") + (op.get("summary") or "")
            assert len(text) >= 10, f"{capability}: {method.upper()} {path} undocumented"

    def test_docs_served(self, agent):
        assert agent.get("/docs").status_code == 200
        assert agent.get("/openapi.json").status_code == 200
