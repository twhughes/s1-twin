"""The auto-crop (``synth/match/target_prep.py``): whatever surrounds a sound in a take, the
matcher gets the sound itself, and a guess at how long its key was held.

Every signal here is synthetic with known answers: a saw note with an ADSR envelope (onset,
key-up and release known to the sample), room noise, clicks, a second note, a clipped take.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
import soundfile as sf

from synth.match import WORKING_SR as SR
from synth.match import target_prep as tp
from synth.match.twin_session import UploadError

ONSET, HELD = 2.0, 0.8           # the note starts 2 s into the take; its key goes up 0.8 s later
KEY_UP_TOL = 0.06                # the key-up guess is good to about ±60 ms


def _saw(f0: float, n: int) -> np.ndarray:
    t = np.arange(n) / SR
    return sum(np.sin(2 * np.pi * h * f0 * t) / h for h in range(1, 30) if h * f0 < SR / 2) / 1.8


def _adsr(n: int, a: float = 0.005, d: float = 0.2, s: float = 0.5, r: float = 0.1,
          gate: float = HELD) -> np.ndarray:
    """The twin's envelope shape: a linear attack, an exponential decay to ``s``, a release at ``gate``."""
    t = np.arange(n) / SR
    held = np.where(t < a, t / a, s + (1 - s) * np.exp(-(t - a) / d))
    at_gate = s + (1 - s) * np.exp(-(gate - a) / d)
    return np.where(t < gate, held, at_gate * np.exp(-(t - gate) / r))


def _noise(n: int, rms: float, seed: int = 0, slope_db_oct: float = 0.0) -> np.ndarray:
    """White (0) or colored noise (-3: pink, a room) at ``rms``."""
    rng = np.random.default_rng(seed)
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / SR)
    f[0] = f[1]
    y = np.fft.irfft(spec * (f / 1000.0) ** (slope_db_oct / 6.02), n)
    return rms * y / np.sqrt(np.mean(y * y))


def _take(lead: float = ONSET, note_s: float = 1.6, after: float = 2.0, f0: float = 130.81,
          amp: float = 0.3, noise_rms: float = 0.003, seed: int = 1, **env) -> np.ndarray:
    """``lead`` s of room, a note (C3 by default), ``after`` s of room."""
    n = int(note_s * SR)
    note = amp * _saw(f0, n) * _adsr(n, **env)
    x = np.concatenate([np.zeros(int(lead * SR)), note, np.zeros(int(after * SR))])
    return x + _noise(len(x), noise_rms, seed) if noise_rms else x


def _click(x: np.ndarray, at: float, amp: float = 0.9, ms: float = 3.0) -> np.ndarray:
    y = x.copy()
    i, k = int(at * SR), int(ms / 1000 * SR)
    y[i:i + k] += amp * np.where(np.arange(k) % 2, -1.0, 1.0)
    return y


def _key_up(p: tp.Prepared) -> float:
    assert p.gate_s is not None, "the key-up was not seen"
    return p.onset + p.gate_s


# ── the main case: 2 s of room, a note, 2 s of room ─────────────────────────────
def test_noise_then_a_note_then_noise() -> None:
    x = _take()
    p = tp.prepare_target(x, SR)
    assert abs(p.onset - ONSET) < 0.005, "the onset is the note's first clear rise"
    assert p.t0 == pytest.approx(p.onset - tp.PRE_S, abs=1e-3), "5 ms are kept before it"
    assert ONSET + HELD + 0.1 < p.t1 < ONSET + 1.6, "the end keeps the release, not the room after it"
    assert abs(_key_up(p) - (ONSET + HELD)) < KEY_UP_TOL
    assert p.warnings == []
    assert (p.t1 - p.t0) * SR - 2 <= len(p.samples) <= (p.onset + tp.MATCH_TAIL_S - p.t0) * SR + 2, \
        "the heard sound and its quiet tail (MATCH_TAIL_S), never the next sound"
    assert abs(p.samples[0]) < 1e-3 and abs(p.samples[-1]) < 1e-3, "faded in over the lead, out at the end"
    assert p.noise_db == pytest.approx(20 * np.log10(0.003), abs=1.5)
    assert p.duration == pytest.approx(len(x) / SR)
    np.testing.assert_allclose(p.samples[int(0.01 * SR):int(0.5 * SR)],
                               x[int(p.t0 * SR) + int(0.01 * SR):int(p.t0 * SR) + int(0.5 * SR)],
                               err_msg="between the fades the crop is the take itself")


