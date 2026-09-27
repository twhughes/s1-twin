"""Thin a recorded match (matches/<slug>.json) for the public page, in place.

    python tools/thin_match.py synth/web/static/matches/*.json [--keep 130]

A thorough run streams ~640 descent frames (~1 MB); the static page's replay only needs the
story. Kept: every non-descent frame (pitch, note-search, done), the first and last frame of each
restart, and every frame where the best loss improved; the rest are sampled evenly until about
``--keep`` frames remain. The replay reads frames in order, so the knobs still move smoothly.
Idempotent: a thinned file thins to itself.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def thin(frames: list[dict], keep: int = 130) -> list[dict]:
    n = len(frames)
    if n <= keep:                                 # already short enough: the story is all of it
        return list(frames)
    must = set()
    last_restart = None
    best_in_restart: dict = {}                    # restart -> (loss, index) of its lowest-loss frame
    for i, f in enumerate(frames):
        if f.get("phase") != "gd":
            must.add(i)
            continue
        r = f.get("restart")
        if r != last_restart:                     # a restart begins: keep its first frame
            must.add(i)
            if i > 0:
                must.add(i - 1)                   # ...and the previous restart's last
            last_restart = r
        loss = f.get("loss", float("inf"))
        if loss < best_in_restart.get(r, (float("inf"), -1))[0]:
            best_in_restart[r] = (loss, i)
    must.update(i for _, i in best_in_restart.values())
    budget = max(0, keep - len(must))
    rest = [i for i in range(n) if i not in must]
    if budget >= len(rest):
        must.update(rest)
    elif rest and budget:
        step = len(rest) / budget                 # > 1 here, so the picks are distinct
        must.update(rest[int(j * step)] for j in range(budget))
    return [frames[i] for i in sorted(must)]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--keep", type=int, default=130)
    args = ap.parse_args()
    for p in args.files:
        if p.name == "index.json":
            continue
        d = json.loads(p.read_text())
        before, size0 = len(d["frames"]), p.stat().st_size
        d["frames"] = thin(d["frames"], args.keep)
        p.write_text(json.dumps(d, separators=(",", ":")))
        size1 = p.stat().st_size
        print(f"{p.name}: {before} -> {len(d['frames'])} frames, {size0 // 1024} -> {size1 // 1024} KB")


if __name__ == "__main__":
    main()
