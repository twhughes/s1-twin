"""On/off pill widget for switch CC parameters."""

from __future__ import annotations

from rich.text import Text

from .param_widget import ParamWidget
from .. import theme as T


class CCToggle(ParamWidget):
    """A compact glowing on/off pill for a switch CC parameter."""

    def render_control(self, width: int, focused: bool) -> Text:
        on = self.value >= 64
        label = self.param.label_for_value(127 if on else 0)
        body = Text(no_wrap=True, overflow="crop")
        if on:
            body.append("◖", style=self.accent)
            body.append(f" {label} ", style=f"bold {T.BG} on {self.accent}")
            body.append("◗", style=self.accent)
        else:
            body.append("◌ ", style=T.FAINT)
            body.append(label, style=T.MUTED)
        return body

    def on_key(self, event) -> None:
        if event.key in ("right", "left", "space", "enter"):
            self.value = 0 if self.value >= 64 else 127
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
