"""Before/after benchmark for the twin matcher's search (round 4: "make the matcher try harder"; round 6:
plain starts, a prior on the extras, the note's length).

Seven targets made by the twin itself, so the true settings are known. Three or more use switches
other than the defaults, which the old search could only reach by one silent sweep at the end:

* ``gate``    Volume shape Gate, with a filter envelope (so Gate is not the same sound as
              Envelope with Sustain full)
* ``sub``     Sub octave −2 asym, with Sub up
* ``vibrato`` LFO wave Square, with vibrato
* ``wobble``  LFO wave Saw on the filter (LFO amount), plus Sub octave −2 with Sub up
* ``pluck``   an ordinary patch on the default switches
* ``square``  the S-1's default patch: a full square, the filter open (BASE below)
* ``short``   an ordinary held patch played SHORT: the key up at 0.35 s, which the search is not told
              (it assumes the twin's 1.2 s; round 6 scans the note's length)

Each target runs through the real code path — WAV bytes, :func:`twin_session.plan` (the upload's
decoder), :func:`twin_session.run`, notes given — under these searches:

* ``old``      the round-3 Thorough search: a budget without the round-4 keys runs it exactly
               (checked frame for frame against the round-3 file, seeded and cold;
               ``tests/test_match_session.py`` pins that path's frame sequence)
* ``before``   round 4's Thorough as of a00dd4c (a budget without the round-6 keys runs it exactly:
               checked frame for frame against that file); ``before-quick`` its Quick
* ``thorough``, ``quick``, ``deep``  the presets as they are now

and is scored with the Match view's own report (``views/match.js`` ``recoveryReport``, through
node): settings back within 10 of those that shape the note (``relevantCCs``), and whether every
switch that shapes it came back. Not part of the default test suite: a full run takes about an hour
on a busy laptop.

``--seeds 0,1,2`` runs each search once per seed; a seed varies the random starts only (0 is the
default draw). Round 6's presets start only from plain starts, so they give the same run for every
seed; the seeds measure how much "before" depended on its random starts. ``--set prior=0.03``
overrides a budget key of the current presets (for tuning).

    python tools/match_benchmark.py                       # old and thorough on all seven
    python tools/match_benchmark.py --search before,thorough --seeds 0,1,2
    python tools/match_benchmark.py --search deep --targets vibrato,wobble
    python tools/match_benchmark.py --json results.json   # also keep the raw numbers
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from synth.match import twin_session as ts  # noqa: E402
from synth.match.twin import S_PARAMS, Twin  # noqa: E402

OLD_THOROUGH = {"gd_iters": 160, "restarts": 4, "neighbor_iters": 24}   # round 3's preset
# Round 4's presets as of a00dd4c (before round 6's plain starts and prior).
BEFORE_THOROUGH = {"gd_iters": 240, "restarts": 4, "neighbor_iters": 24, "patience": 25, "min_iters": 60,
                   "tol": 5e-4, "lr_min": 0.008, "switch_top": 3, "switch_iters": 40, "continue_iters": 120,
                   "polish_iters": 80, "lfo_top": 1}
BEFORE_QUICK = {"gd_iters": 90, "restarts": 1, "neighbor_iters": 14, "patience": 15, "min_iters": 30,
                "tol": 1e-3, "lr_min": 0.008, "switch_top": 1, "switch_iters": 24, "continue_iters": 40,
                "polish_iters": 20, "lfo_top": 1}
SEARCHES = {"old": OLD_THOROUGH, "before": BEFORE_THOROUGH, "before-quick": BEFORE_QUICK,
            "quick": None, "thorough": None, "deep": None}

# The twin's S-1 defaults for the 21 settings (s1.json), which each target then changes.
BASE = {20: 0, 19: 127, 21: 0, 23: 0, 15: 0, 13: 0, 76: 64, 22: 2, 74: 127, 71: 0, 24: 0, 25: 0,
        26: 0, 73: 0, 75: 42, 30: 25, 72: 21, 28: 1, 3: 60, 17: 15, 12: 2}
TARGETS: dict[str, dict] = {
    "gate": {"note": 48, "words": "Gate, with a filter envelope",
             "cc": {20: 100, 19: 40, 15: 20, 74: 60, 71: 40, 24: 50, 73: 5, 75: 60, 30: 30, 72: 30, 28: 0}},
    "sub": {"note": 52, "words": "Sub octave −2 asym, Sub up",
            "cc": {20: 0, 19: 90, 21: 90, 15: 30, 74: 75, 71: 25, 24: 20, 73: 2, 75: 50, 30: 70, 72: 25,
                   22: 0}},
    "vibrato": {"note": 55, "words": "LFO wave Square, vibrato",
                "cc": {20: 90, 19: 0, 74: 85, 71: 20, 24: 25, 73: 4, 75: 55, 30: 80, 72: 30,
                       3: 55, 13: 35, 17: 60, 12: 3}},
    "wobble": {"note": 45, "words": "LFO wave Saw on the filter, Sub octave −2, Sub up",
               "cc": {20: 110, 19: 30, 21: 60, 15: 10, 74: 55, 71: 45, 24: 30, 25: 60, 73: 2, 75: 60,
                      30: 60, 72: 35, 3: 50, 17: 50, 12: 0, 22: 1}},
    "pluck": {"note": 48, "words": "an ordinary pluck, default switches",
              "cc": {20: 0, 19: 127, 74: 90, 71: 30, 24: 40, 73: 0, 75: 45, 30: 20, 72: 20}},
    "square": {"note": 48, "words": "the S-1 default patch: a full square, the filter open", "cc": {}},
    "short": {"note": 48, "words": "an ordinary held patch, the key up at 0.35 s", "gate": 0.35,
              "cc": {20: 110, 19: 30, 74: 80, 71: 30, 24: 30, 73: 2, 75: 60, 30: 90, 72: 45}},
}
SWITCH_CCS = (22, 12, 28)


def target_wav(name: str) -> tuple[bytes, dict[int, int], int]:
    """The target as WAV bytes (what an upload carries), its true 21 settings, and its note. Rendered
    like the Match view's test note: 2.2 s, the key up at 1.2 s (or the target's own), peak 0.9."""
    import soundfile as sf

    spec = TARGETS[name]
    cc = {**BASE, **spec["cc"]}
    twin = Twin(seconds=2.2, gate_fraction=spec.get("gate", 1.2) / 2.2)
    s = {sp.name: cc[sp.cc] for sp in S_PARAMS}
    audio = np.asarray(twin.render(twin.cc_to_k(cc), s, spec["note"]), dtype=np.float64)
    audio = 0.9 * audio / max(float(np.abs(audio).max()), 1e-12)
    buf = io.BytesIO()
    sf.write(buf, audio.astype(np.float32), twin.sr, format="WAV", subtype="PCM_16")
    return buf.getvalue(), cc, spec["note"]


_REPORT_JS = """
import { recoveryReport } from %s;
let raw = "";
process.stdin.on("data", (d) => { raw += d; });
process.stdin.on("end", () => {
  const out = JSON.parse(raw).map(({ truth, found, notes }) => {
    const r = recoveryReport(truth, found, { notes });
    const sw = r.rows.filter((x) => [22, 12, 28].includes(x.cc));
    return { good: r.good, total: r.total, switches: sw.map((x) => ({ cc: x.cc, ok: x.ok })),
      missed: r.rows.filter((x) => !x.ok).map((x) => `${x.label} ${x.trueText}>${x.foundText}`) };
  });
  process.stdout.write(JSON.stringify(out));
});
"""


def score(runs: list[dict]) -> list[dict]:
    """The Match view's own report for each run (node + views/match.js)."""
    node = shutil.which("node")
    if node is None:
        raise SystemExit("node is needed to score the runs with views/match.js")
    module = json.dumps((ROOT / "synth" / "web" / "static" / "views" / "match.js").as_uri())
    payload = [{"truth": {str(k): v for k, v in r["truth"].items()}, "found": r["found"],
                "notes": [r["note"]]} for r in runs]
    out = subprocess.run([node, "--input-type=module", "-e", _REPORT_JS % module], input=json.dumps(payload),
                         capture_output=True, text=True, timeout=120, check=True).stdout
    return json.loads(out)


