"""Menu view — deeper parameters as neon module cards (no nested tab bar)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from ..schema import AccessLevel, S1Param, sections_for_access
from ..theme import section_accent
from .cards import ModuleColumns, columns_for_width


def _build_menu_sections() -> list[tuple[str, list[S1Param]]]:
    """Ordered menu sections (MENU/SHIFT/EXTERNAL access)."""
    return sections_for_access((AccessLevel.MENU, AccessLevel.SHIFT, AccessLevel.EXTERNAL))


class MenuView(Static):
    """Deep-menu view: a scrollable wall of module cards."""

    DEFAULT_CSS = """
    MenuView { height: 1fr; width: 100%; }
    MenuView #menu-scroll { height: 1fr; width: 100%; padding: 1 1 0 1; }
    """

    def compose(self) -> ComposeResult:
        sections = [
            (name, params, section_accent(name))
            for name, params in _build_menu_sections()
        ]
        try:
            ncols = columns_for_width(self.app.size.width)
        except Exception:
            ncols = 2
        with VerticalScroll(id="menu-scroll"):
            yield ModuleColumns(sections, ncols)
