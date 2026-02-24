"""CLI entry point for S1 TUI."""

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Terminal UI controller for the Roland S-1")
    parser.add_argument(
        "--list-ports", action="store_true",
        help="List available MIDI ports and exit",
    )
    parser.add_argument(
        "--port", "-p", type=str, default=None,
        help="Auto-connect to this MIDI port on startup",
    )
    parser.add_argument(
        "--channel", "-c", type=int, default=3,
        help="MIDI channel (1-16, default: 3 — the S-1's factory setting)",
    )
    args = parser.parse_args()

    if args.list_ports:
        from .midi_backend import MidiBackend
        print("MIDI Output Ports:")
        for p in MidiBackend.list_output_ports():
            print(f"  {p}")
        print("\nMIDI Input Ports:")
        for p in MidiBackend.list_input_ports():
            print(f"  {p}")
        sys.exit(0)

    from .app import S1App

    app = S1App()
    app.midi.channel = args.channel - 1  # 0-indexed internally

    if args.port:
        try:
            app.midi.connect(args.port)
        except Exception as e:
            print(f"Warning: Could not connect to '{args.port}': {e}", file=sys.stderr)

    app.run()


if __name__ == "__main__":
    main()
