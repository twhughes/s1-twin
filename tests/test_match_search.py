"""Round 4 of the twin matcher's search (``synth/match/twin_session.py``): it tries harder.

Each descent runs until it stops improving (the plateau rule), at a cosine learning rate; after each
start the other switch settings are re-descended briefly and a winner is kept; a final polish runs;
and "finish" ends a run early with a done frame. Round 6: the starts are plain patches scored at the
target, a mild prior keeps the extras down, a seed varies the random starts only, and the note's
length (the key-up) is scanned, with the target's noise floor matched. The rules are pure and pinned
here; the search runs on tiny budgets so this file stays fast. The before/after numbers live in
docs/match-benchmarks.md (``tools/match_benchmark.py``, not part of this suite).
"""

from __future__ import annotations

import io
import math
import threading

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

pytest.importorskip("autograd")
pytest.importorskip("scipy")

import synth.web.server as server_mod  # noqa: E402
from synth.match import twin_session as ts  # noqa: E402
from synth.match.twin import S_PARAMS, Twin  # noqa: E402

# A tiny round-4 budget: every new stage runs, a few steps each.
TINY_NEW = {"gd_iters": 8, "restarts": 1, "neighbor_iters": 2, "patience": 3, "min_iters": 4, "tol": 1e-3,
            "lr_min": 0.008, "switch_top": 1, "switch_iters": 8, "continue_iters": 4, "polish_iters": 3}
# Gate, with a filter envelope: then Gate is not the same sound as Envelope with Sustain full.
GATE = {20: 100, 19: 40, 21: 0, 23: 0, 15: 20, 74: 60, 71: 40, 24: 50, 25: 0, 26: 0, 73: 5, 75: 60,
        30: 30, 72: 30, 3: 60, 13: 0, 17: 15, 76: 64, 22: 2, 12: 2, 28: 0}


def _render(cc: dict[int, int], note: int = 48) -> np.ndarray:
    twin = Twin()
    s = {sp.name: cc[sp.cc] for sp in S_PARAMS}
    return np.asarray(twin.render_chord(twin.cc_to_k(cc), s, [note]), dtype=np.float64)


# ── the rules ──────────────────────────────────────────────────────────────────────
def test_cosine_schedule_falls_from_the_start_rate_to_the_floor() -> None:
    lrs = [ts.cosine_lr(t, 100, 0.08, 0.008) for t in range(1, 101)]
    assert lrs[0] == pytest.approx(0.08) and lrs[-1] == pytest.approx(0.008)
    assert ts.cosine_lr(50, 99, 0.08, 0.008) == pytest.approx(0.044)            # the middle: halfway
    assert all(b <= a + 1e-15 for a, b in zip(lrs, lrs[1:])), "it only falls"
    assert ts.cosine_lr(500, 100, 0.08, 0.008) == pytest.approx(0.008)          # past the cap: the floor
    assert ts.cosine_lr(7, 100, 0.08, None) == 0.08 == ts.cosine_lr(1, 1, 0.08, 0.008)  # constant


def test_plateau_rule() -> None:
    improving = [10.0 * 0.9 ** i for i in range(40)]
    flat = [5.0] * 10 + [4.9999] * 30
    assert not ts.plateaued(flat, None) and not ts.plateaued(flat, 0), "no patience: run to the cap"
    assert not ts.plateaued(improving, 5, 1e-3), "still improving"
    assert ts.plateaued(flat, 5, 1e-3), "no gain over 5 steps"
    assert not ts.plateaued(flat[:12], 5, 1e-3, min_iters=20), "never before min_iters"
    assert not ts.plateaued(flat[:5], 5, 1e-3), "needs patience + 1 steps to compare"
    slow = [1.0 - 0.0001 * i for i in range(30)]                               # 0.05% per 5 steps
    assert ts.plateaued(slow, 5, 1e-3) and not ts.plateaued(slow, 5, 1e-4), "the tolerance is relative"
    assert not ts.plateaued([math.inf] * 10, 3, 1e-3), "nothing finite yet"
    assert ts.plateaued([0.0] * 10, 3, 1e-3), "a perfect match has nothing left to gain"


