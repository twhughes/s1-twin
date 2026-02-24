"""Take an SVG screenshot of the app for the README."""

import asyncio
from unittest.mock import MagicMock, patch

from s1tui.app import S1App
from s1tui.sequence import Note, Sequence
from s1tui.widgets.piano_roll import PianoRoll


async def main():
    with patch("s1tui.app.MidiBackend") as MockMidi:
        instance = MagicMock()
        instance.connected = True
        instance.port_name = "S-1 MIDI IN"
        instance.channel = 0
        instance.poll_input.return_value = []
        MockMidi.return_value = instance
        MockMidi.list_output_ports.return_value = ["S-1 MIDI IN"]

        app = S1App()

        async with app.run_test(size=(100, 36)) as pilot:
            # Switch to Seq tab
            tabs = app.query_one("#top-tabs")
            tabs.active = "tab-seq"
            await pilot.pause()

            # Load a fun bassline into the piano roll
            roll = app.query_one("#piano-roll", PianoRoll)
            seq = Sequence(
                notes=[
                    # A little bassline pattern
                    Note(step=0, pitch=36, velocity=100, duration=2),   # C2
                    Note(step=2, pitch=36, velocity=90, duration=1),    # C2
                    Note(step=3, pitch=38, velocity=85, duration=1),    # D2
                    Note(step=4, pitch=40, velocity=100, duration=2),   # E2
                    Note(step=6, pitch=43, velocity=95, duration=1),    # G2
                    Note(step=7, pitch=41, velocity=80, duration=1),    # F2
                    Note(step=8, pitch=36, velocity=100, duration=2),   # C2
                    Note(step=10, pitch=38, velocity=90, duration=2),   # D2
                    Note(step=12, pitch=40, velocity=100, duration=1),  # E2
                    Note(step=13, pitch=43, velocity=95, duration=2),   # G2
                    Note(step=15, pitch=48, velocity=100, duration=1),  # C3
                ],
                steps=16,
                bpm=128.0,
                step_resolution="1/16",
            )
            roll.set_sequence(seq)

            # Simulate playhead at step 6
            roll.playhead = 6

            # Move cursor to a note for the info line
            roll.cursor_step = 4
            roll.cursor_pitch = 40

            await pilot.pause()
            await pilot.pause()

            # Export SVG screenshot
            path = app.save_screenshot("screenshot.svg", path="./")
            print(f"Saved: {path}")


asyncio.run(main())
