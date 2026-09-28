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

## Round 6: plain starts, a mild prior on the extras, the note's length (2026-09-28)

The problem: the search still ended in "invented extras" basins. Noise, Sub, Vibrato or an LFO amount
sat half up while the true sound had none. Every start began in the middle of the knob cube or at a
random point in it, where each extra is half up. Two examples from the before runs (CC values, 0 to
127): gate (Thorough, seed 0) set Sub to 124, Noise to 64 and Vibrato to 121; pluck (Quick) turned a
square into a full sub (Square 0, Sub 127) with Noise 21.

What changed (`synth/match/twin_session.py`, `steps()`). A budget without the round-6 keys still runs
a00dd4c's search exactly (checked frame for frame, seeded and cold).

- **Plain starts.** Before any descent, 40 plain patches are scored at the target, with renders only:
  five oscillator mixes (saw, square, saw and square, saw and sub, square and sub), the filter open or
  half open, the filter envelope off or mid, and the volume held or short. Noise, vibrato, the LFO
  amounts and the LFO depth are 0 in all; the sub is up only in the two sub mixes. Quick starts from
  the best one, Thorough from the best 4 and Deep from the best 8, each with a different mix or
  filter. Their frames say which (`"trying": "Square, filter open"`). "Start from: Current knobs"
  still starts at the knobs, and the plain starts follow (scored under the knobs' switches). Random
  starts fill any rest, and they draw the extras near 0.
- **A mild prior on the extras**: λ × (the levels of Noise, Sub, Vibrato, LFO amount and LFO depth,
  in k space), in the search loss only. λ = 0.02 (tuning below). The done frame's closeness is the
  plain metric, as before.
- **Seeds.** `steps(seed=…)` and `run(seed=…)` vary the random starts only; seed 0 (or none) gives
  the old draws. The round-6 presets start only from plain starts, so every seed gives the same run
  (checked frame for frame, seeds 0, 1 and 2): the new search is deterministic, and its rows below are
  one run each.
- **The note's length.** `steps(gate_s=…)` and `Plan.gate_s` set the key-up time: default 1.2 s, the
  twin's own; a time past the render's end means the note is held through it. The run also scans it.
  Before the first descent, it scores the key-up at ×0.5, ×0.7, ×1.4, ×2 and held on the 6 best
  plain starts, and steps on while one wins (up to 3 rounds). A winner rebuilds both twins, and the
  plain starts are scored again under it. After the first start's descent, the same scan runs at its
  knobs, and a winner there gets a short re-descent. Frames say `"trying": "note held 0.4 s"`; the
  done frame adds `"held"` (seconds), and its A/B audio uses that key-up.
- **The target's noise floor** (a finding). A 16-bit WAV's tail sits at its noise floor, about −96 dB,
  while a render falls to true silence. On the log spectrum, that gap outweighed the note: the short
  target's true settings scored 3.33, against 0.03 without the WAV step. So the search adds a fixed,
  quiet noise at the target's floor (its quietest 20 ms, at most 60 dB under its peak) to every
  candidate. The true settings' loss fell on all seven targets: short 3.33 → 0.92, gate 0.83 → 0.38,
  wobble 1.19 → 0.36, sub 0.73 → 0.31, pluck 0.70 → 0.43, square 0.45 → 0.42, vibrato 0.37 → 0.32.
  The done frame's closeness does not use it.

Why the length scan also runs before the first descent (the brief asked for it after): a descent
under the wrong length fakes the short note with the envelope (a fast decay), and at those knobs the
length hardly shows. With the scan after the descent only, Quick on the short target picked "held
through" (closeness 15 to 21). On the plain starts, the scan finds 0.42 s (the truth: 0.35 s).

The benchmark (`tools/match_benchmark.py`) adds two targets: `square` (the S-1's default patch) and
`short` (an ordinary held patch played short: the key up at 0.35 s, which the search is not told).
"Before" is a00dd4c's search, run by the same code under a budget without the round-6 keys: 3 seeds
per target for Thorough (mean and min), 1 for Quick. "After" is one run per target, the same for
every seed. An **invented extra** is a Sub, Noise, Vibrato or LFO amount that the true sound lacks,
found at CC 20 or more.

Thorough (before: 3 seeds, mean and min; after: one run, the same for every seed):

| target | what it tests | closeness before | after | settings within 10 before | after | switches back before | after | invented extras before | after |
|---|---|---|---|---|---|---|---|---|---|
| gate | Volume shape Gate, a filter envelope | 69.9 (62.5) | 68.2 | 76% (47%) | 71% | 3 of 3 | yes | 3, in 1 of 3 runs | 0 |
| sub | Sub octave −2 asym, Sub up | 75.4 (75.0) | 78.5 | 100% (100%) | 100% | 3 of 3 | yes | 0 | 0 |
| vibrato | LFO wave Square, vibrato | 64.0 (60.9) | 76.4 | 82% (76%) | 94% | 3 of 3 | yes | 0 | 0 |
| wobble | LFO wave Saw on the filter, Sub octave −2 | 59.3 (58.6) | 69.2 | 65% (58%) | 95% | 0 of 3 | yes | 3, in 3 of 3 runs | 0 |
| pluck | an ordinary pluck | 68.4 (64.3) | 86.1 | 82% (82%) | 100% | 3 of 3 | yes | 1, in 1 of 3 runs | 0 |
| square | the S-1's default patch | 85.6 (84.8) | 89.5 | 82% (76%) | 100% | 3 of 3 | yes | 2, in 2 of 3 runs | 0 |
| short | the key up at 0.35 s, not told | 13.8 (11.5) | 51.0 | 18% (12%) | 65% | 3 of 3 | yes | 10, in 3 of 3 runs | 1 |
| **all seven** | | **62.3** (worst 11.5) | **74.1** (worst 51.0) | **72%** | **89%** | **18 of 21** | **7 of 7** | **19, in 10 of 21 runs** | **1** |

Quick (one seed):

| target | closeness before | after | settings within 10 before | after | switches back before | after | invented extras before | after |
|---|---|---|---|---|---|---|---|---|
| gate | 72.6 | 62.7 | 93% | 50% | yes | yes | 0 | 1 |
| sub | 61.3 | 66.6 | 67% | 67% | no | yes | 0 | 0 |
| vibrato | 58.9 | 66.2 | 65% | 82% | no | yes | 0 | 0 |
| wobble | 55.7 | 62.9 | 53% | 74% | no | no | 2 | 0 |
| pluck | 40.2 | 84.2 | 57% | 93% | yes | yes | 2 | 0 |
| square | 84.3 | 88.7 | 76% | 100% | yes | yes | 1 | 0 |
| short | 12.1 | 45.0 | 24% | 71% | yes | yes | 3 | 0 |
| **all seven** | **55.0** | **68.1** | **62%** | **77%** | **4 of 7** | **6 of 7** | **8** | **1** |

Tuning λ (Thorough, everything else as above; closeness, then settings within 10):

| target | λ = 0 | λ = 0.02 |
|---|---|---|
| gate | 74.5 (93%) | 68.2 (71%) |
| sub | 78.4 (100%) | 78.5 (100%) |
| vibrato | 59.4 (71%) | 76.4 (94%) |
| wobble | 70.6 (100%) | 69.2 (95%) |
| pluck | 85.9 (88%) | 86.1 (100%) |
| square | 89.3 (100%) | 89.5 (100%) |
| short | 51.2 (65%) | 51.0 (65%) |
| **mean** | **72.8 (88%)** | **74.1 (89%)** |

λ = 0.02 won on the mean, mostly on vibrato: without the prior, the descent traded the vibrato
amount (35 → 13) for Fine tune (+61). It cost gate 6 points (see below). A larger λ hurt the sub
target in an earlier sweep, before the floor match: λ = 0.05 and 0.15 took its closeness from 75.4 to
61.4 and 63.4, and at 0.15 its mix flipped (Square 90 → 0, Saw 0 → 112) and its Sub dropped. So λ
stays mild: a real vibrato still scores better with its LFO at 4 × this λ
(`tests/test_match_search.py`).

- **The invented extras are gone.** Thorough: 19 invented extras in 10 of 21 runs before, 1 after.
  Quick: 8 in 4 of 7 runs before, 1 after. The one left in Thorough is on the short target: a full
  LFO on the filter (LFO amount 127). It fills what the found key-up (0.42 s; the truth: 0.35 s)
  leaves; the prior is too mild to stop it.
- **Thorough got better on six of seven targets**: mean closeness 62.3 → 74.1, settings within 10
  72% → 89%, and every switch came back (the wobble's Sub octave −2 did not in any before run).
  Without the new short target: 70.4 → 78.0.
- **Quick gained most**: pluck 40.2 → 84.2 (before, it made the square a full sub with noise), short
  12.1 → 45.0, mean 55.0 → 68.1.
- **Gate lost ground**: Quick 72.6 → 62.7, Thorough 68.2 (before: 69.9 mean, 62.5 min). The cause:
  every plain start has Volume shape Envelope, and Envelope makes a gate-like volume only with Sustain
  full, which leaves the filter envelope nothing to do. So the descent lands at Sustain 126 to 127,
  Decay 0 and Env amount 0 to 13 (CC values: a "fake gate"). Gate at those knobs sounds the same as Envelope, so
  the Gate trial cannot find the filter sweep. The basin comes from the plain starts: Quick at λ = 0
  also fell into it (67.9, Volume shape Envelope, Sustain 127). Thorough escaped it at λ = 0 (74.5),
  but not at λ = 0.02. A fix to try next: Gate-family plain starts, chosen by a short descent from
  each family. Scoring alone misleads here: the sub target's best Gate start scores 2.03 and its best
  Envelope start 2.87, but the true sub is Envelope.
- **Deterministic now.** "Before" varied by seed: pluck 64.3 to 76.5, gate 62.5 to 74.2. The new
  presets give one answer per target and budget.
- **Time**: after Thorough took 1163 to 1718 steps (before: 1215 to 1632), after Quick 246 to 359
  (before: 176 to 279; the plain scoring and the length scan add 60 or more renders). Seconds are
  wall-clock at load averages of 10 to 320 from other work on this laptop, so they are not compared
  here.
- The two caveats at the top of this file still hold: closeness is not yet checked by ear, and there
  is no real S-1 in these runs.
