"""Roland S-1 .PRM pattern files — parse, edit, and write.

The S-1 transfers patterns/patches only through USB disk mode (hold [PLAY]
while powering on; the device mounts as a drive named "S-1" with BACKUP/ and
RESTORE/ folders of 64 files ``S1_PTN<bank>-<slot>.PRM``). The format is plain
text ``KEY = VALUE``, community-decoded (reference: denzlobin/S1Utility's
published format notes, hardware-verified; reimplemented cleanly here).

Design: a :class:`PrmFile` preserves the source file's exact lines, so an
unmodified parse→serialize round-trip is byte-identical, and edits touch only
the lines they change. Writing a new pattern starts from a real device dump
(the bundled init backup) as the template — only keys the device itself wrote
are updated, so the output is always something the firmware has vouched for.

Scaling between CC values (0-127) and native PRM ranges follows the
hardware-verified table:

- most knobs store 0-255            (cc = round(prm * 127/255))
- selectors/switch-like store 0-127  (direct; option indexes)
- flags store 0/1                    (cc 0/127)
- chord key shifts store -64..63     (cc = prm + 64)
- draw multiply / chop comb store 7-255 anchored to cc 3-127
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .sequence import MAX_NOTES_PER_STEP, MAX_STEPS, Note, Sequence

# ──────────────────────────────────────────────────────────────
# CC <-> PRM scaling
# ──────────────────────────────────────────────────────────────

_SCALE_255 = {3, 5, 13, 15, 17, 19, 20, 21, 23, 24, 25, 26, 30,
              71, 72, 73, 74, 75, 76, 89, 90, 91, 92, 103}
_DIRECT = {10, 12, 14, 16, 18, 22, 27, 28, 29, 31, 77, 78, 79, 80, 93, 105, 106, 107}
_FLAG = {65, 81, 82, 83}
_SIGNED = {85, 86, 87}
_ANCHORED = {102, 104}  # PRM 7-255 <-> CC 3-127

# PRM key -> CC for every CC-mapped parameter that appears in pattern files.
PRM_CC_KEYS: dict[str, int] = {
    "LFO_RATE": 3,
    "LFO_WAVE_FORM": 12,
    "LFO_MOD_DEPTH": 17,
    "LFO_MODE": 79,
    "LFO_KEY_TRIG": 105,
    "LFO_SYNC": 106,
    "PORTAMENTO_TIME": 5,
    "PAN": 10,
    "PORTAMENTO_MODE": 31,
    "PORTAMENTO": 65,
    "KBD_TRANSPOSE": 77,
    "ASSIGN_MODE": 80,
    "CHORD_VOICE2_SW": 81,
    "CHORD_VOICE3_SW": 82,
    "CHORD_VOICE4_SW": 83,
    "CHORD_VOICE2_KEY_SHIFT": 85,
    "CHORD_VOICE3_KEY_SHIFT": 86,
    "CHORD_VOICE4_KEY_SHIFT": 87,
    "CHORUS": 93,
    "VCO_MOD_DEPTH": 13,
    "VCO_RANGE": 14,
    "VCO_PULSE_WIDTH": 15,
    "VCO_PWM_SOURCE": 16,
    "VCO_BEND_SENS": 18,
    "VCO_PWM_LEVEL": 19,
    "VCO_SAW_LEVEL": 20,
    "VCO_SUB_LEVEL": 21,
    "VCO_SUB_TYPE": 22,
    "VCO_NOISE_LEVEL": 23,
    "FINE_TUNE": 76,
    "NOISE_MODE": 78,
    "OSC_DRAW_MULT": 102,
    "OSC_CHOP_OVERTONE": 103,
    "OSC_CHOP_COMB": 104,
    "OSC_DRAW_SW": 107,
    "VCF_ENV_DEPTH": 24,
    "VCF_MOD_DEPTH": 25,
    "VCF_KEY_FOLLOW": 26,
    "VCF_BEND_SENS": 27,
    "VCF_RESONANCE": 71,
    "VCF_CUTOFF": 74,
    "VCA_ENV_MODE": 28,
    "ENV_TRG_MODE": 29,
    "ENV_SUSTAIN": 30,
    "ENV_RELEASE": 72,
    "ENV_ATTACK": 73,
    "ENV_DECAY": 75,
    "REVERB_TIME": 89,
    "DELAY_TIME": 90,
    "REVERB_LEVEL": 91,
    "DELAY_LEVEL": 92,
}

CC_PRM_KEYS: dict[int, str] = {cc: key for key, cc in PRM_CC_KEYS.items()}


def prm_to_cc(cc: int, prm_value: int) -> int:
    """Native PRM value -> CC value (0-127)."""
    if cc in _FLAG:
        return 127 if prm_value > 0 else 0
    if cc in _SIGNED:
        return max(0, min(127, prm_value + 64))
    if cc in _ANCHORED:
        norm = (max(7, min(255, prm_value)) - 7) / 248.0
        return 3 + round(norm * 124)
    if cc in _SCALE_255:
        return round(max(0, min(255, prm_value)) * 127 / 255)
    return max(0, min(127, prm_value))


def cc_to_prm(cc: int, cc_value: int) -> int:
    """CC value (0-127) -> native PRM value."""
    if cc in _FLAG:
        return 1 if cc_value >= 64 else 0
    if cc in _SIGNED:
        return max(-64, min(63, cc_value - 64))
    if cc in _ANCHORED:
        norm = (max(3, min(127, cc_value)) - 3) / 124.0
        return 7 + round(norm * 248)
    if cc in _SCALE_255:
        return round(max(0, min(127, cc_value)) * 255 / 127)
    return max(0, min(127, cc_value))


# ──────────────────────────────────────────────────────────────
# Step encoding
# ──────────────────────────────────────────────────────────────

# STEP_NOTE lengths are in 1/96-of-a-quarter-note units: 24 = 1/16 note.
_TICKS_PER_STEP = {"1/4": 96, "1/8": 48, "1/16": 24, "1/32": 12, "1/64": 6,
                   "8t": 32, "16t": 16, "32t": 8}
# The device's SCALE selector (pattern scale): index -> app resolution.
SCALE_TO_RESOLUTION = {0: "1/8", 1: "1/16", 2: "1/32", 3: "8t", 4: "16t", 5: "32t"}
RESOLUTION_TO_SCALE = {v: k for k, v in SCALE_TO_RESOLUTION.items()}


def ticks_per_step(resolution: str) -> int:
    return _TICKS_PER_STEP.get(resolution, 24)


@dataclass
class PrmStep:
    """One STEP_NOTE line: up to 4 note slots plus substep/probability."""
    notes: list[int] = field(default_factory=lambda: [-1, -1, -1, -1])
    velocities: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    lengths: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    substep: int = 0
    prob: int = 10  # tenths: 10 = 100%

    @classmethod
    def parse(cls, raw: str) -> PrmStep:
        step = cls()
        for token in raw.split():
            if "=" not in token:
                continue
            k, _, v = token.partition("=")
            try:
                value = int(v)
            except ValueError:
                continue
            k = k.upper()
            if k.startswith("NOTE") and k[4:].isdigit() and 1 <= int(k[4:]) <= 4:
                step.notes[int(k[4:]) - 1] = value
            elif k.startswith("VELO") and k[4:].isdigit() and 1 <= int(k[4:]) <= 4:
                step.velocities[int(k[4:]) - 1] = value
            elif k.startswith("LENG") and k[4:].isdigit() and 1 <= int(k[4:]) <= 4:
                step.lengths[int(k[4:]) - 1] = value
            elif k == "SUBSTEP":
                step.substep = value
            elif k == "PROB":
                step.prob = value
        return step

    def serialize(self) -> str:
        parts = []
        for i in range(4):
            parts.append(f"NOTE{i + 1}={self.notes[i]}")
            parts.append(f"VELO{i + 1}={self.velocities[i]}")
            parts.append(f"LENG{i + 1}={self.lengths[i]}")
        parts.append(f"SUBSTEP={self.substep}")
        parts.append(f"PROB={self.prob}")
        return " ".join(parts)

    def active_notes(self) -> list[tuple[int, int, int]]:
        """[(pitch, velocity, length_ticks)] for slots that hold a note."""
        return [
            (self.notes[i], self.velocities[i], self.lengths[i])
            for i in range(4)
            if self.notes[i] >= 0
        ]


# ──────────────────────────────────────────────────────────────
# The file
# ──────────────────────────────────────────────────────────────

_STEP_NOTE_RE = re.compile(r"^STEP_NOTE\s+(\d+)$", re.IGNORECASE)
MAX_FILE_BYTES = 1_000_000


class PrmParseError(ValueError):
    """Raised for files that are not S-1 .PRM dumps."""


@dataclass
class _Line:
    raw: str            # the exact original line (no newline)
    key: str | None     # uppercased key, None for comments/blank/unparsable
    value: str | None


class PrmFile:
    """A parsed .PRM file that can reproduce its source byte-for-byte."""

    def __init__(self, lines: list[_Line], newline_at_eof: bool):
        self._lines = lines
        self._eof_newline = newline_at_eof
        # key -> line index (last occurrence wins, like the device's parser)
        self._index: dict[str, int] = {}
        for i, line in enumerate(lines):
            if line.key is not None:
                self._index[line.key] = i

    # ── parsing ──────────────────────────────────────────────
    @classmethod
    def parse(cls, text: str) -> PrmFile:
        if len(text) > MAX_FILE_BYTES:
            raise PrmParseError("file too large to be a .PRM dump")
        lines: list[_Line] = []
        recognized = 0
        raw_lines = text.split("\n")
        newline_at_eof = text.endswith("\n")
        if newline_at_eof:
            raw_lines = raw_lines[:-1]
        for raw in raw_lines:
            stripped = raw.strip()
            if not stripped or stripped.startswith((";", "#")) or "=" not in stripped:
                lines.append(_Line(raw, None, None))
                continue
            key, _, value = stripped.partition("=")
            key = key.strip().upper()
            value = value.strip()
            if not key:
                lines.append(_Line(raw, None, None))
                continue
            lines.append(_Line(raw, key, value))
            recognized += 1
        if recognized == 0:
            raise PrmParseError("no KEY = VALUE entries found")
        return cls(lines, newline_at_eof)

    @classmethod
    def load(cls, path: Path | str) -> PrmFile:
        return cls.parse(Path(path).read_text(encoding="ascii", errors="replace"))

    def serialize(self) -> str:
        text = "\n".join(line.raw for line in self._lines)
        return text + ("\n" if self._eof_newline else "")

    def save(self, path: Path | str) -> Path:
        path = Path(path)
        path.write_text(self.serialize(), encoding="ascii")
        return path

    # ── raw key access ───────────────────────────────────────
    def get(self, key: str) -> str | None:
        i = self._index.get(key.upper())
        return self._lines[i].value if i is not None else None

    def get_int(self, key: str, default: int = 0) -> int:
        v = self.get(key)
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    def has(self, key: str) -> bool:
        return key.upper() in self._index

    def set(self, key: str, value: int | str) -> bool:
        """Update an existing key in place, preserving the line's layout.

        Returns False if the key is not in the file — the writer only touches
        keys the device itself wrote, so restores never contain novel keys.
        """
        i = self._index.get(key.upper())
        if i is None:
            return False
        line = self._lines[i]
        prefix, _, _ = line.raw.partition("=")
        new_raw = f"{prefix}= {value}"
        self._lines[i] = _Line(new_raw, line.key, str(value))
        return True

    def keys(self) -> list[str]:
        return [line.key for line in self._lines if line.key is not None]

    # ── steps ────────────────────────────────────────────────
    def step(self, number: int) -> PrmStep | None:
        """STEP_NOTE for a 1-indexed step, or None if the line is absent."""
        raw = self.get(f"STEP_NOTE {number}")
        return PrmStep.parse(raw) if raw is not None else None

    def set_step(self, number: int, step: PrmStep) -> bool:
        return self.set(f"STEP_NOTE {number}", step.serialize())

    # ── patch (CC) view ──────────────────────────────────────
    def to_cc_values(self) -> dict[int, int]:
        """All CC-mapped parameters present in the file, as CC values."""
        out: dict[int, int] = {}
        for key, cc in PRM_CC_KEYS.items():
            if self.has(key):
                out[cc] = prm_to_cc(cc, self.get_int(key))
        return out

    def apply_cc_values(self, values: dict[int, int]) -> list[int]:
        """Write CC values into their PRM keys. Returns CCs that had no key
        in this file (not part of the device dump — skipped)."""
        skipped: list[int] = []
        for cc, value in values.items():
            key = CC_PRM_KEYS.get(cc)
            if key is None or not self.set(key, cc_to_prm(cc, value)):
                skipped.append(cc)
        return skipped

    # ── sequence view ────────────────────────────────────────
    def to_sequence(self) -> Sequence:
        """Decode the pattern's sequence data."""
        steps = max(1, min(MAX_STEPS, self.get_int("LENG", 16)))
        bpm = self.get_int("TEMPO", 12000) / 100.0
        resolution = SCALE_TO_RESOLUTION.get(self.get_int("SCALE", 1), "1/16")
        tps = ticks_per_step(resolution)
        notes: list[Note] = []
        for n in range(1, steps + 1):
            prm_step = self.step(n)
            if prm_step is None:
                continue
            for pitch, velocity, length in prm_step.active_notes():
                notes.append(Note(
                    step=n - 1,
                    pitch=max(0, min(127, pitch)),
                    velocity=max(1, min(127, velocity or 100)),
                    duration=max(1, round(length / tps)) if length > 0 else 1,
                ))
        return Sequence(notes=notes, steps=steps, bpm=bpm, step_resolution=resolution)

    def apply_sequence(self, seq: Sequence) -> list[int]:
        """Write a Sequence into the pattern. Returns steps whose notes were
        truncated to the device's 4-per-step ceiling."""
        steps = max(1, min(MAX_STEPS, seq.steps))
        self.set("LENG", steps)
        self.set("TEMPO", round(seq.bpm * 100))
        scale = RESOLUTION_TO_SCALE.get(seq.step_resolution)
        if scale is not None:
            self.set("SCALE", scale)
        tps = ticks_per_step(seq.step_resolution)

        truncated: list[int] = []
        for n in range(1, MAX_STEPS + 1):
            prm_step = PrmStep()
            at_step = [note for note in seq.notes if note.step == n - 1] if n <= steps else []
            if len(at_step) > MAX_NOTES_PER_STEP:
                truncated.append(n - 1)
            for slot, note in enumerate(at_step[:MAX_NOTES_PER_STEP]):
                prm_step.notes[slot] = max(0, min(127, note.pitch))
                prm_step.velocities[slot] = max(1, min(127, note.velocity))
                prm_step.lengths[slot] = max(1, note.duration) * tps
            self.set_step(n, prm_step)
        return truncated


