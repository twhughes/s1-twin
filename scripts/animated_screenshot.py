"""Generate animated SVGs of the app for the README."""

import asyncio
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

from s1tui.app import S1App
from s1tui.sequence import Note, Sequence
from s1tui.widgets.piano_roll import PianoRoll

OUTDIR = Path("./docs")

BASSLINE = [
    Note(step=0, pitch=36, velocity=100, duration=2),
    Note(step=2, pitch=36, velocity=90, duration=1),
    Note(step=3, pitch=38, velocity=85, duration=1),
    Note(step=4, pitch=40, velocity=100, duration=2),
    Note(step=6, pitch=43, velocity=95, duration=1),
    Note(step=7, pitch=41, velocity=80, duration=1),
    Note(step=8, pitch=36, velocity=100, duration=2),
    Note(step=10, pitch=38, velocity=90, duration=2),
    Note(step=12, pitch=40, velocity=100, duration=1),
    Note(step=13, pitch=43, velocity=95, duration=2),
    Note(step=15, pitch=48, velocity=100, duration=1),
]


def make_mock_midi():
    """Create a mocked MidiBackend context."""
    p = patch("s1tui.app.MidiBackend")
    MockMidi = p.start()
    instance = MagicMock()
    instance.connected = True
    instance.port_name = "S-1 MIDI IN"
    instance.channel = 0
    instance.poll_input.return_value = []
    MockMidi.return_value = instance
    MockMidi.list_output_ports.return_value = ["S-1 MIDI IN"]
    return p, instance


async def capture_piano_roll_frames():
    """Capture one SVG per playhead step on the Seq tab."""
    p, _ = make_mock_midi()
    frames = []
    try:
        app = S1App()
        async with app.run_test(size=(100, 36)) as pilot:
            tabs = app.query_one("#top-tabs")
            tabs.active = "tab-seq"
            await pilot.pause()

            roll = app.query_one("#piano-roll", PianoRoll)
            seq = Sequence(notes=list(BASSLINE), steps=16, bpm=128.0)
            roll.set_sequence(seq)
            await pilot.pause()

            tmpdir = Path("/tmp/s1tui_frames_seq")
            tmpdir.mkdir(exist_ok=True)

            for step in range(16):
                roll.playhead = step
                roll.cursor_step = step
                notes_here = seq.notes_at_step(step)
                roll.cursor_pitch = notes_here[0].pitch if notes_here else 36
                await pilot.pause()
                await pilot.pause()
                fname = f"frame_{step:02d}.svg"
                app.save_screenshot(fname, path=str(tmpdir))
                frames.append(tmpdir / fname)
    finally:
        p.stop()
    return frames


async def capture_panel_frames():
    """Capture frames on the Panel tab sweeping filter cutoff."""
    p, _ = make_mock_midi()
    frames = []
    try:
        app = S1App()
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.pause()
            await pilot.pause()

            # Focus the filter frequency slider (CC 74)
            freq_widget = app._widgets.get(74)
            if freq_widget:
                freq_widget.focus()
                await pilot.pause()

            tmpdir = Path("/tmp/s1tui_frames_panel")
            tmpdir.mkdir(exist_ok=True)

            # Sweep filter cutoff down and back up — 24 frames
            sweep = list(range(127, 20, -9)) + list(range(20, 128, 9))
            for i, val in enumerate(sweep):
                # Update filter freq
                if 74 in app._widgets:
                    app._widgets[74].value = val
                # Also wiggle resonance a bit for visual interest
                if 71 in app._widgets:
                    res = 40 + int(30 * ((i % 6) / 5))
                    app._widgets[71].value = res
                await pilot.pause()
                await pilot.pause()
                fname = f"frame_{i:02d}.svg"
                app.save_screenshot(fname, path=str(tmpdir))
                frames.append(tmpdir / fname)
    finally:
        p.stop()
    return frames


def build_animated_svg(frame_paths: list[Path], output: Path, frame_duration: float = 0.25):
    """Combine frame SVGs into one animated SVG with CSS keyframes."""
    first_text = frame_paths[0].read_text()

    # Get viewBox
    vb_match = re.search(r'viewBox="([^"]+)"', first_text)
    viewbox = vb_match.group(1) if vb_match else "0 0 1238 928.4"

    # Collect each frame's full inner content INCLUDING its own <style> block,
    # since each Textual render generates unique class name prefixes.
    frame_contents = []
    for path in frame_paths:
        svg_text = path.read_text()
        inner = re.sub(r"<svg[^>]*>", "", svg_text, count=1)
        inner = re.sub(r"</svg>\s*$", "", inner)
        # Keep <!-- comments --> but that's fine
        frame_contents.append(inner.strip())

    n = len(frame_contents)
    total = n * frame_duration

    anim_css = "\n        .frame { opacity: 0; }\n"
    for i in range(n):
        s = (i / n) * 100
        e = ((i + 1) / n) * 100
        anim_css += (
            f"        @keyframes f{i} {{\n"
            f"            0%,{s:.2f}% {{ opacity:0 }}\n"
            f"            {s+0.01:.2f}%,{e:.2f}% {{ opacity:1 }}\n"
            f"            {min(e+0.01,100):.2f}%,100% {{ opacity:0 }}\n"
            f"        }}\n"
            f"        .f{i} {{ animation:f{i} {total}s steps(1) infinite }}\n"
        )

    parts = [
        f'<svg class="rich-terminal" viewBox="{viewbox}" xmlns="http://www.w3.org/2000/svg">',
        f"<style>{anim_css}</style>",
    ]
    for i, content in enumerate(frame_contents):
        parts.append(f'<g class="frame f{i}">{content}</g>')
    parts.append("</svg>")

    output.parent.mkdir(exist_ok=True)
    output.write_text("\n".join(parts))
    size_kb = output.stat().st_size // 1024
    print(f"  -> {output} ({size_kb}K, {n} frames, {total:.1f}s loop)")


async def main():
    OUTDIR.mkdir(exist_ok=True)

    print("Capturing piano roll playback...")
    seq_frames = await capture_piano_roll_frames()
    build_animated_svg(seq_frames, OUTDIR / "screenshot.svg", frame_duration=0.25)

    print("Capturing panel filter sweep...")
    panel_frames = await capture_panel_frames()
    build_animated_svg(panel_frames, OUTDIR / "panel.svg", frame_duration=0.15)

    print("Done!")


asyncio.run(main())
