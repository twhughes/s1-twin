"""Tests for params.py backward compatibility wrapper."""

from s1tui.params import CCParam, SECTIONS, all_params, find_param_by_cc


class TestBackwardCompat:
    def test_sections_dict_has_9_sections(self):
        assert len(SECTIONS) == 9

    def test_sections_names_match_original(self):
        expected = [
            "LFO", "Oscillator", "Osc Draw/Chop", "Filter",
            "Envelope", "Voice", "Chord Mode", "Effects", "Controls",
        ]
        for name in expected:
            assert name in SECTIONS, f"Missing section: {name}"

    def test_all_params_returns_54(self):
        assert len(all_params()) == 54

    def test_all_params_returns_ccparam_instances(self):
        for p in all_params():
            assert isinstance(p, CCParam)

    def test_find_param_by_cc(self):
        p = find_param_by_cc(74)
        assert p is not None
        assert p.name == "Frequency"
        assert isinstance(p, CCParam)

    def test_find_param_by_cc_not_found(self):
        assert find_param_by_cc(999) is None

    def test_section_lfo_has_6_params(self):
        assert len(SECTIONS["LFO"]) == 6

    def test_section_oscillator_has_12_params(self):
        assert len(SECTIONS["Oscillator"]) == 12

    def test_section_draw_chop_has_4_params(self):
        assert len(SECTIONS["Osc Draw/Chop"]) == 4

    def test_section_filter_has_6_params(self):
        assert len(SECTIONS["Filter"]) == 6

    def test_section_effects_has_5_params(self):
        assert len(SECTIONS["Effects"]) == 5

    def test_section_controls_has_3_params(self):
        assert len(SECTIONS["Controls"]) == 3

    def test_ccparam_has_expected_fields(self):
        p = find_param_by_cc(74)
        assert hasattr(p, "name")
        assert hasattr(p, "cc")
        assert hasattr(p, "min_val")
        assert hasattr(p, "max_val")
        assert hasattr(p, "default")
        assert hasattr(p, "description")

    def test_defaults_preserved(self):
        p = find_param_by_cc(74)  # Filter Frequency default=127
        assert p.default == 127
        p2 = find_param_by_cc(14)  # Range default=64
        assert p2.default == 64
