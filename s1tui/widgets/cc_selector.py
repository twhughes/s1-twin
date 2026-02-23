"""Compact discrete choice selector widget for CC parameters with named values."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Label, Static

from ..schema import S1Param, SeqParam


class CCSelector(Static):
    """A compact discrete choice selector for CC parameters with named values."""

    DEFAULT_CSS = """
    CCSelector {
        height: 1;
        margin: 0;
        padding: 0;
        layout: horizontal;
    }
    CCSelector .selector-label {
        width: 18;
        height: 1;
        color: $text-muted;
    }
    CCSelector .selector-value {
        width: 1fr;
        height: 1;
    }
    CCSelector .selector-raw {
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
        self._choices = sorted(param.value_labels.keys()) if param.value_labels else list(range(0, 128))

    def compose(self) -> ComposeResult:
        yield Label(self.param.name, classes="selector-label")
        yield Label(self._render_choice(), classes="selector-value", id=f"sel-{self._wk}")
        yield Label(f"{self.value:>3}", classes="selector-raw", id=f"selraw-{self._wk}")

    def _render_choice(self) -> str:
        label = self.param.label_for_value(self.value)
        idx = self._current_index()
        left = "◁ " if idx > 0 else "  "
        right = " ▷" if idx < len(self._choices) - 1 else "  "
        return f"{left}[b]{label}[/b]{right}"

    def _current_index(self) -> int:
        closest = min(self._choices, key=lambda c: abs(c - self.value))
        return self._choices.index(closest)

    def watch_value(self, new_value: int) -> None:
        try:
            sel = self.query_one(f"#sel-{self._wk}", Label)
            sel.update(self._render_choice())
            raw = self.query_one(f"#selraw-{self._wk}", Label)
            raw.update(f"{new_value:>3}")
        except Exception:
            pass

    def on_key(self, event) -> None:
        if event.key == "right":
            idx = self._current_index()
            if idx < len(self._choices) - 1:
                self.value = self._choices[idx + 1]
                self.post_message(self.Changed(self))
            event.stop()
        elif event.key == "left":
            idx = self._current_index()
            if idx > 0:
                self.value = self._choices[idx - 1]
                self.post_message(self.Changed(self))
            event.stop()
        elif event.key == "home":
            self.value = self._choices[0]
            self.post_message(self.Changed(self))
            event.stop()
        elif event.key == "end":
            self.value = self._choices[-1]
            self.post_message(self.Changed(self))
            event.stop()

    def on_click(self, event) -> None:
        self.focus()

    class Changed(Message):
        """Posted when selector value changes."""
        def __init__(self, selector: CCSelector) -> None:
            super().__init__()
            self.slider = selector
            self.param = selector.param
            self.cc_value = selector.value
