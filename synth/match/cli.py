"""``synth-match`` — headless sound matching from the command line.

Drives the full capture -> score -> optimize loop on real hardware and writes the
winning patch to the synth JSON bank. This is the smoke test that proves the
engine before any GUI sits on top of it.
"""

from __future__ import annotations

import argparse
import sys

from ..midi_backend import MidiBackend
from ..patches import save_patch
from .distance import Weights
from .driver import PROBE_NOTE, SynthDriver
from .session import MatchConfig, MatchSession


def _auto_port(explicit: str | None) -> str | None:
    ports = MidiBackend.list_output_ports()
    if explicit:
        for p in ports:
            if explicit.lower() in p.lower():
                return p
        return explicit  # let connect() raise a clear error
    for p in ports:
        if "s-1" in p.lower() or "s1" in p.lower():
            return p
    return None


def _bar(pct: float, width: int = 30) -> str:
    filled = int(round(pct / 100 * width))
    return "█" * filled + "░" * (width - filled)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Match an audio file by tuning the Roland S-1.")
    ap.add_argument("target", nargs="?", help="Target audio file (wav/aiff/flac/ogg)")
    ap.add_argument("--list-devices", action="store_true", help="List audio input devices and exit")
    ap.add_argument("--list-ports", action="store_true", help="List MIDI ports and exit")
    ap.add_argument("--device", "-d", default=None, help="Audio input device (index or name substring)")
    ap.add_argument("--port", "-p", default=None, help="MIDI port (substring ok); auto-detects S-1")
    ap.add_argument("--channel", "-c", type=int, default=3, help="MIDI channel 1-16 (default 3)")
    ap.add_argument("--iters", "-n", type=int, default=40, help="Max optimizer iterations")
    ap.add_argument("--popsize", type=int, default=None, help="Candidates per generation")
    ap.add_argument("--note", type=int, default=PROBE_NOTE, help="Probe note (MIDI number)")
    ap.add_argument("--hold", type=float, default=1.2, help="Note hold seconds")
    ap.add_argument("--tail", type=float, default=1.0, help="Post-release record seconds")
    ap.add_argument("--no-effects", action="store_true", help="Exclude FX from the search")
    ap.add_argument("--optimizer", choices=["cma", "random"], default="cma")
    ap.add_argument("--no-calibrate", action="store_true", help="Skip latency calibration")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", "-o", default=None, help="Patch name to save the result as")
    args = ap.parse_args(argv)

    if args.list_devices:
        from .capture import list_input_devices

        for d in list_input_devices():
            print(f"  [{d['index']}] {d['name']}  ({d['channels']} ch @ {d['samplerate']} Hz)")
        return 0

    if args.list_ports:
        for p in MidiBackend.list_output_ports():
            print(f"  {p}")
        return 0

    if not args.target:
        ap.error("a target audio file is required (or use --list-devices/--list-ports)")

    port = _auto_port(args.port)
    if not port:
        print("No S-1 MIDI port found. Use --port or --list-ports.", file=sys.stderr)
        return 2

    midi = MidiBackend()
    midi.channel = args.channel - 1
    try:
        midi.connect(port)
    except Exception as e:  # noqa: BLE001
        print(f"Could not connect to MIDI port '{port}': {e}", file=sys.stderr)
        return 2
    print(f"MIDI: {port}  (ch {args.channel})")

    device: int | str | None = args.device
    if isinstance(device, str) and device.isdigit():
        device = int(device)

    driver = SynthDriver(midi, device=device, note=args.note, hold_s=args.hold, tail_s=args.tail)
    config = MatchConfig(
        max_iters=args.iters,
        popsize=args.popsize,
        include_effects=not args.no_effects,
        optimizer=args.optimizer,
        mode="auto",
        weights=Weights(),
        seed=args.seed,
    )
    session = MatchSession(driver, config)

    print(f"Target: {args.target}")
    session.load_target(args.target)

    if not args.no_calibrate:
        latency = session.calibrate()
        print(f"Latency calibration: {latency * 1000:.0f} ms")

    def on_progress(p) -> None:
        if not p.generation_done and not p.done:
            return
        msg = (
            f"\riter {p.iteration:>3}/{p.max_iters}  evals {p.evals:>4}  "
            f"[{_bar(p.best_closeness)}] {p.best_closeness:5.1f}%"
        )
        sys.stdout.write(msg)
        sys.stdout.flush()

    try:
        try:
            best = session.run(on_progress=on_progress)
        except KeyboardInterrupt:
            session.stop()
            best = session.best_patch()
    finally:
        # Always release the port — a crashed run must not leak the connection
        midi.disconnect()
    print()

    if not best:
        print(
            "No candidate produced usable audio — nothing to save. "
            "Check the audio device and signal chain.",
            file=sys.stderr,
        )
        return 1

    print(f"Best closeness: {session.best_closeness:.1f}%")
    name = args.out or "match"
    path = save_patch(name, best)
    print(f"Saved patch -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