@pytest.mark.parametrize("at", [1.7, 1.9])
def test_a_click_before_the_note_is_not_the_onset(at: float) -> None:
    p = tp.prepare_target(_click(_take(), at), SR)
    assert abs(p.onset - ONSET) < 0.005, f"a click at {at} s is not the sound"
    assert p.warnings == []


def test_the_click_of_stop_after_the_note_is_left_out() -> None:
    x = _click(_take(), ONSET + 1.6 + 1.2, amp=0.8, ms=4.0)
    p = tp.prepare_target(x, SR)
    assert p.t1 < ONSET + 1.6 and abs(p.onset - ONSET) < 0.005


@pytest.mark.parametrize("zeros", [0.08, 0.6])
def test_warm_up_zeros_are_not_the_room(zeros: float) -> None:
    """A recorder can deliver exact zeros before the room: the floor is the room's, so its noise
    before the note is still cut."""
    x = _take()
    x[:int(zeros * SR)] = 0.0
    p = tp.prepare_target(x, SR)
    assert abs(p.onset - ONSET) < 0.005 and p.noise_db > -60 and p.t1 < ONSET + 1.6


# ── the key-up ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("env", [
    {"r": 0.01}, {"r": 0.1}, {"r": 0.5}, {"s": 0.1}, {"s": 1.0, "d": 1.0, "r": 0.02},   # the last: a gate
    {"gate": 1.5}, {"gate": 0.3, "d": 0.05},
], ids=["fast-release", "release", "slow-release", "low-sustain", "gate", "long-hold", "short-hold"])
def test_a_held_note_gives_its_key_up(env: dict) -> None:
    gate = env.get("gate", HELD)
    p = tp.prepare_target(_take(note_s=gate + 6 * env.get("r", 0.1) + 0.2, **env), SR)
    assert abs(_key_up(p) - (ONSET + gate)) < KEY_UP_TOL, p.gate_s


def test_a_pluck_has_no_key_up() -> None:
    """It dies away while the key is still held: no plateau, so no key-up to see."""
    p = tp.prepare_target(_take(s=0.0, d=0.25, gate=1.5, note_s=2.0), SR)
    assert p.gate_s is None and abs(p.onset - ONSET) < 0.005


def test_a_take_that_ends_while_the_key_is_held_has_no_key_up() -> None:
    p = tp.prepare_target(_take(gate=3.0, note_s=2.0, after=0.0, noise_rms=0.0), SR)
    assert p.gate_s is None and p.t1 == pytest.approx(ONSET + 2.0, abs=0.02)


def test_the_twins_own_note_gives_its_key_up() -> None:
    """The twin's render (key up at 1.2 s, as "Match the synth's current sound" plays it)."""
    pytest.importorskip("autograd")
    from synth.match.twin import Twin

    twin = Twin(seconds=2.2, gate_fraction=1.2 / 2.2)
    y = np.asarray(twin.render(twin.cc_to_k({}), None, 48), dtype=np.float64)
    x = np.concatenate([np.zeros(SR), 0.9 * y / np.abs(y).max(), np.zeros(SR)])
    x += _noise(len(x), 0.002)
    p = tp.prepare_target(x, SR)
    assert abs(p.onset - 1.0) < 0.005 and abs(_key_up(p) - 2.2) < KEY_UP_TOL


# ── what to know: warnings ──────────────────────────────────────────────────────
def test_two_sounds_the_loudest_is_used() -> None:
    x = np.concatenate([_take(after=0.6, amp=0.15), _take(lead=0.4, f0=196.0, seed=5)])
    p = tp.prepare_target(x, SR)
    second = ONSET + 1.6 + 0.6 + 0.4
    assert abs(p.onset - second) < 0.005 and p.t0 > ONSET + 1.6, "only the louder second note"
    assert p.warnings == [tp.MORE]


