"""Tests for schema.py — the schema must mirror the device 1:1.

Ground truth encoded here: the official Roland S-1 MIDI implementation
chart v1.02 ("Knob assignments" manual page + midi.guide/d/roland/s-1).
"""

from synth.schema import (
    PARAM_KINDS,
    PRM_PARAMS,
    S1_PARAMS,
    SEQ_PARAMS,
    SETTINGS_MENU,
    AccessLevel,
    ControlType,
    all_sections,
    all_seq_sections,
    load_device_file,
    menu_params,
    panel_params,
    param_by_cc,
    params_by_access,
    params_by_section,
    prm_param_by_key,
    sections_for_access,
    seq_param_by_key,
    seq_params_by_section,
    settings_menu_params,
)

# ──────────────────────────────────────────────────────────────
# The complete documented CC chart (official chart v1.02).
# name → CC, exactly as published. 54 assignments.
# ──────────────────────────────────────────────────────────────
OFFICIAL_CHART_CCS = {
    1: "Modulation",
    3: "LFO Rate",
    5: "Portamento Time",
    10: "Pan",
    11: "Expression",
    12: "LFO Waveform",
    13: "Oscillator LFO Pitch",
    14: "Oscillator Range",
    15: "Oscillator Square Pulse Width",
    16: "Oscillator PWM Source",
    17: "LFO Modulation Depth",
    18: "Oscillator Pitch Bend Sensitivity",
    19: "Oscillator Square Level",
    20: "Oscillator Saw Level",
    21: "Oscillator Sub Level",
    22: "Oscillator Sub Octave Type",
    23: "Oscillator Noise Level",
    24: "Filter Envelope Depth",
    25: "Filter LFO Depth",
    26: "Filter Keyboard Follow",
    27: "Filter Bend Sensitivity",
    28: "Amp Envelope Mode SW",
    29: "Envelope Trigger Mode",
    30: "Envelope Sustain",
    31: "Portamento Mode",
    64: "Damper Pedal",
    65: "Portamento",
    71: "Filter Resonance",
    72: "Envelope Release",
    73: "Envelope Attack",
    74: "Filter Frequency",
    75: "Envelope Decay",
    76: "Oscillator Range Fine Tune",
    77: "Keyboard Transpose",
    78: "Noise Mode",
    79: "LFO Mode",
    80: "Polyphony Mode",
    81: "Chord Mode Voice 2 On/Off",
    82: "Chord Mode Voice 3 On/Off",
    83: "Chord Mode Voice 4 On/Off",
    85: "Chord Mode Voice 2 Key Shift",
    86: "Chord Mode Voice 3 Key Shift",
    87: "Chord Mode Voice 4 Key Shift",
    89: "EFX Reverb Time",
    90: "EFX Delay Time",
    91: "EFX Reverb Level",
    92: "EFX Delay Level",
    93: "Chorus Type",
    102: "Oscillator Draw Multiply",
    103: "Oscillator Chop Overtone",
    104: "Oscillator Chop Comb",
    105: "LFO Key Trigger",
    106: "LFO Sync Mode",
    107: "Oscillator Draw Step/Slope",
}

# The S-1's faceplate sections, in physical order, plus the MIDI-only tier.
FACEPLATE_SECTIONS = ["LFO", "OSC", "FILTER", "AMP", "ENV", "EFX", "CONTROLLER", "MIDI"]


class TestOfficialChart:
    def test_cc_set_equals_documented_chart(self):
        """The schema's CC set must equal the official chart's, exactly."""
        schema_ccs = {p.cc for p in S1_PARAMS}
        chart_ccs = set(OFFICIAL_CHART_CCS)
        missing = chart_ccs - schema_ccs
        extra = schema_ccs - chart_ccs
        assert not missing, f"Chart CCs missing from schema: {sorted(missing)}"
        assert not extra, f"Schema CCs not in the chart: {sorted(extra)}"

    def test_total_param_count(self):
        assert len(S1_PARAMS) == len(OFFICIAL_CHART_CCS) == 54

    def test_all_cc_numbers_unique(self):
        ccs = [p.cc for p in S1_PARAMS]
        assert len(ccs) == len(set(ccs))


