"""Patch save/load utilities."""

from __future__ import annotations

import json
from pathlib import Path

PATCH_DIR = Path.home() / ".s1tui" / "patches"


def save_patch(name: str, values: dict[int, int]) -> Path:
    """Save CC values to a JSON patch file."""
    PATCH_DIR.mkdir(parents=True, exist_ok=True)
    path = PATCH_DIR / f"{name}.json"
    path.write_text(json.dumps({"name": name, "cc_values": {str(k): v for k, v in values.items()}}))
    return path


def load_patch(path: Path) -> dict[int, int]:
    """Load CC values from a JSON patch file."""
    data = json.loads(path.read_text())
    return {int(k): v for k, v in data["cc_values"].items()}


def list_patches() -> list[Path]:
    """List all saved patch files."""
    if not PATCH_DIR.exists():
        return []
    return sorted(PATCH_DIR.glob("*.json"))