def test_switch_words_and_candidates() -> None:
    base = dict(ts.S_DEFAULT)
    assert ts.switch_words({**base, "amp_env_mode": 0}, base) == "Volume shape: Gate"
    assert ts.switch_words({"sub_octave": 0, "lfo_shape": 3, "amp_env_mode": 1}, base) == \
        "Sub octave: −2 asym, LFO wave: Square"
    assert ts.switch_words(base, base) == ""
    cur = {"sub_octave": 2, "lfo_shape": 2, "amp_env_mode": 1}
    scored = [
        (cur, 1.0),
        ({"sub_octave": 2, "lfo_shape": 3, "amp_env_mode": 1}, 1.0),   # does nothing here: left out
        ({"sub_octave": 1, "lfo_shape": 2, "amp_env_mode": 1}, 0.7),
        ({"sub_octave": 1, "lfo_shape": 3, "amp_env_mode": 1}, 0.7),   # sounds like the one above
        ({"sub_octave": 2, "lfo_shape": 2, "amp_env_mode": 0}, 0.5),
        ({"sub_octave": 0, "lfo_shape": 2, "amp_env_mode": 1}, 1.4),
        ({"sub_octave": 0, "lfo_shape": 2, "amp_env_mode": 0}, float("nan")),
    ]
    all_ = ts.switch_candidates(scored, cur, 1.0, -1)
    assert all_ == [{"sub_octave": 2, "lfo_shape": 2, "amp_env_mode": 0},
                    {"sub_octave": 1, "lfo_shape": 2, "amp_env_mode": 1},
                    {"sub_octave": 0, "lfo_shape": 2, "amp_env_mode": 1}], "best first; one of each sound"
    assert ts.switch_candidates(scored, cur, 1.0, 1) == all_[:1]
    assert ts.switch_candidates(scored, cur, 1.0, 0) == []


def test_presets_and_old_budgets() -> None:
    assert ts.parse_quality("deep") == "deep" and set(ts.QUALITY_PRESETS) == {"quick", "thorough", "deep"}
    q, t, d = (ts.QUALITY_PRESETS[k] for k in ("quick", "thorough", "deep"))
    assert q["restarts"] < t["restarts"] < d["restarts"] and d["restarts"] >= 8
    assert q["switch_top"] == 1 and t["switch_top"] == 3 and d["switch_top"] == -1, "Deep tries every setting"
    assert (q["lfo_top"], t["lfo_top"], d["lfo_top"]) == (1, 1, 2), "Deep scans the pitch and the filter"
    assert (q["plain_starts"], t["plain_starts"], d["plain_starts"]) == (1, 4, 8), "plain starts"
    assert all(p["gate_scan"] and p["floor_match"] for p in (q, t, d)), "the length scan and the floor"
    assert all(p["polish_iters"] > 0 and p["patience"] and p["lr_min"] for p in (q, t, d))
    old = ts.resolve_budget("thorough", {"gd_iters": 3, "restarts": 2, "neighbor_iters": 2})
    assert (old["patience"], old["lr_min"], old["switch_top"], old["polish_iters"], old["lfo_top"]) == \
        (None, None, 0, 0, 0), "a budget without the round-4 keys runs the old search"


def test_the_lfo_scan_grid() -> None:
    """The LFO wave only shows near the right rate, so the switch stage scans each wave the matcher
    tries at rates across the range, on the pitch or on the filter (renders only)."""
    from synth.match.twin import K_NAMES

    assert ts.LFO_WAVES == next(sp.choices for sp in S_PARAMS if sp.name == "lfo_shape")
    k = np.full(len(K_NAMES), 0.25)
    grid = ts.lfo_hypotheses(k, dict(ts.S_DEFAULT))
    assert len(grid) == 3 * 9 * 2
    i = {n: K_NAMES.index(n) for n in ("lfo_rate", "lfo_to_pitch", "lfo_to_cutoff", "lfo_depth", "cutoff")}
    for k2, s2, words in grid:
        assert s2["lfo_shape"] in ts.LFO_WAVES and s2["sub_octave"] == ts.S_DEFAULT["sub_octave"]
        assert k2[i["lfo_depth"]] == 0.5 and k2[i["cutoff"]] == 0.25, "only the LFO knobs change"
        on_pitch = words.endswith("on the pitch")
        assert (k2[i["lfo_to_pitch"]] > 0) == on_pitch and (k2[i["lfo_to_cutoff"]] > 0) == (not on_pitch)
        assert words.startswith("LFO wave: ") and ", rate " in words
    rates = sorted({round(float(k2[i["lfo_rate"]]), 6) for k2, _, _ in grid})
    assert rates[0] == pytest.approx(0.1) and rates[-1] == pytest.approx(0.9) and len(rates) == 9
    assert grid[0][2] == "LFO wave: Triangle, rate 13, on the pitch"
    assert k[i["lfo_rate"]] == 0.25, "the start's knobs are left as they were"


