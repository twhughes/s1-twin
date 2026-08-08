# match benchmarks — the results log

The Phase-0 harness (`synth/match/corpus.py`, `benchmark()`) returns four metrics —
`median_seconds`, `mean_closeness`, `probe_count`, `cache_hits` — so every phase is a
number, not a vibe. This file logs them.

**Two honesty caveats, always in force (FABLE):**

1. **`closeness` is measured, not perceptually validated.** It is the existing
   `distance.closeness` metric. Nobody has confirmed it tracks Tyler's ears yet — that
   is a separate human ceremony (play pairs, ask which is closer). Do not read a high
   closeness as "sounds the same to Tyler".
2. **No real S-1 in these runs.** The corpus audio is either the labeled *placeholder*
   renderer (a crude, different synth) or the *twin itself*. Real-hardware numbers
   replace these after the calibration/ears session.

## Digital twin — twin-guided match (2026-08-08)

`TwinMatcher` gradient-descends the twin's k-vector to the target with an autograd
differentiable loss, enumerating discrete s-configs; it touches **zero hardware**
(`probe_count = 0`). Defaults: search at 16 kHz / 1.5 s, 160 Adam iters.

| corpus | mean closeness | median s/match | probes |
|---|---|---|---|
| twin-native (self-consistent) | **77.0** | 23.1 | 0 |
| Phase-0 placeholder | **51.2** | 22.5 | 0 |

Placeholder per-target: bright_saw_c3 **51.0** · dark_square_g4 **60.0** ·
sub_heavy_c2 **71.3** · noisy_a3 **22.4**.

- **The placeholder gap is real and expected.** The placeholder is a naive
  scipy-saw + 2-pole-butterworth + linear-ADSR synth; the twin models a *different*
  (band-limited, 4-pole-ladder, analog-ADSR) instrument. `twin@ground-truth-CC` scores
  only ~44 against the placeholder — so ~50 is near the achievable ceiling there, not a
  search failure. The twin-native number (77) is the search's true quality once target
  and model are the same instrument.
- **Speed:** median ~23 s per match, all offline — already far under FABLE's 4-minute
  bar, before any of the analytic-seed (Phase C) work. The old CMA-ES engine took
  15-20 min hitting real hardware ~500× per match.

**Gradcheck** (the correctness proof, `tests/test_twin.py`): the twin's
`k → render → differentiable-loss` gradient agrees with central finite differences to
**max rel-err ≈ 3e-8** (bar: < 1e-3), exercising the time-varying-filter + LFO path.

### Not yet measured (needs the hardware/ears session)
- twin-vs-**real-S-1** feature error on held-out probes (the sim-to-real gap).
- metric-vs-ears agreement (is `closeness` perceptually right at all).
- whether an analytic seed (Phase C) beats twin-only search — build only if it wins.
