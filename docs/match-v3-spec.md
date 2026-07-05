# match-v3 — from blind search to a digital twin

**Status:** ready to run. Supersedes `docs/match-v2-spec.md` (v2's analytic estimation
is folded in here as Phase B). Execute with `/goal implement @docs/match-v3-spec.md`.

## Why

The current matcher (`s1tui/match/`) blind-searches CC values with CMA-ES, probing the
real S-1 hundreds of times at ~2.3s each — 15-20 minutes per match, often not close.
Two root causes:

1. **Pitch mismatch.** It always probes a hardwired C3 (`match/driver.py:17`,
   `PROBE_NOTE = 48`) regardless of the target's pitch. Comparing a C3 candidate to a
   G4 target compares the wrong thing.
2. **It ignores the physics.** The S-1 is an SH-101 clone with a tiny, fully-known
   signal chain: one VCO (saw/square/sub/noise), one 4-pole filter, one ADSR, one LFO.
   A black-box optimizer is the wrong tool when the architecture is known.

The endgame is a **software model of the S-1** ("digital twin") calibrated to the real
hardware, so most of the search happens offline in milliseconds and the synth is only
touched for a short refinement pass. Beyond speed, the twin is the seed of a standalone
software synth — see repo `FABLE.md` and the project vision.

## Success criteria (the whole spec passes when)

On the Phase-0 corpus: **median wall-clock per match ≤ 4 min** (down from ~15-20) with
**closeness ≥ the current engine's best-ever** on the same targets, and pitch-correct
probing eliminates the C3 failure mode. Every phase lands behind green tests
(238 pass today — keep them green) and logs probe count, cache hits, wall-clock, and
closeness so improvement is measured, not asserted.

## S-1 signal chain (the model we're fitting) — CCs from `schema.py`

- **Oscillator:** Range/octave (14), Fine Tune (76), Saw Level (20), Square Level (19),
  Pulse Width (15), PWM Source (16), Sub Level (21), Sub Octave Type (22),
  Noise Level (23), LFO Pitch (13).
- **Filter (4-pole LP):** Frequency/cutoff (74), Resonance (71), Env Depth (24),
  LFO Depth (25), Key Follow (26).
- **Envelope (ADSR):** Attack (73), Decay (75), Sustain (30), Release (72),
  Amp Env Mode (28: Gate/Env).
- **LFO:** Rate (3), Waveform (12), Mod Depth (17).
- **Effects (leave OUT of matching v1):** Delay (90/92), Reverb (89/91).

`schema.py` is the source of truth — read CC numbers/labels from it, don't hardcode.

---

## Phase 0 — measurement harness (do this first; nothing improves without it)

You cannot tune what you can't measure, and you can't run the S-1 in CI. Build the
scaffolding that lets every later phase report a number.

**Files:** new `s1tui/match/corpus.py`, new `tests/test_match_corpus.py`, new
`docs/match-benchmarks.md` (results log).

**Changes:**
- A `Target` record: audio clip + true f0 + (optional) ground-truth CC vector.
- A **synthetic corpus generator**: for a set of known CC vectors, render/capture the
  S-1's output once and cache the clips under `~/.s1tui/corpus/` so later runs score
  against fixed audio with *no* hardware. Ship a small committed corpus of
  feature-only fixtures (no large WAVs in git) so CI can run the scoring math.
- A `benchmark(matcher, corpus) -> {median_seconds, mean_closeness, probe_count,
  cache_hits}` function that every phase calls.

**Verify:** `pytest tests/test_match_corpus.py` passes offline (no device); `benchmark`
returns the four metrics on the committed fixtures. Record the *current* engine's
numbers in `docs/match-benchmarks.md` as the baseline to beat.

---

## Phase A — pitch-aware probing + segmentation (biggest single accuracy win)

**Files:** new `s1tui/match/analyze.py` (start it here), `match/driver.py`,
`match/session.py`, `tests/test_match_engine.py`.

**Changes:**
- In `analyze.py`, detect the target's fundamental with YIN/pYIN (scipy-only if
  feasible; else add a light dependency behind `[studio]`). Return a MIDI note number.
- Thread that note through so `driver.py` probes **at the target's pitch** instead of
  the hardwired `PROBE_NOTE = 48`. Keep C3 as the fallback when f0 is unvoiced/noisy.
- Segment the target amplitude envelope into attack / sustain / release regions;
  expose them for later phases.