# ──────────────────────────────────────────────────────────────
# The exporter
# ──────────────────────────────────────────────────────────────

# A real S-1 device dump (the factory init pattern) used as the writer's
# template: every key in it is one the firmware itself wrote.
TEMPLATE_PATH = Path(__file__).parent / "data" / "init_pattern.prm"


def load_template() -> PrmFile:
    return PrmFile.load(TEMPLATE_PATH)


def pattern_filename(bank: int, slot: int) -> str:
    """Device naming: S1_PTN<bank>-<slot>.PRM, slot zero-padded (e.g. 2-05)."""
    if not 1 <= bank <= 4:
        raise ValueError("bank must be 1-4")
    if not 1 <= slot <= 16:
        raise ValueError("slot must be 1-16")
    return f"S1_PTN{bank}-{slot:02d}.PRM"


def build_pattern(
    cc_values: dict[int, int],
    seq: Sequence | None = None,
    template: PrmFile | None = None,
) -> PrmFile:
    """A ready-to-restore pattern: patch + sequence over a real device dump."""
    prm = template if template is not None else load_template()
    prm.apply_cc_values(cc_values)
    if seq is not None:
        prm.apply_sequence(seq)
    return prm


# ──────────────────────────────────────────────────────────────
# Disk-mode volume detection (macOS)
# ──────────────────────────────────────────────────────────────

