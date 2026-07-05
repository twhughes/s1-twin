"""Base parameter widget + factory.

All three control types (slider / toggle / selector) share one row layout,
focus treatment and change message. Each row renders itself with Rich
``Text`` so the meter fills the full available width — no more stranded
fixed-width dials.
"""

from __future__ import annotations

from rich.text import Text
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget

from .. import theme as T
from ..schema import ControlType, S1Param, SeqParam

LABEL_W = 15  # columns reserved for the parameter name
# Fractional fill ramp (eighth-blocks) for sub-cell meter precision.
_RAMP = " ▏▎▍▌▋▊▉"


class ParamChanged(Message):
    """Unified message posted by any parameter widget when its value changes."""

    def __init__(self, param: S1Param | SeqParam, value: int, widget: Widget) -> None:
        super().__init__()
        self.param = param
        self.value = value
        self.cc_value = value  # back-compat alias
        self.widget = widget


def neon_meter(value: int, width: int, accent: str) -> Text:
    """A filled neon meter `████▌·····` of exactly ``width`` cells."""
    width = max(1, width)
    exact = (value / 127) * width
    full = int(exact)
    rem = exact - full
    cap = ""
    if full < width:
        idx = int(rem * 8)
        if idx:
            cap = _RAMP[idx]
    full = min(full, width)
    bar = Text()
    if full:
        bar.append("█" * full, style=accent)
    if cap:
        bar.append(cap, style=accent)
    empty = width - full - len(cap)
    if empty > 0:
        bar.append("·" * empty, style=T.FAINT)
    return bar


class ParamWidget(Widget, can_focus=True):
    """A single parameter row: name · control · value."""

    DEFAULT_CSS = """
    ParamWidget {
        height: 1;
        width: 1fr;
        padding: 0;
    }
    ParamWidget:focus {
        background: $boost;
    }
    """

    value: reactive[int] = reactive(0, init=False)

    def __init__(self, param: S1Param | SeqParam, accent: str | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.param = param
        self.accent = accent or T.DEFAULT_ACCENT
        self._wk = param.key if isinstance(param, SeqParam) else str(param.cc)

    # ── rendering ──

    def render(self) -> Text:
        width = self.size.width or 60
        focused = self.has_focus
        line = Text(no_wrap=True, overflow="crop")
        # focus marker + name
        line.append("▸ " if focused else "  ",
                    style=self.accent if focused else T.PANEL)
        name = self.param.name[:LABEL_W]
        line.append(
            f"{name:<{LABEL_W}}",
            style=f"bold {self.accent}" if focused else T.MUTED,
        )
        body_w = max(4, width - 2 - LABEL_W)
        line.append_text(self.render_control(body_w, focused))
        return line

    def render_control(self, width: int, focused: bool) -> Text:  # pragma: no cover
        raise NotImplementedError

    def watch_value(self, _new: int) -> None:
        self.refresh()

    def on_focus(self) -> None:
        self.refresh()

    def on_blur(self) -> None:
        self.refresh()

    def on_resize(self) -> None:
        self.refresh()

    def on_click(self, event) -> None:
        self.focus()

    def _emit(self) -> None:
        self.post_message(ParamChanged(self.param, self.value, self))

    # back-compat: some callers referenced .Changed; keep an alias
    Changed = ParamChanged


def make_param_widget(param: S1Param | SeqParam, accent: str | None = None, **kwargs):
    """Create the right widget for a parameter, tinted with ``accent``."""
    from .cc_selector import CCSelector
    from .cc_slider import CCSlider
    from .cc_toggle import CCToggle

    if isinstance(param, SeqParam):
        widget_id = kwargs.pop("id", f"seq-{param.key}")
    else:
        widget_id = kwargs.pop("id", f"param-{param.cc}")

    if param.control_type == ControlType.SWITCH:
        cls = CCToggle
    elif param.control_type == ControlType.DISCRETE and param.value_labels:
        cls = CCSelector
    else:
        cls = CCSlider
    return cls(param, accent=accent, id=widget_id, **kwargs)
