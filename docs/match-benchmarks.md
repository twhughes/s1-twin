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


## The reproduction suite, and Tyler's own takes (2026-09-28, evening)

`tools/match_suite.py`: 12 twin-made cases and 6 recorded copies (a small speaker, a room, 48 kHz, −18 dB,
2 s of room noise before the note and 1.5 s after, −60 dBFS noise under it), the notes given, scored with
the Match view's own report. The baseline (`docs/match-baseline.json`) is the Thorough column below.

| case | Quick, before round 5 | Thorough, now (baseline) | settings within 10, now |
|---|---|---|---|
| square | 84.3 | 89.8 | 14 of 14 |
| saw | 80.5 | 81.6 | 13 of 13 |
| gate | 72.6 | 74.4 | 13 of 14 |
| sub | 61.3 | 78.4 | 15 of 15 |
| vibrato | 58.9 | 62.5 | 12 of 17 (LFO wave missed) |
| wobble | 55.7 | 42.4 | 18 of 19 (closeness now spans the whole release: an LFO a hair off drifts out of phase) |
| pluck | 40.2 | 85.9 | 16 of 17 |
| bass | 35.8 | 39.7 | 10 of 17 |
| high | 31.5 | 61.9 | 10 of 14 |
| pad | 54.6 | 89.5 | 11 of 14 |
| short | 25.8 | 34.9 | 12 of 17 |
| chord | 77.7 | 78.4 | 14 of 14 |
| square@rec | 8.3 | 38.7 | 9 of 14 |
| gate@rec | 8.3 | 25.2 | 5 of 14 |
| pluck@rec | 8.3 | 37.8 | 6 of 14 |
| bass@rec | 2.8 | 9.3 | 7 of 17 |
| pad@rec | 5.1 | 20.0 | 13 of 17 |
| short@rec | 8.2 | 28.8 | 7 of 14 |

