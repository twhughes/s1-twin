"""Panel view — shows front-panel (directly accessible) parameters grouped by section."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Label, Static

from ..schema import S1_PARAMS, AccessLevel, S1Param
from ..widgets.param_widget import make_param_widget


def _build_panel_sections() -> list[tuple[str, list[S1Param]]]:
    """Build ordered panel sections from params with PANEL or SHIFT access."""
    sections: dict[str, list[S1Param]] = {}
    order: list[str] = []
    for p in S1_PARAMS:
        if p.access in (AccessLevel.PANEL, AccessLevel.SHIFT):
            if p.section not in sections:
                sections[p.section] = []
                order.append(p.section)
            sections[p.section].append(p)
    return [(name, sections[name]) for name in order]


class PanelView(Static):
    """Front-panel view showing directly accessible parameters."""

    DEFAULT_CSS = """
    PanelView {
        height: 1fr;
        width: 100%;
    }
    PanelView #panel-scroll {
        height: 1fr;
        width: 100%;
    }
    PanelView .section-header {
        text-style: bold;
        color: $accent;
        margin: 1 0 0 0;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        sections = _build_panel_sections()
        with VerticalScroll(id="panel-scroll"):
            for section_name, params in sections:
                yield Label(f"── {section_name} ──", classes="section-header")
                for param in params:
                    yield make_param_widget(param)
