"""Neon meter widget for continuous CC parameters."""

from __future__ import annotations

from rich.text import Text

from .param_widget import ParamWidget, neon_meter
from .. import theme as T


class CCSlider(ParamWidget):
    """A full-width neon meter for a single continuous CC parameter."""

    def render_control(self, width: int, focused: bool) -> Text:
        meter_w = max(1, width - 5)  # leave room for " 127"
        body = neon_meter(self.value, meter_w, self.accent)
        body.append(f" {self.value:>3}",
                    style=f"bold {self.accent}" if focused else self.accent)
        return body

    def on_key(self, event) -> None:
        step = {
            "right": 1, "left": -1,
            "shift+right": 10, "shift+left": -10,
        }.get(event.key)
        if step is not None:
            self.value = max(0, min(127, self.value + step))
            self._emit()
            event.stop()
        elif event.key == "home":
            self.value = 0
            self._emit()
            event.stop()
        elif event.key == "end":
            self.value = 127
            self._emit()
            event.stop()
