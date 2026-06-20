"""Synthwave-neon theme and per-section accent palette for s1tui.

Centralises every colour the UI uses so the whole look can be re-skinned
from one place. Section accents drive the neon meters, module-card borders
and headers throughout the app.
"""

from __future__ import annotations

from textual.theme import Theme

# ── Base palette (deep synthwave dusk) ──
BG = "#0c0a18"        # near-black indigo
SURFACE = "#15122b"   # raised surface
PANEL = "#1c1838"     # cards / panels
FG = "#e8e3ff"        # soft lavender-white text
MUTED = "#716c9c"     # dim labels / inactive
FAINT = "#383258"     # grid lines, empty meter dots

MAGENTA = "#ff2e97"   # hot pink — primary
CYAN = "#2de2e6"      # electric cyan — secondary
VIOLET = "#b388ff"    # neon violet — accent
LIME = "#36f9b3"      # mint green — success
GOLD = "#ffb86c"      # amber — warning
RED = "#ff5370"       # error

SYNTHWAVE = Theme(
    name="synthwave",
    primary=MAGENTA,
    secondary=CYAN,
    accent=VIOLET,
    foreground=FG,
    background=BG,
    surface=SURFACE,
    panel=PANEL,
    success=LIME,
    warning=GOLD,
    error=RED,
    boost="#2a2350",
    dark=True,
    variables={
        "block-cursor-foreground": BG,
        "block-cursor-background": MAGENTA,
        "block-cursor-text-style": "bold",
        "footer-key-foreground": CYAN,
        "footer-description-foreground": MUTED,
        "footer-background": SURFACE,
        "border": MAGENTA,
        "scrollbar": "#2a2350",
        "scrollbar-hover": VIOLET,
        "scrollbar-active": MAGENTA,
    },
)


# ── Per-section neon accents ──
# Roughly follows signal flow: modulation = violet/cyan, filter = gold,
# amp/env = orange, effects = magenta.
SECTION_ACCENT: dict[str, str] = {
    "LFO": VIOLET,
    "Oscillator": CYAN,
    "Draw/Chop": LIME,
    "Filter": GOLD,
    "Envelope": "#ff8c42",
    "Voice": "#4d9eff",
    "Chord Mode": "#9d7bff",
    "Effects": MAGENTA,
    "Controls": LIME,
    "Sequencer": CYAN,
    "Arpeggiator": VIOLET,
}

DEFAULT_ACCENT = MAGENTA


def section_accent(section: str) -> str:
    """Neon accent colour for a section name."""
    return SECTION_ACCENT.get(section, DEFAULT_ACCENT)


# ── Colour helpers ──

def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, round(c))) for c in rgb))


def blend(a: str, b: str, t: float) -> str:
    """Linearly blend hex colour ``a``→``b`` by ``t`` in [0, 1]."""
    ar, ag, ab = _hex_to_rgb(a)
    br, bg, bb = _hex_to_rgb(b)
    return _rgb_to_hex((ar + (br - ar) * t, ag + (bg - ag) * t, ab + (bb - ab) * t))


def gradient(text: str, stops: list[str], *, bold: bool = True):
    """A Rich ``Text`` with colour swept across ``stops`` (multi-stop)."""
    from rich.text import Text

    out = Text()
    n = max(1, len(text) - 1)
    segs = len(stops) - 1
    pre = "bold " if bold else ""
    for i, ch in enumerate(text):
        if segs <= 0:
            color = stops[0]
        else:
            pos = (i / n) * segs
            k = min(int(pos), segs - 1)
            color = blend(stops[k], stops[k + 1], pos - k)
        out.append(ch, style=f"{pre}{color}")
    return out
