"""A braille-trace oscilloscope that visualises the oscillator mix."""

from __future__ import annotations

from rich.text import Text
from textual.reactive import reactive
from textual.widget import Widget

from .. import theme as T

# Braille dot bit per (col, row) inside a 2×4 cell. Base char is U+2800.
_DOT = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))


class WaveScope(Widget):
    """Live oscilloscope. Set the oscillator mix; it draws the waveform."""

    DEFAULT_CSS = """
    WaveScope { height: 1fr; width: 1fr; }
    """

    saw: reactive[float] = reactive(1.0)
    square: reactive[float] = reactive(0.0)
    sub: reactive[float] = reactive(0.0)
    pw: reactive[float] = reactive(0.5)

    def set_mix(self, saw: float, square: float, sub: float, pw: float) -> None:
        self.saw, self.square, self.sub, self.pw = saw, square, sub, pw

    def _wave(self, phase: float) -> float:
        saw_v = 2.0 * phase - 1.0
        sq_v = 1.0 if phase < self.pw else -1.0
        sub_v = 1.0 if (phase * 0.5) % 1.0 < 0.5 else -1.0
        return self.saw * saw_v + self.square * sq_v + self.sub * sub_v

    def render(self) -> Text:
        w = self.size.width or 40
        h = self.size.height or 6
        if w < 2 or h < 1:
            return Text("")
        dots_w, dots_h = w * 2, h * 4
        cells = [0] * (w * h)
        amp = self.saw + self.square + self.sub
        norm = amp if amp > 0.001 else 1.0
        cycles = 2.0
        for x in range(dots_w):
            phase = (x / dots_w * cycles) % 1.0
            y = max(-1.0, min(1.0, self._wave(phase) / norm))
            drow = round((1.0 - (y + 1.0) / 2.0) * (dots_h - 1))
            cx, col = divmod(x, 2)
            cy, row = divmod(drow, 4)
            cells[cy * w + cx] |= _DOT[col][row]

        mid = h // 2
        out = Text(no_wrap=True, overflow="crop")
        for cy in range(h):
            for cx in range(w):
                b = cells[cy * w + cx]
                if b:
                    out.append(chr(0x2800 + b), style=T.CYAN)
                elif cy == mid:
                    out.append("─", style=T.FAINT)
                else:
                    out.append(" ")
            if cy < h - 1:
                out.append("\n")
        return out

    def watch_saw(self) -> None:
        self.refresh()

    def watch_square(self) -> None:
        self.refresh()

    def watch_sub(self) -> None:
        self.refresh()

    def watch_pw(self) -> None:
        self.refresh()

    def on_resize(self) -> None:
        self.refresh()
