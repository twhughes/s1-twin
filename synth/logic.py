"""``synth-logic`` — drive Logic Pro's transport from the cockpit (M4).

Logic is the chassis (chassis-spec C7/C8). When its template has "Listen to
MMC" on, it obeys **MIDI Machine Control** transport commands. This module
sends those commands over the ``HQ Clock`` IAC bus (C5), so the cockpit — and
any agent on the REST API — can roll, stop, and arm Logic without touching the
keyboard.

MMC commands are System Exclusive messages of the form
``F0 7F <device> 06 <command> F7``; device ``0x7F`` is the broadcast address.
Only three transport verbs exist here — there is **no** tempo-set message (C4);
tempo is typed in Logic, which then broadcasts it.
"""

from __future__ import annotations

import argparse
import sys

import mido

from .midi_backend import MidiBackend

HQ_CLOCK_PORT = "HQ Clock"

# MMC real-time transport commands, full sysex including the 0xF0/0xF7 framing.
# device-id 0x7F = broadcast/all. Deferred-play 0x03 (not plain 0x02) is the
# safe roll; record-strobe 0x06 punches in.
MMC_STOP = bytes([0xF0, 0x7F, 0x7F, 0x06, 0x01, 0xF7])
MMC_PLAY = bytes([0xF0, 0x7F, 0x7F, 0x06, 0x03, 0xF7])
MMC_RECORD = bytes([0xF0, 0x7F, 0x7F, 0x06, 0x06, 0xF7])

MMC_COMMANDS: dict[str, bytes] = {
    "play": MMC_PLAY,
    "stop": MMC_STOP,
    "record": MMC_RECORD,
}


class LogicTransportError(RuntimeError):
    """The transport command could not be delivered (missing bus or send failure)."""


def send_transport(action: str, port_name: str = HQ_CLOCK_PORT, midi_module=None) -> bytes:
    """Send one MMC transport command to ``port_name`` and return the bytes sent.

    ``action`` is ``"play"``, ``"stop"``, or ``"record"``. A missing bus fails
    loud with a one-line setup pointer (C5) rather than silently doing nothing.
    ``midi_module`` lets tests inject a fake mido-shaped module.
    """
    try:
        data = MMC_COMMANDS[action]
    except KeyError:
        raise LogicTransportError(f"unknown transport action {action!r} (play/stop/record)")

    mod = midi_module or mido
    ports = mod.get_output_names()
    if port_name not in ports:
        found = ", ".join(ports) if ports else "no output ports"
        raise LogicTransportError(
            f"MIDI output port {port_name!r} not found — create the IAC bus in "
            f"Audio MIDI Setup (chassis-spec C5). Available: {found}"
        )

    backend = MidiBackend(midi_module=mod)
    backend.connect(port_name)
    try:
        if not backend.send_sysex(data):
            raise LogicTransportError(
                f"send to {port_name!r} failed — the bus disappeared mid-send"
            )
    finally:
        # Close without an all-notes-off flush: the clock bus carries transport,
        # not notes, and a stray CC there is noise.
        backend.drop_ports()
    return data


def play(port_name: str = HQ_CLOCK_PORT, midi_module=None) -> bytes:
    """Roll Logic (MMC deferred-play)."""
    return send_transport("play", port_name, midi_module)


def stop(port_name: str = HQ_CLOCK_PORT, midi_module=None) -> bytes:
    """Stop Logic (MMC stop)."""
    return send_transport("stop", port_name, midi_module)


def record(port_name: str = HQ_CLOCK_PORT, midi_module=None) -> bytes:
    """Arm/punch-in Logic (MMC record-strobe)."""
    return send_transport("record", port_name, midi_module)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="synth-logic",
        description="Drive Logic Pro's transport with MMC over the HQ Clock bus (chassis-spec M4).",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    for verb, help_text in (
        ("play", "Roll Logic (MMC deferred-play)."),
        ("stop", "Stop Logic (MMC stop)."),
        ("record", "Arm/punch-in Logic (MMC record-strobe)."),
    ):
        p = sub.add_parser(verb, help=help_text)
        p.add_argument(
            "--port", default=HQ_CLOCK_PORT,
            help=f"Output port to send MMC to (default {HQ_CLOCK_PORT!r})",
        )

    args = ap.parse_args(argv)
    try:
        data = send_transport(args.cmd, args.port)
    except LogicTransportError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"{args.cmd} -> {args.port}: {' '.join(f'{b:02X}' for b in data)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
