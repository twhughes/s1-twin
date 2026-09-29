"""A stand-in for the systap helper (synth/native/systap.swift) in tests: the same arguments, lines
and exit codes, and no audio at all. Standard library only, so it runs anywhere.

    fake_systap.py [--mode M] [--apps a,b] --list
    fake_systap.py [--mode M] --out PATH [--app BUNDLE_ID] [--seconds N]

Recording prints "recording 48000", waits for SIGTERM or SIGINT (or N seconds), then writes a known
take to PATH, as the real helper does: 0.3 s of digital silence, C3 (a decaying saw, 1 s), 0.5 s of
silence; mono, 16-bit, 48 kHz. ``--mode`` picks what goes wrong: silent, empty, blocked, no-app,
too-old, slow (never starts), crash (fails at the end), blocked-late (macOS said no while it recorded).
"""

from __future__ import annotations

import argparse
import json
import math
import signal
import sys
import time
import wave
from array import array

RATE = 48000
BLOCKED_WORDS = ("macOS blocked system audio. Allow it in System Settings > Privacy & Security > "
                 "Screen & System Audio Recording, then press Record again.")


def take(mode: str) -> array:
    """The known take as 16-bit samples."""
    if mode == "empty":
        return array("h")
    lead, note, tail = int(0.3 * RATE), int(1.0 * RATE), int(0.5 * RATE)
    out = array("h", bytes(2 * lead))
    for i in range(note):
        t = i / RATE
        x = 0.0 if mode == "silent" else sum(
            math.sin(2 * math.pi * h * 130.81 * t) / h for h in range(1, 9)) * 0.3 * math.exp(-t / 0.5)
        out.append(int(round(max(-1.0, min(1.0, x)) * 32767)))
    out.extend(array("h", bytes(2 * tail)))
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", default="ok")
    p.add_argument("--apps", default="com.apple.logic10,com.apple.Safari")
    p.add_argument("--list", action="store_true")
    p.add_argument("--out")
    p.add_argument("--app")
    p.add_argument("--seconds", type=float, default=30.0)
    a = p.parse_args()

    if a.list:
        bundles = [b for b in a.apps.split(",") if b]
        apps = [{"bundle": b, "pid": 100 + i, "output": False} for i, b in enumerate(bundles)]
        print(json.dumps({"permission": "granted", "apps": apps}), flush=True)
        return 0
    if not a.out:
        print("usage: systap --out PATH [--app BUNDLE_ID] [--seconds N] | systap --list", file=sys.stderr)
        return 2
    if a.mode == "blocked":
        print(BLOCKED_WORDS, file=sys.stderr)
        return 3
    if a.mode == "too-old":
        print("Recording this Mac's sound needs macOS 14.2 or later.", file=sys.stderr)
        return 5
    if a.app and (a.mode == "no-app" or a.app not in a.apps.split(",")):
        print(f"No running app has the bundle id {a.app}.", file=sys.stderr)
        return 4

    stopped = []
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stopped.append(True))
    if a.mode != "slow":
        print(f"recording {RATE}", flush=True)
    end = time.monotonic() + a.seconds
    while not stopped and time.monotonic() < end:
        time.sleep(0.01)
    if a.mode == "slow":
        return 1
    if a.mode == "crash":
        print("Starting the recording failed (Core Audio error '!obj').", file=sys.stderr)
        return 1
    samples = take(a.mode)
    if sys.byteorder == "big":        # a WAV is little-endian
        samples.byteswap()
    with wave.open(a.out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.tobytes())
    print(f"wrote {len(samples)} frames", flush=True)
    if a.mode == "blocked-late":      # "Don't Allow" answered while it recorded
        print(BLOCKED_WORDS, file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
