"""Piano roll grid widget for sequence display and editing."""

from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.events import Click
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget

from ..sequence import Note, Sequence
from .. import theme as T

_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_BLACK_KEYS = {1, 3, 6, 8, 10}

# Subtle alternating 4-step bands so the eye can count bars.
_BAND_A = "#14112a"
_BAND_B = "#1a1638"
_BARLINE = "#4a4378"


def _note_name(pitch: int) -> str:
    octave = (pitch // 12) - 1
    return f"{_NOTE_NAMES[pitch % 12]}{octave}"


class PianoRoll(Widget, can_focus=True):
    """A grid-based piano roll for viewing and editing a Sequence."""

    DEFAULT_CSS = """
    PianoRoll { height: 1fr; width: 1fr; min-height: 16; }
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
    cursor_pitch: reactive[int] = reactive(48)  # C3
    playhead: reactive[int] = reactive(-1)  # -1 = not playing

    _LABEL_COLS = 4  # name(3) + key strip(1)

    class NoteToggled(Message):
        def __init__(self, step: int, pitch: int) -> None:
            super().__init__()
            self.step = step
            self.pitch = pitch

    def __init__(self, sequence: Sequence | None = None, visible_rows: int = 16, **kwargs) -> None:
        super().__init__(**kwargs)
        self.sequence = sequence or Sequence()
        self._visible_rows = visible_rows
        self._pitch_top = 48 + visible_rows // 2  # center on C3
        self._cell_width = 3

    @property
    def _pitch_bottom(self) -> int:
        return self._pitch_top - self._visible_rows + 1

    def _cw(self) -> int:
        """Cell width that fills the available card width."""
        steps = max(1, self.sequence.steps)
        avail = (self.size.width or 60) - self._LABEL_COLS
        self._cell_width = max(2, min(6, avail // steps))
        return self._cell_width

    def set_sequence(self, seq: Sequence) -> None:
        self.sequence = seq
        if seq.notes:
            pitches = [n.pitch for n in seq.notes]
            mid = (min(pitches) + max(pitches)) // 2
            self._pitch_top = min(127, mid + self._visible_rows // 2)
            self.cursor_pitch = mid
        self.cursor_step = 0
        self.refresh()

    # ── colour helpers ──

    @staticmethod
    def _vel_color(vel: int) -> str:
        """Quiet → loud mapped along dark-teal → bright cyan-white."""
        t = max(0.0, min(1.0, (vel - 1) / 126))
        return T.blend("#0e8f86", "#b6ffff", t)

    @staticmethod
    def _band(step: int) -> str:
        return _BAND_A if (step // 4) % 2 == 0 else _BAND_B

    # ── render ──

    def render(self) -> Text:
        seq = self.sequence
        cw = self._cw()
        out = Text(no_wrap=True, overflow="crop")

        note_map: dict[tuple[int, int], Note] = {}
        for note in seq.notes:
            for s in range(note.step, min(note.step + note.duration, seq.steps)):
                note_map[(s, note.pitch)] = note

        for pitch in range(self._pitch_top, self._pitch_bottom - 1, -1):
            is_black = pitch % 12 in _BLACK_KEYS
            on_cursor_row = pitch == self.cursor_pitch
            # gutter: note name + key strip
            name = _note_name(pitch).rjust(3)
            name_style = T.CYAN if on_cursor_row else (T.MUTED if is_black else T.FG)
            out.append(name, style=("bold " + name_style) if on_cursor_row else name_style)
            out.append("█", style="#2a2740" if is_black else "#cdc8ee")

            for step in range(seq.steps):
                bg = self._band(step)
                is_cursor = step == self.cursor_step and on_cursor_row
                is_ph = step == self.playhead
                note = note_map.get((step, pitch))
                if is_ph:
                    bg = T.blend(T.MAGENTA, T.BG, 0.35)

                if note is not None:
                    vc = self._vel_color(note.velocity)
                    if is_cursor:
                        out.append("█" * cw, style=f"bold {T.FG} on {T.CYAN}")
                    elif step == note.step:
                        out.append("█" * cw, style=f"{vc} on {bg}")
                    else:  # sustained
                        out.append("▬" * cw, style=f"{T.blend(vc, T.BG, 0.3)} on {bg}")
                else:
                    if is_cursor:
                        out.append(" ◆ ", style=f"bold {T.CYAN} on {bg}")
                    elif step % 4 == 0:
                        out.append("▏", style=f"{_BARLINE} on {bg}")
                        out.append("··", style=f"{T.FAINT} on {bg}")
                    else:
                        out.append("·" * cw, style=f"{T.FAINT} on {bg}")
            out.append("\n")

        # step-number footer
        out.append(" " * self._LABEL_COLS)
        for step in range(seq.steps):
            num = str(step + 1)
            if step == self.playhead:
                style = f"bold {T.MAGENTA}"
            elif step % 4 == 0:
                style = f"bold {T.CYAN}"
            else:
                style = T.MUTED
            out.append(f"{num:^{cw}}", style=style)
        out.append("\n")

        # info line
        note_at = seq.note_at(self.cursor_step, self.cursor_pitch)
        info = Text()
        info.append(f" ▸ {seq.bpm:.0f} BPM", style=f"bold {T.CYAN}")
        info.append("   step ", style=T.MUTED)
        info.append(f"{self.cursor_step + 1}/{seq.steps}", style=T.FG)
        info.append("   ", style=T.MUTED)
        info.append(_note_name(self.cursor_pitch), style=f"bold {T.VIOLET}")
        if note_at:
            info.append(f"   vel {note_at.velocity}  dur {note_at.duration}", style=T.GOLD)
        else:
            info.append("   —", style=T.FAINT)
        out.append_text(info)
        return out

    def watch_cursor_step(self) -> None:
        self.refresh()

    def watch_cursor_pitch(self) -> None:
        self.refresh()

    def watch_playhead(self) -> None:
        self.refresh()

    # ── scrolling ──

    def _ensure_cursor_visible(self) -> None:
        if self.cursor_pitch > self._pitch_top:
            self._pitch_top = min(127, self.cursor_pitch)
        elif self.cursor_pitch < self._pitch_bottom:
            self._pitch_top = self.cursor_pitch + self._visible_rows - 1

    # ── mouse editing ──

    def _click_to_grid(self, x: int, y: int) -> tuple[int, int] | None:
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
        result = self._click_to_grid(event.x, event.y)
        if result is None:
            return
        self.focus()
        step, pitch = result
        self.cursor_step = step
        self.cursor_pitch = pitch
        self._ensure_cursor_visible()
        self.sequence.toggle_note(step, pitch)
        self.post_message(self.NoteToggled(step, pitch))
        self.refresh()

    # ── cursor actions ──

    def action_cursor_up(self) -> None:
        if self.cursor_pitch < 127:
            self.cursor_pitch += 1
            self._ensure_cursor_visible()

    def action_cursor_down(self) -> None:
        if self.cursor_pitch > 0:
            self.cursor_pitch -= 1
            self._ensure_cursor_visible()

    def action_cursor_left(self) -> None:
        if self.cursor_step > 0:
            self.cursor_step -= 1

    def action_cursor_right(self) -> None:
        if self.cursor_step < self.sequence.steps - 1:
            self.cursor_step += 1

    # ── note editing ──

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
            note.duration = min(self.sequence.steps - note.step, note.duration + 1)
            self.refresh()

    def action_duration_down(self) -> None:
        note = self.sequence.note_at(self.cursor_step, self.cursor_pitch)
        if note:
            note.duration = max(1, note.duration - 1)
            self.refresh()
