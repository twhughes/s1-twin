"""Sequencer view — piano roll + transport + seq/arp parameter cards."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Container, VerticalScroll
from textual.widgets import Label, Static

from ..schema import SEQ_PARAMS, all_seq_sections
from ..theme import section_accent
from .cards import ModuleColumns
from ..widgets.piano_roll import PianoRoll

_TRANSPORT_HELP = (
    "[$secondary]space[/] play/stop   [$secondary]p[/]/[$secondary]x[/] play·stop   "
    "[$secondary]m[/] load MIDI   ║   "
    "[$accent]z[/] toggle   [$accent]+/-[/] velocity   [$accent]\\[/][/] duration"
)


class SequencerView(Static):
    """Sequencer view with piano roll, transport, and seq/arp params."""

    DEFAULT_CSS = """
    SequencerView { height: 1fr; width: 100%; }
    SequencerView #seq-scroll { height: 1fr; width: 100%; padding: 1 1 0 1; }
    SequencerView .transport-bar { height: 1; padding: 0 1; color: $text-muted; margin: 0 0 1 0; }
    SequencerView #roll-card {
        height: auto; width: 100%;
        border: round $secondary;
        border-title-align: left;
        padding: 0 1; margin: 0 0 1 0;
        background: $panel 35%;
    }
    SequencerView #file-info { height: 1; color: $text-muted; margin: 0 0 1 0; }
    SequencerView PianoRoll { height: 19; width: 100%; }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="seq-scroll"):
            yield Label(_TRANSPORT_HELP, classes="transport-bar", markup=True)
            with Container(id="roll-card"):
                yield Label("○ no midi file — click the grid or press [b]m[/]", id="file-info")
                yield PianoRoll(id="piano-roll")
            sections = [
                (name, [p for p in SEQ_PARAMS if p.section == name], section_accent(name))
                for name in all_seq_sections()
            ]
            yield ModuleColumns(sections, 2)

    def on_mount(self) -> None:
        try:
            self.query_one("#roll-card").border_title = " PIANO ROLL "
        except Exception:
            pass
