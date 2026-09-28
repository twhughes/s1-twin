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

## Round 4: the search tries harder (2026-09-28)

Tyler: *"i wish also the optimization tried harder. like do many iterations. i feel it gives up
too easily. especially on the discrete options."* He was right on both counts. Round 3's search ran
every descent under the default switches (Sub octave −1, LFO wave Triangle, Volume shape Envelope)
and scored the other switch settings once at the end, at knobs tuned for the defaults, with no
re-descent. A Gate or a −2 sub was nearly out of reach. Each start also ran a fixed number of steps
at a fixed rate.

What changed (`synth/match/twin_session.py`, `steps()`):

- **Descents run until they stop improving**: no relative gain above a tolerance over a patience
  window, after a minimum, with a hard cap; the rate falls on a cosine from 0.08 to 0.008.
- **Switches with re-descent.** After each start, every switch setting is scored at that start's
  best knobs (renders only). The most promising are re-descended briefly (Quick 1, Thorough 3,
  Deep all), a winner is kept, and the descent continues under it. Their frames say what they try
  (`"trying": "Volume shape: Gate"`), and the Match view shows it.
- **An LFO scan.** The LFO wave only shows near the right rate, which a descent from a random
  start rarely finds: in round 3 it turned the LFO into a fixed pitch offset (rate 0) or dropped
  it. So the same stage scores each wave the matcher tries at 9 rates, on the pitch or on the
  filter (54 renders, about 1 s), and re-descends the best (Deep: the best on each).
- **Gentle starts.** Adam's first step moves every knob by about the rate, so the descents that
  start from good knobs (the trials, the continuation, the polish) use lower rates and an 8-step
  warmup. Without it, a trial from the scan's LFO lost its start and stopped before recovering.
- **A final polish** at small steps from the overall best.
- **Deep**, a new preset: 8 starts, every switch setting, two LFO trials, a longer cap.
- **Stop means finish**: the text message `"finish"` on `/ws/match` ends the search at the next
  step, and the done frame still comes (closeness, A/B), marked `"finished": true`.

The benchmark (`tools/match_benchmark.py`): five targets made by the twin, so the truth is known,
matched through the upload path (WAV → `plan()` → `run()`) with the note given. "Old" is round 3's
Thorough, run by the same code under a budget without the round-4 keys; it is frame-for-frame the
old search (checked against the round-3 file). Settings are scored by the Match view's own report
(`views/match.js` `recoveryReport`, through node). "Within 10" counts the settings that shape the
true sound, plus the LFO settings when the found patch adds an LFO the true sound lacks (a real
miss), so the count can differ between two runs on one target. "Switches back" means every switch
that shapes the sound came back.

| target | what it tests | search | closeness | switches back | settings within 10 | steps | seconds |
|---|---|---|---|---|---|---|---|
| gate | Volume shape Gate, with a filter envelope | old | 66.6 | yes | 13 of 17 | 659 | 55 |
| | | **thorough** | **73.0** | **yes** | **14 of 14** | 1843 | 140 |
| sub | Sub octave −2 asym, Sub up | old | 59.4 | yes | 10 of 18 | 659 | 49 |
| | | **thorough** | **72.6** | **yes** | **15 of 15** | 1473 | 94 |
| vibrato | LFO wave Square, vibrato | old | 37.1 | no | 9 of 17 | 659 | 45 |
| | | **thorough** | **60.3** | **yes** | **14 of 17** | 1434 | 83 |
| | | deep | 63.2 | yes | 13 of 17 | 4207 | 259 |
| wobble | LFO wave Saw on the filter, Sub octave −2 | old | 52.9 | no | 9 of 19 | 659 | 65 |
| | | **thorough** | **54.4** | **yes** | **12 of 19** | 1499 | 126 |
| | | deep | 68.3 | yes | 18 of 19 | 4975 | 440 |
| pluck | an ordinary pluck, default switches | old | 61.4 | yes | 13 of 17 | 659 | 59 |
| | | **thorough** | **72.6** | **yes** | **14 of 14** | 1510 | 117 |

- **Thorough got better, not just longer.** Closeness rose on all five (mean 55.5 → 66.6). The
  switches came back on all five (round 3: three), and the share of settings back within 10 rose
  on every target. It took 1.4 to 2.3 minutes here, 1.8 to 2.5 times as long as round 3.
- **The LFO targets needed the scan.** Before it, the first round-4 Thorough still missed both LFO
  waves (vibrato 54.2, wobble 49.2, worse than round 3's 52.9); with the scan and the gentle
  starts, both waves came back.
- **Deep pays off on the hardest target**: wobble 54.4 → 68.3 and 18 of 19 settings back, in about
  7 minutes. On vibrato, it gains closeness (63.2) but not settings: Fine tune and the vibrato's
  depth still trade against each other.
- **Still hard**: the pitch settings. The log-mel loss is coarse at low pitches, so Fine tune is
  often far off (vibrato, wobble), and the vibrato's rate is only found when the scan lands near it.
- Seconds are wall-clock on a busy laptop (load average 15 to 60), so compare them within the
  table only. The two caveats at the top of this file still hold: closeness is not yet checked by
  ear, and there is no real S-1 in these runs.
