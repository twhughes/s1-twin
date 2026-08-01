# Match Engine v2 — Direct Spectral Estimation

Why the current engine is slow and inaccurate, and a redesign that reads most
parameters straight off the target spectrum instead of guessing them through
hardware probes.

## Diagnosis

**Slow:** every candidate costs real time — CC settle (0.05 s) + hold (1.2 s) +
tail (1.0 s) ≈ 2.3 s. CMA-ES at dim ≈ 30 defaults to popsize 14, so 40
iterations ≈ 560 probes ≈ **20+ minutes** of hardware time per match.

**Inaccurate — three root causes, in order of impact:**

1. **Pitch mismatch.** The probe note is hardwired to C3 (`driver.py:PROBE_NOTE`)
   and all pitch parameters are excluded from the search space (`space.py`).
   If the target sound sits at any other pitch, its log-mel spectrum can never
   align with the candidate's, so the optimizer chases an unfixable error with
   timbre knobs. This is likely the single biggest reason results "don't work".
2. **Black-box search over a known instrument.** The S-1 is an SH-101-style
   architecture: saw + square(+PW) + sub + noise → 4-pole LPF → ADSR VCA, with
   one LFO. Its parameters have *direct, readable signatures in the spectrum* —
   searching blindly over 30 dimensions throws that structure away.
3. **Everything rides on one noisy 2.3 s capture per candidate** with a fixed
   analysis window regardless of what the target actually looks like.

## Architecture: estimate → seed → refine

Replace "CMA from schema defaults" with a three-stage pipeline. Stages 1–2 are
pure DSP on the target clip (milliseconds, no hardware). Stage 3 is a *short*
hardware polish around the analytic seed.

### Stage 1 — target analysis (new module `synth/match/analyze.py`)

All computed once from the target clip:

- **Pitch (f0):** autocorrelation / YIN over the sustain segment → f0 in Hz →
  nearest MIDI note. Also detect "no stable pitch" (noise/percussive targets).
- **Segmentation:** onset (existing `find_onset`), peak, sustain plateau,
  release tail — from the RMS envelope. Note the target's usable duration.
- **Harmonic decomposition (sustain segment):** measure amplitudes of the
  first ~24 harmonics of f0 plus the inter-harmonic noise floor.
- **Spectral envelope:** smooth dB envelope over frequency for the sustain
  segment (for filter fitting).
- **Amplitude envelope:** the existing per-frame RMS, in seconds.
- **Modulation:** FFT of the f0 track (vibrato → LFO rate/depth on pitch) and
  of the RMS envelope (tremolo/PWM wobble).

### Stage 2 — analytic parameter estimation (same module)

Map analysis → CC estimates using the S-1's known structure:

- **Probe note = detected pitch.** Choose the nearest MIDI note; use Range
  (16'/8'/4'/2', CC 14) to get f0 into the playable region if needed. THE
  probe note becomes per-session, not the C3 constant. For unpitched targets,
  keep C3 and rely on Stage 3.
- **Oscillator mix:** nonnegative least squares of the target's harmonic
  amplitudes against four basis spectra — saw (1/k), square (1/k odd, with
  pulse-width skew for even-harmonic content), sub (−1 octave series), noise
  (flat floor). Yields Saw/Square/Sub/Noise levels (CCs 20/19/21/23) and Pulse
  Width (CC 15) directly.
- **Filter cutoff + resonance:** fit a −24 dB/oct knee to the spectral
  envelope; knee frequency → Frequency (CC 74) via an empirical Hz→CC curve,
  peak height above the fit at the knee → Resonance (CC 71).
- **Amp ADSR:** attack = onset→peak time; sustain = plateau level; decay =
  peak→plateau time; release = tail time constant after note-off (when the
  target contains one). Map seconds→CC with an approximate exponential curve
  (calibrate the curve once against the hardware — see Stage 3 note). Set Amp
  Env Mode (CC 28) to Gate when attack≈0, decay≈0, sustain≈full.
- **LFO:** vibrato rate → LFO Rate (CC 3) + LFO Pitch depth (CC 13);
  tremolo/filter wobble → Filter LFO Depth (CC 25). Zero them when no
  modulation is detected.
- **Effects:** default to dry (delay/reverb levels 0) unless the tail analysis
  shows repeating echoes (delay) or an exponential diffuse tail longer than
  the release fit (reverb). Coarse is fine — Stage 3 refines.

Output: a full CC dict + a per-parameter confidence, plus the chosen probe
note. Unit-testable end-to-end with synthetic audio: render a known
saw+filter+ADSR tone in numpy, run the estimator, assert parameters recovered
within tolerance.

### Stage 3 — short hardware refinement (changes to `session.py`)

- Seed the optimizer with the Stage-2 estimate: `x0 = space.encode(estimate)`
  and a *small* `sigma0` (≈0.1) since we start near the target.
- Search only what analysis is unsure about: build the `ParamSpace` from
  parameters whose confidence is low + the always-fuzzy ones (resonance,
  env depth, draw/chop). Typical dim drops from ~30 to ~10 → CMA popsize ~10.
- Early stop at a closeness threshold (config `target_closeness`, default
  e.g. 90%) or when the best hasn't improved for N generations.
- **Adaptive probe cost:** hold = clamp(target sustain length, 0.4–1.2 s);
  tail only as long as the fitted release; diff-based `apply()` (send only
  CCs that changed from the previous candidate). Cuts per-probe time roughly
  in half for short targets.
- Confirm the final winner with a second capture (average the two) so the
  saved patch isn't a lucky noise fluke.
- Budget: default ~10 generations. With the eval cache already in place, a
  full match should land in **2–4 minutes instead of 20**.

### Plumbing

- CLI: print the Stage-2 estimate and its instant closeness before refinement
  starts; `--no-refine` saves the analytic estimate directly.
- Web: new progress phases ("analyzing", "estimating", "refining") in the WS
  payload; show the analytic estimate's spectrogram/closeness immediately so
  the user sees a result in seconds, improving live afterwards.
- Keep the current path as a fallback (`--optimizer cma --no-estimate`) for
  A/B-ing the two engines.

### Out of scope (note, don't build yet)

- Full software S-1 simulator for offline pre-search — biggest possible
  speedup but large model-mismatch risk; revisit if Stage 2+3 plateaus.
- Multi-note matching — less urgent once the probe note matches target pitch.

## Verify

- Synthetic round-trip tests for every Stage-2 estimator (render known
  params → estimate → assert recovery).
- Pitch detector tests across the S-1's range + a noise target (must report
  unpitched, not a garbage note).
- Session test: seeded run with mock driver converges in fewer evals than
  unseeded (assert on eval count).
- End-to-end on hardware (manual): a handful of reference sounds — sub bass,
  reso pluck, PWM pad — comparing v1 vs v2 closeness and wall-clock.
