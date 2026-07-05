"""Panel view — scope hero, a wall of neon module cards, and a signal ribbon."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Container, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Static

from ..schema import AccessLevel, S1Param, sections_for_access
from ..theme import section_accent
from ..widgets.wave_scope import WaveScope
from .cards import ModuleColumns

# Signal path teaser, each stage in its section colour.
_FLOW = [
    ("LFO", "LFO"), ("OSC", "Oscillator"), ("FILTER", "Filter"),
    ("AMP", "Envelope"), ("FX", "Effects"), ("OUT", "Controls"),
]


def _signal_ribbon() -> str:
    chips = [f"[b {section_accent(sec)}]{label}[/]" for label, sec in _FLOW]
    arrow = "  [#4a4378]─▶[/]  "
    return arrow.join(chips)


def _build_panel_sections() -> list[tuple[str, list[S1Param]]]:
    return sections_for_access((AccessLevel.PANEL, AccessLevel.SHIFT))


class PanelView(Static):
    """Front-panel view: scope + directly accessible parameters."""

    DEFAULT_CSS = """
    PanelView { height: 1fr; width: 100%; }
    PanelView #panel-scroll { height: 1fr; width: 100%; padding: 1 1 0 1; }
    PanelView #scope-card {
        height: 9; width: 100%;
        border: round $secondary;
        border-title-align: left;
        padding: 0 1; margin: 0 0 1 0;
        background: $panel 35%;
    }
    PanelView #signal-ribbon {
        dock: bottom; height: 3;
        content-align: center middle; text-align: center;
        border-top: round $primary;
        background: $surface;
    }
    """

    def compose(self) -> ComposeResult:
        sections = [
            (name, params, section_accent(name))
            for name, params in _build_panel_sections()
        ]
        with VerticalScroll(id="panel-scroll"):
            with Container(id="scope-card"):
                yield WaveScope(id="wave-scope")
            yield ModuleColumns(sections, 2)
        yield Static(_signal_ribbon(), id="signal-ribbon", markup=True)

    def on_mount(self) -> None:
        try:
            self.query_one("#scope-card").border_title = " SCOPE "
        except NoMatches:
            pass