def test_warmup_ramps_the_first_steps() -> None:
    """A descent from good knobs ramps its rate up: Adam's first step moves each knob by about the
    rate whatever the gradient, which would throw a good start away."""
    def run(warmup: int) -> list[float]:
        it = ts._adam(lambda x: np.ones_like(x), lambda x: x, lambda a: float(a.sum()),
                      np.full(3, 0.5), 3, 0.08, warmup=warmup)
        return [float(x[0]) for x, _l, _a in it]
    assert run(0)[0] == pytest.approx(0.5 - 0.08, abs=1e-6)
    cold = run(8)
    assert cold[0] == pytest.approx(0.5 - 0.01, abs=1e-6)
    assert cold[1] == pytest.approx(cold[0] - 0.02, abs=1e-6)


# ── the search ─────────────────────────────────────────────────────────────────────
def test_a_switch_re_descent_finds_gate_on_a_tiny_budget() -> None:
    """From the true knobs but the default switches (Envelope), the descent cannot reach the target's
    Gate; the switch trial re-descends under Gate, wins, and the run keeps it."""
    target = _render(GATE)
    k, _ = ts.cc_init(GATE)
    frames = [s.frame for s in ts.steps(target, [48], seeded=True, init_k=k, budget=TINY_NEW)]
    phases = [f["phase"] for f in frames]
    assert phases[0] == "pitch" and phases[-1] == "done" and set(phases[1:-1]) == {"gd"}
    trials = [f for f in frames if f.get("trying") and f["trying"] != ts.POLISH_WORDS]
    assert trials and all(f["trying"] == "Volume shape: Gate" for f in trials)
    assert all(f["cc"]["28"] == 0 for f in trials), "a trial frame's candidate is under the tried switch"
    polish = [f for f in frames if f.get("trying") == ts.POLISH_WORDS]
    assert 0 < len(polish) <= TINY_NEW["polish_iters"]
    assert frames[-1]["cc"]["28"] == 0, "Gate came back"
    assert all(f["starts"] == 1 for f in frames if f["phase"] == "gd")
    best = [f["best_loss"] for f in frames[:-1]]
    assert all(b <= a + 1e-12 for a, b in zip(best, best[1:])), "best_loss never rises"
    assert frames[-1]["iter"] == len(frames) - 2 and frames[-1]["iter"] <= frames[1]["total"]
    assert "finished" not in frames[-1]


def test_the_old_search_carries_no_new_fields() -> None:
    frames = [s.frame for s in ts.steps(_render(GATE), [48], seeded=True,
                                         budget={"gd_iters": 3, "restarts": 1, "neighbor_iters": 2})]
    assert not any("trying" in f or "starts" in f or "finished" in f for f in frames)


def test_finish_ends_a_run_early_and_still_yields_a_done_frame() -> None:
    stop = threading.Event()
    frames = []
    for step in ts.steps(_render(GATE), [48], seeded=True, quality="thorough", stop=stop):
        frames.append(step.frame)
        if len(frames) == 3:
            stop.set()                            # "finish now", after two gd steps
    assert [f["phase"] for f in frames] == ["pitch", "gd", "gd", "done"]
    done = frames[-1]
    assert done["finished"] is True and done["iter"] == 2
    assert 0.0 <= done["closeness"] <= 100.0 and done["match_wav_b64"] and done["target_wav_b64"]
    assert done["best_loss"] == min(f["loss"] for f in frames[:-1]), "the best so far"


