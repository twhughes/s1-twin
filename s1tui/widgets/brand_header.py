"""Neon brand header — a gradient wordmark and an equaliser flourish."""

from __future__ import annotations

from rich.text import Text
from textual.widget import Widget

from .. import theme as T

_EQ = "▁▂▃▅▇▆▄▂▁▂▄▆"  # decorative right-side equaliser bars


class BrandHeader(Widget):
    """Top brand bar: `◉ S-1 │ ROLAND S-1 SYNTH CONTROLLER  ▁▂▃▅▇`."""

    DEFAULT_CSS = """
    BrandHeader {
        dock: top;
        height: 1;
        background: $surface;
        color: $text;
    }
    """

    def render(self) -> Text:
        w = self.size.width or 80
        t = Text(no_wrap=True, overflow="crop")
        t.append("  ◉ ", style=f"bold {T.MAGENTA}")
        t.append_text(T.gradient("S-1", [T.MAGENTA, T.VIOLET, T.CYAN]))
        t.append("  ┃  ", style=T.FAINT)
        t.append("ROLAND S-1", style=f"bold {T.FG}")
        t.append("  SYNTH CONTROLLER", style=T.MUTED)
        right = T.gradient(_EQ, [T.CYAN, T.VIOLET, T.MAGENTA], bold=False)
        pad = w - t.cell_len - right.cell_len - 2
        if pad > 0:
            t.append(" " * pad)
        t.append_text(right)
        t.append("  ")
        return t

    def on_resize(self) -> None:
        self.refresh()
