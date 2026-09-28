"""What in a sound the S-1 cannot make (synth/match/reach.py): a vowel's resonances, a pitch that
wavers unevenly, breath. Synthetic signals only."""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("scipy")

from synth.match import WORKING_SR as SR  # noqa: E402
from synth.match import reach as R  # noqa: E402
from tests.voices import lowpass_saw, vowel, whistle  # noqa: E402


def near(hz: float, want: float, within: float = 0.12) -> bool:
    return abs(hz / want - 1.0) <= within


def test_a_vowel_has_two_resonances_an_octave_apart() -> None:
    got = R.reach(vowel(), SR)
    hz = [r["hz"] for r in got["resonances"]]
    assert got["vowel_like"] is True
    assert len(hz) == 2 and near(hz[0], 500) and near(hz[1], 1500), hz
    assert got["resonances"][0]["db"] == 0.0 and -25.0 < got["resonances"][1]["db"] < 0.0
    assert set(got) == {"resonances", "vowel_like", "breath_db", "wobble", "irregular", "vibrato_hz"}


@pytest.mark.parametrize("formants,want", [
    (((730, 90), (1090, 110), (2440, 160)), (730, 1090, 2440)),       # "ah": F1 and F2 close, F3 an octave up
    (((270, 60), (2290, 150)), (270, 2290)),                           # "ee": far apart
], ids=["ah", "ee"])
def test_other_vowels(formants: tuple, want: tuple) -> None:
    got = R.reach(vowel(formants=formants), SR)
    hz = [r["hz"] for r in got["resonances"]]
    assert got["vowel_like"] is True and len(hz) == len(want)
    assert all(near(a, b) for a, b in zip(hz, want)), hz


def test_a_saw_through_a_lowpass_is_not_a_vowel() -> None:
    got = R.reach(lowpass_saw(), SR)
    assert got["vowel_like"] is False and got["resonances"] == []
    assert got["breath_db"] < -25.0 and got["wobble"] < 1.0 and got["irregular"] < 1.0


def test_a_whistle_is_too_high_to_show_resonances() -> None:
    got = R.reach(whistle(), SR)
    assert got["vowel_like"] is False and got["resonances"] == []


def test_the_s1s_own_sounds_have_one_resonance_at_most() -> None:
    """A saw through the twin's resonant filter: one resonance, near the cutoff. A narrow pulse's
    spectrum has humps between gaps at every 9th harmonic: its shape, not resonances."""
    pytest.importorskip("autograd")
    from synth.match.twin import S_PARAMS, Twin

    def render(cc: dict[int, int], note: int) -> np.ndarray:
        base = {20: 0, 19: 127, 21: 0, 23: 0, 15: 0, 13: 0, 76: 64, 22: 2, 74: 127, 71: 0, 24: 0, 25: 0,
                26: 0, 73: 0, 75: 42, 30: 25, 72: 21, 28: 1, 3: 60, 17: 15, 12: 2}
        full = {**base, **cc}
        tw = Twin(seconds=1.5, gate_fraction=0.8)
        audio = np.asarray(tw.render(tw.cc_to_k(full), {sp.name: full[sp.cc] for sp in S_PARAMS}, note))
        return 0.9 * audio / np.abs(audio).max()

    resonant = R.reach(render({20: 120, 19: 0, 74: 70, 71: 100}, 48), SR)
    cutoff = 30.0 * 400.0 ** (70 / 127)                  # twin.py's cutoff curve at CC 70
    assert resonant["vowel_like"] is False and len(resonant["resonances"]) == 1
    assert near(resonant["resonances"][0]["hz"], cutoff, 0.2)
    pulse = R.reach(render({15: 110, 74: 90}, 45), SR)
    assert pulse["vowel_like"] is False and pulse["resonances"] == []


def test_a_pitch_that_wavers_unevenly() -> None:
    """A vibrato the S-1's LFO can follow leaves little; a wandering pitch leaves a lot."""
    even = R.reach(vowel(vib_cents=40.0, drift_cents=0.0), SR)
    uneven = R.reach(vowel(vib_cents=0.0, drift_cents=60.0), SR)
    assert even["wobble"] > 30.0 and even["irregular"] < 12.0
    assert even["vibrato_hz"] == pytest.approx(5.5, abs=0.5)
    assert uneven["irregular"] >= 20.0


def test_breath() -> None:
    clean = R.reach(vowel(breath_db=-40.0), SR)
    breathy = R.reach(vowel(breath_db=0.0), SR)
    assert breathy["breath_db"] > clean["breath_db"] + 10.0
