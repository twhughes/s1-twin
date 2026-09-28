"""The reproduction suite: sounds the matcher must be able to reproduce, clean and "recorded".

Each case is a patch the twin plays (so its true settings are known), a note or a chord, and how
long the key is held. Its recorded copy (``<case>@rec``) puts the same note through what a real take
does to a sound: a small speaker (no deep bass, a softer top), a room's echo, 48 kHz, a quiet level,
and two seconds of room noise before the note and more after it. Every case runs through the real
path (WAV bytes -> twin_session.plan -> run), is scored with the Match view's own report
(views/match.js recoveryReport, via tools/match_benchmark.score), and is compared with the baseline
kept in the repo (docs/match-baseline.json). The fast regression cases live in tests/test_match_suite.py.

    python tools/match_suite.py                      # every case, Thorough, compared with the baseline
    python tools/match_suite.py --search quick --jobs 3
    python tools/match_suite.py --cases gate,gate@rec --seeds 0,1,2
    python tools/match_suite.py --update-baseline    # accept these numbers as the new baseline

Exit status 1 when a case falls below its baseline by more than the tolerance (closeness by more
than CLOSENESS_TOL points, settings back by more than SETTINGS_TOL, or a switch it used to find).
A full Thorough run takes about 20 minutes with 3 jobs on a busy laptop.
"""

from __future__ import annotations

import argparse
import inspect
import io
import json
import math
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

BASELINE = ROOT / "docs" / "match-baseline.json"
CLOSENESS_TOL = 6.0        # points of closeness a case may lose before the suite fails
SETTINGS_TOL = 2           # settings back within 10 it may lose
REC_SR = 48000             # a recording's rate

# The S-1 defaults for the 21 settings the twin models (s1.json); each case changes some of them.
BASE = {20: 0, 19: 127, 21: 0, 23: 0, 15: 0, 13: 0, 76: 64, 22: 2, 74: 127, 71: 0, 24: 0, 25: 0,
        26: 0, 73: 0, 75: 42, 30: 25, 72: 21, 28: 1, 3: 60, 17: 15, 12: 2}

CASES: dict[str, dict] = {
    "square":  {"notes": [48], "held": 1.2, "words": "the S-1's default patch: a full square, filter open",
                "cc": {}},
    "saw":     {"notes": [60], "held": 1.2, "words": "a saw lead, a little resonance, a filter sweep",
                "cc": {20: 110, 19: 0, 74: 80, 71: 30, 24: 40, 73: 2, 75: 50, 30: 70, 72: 30}},
    "gate":    {"notes": [48], "held": 1.2, "words": "Volume shape Gate, with a filter envelope",
                "cc": {20: 100, 19: 40, 15: 20, 74: 60, 71: 40, 24: 50, 73: 5, 75: 60, 30: 30, 72: 30,
                       28: 0}},
    "sub":     {"notes": [52], "held": 1.2, "words": "Sub octave -2 asym, Sub up",
                "cc": {20: 0, 19: 90, 21: 90, 15: 30, 74: 75, 71: 25, 24: 20, 73: 2, 75: 50, 30: 70,
                       72: 25, 22: 0}},
    "vibrato": {"notes": [55], "held": 1.2, "words": "LFO wave Square on the pitch",
                "cc": {20: 90, 19: 0, 74: 85, 71: 20, 24: 25, 73: 4, 75: 55, 30: 80, 72: 30, 3: 55, 13: 35,
                       17: 60, 12: 3}},
    "wobble":  {"notes": [45], "held": 1.2, "words": "LFO wave Saw on the filter, Sub octave -2",
                "cc": {20: 110, 19: 30, 21: 60, 15: 10, 74: 55, 71: 45, 24: 30, 25: 60, 73: 2, 75: 60,
                       30: 60, 72: 35, 3: 50, 17: 50, 12: 0, 22: 1}},
    "pluck":   {"notes": [48], "held": 1.2, "words": "a square pluck, default switches",
                "cc": {20: 0, 19: 127, 74: 90, 71: 30, 24: 40, 73: 0, 75: 45, 30: 20, 72: 20}},
    "bass":    {"notes": [36], "held": 1.0, "words": "a low bass: saw and sub, a closed resonant filter",
                "cc": {20: 100, 19: 0, 21: 80, 74: 45, 71: 55, 24: 60, 73: 0, 75: 40, 30: 10, 72: 25}},
    "high":    {"notes": [72], "held": 1.0, "words": "a high square lead with a narrow pulse",
                "cc": {20: 0, 19: 100, 15: 60, 74: 100, 71: 10, 24: 10, 73: 3, 75: 50, 30: 80, 72: 30}},
    "pad":     {"notes": [55], "held": 1.4, "words": "a slow pad: saw and square, a long attack",
                "cc": {20: 90, 19: 40, 74: 70, 71: 15, 24: 20, 73: 70, 75: 60, 30: 90, 72: 70}},
    "short":   {"notes": [50], "held": 0.35, "words": "an ordinary patch, the key let go at 0.35 s",
                "cc": {20: 80, 19: 60, 74: 85, 71: 20, 24: 30, 73: 2, 75: 50, 30: 70, 72: 30}},
    "chord":   {"notes": [48, 52, 55], "held": 1.2, "words": "a C major triad on a saw patch",
                "cc": {20: 100, 19: 20, 74: 85, 71: 15, 24: 25, 73: 3, 75: 55, 30: 60, 72: 35}},
}
RECORDED = ("square", "gate", "pluck", "bass", "pad", "short")   # these also run as "<case>@rec"