def run_one(name: str, search: str, seed: int = 0, overrides: dict | None = None) -> dict:
    wav, truth, note = target_wav(name)
    preset = search if search in ts.QUALITY_PRESETS else "thorough"
    plan = ts.plan(wav, str(note), preset)
    budget = SEARCHES[search]
    if budget is None and overrides:
        budget = {**ts.QUALITY_PRESETS[preset], **overrides}
    done = None
    for step in ts.run(plan, budget, seed=seed):
        done = step.frame
    assert done is not None and done["phase"] == "done"
    return {"target": name, "search": search, "seed": seed, "note": note, "truth": truth,
            "found": done["cc"], "closeness": done["closeness"], "steps": done["steps"],
            "seconds": done["seconds"], "held": done.get("held")}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--search", default="old,thorough", help="comma list of: " + ", ".join(SEARCHES))
    ap.add_argument("--targets", default=",".join(TARGETS), help="comma list of: " + ", ".join(TARGETS))
    ap.add_argument("--seeds", default="0", help="comma list of seeds for the random starts (0: default)")
    ap.add_argument("--set", default="", help="budget overrides for the current presets, e.g. prior=0.03")
    ap.add_argument("--json", default=None, help="also write the raw results here")
    args = ap.parse_args(argv)
    searches = [s for s in args.search.split(",") if s]
    names = [t for t in args.targets.split(",") if t]
    seeds = [int(x) for x in args.seeds.split(",") if x != ""]
    overrides = {k: json.loads(v) for k, v in (kv.split("=", 1) for kv in args.set.split(",") if kv)}
    rows = []
    for name in names:
        for search in searches:
            for seed in seeds:
                r = run_one(name, search, seed, overrides)
                rows.append(r)
                print(f"  {name:8s} {search:12s} seed {seed}  closeness {r['closeness']:6.2f}  "
                      f"{r['steps']:5d} steps  {r['seconds']:7.1f} s", flush=True)
                if args.json:
                    Path(args.json).write_text(json.dumps(rows, indent=1) + "\n")
    for r, s in zip(rows, score(rows)):
        r.update(within=f"{s['good']} of {s['total']}", good=s["good"], total=s["total"],
                 switches_ok=all(x["ok"] for x in s["switches"]), missed=s["missed"])
    print("\n| target | search | closeness mean (min) | switches back | settings within 10, mean (min) "
          "| steps | seconds |")
    print("|---|---|---|---|---|---|---|")
    for name in names:
        for search in searches:
            rs = [r for r in rows if r["target"] == name and r["search"] == search]
            cl = [r["closeness"] for r in rs]
            frac = [r["good"] / max(1, r["total"]) for r in rs]
            back = sum(r["switches_ok"] for r in rs)
            steps_, secs = np.mean([r["steps"] for r in rs]), np.mean([r["seconds"] for r in rs])
            print(f"| {name} | {search} | {np.mean(cl):.1f} ({min(cl):.1f}) | {back} of {len(rs)} | "
                  f"{100 * np.mean(frac):.0f}% ({100 * min(frac):.0f}%) | {steps_:.0f} | {secs:.0f} |")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1) + "\n")


if __name__ == "__main__":
    main()
