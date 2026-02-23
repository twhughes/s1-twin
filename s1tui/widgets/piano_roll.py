"""Piano roll grid widget for sequence display and editing."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.events import Click
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from ..sequence import Note, Sequence

# MIDI note names
_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def _note_name(pitch: int) -> str:
    """Convert MIDI pitch to note name like 'C4'."""
    octave = (pitch // 12) - 1
    name = _NOTE_NAMES[pitch % 12]
    return f"{name}{octave}"


class PianoRoll(Widget, can_focus=True):
    """A grid-based piano roll for viewing and editing a Sequence."""

    DEFAULT_CSS = """
    PianoRoll {
        height: 1fr;
        width: 1fr;
        min-height: 16;
    }
    """

    BINDINGS = [
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("left", "cursor_left", "Left", show=False),
        Binding("right", "cursor_right", "Right", show=False),
        Binding("enter", "toggle_note", "Toggle", show=False),
        Binding("z", "toggle_note", "Toggle", show=False),
        Binding("plus,equal", "velocity_up", "+Vel", show=False),
        Binding("minus,underscore", "velocity_down", "-Vel", show=False),
        Binding("bracketright", "duration_up", "+Dur", show=False),
        Binding("bracketleft", "duration_down", "-Dur", show=False),
    ]

    cursor_step: reactive[int] = reactive(0)
    cursor_pitch: reactive[int] = reactive(36)  # C2
    playhead: reactive[int] = reactive(-1)  # -1 = not playing

    class NoteToggled(Message):
        """Fired when a note is added or removed."""
        def __init__(self, step: int, pitch: int) -> None:
            super().__init__()
            self.step = step
            self.pitch = pitch

    def __init__(
        self,
        sequence: Sequence | None = None,
        visible_rows: int = 16,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.sequence = sequence or Sequence()
        self._visible_rows = visible_rows
        # Pitch range: center around C2 (MIDI 36)
        self._pitch_top = 36 + visible_rows // 2  # center on C2
        self._cell_width = 3  # chars per step cell

    @property
    def _pitch_bottom(self) -> int:
        return self._pitch_top - self._visible_rows + 1

    def set_sequence(self, seq: Sequence) -> None:
        """Replace the current sequence and re-render."""
        self.sequence = seq
        # Center pitch range on note content if any
        if seq.notes:
            pitches = [n.pitch for n in seq.notes]
            mid = (min(pitches) + max(pitches)) // 2
            self._pitch_top = min(127, mid + self._visible_rows // 2)
            self.cursor_pitch = mid
        self.cursor_step = 0
        self.refresh()

    def render(self) -> str:
        """Render the piano roll grid as text."""
        seq = self.sequence
        cw = self._cell_width
        lines: list[str] = []

        # Build a set of (step, pitch) for quick lookup
        note_map: dict[tuple[int, int], Note] = {}
        for note in seq.notes:
            for s in range(note.step, min(note.step + note.duration, seq.steps)):
                note_map[(s, note.pitch)] = note

        for pitch in range(self._pitch_top, self._pitch_bottom - 1, -1):
            name = _note_name(pitch).rjust(3)
            row_chars: list[str] = [name, " "]

            for step in range(seq.steps):
                is_cursor = step == self.cursor_step and pitch == self.cursor_pitch
                is_playhead = step == self.playhead
                note = note_map.get((step, pitch))

                if note is not None:
                    # Note start vs continuation
                    if step == note.step:
                        cell = "█" * cw
                    else:
                        cell = "▓" * cw
                    if is_cursor:
                        cell = f"[reverse green]{cell}[/reverse green]"
                    elif is_playhead:
                        cell = f"[bold cyan]{cell}[/bold cyan]"
                    else:
                        cell = f"[green]{cell}[/green]"
                else:
                    # Empty cell
                    if is_cursor:
                        cell = f"[reverse]{'·' * cw}[/reverse]"
                    elif is_playhead:
                        cell = f"[bold cyan]{'▎' + '·' * (cw - 1)}[/bold cyan]"
                    else:
                        # Alternate shading for black/white keys
                        is_black = pitch % 12 in (1, 3, 6, 8, 10)
                        if is_black:
                            cell = f"[dim]{'─' * cw}[/dim]"
                        else:
                            cell = f"[dim]{'·' * cw}[/dim]"

                row_chars.append(cell)

            lines.append("".join(row_chars))

        # Step numbers footer
        footer_parts = ["    "]
        for step in range(seq.steps):
            num = str(step + 1)
            if step == self.playhead:
                footer_parts.append(f"[bold cyan]{num:^{cw}}[/bold cyan]")
            elif step == self.cursor_step:
                footer_parts.append(f"[reverse]{num:^{cw}}[/reverse]")
            else:
                footer_parts.append(f"[dim]{num:^{cw}}[/dim]")
        lines.append("".join(footer_parts))

        # Info line
        note_at_cursor = seq.note_at(self.cursor_step, self.cursor_pitch)
        if note_at_cursor:
            info = (
                f" {seq.bpm:.0f} BPM | Step {self.cursor_step + 1}/{seq.steps} | "
                f"{_note_name(self.cursor_pitch)} | "
                f"Vel: {note_at_cursor.velocity} | Dur: {note_at_cursor.duration}"
            )
        else:
            info = (
                f" {seq.bpm:.0f} BPM | Step {self.cursor_step + 1}/{seq.steps} | "
                f"{_note_name(self.cursor_pitch)} | (empty)"
            )
        lines.append(info)

        return "\n".join(lines)

    def watch_cursor_step(self) -> None:
        self.refresh()

    def watch_cursor_pitch(self) -> None:
        self.refresh()

    def watch_playhead(self) -> None:
        self.refresh()

    # ── Mouse editing ──

    _LABEL_COLS = 4  # "C#2 " = 4 chars before grid starts

    def _click_to_grid(self, x: int, y: int) -> tuple[int, int] | None:
        """Convert widget-relative click coords to (step, pitch) or None."""
        col = x - self._LABEL_COLS
        if col < 0:
            return None
        step = col // self._cell_width
        if step < 0 or step >= self.sequence.steps:
            return None
        pitch = self._pitch_top - y
        if pitch < 0 or pitch > 127:
            return None
        return step, pitch

    def on_click(self, event: Click) -> None:
        """Click on a grid cell to move cursor there and toggle the note."""
        result = self._click_to_grid(event.x, event.y)
        if result is None:
            return
        step, pitch = result
        self.cursor_step = step
        self.cursor_pitch = pitch
        # Scroll into view if needed
        if pitch > self._pitch_top:
            self._pitch_top = min(127, pitch + self._visible_rows // 2)
        elif pitch < self._pitch_bottom:
            self._pitch_top = max(self._visible_rows - 1, pitch + self._visible_rows // 2)
        self.sequence.toggle_note(step, pitch)
        self.post_message(self.NoteToggled(step, pitch))
        self.refresh()

    # ── Cursor actions ──

    def action_cursor_up(self) -> None:
        if self.cursor_pitch < 127:
            self.cursor_pitch += 1
            # Scroll view if needed
            if self.cursor_pitch > self._pitch_top:
                self._pitch_top = min(127, self._pitch_top + 1)

    def action_cursor_down(self) -> None:
        if self.cursor_pitch > 0:
            self.cursor_pitch -= 1
            if self.cursor_pitch < self._pitch_bottom:
                self._pitch_top = max(self._visible_rows - 1, self._pitch_top - 1)

    def action_cursor_left(self) -> None:
        if self.cursor_step > 0:
            self.cursor_step -= 1

    def action_cursor_right(self) -> None:
        if self.cursor_step < self.sequence.steps - 1:
            self.cursor_step += 1

    # ── Note editing ──

    def action_toggle_note(self) -> None:
        self.sequence.toggle_note(self.cursor_step, self.cursor_pitch)
        self.post_message(self.NoteToggled(self.cursor_step, self.cursor_pitch))
        self.refresh()

    def action_velocity_up(self) -> None:
        note = self.sequence.note_at(self.cursor_step, self.cursor_pitch)
        if note:
            note.velocity = min(127, note.velocity + 10)
            self.refresh()

    def action_velocity_down(self) -> None:
        note = self.sequence.note_at(self.cursor_step, self.cursor_pitch)
        if note:
            note.velocity = max(1, note.velocity - 10)
            self.refresh()

    def action_duration_up(self) -> None:
        note = self.sequence.note_at(self.cursor_step, self.cursor_pitch)
        if note:
            max_dur = self.sequence.steps - note.step
            note.duration = min(max_dur, note.duration + 1)
            self.refresh()

    def action_duration_down(self) -> None:
        note = self.sequence.note_at(self.cursor_step, self.cursor_pitch)
        if note:
            note.duration = max(1, note.duration - 1)
            self.refresh()
