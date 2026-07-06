"""Sequence bank — app-side pattern storage (JSON in ~/.synth/sequences/).

Same shape as the patch bank: one JSON file per pattern, with optional
metadata. Device limits are enforced on load (64 steps; notes past the end
are dropped and counted) so anything in the bank can be sent to the S-1.
"""

from __future__ import annotations

import json
from pathlib import Path

from .patches import resolve_in_dir, sanitize_name
from .paths import data_dir
from .sequence import MAX_STEPS, Note, Sequence

SEQUENCE_DIR = data_dir() / "sequences"


def sequence_to_dict(seq: Sequence) -> dict:
    """JSON-ready dict of a Sequence."""
    return {
        "steps": seq.steps,
        "bpm": seq.bpm,
        "step_resolution": seq.step_resolution,
        "notes": [
            {"step": n.step, "pitch": n.pitch, "velocity": n.velocity, "duration": n.duration}
            for n in seq.notes
        ],
    }


def sequence_from_dict(data: dict) -> Sequence:
    """Build a Sequence from a dict, enforcing device limits.

    Steps clamp to 1..MAX_STEPS; notes starting past the end are dropped
    (counted in ``dropped_notes``); pitch/velocity clamp to MIDI range.
    """
    steps = max(1, min(MAX_STEPS, int(data.get("steps", 16))))
    notes: list[Note] = []
    dropped = 0
    for nd in data.get("notes", []):
        step = int(nd["step"])
        if not 0 <= step < steps:
            dropped += 1
            continue
        notes.append(Note(
            step=step,
            pitch=max(0, min(127, int(nd["pitch"]))),
            velocity=max(1, min(127, int(nd.get("velocity", 100)))),
            duration=max(1, int(nd.get("duration", 1))),
        ))
    return Sequence(
        notes=notes,
        steps=steps,
        bpm=max(1.0, float(data.get("bpm", 120.0))),
        step_resolution=str(data.get("step_resolution", "1/16")),
        dropped_notes=dropped,
    )


def save_sequence(name: str, seq: Sequence, metadata: dict | None = None) -> Path:
    """Save a sequence to the bank."""
    name = sanitize_name(name)
    SEQUENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = resolve_in_dir(SEQUENCE_DIR, f"{name}.json")
    payload: dict = {"name": name, "sequence": sequence_to_dict(seq)}
    if metadata:
        payload["metadata"] = metadata
    path.write_text(json.dumps(payload))
    return path


def load_sequence(path: Path) -> Sequence:
    """Load a sequence from a bank file."""
    data = json.loads(path.read_text())
    return sequence_from_dict(data["sequence"])


def load_sequence_metadata(path: Path) -> dict:
    """Metadata block of a bank file (empty dict if none)."""
    data = json.loads(path.read_text())
    return data.get("metadata", {})


def sequence_path(name: str) -> Path:
    """Resolve a sequence name to its file path."""
    name = sanitize_name(name)
    return resolve_in_dir(SEQUENCE_DIR, f"{name}.json")


def list_sequences() -> list[Path]:
    """List all saved sequence files."""
    if not SEQUENCE_DIR.exists():
        return []
    return sorted(SEQUENCE_DIR.glob("*.json"))


def delete_sequence(name: str) -> bool:
    """Delete a sequence by name. Returns True if a file was removed."""
    path = sequence_path(name)
    if path.exists():
        path.unlink()
        return True
    return False