class TestHardwareOrganization:
    """Section/menu grouping matches the manual's organization."""

    def test_sections_are_the_faceplate_sections(self):
        assert all_sections() == FACEPLATE_SECTIONS

    def test_lfo_section(self):
        assert {p.cc for p in params_by_section("LFO")} == {3, 12, 79, 105, 106}

    def test_osc_section(self):
        # Range/LFO/mixer knobs + PWM, fine tune, sub/noise types, draw/chop.
        assert {p.cc for p in params_by_section("OSC")} == {
            13, 14, 15, 16, 19, 20, 21, 22, 23, 76, 78, 102, 103, 104, 107,
        }

    def test_filter_section(self):
        assert {p.cc for p in params_by_section("FILTER")} == {24, 25, 26, 71, 74}

    def test_amp_section(self):
        assert {p.cc for p in params_by_section("AMP")} == {28}

    def test_env_section(self):
        assert {p.cc for p in params_by_section("ENV")} == {29, 30, 72, 73, 75}

    def test_efx_section(self):
        assert {p.cc for p in params_by_section("EFX")} == {89, 90, 91, 92, 93}

    def test_controller_section(self):
        # POLY / chord / portamento / transpose pads + menu bend & mod depth.
        assert {p.cc for p in params_by_section("CONTROLLER")} == {
            5, 17, 18, 27, 31, 65, 77, 80, 81, 82, 83, 85, 86, 87,
        }

    def test_midi_only_section(self):
        assert {p.cc for p in params_by_section("MIDI")} == {1, 10, 11, 64}

    def test_knob_normal_operations_are_panel(self):
        """Normal knob functions, per the manual's Knob assignments table."""
        panel_ccs = {p.cc for p in params_by_access(AccessLevel.PANEL)}
        assert panel_ccs == {
            3, 12,               # LFO rate / waveform
            14, 13, 19, 20, 21, 23, 102,  # OSC range, LFO, mixer, draw multiply
            74, 71, 25, 24,      # FILTER
            73, 75, 30, 72,      # ENV ADSR
            92, 91,              # EFX delay/reverb LEVEL (normal op = level)
        }

    def test_delay_reverb_level_is_normal_time_is_shift(self):
        """Manual: [DELAY]/[REVERB] adjust LEVEL; TIME needs SHIFT."""
        assert param_by_cc(92).access == AccessLevel.PANEL
        assert param_by_cc(91).access == AccessLevel.PANEL
        assert param_by_cc(90).access == AccessLevel.SHIFT
        assert param_by_cc(89).access == AccessLevel.SHIFT

    def test_settings_menu_matches_manual_order(self):
        """Menu list order from the manual's "Using the menus" page."""
        codes = [code for code, _ in SETTINGS_MENU]
        assert codes == ["vOL", "Nod.d", "bnd.o", "bnd.F", "nS.Nd",
                         "rS.Nd", "rS.rS", "rS.Sh", "rS.Lv",
                         "LFO.N", "LFO.S", "LFO.K", "Cho", "trAn", "P.SCL"]

    def test_settings_menu_params_resolve(self):
        resolved = settings_menu_params()
        assert len(resolved) == len(SETTINGS_MENU)
        by_code = dict(resolved)
        assert by_code["Nod.d"].cc == 17
        assert by_code["Cho"].cc == 93
        assert by_code["vOL"].key == "LEVEL"
        assert by_code["P.SCL"].key == "SCALE"

    def test_menu_access_params(self):
        menu_ccs = {p.cc for p in params_by_access(AccessLevel.MENU)}
        assert menu_ccs == {17, 18, 27, 93, 105}

    def test_external_params(self):
        ext = {p.cc for p in params_by_access(AccessLevel.EXTERNAL)}
        assert ext == {1, 10, 11, 64}

    def test_access_levels_cover_all_params(self):
        total = sum(len(params_by_access(level)) for level in AccessLevel)
        assert total == 54  # PRM tier holds no CC params

    def test_panel_params_helper_is_faceplate(self):
        assert panel_params() == [
            p for p in S1_PARAMS if p.access in (AccessLevel.PANEL, AccessLevel.SHIFT)
        ]

    def test_menu_params_helper(self):
        assert menu_params() == [
            p for p in S1_PARAMS if p.access in (AccessLevel.MENU, AccessLevel.EXTERNAL)
        ]

    def test_sections_for_access(self):
        groups = sections_for_access((AccessLevel.PANEL, AccessLevel.SHIFT))
        names = [name for name, _ in groups]
        assert names == ["LFO", "OSC", "FILTER", "AMP", "ENV", "EFX", "CONTROLLER"]