Tyler's six real takes (sung and whistled, kept only on his machine), cold start as he used them, Thorough:
his runs detected clusters (C4+C#4+D4+E4, C#3+D3+D#3, E6+F6+F#6+G6, C3+C#3) or the whistles' D#6 and F6; now
every take is one correct note. Closeness: 0.4 → 22.0 (a C#3 vowel), 0.9 → 6.4 (a high glide), 9.9 → 11.5,
10.6 → 10.9, 26.8 → 27.7 and 20.0 → 18.1 (whistles). Vowels stay low because the S-1's one filter cannot make
two resonances; the whistles score badly even from a hand-made pure tone (loss 12.96), so something else in
those takes dominates: the next thing to look at.

## Rounds 12 and 14: is the search the limit, or the loss? (2026-09-29)

Tyler: "u sure this is the best algorithm for optimization?", then "yea 1,2 please": (1) run the starts at once
on every core, (2) a global phase (CMA-ES) before the gradient descent.

**First: where can any search gain at all?** For each suite case, the search loss of the true patch, of the
true patch after 80 local Adam steps (the bottom of its basin), and of the patch Thorough found (the baseline
run). A truth basin lower than the found patch is a search failure; a found patch as low or lower means the
loss itself prefers the wrong patch, and no optimizer can fix that.

| case | truth | truth, polished | found | verdict |
|---|---|---|---|---|
| square | 0.426 | 0.426 | 0.438 | tie |
| saw | 0.236 | 0.212 | 0.211 | tie |
| gate | 0.378 | 0.374 | 0.400 | search missed (a little) |
| sub | 0.325 | 0.322 | 0.332 | search missed (a little) |
| vibrato | 0.343 | 0.343 | 1.825 | **search missed** |
| wobble | 0.394 | 0.394 | 1.457 | **search missed** |
| pluck | 0.336 | 0.332 | 0.357 | search missed (a little) |
| bass | 4.325 | 3.215 | 3.247 | tie |
| high | 0.966 | 0.870 | 1.526 | **search missed** |
| pad | 1.010 | 0.810 | 0.809 | tie |
| short | 1.680 | 1.587 | 1.674 | search missed (a little) |
| chord | 0.135 | 0.129 | 0.110 | the loss prefers the found patch |
| square@rec | 3.496 | 1.626 | 1.513 | the loss prefers the found patch |
| gate@rec | 5.466 | 2.749 | 2.654 | the loss prefers the found patch |
| pluck@rec | 4.531 | 1.483 | 1.479 | tie |
| bass@rec | 10.754 | 1.781 | 1.027 | the loss prefers the found patch |
| pad@rec | 2.223 | 1.346 | 0.893 | the loss prefers the found patch |
| short@rec | 1.966 | 1.182 | 1.182 | tie |

So on the recorded copies no search could help: the loss scored a patch that imitates the room better than
the true one. Split by term, the log-mel term carried it (square@rec 2.38 true against 0.96 found).

**The race (round 12),** on the seven cases where the search can gain, the notes given, closeness. A is
Thorough; B its starts at once (14 lanes on 7 cores); C a CMA-ES phase per promising switch setting, then the
descents from its best 2; C-freeze leaves LFO rate and fine tune to CMA-ES; D mixes B and C. Each took 3 to 4x
A's steps. The times are not comparable: other apps held this Mac at load 20 to 150 during the race.

| case | A | B | C | C-freeze | D | A, floor | B, floor |
|---|---|---|---|---|---|---|---|
| vibrato | 62.5 | 58.0 | 59.2 | 60.1 | 43.9 | 80.5 | 80.5 |
| wobble | 42.4 | 63.8 | 54.3 | 54.1 | 54.4 | 70.6 | 54.7 |
| high | 61.9 | 74.9 | 61.0 | 62.0 | 60.6 | 54.7 | 78.0 |
| gate | 74.4 | 74.7 | 74.2 | 74.6 | 74.7 | 74.6 | 74.7 |
| sub | 78.4 | 78.4 | 78.6 | 78.4 | 78.4 | 76.6 | 78.5 |
| pluck | 85.9 | 85.9 | 70.7 | 70.4 | 85.9 | 86.2 | 86.2 |
| short | 34.9 | 34.9 | 40.8 | 34.1 | 34.4 | 71.1 | 71.1 |
| mean | 62.9 | 67.2 | 62.7 | 61.9 | 61.8 | 73.5 | 74.8 |

CMA-ES brought nothing. Starts at once helped a little (+4.3; +1.3 once the loss was fixed) for 3x the compute
on every core, and it traded cases (high up, wobble down). Neither is merged; the contenders are kept on the
branch `r12/global` (`synth/match/pool.py`, `twin_session.RACE_BUDGETS`).

**The loss was the bigger limit (round 14).** The log-mel and log-STFT terms looked about 100 dB below each
spectrogram's peak. White noise 70 dB under a tone alone scored 7.4, more than two different patches usually
differ by; resampling residue, room noise and echo tails counted like the note. `twin.LOSS_FLOOR_DB = 50`
clamps every cell 50 dB below its spectrogram's peak (that noise now scores 0.0001; noise 20 dB under the
tone still scores 2.0). Quick first (closeness; mean over the 12 clean cases / the 6 recorded ones):

| Quick | no floor | 60 dB | 50 dB |
|---|---|---|---|
| clean, closeness | 62.5 | 70.5 | 70.8 |
| recorded, closeness | 27.0 | 14.4 | 15.6 |
| recorded, settings back within 10 | 43 of 93 | 48 of 82 | 58 of 84 |

Then Thorough, 50 dB, against the baseline (the new `docs/match-baseline.json`):

| case | baseline | floor 50 dB |
|---|---|---|
| square | 89.8, 14 of 14 | 89.7, 14 of 14 |
| saw | 81.6, 13 of 13 | 81.8, 13 of 13 |
| gate | 74.4, 13 of 14 | 74.6, 14 of 14 |
| sub | 78.4, 15 of 15 | 76.6, 14 of 15 |
| vibrato | 62.5, 12 of 17 | **80.5, 17 of 17** |
| wobble | 42.4, 18 of 19 | **70.6, 19 of 19** |
| pluck | 85.9, 16 of 17 | 86.2, 14 of 14 |
| bass | 39.7, 10 of 17 | 35.4, 8 of 14 |
| high | 61.9, 10 of 14 | 54.7, 11 of 14 (Volume shape lost: Gate plus a filter envelope) |
| pad | 89.5, 11 of 14 | **95.6, 14 of 14** |
| short | 34.9, 12 of 17 | **71.1, 14 of 14** |
| chord | 78.4, 14 of 14 | 78.5, 14 of 14 |
| square@rec | 38.7, 9 of 14 | 15.0, 9 of 14 |
| gate@rec | 25.2, 5 of 14 | 19.0, **10 of 14** |
| pluck@rec | 37.8, 6 of 14 | 14.5, **10 of 14** |
| bass@rec | 9.3, 7 of 17 | 12.3, 5 of 17 |
| pad@rec | 20.0, 13 of 17 | 18.0, 13 of 14 |
| short@rec | 28.8, 7 of 14 | 14.1, **11 of 14** |

Clean: closeness 68.3 → 74.6%, settings back 86 → 94%. Recorded: settings back 52 → 68%, while the closeness
number falls 26.6 → 15.5%: closeness (the plain metric) still looks 80 dB down, so it still rewards a patch
that imitates the room's echo and noise, which the S-1 would add again when played in that room. Left open:
"high" (the fake-gate basin; starts at once find it), "bass" (a small speaker's low cut removes the note
itself), and whether the closeness number should get the same 50 dB range.

Tyler's six takes, cold, Thorough, closeness (the old measure), old loss → floor: 10.9 → 12.8 (B3), 27.7 → 23.6
and 18.1 → 10.7 (whistles), 11.5 → 5.7, 6.4 → 5.7 and 22.0 → 11.3 (voice); every take kept its note. A voice
has no true patch to count settings against, so his ears decide: the A/B files are local only.
