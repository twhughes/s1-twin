"""Where the app keeps user data: ~/.synth (patches, sequences, backups,
recordings). The project was born as "s1tui"; a legacy ~/.s1tui directory is
migrated (renamed) the first time this module is imported, so nothing the
user saved is left behind.
"""

from __future__ import annotations

from pathlib import Path

DATA_DIR = Path.home() / ".synth"
LEGACY_DATA_DIR = Path.home() / ".s1tui"


def data_dir() -> Path:
    """~/.synth, adopting a legacy ~/.s1tui wholesale if it's still there."""
    if not DATA_DIR.exists() and LEGACY_DATA_DIR.is_dir():
        try:
            LEGACY_DATA_DIR.rename(DATA_DIR)
        except OSError:
            pass
    return DATA_DIR
