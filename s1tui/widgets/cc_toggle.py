"""Compact on/off toggle widget for switch CC parameters."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Label, Static

from ..schema import S1Param, SeqParam


class CCToggle(Static):
    """A compact on/off toggle for a switch CC parameter."""

    DEFAULT_CSS = """
    CCToggle {
        height: 1;
        margin: 0;
        padding: 0;
        layout: horizontal;
    }
    CCToggle .toggle-label {
        width: 18;
        height: 1;
        color: $text-muted;
    }
    CCToggle .toggle-state {
        width: 1fr;
        height: 1;
    }
    """

    value: reactive[int] = reactive(0, init=False)
    can_focus = True

    def __init__(self, param: S1Param | SeqParam, **kwargs) -> None:
        super().__init__(**kwargs)
        self.param = param
        self._wk = param.key if isinstance(param, SeqParam) else str(param.cc)

    def compose(self) -> ComposeResult:
        yield Label(self.param.name, classes="toggle-label")
        yield Label(self._render_state(), classes="toggle-state", id=f"toggle-{self._wk}")

    def _render_state(self) -> str:
        on = self.value >= 64
        label = self.param.label_for_value(127 if on else 0)
        if label in ("On", "Off"):
            return "[b green]● ON[/]" if on else "[dim]○ OFF[/]"
        return f"[b green]● {label}[/]" if on else f"[dim]○ {label}[/]"

    def watch_value(self, new_value: int) -> None:
        try:
            state = self.query_one(f"#toggle-{self._wk}", Label)
            state.update(self._render_state())
        except Exception:
            pass

    def on_key(self, event) -> None:
        if event.key in ("right", "left", "space", "enter"):
            self.value = 0 if self.value >= 64 else 127
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
        """Posted when toggle value changes."""
        def __init__(self, toggle: CCToggle) -> None:
            super().__init__()
            self.slider = toggle
            self.param = toggle.param
            self.cc_value = toggle.value
