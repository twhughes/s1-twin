"""Patch save modal screen."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label


class SavePatchScreen(ModalScreen[str | None]):
    DEFAULT_CSS = """
    SavePatchScreen { align: center middle; }
    #save-dialog {
        width: 50; height: auto; border: thick $accent;
        background: $surface; padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="save-dialog"):
            yield Label("Save Patch — enter a name:")
            yield Input(placeholder="my-patch", id="patch-name-input")
            with Horizontal():
                yield Button("Save", variant="primary", id="save-btn")
                yield Button("Cancel", id="cancel-btn")

    @on(Button.Pressed, "#save-btn")
    def do_save(self) -> None:
        name = self.query_one("#patch-name-input", Input).value.strip()
        if name:
            self.dismiss(name)

    @on(Input.Submitted, "#patch-name-input")
    def submit_input(self) -> None:
        self.do_save()

    @on(Button.Pressed, "#cancel-btn")
    def cancel(self) -> None:
        self.dismiss(None)
