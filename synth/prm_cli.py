"""``synth-prm`` — move S-1 patterns to and from Logic as standard MIDI files.

The bridge is contract C3: a pattern travels as a plain ``.mid`` file, never a
bespoke format. Two directions:

- ``export-mid`` reads a ``.PRM`` pattern, writes a ``.mid`` with the notes plus
  the pattern's motion lanes as control-change events on the S-1's synth
  channel — drag it onto the S-1 Rig track in Logic and the hardware replays
  its own pattern, knob motion and all.
- ``import-mid`` reads a ``.mid`` and writes a RESTORE-ready ``.PRM`` for the
  device's disk mode. The device caps a pattern at 64 steps and 4 notes per
  step; both limits are reported, never applied silently.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .engine import DEFAULT_SYNTH_CHANNEL
from .prm import PrmFile, build_pattern, pattern_filename
from .sequence import load_midi, save_midi


def _export(args: argparse.Namespace) -> int:
    src = Path(args.pattern)
    try:
        prm = PrmFile.load(src)
    except (OSError, ValueError) as e:
        print(f"Could not read pattern '{src}': {e}", file=sys.stderr)
        return 2

    seq = prm.to_sequence()
    events = prm.to_motion_events()
    channel = max(1, min(16, args.channel)) - 1  # 1-indexed flag -> 0-indexed

    out = Path(args.out) if args.out else src.with_suffix(".mid")
    save_midi(seq, out, cc_events=events, channel=channel)

    lanes = sorted({cc for _, cc, _ in events})
    print(f"Exported {len(seq.notes)} notes across {seq.steps} steps -> {out}")
    print(f"  tempo {seq.bpm:g} bpm, grid {seq.step_resolution}, MIDI channel {args.channel}")
    if lanes:
        print(f"  motion: {len(events)} CC events on CC {', '.join(map(str, lanes))}")
    else:
        print("  motion: no lanes recorded")
    return 0


def _parse_slot(slot: str) -> tuple[int, int]:
    bank_s, _, slot_s = slot.partition("-")
    bank, slot_n = int(bank_s), int(slot_s)
    if not 1 <= bank <= 4 or not 1 <= slot_n <= 16:
        raise ValueError("slot must be <bank 1-4>-<slot 1-16>, e.g. 2-05")
    return bank, slot_n


def _import(args: argparse.Namespace) -> int:
    src = Path(args.infile)
    try:
        bank, slot = _parse_slot(args.slot)
    except ValueError as e:
        print(f"Bad --slot: {e}", file=sys.stderr)
        return 2

    try:
        seq = load_midi(src, quantize=args.quantize)
    except (OSError, ValueError) as e:
        print(f"Could not read MIDI '{src}': {e}", file=sys.stderr)
        return 2

    # No silent caps: surface both device limits before writing (spec guardrail).
    if seq.dropped_notes:
        print(
            f"WARNING: {seq.dropped_notes} note(s) past the 64-step limit were dropped.",
            file=sys.stderr,
        )
    poly = seq.poly_warnings()
    if poly:
        steps = ", ".join(str(s) for s in poly)
        print(
            f"WARNING: steps {steps} hold more than 4 notes; each is truncated to 4 "
            "(the device's per-step poly ceiling).",
            file=sys.stderr,
        )

    prm = build_pattern({}, seq)
    out_dir = Path(args.out) if args.out else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    written = prm.save(out_dir / pattern_filename(bank, slot))

    print(f"Imported {len(seq.notes)} notes across {seq.steps} steps -> {written}")
    print(f"  RESTORE-ready for bank {bank}, slot {slot} (drop into the S-1's RESTORE/ folder)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="synth-prm",
        description="Bridge S-1 .PRM patterns to and from Logic as standard MIDI files (C3).",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    exp = sub.add_parser("export-mid", help="Write a .PRM pattern out as a .mid file.")
    exp.add_argument("pattern", help="Source S-1 .PRM pattern file")
    exp.add_argument("out", nargs="?", default=None, help="Output .mid (default: input name .mid)")
    exp.add_argument(
        "--channel", type=int, default=DEFAULT_SYNTH_CHANNEL + 1,
        help=f"S-1 synth MIDI channel 1-16 (default {DEFAULT_SYNTH_CHANNEL + 1})",
    )
    exp.set_defaults(func=_export)

    imp = sub.add_parser("import-mid", help="Write a .mid in as a RESTORE-ready .PRM.")
    imp.add_argument("infile", help="Source .mid file")
    imp.add_argument("--slot", required=True, help="Target device slot <bank>-<slot>, e.g. 2-05")
    imp.add_argument("--out", default=None, help="Output directory (default: current dir)")
    imp.add_argument("--quantize", default="1/16", help="Import grid (default 1/16)")
    imp.set_defaults(func=_import)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
