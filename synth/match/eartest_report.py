"""``synth-eartest-report`` — does the metric agree with your ears? (FABLE rule 1)

Reads the ear test's answer files (one JSON object per line, written by
``synth/web/eartest.py``) and reports how often your choice matches the metric's
choice — the candidate with the lower ``spectral_loss`` to the reference:

- agreement on **clear** trials (you chose A or B), with a bootstrap 95% CI;
- "Can't tell" answers handled explicitly: counted and reported per bin, never scored
  as agreement (the strict rate, which counts them as misses, is shown too);
- a breakdown by metric-gap bin (how much farther the far candidate is, by the metric);
- where the disagreements come from (what the metric's "closer" candidate changed);
- one verdict line at the end, e.g.
  "The metric agrees with your ears on 82% of clear trials (95% CI 70–91%)."

Pure standard library, so it runs anywhere::

    synth-eartest-report                  # the newest session in ~/.synth/eartest/
    synth-eartest-report 2026-09-28-1412  # a session by name (or a path to its .jsonl)
    synth-eartest-report --all --json     # every session, machine-readable
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
from collections import Counter
from pathlib import Path

# Metric-gap bins by the ACHIEVED loss ratio (far / near). The edges are the ear test's
# tier edges — the one home is synth/web/eartest.py TIERS; tests/test_eartest.py asserts
# the two still agree.
BINS: tuple[tuple[str, float, float], ...] = (
    ("easy", 2.2, math.inf),
    ("medium", 1.6, 2.2),
    ("hard", 1.3, 1.6),
    ("very hard", 1.0, 1.3),
)
MODULE_LABELS = {"osc": "Oscillator", "filter": "Filter", "env": "Envelope", "lfo": "LFO"}

# Verdict thresholds. Agreement is "good" when the clear-trial rate is at least 75% and
# the CI's lower end clears 60%; a bin "fails" when its rate is under 65% on 3+ clear trials.
GOOD_RATE = 0.75
GOOD_LOWER = 0.60
BIN_FAIL = 0.65
BIN_MIN_CLEAR = 3
N_BOOT = 10_000


def eartest_dir() -> Path:
    env = os.environ.get("SYNTH_EARTEST_DIR")
    return Path(env).expanduser() if env else Path.home() / ".synth" / "eartest"


def read_rows(paths: list[Path]) -> list[dict]:
    """Every answer line from ``paths``; the first answer to a trial wins."""
    seen: set[str] = set()
    rows: list[dict] = []
    for path in paths:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            tid = str(row.get("trial_id"))
            if tid in seen or row.get("choice") not in ("A", "B", "same"):
                continue
            if row.get("closer") not in ("A", "B"):
                continue
            seen.add(tid)
            rows.append(row)
    return rows


def ratio_of(row: dict) -> float:
    if "ratio" in row:
        return float(row["ratio"])
    a, b = float(row["loss_a"]), float(row["loss_b"])
    lo, hi = sorted((a, b))
    return hi / max(lo, 1e-12)


def bin_of(ratio: float) -> str:
    for name, lo, hi in BINS:
        if lo <= ratio < hi:
            return name
    return BINS[-1][0]


def bootstrap_ci(outcomes: list[int], n_boot: int = N_BOOT, seed: int = 0,
                 level: float = 0.95) -> tuple[float, float] | None:
    """Percentile bootstrap CI of the mean of 0/1 outcomes (resample trials with
    replacement). Deterministic for a seed."""
    n = len(outcomes)
    if n == 0:
        return None
    rng = random.Random(seed)
    means = sorted(sum(outcomes[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot))
    lo = means[int(math.floor((1.0 - level) / 2.0 * n_boot))]
    hi = means[min(n_boot - 1, int(math.ceil((1.0 + level) / 2.0 * n_boot)) - 1)]
    return lo, hi


def p_better_than_chance(k: int, n: int) -> float | None:
    """One-sided exact binomial P(X >= k | n, 1/2)."""
    if n == 0:
        return None
    return sum(math.comb(n, j) for j in range(k, n + 1)) / 2.0 ** n


def _group(rows: list[dict], seed: int, n_boot: int) -> dict:
    clear = [r for r in rows if r["choice"] in ("A", "B")]
    agree = [r for r in clear if r["choice"] == r["closer"]]
    same = [r for r in rows if r["choice"] == "same"]
    rate = len(agree) / len(clear) if clear else None
    ci = bootstrap_ci([1 if r["choice"] == r["closer"] else 0 for r in clear], n_boot, seed)
    return {
        "trials": len(rows), "clear": len(clear), "agree": len(agree), "cant_tell": len(same),
        "rate": rate, "ci": list(ci) if ci else None,
        "strict_rate": len(agree) / len(rows) if rows else None,
    }


def summarize(rows: list[dict], seed: int = 0, n_boot: int = N_BOOT) -> dict:
    """The whole report as a dict (``--json`` prints it)."""
    overall = _group(rows, seed, n_boot)
    clear = [r for r in rows if r["choice"] in ("A", "B")]
    overall["p_chance"] = p_better_than_chance(overall["agree"], overall["clear"])
    bins = {}
    for j, (name, lo, hi) in enumerate(BINS):
        in_bin = [r for r in rows if bin_of(ratio_of(r)) == name]
        g = _group(in_bin, seed + 1 + j, n_boot)
        g["range"] = [lo, None if math.isinf(hi) else hi]
        bins[name] = g
    # When the ears and the metric disagree, what had the metric's "closer" candidate
    # changed? A module that shows up again and again is one the metric underrates.
    underrated: Counter[str] = Counter()
    for r in clear:
        if r["choice"] != r["closer"]:
            mod = r.get("a_module") if r["closer"] == "A" else r.get("b_module")
            if mod:
                underrated[mod] += 1
    times = [float(r["ms"]) / 1000.0 for r in rows if isinstance(r.get("ms"), (int, float)) and r["ms"] > 0]
    failing = [name for name, g in bins.items()
               if g["clear"] >= BIN_MIN_CLEAR and g["rate"] is not None and g["rate"] < BIN_FAIL]
    summary = {
        "overall": overall, "bins": bins, "underrated_modules": dict(underrated.most_common()),
        "median_seconds": statistics.median(times) if times else None,
        "failing_bins": failing, "sessions": sorted({str(r.get("session")) for r in rows}),
    }
    summary["verdict"] = verdict(summary)
    return summary


def _pct(x: float) -> str:
    return f"{round(100.0 * x)}%"


def verdict(summary: dict) -> str:
    """The one-line answer to FABLE's question."""
    o = summary["overall"]
    if not o["clear"]:
        return "No clear answers yet, so there is nothing to compare."
    lo, hi = o["ci"]
    line = (f"The metric agrees with your ears on {_pct(o['rate'])} of clear trials "
            f"(95% CI {round(100 * lo)}–{round(100 * hi)}%)")
    poor = o["rate"] < GOOD_RATE or lo < GOOD_LOWER
    if not poor:
        return line + "."
    fails = summary["failing_bins"]
    if fails:
        parts = [f"{name} ({_pct(summary['bins'][name]['rate'])})" for name in fails]
        joined = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
        return f"{line}; it fails on {joined} pairs."
    return f"{line}; that is weak, though no single gap bin fails on its own."