class TestValueEncodings:
    """Selector encodings per the manual + hardware-verified S1Utility map."""

    def test_lfo_waveforms(self):
        wf = param_by_cc(12)
        assert wf.control_type == ControlType.DISCRETE
        assert wf.value_labels == {0: "Saw", 1: "Inv Saw", 2: "Triangle",
                                   3: "Square", 4: "Random", 5: "Noise"}
        assert wf.max_val == 5

    def test_osc_range_footage(self):
        rng = param_by_cc(14)
        assert rng.value_labels == {0: "64'", 1: "32'", 2: "16'", 3: "8'", 4: "4'", 5: "2'"}

    def test_chorus_is_type_selector(self):
        cho = param_by_cc(93)
        assert cho.control_type == ControlType.DISCRETE
        assert cho.value_labels[0] == "Off"
        assert len(cho.value_labels) == 5  # Off + types 1-4

    def test_polyphony_modes(self):
        poly = param_by_cc(80)
        assert poly.value_labels == {0: "Mono", 1: "Unison", 2: "Poly", 3: "Chord"}

    def test_env_trigger_modes(self):
        trg = param_by_cc(29)
        assert trg.value_labels == {0: "LFO", 1: "Gate", 2: "Gate+Trig"}

    def test_portamento_mode_off_auto_on(self):
        pm = param_by_cc(31)
        assert pm.value_labels == {0: "Off", 1: "Auto", 2: "On"}

    def test_sub_oct_types(self):
        sub = param_by_cc(22)
        assert sub.value_labels == {0: "-2 Oct Asym", 1: "-2 Oct", 2: "-1 Oct"}

    def test_noise_modes(self):
        nm = param_by_cc(78)
        assert nm.value_labels == {0: "Pink", 1: "White"}

    def test_draw_switch(self):
        ds = param_by_cc(107)
        assert ds.value_labels == {0: "Off", 1: "Step", 2: "Slope"}

    def test_switches_use_0_127(self):
        for cc in (64, 65, 81, 82, 83):
            p = param_by_cc(cc)
            assert p.control_type == ControlType.SWITCH
            assert set(p.value_labels) == {0, 127}

    def test_anchored_draw_chop_floor(self):
        """Draw Multiply / Chop Comb bottom out at CC 3 (display x1.0)."""
        assert param_by_cc(102).min_val == 3
        assert param_by_cc(104).min_val == 3

    def test_discrete_values_are_option_indexes(self):
        """DISCRETE params span exactly 0..N-1 with a label per index."""
        for p in S1_PARAMS:
            if p.control_type == ControlType.DISCRETE:
                n = len(p.value_labels)
                assert p.min_val == 0 and p.max_val == n - 1, p.name
                assert set(p.value_labels) == set(range(n)), p.name


class TestDefaults:
    """Defaults come from the device's factory init patch."""

    def test_default_within_range(self):
        for p in S1_PARAMS:
            assert p.min_val <= p.default <= p.max_val, p.name

    def test_min_less_than_max(self):
        for p in S1_PARAMS:
            assert p.min_val < p.max_val, p.name

    def test_init_patch_key_values(self):
        assert param_by_cc(74).default == 127   # cutoff fully open
        assert param_by_cc(19).default == 127   # square level full
        assert param_by_cc(20).default == 0     # saw silent
        assert param_by_cc(3).default == 60     # LFO rate (PRM 120)
        assert param_by_cc(12).default == 2     # triangle
        assert param_by_cc(80).default == 2     # poly
        assert param_by_cc(76).default == 64    # fine tune centered
        assert param_by_cc(77).default == 64    # transpose 0
        assert param_by_cc(10).default == 64    # pan centered
        # The chord-voice key shifts deliberately depart from the factory
        # patch (76/71/69 = +12/+7/+5): see TestCanonicalDeviceFile.
        assert param_by_cc(85).default == 64    # chord voice 2 centered
        assert param_by_cc(86).default == 64    # chord voice 3 centered
        assert param_by_cc(87).default == 64    # chord voice 4 centered


class TestLabelForValue:
    def test_exact_match(self):
        wf = param_by_cc(12)
        assert wf.label_for_value(0) == "Saw"
        assert wf.label_for_value(5) == "Noise"

    def test_continuous_returns_str(self):
        rate = param_by_cc(3)
        assert rate.label_for_value(64) == "64"

    def test_nearest_match(self):
        damper = param_by_cc(64)
        assert damper.label_for_value(100) == "On"
        assert damper.label_for_value(20) == "Off"

    def test_s1param_is_frozen(self):
        p = param_by_cc(74)
        try:
            p.name = "something"
            assert False, "frozen dataclass should reject assignment"
        except AttributeError:
            pass