def _wav(samples: np.ndarray, sr: int = 22050) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, samples.astype(np.float32), sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def test_finish_over_the_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """/ws/match: the text message "finish" ends the search at the next step, and the done frame
    still comes, with the best so far scored."""
    monkeypatch.setitem(ts.QUALITY_PRESETS, "quick", {**TINY_NEW, "gd_iters": 400, "patience": None})
    host = f"127.0.0.1:{server_mod.PORT}"
    client = TestClient(server_mod.app, base_url=f"http://{host}")
    frames = []
    query = "/ws/match?throttle=0&quality=quick&notes=48"
    with client.websocket_connect(query, headers={"host": host}) as ws:
        ws.send_bytes(_wav(0.9 * _render(GATE) / np.abs(_render(GATE)).max()))
        while True:
            f = ws.receive_json()
            frames.append(f)
            if len(frames) == 4:
                ws.send_text("finish")
            if f["phase"] in ("done", "error"):
                break
    done = frames[-1]
    assert done["phase"] == "done" and done["finished"] is True
    assert len(frames) < 60, f"finished early ({len(frames)} frames, not the 400-step budget)"
    assert done["closeness"] >= 0.0 and done["match_wav_b64"]


# ── round 6: plain starts and a mild prior on the extras ────────────────────────────
# The S-1's default patch (a full square, the filter open) and a real vibrato (tools/match_benchmark.py).
SQUARE = {20: 0, 19: 127, 21: 0, 23: 0, 15: 0, 13: 0, 76: 64, 22: 2, 74: 127, 71: 0, 24: 0, 25: 0, 26: 0,
          73: 0, 75: 42, 30: 25, 72: 21, 28: 1, 3: 60, 17: 15, 12: 2}
VIBRATO = {**SQUARE, 20: 90, 19: 0, 74: 85, 71: 20, 24: 25, 73: 4, 75: 55, 30: 80, 72: 30,
           3: 55, 13: 35, 17: 60, 12: 3}
TINY_R6 = {**TINY_NEW, "restarts": 2, "plain_starts": 2, "prior": 0.05}


def _switches(cc: dict[int, int]) -> dict[str, int]:
    return {sp.name: cc[sp.cc] for sp in S_PARAMS}


def test_the_plain_starts() -> None:
    from synth.match.twin import K_NAMES

    patches = ts.plain_patches()
    at = {n: i for i, n in enumerate(K_NAMES)}
    assert len(patches) == 5 * 2 * 2 * 2
    for k, _w, (mix, _f) in patches:
        assert all(k[at[n]] == 0.0 for n in ("noise_lvl", "lfo_to_pitch", "lfo_to_cutoff", "lfo_depth")), \
            "no plain start has noise, vibrato or an LFO"
        assert (k[at["sub_lvl"]] > 0) == mix.endswith("Sub"), "a sub only in the sub mixes"
    kinds = {kind for _k, _w, kind in patches}
    assert kinds == {(m, f) for m, _ in ts.PLAIN_MIXES for f, _ in ts.PLAIN_FILTERS}
    scored = [(1.0, patches[0][0], "a", ("Saw", "open")), (0.5, patches[1][0], "b", ("Saw", "open")),
              (0.7, patches[2][0], "c", ("Square", "open")),
              (float("nan"), patches[3][0], "d", ("Saw", "half open"))]
    assert [w for _k, w in ts.pick_plain(scored, 2)] == ["b", "c"], "best first, one of each kind"
    assert [w for _k, w in ts.pick_plain(scored, 3)] == ["b", "c", "a"], "then the next best"


def test_a_square_target_gets_a_square_start() -> None:
    """The plain starts are scored at the target (renders only): the S-1's default square picks the
    square start, and its descent says so."""
    budget = {**TINY_R6, "restarts": 1}
    frames = [s.frame for s in ts.steps(_render(SQUARE), [48], seeded=True, budget=budget)]
    first = next(f for f in frames if f["phase"] == "gd")
    assert first["trying"] == "Square, filter open"
    pitch = frames[0]
    cc = pitch["cc"]
    assert (cc["19"], cc["20"], cc["23"]) == (127, 0, 0), "it starts at that square"


