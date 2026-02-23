"""Roland S-1 MIDI CC parameter definitions.

Backward-compatibility wrapper around schema.py.
"""

from dataclasses import dataclass

from .schema import S1_PARAMS, param_by_cc


@dataclass
class CCParam:
    """A single MIDI CC parameter."""
    name: str
    cc: int
    min_val: int = 0
    max_val: int = 127
    default: int = 0
    description: str = ""


def _build_sections() -> dict[str, list[CCParam]]:
    """Build legacy SECTIONS dict from the new schema."""
    sections: dict[str, list[CCParam]] = {}
    for p in S1_PARAMS:
        legacy = CCParam(
            name=p.name, cc=p.cc, min_val=p.min_val, max_val=p.max_val,
            default=p.default, description=p.description,
        )
        # Map section names to legacy format
        section_name = p.section
        if section_name == "Draw/Chop":
            section_name = "Osc Draw/Chop"
        sections.setdefault(section_name, []).append(legacy)
    return sections


SECTIONS: dict[str, list[CCParam]] = _build_sections()


def all_params() -> list[CCParam]:
    """Return a flat list of all parameters."""
    return [p for section in SECTIONS.values() for p in section]


def find_param_by_cc(cc: int) -> CCParam | None:
    """Look up a parameter by CC number."""
    s = param_by_cc(cc)
    if s is None:
        return None
    return CCParam(
        name=s.name, cc=s.cc, min_val=s.min_val, max_val=s.max_val,
        default=s.default, description=s.description,
    )
