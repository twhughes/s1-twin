"""The reproduction suite's fast cases (the full suite is tools/match_suite.py, compared with
docs/match-baseline.json). These run on every test run: the "recorded" take is what it claims to be,
and the matcher still reproduces the S-1's own default patch (the 16 kHz search-twin bug of
2026-09-28: Quick had fallen to 39% there, trading the Square for Sub and noise)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

pytest.importorskip("autograd")
pytest.importorskip("scipy")

import match_suite as suite  # noqa: E402


def test_every_case_names_real_settings() -> None:
    for name, spec in suite.CASES.items():
        assert set(spec["cc"]) <= set(suite.BASE), name
        assert 0.05 < spec["held"] < 3.0 and spec["notes"], name
    assert set(suite.RECORDED) <= set(suite.CASES)


def test_a_recorded_take_is_quiet_late_and_noisy() -> None:
    """2 s of room noise before the note, the peak near -18 dBFS, a noise floor near -60 dBFS, 48 kHz."""
    audio, sr, _truth, _notes = suite.render_case("pluck")
    take = suite.record(audio, sr)
    rate = suite.REC_SR
    frame = int(0.01 * rate)
    rms = np.sqrt(np.convolve(take * take, np.ones(frame) / frame, mode="valid"))
    onset = int(np.argmax(rms > 10 ** (-40 / 20))) / rate
    assert 1.95 < onset < 2.1
    assert -19.5 < 20 * np.log10(np.abs(take).max()) < -16.5
    floor = 20 * np.log10(np.sqrt(np.mean(take[: int(1.5 * rate)] ** 2)))
    assert -63 < floor < -57
    assert take.size > (2.0 + 1.5) * rate


def test_quick_still_reproduces_the_default_square() -> None:
    """The regression case for the search-twin bug: a full square, the filter open, note C3."""
    run = suite.run_case(("square", "quick", 0))
    suite.score_runs([run])
    assert run["closeness"] >= 70.0, run
    assert run["found"]["19"] >= 100 and run["found"]["21"] <= 20 and run["found"]["23"] <= 20, run["found"]
    assert run["switches"], run["missed"]


# Round 9 (W-voice): the cold start's notes for every case, clean and recorded (twin_session.detect on
# the crop plan() makes). The pitch tracker (synth/match/pitch.py) left 15 of the 18 as they were;
# three were wrong before and are now the note played: "vibrato" (a square LFO's trill, 53+54+55+56)
# and "bass@rec" (35+36+37+38) were clusters of neighbours, "wobble" (40+45) read its sub-oscillator's
# third harmonic as a note. "sub" and "bass" still do (a sub-oscillator under 50 Hz: the sound repeats
# more slowly than any pitch the tracker follows, so the chord detector decides, as before).
COLD_START = {
    "square": [48], "saw": [60], "gate": [48], "sub": [40, 47], "vibrato": [55], "wobble": [45],
    "pluck": [48], "bass": [36, 43], "high": [72], "pad": [55], "short": [50], "chord": [48, 52, 55],
    "square@rec": [48], "gate@rec": [48], "pluck@rec": [48], "bass@rec": [36], "pad@rec": [55],
    "short@rec": [50],
}


@pytest.mark.parametrize("case", sorted(COLD_START))
def test_cold_start_notes(case: str) -> None:
    from synth.match import twin_session as ts
    from synth.match.target_prep import prepare_upload

    wav, _truth, _notes = suite.target(case)
    assert ts.detect(prepare_upload(wav)[1].samples)[0] == COLD_START[case]


def test_every_case_has_its_cold_start() -> None:
    assert set(COLD_START) == set(suite.all_case_names())
