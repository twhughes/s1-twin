"""MIDI port selection modal screen."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, OptionList
from textual.widgets.option_list import Option


class PortSelectScreen(ModalScreen[str | None]):
    """Modal to select a MIDI output port."""

    DEFAULT_CSS = """
    PortSelectScreen {
        align: center middle;
    }
    #port-dialog {
        width: 60;
        max-height: 24;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #port-dialog Label {
        margin-bottom: 1;
    }
    """

    def __init__(self, ports: list[str]) -> None:
        super().__init__()
        self.ports = ports

    def compose(self) -> ComposeResult:
        with Vertical(id="port-dialog"):
            yield Label("Select MIDI Output Port")
            if self.ports:
                yield OptionList(
                    *[Option(p, id=p) for p in self.ports],
                    id="port-list",
                )
            else:
                yield Label("[red]No MIDI ports found.[/red]\nConnect the S-1 via USB and restart.")
            yield Button("Cancel", variant="default", id="cancel-btn")

    @on(OptionList.OptionSelected, "#port-list")
    def port_chosen(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.prompt))

    @on(Button.Pressed, "#cancel-btn")
    def cancel(self) -> None:
        self.dismiss(None)
