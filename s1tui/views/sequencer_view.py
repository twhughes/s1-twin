"""Sequencer view — piano roll with transport controls and seq parameters."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Label, Static

from ..schema import SEQ_PARAMS, SeqParam, all_seq_sections
from ..widgets.param_widget import make_param_widget
from ..widgets.piano_roll import PianoRoll


class SequencerView(Static):
    """Sequencer view with piano roll, transport status, and per-section parameters."""

    DEFAULT_CSS = """
    SequencerView {
        height: 1fr;
        width: 100%;
    }
    SequencerView #seq-scroll {
        height: 1fr;
        width: 100%;
    }
    SequencerView .section-header {
        text-style: bold;
        color: $accent;
        margin: 1 0 0 0;
        padding: 0 1;
    }
    SequencerView .transport-bar {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    SequencerView .file-info {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    SequencerView PianoRoll {
        height: 20;
        width: 100%;
    }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="seq-scroll"):
            yield Label(
                "Transport: [b]space[/b] play/stop  [b]p[/b] play  [b]x[/b] stop  |  "
                "[b]m[/b] load MIDI  |  "
                "Edit: [b]z[/b]/[b]enter[/b] toggle note  [b]+/-[/b] velocity  [b]\\[/][/b] duration",
                classes="transport-bar",
            )
            yield Label("No MIDI file loaded", id="file-info", classes="file-info")
            yield PianoRoll(id="piano-roll")
            for section_name in all_seq_sections():
                yield Label(f"── {section_name} ──", classes="section-header")
                for param in SEQ_PARAMS:
                    if param.section == section_name:
                        yield make_param_widget(param)
