"""Factory for creating the appropriate widget for a parameter."""

from __future__ import annotations

from textual.message import Message
from textual.widget import Widget

from ..schema import S1Param, SeqParam, ControlType
from .cc_slider import CCSlider
from .cc_toggle import CCToggle
from .cc_selector import CCSelector


class ParamChanged(Message):
    """Unified message for any parameter value change."""
    def __init__(self, param: S1Param, value: int, widget: Widget) -> None:
        super().__init__()
        self.param = param
        self.value = value
        self.widget = widget


def make_param_widget(param: S1Param | SeqParam, **kwargs) -> CCSlider | CCToggle | CCSelector:
    """Create the appropriate widget type for a parameter."""
    if isinstance(param, SeqParam):
        widget_id = kwargs.pop("id", f"seq-{param.key}")
    else:
        widget_id = kwargs.pop("id", f"param-{param.cc}")
    if param.control_type == ControlType.SWITCH:
        return CCToggle(param, id=widget_id, **kwargs)
    elif param.control_type == ControlType.DISCRETE and param.value_labels:
        return CCSelector(param, id=widget_id, **kwargs)
    else:
        return CCSlider(param, id=widget_id, **kwargs)