def test_clipped_is_said() -> None:
    x = np.round(np.clip(_take(amp=1.5), -1, 1) * 32767) / 32767
    assert tp.CLIPPED in tp.prepare_target(x, SR).warnings


def test_clipping_is_judged_at_the_takes_own_rate() -> None:
    """Resampled to the twin's rate a clipped top is smooth again; the upload's own samples tell."""
    from synth.match.capture import AudioClip

    native = np.clip(_take(amp=1.5), -1, 1)
    at_48k = AudioClip(native.astype(np.float32), SR).resample(48000).samples.astype(np.float64)
    clipped = np.clip(at_48k * 1.0, -1, 1)
    resampled = AudioClip(clipped.astype(np.float32), 48000).resample(SR).samples.astype(np.float64)
    assert tp.CLIPPED in tp.prepare_target(resampled, SR, source=(clipped, 48000)).warnings
    assert tp.CLIPPED not in tp.prepare_target(resampled, SR).warnings, "the resampled take alone hides it"
    assert tp.CLIPPED not in tp.prepare_target(0.5 * resampled, SR, source=(0.5 * clipped, 48000)).warnings


def test_a_peak_normalized_sound_is_not_clipped() -> None:
    x = _take(noise_rms=0.0)
    assert tp.CLIPPED not in tp.prepare_target(x / np.abs(x).max(), SR).warnings


def test_very_quiet_is_said() -> None:
    p = tp.prepare_target(_take(amp=0.005, noise_rms=0.00003), SR)
    assert p.warnings == [tp.QUIET] and abs(p.onset - ONSET) < 0.005


def test_a_sound_barely_over_the_room_is_said_and_still_found() -> None:
    p = tp.prepare_target(_take(noise_rms=0.06), SR)
    assert tp.FAINT in p.warnings and abs(p.onset - ONSET) < 0.02


def test_a_long_sound_keeps_its_first_four_seconds() -> None:
    p = tp.prepare_target(_take(gate=5.5, note_s=6.0), SR)
    assert p.t1 - p.onset == pytest.approx(tp.MAX_KEEP_S, abs=0.01) and tp.LONG in p.warnings


# ── takes with no room: files ───────────────────────────────────────────────────
def test_a_clean_sample_keeps_its_start() -> None:
    p = tp.prepare_target(_take(lead=0.0, after=0.5, noise_rms=0.0), SR)
    assert p.t0 == 0.0 and p.onset < 0.003 and abs(_key_up(p) - HELD) < KEY_UP_TOL


def test_a_sample_cut_off_while_it_sounds_keeps_its_end() -> None:
    t = np.arange(int(0.6 * SR)) / SR
    x = 0.4 * _saw(220.0, len(t)) * np.exp(-t / 0.4)          # still at -13 dB when the file ends
    p = tp.prepare_target(x, SR)
    assert p.t0 == 0.0 and p.t1 == pytest.approx(0.6, abs=1e-3)


def test_a_steady_tone_that_fills_the_take_is_the_sound() -> None:
    x = 0.3 * _saw(110.0, 3 * SR)
    p = tp.prepare_target(x, SR)
    assert p.t0 == pytest.approx(0.0, abs=1e-3) and p.t1 == pytest.approx(3.0, abs=1e-3) and p.gate_s is None


# ── nothing to match: a plain error ─────────────────────────────────────────────
@pytest.mark.parametrize("x", [
    np.zeros(3 * SR),
    _noise(3 * SR, 0.1),                                     # white
    _noise(3 * SR, 0.05, slope_db_oct=-3),                   # pink, a room
    _noise(3 * SR, 0.05, slope_db_oct=-6),                   # a rumble that swells 20 dB and more
    _click(np.zeros(3 * SR), 1.0),                           # only a click
], ids=["silence", "white-noise", "pink-noise", "rumble", "only-a-click"])
def test_no_clear_sound_is_a_plain_error(x: np.ndarray) -> None:
    with pytest.raises(UploadError) as exc:
        tp.prepare_target(x, SR)
    assert str(exc.value) == tp.NO_SOUND and str(exc.value).endswith("or choose another file.")


