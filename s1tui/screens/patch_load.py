"""Patch load modal screen."""

from __future__ import annotations

from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, OptionList
from textual.widgets.option_list import Option

from ..patches import list_patches


class LoadPatchScreen(ModalScreen[Path | None]):
    DEFAULT_CSS = """
    LoadPatchScreen { align: center middle; }
    #load-dialog {
        width: 60; max-height: 24; border: thick $accent;
        background: $surface; padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        patches = list_patches()
        with Vertical(id="load-dialog"):
            yield Label("Load Patch")
            if patches:
                yield OptionList(
                    *[Option(p.stem, id=str(p)) for p in patches],
                    id="patch-list",
                )
            else:
                yield Label("[dim]No saved patches found.[/dim]")
            yield Button("Cancel", id="cancel-btn")

    @on(OptionList.OptionSelected, "#patch-list")
    def patch_chosen(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(Path(str(event.option.id)))

    @on(Button.Pressed, "#cancel-btn")
    def cancel(self) -> None:
        self.dismiss(None)
