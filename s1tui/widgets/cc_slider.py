"""Arc dial widget for continuous CC parameters."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Label, Static

from ..schema import S1Param, SeqParam


class CCSlider(Static):
    """A compact arc dial for a single CC parameter."""

    DEFAULT_CSS = """
    CCSlider {
        height: 1;
        margin: 0;
        padding: 0;
        layout: horizontal;
    }
    CCSlider .slider-label {
        width: 18;
        height: 1;
        color: $text-muted;
    }
    CCSlider .slider-track {
        width: 1fr;
        height: 1;
    }
    CCSlider .slider-value {
        width: 4;
        height: 1;
        text-align: right;
        color: $accent;
    }
    """

    value: reactive[int] = reactive(0, init=False)
    can_focus = True

    def __init__(self, param: S1Param | SeqParam, **kwargs) -> None:
        super().__init__(**kwargs)
        self.param = param
        self._wk = param.key if isinstance(param, SeqParam) else str(param.cc)
        self._dial_width = 20

    def compose(self) -> ComposeResult:
        yield Label(self.param.name, classes="slider-label")
        yield Label(self._render_dial(), classes="slider-track", id=f"track-{self._wk}")
        yield Label(f"{self.value:>3}", classes="slider-value", id=f"val-{self._wk}")

    def _render_dial(self) -> str:
        w = self._dial_width
        pos = round((self.value / 127) * (w - 1))
        chars = list("─" * w)
        chars[pos] = "●"
        return f"◜{''.join(chars)}◝"

    def watch_value(self, new_value: int) -> None:
        try:
            track = self.query_one(f"#track-{self._wk}", Label)
            track.update(self._render_dial())
            val_label = self.query_one(f"#val-{self._wk}", Label)
            val_label.update(f"{new_value:>3}")
        except Exception:
            pass

    def on_key(self, event) -> None:
        if event.key == "right":
            self.value = min(127, self.value + 1)
            self.post_message(self.Changed(self))
            event.stop()
        elif event.key == "left":
            self.value = max(0, self.value - 1)
            self.post_message(self.Changed(self))
            event.stop()
        elif event.key == "shift+right":
            self.value = min(127, self.value + 10)
            self.post_message(self.Changed(self))
            event.stop()
        elif event.key == "shift+left":
            self.value = max(0, self.value - 10)
            self.post_message(self.Changed(self))
            event.stop()
        elif event.key == "home":
            self.value = 0
            self.post_message(self.Changed(self))
            event.stop()
        elif event.key == "end":
            self.value = 127
            self.post_message(self.Changed(self))
            event.stop()

    def on_click(self, event) -> None:
        self.focus()

    class Changed(Message):
        """Posted when slider value changes."""
        def __init__(self, slider: CCSlider) -> None:
            super().__init__()
            self.slider = slider
            self.param = slider.param
            self.cc_value = slider.value
