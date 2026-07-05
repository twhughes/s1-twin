"""Tests for widget factory and widget types."""


from s1tui.schema import S1_PARAMS, ControlType, S1Param
from s1tui.widgets import CCSelector, CCSlider, CCToggle, make_param_widget


class TestWidgetFactory:
    def test_continuous_returns_slider(self):
        p = S1Param("Test", 3, "LFO", control_type=ControlType.CONTINUOUS)
        w = make_param_widget(p)
        assert isinstance(w, CCSlider)

    def test_switch_returns_toggle(self):
        p = S1Param("Test", 65, "Voice", control_type=ControlType.SWITCH,
                     value_labels={0: "Off", 127: "On"})
        w = make_param_widget(p)
        assert isinstance(w, CCToggle)

    def test_discrete_with_labels_returns_selector(self):
        p = S1Param("Test", 12, "LFO", control_type=ControlType.DISCRETE,
                     value_labels={0: "Tri", 64: "Sqr", 127: "Saw"})
        w = make_param_widget(p)
        assert isinstance(w, CCSelector)

    def test_discrete_without_labels_returns_slider(self):
        p = S1Param("Test", 106, "LFO", control_type=ControlType.DISCRETE)
        w = make_param_widget(p)
        assert isinstance(w, CCSlider)

    def test_factory_sets_widget_id(self):
        p = S1Param("Rate", 3, "LFO")
        w = make_param_widget(p)
        assert w.id == "param-3"

    def test_factory_custom_id(self):
        p = S1Param("Rate", 3, "LFO")
        w = make_param_widget(p, id="custom-id")
        assert w.id == "custom-id"

    def test_all_54_params_produce_widget(self):
        """Every S1 param should produce a widget without error."""
        for p in S1_PARAMS:
            w = make_param_widget(p)
            assert w is not None
            assert hasattr(w, "param")
            assert w.param is p

    def test_widget_type_distribution(self):
        """Verify we get a reasonable mix of widget types."""
        types = {CCSlider: 0, CCToggle: 0, CCSelector: 0}
        for p in S1_PARAMS:
            w = make_param_widget(p)
            types[type(w)] += 1
        assert types[CCSlider] > 20, "Should have many sliders"
        assert types[CCToggle] > 0, "Should have some toggles"
        assert types[CCSelector] > 0, "Should have some selectors"


class TestCCSliderUnit:
    def test_slider_stores_param(self):
        p = S1Param("Rate", 3, "LFO")
        s = CCSlider(p, id="test")
        assert s.param is p

    def test_slider_initial_value(self):
        p = S1Param("Rate", 3, "LFO")
        s = CCSlider(p, id="test")
        assert s.value == 0


class TestCCToggleUnit:
    def test_toggle_stores_param(self):
        p = S1Param("Porta", 65, "Voice", control_type=ControlType.SWITCH,
                     value_labels={0: "Off", 127: "On"})
        t = CCToggle(p, id="test")
        assert t.param is p

    def test_toggle_initial_value(self):
        p = S1Param("Porta", 65, "Voice", control_type=ControlType.SWITCH,
                     value_labels={0: "Off", 127: "On"})
        t = CCToggle(p, id="test")
        assert t.value == 0


class TestCCSelectorUnit:
    def test_selector_stores_param(self):
        p = S1Param("Wave", 12, "LFO", control_type=ControlType.DISCRETE,
                     value_labels={0: "Tri", 64: "Sqr", 127: "Saw"})
        s = CCSelector(p, id="test")
        assert s.param is p

    def test_selector_builds_choices(self):
        p = S1Param("Wave", 12, "LFO", control_type=ControlType.DISCRETE,
                     value_labels={0: "Tri", 64: "Sqr", 127: "Saw"})
        s = CCSelector(p, id="test")
        assert s._choices == [0, 64, 127]

    def test_selector_current_index(self):
        p = S1Param("Wave", 12, "LFO", control_type=ControlType.DISCRETE,
                     value_labels={0: "Tri", 64: "Sqr", 127: "Saw"})
        s = CCSelector(p, id="test")
        s.value = 0
        assert s._current_index() == 0
        s.value = 64
        assert s._current_index() == 1
        s.value = 127
        assert s._current_index() == 2

    def test_selector_nearest_index(self):
        p = S1Param("Wave", 12, "LFO", control_type=ControlType.DISCRETE,
                     value_labels={0: "Tri", 64: "Sqr", 127: "Saw"})
        s = CCSelector(p, id="test")
        s.value = 10  # Nearest to 0
        assert s._current_index() == 0
        s.value = 60  # Nearest to 64
        assert s._current_index() == 1
