"""The static page's schema copy (``core/schema.json``) must equal the live
``/api/schema`` — the drift guard between the two contexts the plate runs in
(docs/design/BUILD.md §0: the cockpit reads the API, the static page the file).
Regenerate with ``python tools/export_schema.py``."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import synth.engine as engine_module
import synth.web.server as server_mod
from synth.engine import S1Engine
from synth.web.server import app
from tests.fakes import FakeMidiWorld

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_JSON = server_mod.STATIC_DIR / "core" / "schema.json"
TOOL = ROOT / "tools" / "export_schema.py"


@pytest.fixture
def client(monkeypatch):
    e = S1Engine(midi_module=FakeMidiWorld(), audio_auto=False, poll_interval=999)
    monkeypatch.setattr(engine_module, "ENGINE", e)
    yield TestClient(app, base_url="http://127.0.0.1:8766")
    e.stop()


def _tool():
    spec = importlib.util.spec_from_file_location("export_schema", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_static_schema_equals_live_api(client):
    live = client.get("/api/schema").json()
    assert json.loads(SCHEMA_JSON.read_text(encoding="utf-8")) == live, (
        "core/schema.json drifted from /api/schema: run python tools/export_schema.py"
    )


def test_export_tool_output_is_committed():
    tool = _tool()
    assert SCHEMA_JSON.read_text(encoding="utf-8") == tool.render(tool.schema_payload())


def test_export_leaves_the_live_engine_alone():
    before = engine_module.ENGINE
    _tool().schema_payload()
    assert engine_module.ENGINE is before
