"""MIDI file save modal screen."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label


class SaveMidiScreen(ModalScreen[str | None]):
    """Modal dialog to name the exported MIDI file."""

    DEFAULT_CSS = """
    SaveMidiScreen { align: center middle; }
    #midi-save-dialog {
        width: 50; height: auto; border: thick $accent;
        background: $surface; padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="midi-save-dialog"):
            yield Label("Save MIDI — enter a file name:")
            yield Input(placeholder="my-pattern", id="midi-name-input")
            with Horizontal():
                yield Button("Save", variant="primary", id="save-btn")
                yield Button("Cancel", id="cancel-btn")

    def on_mount(self) -> None:
        self.query_one("#midi-name-input", Input).focus()

    @on(Button.Pressed, "#save-btn")
    def do_save(self) -> None:
        name = self.query_one("#midi-name-input", Input).value.strip()
        if name:
            self.dismiss(name)

    @on(Input.Submitted, "#midi-name-input")
    def submit_input(self) -> None:
        self.do_save()

    @on(Button.Pressed, "#cancel-btn")
    def cancel(self) -> None:
        self.dismiss(None)
