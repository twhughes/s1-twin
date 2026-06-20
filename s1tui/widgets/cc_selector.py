"""Discrete choice selector widget for CC parameters with named values."""

from __future__ import annotations

from rich.text import Text

from .param_widget import ParamWidget
from .. import theme as T


class CCSelector(ParamWidget):
    """A `◀ Choice ▶` selector for CC parameters with named values."""

    def __init__(self, param, accent=None, **kwargs) -> None:
        super().__init__(param, accent=accent, **kwargs)
        self._choices = sorted(param.value_labels.keys()) if param.value_labels else list(range(128))

    def _current_index(self) -> int:
        closest = min(self._choices, key=lambda c: abs(c - self.value))
        return self._choices.index(closest)

    def render_control(self, width: int, focused: bool) -> Text:
        idx = self._current_index()
        label = self.param.label_for_value(self.value)
        body = Text(no_wrap=True, overflow="crop")
        body.append("◀ " if idx > 0 else "  ",
                    style=self.accent if idx > 0 else T.FAINT)
        body.append(label, style=f"bold {self.accent}")
        body.append(" ▶" if idx < len(self._choices) - 1 else "  ",
                    style=self.accent if idx < len(self._choices) - 1 else T.FAINT)
        # right-aligned raw value
        raw = f"{self.value:>3}"
        pad = width - body.cell_len - len(raw)
        if pad > 0:
            body.append(" " * pad)
        body.append(raw, style=T.MUTED)
        return body

    def on_key(self, event) -> None:
        idx = self._current_index()
        if event.key == "right" and idx < len(self._choices) - 1:
            self.value = self._choices[idx + 1]
            self._emit()
            event.stop()
        elif event.key == "left" and idx > 0:
            self.value = self._choices[idx - 1]
            self._emit()
            event.stop()
        elif event.key == "home":
            self.value = self._choices[0]
            self._emit()
            event.stop()
        elif event.key == "end":
            self.value = self._choices[-1]
            self._emit()
            event.stop()
