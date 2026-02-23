"""MIDI file load modal screen with path input."""

from __future__ import annotations

from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label


class LoadMidiScreen(ModalScreen[Path | None]):
    """Modal dialog to enter a path to a MIDI file."""

    DEFAULT_CSS = """
    LoadMidiScreen { align: center middle; }
    #midi-load-dialog {
        width: 70; max-height: 12; border: thick $accent;
        background: $surface; padding: 1 2;
    }
    #midi-load-dialog Label { margin: 0 0 1 0; }
    #midi-load-dialog Input { width: 100%; }
    #midi-load-dialog #error-label { color: $error; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="midi-load-dialog"):
            yield Label("Load MIDI File")
            yield Input(placeholder="Path to .mid file", id="midi-path-input")
            yield Label("", id="error-label")
            yield Button("Cancel", id="cancel-btn")

    def on_mount(self) -> None:
        self.query_one("#midi-path-input", Input).focus()

    @on(Input.Submitted, "#midi-path-input")
    def path_submitted(self, event: Input.Submitted) -> None:
        path = Path(event.value.strip()).expanduser()
        if not path.exists():
            self.query_one("#error-label", Label).update(f"File not found: {path}")
            return
        if path.suffix.lower() not in (".mid", ".midi"):
            self.query_one("#error-label", Label).update("Not a MIDI file (.mid/.midi)")
            return
        self.dismiss(path)

    @on(Button.Pressed, "#cancel-btn")
    def cancel(self) -> None:
        self.dismiss(None)