VOLUMES_DIR = Path("/Volumes")

# Where users keep device dumps app-side (the test suite also round-trips
# every file found here against the parser).
BACKUPS_DIR = Path.home() / ".s1tui" / "backups"

_PATTERN_NAME_RE = re.compile(r"^S1_PTN([1-4])-(\d{1,2})\.PRM$", re.IGNORECASE)


def parse_pattern_filename(name: str) -> tuple[int, int] | None:
    """(bank, slot) from a device filename like S1_PTN2-05.PRM, else None."""
    m = _PATTERN_NAME_RE.match(name)
    if m is None:
        return None
    bank, slot = int(m.group(1)), int(m.group(2))
    return (bank, slot) if 1 <= slot <= 16 else None


def list_prm_files(root: Path) -> list[Path]:
    """Every .PRM file under a directory (recursive), sorted by path."""
    try:
        return sorted(
            p for p in root.rglob("*")
            if p.is_file() and p.suffix.upper() == ".PRM"
        )
    except OSError:
        return []


def find_s1_volume(volumes_dir: Path | None = None) -> Path | None:
    """The mounted S-1 disk-mode volume, if the ritual has been performed.

    Identified by a volume whose name starts with "S-1" containing the
    device's RESTORE/ folder.
    """
    root = volumes_dir if volumes_dir is not None else VOLUMES_DIR
    try:
        candidates = sorted(root.iterdir())
    except OSError:
        return None
    for vol in candidates:
        if vol.name.upper().startswith("S-1") and (vol / "RESTORE").is_dir():
            return vol
    return None


def write_to_device(prm: PrmFile, bank: int, slot: int,
                    volumes_dir: Path | None = None) -> Path | None:
    """Copy the pattern into the mounted S-1's RESTORE folder.

    Returns the written path, or None when the volume isn't mounted (the UI
    then falls back to a download + manual ritual walkthrough).
    """
    vol = find_s1_volume(volumes_dir)
    if vol is None:
        return None
    return prm.save(vol / "RESTORE" / pattern_filename(bank, slot))
