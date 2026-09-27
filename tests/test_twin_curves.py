"""Drift guard: the browser twin's bundled curves must equal ``twin.export_curves()``.

``synth/web/static/twin/curves.json`` is what the in-browser twin runs when no server
serves calibrated curves (the static page, or before calibration). It is generated from
``synth/match/twin.py`` — the same guard the repo keeps on ``s1.json``. If this fails,
twin.py's curves or tables (or ``s1.json``) changed; regenerate with::

    python -m synth.match.twin --export-curves synth/web/static/twin/curves.json
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from synth.match import ANALYSIS_SECONDS, WORKING_SR, twin
from synth.schema import S1_PARAMS

CURVES_JSON = Path(twin.__file__).resolve().parents[1] / "web" / "static" / "twin" / "curves.json"


def test_committed_curves_match_export():
    assert json.loads(CURVES_JSON.read_text(encoding="utf-8")) == twin.export_curves()


def test_export_is_json_native_and_complete():
    doc = twin.export_curves()
    assert json.loads(json.dumps(doc)) == doc  # no tuples, no int keys: nothing lossy
    assert doc["version"] == 1
    assert doc["calibrated"] is False
    assert doc["source"] == "twin.DEFAULT_CURVES"
    assert (doc["sr"], doc["seconds"], doc["gate_fraction"]) == (WORKING_SR, ANALYSIS_SECONDS, 0.6)
    assert [name for name, _ in doc["k_params"]] == [p.name for p in twin.K_PARAMS]
    assert set(doc["curves"]) == {p.name for p in twin.K_PARAMS}
    for name, cur in twin.DEFAULT_CURVES.items():
        assert doc["curves"][name] == {"lo": cur.lo, "hi": cur.hi, "kind": cur.kind, "unit": cur.unit}
    assert doc["s_params"] == [[p.name, p.cc, list(p.choices)] for p in twin.S_PARAMS]
    assert doc["default_s"] == twin._default_s()
    # every option a CC can select has an entry (the browser clamps into these maps)
    assert set(doc["lfo_shape"]) == {"0", "1", "2", "3", "4", "5"}
    assert set(doc["sub_octave"]) == {"0", "1", "2"}
    # cc_to_k's normalization needs every CC's range and default
    assert set(doc["cc_ranges"]) == {str(p.cc) for p in S1_PARAMS}
    for p in S1_PARAMS:
        assert doc["cc_ranges"][str(p.cc)] == [p.min_val, p.max_val, p.default]


def test_export_carries_a_calibrated_mapping():
    """A calibration run exports its fitted mapping with the same sub-schema."""
    fitted = twin.DEFAULT_MAPPING.calibrated(
        "probe fit 2026-09-27", cutoff=twin.Curve(40.0, 9000.0, "exp", "Hz")
    )
    doc = twin.export_curves(fitted, calibrated=True)
    assert doc["calibrated"] is True
    assert doc["source"] == "probe fit 2026-09-27"
    assert doc["curves"]["cutoff"] == {"lo": 40.0, "hi": 9000.0, "kind": "exp", "unit": "Hz"}
    assert doc["curves"]["attack"] == twin.export_curves()["curves"]["attack"]


def test_module_cli_writes_the_export(tmp_path):
    out = tmp_path / "curves.json"
    root = CURVES_JSON.parents[4]
    subprocess.run(
        [sys.executable, "-m", "synth.match.twin", "--export-curves", str(out)],
        check=True, cwd=root, capture_output=True, timeout=120,
    )
    assert json.loads(out.read_text(encoding="utf-8")) == twin.export_curves()
