"""Parity gate: the browser twin (``synth/web/static/twin/dsp.js``) against ``twin.py``.

Eight fixed patches x three notes (36, 48, 60) are rendered at the twin's own settings
(22050 Hz, 2 s, key up at 1.2 s) three ways, and compared with ``twin.logmel_loss``:

* **python** — ``Twin().render(Twin().cc_to_k(cc), s, note)``: the reference.
* **offline** — ``node tools/twin_render.mjs --path offline``: ``dsp.renderNote``, a
  line-by-line port (additive oscillators, numpy's seeded noise, the FFT ladder).
* **realtime** — ``node tools/twin_render.mjs --path realtime``: ``dsp.Engine`` in
  128-sample blocks, the code the AudioWorklet runs (wavetable oscillators,
  impulse-invariant ladders in twin.py's frame layout).

The scale behind the thresholds
-------------------------------
``logmel_loss`` is the mean absolute difference of level-normalized log-mel
spectrograms, in nats. Between two DIFFERENT patches of this set at the same note it is
1.87 at the closest pair and about 6-7 at the median; ``test_patches_are_distinct``
keeps the closest pair above ``MIN_DISTINCT`` so the thresholds keep their meaning if
the patch list changes.

* ``OFFLINE_TOL = 0.01`` (1/150 of the closest pair). The port is exact; what remains is
  the float32 file format near the log-mel floor (measured max 0.0009).
* ``REALTIME_TOL = 0.1`` (1/15 of the closest pair) against twin.py's own ladder math
  with its frame wrap removed (see below). Measured max 0.07 (``noise_breath``): the
  impulse-invariant ladder aliases its 24 dB/oct tail by +1.6 to +2.8 dB between 0.40 and
  0.44 x sr, and broadband noise shows it at 22.05 kHz. At the browser's 48 kHz the same
  patch is at 0.024. Every other case is at 0.027 or below.
* Against twin.py itself, the real-time path may differ by ``REALTIME_TOL`` plus twin.py's
  own frame-wrap artifact on that case. ``twin._ladder_tv`` filters 40 ms Hann frames by
  circular convolution with nfft = 2 x window, so ringing that outlasts the next 40 ms
  wraps onto the frame's start. On the modulated low notes that moves twin.py's output by
  up to 0.48 against the identical math with a wrap-free FFT (computed here, per case, as
  ``wrap``). A causal filter cannot reproduce a wrap, so the gate is
  ``loss(realtime, twin) <= wrap + REALTIME_TOL``.

Skips cleanly when ``node`` is not installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

import synth.match.twin as twin_mod
from synth.match.twin import S_PARAMS, Twin, _default_s, _ladder_response, logmel_loss

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "tools" / "twin_render.mjs"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

OFFLINE_TOL = 0.01
REALTIME_TOL = 0.1
MIN_DISTINCT = 1.5

# CC maps (S-1 numbers). Unlisted CCs take s1.json defaults, exactly as cc_to_k does.
PATCHES: dict[str, dict[int, int]] = {
    "bright_saw": {20: 127, 19: 0, 21: 0, 74: 127, 71: 0, 73: 0, 75: 60, 30: 100, 72: 40},
    "resonant": {20: 110, 19: 0, 74: 45, 71: 115, 73: 0, 75: 70, 30: 110, 72: 50},
    "heavy_env_sweep": {20: 120, 19: 0, 74: 25, 71: 70, 24: 110, 73: 0, 75: 45, 30: 20, 72: 40},
    "slow_attack": {20: 0, 19: 110, 15: 50, 74: 85, 71: 20, 73: 105, 75: 90, 30: 110, 72: 90},
    "lfo_on_cutoff": {20: 100, 19: 0, 74: 55, 71: 50, 25: 90, 3: 70, 17: 110, 12: 2,
                      73: 0, 75: 80, 30: 110, 72: 50},
    "sub_heavy": {20: 40, 19: 0, 21: 127, 22: 0, 74: 70, 71: 30, 73: 0, 75: 70, 30: 90, 72: 40},
    "noise_breath": {20: 30, 19: 0, 23: 110, 74: 80, 71: 40, 24: 60, 73: 40, 75: 80, 30: 60, 72: 60},
    "vibrato_gate": {20: 0, 19: 100, 15: 20, 13: 20, 17: 90, 3: 75, 12: 3, 28: 0, 74: 95, 71: 25,
                     73: 10, 72: 30, 76: 80},
}
NOTES = (36, 48, 60)
CASES = [(name, note) for name in PATCHES for note in NOTES]
IDS = [f"{name}-{note}" for name, note in CASES]


def _s_of(cc: dict[int, int]) -> dict[str, int]:
    """The discrete s choices a CC map selects (twin.calibrate's rule)."""
    return {sp.name: int(cc.get(sp.cc, _default_s()[sp.name])) for sp in S_PARAMS}


def _ladder_tv_wrap_free(x, fc_t, k, sr, block):
    """twin._ladder_tv's exact math with nfft = 4 x window instead of 2 x window, so no
    frame's ringing wraps onto its start (plain numpy: this is a reference, not a gradient)."""
    n = x.shape[-1]
    block = int(max(8, block))
    win, hop = 2 * block, block
    nfft = 4 * win
    pad = hop
    n_frames = 1 + -(-(pad + n) // hop)
    total = (n_frames - 1) * hop + win
    xp = np.concatenate([np.zeros(pad), x, np.zeros(total - pad - n)])
    cp = np.concatenate([np.full(pad, fc_t[0]), fc_t, np.full(total - pad - n, fc_t[-1])])
    idx = np.arange(win)[None, :] + hop * np.arange(n_frames)[:, None]
    frames = xp[idx] * np.hanning(win)
    fc_frame = cp[idx].mean(axis=-1)
    freqs = np.fft.rfftfreq(nfft, 1.0 / float(sr))
    y = np.fft.irfft(np.fft.rfft(frames, n=nfft, axis=-1)
                     * _ladder_response(freqs[None, :], fc_frame[:, None], k), n=nfft, axis=-1)
    out = np.zeros(total + nfft)
    for i in range(n_frames):
        out[i * hop:i * hop + nfft] += y[i]
    return out[pad:pad + n]


def _node_render(jobs: list[dict], tmp: Path) -> list[np.ndarray]:
    for i, job in enumerate(jobs):
        job["out"] = str(tmp / f"{i}.f32")
    jobs_file = tmp / "jobs.json"
    jobs_file.write_text(json.dumps(jobs), encoding="utf-8")
    subprocess.run([NODE, str(CLI), "--jobs", str(jobs_file)], check=True, cwd=ROOT, timeout=600)
    return [np.fromfile(job["out"], dtype="<f4").astype(np.float64) for job in jobs]


@pytest.fixture(scope="module")
def renders(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("twin_parity")
    jobs = [{"cc": {str(c): v for c, v in PATCHES[name].items()}, "note": note, "path": path}
            for path in ("offline", "realtime") for name, note in CASES]
    js = _node_render(jobs, tmp)
    offline = dict(zip(CASES, js[:len(CASES)]))
    realtime = dict(zip(CASES, js[len(CASES):]))
    tw = Twin()
    python, wrap_free = {}, {}
    for name, note in CASES:
        k, s = tw.cc_to_k(PATCHES[name]), _s_of(PATCHES[name])
        python[(name, note)] = np.asarray(tw.render(k, s, note), dtype=np.float64)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(twin_mod, "_ladder_tv", _ladder_tv_wrap_free)
            wrap_free[(name, note)] = np.asarray(tw.render(k, s, note), dtype=np.float64)
    return {"python": python, "wrap_free": wrap_free, "offline": offline, "realtime": realtime}


def test_patches_are_distinct(renders):
    """The yardstick: different patches must stay far apart, or the tolerances mean nothing."""
    py = renders["python"]
    for note in NOTES:
        names = list(PATCHES)
        closest = min(float(logmel_loss(py[(a, note)], py[(b, note)]))
                      for i, a in enumerate(names) for b in names[i + 1:])
        assert closest > MIN_DISTINCT, f"note {note}: two patches only {closest:.3f} apart"


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_offline_matches_twin(renders, case):
    js, py = renders["offline"][case], renders["python"][case]
    assert js.shape == py.shape
    loss = float(logmel_loss(js, py))
    assert loss < OFFLINE_TOL, f"{case}: logmel {loss:.4f}"


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_realtime_matches_twin_math(renders, case):
    """The real-time engine against twin.py's ladder math without the frame wrap."""
    js, ref = renders["realtime"][case], renders["wrap_free"][case]
    assert js.shape == ref.shape
    assert np.isfinite(js).all()
    loss = float(logmel_loss(js, ref))
    assert loss < REALTIME_TOL, f"{case}: logmel {loss:.4f}"


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_realtime_matches_twin(renders, case):
    """The real-time engine against twin.py as shipped: within REALTIME_TOL of it, beyond
    the part that is twin.py's own frame-wrap artifact on this case."""
    js, py = renders["realtime"][case], renders["python"][case]
    wrap = float(logmel_loss(renders["wrap_free"][case], py))
    loss = float(logmel_loss(js, py))
    assert loss < wrap + REALTIME_TOL, f"{case}: logmel {loss:.4f} (twin's own wrap: {wrap:.4f})"


def test_cli_single_render_contract(tmp_path, renders):
    """The documented one-shot interface writes raw little-endian float32 at the twin's rate."""
    out = tmp_path / "note.f32"
    cc = json.dumps({str(c): v for c, v in PATCHES["bright_saw"].items()})
    subprocess.run([NODE, str(CLI), "--cc", cc, "--note", "48", "--out", str(out)],
                   check=True, cwd=ROOT, timeout=120)
    audio = np.fromfile(out, dtype="<f4")
    assert audio.shape == (int(round(Twin().seconds * Twin().sr)),)
    assert np.array_equal(audio.astype(np.float64), renders["offline"][("bright_saw", 48)])