def all_case_names() -> list[str]:
    return list(CASES) + [f"{c}@rec" for c in RECORDED]


# ── the targets ───────────────────────────────────────────────────────────────────────────────
def render_case(name: str) -> tuple[np.ndarray, int, dict[int, int], list[int]]:
    """The case's note as the twin plays it (float64 at the twin's rate, peak 0.9), its rate, its true
    21 settings and its notes. The render is long enough for the whole release after the key-up."""
    from synth.match.twin import S_PARAMS, Twin

    spec = CASES[name]
    cc = {**BASE, **spec["cc"]}
    held = float(spec["held"])
    seconds = max(2.2, held + 1.0)
    twin = Twin(seconds=seconds, gate_fraction=held / seconds)
    s = {sp.name: cc[sp.cc] for sp in S_PARAMS}
    audio = np.asarray(twin.render_chord(twin.cc_to_k(cc), s, spec["notes"]), dtype=np.float64)
    audio = 0.9 * audio / max(float(np.abs(audio).max()), 1e-12)
    return audio, twin.sr, {c: cc[c] for c in BASE}, list(spec["notes"])


def record(audio: np.ndarray, sr: int, seed: int = 7) -> np.ndarray:
    """What a real take does to a clean note: a small speaker (a 150 Hz high-pass, a gentle top roll-off),
    a room (a short exponentially decaying echo, 12 dB down), 48 kHz, a peak at -18 dBFS, and room noise:
    2 s of it before the note, 1.5 s after, and under everything (-60 dBFS RMS, pinkish)."""
    from scipy.signal import butter, fftconvolve, resample_poly, sosfilt

    rng = np.random.default_rng(seed)
    x = sosfilt(butter(2, 150.0, "highpass", fs=sr, output="sos"), audio)
    x = sosfilt(butter(1, min(9000.0, 0.45 * sr), "lowpass", fs=sr, output="sos"), x)
    rt60 = 0.35
    t = np.arange(int(rt60 * sr)) / sr
    ir = rng.standard_normal(t.size) * np.exp(-6.91 * t / rt60)
    wet = fftconvolve(x, ir)[: x.size]
    x = x + 10 ** (-12 / 20) * wet * (np.abs(x).max() / max(np.abs(wet).max(), 1e-12))
    g = math.gcd(REC_SR, sr)
    x = resample_poly(x, REC_SR // g, sr // g)
    x = 10 ** (-18 / 20) * x / max(float(np.abs(x).max()), 1e-12)
    pre, post = int(2.0 * REC_SR), int(1.5 * REC_SR)
    x = np.concatenate([np.zeros(pre), x, np.zeros(post)])
    white = rng.standard_normal(x.size)
    pink = sosfilt(butter(1, 400.0, "lowpass", fs=REC_SR, output="sos"), white) * 3.0 + 0.3 * white
    return x + 10 ** (-60 / 20) * pink / max(float(np.std(pink)), 1e-12)


def wav_bytes(audio: np.ndarray, sr: int) -> bytes:
    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, np.clip(audio, -1.0, 1.0).astype(np.float32), sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def target(case: str) -> tuple[bytes, dict[int, int], list[int]]:
    """WAV bytes (what an upload carries), the true settings and the notes for a case name."""
    name, recorded = (case[:-4], True) if case.endswith("@rec") else (case, False)
    audio, sr, truth, notes = render_case(name)
    if recorded:
        return wav_bytes(record(audio, sr), REC_SR), truth, notes
    return wav_bytes(audio, sr), truth, notes


# ── one run ───────────────────────────────────────────────────────────────────────────────────
def run_case(job: tuple[str, str, int]) -> dict:
    """Match one case through the real path: plan() on the WAV bytes, the notes given (as the Match view
    sends them once they are marked), then run() to the done frame."""
    case, search, seed = job
    from synth.match import twin_session as ts

    wav, truth, notes = target(case)
    t0 = time.perf_counter()
    plan = ts.plan(wav, ",".join(map(str, notes)), search)
    kwargs = {"seed": seed} if seed and "seed" in inspect.signature(ts.run).parameters else {}
    done = None
    for step in ts.run(plan, **kwargs):
        done = step.frame
    assert done is not None and done["phase"] == "done", case
    return {"case": case, "search": search, "seed": seed, "notes": notes, "truth": truth,
            "found": done["cc"], "closeness": float(done["closeness"]), "steps": int(done["steps"]),
            "seconds": round(time.perf_counter() - t0, 1), "held": done.get("held"),
            "crop": list(getattr(plan, "crop", None) or []) or None}


def score_runs(runs: list[dict]) -> None:
    """Add the Match view's own report to each run: settings back within 10 and switches back."""
    from match_benchmark import score

    for r, s in zip(runs, score([{"truth": r["truth"], "found": r["found"], "note": r["notes"][0]}
                                 for r in runs])):
        r.update(good=s["good"], total=s["total"], switches=all(x["ok"] for x in s["switches"]),
                 missed=s["missed"])


# ── the baseline ──────────────────────────────────────────────────────────────────────────────
def summarize(runs: list[dict]) -> dict[str, dict]:
    """Per case: the mean closeness over seeds, the worst settings count, and whether every seed found
    the switches."""
    out: dict[str, dict] = {}
    for case in dict.fromkeys(r["case"] for r in runs):
        rs = [r for r in runs if r["case"] == case]
        out[case] = {"closeness": round(float(np.mean([r["closeness"] for r in rs])), 1),
                     "worst_closeness": round(min(r["closeness"] for r in rs), 1),
                     "good": min(r["good"] for r in rs), "total": rs[0]["total"],
                     "switches": all(r["switches"] for r in rs), "seeds": len(rs)}
    return out


def compare(now: dict[str, dict], base: dict[str, dict]) -> list[str]:
    """The cases that fell below their baseline by more than the tolerance (plain sentences)."""
    fails = []
    for case, n in now.items():
        b = base.get(case)
        if not b:
            continue
        if n["closeness"] < b["closeness"] - CLOSENESS_TOL:
            fails.append(f"{case}: closeness {n['closeness']} < baseline {b['closeness']} - {CLOSENESS_TOL}")
        if n["good"] < b["good"] - SETTINGS_TOL:
            fails.append(f"{case}: {n['good']} settings back < baseline {b['good']} - {SETTINGS_TOL}")
        if b["switches"] and not n["switches"]:
            fails.append(f"{case}: the switches no longer come back")
    return fails


def git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    names = all_case_names()
    ap.add_argument("--cases", default=",".join(names), help="comma list of: " + ", ".join(names))
    ap.add_argument("--search", default="thorough", help="quick, thorough or deep")
    ap.add_argument("--seeds", default="0", help="comma list of seeds for the random starts")
    ap.add_argument("--jobs", type=int, default=3, help="cases run at once")
    ap.add_argument("--json", default=None, help="also write the raw runs here")
    ap.add_argument("--update-baseline", action="store_true", help="accept these numbers as the baseline")
    args = ap.parse_args(argv)

    cases = [c for c in args.cases.split(",") if c]
    unknown = [c for c in cases if c not in all_case_names()]
    if unknown:
        ap.error(f"unknown cases: {', '.join(unknown)}")
    seeds = [int(s) for s in args.seeds.split(",") if s != ""]
    jobs = [(c, args.search, s) for c in cases for s in seeds]
    t0 = time.perf_counter()
    runs: list[dict] = []
    with ProcessPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for r in pool.map(run_case, jobs):
            runs.append(r)
            print(f"  {r['case']:12s} seed {r['seed']}  closeness {r['closeness']:6.2f}  "
                  f"{r['steps']:5d} steps  {r['seconds']:7.1f} s", flush=True)
    score_runs(runs)
    now = summarize(runs)
    stored = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    base = stored.get(args.search, {}).get("cases", {})

    print(f"\n{args.search}, {len(runs)} runs in {time.perf_counter() - t0:.0f} s\n")
    print("| case | closeness | worst | settings within 10 | switches back | baseline |")
    print("|---|---|---|---|---|---|")
    for case, n in now.items():
        b = base.get(case)
        was = f"{b['closeness']}, {b['good']} of {b['total']}" if b else "none"
        print(f"| {case} | {n['closeness']} | {n['worst_closeness']} | {n['good']} of {n['total']} | "
              f"{'yes' if n['switches'] else 'no'} | {was} |")
    if args.json:
        Path(args.json).write_text(json.dumps(runs, indent=1) + "\n")

    if args.update_baseline:
        stored[args.search] = {"made": date.today().isoformat(), "commit": git_sha(),
                               "cases": {**base, **now}}
        BASELINE.write_text(json.dumps(stored, indent=1) + "\n")
        print(f"\nbaseline for {args.search} written to {BASELINE.relative_to(ROOT)}")
        return 0
    fails = compare(now, base)
    if fails:
        print("\nWORSE THAN THE BASELINE:\n  " + "\n  ".join(fails))
        return 1
    print("\nno case fell below its baseline" if base else "\nno baseline yet: run with --update-baseline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
