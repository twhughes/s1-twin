"""Patch save/load utilities — the s1tui sound bank (JSON in ~/.s1tui/patches/)."""

from __future__ import annotations

import json
from pathlib import Path

PATCH_DIR = Path.home() / ".s1tui" / "patches"


def sanitize_name(name: str) -> str:
    """Validate a user-supplied file name for the bank.

    Names come from the TUI, CLI, and web API — they must stay inside the
    target directory. Raises ValueError on path separators, '..', hidden
    names, or an empty result.
    """
    name = name.strip()
    if not name or name in (".", ".."):
        raise ValueError("empty patch name")
    if "/" in name or "\\" in name or "\x00" in name:
        raise ValueError("patch name must not contain path separators")
    if name.startswith("."):
        raise ValueError("patch name must not start with '.'")
    return name


def resolve_in_dir(directory: Path, filename: str) -> Path:
    """Join and verify the result stays inside *directory* (belt and braces)."""
    path = (directory / filename).resolve()
    if path.parent != directory.resolve():
        raise ValueError("patch name escapes the bank directory")
    return path


def save_patch(name: str, values: dict[int, int], metadata: dict | None = None) -> Path:
    """Save CC values to a JSON patch file, with optional metadata.

    Metadata (e.g. match closeness, source audio name) is stored alongside the
    values; older patches without it still load fine.
    """
    name = sanitize_name(name)
    PATCH_DIR.mkdir(parents=True, exist_ok=True)
    path = resolve_in_dir(PATCH_DIR, f"{name}.json")
    payload: dict = {"name": name, "cc_values": {str(k): v for k, v in values.items()}}
    if metadata:
        payload["metadata"] = metadata
    path.write_text(json.dumps(payload))
    return path


def load_patch(path: Path) -> dict[int, int]:
    """Load CC values from a JSON patch file."""
    data = json.loads(path.read_text())
    return {int(k): v for k, v in data["cc_values"].items()}


def load_patch_metadata(path: Path) -> dict:
    """Load the metadata block from a patch file (empty dict if none)."""
    data = json.loads(path.read_text())
    return data.get("metadata", {})


def patch_path(name: str) -> Path:
    """Resolve a patch name to its file path."""
    name = sanitize_name(name)
    return resolve_in_dir(PATCH_DIR, f"{name}.json")


def list_patches() -> list[Path]:
    """List all saved patch files."""
    if not PATCH_DIR.exists():
        return []
    return sorted(PATCH_DIR.glob("*.json"))


def delete_patch(name: str) -> bool:
    """Delete a patch by name. Returns True if a file was removed."""
    path = patch_path(name)
    if path.exists():
        path.unlink()
        return True
    return False
