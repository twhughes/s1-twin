"""Tests for the twin matcher's session (``synth/match/twin_session.py``).

The session is the phase logic shared by the cockpit's ``/ws/match`` and the soft
page: pitch → gd → note-search → done, one frame per optimization step, with the
candidate in CC space on every frame (docs/design/BUILD.md §2.4). These tests pin the
frame schema, the CC-space candidates, determinism, and the recorded-match format,
on tiny budgets so the whole file runs in seconds.
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import math
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

pytest.importorskip("autograd")
pytest.importorskip("scipy")

from synth.match import twin_session as ts  # noqa: E402
from synth.match.twin import K_PARAMS, S_PARAMS, Twin  # noqa: E402
from synth.schema import param_by_cc  # noqa: E402

TINY = {"gd_iters": 3, "restarts": 2, "neighbor_iters": 2}
TWIN_CCS = {str(p.cc) for p in K_PARAMS} | {str(p.cc) for p in S_PARAMS}
BASE_KEYS = {"phase", "notes", "chord_name", "note", "note_name", "iter", "total",
             "loss", "best_loss", "cc", "wave"}


def _target(notes=(57,)) -> np.ndarray:
    """A known patch rendered by the twin itself (bright, no sub or noise)."""
    twin = Twin()
    cc = {20: 122, 19: 55, 21: 0, 23: 0, 74: 100, 71: 20, 73: 3, 75: 45, 30: 105, 72: 20}
    return np.asarray(twin.render_chord(twin.cc_to_k(cc), None, list(notes)), dtype=np.float64)


@pytest.fixture(scope="module")
def seeded_run() -> list[ts.Step]:
    return list(ts.steps(_target(), [57], seeded=True, quality="quick", budget=TINY))


def _check_wave(w: dict) -> None:
    assert w["spc"] == ts.WAVE_SPC
    assert len(w["y"]) == ts.WAVE_SPC * ts.WAVE_CYCLES + 4
    assert all(math.isfinite(v) and abs(v) <= 1.0 + 1e-9 for v in w["y"])
    assert max(abs(v) for v in w["y"]) > 0.5          # peak-normalized, not silence
    assert w["level"] >= 0.0


def test_frame_schema_seeded(seeded_run: list[ts.Step]) -> None:
    frames = [s.frame for s in seeded_run]
    phases = [f["phase"] for f in frames]
    n_gd = TINY["gd_iters"] * TINY["restarts"]
    assert phases == ["pitch"] + ["gd"] * n_gd + ["done"]  # seeded: no note-search

    for f in frames:
        assert BASE_KEYS <= set(f), f"missing {BASE_KEYS - set(f)} on {f['phase']}"
        assert "params" not in f                          # page units belong to soft/
        assert f["notes"] == [57] and f["chord_name"] == "A3" and f["note_name"] == "A3"
        assert math.isfinite(f["loss"]) and math.isfinite(f["best_loss"])
        _check_wave(f["wave"])
        json.dumps(f)                                     # JSON-ready as-is

    pitch, done = frames[0], frames[-1]
    assert pitch["seeded"] is True and done["seeded"] is True
    assert pitch["iter"] == 0 and pitch["total"] == n_gd and pitch["restart"] == 0
    _check_wave(pitch["target_wave"])
    _check_wave(done["target_wave"])

    gd = frames[1:-1]
    assert [f["iter"] for f in gd] == list(range(1, n_gd + 1))
    assert [f["restart"] for f in gd] == [0] * TINY["gd_iters"] + [1] * TINY["gd_iters"]
    best = [f["best_loss"] for f in frames[:-1]]
    assert all(b <= a + 1e-12 for a, b in zip(best, best[1:])), "best_loss must not rise"

    for key in ("closeness", "seconds", "steps", "match_wav_b64", "target_wav_b64"):
        assert key in done
    assert 0.0 <= done["closeness"] <= 100.0
    assert done["steps"] >= len(frames) - 1
    for key in ("match_wav_b64", "target_wav_b64"):
        data, sr = sf.read(io.BytesIO(base64.b64decode(done[key])))
        assert sr > 0 and data.size > 0 and float(np.abs(data).max()) > 0.0


def test_every_frame_is_a_full_cc_candidate(seeded_run: list[ts.Step]) -> None:
    """CC space on EVERY frame: all 21 twin CCs, integers inside each CC's schema
    range, and exactly the twin's own k/s → CC mapping of that step's candidate."""
    twin = Twin()
    for step in seeded_run:
        cc = step.frame["cc"]
        assert set(cc) == TWIN_CCS
        for key, value in cc.items():
            p = param_by_cc(int(key))
            assert isinstance(value, int) and p.min_val <= value <= p.max_val
        assert cc == {str(c): v for c, v in twin.k_to_cc(step.k, step.s).items()}


def test_run_is_deterministic(seeded_run: list[ts.Step]) -> None:
    again = list(ts.steps(_target(), [57], seeded=True, quality="quick", budget=TINY))
    strip = ("seconds",)
    a = [{k: v for k, v in s.frame.items() if k not in strip} for s in seeded_run]
    b = [{k: v for k, v in s.frame.items() if k not in strip} for s in again]
    assert a == b


def test_cold_start_runs_the_note_search() -> None:
    frames = [s.frame for s in ts.steps(_target(), [57], seeded=False, cold_candidates=[57, 64],
                                         quality="quick", budget=TINY)]
    assert frames[0]["phase"] == "pitch" and frames[0]["seeded"] is False
    ns = [f for f in frames if f["phase"] == "note-search"]
    # transpose −12 and +12, and add the next candidate (64): three sets × 2 steps
    assert len(ns) == 3 * TINY["neighbor_iters"]
    assert all(isinstance(f["improved"], bool) and f["total"] == len(ns) for f in ns)
    assert {tuple(f["notes"]) for f in ns} == {(45,), (69,), (57, 64)}
    assert all(set(f["cc"]) == TWIN_CCS for f in ns)
    assert frames[-1]["phase"] == "done"


def test_warm_start_from_a_cc_map() -> None:
    """init as a CC map: the first frame IS the synth's current knobs (CC → k → CC is
    exact), and the switch CCs pick the discrete choices."""
    init = {"74": 30, "71": 100, "73": 9, "22": 0, "28": 0, "12": 3, "99": 5, "bad": "x"}
    k, s = ts.cc_init(init)
    assert s == {"sub_octave": 0, "amp_env_mode": 0, "lfo_shape": 3}
    first = next(ts.steps(_target(), [57], seeded=True, init_k=k, init_s=s, budget=TINY))
    cc = first.frame["cc"]
    assert (cc["74"], cc["71"], cc["73"]) == (30, 100, 9)
    assert (cc["22"], cc["28"], cc["12"]) == (0, 0, 3)
    # an out-of-range switch value is ignored, not trusted
    assert ts.cc_init({"28": 7})[1] == {}


def test_request_parsing() -> None:
    assert ts.parse_notes("60, 64,x,64,200,-1,67,72,48") == [48, 60, 64, 67]
    assert ts.parse_notes("") == [] and ts.parse_notes(None) == []
    assert ts.parse_quality("quick") == "quick"
    assert ts.parse_quality("fast") == ts.DEFAULT_QUALITY == ts.parse_quality(None)
    assert ts.clamp_throttle("abc") == ts.DEFAULT_THROTTLE
    assert ts.clamp_throttle("nan") == ts.DEFAULT_THROTTLE
    assert ts.clamp_throttle("9") == ts.MAX_THROTTLE
    assert ts.clamp_throttle("-1") == 0.0
    assert ts.clamp_throttle(None, 0.1) == 0.1


def _wav_bytes(samples: np.ndarray, sr: int = 22050) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, samples, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def test_decode_upload_says_what_went_wrong() -> None:
    with pytest.raises(ts.UploadError, match="empty"):
        ts.decode_upload(b"")
    with pytest.raises(ts.UploadError, match="audio"):
        ts.decode_upload(b"definitely not audio")
    with pytest.raises(ts.UploadError, match="silent"):
        ts.decode_upload(_wav_bytes(np.zeros(4000)))
    ok = ts.decode_upload(_wav_bytes(0.3 * np.sin(np.arange(22050) * 2 * np.pi * 220 / 22050)))
    assert ok.dtype == np.float64 and ok.size > 20000


def test_wave_snippet_is_two_cycles() -> None:
    sr, f0 = 16000, 200.0
    sine = np.sin(2 * np.pi * f0 * np.arange(sr) / sr)
    w = ts.wave_snippet(sine, sr, f0, 0.25)
    y = np.asarray(w["y"])
    spc = w["spc"]
    assert np.allclose(y[2:2 + spc], y[2 + spc:2 + 2 * spc], atol=2e-3)  # periodic at spc
    assert w["level"] == pytest.approx(1.0, abs=0.02)                      # steady tone
    quiet = ts.wave_snippet(sine * np.linspace(0, 1, sr) ** 4, sr, f0, 0.05)
    assert quiet["level"] < 0.1                                            # still quiet there


def test_soft_page_frames_keep_their_schema(seeded_run: list[ts.Step]) -> None:
    """soft/server.py converts session steps to its page units: ``params`` on every
    frame, ``cc`` only on done, no plume keys (the documented soft schema)."""
    path = Path(__file__).resolve().parent.parent / "soft" / "server.py"
    spec = importlib.util.spec_from_file_location("soft_server_for_session_test", path)
    assert spec is not None and spec.loader is not None
    soft = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(soft)
    frames = [soft.page_frame(s) for s in seeded_run]
    for f in frames[:-1]:
        assert "params" in f and "cc" not in f and "wave" not in f and "target_wave" not in f
    assert "params" in frames[-1] and set(frames[-1]["cc"]) == TWIN_CCS
    assert frames[0]["params"]["saw"] == pytest.approx(float(seeded_run[0].k[0]))


def test_recorded_match_format(tmp_path: Path) -> None:
    wav = tmp_path / "target.wav"
    wav.write_bytes(_wav_bytes(_target().astype(np.float32)))
    rec = ts.record_match(wav, title="A3 bright saw", notes="57", quality="quick", budget=TINY)
    assert set(rec) == {"title", "target_url", "notes", "frames", "recorded", "engine"}
    assert rec["notes"] == [57] and rec["engine"].startswith("twin ")
    assert rec["frames"][0]["phase"] == "pitch" and rec["frames"][-1]["phase"] == "done"

    out = tmp_path / "matches"
    path = ts.write_recording(rec, out)
    assert path == out / "a3-bright-saw.json"
    assert json.loads(path.read_text())["frames"] == rec["frames"]
    index = json.loads((out / "index.json").read_text())
    assert [r["slug"] for r in index] == ["a3-bright-saw"]
    assert index[0]["closeness"] == rec["frames"][-1]["closeness"]
    ts.write_recording(rec, out)                      # re-recording replaces its row
    assert len(json.loads((out / "index.json").read_text())) == 1
