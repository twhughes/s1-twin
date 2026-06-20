"""Module-card layout primitives shared by the views.

A ``SectionCard`` is a bordered, accent-tinted panel holding the parameter
rows for one synth section. ``ModuleColumns`` packs cards into balanced
columns so the screen fills edge-to-edge instead of hugging the left.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical

from ..schema import S1Param, SeqParam
from ..widgets.param_widget import make_param_widget

Section = tuple[str, list, str]  # (title, params, accent)


class SectionCard(Container):
    """A bordered module holding a section's parameter rows."""

    DEFAULT_CSS = """
    SectionCard {
        height: auto;
        width: 1fr;
        border: round $primary;
        border-title-align: left;
        padding: 0 1;
        margin: 0 0 1 0;
        background: $panel 35%;
    }
    """

    def __init__(self, title: str, params: list[S1Param | SeqParam], accent: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._title = title
        self._params = params
        self._accent = accent

    def compose(self) -> ComposeResult:
        for param in self._params:
            yield make_param_widget(param, accent=self._accent)

    def on_mount(self) -> None:
        self.border_title = f" {self._title.upper()} "
        self.styles.border = ("round", self._accent)
        try:
            self.styles.border_title_color = self._accent
        except Exception:
            pass


def balance_columns(sections: list[Section], ncols: int) -> list[list[Section]]:
    """Greedily pack sections into ``ncols`` columns balanced by height."""
    cols: list[list[Section]] = [[] for _ in range(ncols)]
    heights = [0] * ncols
    for sec in sections:
        i = min(range(ncols), key=lambda k: heights[k])
        cols[i].append(sec)
        heights[i] += len(sec[1]) + 3  # rows + border + margin
    return cols


class ModuleColumns(Horizontal):
    """Balanced multi-column wall of section cards."""

    DEFAULT_CSS = """
    ModuleColumns {
        height: auto;
        width: 1fr;
    }
    ModuleColumns > .module-col {
        width: 1fr;
        height: auto;
        padding: 0 1 0 0;
    }
    """

    def __init__(self, sections: list[Section], ncols: int = 2, **kwargs) -> None:
        super().__init__(**kwargs)
        self._sections = sections
        self._ncols = max(1, ncols)

    def compose(self) -> ComposeResult:
        for col in balance_columns(self._sections, self._ncols):
            with Vertical(classes="module-col"):
                for title, params, accent in col:
                    yield SectionCard(title, params, accent)


def columns_for_width(width: int, *, wide: int = 3, normal: int = 2) -> int:
    """Pick a column count for the terminal width."""
    if width >= 132:
        return wide
    if width >= 84:
        return normal
    return 1
