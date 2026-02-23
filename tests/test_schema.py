"""Tests for schema.py — parameter definitions and lookup utilities."""

from s1tui.schema import (
    S1_PARAMS,
    S1Param,
    SEQ_PARAMS,
    SeqParam,
    AccessLevel,
    ControlType,
    param_by_cc,
    params_by_section,
    params_by_access,
    panel_params,
    menu_params,
    all_sections,
    seq_param_by_key,
    seq_params_by_section,
    all_seq_sections,
)


class TestS1ParamDefinitions:
    def test_total_param_count(self):
        assert len(S1_PARAMS) == 54

    def test_all_cc_numbers_unique(self):
        ccs = [p.cc for p in S1_PARAMS]
        assert len(ccs) == len(set(ccs)), f"Duplicate CCs: {[c for c in ccs if ccs.count(c) > 1]}"

    def test_all_params_have_names(self):
        for p in S1_PARAMS:
            assert p.name, f"CC {p.cc} has empty name"

    def test_all_params_have_sections(self):
        for p in S1_PARAMS:
            assert p.section, f"CC {p.cc} ({p.name}) has empty section"

    def test_cc_range_valid(self):
        for p in S1_PARAMS:
            assert 0 <= p.cc <= 127, f"{p.name} CC {p.cc} out of range"

    def test_default_within_range(self):
        for p in S1_PARAMS:
            assert p.min_val <= p.default <= p.max_val, (
                f"{p.name} default {p.default} outside [{p.min_val}, {p.max_val}]"
            )

    def test_min_less_than_max(self):
        for p in S1_PARAMS:
            assert p.min_val <= p.max_val, f"{p.name} min > max"


class TestAccessLevels:
    def test_panel_count(self):
        panel = params_by_access(AccessLevel.PANEL)
        assert len(panel) == 19

    def test_shift_count(self):
        shift = params_by_access(AccessLevel.SHIFT)
        assert len(shift) == 2

    def test_menu_count(self):
        menu = params_by_access(AccessLevel.MENU)
        assert len(menu) == 30

    def test_external_count(self):
        ext = params_by_access(AccessLevel.EXTERNAL)
        assert len(ext) == 3

    def test_all_access_levels_sum_to_total(self):
        total = sum(
            len(params_by_access(level))
            for level in AccessLevel
        )
        assert total == 54

    def test_panel_params_helper(self):
        assert panel_params() == params_by_access(AccessLevel.PANEL)

    def test_menu_params_includes_shift_menu_external(self):
        m = menu_params()
        for p in m:
            assert p.access in (AccessLevel.SHIFT, AccessLevel.MENU, AccessLevel.EXTERNAL)
        assert len(m) == 35  # 2 + 30 + 3


class TestControlTypes:
    def test_switch_params_have_on_off_labels(self):
        switches = [p for p in S1_PARAMS if p.control_type == ControlType.SWITCH]
        assert len(switches) > 0
        for p in switches:
            assert p.value_labels, f"Switch {p.name} has no value_labels"

    def test_discrete_params_have_labels(self):
        discrete = [p for p in S1_PARAMS if p.control_type == ControlType.DISCRETE]
        for p in discrete:
            if p.value_labels:
                assert len(p.value_labels) >= 2, f"{p.name} has fewer than 2 labels"

    def test_continuous_is_default(self):
        cont = [p for p in S1_PARAMS if p.control_type == ControlType.CONTINUOUS]
        assert len(cont) > 20  # Most params are continuous


class TestLookupUtilities:
    def test_param_by_cc_found(self):
        p = param_by_cc(74)
        assert p is not None
        assert p.name == "Frequency"

    def test_param_by_cc_not_found(self):
        assert param_by_cc(999) is None

    def test_params_by_section(self):
        lfo = params_by_section("LFO")
        assert len(lfo) == 6
        names = [p.name for p in lfo]
        assert "Rate" in names
        assert "Waveform" in names

    def test_params_by_section_unknown(self):
        assert params_by_section("Nonexistent") == []

    def test_all_sections(self):
        sections = all_sections()
        assert "LFO" in sections
        assert "Oscillator" in sections
        assert "Filter" in sections
        assert "Envelope" in sections
        assert "Effects" in sections
        assert "Controls" in sections
        assert len(sections) == 9