class TestPrmTier:
    """The third access tier: PRM-only parameters (no CC)."""

    def test_prm_access_level_exists(self):
        assert AccessLevel.PRM.value == "prm"

    def test_known_prm_keys_present(self):
        for key in ("REVERB_TYPE", "REVERB_PRE_DELAY", "DELAY_FEEDBACK",
                    "DELAY_TEMPO", "RISER_MODE", "DM_ASSIGN_X",
                    "OSC_CHOP_PWM", "OSC_DRAW_P1", "LEVEL", "SCALE"):
            assert prm_param_by_key(key) is not None, key

    def test_prm_keys_unique(self):
        keys = [p.key for p in PRM_PARAMS]
        assert len(keys) == len(set(keys))

    def test_prm_lookup_case_insensitive(self):
        assert prm_param_by_key("reverb_type") is prm_param_by_key("REVERB_TYPE")

    def test_prm_defaults_within_range(self):
        for p in PRM_PARAMS:
            assert p.min_val <= p.default <= p.max_val, p.key

    def test_reverb_types_from_manual(self):
        rt = prm_param_by_key("REVERB_TYPE")
        assert rt.value_labels == {0: "Ambience", 1: "Room", 2: "Hall 1",
                                   3: "Hall 2", 4: "Plate", 5: "Spring", 6: "Modulate"}

    def test_dm_destinations(self):
        dm = prm_param_by_key("DM_ASSIGN_X")
        assert len(dm.value_labels) == 9
        assert dm.value_labels[0] == "Off"

    def test_prm_param_not_found(self):
        assert prm_param_by_key("NOT_A_KEY") is None


class TestLookupUtilities:
    def test_param_by_cc_found(self):
        p = param_by_cc(74)
        assert p is not None
        assert p.name == "Frequency"

    def test_param_by_cc_not_found(self):
        assert param_by_cc(999) is None

    def test_params_by_section_unknown(self):
        assert params_by_section("Nonexistent") == []


class TestSeqParams:
    def test_seq_param_count(self):
        assert len(SEQ_PARAMS) == 8

    def test_all_keys_unique(self):
        keys = [p.key for p in SEQ_PARAMS]
        assert len(keys) == len(set(keys))

    def test_lookup_by_key(self):
        p = seq_param_by_key("seq_tempo")
        assert p is not None
        assert p.name == "SEQ Tempo"

    def test_lookup_not_found(self):
        assert seq_param_by_key("nonexistent") is None

    def test_sequencer_section(self):
        assert len(seq_params_by_section("Sequencer")) == 8
        assert all_seq_sections() == ["Sequencer"]

    def test_default_within_range(self):
        for p in SEQ_PARAMS:
            assert p.min_val <= p.default <= p.max_val, p.key


# ──────────────────────────────────────────────────────────────
# The canonical device file (data/s1.json) — drift guard.
# ──────────────────────────────────────────────────────────────

class TestCanonicalDeviceFile:
    """The CC table is data now: ``synth/data/s1.json``.

    The music project vendors a copy of this file; its own test compares the
    two. This side pins the facts that made the file necessary.
    """

    def test_device_file_has_all_54_params(self):
        doc = load_device_file()
        assert len(doc["params"]) == 54
        assert len(S1_PARAMS) == 54

    def test_chord_voice_key_shifts_are_centered(self):
        """CC 85/86/87 default to 64 — the fix, not the factory value.

        The factory init patch ships 76/71/69 (+12/+7/+5 semitones). In chord
        mode those overlay a transposed copy of every note played; a synth
        that starts by transposing itself is broken (found live 2026-07-28).
        """
        for cc in (85, 86, 87):
            param = param_by_cc(cc)
            assert param is not None, cc
            assert param.default == 64, f"CC {cc} key shift must be centered"

    def test_loaded_table_matches_the_file(self):
        entries = {e["cc"]: e for e in load_device_file()["params"]}
        assert len(entries) == len(S1_PARAMS)
        for param in S1_PARAMS:
            entry = entries[param.cc]
            assert param.name == entry["name"]
            assert param.section == entry["section"]
            assert param.min_val == entry["min"]
            assert param.max_val == entry["max"]
            assert param.default == entry["default"]
            assert param.access.value == entry["access"]
            assert param.control_type.value == entry["control_type"]

    def test_every_param_carries_a_ks_kind(self):
        """The k/s tag exists for the music project; it must stay complete."""
        assert set(PARAM_KINDS) == {p.cc for p in S1_PARAMS}
        assert set(PARAM_KINDS.values()) <= {"k", "s"}

    def test_defaults_within_range(self):
        for p in S1_PARAMS:
            assert p.min_val <= p.default <= p.max_val, p.name
