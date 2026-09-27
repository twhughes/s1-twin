#!/usr/bin/env python3
"""Export the cockpit's ``/api/schema`` to ``synth/web/static/core/schema.json``.

The static page (no server) builds the plate from that file; the cockpit builds it
from the live ``/api/schema``. ``tests/test_plate_schema.py`` asserts the two agree,
so re-run this after any change to ``synth/data/s1.json`` or ``synth/schema.py``::

    python tools/export_schema.py          # rewrite the file
    python tools/export_schema.py --check  # exit 1 if the file is stale

The export is what a freshly started cockpit serves: every value at its factory
default. It never opens a MIDI port or an audio device.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "synth" / "web" / "static" / "core" / "schema.json"


class _NoPorts:
    """A mido-shaped module with no ports, so the throwaway engine touches nothing."""

    @staticmethod
    def get_output_names() -> list[str]:
        return []

    @staticmethod
    def get_input_names() -> list[str]:
        return []


def schema_payload() -> dict:
    """``/api/schema`` as a fresh, never-started engine serves it."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))   # this checkout's package, not an installed copy
    import synth.engine as engine_module
    from synth.engine import S1Engine
    from synth.web import server

    saved = engine_module.ENGINE
    engine_module.ENGINE = S1Engine(midi_module=_NoPorts(), audio_auto=False)
    try:
        return server.get_schema()
    finally:
        engine_module.ENGINE = saved


def render(payload: dict) -> str:
    """The file's exact text (stable, so a re-export of an unchanged schema is a no-op)."""
    return json.dumps(payload, indent=1, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if schema.json is stale")
    args = parser.parse_args(argv)
    text = render(schema_payload())
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(f"{OUT.relative_to(ROOT)} is stale: run python tools/export_schema.py")
            return 1
        print(f"{OUT.relative_to(ROOT)} is current")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