def test_the_knobs_still_come_first() -> None:
    """'Start from: Current knobs' (init_k, init_s) is still the first start; the plain starts follow."""
    k, _s = ts.cc_init(GATE)
    budget = {**TINY_R6, "restarts": 2, "plain_starts": 4, "switch_top": 0, "lfo_top": 0}
    frames = [s.frame for s in ts.steps(_render(SQUARE), [48], seeded=True, init_k=k, init_s=_switches(GATE),
                                        budget=budget)]
    pitch = frames[0]
    assert all(abs(pitch["cc"][str(c)] - v) <= 1 for c, v in GATE.items()), "it starts at the knobs"
    gd = [f for f in frames if f["phase"] == "gd"]
    assert "trying" not in next(f for f in gd if f["restart"] == 0), "the knobs' descent is not a plain start"
    # (the plain starts are scored under the knobs' switches, here Gate)
    second = next(f for f in gd if f["restart"] == 1)
    assert second["trying"].startswith("Square, filter"), "a plain start after"


def test_the_prior_is_zero_when_every_extra_is_zero() -> None:
    """A plain start has every extra at 0, so its loss is the same with the prior on or off."""
    target = _render(SQUARE)
    off = next(ts.steps(target, [48], seeded=True, budget={**TINY_R6, "prior": 0.0})).frame
    on = next(ts.steps(target, [48], seeded=True, budget={**TINY_R6, "prior": 0.5})).frame
    assert off["loss"] == on["loss"] and off["cc"] == on["cc"]


def test_the_prior_never_flips_a_real_vibrato() -> None:
    """A real vibrato scores better with its LFO than without, even with the prior far above the one
    the presets use; and a short descent from it under the prior keeps the vibrato."""
    target = _render(VIBRATO, note=55)
    lam = 4 * max(ts.QUALITY_PRESETS[q]["prior"] for q in ts.QUALITY_PRESETS) or 0.2
    budget = {**TINY_R6, "prior": lam, "restarts": 1, "plain_starts": 0}

    def start_loss(cc: dict[int, int]) -> float:
        k, _s = ts.cc_init(cc)
        first = next(ts.steps(target, [55], seeded=True, init_k=k, init_s=_switches(cc), budget=budget))
        return first.frame["loss"]

    assert start_loss(VIBRATO) < start_loss({**VIBRATO, 13: 0, 17: 0}), "the vibrato is worth its prior"
    k, _s = ts.cc_init(VIBRATO)
    done = [s.frame for s in ts.steps(target, [55], seeded=True, init_k=k, init_s=_switches(VIBRATO),
                                      budget={**budget, "switch_top": 0, "lfo_top": 0})][-1]
    assert abs(done["cc"]["13"] - 35) <= 10 and done["cc"]["17"] > 30 and done["cc"]["12"] == 3


def test_a_seed_changes_the_random_starts_only() -> None:
    target = _render(SQUARE)
    budget = {**TINY_R6, "restarts": 3, "plain_starts": 1, "gd_iters": 3, "switch_top": 0, "lfo_top": 0,
              "polish_iters": 0}
    runs = {seed: [s.frame for s in ts.steps(target, [48], seeded=True, budget=budget, seed=seed)]
            for seed in (0, 1)}
    starts = {seed: [f for f in fr if f["phase"] == "gd"] for seed, fr in runs.items()}
    words = [f.get("trying") for f in starts[0]]
    assert words[0] == "Square, filter open" and "a random start" in words
    plain0 = [f["cc"] for f in starts[0] if f["restart"] == 0]
    plain1 = [f["cc"] for f in starts[1] if f["restart"] == 0]
    assert plain0 == plain1, "the plain start does not change with the seed"
    rand0 = [f["cc"] for f in starts[0] if f["restart"] == 1]
    assert rand0 != [f["cc"] for f in starts[1] if f["restart"] == 1], "the random start does"
    rand = next(f for f in starts[0] if f["restart"] == 1)
    assert all(rand["cc"][c] <= 20 for c in ("23", "21", "13", "25")), "a random start keeps its extras low"