def test_a_noise_sound_in_a_room_is_still_a_sound() -> None:
    """The S-1's noise, shaped by an envelope, is a sound even though it has no pitch: its
    spectrum is not the room's."""
    x = _noise(5 * SR, 0.003, seed=3, slope_db_oct=-3)
    burst = _noise(int(0.6 * SR), 0.1, seed=4) * _adsr(int(0.6 * SR), d=0.15, s=0.0, gate=0.6)
    x[2 * SR:2 * SR + len(burst)] += burst
    p = tp.prepare_target(x, SR)
    assert abs(p.onset - 2.0) < 0.005 and p.t1 < 2.7


# ── the user's own edges ────────────────────────────────────────────────────────
def test_a_user_crop_is_kept_and_the_key_up_read_inside_it() -> None:
    p = tp.prepare_target(_take(), SR, crop=(1.5, 3.2))
    assert (p.t0, p.t1) == (pytest.approx(1.5, abs=1e-3), pytest.approx(3.2, abs=1e-3))
    assert abs(p.onset - ONSET) < 0.005 and abs(_key_up(p) - (ONSET + HELD)) < KEY_UP_TOL
    assert len(p.samples) == pytest.approx(1.7 * SR, abs=2)


def test_a_user_crop_is_clamped_to_the_take() -> None:
    x = _take()
    p = tp.prepare_target(x, SR, crop=(5.0, 99.0))
    assert p.t1 == pytest.approx(len(x) / SR) and p.t1 - p.t0 >= 0.05


def test_parse_crop() -> None:
    assert tp.parse_crop("2.1,3.4") == (2.1, 3.4)
    assert tp.parse_crop([-1, 0.5]) == (0.0, 0.5)
    for junk in (None, "", "1", "a,b", "3,2", "1,nan", "1,2,3", 7):
        assert tp.parse_crop(junk) is None, junk


# ── the plan the matcher runs on ────────────────────────────────────────────────
def _wav(x: np.ndarray, sr: int = SR) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, np.clip(x, -1, 1).astype(np.float32), sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def test_plan_crops_the_upload_and_keeps_the_edges() -> None:
    pytest.importorskip("scipy")
    from synth.match import twin_session as ts

    p = ts.plan(_wav(_take()), "48", "quick")
    assert p.crop is not None and abs(p.crop[0] - (ONSET - tp.PRE_S)) < 0.005
    assert (p.crop[1] - p.crop[0]) * SR - 2 <= len(p.samples) <= (tp.MATCH_TAIL_S + 0.01) * SR, "and its tail"
    assert p.gate_s is not None and abs(p.gate_s - HELD) < KEY_UP_TOL
    cold = ts.plan(_wav(_take()))
    assert cold.notes == [48] and not cold.seeded, "cold-start detection runs on the crop"
    moved = ts.plan(_wav(_take()), "48", crop="1.0,3.5")
    assert moved.crop == (pytest.approx(1.0, abs=1e-3), pytest.approx(3.5, abs=1e-3))


def test_a_train_of_short_beeps_is_more_than_one_sound() -> None:
    """Chrome's fake microphone: a 20 ms beep every half second. One beep is used, and the take is
    said to hold more than one sound (a click next to a note is not)."""
    x = _noise(5 * SR, 0.001)
    beep = 0.5 * np.sign(np.sin(2 * np.pi * 400 * np.arange(int(0.025 * SR)) / SR))
    for k in range(9):
        i = int((0.5 + 0.5 * k) * SR)
        x[i:i + len(beep)] += beep * (1.2 if k == 3 else 1.0)
    p = tp.prepare_target(x, SR)
    assert abs(p.onset - 2.0) < 0.005 and p.t1 < 2.2 and tp.MORE in p.warnings
