"""`s1` — start the S-1 web cockpit.

The happy path is zero-argument: start the server, open the browser, and let
the sync engine find the hardware. Everything else (MIDI, audio, keyboards)
is hot-plugged automatically.
"""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="s1",
        description="Roland S-1 cockpit — the whole synth in a browser tab.",
    )
    parser.add_argument(
        "--list-ports", action="store_true",
        help="List available MIDI ports and exit",
    )
    parser.add_argument(
        "--host", default=None, help="Bind address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port", type=int, default=None, help="HTTP port (default: 8766)",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="Don't auto-open the browser tab",
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

    from .web.server import run

    run(host=args.host, port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
