"""tools/thin_match.py: a recorded match keeps its story (phases, restarts, bests) in ~130 frames."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _thin():
    spec = importlib.util.spec_from_file_location("thin_match", ROOT / "tools" / "thin_match.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.thin


def _run(restarts=4, steps=160):
    frames = [{"phase": "pitch", "iter": 0}]
    for r in range(restarts):
        for s in range(steps):
            loss = 10.0 / (1 + s) + r * 0.3 + (0.01 if s % 7 else 0.0)
            frames.append({"phase": "gd", "restart": r, "iter": s, "loss": loss})
    frames.append({"phase": "done", "iter": restarts * steps})
    return frames


def test_thin_keeps_the_story_within_budget():
    thin = _thin()
    frames = _run()
    out = thin(frames, keep=130)
    assert 120 <= len(out) <= 140
    assert out[0]["phase"] == "pitch" and out[-1]["phase"] == "done"
    idx = [frames.index(f) for f in out]
    assert idx == sorted(idx), "order must be preserved for the replay"
    for r in range(4):                                  # every restart's first frame and its best survive
        gd = [f for f in frames if f.get("restart") == r]
        assert gd[0] in out
        assert min(gd, key=lambda f: f["loss"]) in out


def test_thin_is_idempotent_and_leaves_short_runs_alone():
    thin = _thin()
    once = thin(_run(), keep=130)
    assert thin(once, keep=130) == once
    short = _run(restarts=1, steps=20)
    assert thin(short, keep=130) == short
