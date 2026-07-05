"""The search space: which S-1 parameters the optimizer is allowed to move, and
how to encode them as a normalized ``[0, 1]`` vector.

Pure logic with no hardware or audio deps, so it is fully unit-testable. The
optimizer thinks in continuous vectors; :class:`ParamSpace` translates to and from
the integer MIDI CC values the synth actually wants, snapping discrete/switch
parameters to legal settings.
"""

from __future__ import annotations

import numpy as np

from ..schema import S1_PARAMS, ControlType, S1Param

# Sections whose parameters shape *timbre* (vs. pitch/voicing/routing).
TIMBRE_SECTIONS: tuple[str, ...] = ("LFO", "OSC", "FILTER", "AMP", "ENV")
EFFECTS_SECTION = "EFX"

# CCs to hold fixed even within timbre sections: they shift pitch (which we hold
# constant via a fixed probe note) rather than tone.
_EXCLUDED_CCS: frozenset[int] = frozenset(
    {
        76,   # Osc Fine Tune
        18,   # Osc Pitch Bend Sens
        27,   # Filter Bend Sensitivity
        106,  # LFO Sync Mode (firmware-dependent, no defined values)
    }
)


def default_params(include_effects: bool = True) -> list[S1Param]:
    """The S1Params the optimizer searches over."""
    sections = set(TIMBRE_SECTIONS)
    if include_effects:
        sections.add(EFFECTS_SECTION)
    return [p for p in S1_PARAMS if p.section in sections and p.cc not in _EXCLUDED_CCS]


class ParamSpace:
    """Bidirectional map between a normalized vector and S-1 CC values."""

    def __init__(self, params: list[S1Param] | None = None, include_effects: bool = True):
        self.params: list[S1Param] = params if params is not None else default_params(include_effects)
        # Pre-compute sorted choice keys for discrete params.
        self._choices: dict[int, list[int]] = {
            p.cc: sorted(p.value_labels) for p in self.params if p.control_type == ControlType.DISCRETE
        }

    @property
    def dim(self) -> int:
        return len(self.params)

    @property
    def ccs(self) -> list[int]:
        return [p.cc for p in self.params]

    def decode(self, vector: np.ndarray) -> dict[int, int]:
        """Vector in [0, 1] -> {cc: int value} snapped to legal settings."""
        v = np.clip(np.asarray(vector, dtype=float), 0.0, 1.0)
        out: dict[int, int] = {}
        for value, p in zip(v, self.params):
            if p.control_type == ControlType.SWITCH:
                out[p.cc] = 127 if value >= 0.5 else 0
            elif p.control_type == ControlType.DISCRETE:
                keys = self._choices[p.cc]
                idx = int(round(value * (len(keys) - 1)))
                out[p.cc] = keys[idx]
            else:
                out[p.cc] = p.min_val + int(round(value * (p.max_val - p.min_val)))
        return out

    def encode(self, params: dict[int, int]) -> np.ndarray:
        """{cc: value} -> vector in [0, 1] (inverse of :meth:`decode`)."""
        v = np.empty(self.dim, dtype=float)
        for i, p in enumerate(self.params):
            value = params.get(p.cc, p.default)
            if p.control_type == ControlType.SWITCH:
                v[i] = 1.0 if value >= 64 else 0.0
            elif p.control_type == ControlType.DISCRETE:
                keys = self._choices[p.cc]
                nearest = min(range(len(keys)), key=lambda k: abs(keys[k] - value))
                v[i] = nearest / max(len(keys) - 1, 1)
            else:
                span = max(p.max_val - p.min_val, 1)
                v[i] = min(max(value - p.min_val, 0), span) / span
        return v

    def default_vector(self) -> np.ndarray:
        """Encoded vector of the schema defaults — a sensible optimizer start."""
        return self.encode({p.cc: p.default for p in self.params})