class TestLabelForValue:
    def test_waveform_labels(self):
        wf = param_by_cc(12)  # LFO Waveform
        assert wf is not None
        assert wf.label_for_value(0) == "Triangle"
        assert wf.label_for_value(127) == "Noise"

    def test_continuous_returns_str(self):
        rate = param_by_cc(3)  # LFO Rate
        assert rate is not None
        assert rate.label_for_value(64) == "64"

    def test_discrete_nearest_match(self):
        wf = param_by_cc(12)
        assert wf is not None
        # Value 1 is closest to key 0 (Triangle)
        assert wf.label_for_value(1) == "Triangle"
        # Value 33 is closest to key 32 (Saw)
        assert wf.label_for_value(33) == "Saw"


class TestKnownParams:
    """Verify specific critical parameters are defined correctly."""

    def test_filter_frequency(self):
        p = param_by_cc(74)
        assert p.name == "Frequency"
        assert p.section == "Filter"
        assert p.access == AccessLevel.PANEL
        assert p.default == 127

    def test_mod_wheel(self):
        p = param_by_cc(1)
        assert p.name == "Mod Wheel"
        assert p.access == AccessLevel.EXTERNAL

    def test_delay_level_is_shift(self):
        p = param_by_cc(92)
        assert p.name == "Delay Level"
        assert p.access == AccessLevel.SHIFT

    def test_polyphony_mode_is_discrete(self):
        p = param_by_cc(80)
        assert p.name == "Polyphony Mode"
        assert p.control_type == ControlType.DISCRETE
        assert 0 in p.value_labels  # "Poly"

    def test_portamento_is_switch(self):
        p = param_by_cc(65)
        assert p.name == "Portamento"
        assert p.control_type == ControlType.SWITCH

    def test_s1param_is_frozen(self):
        p = param_by_cc(74)
        try:
            p.name = "something"
            assert False, "Should not be able to set attribute on frozen dataclass"
        except AttributeError:
            pass


class TestSeqParamDefinitions:
    def test_seq_param_count(self):
        assert len(SEQ_PARAMS) == 10

    def test_all_keys_unique(self):
        keys = [p.key for p in SEQ_PARAMS]
        assert len(keys) == len(set(keys)), f"Duplicate keys: {[k for k in keys if keys.count(k) > 1]}"

    def test_all_params_have_names(self):
        for p in SEQ_PARAMS:
            assert p.name, f"Key {p.key} has empty name"

    def test_all_params_have_sections(self):
        for p in SEQ_PARAMS:
            assert p.section, f"Key {p.key} ({p.name}) has empty section"

    def test_default_within_range(self):
        for p in SEQ_PARAMS:
            assert p.min_val <= p.default <= p.max_val, (
                f"{p.name} default {p.default} outside [{p.min_val}, {p.max_val}]"
            )

    def test_seq_param_is_frozen(self):
        p = SEQ_PARAMS[0]
        try:
            p.name = "something"
            assert False, "Should not be able to set attribute on frozen dataclass"
        except AttributeError:
            pass


class TestSeqParamSections:
    def test_sequencer_section(self):
        params = seq_params_by_section("Sequencer")
        assert len(params) == 8

    def test_arpeggiator_section(self):
        params = seq_params_by_section("Arpeggiator")
        assert len(params) == 2

    def test_all_seq_sections(self):
        sections = all_seq_sections()
        assert "Sequencer" in sections
        assert "Arpeggiator" in sections
        assert len(sections) == 2


class TestSeqParamLookup:
    def test_lookup_by_key(self):
        p = seq_param_by_key("seq_tempo")
        assert p is not None
        assert p.name == "SEQ Tempo"

    def test_lookup_not_found(self):
        assert seq_param_by_key("nonexistent") is None

    def test_label_for_value_discrete(self):
        p = seq_param_by_key("seq_scale")
        assert p is not None
        assert p.label_for_value(64) == "1/16"

    def test_label_for_value_continuous(self):
        p = seq_param_by_key("seq_tempo")
        assert p is not None
        assert p.label_for_value(64) == "64"

    def test_arp_type_labels(self):
        p = seq_param_by_key("arp_type")
        assert p is not None
        assert p.label_for_value(0) == "Off"
        assert p.label_for_value(26) == "Up"

    def test_switch_params(self):
        met = seq_param_by_key("metronome")
        assert met is not None
        assert met.control_type == ControlType.SWITCH
        assert met.value_labels == {0: "Off", 127: "On"}
