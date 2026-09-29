"""Suite-wide guards."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _matches_stay_out_of_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every /ws/match run is kept for diagnosis (synth/web/match_ws.py save_match). A test run
    must never write into the user's own ~/.synth/matches, so it goes to a temporary folder."""
    from synth.web import match_ws

    monkeypatch.setattr(match_ws, "MATCH_DIR", tmp_path_factory.mktemp("matches"))


@pytest.fixture(autouse=True)
def _system_audio_is_fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Recording this Mac's sound (synth/native/systap.py) runs a fake helper in every test: no test
    touches real audio, builds into ~/.synth/bin, or makes macOS ask for a permission. A test that
    needs the real helper sets TAP_COMMAND back to None itself (and builds into a temporary folder)."""
    from synth.native import systap

    fake = Path(__file__).resolve().parent / "fixtures" / "fake_systap.py"
    monkeypatch.setattr(systap, "TAP_COMMAND", [sys.executable, str(fake)])
    yield
    systap.shutdown()                  # a take a test left running ends with it