**Verify:** on the Phase-0 corpus, `benchmark` mean closeness beats the C3-only baseline
by a clear margin on non-C3 targets; a regression test asserts a G4 target now probes
G4, not C3.

---

## Phase B — analytic estimation (zero-probe CC estimate)

Derive a full CC guess from the target audio alone, before touching the hardware. This
is the old v2 spec, made concrete.

**Files:** `s1tui/match/analyze.py` (extend), new `s1tui/match/estimate.py`,
`tests/test_estimate.py`.

**Changes — analyze the target:**
- **Harmonic decomposition:** amplitudes of harmonics relative to f0 over the sustain
  window.
- **Spectral envelope + knee:** find the filter cutoff as the spectral roll-off knee;
  estimate resonance from peakiness at the knee.
- **ADSR:** fit attack/decay/sustain/release times and level from the amplitude
  envelope (Phase-A segments).
- **Modulation:** detect vibrato (pitch LFO) and tremolo/filter wobble (LFO rate/depth)
  from periodicity in the f0 and amplitude tracks.

**Changes — map analysis → CCs (`estimate.py`):**
- **Osc mix via NNLS:** solve a non-negative least-squares fit of {saw, square, sub,
  noise} harmonic templates to the observed harmonic profile → Saw/Square/Sub/Noise
  Level CCs.
- **Filter:** map cutoff-knee frequency → CC74, resonance peakiness → CC71 (calibrate
  the Hz↔CC curve against a few probes, cache it).
- **Envelope:** map fitted ADSR seconds → CC73/75/30/72; pick Amp Env Mode (28).
- **LFO:** map detected rate/depth → CC3/12/17 (+ CC13 for pitch vibrato).

**Verify:** on synthetic S-1 targets, **estimate-only** closeness is already usable
(target ≥ ~70) with **zero hardware probes**. `test_estimate.py` covers NNLS osc mix
and the ADSR/filter mappings on synthetic inputs.

---

## Phase C — the digital twin (offline search)

A cheap forward model `twin(cc_vector, note) -> features` calibrated to the real S-1, so
the optimizer searches the *model* instead of the synth.

**Files:** new `s1tui/match/twin.py`, `match/session.py` (new "twin-first" mode),
`match/optimizer.py`, `tests/test_twin.py`.

**Changes:**
- Implement a lightweight SH-101-style renderer: band-limited saw/square/sub + noise →
  4-pole ladder filter → ADSR VCA → LFO modulation. Output the same feature vector
  `features.py` produces, so twin and hardware are directly comparable.
- **Calibrate** the twin against a batch of real probes (reuse the Phase-0 corpus):
  fit the twin's free constants (filter Hz↔CC curve, env time↔CC curve, osc levels) so
  `twin(cc)` ≈ real `S-1(cc)` in feature space. Cache calibration under `~/.s1tui/`.
- New match flow: analyze (A) → estimate (B) → **descend the twin to the target**
  (gradient or CMA over the model, thousands of cheap evals) → **short hardware
  refinement** seeded at the twin's answer to close the sim-to-real gap, with early
  stop when closeness plateaus.

**Verify:** on the Phase-0 corpus, **median wall-clock ≤ 4 min** with **closeness ≥ the
current engine's best-ever**, and hardware probe count down by an order of magnitude
vs. the baseline. `test_twin.py` asserts twin↔hardware feature agreement within a
tolerance on the calibration set (and runs the DSP path with no device).

---

## Phase D — the magic (optional, do after A-C land and pass)

Each item is independently shippable; keep them optional per "exactly as written."

- **Match from a stem:** source-separate a messy clip before analysis so matching works
  on real-song material, not just clean single notes.
- **Text nudges:** "brighter" / "fatter" / "shorter" as post-match offsets on top of a
  match (map adjectives → CC deltas).
- **Auto-save:** write the matched patch straight into the bank (`patches.py`) with a
  generated name, so a match ends as a loadable preset.

**Verify:** an end-to-end demo from a real song stem to a patch loadable in the TUI/web.

---

## Guardrails

- **Keep 238 tests green.** Add tests with every phase; the matcher's "it works" must be
  a measured number, not vibes.
- **Report deltas.** Every phase updates `docs/match-benchmarks.md` with before/after
  probes, seconds, and closeness.
- **No silent caps.** If you sample, early-stop, or bound anything, `log()` it.
- **Effects (delay/reverb) stay out** of matching v1 — they blow up the search space;
  revisit only after A-C.
- **Twin is a means and an end.** Build `twin.py` clean enough to later stand alone as a
  playable software synth, not just an optimizer inner loop.