# ── round 6: the note's length, and the target's noise floor ────────────────────────
SHORT = {**SQUARE, 20: 110, 19: 30, 74: 80, 71: 30, 24: 30, 73: 2, 75: 60, 30: 90, 72: 45}   # "short"


def _wav_target(cc: dict[int, int], gate: float, note: int = 48) -> np.ndarray:
    """A 16-bit WAV of the twin (the key up at ``gate`` s), decoded like an upload: it has a noise floor."""
    twin = Twin(seconds=2.2, gate_fraction=gate / 2.2)
    audio = np.asarray(twin.render(twin.cc_to_k(cc), _switches(cc), note))
    return ts.decode_upload(_wav(0.9 * audio / np.abs(audio).max()))


def test_the_key_up_candidates() -> None:
    assert ts.gate_candidates(1.2, 1.5) == [0.6, 0.84, ts.HELD], "x1.4 and x2 sound held in a 1.5 s window"
    assert ts.gate_candidates(0.6, 1.5) == [0.3, 0.42, 0.84, 1.2, ts.HELD]
    assert ts.gate_candidates(0.12, 1.5)[0] == ts.GATE_MIN and ts.HELD not in ts.gate_candidates(ts.HELD, 1.5)
    assert ts.gate_words(0.42, 1.5) == "note held 0.4 s"
    assert ts.gate_words(ts.HELD, 1.5) == "note held to the end"


def test_the_noise_floor() -> None:
    sr = 16000
    rng = np.random.default_rng(0)
    note = np.concatenate([0.5 * np.sin(np.arange(8000) * 0.1), 1e-5 * rng.standard_normal(16000)])
    assert ts.noise_floor(note, sr) == pytest.approx(1e-5, rel=0.3), "the quietest frames: the floor"
    held = 0.5 * np.sin(np.arange(24000) * 0.1)
    assert ts.noise_floor(held, sr) == pytest.approx(0.5 * 1e-3, rel=0.05), "no floor: capped 60 dB down"
    assert ts.noise_floor(np.zeros(24000), sr) == 0.0 and ts.noise_floor(np.zeros(10), sr) == 0.0


def test_the_floor_lets_a_short_notes_truth_score() -> None:
    """A WAV target's tail sits at its noise floor; a render's falls silent. Matching the floor, the true
    settings score far better (the log spectrum no longer weighs the silence)."""
    target = _wav_target(SHORT, 0.35)
    k, _s = ts.cc_init(SHORT)
    budget = {**TINY_R6, "plain_starts": 0, "gate_scan": 0}

    def truth_loss(floor: int) -> float:
        run = ts.steps(target, [48], seeded=True, init_k=k, init_s=_switches(SHORT), gate_s=0.35,
                       budget={**budget, "floor_match": floor})
        first = next(run)
        return first.frame["loss"]

    assert truth_loss(1) < 0.5 * truth_loss(0)


def test_the_scan_finds_a_short_note() -> None:
    """The key goes up at 0.35 s and the search is not told: the length scan (before the first descent,
    on the plain starts) finds it, the first start says so, and the done frame reports it."""
    budget = {**TINY_R6, "restarts": 1, "plain_starts": 1, "gate_scan": 3, "floor_match": 1}
    frames = [s.frame for s in ts.steps(_wav_target(SHORT, 0.35), [48], seeded=True, budget=budget)]
    assert 0.25 <= frames[-1]["held"] <= 0.5
    first = next(f for f in frames if f["phase"] == "gd")
    assert first["trying"].endswith(ts.gate_words(frames[-1]["held"], ts.SEARCH_SECONDS))


def test_a_plans_key_up_reaches_the_run() -> None:
    """Plan.gate_s (the upload's key-up, when known) sets both twins' key-up; the done frame says it."""
    p = ts.Plan(_render(SQUARE), [48], True, "quick", gate_s=0.5)
    done = [s.frame for s in ts.run(p, {**TINY_R6, "plain_starts": 0, "restarts": 1})][-1]
    assert done["held"] == 0.5
    assert ts.Plan(_render(SQUARE), [48], True).gate_s is None, "unknown by default (the twin's 1.2 s)"
