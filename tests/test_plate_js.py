"""The plate's pure JS modules carry node checks next to them (docs/design/BUILD.md §4):
core/layout.check.mjs (every schema parameter placed exactly once, the fallback, the leaders)
and core/ctx.check.mjs (set/on/source semantics, note routing, the echo filter). Running them
here puts them in pytest and CI. Skips cleanly when node is not installed."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "synth" / "web" / "static"
CHECKS = ["core/layout.check.mjs", "core/ctx.check.mjs"]


@pytest.mark.parametrize("check", CHECKS)
def test_node_check_passes(check):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    r = subprocess.run([node, str(STATIC / check)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"{check} failed:\n{r.stdout}\n{r.stderr}"
    assert "checks passed" in r.stdout
