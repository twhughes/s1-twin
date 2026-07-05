"""Roland S-1 parameter schema — single source of truth for every parameter.

Mirrors the device 1:1, organized exactly as the hardware:

- CC parameters live in the faceplate section that owns their control
  (LFO / OSC / FILTER / AMP / ENV / EFX / CONTROLLER), with the access level
  saying *how* the control is reached (knob, SHIFT combo, settings menu, or
  MIDI-only). Sources: official MIDI implementation chart v1.02, the manual's
  "Knob assignments" page, and midi.guide/d/roland/s-1.
- Selector (DISCRETE) parameters use option-index CC values (0..N-1) and
  switches use 0/127 — the encoding verified against hardware by the
  denzlobin/S1Utility project.
- Defaults are the device's factory init patch (a real ``InitPatch.prm``
  backup, PRM values scaled to CC domain).
- PRM-only parameters (no CC equivalent; reachable only through .PRM pattern
  files) form a third tier: :data:`PRM_PARAMS`. The set below is what the
  community has decoded so far; completing it is the M3 librarian milestone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class AccessLevel(Enum):
    """How the parameter is accessed on the physical S-1."""
    PANEL = "panel"        # Knob / control, normal operation
    SHIFT = "shift"        # SHIFT + knob or SHIFT + pad combo
    MENU = "menu"          # Settings menu (SHIFT + pad 15)
    EXTERNAL = "external"  # MIDI-only performance control, no physical control
    PRM = "prm"            # Only exists in .PRM pattern files (no CC)


class ControlType(Enum):
    """Widget type for the parameter."""
    CONTINUOUS = "continuous"  # value slider
    SWITCH = "switch"          # On/off toggle (0 or 127)
    DISCRETE = "discrete"      # Named choices; CC value = option index


def _label_for_value(value_labels: dict[int, str], value: int) -> str:
    """Human-readable label for a value: exact match, else nearest key."""
    if not value_labels:
        return str(value)
    if value in value_labels:
        return value_labels[value]
    closest = min(value_labels.keys(), key=lambda k: abs(k - value))
    return value_labels[closest]


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
    # Rendering hint for value readouts: "" (raw), "signed64" (value-64),
    # "mult" (draw/comb x1.0-x32.0), "chop200" (overtone 0-200 display).
    display_format: str = ""
    # Display code in the manual's settings menu (e.g. "Nod.d"), when the
    # parameter appears there.
    menu_item: str = ""

    def label_for_value(self, value: int) -> str:
        """Return human-readable label for a value, or the numeric value."""
        return _label_for_value(self.value_labels, value)


_ON_OFF = {0: "Off", 127: "On"}

# ──────────────────────────────────────────────────────────────
# All 54 CC parameters, grouped by faceplate section.
# Defaults: factory init patch (InitPatch.prm), PRM→CC scaled.
# ──────────────────────────────────────────────────────────────

S1_PARAMS: tuple[S1Param, ...] = (
    # ── LFO ── [RATE] and [WAVE FORM] knobs; MODE/SYNC on shift.
    S1Param("Rate", 3, "LFO", AccessLevel.PANEL, default=60,
            description="LFO/random speed. With LFO Sync on, selects a note length instead."),
    S1Param("Waveform", 12, "LFO", AccessLevel.PANEL, ControlType.DISCRETE,
            max_val=5, default=2,
            value_labels={0: "Saw", 1: "Inv Saw", 2: "Triangle", 3: "Square",
                          4: "Random", 5: "Noise"},
            description="Modulator output signal (LFO shapes, random, or noise)."),
    S1Param("Mode", 79, "LFO", AccessLevel.SHIFT, ControlType.DISCRETE,
            max_val=1, default=0, menu_item="LFO.N",
            value_labels={0: "Normal", 1: "Fast"},
            description="LFO speed range; Fast raises the cycle to extremes. "
                        "Disabled while LFO Sync is on. SHIFT + [RATE]."),
    S1Param("Sync", 106, "LFO", AccessLevel.SHIFT, ControlType.DISCRETE,
            max_val=1, default=0, menu_item="LFO.S",
            value_labels={0: "Off", 1: "On"},
            description="Sync the LFO cycle to the tempo (v1.02+). SHIFT + [WAVE FORM]."),
    S1Param("Key Trigger", 105, "LFO", AccessLevel.MENU, ControlType.DISCRETE,
            max_val=1, default=0, menu_item="LFO.K",
            value_labels={0: "Off", 1: "On"},
            description="Reset the LFO whenever a note sounds."),

    # ── OSC ── source mixer knobs + RANGE/LFO; draw/chop and switches on shift.
    S1Param("Range", 14, "OSC", AccessLevel.PANEL, ControlType.DISCRETE,
            max_val=5, default=2,
            value_labels={0: "64'", 1: "32'", 2: "16'", 3: "8'", 4: "4'", 5: "2'"},
            description="Oscillator frequency range in octaves (footage)."),
    S1Param("LFO Depth", 13, "OSC", AccessLevel.PANEL, default=0,
            description="Pitch modulation intensity from the LFO section."),
    S1Param("Square Level", 19, "OSC", AccessLevel.PANEL, default=127,
            description="Level of the square wave (or the OSC DRAW waveform)."),
    S1Param("Saw Level", 20, "OSC", AccessLevel.PANEL, default=0,
            description="Level of the sawtooth wave."),
    S1Param("Sub Level", 21, "OSC", AccessLevel.PANEL, default=0,
            description="Level of the sub oscillator."),
    S1Param("Noise Level", 23, "OSC", AccessLevel.PANEL, default=0,
            description="Level of the noise source (riser control when Riser Mode is on)."),
    S1Param("Pulse Width", 15, "OSC", AccessLevel.SHIFT, default=0,
            description="Square pulse width value, or PWM depth when the source "
                        "is LFO/Envelope. SHIFT + [PWM DEPTH] pad."),
    S1Param("PWM Source", 16, "OSC", AccessLevel.SHIFT, ControlType.DISCRETE,
            max_val=2, default=2,
            value_labels={0: "Envelope", 1: "Manual", 2: "LFO"},
            description="Pulse-width control source. SHIFT + [PWM SRC] pad."),
    S1Param("Fine Tune", 76, "OSC", AccessLevel.SHIFT, default=64,
            display_format="signed64",
            description="Pitch within ±1 octave. SHIFT + [RANGE]."),
    S1Param("Sub Oct Type", 22, "OSC", AccessLevel.SHIFT, ControlType.DISCRETE,
            max_val=2, default=2,
            value_labels={0: "-2 Oct Asym", 1: "-2 Oct", 2: "-1 Oct"},
            description="Sub-oscillator output waveform. SHIFT + [SUB OCT] pad."),
    S1Param("Noise Mode", 78, "OSC", AccessLevel.SHIFT, ControlType.DISCRETE,
            max_val=1, default=0, menu_item="nS.Nd",
            value_labels={0: "Pink", 1: "White"},
            description="Noise output type. SHIFT + [NOISE] (or menu nS.Nd)."),
    S1Param("Draw Switch", 107, "OSC", AccessLevel.SHIFT, ControlType.DISCRETE,
            max_val=2, default=0,
            value_labels={0: "Off", 1: "Step", 2: "Slope"},
            description="OSC DRAW original-waveform mode. SHIFT + saw [LEVEL] knob."),
    S1Param("Draw Multiply", 102, "OSC", AccessLevel.PANEL,
            min_val=3, default=3, display_format="mult",
            description="OSC DRAW frequency multiplier ×1.0-×32.0 (pulse-width "
                        "knob while Draw Switch is Step/Slope)."),
    S1Param("Chop Overtone", 103, "OSC", AccessLevel.SHIFT,
            default=50, display_format="chop200",
            description="OSC CHOP overtone amount (0-200 display). SHIFT + [LFO]. "
                        "Audible only when a chop grid mask is engaged."),
    S1Param("Chop Comb", 104, "OSC", AccessLevel.SHIFT,
            min_val=3, default=3, display_format="mult",
            description="OSC CHOP comb frequency ×1.0-×32.0. SHIFT + [SUB]."),

    # ── FILTER ── FREQ/RESO/LFO/ENV knobs; key follow on shift.
    S1Param("Frequency", 74, "FILTER", AccessLevel.PANEL, default=127,
            description="Low-pass filter cutoff point."),
    S1Param("Resonance", 71, "FILTER", AccessLevel.PANEL, default=0,
            description="Emphasis around the cutoff point; maximum self-oscillates."),
    S1Param("LFO Depth", 25, "FILTER", AccessLevel.PANEL, default=0,
            description="Cutoff modulation intensity from the LFO section."),
    S1Param("Env Depth", 24, "FILTER", AccessLevel.PANEL, default=0,
            description="Cutoff modulation intensity from the envelope."),
    S1Param("Key Follow", 26, "FILTER", AccessLevel.SHIFT, default=0,
            description="Cutoff tracks keyboard pitch (255 display = perfect "
                        "follow). SHIFT + pad 7 (FILTER KYBD)."),

    # ── AMP ── level control source.
    S1Param("Env Mode", 28, "AMP", AccessLevel.SHIFT, ControlType.DISCRETE,
            max_val=1, default=1,
            value_labels={0: "Gate", 1: "Envelope"},
            description="Control the amp with the gate signal or the envelope. "
                        "SHIFT + [AMP] pad."),

    # ── ENV ── ADSR knobs; trigger mode on shift.
    S1Param("Attack", 73, "ENV", AccessLevel.PANEL, default=0,
            description="Time to reach peak level after note-on."),
    S1Param("Decay", 75, "ENV", AccessLevel.PANEL, default=42,
            description="Time from peak down to the sustain level."),
    S1Param("Sustain", 30, "ENV", AccessLevel.PANEL, default=25,
            description="Held level while the key stays down."),
    S1Param("Release", 72, "ENV", AccessLevel.PANEL, default=21,
            description="Time to silence after note-off."),
    S1Param("Trigger Mode", 29, "ENV", AccessLevel.SHIFT, ControlType.DISCRETE,
            max_val=2, default=2,
            value_labels={0: "LFO", 1: "Gate", 2: "Gate+Trig"},
            description="Which signal (re)triggers the envelope. SHIFT + [ENV TRG] pad."),

    # ── EFX ── DELAY/REVERB knobs (level normal, time on shift); chorus in menu.
    S1Param("Delay Level", 92, "EFX", AccessLevel.PANEL, default=0,
            description="Delay send volume ([DELAY] knob)."),
    S1Param("Delay Time", 90, "EFX", AccessLevel.SHIFT, default=87,
            description="Delay time (1-740 ms, or a note value when Delay Sync "
                        "is on). SHIFT + [DELAY]."),
    S1Param("Reverb Level", 91, "EFX", AccessLevel.PANEL, default=0,
            description="Reverb volume ([REVERB] knob)."),
    S1Param("Reverb Time", 89, "EFX", AccessLevel.SHIFT, default=100,
            description="Reverberation length. SHIFT + [REVERB]."),
    S1Param("Chorus", 93, "EFX", AccessLevel.MENU, ControlType.DISCRETE,
            max_val=4, default=0, menu_item="Cho",
            value_labels={0: "Off", 1: "Type 1", 2: "Type 2", 3: "Type 3", 4: "Type 4"},
            description="Chorus type: 1 standard, 2 faster, 3 rotary-like, 4 relaxed."),

    # ── CONTROLLER ── POLY / chord / portamento / transpose pads; bend & mod in menu.
    S1Param("Polyphony", 80, "CONTROLLER", AccessLevel.SHIFT, ControlType.DISCRETE,
            max_val=3, default=2,
            value_labels={0: "Mono", 1: "Unison", 2: "Poly", 3: "Chord"},
            description="How the sound source is triggered. SHIFT + [POLY] pad."),
    S1Param("Voice 2 Switch", 81, "CONTROLLER", AccessLevel.SHIFT, ControlType.SWITCH,
            default=127, value_labels=_ON_OFF,
            description="Chord mode: voice 2 on/off."),
    S1Param("Voice 3 Switch", 82, "CONTROLLER", AccessLevel.SHIFT, ControlType.SWITCH,
            default=127, value_labels=_ON_OFF,
            description="Chord mode: voice 3 on/off."),
    S1Param("Voice 4 Switch", 83, "CONTROLLER", AccessLevel.SHIFT, ControlType.SWITCH,
            default=127, value_labels=_ON_OFF,
            description="Chord mode: voice 4 on/off."),
    S1Param("Voice 2 Key Shift", 85, "CONTROLLER", AccessLevel.SHIFT,
            default=76, display_format="signed64",
            description="Chord mode: voice 2 transpose (hardware range ±12 semitones)."),
    S1Param("Voice 3 Key Shift", 86, "CONTROLLER", AccessLevel.SHIFT,
            default=71, display_format="signed64",
            description="Chord mode: voice 3 transpose (hardware range ±12 semitones)."),
    S1Param("Voice 4 Key Shift", 87, "CONTROLLER", AccessLevel.SHIFT,
            default=69, display_format="signed64",
            description="Chord mode: voice 4 transpose (hardware range ±12 semitones)."),
    S1Param("Portamento", 65, "CONTROLLER", AccessLevel.SHIFT, ControlType.SWITCH,
            default=0, value_labels=_ON_OFF,
            description="Standard MIDI portamento switch."),
    S1Param("Portamento Mode", 31, "CONTROLLER", AccessLevel.SHIFT, ControlType.DISCRETE,
            max_val=2, default=0,
            value_labels={0: "Off", 1: "Auto", 2: "On"},
            description="Off / Auto (legato only) / On. SHIFT + [PORTA ON] pad."),
    S1Param("Portamento Time", 5, "CONTROLLER", AccessLevel.SHIFT, default=15,
            description="Glide time between pitches. SHIFT + [PORTA TIME] pad."),
    S1Param("Transpose", 77, "CONTROLLER", AccessLevel.SHIFT, default=64,
            display_format="signed64",
            description="Tonal range ±60 half steps. SHIFT + [STEP]. Menu trAn.",
            menu_item="trAn"),
    S1Param("LFO Mod Depth", 17, "CONTROLLER", AccessLevel.MENU, default=15,
            menu_item="Nod.d",
            description="Vibrato/growl depth applied from the LFO when "
                        "modulation (mod wheel / D-MOTION) is used."),
    S1Param("Osc Bend Sens", 18, "CONTROLLER", AccessLevel.MENU, default=20,
            menu_item="bnd.o",
            description="Pitch-bend range for the OSC (display 0-240; 120 = ±1 oct)."),
    S1Param("Filter Bend Sens", 27, "CONTROLLER", AccessLevel.MENU, default=0,
            menu_item="bnd.F",
            description="Pitch-bend range for the filter cutoff."),

    # ── MIDI ── performance controls with no physical control on the S-1.
    S1Param("Mod Wheel", 1, "MIDI", AccessLevel.EXTERNAL, default=0,
            description="Modulation input (depth set by LFO Mod Depth)."),
    S1Param("Pan", 10, "MIDI", AccessLevel.EXTERNAL, default=64,
            display_format="signed64",
            description="Stereo pan (also a D-MOTION destination)."),
    S1Param("Expression", 11, "MIDI", AccessLevel.EXTERNAL, default=127,
            description="Expression pedal volume."),
    S1Param("Damper Pedal", 64, "MIDI", AccessLevel.EXTERNAL, ControlType.SWITCH,
            default=0, value_labels=_ON_OFF,
            description="Hold pedal."),
)


# ──────────────────────────────────────────────────────────────
# The manual's settings menu (SHIFT + pad 15), in manual order.
# Entries are (menu code, CC) for CC-backed items and
# (menu code, PRM key) for PRM-only items.
# ──────────────────────────────────────────────────────────────

SETTINGS_MENU: tuple[tuple[str, int | str], ...] = (
    ("vOL", "LEVEL"),
    ("Nod.d", 17),
    ("bnd.o", 18),
    ("bnd.F", 27),
    ("nS.Nd", 78),
    ("rS.Nd", "RISER_MODE"),
    ("rS.rS", "RISER_RESO"),
    ("rS.Sh", "RISER_SHAPE"),
    ("rS.Lv", "RISER_LEVEL"),
    ("LFO.N", 79),
    ("LFO.S", 106),
    ("LFO.K", 105),
    ("Cho", 93),
    ("trAn", 77),
    ("P.SCL", "SCALE"),
)


# ──────────────────────────────────────────────────────────────
# PRM-only parameters (third access tier — no CC equivalent).
# Known keys from the community-decoded .PRM format; ranges/labels
# from the manual's EFX / riser / D-MOTION pages. Completing and
# fully surfacing these is the M3 librarian milestone.
# ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PrmParam:
    """A parameter that exists only in .PRM pattern files."""
    name: str
    key: str            # PRM file key, e.g. "REVERB_TYPE"
    section: str
    control_type: ControlType = ControlType.CONTINUOUS
    min_val: int = 0
    max_val: int = 255
    default: int = 0
    description: str = ""
    value_labels: dict[int, str] = field(default_factory=dict)

    def label_for_value(self, value: int) -> str:
        """Return human-readable label for a value, or the numeric value."""
        return _label_for_value(self.value_labels, value)


_LOW_CUT = {0: "Flat", 1: "20 Hz", 2: "25 Hz", 3: "31.5 Hz", 4: "40 Hz", 5: "50 Hz",
            6: "63 Hz", 7: "80 Hz", 8: "100 Hz", 9: "125 Hz", 10: "160 Hz", 11: "200 Hz",
            12: "250 Hz", 13: "315 Hz", 14: "400 Hz", 15: "500 Hz", 16: "630 Hz", 17: "800 Hz"}
_HIGH_CUT = {0: "630 Hz", 1: "800 Hz", 2: "1 kHz", 3: "1.25 kHz", 4: "1.6 kHz", 5: "2 kHz",
             6: "2.5 kHz", 7: "3.15 kHz", 8: "4 kHz", 9: "5 kHz", 10: "6.3 kHz", 11: "8 kHz",
             12: "10 kHz", 13: "12.5 kHz", 14: "Flat"}
_DM_DEST = {0: "Off", 1: "Modulation", 2: "Frequency", 3: "Resonance", 4: "Pitch Bend",
            5: "Pan", 6: "Expression", 7: "Delay Level", 8: "Reverb Level"}

PRM_PARAMS: tuple[PrmParam, ...] = (
    # Pattern-level settings (menu items saved per pattern).
    PrmParam("Volume", "LEVEL", "PATTERN", max_val=127, default=70,
             description="Pattern volume (menu vOL)."),
    PrmParam("Pattern Scale", "SCALE", "PATTERN", ControlType.DISCRETE, max_val=5, default=1,
             value_labels={0: "1/8", 1: "1/16", 2: "1/32", 3: "8t", 4: "16t", 5: "32t"},
             description="Length of a single step (menu P.SCL)."),
    PrmParam("Arp Type", "ARP_TYPE", "PATTERN", default=0,
             description="Arpeggio type."),
    PrmParam("Arp Rate", "ARP_RATE", "PATTERN", default=2,
             description="Arpeggio rate."),
    PrmParam("Tempo Sync", "TEMPO_SYNC", "PATTERN", ControlType.SWITCH, max_val=1, default=1,
             value_labels={0: "Off", 1: "On"},
             description="Pattern tempo sync flag."),

    # Delay advanced (SHIFT + pad 13).
    PrmParam("Delay Sync", "DELAY_SW", "EFX", ControlType.SWITCH, max_val=1, default=1,
             value_labels={0: "Off", 1: "On"},
             description="Synchronize the delay time to the tempo (d.Syn)."),
    PrmParam("Delay Tempo", "DELAY_TEMPO", "EFX", ControlType.DISCRETE, max_val=15, default=14,
             value_labels={0: "1/4", 1: "8d", 2: "4t", 3: "1/8", 4: "16d", 5: "8t",
                           6: "1/16", 7: "32d", 8: "16t", 9: "1/32", 10: "64d", 11: "32t",
                           12: "1/64", 13: "128d", 14: "64t", 15: "128"},
             description="Delay time as a note value when Delay Sync is on."),
    PrmParam("Delay Feedback", "DELAY_FEEDBACK", "EFX", default=136,
             description="Delay repetition amount (Fdbk)."),
    PrmParam("Delay Low Cut", "DELAY_LOW_CUT", "EFX", ControlType.DISCRETE, max_val=17,
             default=12, value_labels=_LOW_CUT,
             description="Cut frequencies below this point (Lo.Ct)."),
    PrmParam("Delay High Cut", "DELAY_HIGH_CUT", "EFX", ControlType.DISCRETE, max_val=14,
             default=14, value_labels=_HIGH_CUT,
             description="Cut frequencies above this point (Hi.Ct)."),

    # Reverb advanced (SHIFT + pad 14).
    PrmParam("Reverb Type", "REVERB_TYPE", "EFX", ControlType.DISCRETE, max_val=6, default=5,
             value_labels={0: "Ambience", 1: "Room", 2: "Hall 1", 3: "Hall 2",
                           4: "Plate", 5: "Spring", 6: "Modulate"},
             description="Reverb algorithm (tyPE)."),
    PrmParam("Reverb Pre Delay", "REVERB_PRE_DELAY", "EFX", max_val=100, default=20,
             description="Time before the reverb starts, ms (Pr.dL)."),
    PrmParam("Reverb Low Cut", "REVERB_LOW_CUT", "EFX", ControlType.DISCRETE, max_val=17,
             default=12, value_labels=_LOW_CUT,
             description="Cut frequencies below this point (Lo.Ct)."),
    PrmParam("Reverb High Cut", "REVERB_HIGH_CUT", "EFX", ControlType.DISCRETE, max_val=14,
             default=13, value_labels=_HIGH_CUT,
             description="Cut frequencies above this point (Hi.Ct)."),
    PrmParam("Reverb Density", "REVERB_DENSITY", "EFX", max_val=10, default=10,
             description="Density of the reverb sound (dEnS)."),

    # Riser (menu rS.*).
    PrmParam("Riser Mode", "RISER_MODE", "RISER", ControlType.DISCRETE, max_val=3, default=0,
             value_labels={0: "Off", 1: "Sync", 2: "Quiver", 3: "Quiver Pan"},
             description="Riser/downer behavior of the [NOISE] knob (rS.Nd)."),
    PrmParam("Riser Resonance", "RISER_RESO", "RISER", max_val=100, default=50,
             description="Shrillness of the riser sound (rS.rS)."),
    PrmParam("Riser Shape", "RISER_SHAPE", "RISER", max_val=100, default=0,
             description="Riser envelope: 0 sawtooth - 100 square (rS.Sh)."),
    PrmParam("Riser Level", "RISER_LEVEL", "RISER", max_val=100, default=70,
             description="Riser volume (rS.Lv)."),
    PrmParam("Riser Switch", "RISER_SW", "RISER", ControlType.SWITCH, max_val=1, default=0,
             value_labels={0: "Off", 1: "On"},
             description="Riser engaged state."),
    PrmParam("Riser Control", "RISER_CTRL", "RISER", default=0,
             description="Riser control (NOISE-knob) position."),
    PrmParam("Riser Beat", "RISER_BEAT", "RISER", default=0,
             description="Riser beat position."),

    # D-MOTION routing.
    PrmParam("D-Motion X", "DM_ASSIGN_X", "D-MOTION", ControlType.DISCRETE, max_val=8,
             default=5, value_labels=_DM_DEST,
             description="Parameter controlled by tilting on the X axis."),
    PrmParam("D-Motion Y", "DM_ASSIGN_Y", "D-MOTION", ControlType.DISCRETE, max_val=8,
             default=6, value_labels=_DM_DEST,
             description="Parameter controlled by tilting on the Y axis."),
    PrmParam("D-Motion Tap", "DM_ASSIGN_TAP", "D-MOTION", ControlType.DISCRETE, max_val=8,
             default=0, value_labels=_DM_DEST,
             description="Parameter controlled by tapping."),
    PrmParam("D-Motion FF", "DM_ASSIGN_FF", "D-MOTION", ControlType.DISCRETE, max_val=8,
             default=0, value_labels=_DM_DEST,
             description="Parameter controlled by fast-forward motion."),
    PrmParam("D-Motion Sens X", "DM_SENS_X", "D-MOTION", max_val=10, default=5,
             description="X-axis tilt sensitivity."),
    PrmParam("D-Motion Sens Y", "DM_SENS_Y", "D-MOTION", max_val=10, default=5,
             description="Y-axis tilt sensitivity."),

    # OSC DRAW / CHOP internals (edited on-device via draw/chop modes).
    PrmParam("Chop Type", "OSC_CHOP_TYPE", "OSC", default=0,
             description="Chop algorithm selector (does not gate overtone audibility)."),
    PrmParam("Chop Comb Type", "OSC_CHOP_COMB_TYPE", "OSC", default=0,
             description="Comb type for OSC CHOP."),
    PrmParam("Chop Grid PWM", "OSC_CHOP_PWM", "OSC", max_val=65535, default=65535,
             description="16-step chop mask for the square wave; bit N-1 = step N. "
                         "All bits on = no chop."),
    PrmParam("Chop Grid Saw", "OSC_CHOP_SAW", "OSC", max_val=65535, default=65535,
             description="16-step chop mask for the saw wave."),
    PrmParam("Chop Grid Sub", "OSC_CHOP_SUB", "OSC", max_val=65535, default=65535,
             description="16-step chop mask for the sub oscillator."),
    PrmParam("Chop Grid Noise", "OSC_CHOP_NOISE", "OSC", max_val=65535, default=65535,
             description="16-step chop mask for the noise source."),
    PrmParam("Draw Point 1", "OSC_DRAW_P1", "OSC", max_val=65535, default=46492,
             description="OSC DRAW waveform point 1 (16-bit)."),
    PrmParam("Draw Point 2", "OSC_DRAW_P2", "OSC", max_val=65535, default=59342,
             description="OSC DRAW waveform point 2 (16-bit)."),
    PrmParam("Draw Point 3", "OSC_DRAW_P3", "OSC", max_val=65535, default=6400,
             description="OSC DRAW waveform point 3 (16-bit)."),
    PrmParam("Draw Point 4", "OSC_DRAW_P4", "OSC", max_val=65535, default=19250,
             description="OSC DRAW waveform point 4 (16-bit)."),
    PrmParam("Draw Point 5", "OSC_DRAW_P5", "OSC", max_val=65535, default=19300,
             description="OSC DRAW waveform point 5 (16-bit)."),
    PrmParam("Draw Point 6", "OSC_DRAW_P6", "OSC", max_val=65535, default=6450,
             description="OSC DRAW waveform point 6 (16-bit)."),
    PrmParam("Draw Point 7", "OSC_DRAW_P7", "OSC", max_val=65535, default=59136,
             description="OSC DRAW waveform point 7 (16-bit)."),
    PrmParam("Draw Point 8", "OSC_DRAW_P8", "OSC", max_val=65535, default=46542,
             description="OSC DRAW waveform point 8 (16-bit)."),
)

_PRM_KEY_INDEX: dict[str, PrmParam] = {p.key: p for p in PRM_PARAMS}


def prm_param_by_key(key: str) -> PrmParam | None:
    """Look up a PRM-only parameter by its .PRM file key."""
    return _PRM_KEY_INDEX.get(key.upper())


# ──────────────────────────────────────────────────────────────
# Sequencer transport parameters (app-side, not CC-controlled)
# ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SeqParam:
    """A sequencer parameter (not CC-controlled, device-menu only)."""
    name: str
    key: str          # unique identifier (e.g., "seq_tempo")
    section: str      # "Sequencer"
    control_type: ControlType = ControlType.CONTINUOUS
    min_val: int = 0
    max_val: int = 127
    default: int = 0
    description: str = ""
    value_labels: dict[int, str] = field(default_factory=dict)

    def label_for_value(self, value: int) -> str:
        """Return human-readable label for a value, or the numeric value."""
        return _label_for_value(self.value_labels, value)


SEQ_PARAMS: tuple[SeqParam, ...] = (
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


# ──────────────────────────────────────────────────────────────
# Lookup utilities
# ──────────────────────────────────────────────────────────────

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


def sections_for_access(levels: tuple[AccessLevel, ...]) -> list[tuple[str, list[S1Param]]]:
    """Ordered (section, params) groups for params at the given access levels."""
    grouped: dict[str, list[S1Param]] = {}
    order: list[str] = []
    for p in S1_PARAMS:
        if p.access in levels:
            if p.section not in grouped:
                grouped[p.section] = []
                order.append(p.section)
            grouped[p.section].append(p)
    return [(name, grouped[name]) for name in order]


def panel_params() -> list[S1Param]:
    """Faceplate params (knobs plus their SHIFT alt-functions)."""
    return [p for p in S1_PARAMS if p.access in (AccessLevel.PANEL, AccessLevel.SHIFT)]


def menu_params() -> list[S1Param]:
    """Params reached through the settings menu or MIDI-only."""
    return [p for p in S1_PARAMS if p.access in (AccessLevel.MENU, AccessLevel.EXTERNAL)]


def settings_menu_params() -> list[tuple[str, S1Param | PrmParam]]:
    """The manual's settings menu in manual order: (menu code, param)."""
    out: list[tuple[str, S1Param | PrmParam]] = []
    for code, ref in SETTINGS_MENU:
        param = param_by_cc(ref) if isinstance(ref, int) else prm_param_by_key(ref)
        if param is not None:
            out.append((code, param))
    return out


def all_sections() -> list[str]:
    """Return ordered list of section names."""
    seen: list[str] = []
    for p in S1_PARAMS:
        if p.section not in seen:
            seen.append(p.section)
    return seen
