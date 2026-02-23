"""Roland S-1 parameter schema — single source of truth for all CC parameters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class AccessLevel(Enum):
    """How the parameter is accessed on the physical S-1."""
    PANEL = "panel"      # Front-panel knob/slider
    SHIFT = "shift"      # Shift + button combo
    MENU = "menu"        # Deep in MENU system
    EXTERNAL = "external"  # Mod wheel, expression, damper


class ControlType(Enum):
    """Widget type for the parameter."""
    CONTINUOUS = "continuous"  # 0-127 slider
    SWITCH = "switch"         # On/off toggle (0 or 127)
    DISCRETE = "discrete"     # Named choices (e.g., waveform)


@dataclass(frozen=True)
class S1Param:
    """A single Roland S-1 MIDI CC parameter."""
    name: str
    cc: int
    section: str
    access: AccessLevel = AccessLevel.PANEL
    control_type: ControlType = ControlType.CONTINUOUS
    min_val: int = 0
    max_val: int = 127
    default: int = 0
    description: str = ""
    value_labels: dict[int, str] = field(default_factory=dict)
    display_format: str = ""

    def label_for_value(self, value: int) -> str:
        """Return human-readable label for a value, or the numeric value."""
        if self.value_labels:
            # For discrete params, find closest matching label
            if value in self.value_labels:
                return self.value_labels[value]
            # Find nearest key
            closest = min(self.value_labels.keys(), key=lambda k: abs(k - value))
            return self.value_labels[closest]
        return str(value)


# ──────────────────────────────────────────────
# All 54 S-1 parameters
# ──────────────────────────────────────────────

S1_PARAMS: tuple[S1Param, ...] = (
    # ── LFO ──
    S1Param("Rate", 3, "LFO", AccessLevel.PANEL),
    S1Param("Waveform", 12, "LFO", AccessLevel.PANEL, ControlType.DISCRETE,
            value_labels={0: "Triangle", 32: "Saw", 64: "Square", 96: "S&H", 127: "Noise"},
            description="LFO waveform shape"),
    S1Param("Mod Depth", 17, "LFO", AccessLevel.PANEL),
    S1Param("Mode", 79, "LFO", AccessLevel.MENU, ControlType.DISCRETE,
            value_labels={0: "Normal", 64: "Fast", 127: "Chaos"}),
    S1Param("Key Trigger", 105, "LFO", AccessLevel.MENU, ControlType.SWITCH,
            value_labels={0: "Off", 127: "On"}),
    S1Param("Sync Mode", 106, "LFO", AccessLevel.MENU, ControlType.DISCRETE,
            description="v1.02+"),

    # ── Oscillator ──
    S1Param("Range", 14, "Oscillator", AccessLevel.PANEL, ControlType.DISCRETE,
            default=64,
            value_labels={0: "16'", 32: "8'", 64: "4'", 96: "2'"}),
    S1Param("Fine Tune", 76, "Oscillator", AccessLevel.MENU, default=64),
    S1Param("Saw Level", 20, "Oscillator", AccessLevel.PANEL, default=127),
    S1Param("Square Level", 19, "Oscillator", AccessLevel.PANEL),
    S1Param("Pulse Width", 15, "Oscillator", AccessLevel.PANEL, default=64),
    S1Param("PWM Source", 16, "Oscillator", AccessLevel.MENU, ControlType.DISCRETE,
            value_labels={0: "LFO", 64: "Manual", 127: "Env"}),
    S1Param("Sub Level", 21, "Oscillator", AccessLevel.PANEL),
    S1Param("Sub Octave Type", 22, "Oscillator", AccessLevel.MENU, ControlType.DISCRETE,
            value_labels={0: "-1 Oct", 32: "-1 Oct Sq", 64: "-2 Oct", 96: "-2 Oct Sq"}),
    S1Param("Noise Level", 23, "Oscillator", AccessLevel.PANEL),
    S1Param("Noise Mode", 78, "Oscillator", AccessLevel.MENU, ControlType.DISCRETE,
            value_labels={0: "White", 127: "Pink"}),
    S1Param("LFO Pitch", 13, "Oscillator", AccessLevel.MENU),
    S1Param("Pitch Bend Sens", 18, "Oscillator", AccessLevel.MENU, default=64),

    # ── Osc Draw/Chop ──
    S1Param("Draw Multiply", 102, "Draw/Chop", AccessLevel.MENU),
    S1Param("Draw Step/Slope", 107, "Draw/Chop", AccessLevel.MENU),
    S1Param("Chop Overtone", 103, "Draw/Chop", AccessLevel.MENU),
    S1Param("Chop Comb", 104, "Draw/Chop", AccessLevel.MENU),

    # ── Filter ──
    S1Param("Frequency", 74, "Filter", AccessLevel.PANEL, default=127),
    S1Param("Resonance", 71, "Filter", AccessLevel.PANEL),
    S1Param("Env Depth", 24, "Filter", AccessLevel.PANEL),
    S1Param("LFO Depth", 25, "Filter", AccessLevel.PANEL),
    S1Param("Key Follow", 26, "Filter", AccessLevel.MENU),
    S1Param("Bend Sensitivity", 27, "Filter", AccessLevel.MENU),

    # ── Envelope ──
    S1Param("Attack", 73, "Envelope", AccessLevel.PANEL),
    S1Param("Decay", 75, "Envelope", AccessLevel.PANEL, default=64),
    S1Param("Sustain", 30, "Envelope", AccessLevel.PANEL, default=127),
    S1Param("Release", 72, "Envelope", AccessLevel.PANEL, default=32),
    S1Param("Amp Env Mode", 28, "Envelope", AccessLevel.MENU, ControlType.DISCRETE,
            value_labels={0: "Gate", 127: "Env"}),
    S1Param("Trigger Mode", 29, "Envelope", AccessLevel.MENU, ControlType.DISCRETE,
            value_labels={0: "Single", 127: "Multi"}),

    # ── Voice ──
    S1Param("Polyphony Mode", 80, "Voice", AccessLevel.MENU, ControlType.DISCRETE,
            value_labels={0: "Poly", 32: "Unison", 64: "Mono", 96: "Legato"}),
    S1Param("Portamento Time", 5, "Voice", AccessLevel.MENU),
    S1Param("Portamento Mode", 31, "Voice", AccessLevel.MENU, ControlType.DISCRETE,
            value_labels={0: "Normal", 127: "Legato"}),
    S1Param("Portamento", 65, "Voice", AccessLevel.MENU, ControlType.SWITCH,
            value_labels={0: "Off", 127: "On"}),
    S1Param("Transpose", 77, "Voice", AccessLevel.MENU),
    S1Param("Pan", 10, "Voice", AccessLevel.MENU, default=64),

    # ── Chord Mode ──
    S1Param("Voice 2 On/Off", 81, "Chord Mode", AccessLevel.MENU, ControlType.SWITCH,
            value_labels={0: "Off", 127: "On"}),
    S1Param("Voice 3 On/Off", 82, "Chord Mode", AccessLevel.MENU, ControlType.SWITCH,
            value_labels={0: "Off", 127: "On"}),
    S1Param("Voice 4 On/Off", 83, "Chord Mode", AccessLevel.MENU, ControlType.SWITCH,
            value_labels={0: "Off", 127: "On"}),
    S1Param("Voice 2 Key Shift", 85, "Chord Mode", AccessLevel.MENU),
    S1Param("Voice 3 Key Shift", 86, "Chord Mode", AccessLevel.MENU),
    S1Param("Voice 4 Key Shift", 87, "Chord Mode", AccessLevel.MENU),

    # ── Effects ──
    S1Param("Delay Time", 90, "Effects", AccessLevel.PANEL),
    S1Param("Delay Level", 92, "Effects", AccessLevel.SHIFT),
    S1Param("Reverb Time", 89, "Effects", AccessLevel.PANEL),
    S1Param("Reverb Level", 91, "Effects", AccessLevel.SHIFT),
    S1Param("Chorus Type", 93, "Effects", AccessLevel.MENU, ControlType.DISCRETE,
            value_labels={0: "Off", 32: "Chorus", 64: "Tremolo", 96: "Flanger"}),

    # ── Controls (External) ──
    S1Param("Mod Wheel", 1, "Controls", AccessLevel.EXTERNAL, default=0),
    S1Param("Expression", 11, "Controls", AccessLevel.EXTERNAL, default=127),
    S1Param("Damper Pedal", 64, "Controls", AccessLevel.EXTERNAL, ControlType.SWITCH,
            value_labels={0: "Off", 127: "On"}),
)


# ──────────────────────────────────────────────
# Sequencer parameters (not CC-controlled)
# ──────────────────────────────────────────────

@dataclass(frozen=True)
class SeqParam:
    """A sequencer parameter (not CC-controlled, device-menu only)."""
    name: str
    key: str          # unique identifier (e.g., "seq_tempo")
    section: str      # "Sequencer" or "Arpeggiator"
    control_type: ControlType = ControlType.CONTINUOUS
    min_val: int = 0
    max_val: int = 127
    default: int = 0
    description: str = ""
    value_labels: dict[int, str] = field(default_factory=dict)

    def label_for_value(self, value: int) -> str:
        """Return human-readable label for a value, or the numeric value."""
        if self.value_labels:
            if value in self.value_labels:
                return self.value_labels[value]
            closest = min(self.value_labels.keys(), key=lambda k: abs(k - value))
            return self.value_labels[closest]
        return str(value)


SEQ_PARAMS: tuple[SeqParam, ...] = (
    # ── Sequencer ──
    SeqParam("SEQ Tempo", "seq_tempo", "Sequencer",
             ControlType.CONTINUOUS, 0, 127, 64,
             description="Sequencer BPM (internal clock)"),
    SeqParam("SEQ Shuffle", "seq_shuffle", "Sequencer",
             ControlType.CONTINUOUS, 0, 127, 0,
             description="Shuffle amount (50-75%)"),
    SeqParam("SEQ Gate", "seq_gate", "Sequencer",
             ControlType.CONTINUOUS, 0, 127, 64,
             description="Gate length ratio"),
    SeqParam("Last Step", "last_step", "Sequencer",
             ControlType.CONTINUOUS, 1, 64, 16,
             description="Pattern length (1-64 steps)"),
    SeqParam("Scale", "seq_scale", "Sequencer",
             ControlType.DISCRETE,
             value_labels={0: "1/4", 32: "1/8", 64: "1/16", 96: "1/32", 127: "1/64"},
             default=64,
             description="Step resolution"),
    SeqParam("Master Prob", "master_prob", "Sequencer",
             ControlType.CONTINUOUS, 0, 127, 127,
             description="Global note probability"),
    SeqParam("Metronome", "metronome", "Sequencer",
             ControlType.SWITCH, 0, 127, 0,
             value_labels={0: "Off", 127: "On"}),
    SeqParam("Count-In", "count_in", "Sequencer",
             ControlType.SWITCH, 0, 127, 0,
             value_labels={0: "Off", 127: "On"}),

    # ── Arpeggiator ──
    SeqParam("ARP Type", "arp_type", "Arpeggiator",
             ControlType.DISCRETE,
             value_labels={0: "Off", 26: "Up", 51: "Down", 77: "Up&Down", 102: "Random", 127: "Note Order"},
             description="Arpeggiator pattern type"),
    SeqParam("ARP Note Len", "arp_note_len", "Arpeggiator",
             ControlType.DISCRETE,
             value_labels={0: "1/4", 32: "1/8", 64: "1/16", 96: "1/32"},
             description="Arpeggiator note length"),
)

_SEQ_KEY_INDEX: dict[str, SeqParam] = {p.key: p for p in SEQ_PARAMS}


def seq_param_by_key(key: str) -> SeqParam | None:
    """Look up a sequencer parameter by key."""
    return _SEQ_KEY_INDEX.get(key)


def seq_params_by_section(section: str) -> list[SeqParam]:
    """Return all sequencer params in a given section."""
    return [p for p in SEQ_PARAMS if p.section == section]


def all_seq_sections() -> list[str]:
    """Return ordered list of sequencer section names."""
    seen: list[str] = []
    for p in SEQ_PARAMS:
        if p.section not in seen:
            seen.append(p.section)
    return seen


# ──────────────────────────────────────────────
# Lookup utilities
# ──────────────────────────────────────────────

_CC_INDEX: dict[int, S1Param] = {p.cc: p for p in S1_PARAMS}
_SECTION_INDEX: dict[str, list[S1Param]] = {}
for _p in S1_PARAMS:
    _SECTION_INDEX.setdefault(_p.section, []).append(_p)


def param_by_cc(cc: int) -> S1Param | None:
    """Look up a parameter by CC number."""
    return _CC_INDEX.get(cc)


def params_by_section(section: str) -> list[S1Param]:
    """Return all params in a given section."""
    return _SECTION_INDEX.get(section, [])


def params_by_access(access: AccessLevel) -> list[S1Param]:
    """Return all params with a given access level."""
    return [p for p in S1_PARAMS if p.access == access]


def panel_params() -> list[S1Param]:
    """Return front-panel (directly accessible) params."""
    return params_by_access(AccessLevel.PANEL)


def menu_params() -> list[S1Param]:
    """Return all non-panel params (SHIFT + MENU + EXTERNAL)."""
    return [p for p in S1_PARAMS if p.access in (AccessLevel.SHIFT, AccessLevel.MENU, AccessLevel.EXTERNAL)]


def all_sections() -> list[str]:
    """Return ordered list of section names."""
    seen: list[str] = []
    for p in S1_PARAMS:
        if p.section not in seen:
            seen.append(p.section)
    return seen
