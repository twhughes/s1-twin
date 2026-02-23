"""Menu view — shows MENU/SHIFT/EXTERNAL parameters organized by section tabs."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static, TabbedContent, TabPane

from ..schema import S1_PARAMS, AccessLevel, S1Param
from ..widgets.param_widget import make_param_widget


def _build_menu_sections() -> list[tuple[str, list[S1Param]]]:
    """Build ordered menu sections from params with MENU/SHIFT/EXTERNAL access."""
    sections: dict[str, list[S1Param]] = {}
    order: list[str] = []
    for p in S1_PARAMS:
        if p.access in (AccessLevel.MENU, AccessLevel.SHIFT, AccessLevel.EXTERNAL):
            if p.section not in sections:
                sections[p.section] = []
                order.append(p.section)
            sections[p.section].append(p)
    return [(name, sections[name]) for name in order]


class MenuView(Static):
    """Menu view with sub-tabs for deeper settings."""

    DEFAULT_CSS = """
    MenuView {
        height: 1fr;
        width: 100%;
    }
    MenuView TabPane {
        padding: 0;
    }
    """

    def compose(self) -> ComposeResult:
        sections = _build_menu_sections()
        with TabbedContent():
            for section_name, params in sections:
                tab_id = f"menu-{section_name.lower().replace(' ', '-').replace('/', '-')}"
                with TabPane(section_name, id=tab_id):
                    with VerticalScroll():
                        for param in params:
                            yield make_param_widget(param)
