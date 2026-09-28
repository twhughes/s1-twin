"""Suite-wide guards."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _matches_stay_out_of_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every /ws/match run is kept for diagnosis (synth/web/match_ws.py save_match). A test run
    must never write into the user's own ~/.synth/matches, so it goes to a temporary folder."""
    from synth.web import match_ws

    monkeypatch.setattr(match_ws, "MATCH_DIR", tmp_path_factory.mktemp("matches"))