def render_text(summary: dict, sources: list[Path]) -> str:
    o = summary["overall"]
    out = [f"Ear test: {', '.join(summary['sessions'])} ({o['trials']} answers from "
           f"{len(sources)} file{'s' if len(sources) != 1 else ''})"]
    if o["trials"]:
        chance = o.get("p_chance")
        out.append(f"Clear answers: {o['clear']} · Can't tell: {o['cant_tell']} "
                   f"({_pct(o['cant_tell'] / o['trials'])})"
                   + (f" · better than chance: p = {chance:.2g}" if chance is not None else ""))
        if o["strict_rate"] is not None:
            out.append(f"Counting \"Can't tell\" as a miss, agreement is {_pct(o['strict_rate'])}.")
        out.append("")
        cant = "Can't tell"
        out.append(f"{'Gap bin':<22}{'Pairs':>6}{'Clear':>7}{'Agree':>7}{'Rate':>7}   "
                   f"{'95% CI':<10}{cant:>11}")
        for name, g in summary["bins"].items():
            lo, hi = g["range"]
            label = f"{name} ({lo:g}x+)" if hi is None else f"{name} ({lo:g}–{hi:g}x)"
            rate = _pct(g["rate"]) if g["rate"] is not None else "–"
            ci = f"{round(100 * g['ci'][0])}–{round(100 * g['ci'][1])}%" if g["ci"] else "–"
            out.append(f"{label:<22}{g['trials']:>6}{g['clear']:>7}{g['agree']:>7}{rate:>7}   {ci:<10}"
                       f"{g['cant_tell']:>11}")
        if summary["underrated_modules"]:
            parts = [f"{MODULE_LABELS.get(m, m)} ({n})" for m, n in summary["underrated_modules"].items()]
            out.append("")
            out.append("When you disagreed, the metric's \"closer\" candidate had mostly changed: "
                       + ", ".join(parts) + ". The metric may underrate changes there.")
        if summary["median_seconds"] is not None:
            out.append(f"Median answer time: {summary['median_seconds']:.1f} s.")
        out.append("")
    out.append(summary["verdict"])
    return "\n".join(out)


def find_sources(args_paths: list[str], directory: Path, every: bool) -> list[Path]:
    if every:
        return sorted(directory.glob("*.jsonl"))
    if not args_paths:
        files = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        return files[-1:]
    out = []
    for a in args_paths:
        p = Path(a).expanduser()
        if p.is_file():
            out.append(p)
        else:
            name = a[:-6] if a.endswith(".jsonl") else a
            out.append(directory / f"{name}.jsonl")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="synth-eartest-report",
        description="Does the metric agree with your ears? Scores the ear test's answers.")
    ap.add_argument("sessions", nargs="*", help="Session names or .jsonl paths (default: the newest)")
    ap.add_argument("--dir", default=None,
                    help="Answer folder (default $SYNTH_EARTEST_DIR or ~/.synth/eartest)")
    ap.add_argument("--all", action="store_true", help="Every session in the folder, combined")
    ap.add_argument("--json", action="store_true", help="Print the numbers as JSON")
    ap.add_argument("--seed", type=int, default=0, help="Bootstrap seed (default 0)")
    args = ap.parse_args(argv)

    directory = Path(args.dir).expanduser() if args.dir else eartest_dir()
    sources = find_sources(args.sessions, directory, args.all)
    missing = [p for p in sources if not p.is_file()]
    if not sources or missing:
        what = ", ".join(str(p) for p in missing) if missing else f"no answer files in {directory}"
        print(f"No ear-test answers found ({what}). Take the test at http://localhost:8766/eartest first.",
              file=sys.stderr)
        return 1
    rows = read_rows(sources)
    summary = summarize(rows, seed=args.seed)
    if args.json:
        print(json.dumps({**summary, "files": [str(p) for p in sources]}, indent=1))
    else:
        print(render_text(summary, sources))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
