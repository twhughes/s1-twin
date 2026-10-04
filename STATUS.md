# STATUS — synth
*updated 2026-10-04, evening (**pushed and redeployed** — main 158b8bd on github.com/twhughes/s1-twin, the page at tylerwhughes.com/s1-twin/ links to Changes and syncs with it live; CI green); 2026-10-04, later (**the music app can follow this page's sound: opened with `#sync=music`, the page sends its whole sound once, then every knob change, to the window that opened it — `core/opener-sync.js` + its check, one way, nothing received; the public page and the README link to Changes (tylerwhughes.com/changes/)**); 2026-10-04 (**the twin plays 8 or 16 notes at once on the web; 4 with an S-1 connected; not yet pushed or deployed**); 2026-09-29 (**the loss ignores what lies 50 dB under the note; the optimizer race; not yet pushed**); 2026-09-28 night (**Record takes this Mac's own sound (Logic Pro); not yet pushed**); 2026-09-28 evening (**recordings, voices and a reproduction suite; not yet pushed**); 2026-09-27 late night (**published: github.com/twhughes/s1-twin, tylerwhughes.com/s1-twin/, CI green**); 2026-09-27 night (**Tyler's first-use fixes are in (round 2): one-screen plate, global transport + shortcuts, record + self-test in Match; 840 tests; README rebuilt** — see the round-2 bullet); earlier 2026-09-27 (the cyanotype build integrated); 2026-09-07 (venv rebuilt, merge prep); 2026-08-08 (soft synth + twin); 2026-08-07 (chassis build); 2026-08-01 (canonical s1.json)*

- **state:** active
- **what:** The Roland S-1 hardware synth, fully present in software: one `s1` command starts a local web cockpit (FastAPI) with every panel knob and menu setting live-synced both directions, a piano-roll sequencer with MIDI clock out, auto-monitored USB audio with a live oscilloscope (drift-servo resampled passthrough, ~35 ms, glitch-free), MIDI-keyboard forwarding, .PRM export ("Save to S-1") *and* import (the librarian), synesthesia note-coloring, and a full REST/WS agent API. The Textual TUI is retired. Plus the CMA-ES sound-matching engine behind `[studio]` and now a differentiable digital twin (`match/twin.py`, autograd, `[twin]`). 615 tests. A standalone headless twin of the monitor+forwarding lives at `music/tools/s1_rig.py` (launch: `music/rig.sh`).
- **interesting:** the "digital twin" — a differentiable software model of the S-1's fully-known signal chain, calibrated on a few hundred hardware probes, so sound-matching becomes offline gradient descent instead of 15 minutes of blind hardware probing. It's also the seed of the standalone soft synth.
- **last mile:** the joint plug-in ceremony (Tyler + hardware: knob-twist→UI, HW keyboard, disk-mode write, replug resync) confirms the ship; then the FABLE north star — validate the perceptual distance metric against Tyler's ears, then build the twin (docs/match-v3-spec.md).
- **cluster:** creative
- **port + `bin/` word (2026-07-30):** cockpit port reassigned per `PORTS.md` **8765 → 8766**
  (8765 is AnkiConnect's whenever Anki is open, and mashup wanted it too). Bind lives in
  `synth/web/server.py` (`HOST, PORT = "127.0.0.1", 8766`); `--port` help text, README, the
  two live-server test suites and `docs/improvement-spec.md` all follow. Adopted the HQ
  launcher convention: **`synth`** (`bin/synth`) runs the venv's `synth --no-browser`
  detached, waits on `/api/status`, opens the cockpit — `synth off` stops it. Appears on the
  HQ control panel (:8800).
- **2026-10-04 — more than 4 notes on the web (local, not yet pushed or deployed):** Tyler: "in s1 twin I'd
  like to play more than 4 notes at a time. I know the S-1 doesn't allow that, but I would on the web."
  **Engine:** the browser twin's voice count is a setting, 4 / 8 / 16 (`dsp.js` `VOICE_COUNTS`, `S1_VOICES`,
  `voiceCount()`; an Engine with no count is still 4, so parity is untouched). `createTwin({voices,
  destination})`, `twin.voices`, `twin.setVoices(n)` (a RangeError past the three); the worklet takes
  `processorOptions.voices` and a `{type:'voices'}` message that rebuilds the voices like new curves (every
  knob kept, notes sounding stop). Only Poly uses voices past 4; Mono / Unison / Chord are unchanged.
  **The page's rule** (`core/ctx.js`: `ctx.voices`, `setVoices`, `on("voices")`): 8 on the static page, 4 in
  the cockpit until chosen, and **4 whenever an S-1 is linked** (port listening/synced, or the connected
  demo). Settings → **Voices in this browser**: a 4 · 8 · 16 switch, "Voices (the S-1 has 4)", saved in
  localStorage (`synth.twinVoices`); while linked it shows 4, dimmed and locked, and says why. No new key.
  **CPU** (node 25 on this M3, 48 kHz, the busy modulated patch, engine + effects): 4 voices 2% of real
  time, 8 → 4%, 16 → 8%; the slowest 128-sample block (a 16-note attack, or a knob drag over 16 held notes)
  0.68 of 2.67 ms. So 16 stays offered. **Also:** `createTwin({destination})` for the music cockpit's new
  `twin` sound driver (it vendors twin/ — see music STATUS), and `close()` now stops the processor (it
  returned true forever, so a closed twin kept running). **Checks:** new `twin/voices.check.mjs` (67: 8 voices
  sound 8 notes and the 9th steals, 16 and the 17th, the voice modes; the worklet rebuild keeps knobs;
  `createTwin` over a fake Web Audio driving the real processor; the ctx rule incl. the S-1 lock; the
  drawer's words), run by `tests/test_plate_js.py`; `dsp.check.mjs` times 16 voices too. Headless Chrome on
  a scratch build (random ports, throwaway profile): the drawer starts at 8, 16 survives a reload, the
  connected demo locks at 4, the real AudioWorklet gets louder from 1 to 4 to 8 notes and `setVoices(4)`
  caps it. pytest 992 passed, ruff clean, every node check green. README "Play it" and BUILD.md §2.2/§2.3
  say it. Not heard by ear yet.
- **2026-09-29 — rounds 12 and 14, the search against the loss (local, not yet pushed):** Tyler: "u sure this
  is the best algorithm for optimization?", then "yea 1,2 please" (starts at once on every core; CMA-ES first).
  **Diagnosis first:** the loss at the true patch against the found one, per suite case. The search missed a
  much lower basin on vibrato, wobble and high (3-5x lower at the truth); on the recorded copies the loss itself
  preferred a patch that imitates the room, so no optimizer could help. **Race** (7 search-limited cases,
  closeness): Thorough 62.9, starts at once 67.2, CMA-ES 62.7 (nothing), mixes lower; kept unmerged on branch
  `r12/global` (`pool.py`). **The loss was the bigger limit:** its log terms looked ~100 dB down (hiss 70 dB
  under a tone scored 7.4). `twin.LOSS_FLOOR_DB = 50`: Thorough clean closeness 68.3 → 74.6% (vibrato 62 → 80,
  wobble 42 → 71, short 35 → 71, pad 90 → 96), settings back on the recordings 52 → 68%; new
  `docs/match-baseline.json`. Worse: high 62 → 55 (the fake-gate basin; starts at once fix it), bass 40 → 35,
  and the closeness number on recordings (27 → 16: it still counts the room 80 dB down). Tyler's takes by the
  old measure: 16.1 → 11.6% mean, same notes; A/B files for his ears in `~/.synth/listen/2026-09-29/`.
- **2026-09-28 (night) — round 13, Record takes this Mac's own sound (local, not yet pushed):** Tyler: "also can
  we record from system audio perhaps? like me playing a logic pro instrument". The Match view's "From" picker
  offers "This Mac's sound (all apps)", and "Logic Pro" while it runs; the take then goes the way of a mic take.
  A Swift helper (`synth/native/systap.swift`, Core Audio process taps, macOS 14.2+) is built on first use into
  `~/.synth/bin/`; routes `/api/match/system/{sources,start,stop}`. Nothing is played: the tap only listens.
  **Who macOS asks:** a terminal has no usage string, so macOS refused it silently and every take was silence.
  The helper starts a copy of itself that owns its permission, with its own Info.plist, so macOS asks about the
  helper (private `responsibility_spawnattrs_setdisclaim` + `TCCAccessPreflight`, looked up at run time, with a
  fallback). A rebuilt helper asks again. A Bluetooth mic (AirPods: 16–24 kHz, processed) gets one plain line
  under Record. 988 tests (31 new, on a fake helper), ruff clean, node checks green; 1470×760 at scale 1.
- **2026-09-28 (evening) — "record sounds and just reproduce them", and why vocals fail (rounds 5 to 11; local,
  not yet pushed):**
  - **Round 5, a bug found from Tyler's own bad match:** the 16 kHz search twin took its filter ceiling
    (0.45 × sr) and harmonic count from its own rate, a duller instrument than the 22 kHz model (−7 dB at 5 kHz);
    `Twin(model_sr=…)` + a band-limited search loss. On the S-1 default square: Quick 39 → 84%. Every match is
    now kept in `~/.synth/matches/` (target, match, meta with a loss trace; newest 20) for diagnosis.
  - **The reproduction suite** (`tools/match_suite.py`): 12 twin-made cases + 6 "recorded" copies (a small
    speaker, a room, 48 kHz, −18 dB, 2 s of room noise before), scored with the view's own report, compared
    with `docs/match-baseline.json`; fast cases in `tests/test_match_suite.py`. Before: recorded 3–8%.
  - **Round 6 (W-rec):** plain starting patches, a mild prior on the extras (λ 0.02), a note-length scan,
    floor-matched hiss (16-bit tails), seeds. **Round 7 (W-rec2):** `target_prep.py` auto-crop (onset within
    ~2 ms), a key-up guess, `/api/match/prepare`, the crop drawn and draggable, Play target = what the matcher
    gets, Play my patch, A–K on Match, the detected note pre-marked. **Round 9 (W-voice):** `pitch.py` (YIN
    50–2500 Hz, one sung note = one note, cents and wobble), `reach.py` + a plain "out of the S-1's reach"
    line after a match (vowels: two or more resonances; the S-1's filter makes one).
  - **Lead integration fixes:** the key-up guess read a 16-bit tail's floor as a key-up (1.505 s on a 1.2 s
    note) and the length scan only tried multiples of the guess → the plateau must sit within 40 dB of the
    peak and before the crop's end, and the scan always tries 1.2 s and a spread; a Gate trial that starts
    where the envelope matters (gate 68.5 → 78% Quick); a clean note keeps its whole release tail for the
    matcher while a recording ends at its room (sub 45 → 78%, square@rec kept at 39%).
  - **Tyler's six real takes, cold (no notes marked), Thorough:** every one now detected as one correct note;
    closeness 0.4 → 22.0 (C♯3 vowel), 0.9 → 6.4 (high glide), 9.9 → 11.5, 10.6 → 10.9, 26.8 → 27.7 and
    20.0 → 18.1 (whistles). **Why vocals stay low:** the S-1 cannot make a vowel (one resonant filter), and a
    voice wavers unevenly. Open: whistles score badly even from a hand-made pure tone (loss 12.96), so
    something else in those takes dominates (loudness swell? room?); short notes (suite "short" ~35%).
  - Checks: 959 passed, ruff clean, every node check green.
- **2026-09-28 — round 4, the matcher tries harder (local, not yet pushed):** Tyler: "i wish also the optimization
  tried harder ... it gives up too easily. especially on the discrete options." Cause: the whole descent ran under the
  default switches, and the other switch settings were scored once at the end with no re-descent; fixed step counts;
  a constant LR. **Now** (W-rec, `twin_session.py`): plateau-stopped descents on a cosine LR, a switch re-descent after
  every start (Quick 1, Thorough top 3, Deep all), an LFO scan (wave × rate × pitch/filter), a final polish, a **Deep**
  preset (8 starts), and **Finish now** (`"finish"` on /ws/match → a done frame with the best so far). **Benchmark**
  (5 twin-made targets, 4 with non-default switches; `docs/match-benchmarks.md`): closeness up on all five (mean
  55.5 → 66.6%), switches found 5/5 (old 3/5), more settings back on every target; Thorough now 1.4–2.3 min (was
  ~1). Soft-page e2e tests pinned to the old quick budget (107 → 40 s). Weak spots left: fine tune and vibrato depth.
- **2026-09-28 — round 3, every view on one screen (local, not yet pushed):** Tyler: "that page is spilling off the
  bottom of the screen" (Match: 1,027 px idle with the Match button below the fold, ~1,650 with the report; the
  Sequencer 1,072). `core/fit.js` (the plate's fit, shared: 1470 px design width, scaled down evenly, never up) and a
  **bottom strip** (footer + key hints, like a status bar; `--strip-h` 34 px) so nothing floats over a control. Match
  (W-rec): compact left column, 150 px wells, one done row, one fixed panel that switches between the knobs and the
  recovery report; every state 554–643 px. Sequencer (W-keys): the roll beside a 300 px column for sequences,
  patterns and the warning; 624 px in every state. At 1470×760 all three views: page height 760, scale 1.
  842 pytest, ruff clean, 11 node-check files green. Next (round 4, running): the matcher tries harder — switches
  with re-descent, plateau-based descents, a Deep preset, Stop = finish with the best so far.
- **2026-09-27 (late night) — PUBLISHED (Tyler: "we can publish this changes. both in github and put on my
  website too"):** GitHub repo renamed `s1tui` → **`twhughes/s1-twin`** (old URL redirects), `main` pushed
  (a3e7821 → e064442, 56 + round-2 commits, history scanned: no secrets, same author identity as before), page
  deployed to `gh-pages` → **tylerwhughes.com/s1-twin/** (live, headless check: no console errors, fits
  1470×760), repo description + homepage + topics set, **CI green** on Python 3.10 and 3.12 (the workflow now
  installs the `twin` extra and Node 22). Tyler's site: "S-1 twin" is first in Side projects (pushed as d3e54be
  after rebasing twice onto another session's pushes). **Still open:** the product name (brand.js `NAME`), the
  ~90-min S-1 + ears session (`docs/hardware-session.md`), then re-deploy with calibrated curves.
- **2026-09-27 (night) — round 2, Tyler's first-use fixes (contract `docs/design/ROUND2.md`):** Tyler played the
  build and asked for sequencer shortcuts, a Synth view that fits one screen at 100% (he had to zoom out), recording
  in Match, and a test on the synth's current sound. **Built:** (1) the one-screen plate — switches share lines,
  44 px dials, 670 px tall at its 1470 px design width, so it fits his 13-inch MacBook Air (viewport ~1470×760)
  unscaled; smaller windows scale it evenly (floor 0.7), below 1180 px it stacks; (2) `core/transport.js` (one
  transport for the page: a pattern keeps sounding on the twin while you switch views; voices through `ctx.note`,
  so keys light) + `core/shortcuts.js` (Space play/pause, ⇧Space stop, 1/2/3 views, ? list, −/= tempo, Delete);
  (3) Match: Record (browser inputs, S-1 preferred, raw PCM → WAV in `core/wav.js`) and "Match the synth's current
  sound" (twin: `renderStages().amp`; S-1: new `POST /api/match/record-note` on `SynthDriver.probe(None)`), always
  Thorough from scratch, then a recovery report. **Result on a saw+square test patch: 14 of 14 settings back within
  10, 71% closeness, ~78 s** (Quick from scratch: 7 of 14 — it trades Saw for Square + Sub). Parallel workers
  `r2/keys`, `r2/rec` + lead `r2/fit`, merged. **Checks:** 840 pytest, ruff clean, 10 node-check files green.
  README rebuilt around three real captures (`docs/images/`: the plate, the match GIF, the recovery table).
  Not shown in the README on purpose: the connected view — the only capture is the demo flag's fake signal.
- **2026-09-27 (end of day) — built and integrated, NOT published (branch `cyanotype`, local only):**
  one front-end in `synth/web/static/` that runs three ways — cockpit + S-1, cockpit with the **browser
  twin** (AudioWorklet port of `twin.py`, parity-gated: offline ≤ 0.001 log-mel, real-time ≤ 0.07; different
  patches ≥ 1.9 apart), and the **static page** (`tools/build_site.py` → `site/`, served from a subfolder with
  zero console errors). Views: Synth (the plate), Sequencer, Match (twin matcher over `/ws/match`,
  `twin_session.py`); Library + Settings drawers; ear test at `/eartest`; `synth-calibrate` (dry run recovers all
  18 hidden curves: held-out gap 0.449 → 0.070, S-1 repeat floor 0.037) + `synth-eartest-report`;
  `docs/hardware-session.md` (the 90-minute script). **Fixes found on the way:** `_ladder_tv` wrapped long
  ringing (nfft 2·win → 4·win); CC15 pulse width was inverted (0 now = 50% square); triplet grids played 3×
  too slow; `s1` wasn't an installed command; the ear-test report wasn't in the package. **Recorded matches**
  for the page (`matches/`, thinned): recovery of a twin-made patch 62% (envelope + resonance recovered almost
  exactly; noise invented), square lead 50%, pluck 36%, acid bass 33% — honest numbers; the ear test decides
  what they mean. **Tyler's personal site:** the entry ("S-1 twin", first in Side projects) is committed on a
  local branch `synth-entry` in `~/Documents/career/twhughes.github.io`, not pushed.
  **Waiting on Tyler (one decision):** publish as `tylerwhughes.com/s1-twin/` — rename the GitHub repo
  `s1tui` → `s1-twin`, merge `cyanotype` → `main` and push, `tools/deploy_site.sh` (gh-pages) + turn on Pages,
  then push the site entry. Still open after that: the product name (brand.js `NAME`), the S-1 session.
- **2026-09-27 (later) — the cyanotype build is underway (Tyler: "go for it"):** `main` fast-forwarded to
  `chassis-hardening` (f37cdd0; 638 green; local only, nothing pushed). Work branch **`cyanotype`** (the name
  `redesign` collides with the old `redesign/synthwave-neon`). Contract `docs/design/BUILD.md` (one UI in three
  contexts: cockpit + S-1 · cockpit with the browser twin · static page). Lead-built design kit in
  `synth/web/static/design/` (tokens, vendored OFL fonts, knob/switch/draw/palette/mark; 23 node checks).
  Router stubs `plate_routes` / `match_ws` / `eartest` wired into `server.py`. Four parallel workers on
  worktrees (branches `w/twin`, `w/plate`, `w/match`, `w/hardware`): browser twin with a parity test vs
  `twin.py` (the soft synth's biquad engine is NOT the twin's model — this fixes it), the shell + Synth view +
  drawers, the matcher in the cockpit + Match/Sequencer views, and `synth-calibrate` + the ear test +
  `docs/hardware-session.md`. Integration + review by the lead next.
- **2026-09-27 — rename + redesign, proposal stage (Claude as art director, Tyler's ask):** name **Menura**
  (the lyrebird's genus: it copies any sound it hears); direction = **cyanotype specimen plate** (Prussian-blue
  field, paper-white line art; color means pitch, bronze means the hardware). Signature pieces: the signal line
  with a waveform window per stage, and **the plume** (output as a phase portrait; real vs. twin overlaid when
  connected). Interactive comp `docs/design/menura-comp.html` (file://, no server), brief
  `docs/design/DIRECTION.md`. Nothing in the app changed. Also found: the public GitHub repo `twhughes/s1tui`
  still shows the retired TUI (last push 2026-02-24; local `main` is 22 commits ahead, `chassis-hardening` 6 more).
  **Tyler, same day:** direction approved ("otherwise it's awesome"); none of the names landed. **Waiting on Tyler:**
  ~~the scope call~~ → decided: synth first (twin + autodiff matching). Next: merge `chassis-hardening`
  (twin + `soft/` live only there), the ~90-min hardware + ears session (calibration is the claim "twin" rests
  on), then the build, a name, and the push.
- **2026-09-07 — venv rebuild + merge prep (no merge):** `.venv` was missing (the 638 tests were unrunnable).
  Recreated with **`/usr/local/bin/python3.12 -m venv .venv && .venv/bin/pip install -e ".[studio,twin,dev]"`**
  (recorded in README; Homebrew `python3` is 3.14 — don't use it). Full suite: **638 passed, 0 failed, ruff clean**
  (numpy 2.5 / scipy 1.18 / fastapi 0.141 / autograd 1.9 / pytest 9 — no rot in synth code; the 3 warnings are
  starlette/anyio deprecations). One real fix: `soft/run.sh` still defaulted to **8767** (mashup's port) after
  `f3fcfe4` moved the server to **8816** — aligned. **Branch state:** `chassis-hardening` = 6 commits / 54 files /
  +7,931 lines ahead of `main` (last main commit `8c2fb90`, 456 tests); `main` is an ancestor → **fast-forward**.
  Review pack: `MERGE-REVIEW.md`. **Waiting on Tyler:** one word — merge `chassis-hardening` into `main`?
- **2026-08-08 — standalone playable soft synth + differentiable visual sound-matcher (`soft/`, :8816, 620→638 tests):**
  a self-contained Web-Audio S-1-style instrument (`soft/index.html`): rotary knobs (osc/filter/env/LFO),
  a clickable + computer-key + Web-MIDI keyboard, Tyler's synesthesia key-colors — plays real-time, no
  hardware. Plus a **streaming visual sound-matcher**: drop a single note or **chord (up to 4)** → either
  **SEED the notes** by clicking keys (recommended; skips detection) or **cold-start** auto-detect
  (`analyze.detect_notes`, iterative harmonic-salience) → gradient-descend the shared patch on **a
  differentiable model of this synth** (reuses `match/twin.py` + `render_chord`; framed as the model, not
  "the twin"). You **watch it train**: knobs animate to the current guess, a loss curve falls, matched keys
  light, note-search refines the set. **Silent + fast by default** (server renders arrays only, zero audio);
  a **MONITOR** toggle plays candidates to hear it converge. Warm-start from the current knobs; QUICK vs
  **THOROUGH** (160 iters × 4 restarts). Server `soft/server.py` (FastAPI, WS `/ws/match`, 127.0.0.1 only,
  `SOFT_PORT` env 8816 — **claim in PORTS.md**); run `bash soft/run.sh`.
  - **Formulation:** objective = differentiable **multi-resolution STFT + log-mel + envelope** loss;
    search space = **~18 continuous k** (Adam) + **3 discrete s** (sub-octave / LFO-wave / amp-mode,
    enumerated) + **1–4 notes** (seeded or searched). Non-convex → multi-restart + a **Continue** button
    (queued). **Gold-standard recovery test** passes: a random rendered patch is recovered to closeness
    54–70 from cold — matches the **sound, not the exact knobs** (the synth is non-injective).
  - **Timing (seeded, thorough):** mono ~37 s (closeness 74) · 3-note ~78 s (87) · 4-note ~99 s (87).
  - **Queued next (same files):** delay + reverb knobs (S-1 CC 89/91/92) + matching, and the Continue
    button to resume a weak match.
- **2026-08-08 — the differentiable digital twin (FABLE centerpiece / M5 Phase B, `match/twin.py`, 601→615 tests):**
  built the software S-1: a differentiable forward model of the fully-known signal chain, in
  **HIPS `autograd`** (NOT torch/jax), added as a `[twin]` optional extra in `pyproject.toml`.
  The DSP math is a clean autograd.numpy reimplementation of music's gradcheck-exact torch
  kernel (credited in-file, never imported across the repo boundary): band-limited saw/pulse/sub
  + noise → **analytic 4-pole ladder** (a frequency-domain transfer function, NOT an unrolled
  recurrence, so autograd's tape stays shallow) → ADSR VCA, with an LFO on pitch + cutoff.
  Continuous params are the normalized **k** vector (18 CCs); discrete **s** (sub octave / LFO
  wave / amp-env mode) enumerated. The twin conforms to `DifferentiableBackend` — it's a real
  tier-1 backend beside the hardware (chassis C9).
  - **Gradcheck (the core proof):** the `k → render → differentiable-loss` gradient matches
    central finite differences to **max rel-err ~3e-8** (well under the 1e-3 bar), on the harder
    time-varying-filter + LFO path. *(Found and fixed a real autograd bug on the way: its
    `fft.rfft` VJP is wrong when the FFT size exceeds the input length — the code zero-pads
    manually instead.)*
  - **Search:** a **differentiable log-mel + multi-scale-STFT + envelope** loss carried in
    autograd (features.py/distance.py are plain numpy, not differentiable) + Adam over k, s
    enumerated. Self-consistency (target rendered by the twin at a known cc\*) recovers to
    **closeness ~78** — proves the search end-to-end with **zero hardware**.
  - **Benchmark numbers** (`benchmark(twin_matcher, corpus)`, probes = **0**, median **~23 s**/match,
    << FABLE's 4-min bar): twin-native corpus mean closeness **77**; the Phase-0 PLACEHOLDER
    corpus mean **51** (bright_saw 51 / dark_square 60 / sub_heavy 71 / noisy 22) — the residual
    is a real **twin-vs-placeholder** timbre gap (the placeholder is a crude different synth),
    which is exactly what calibration closes. See `docs/match-benchmarks.md`.
  - **HONEST GAPS (not done, by design — the tagged hardware/ears session):** calibration is
    against **synthetic/self-consistent** targets only (no real S-1 this session); `calibrate(probes)`
    is a working seam that fits the same curves against real hardware when it exists, and it
    reports a held-out feature gap (labeled synthetic). The differentiable distance is
    perceptually **MOTIVATED, not validated** — validating it against Tyler's ears is still the
    gate before trusting any match (FABLE rule #1). No metric-vs-ears claim is made anywhere.
- **2026-08-07 — chassis build + hardening sweep (branch `chassis-hardening`, 351→552 tests):**
  built the code half of `docs/chassis-spec.md` with parallel Opus subagents, then hardened
  every architecture seam into an explicit, tested contract. NOT committed to `main` yet —
  it sits on the branch for review. What landed:
  - **M1 mode switch (C8):** `S1Engine.mode ∈ {solo, logic}`; `logic` suppresses MK3
    forwarding + audio monitor + sequencer clock-out atomically (`set_mode`/`_apply_mode`),
    everything else stays live. `GET/POST /api/mode`, WS broadcast, `hello` carries mode,
    header chip.
  - **M2 pattern bridge (C3):** `synth-prm export-mid`/`import-mid` — PRM ⇄ standard `.mid`
    via `sequence.py`'s 480-PPQN writer; motion lanes (`MOTION_CC1..8`) export as CC events
    on the synth channel; poly/step overflow surfaced, never silent. `save_midi` gained
    optional `cc_events`/`channel`.
  - **M4 headless Logic transport (C4/C5):** `synth-logic play|stop|record` + `POST
    /api/logic/transport` send MMC (`F0 7F 7F 06 0N F7`, bytes pinned in tests) over the
    `HQ Clock` IAC bus; missing bus fails loud. `midi_backend.send_sysex` added.
  - **Matcher Phase 0 + A (offline half of FABLE ws#1):** `match/corpus.py` (Target,
    deterministic corpus gen, `benchmark()→{median_seconds,mean_closeness,probe_count,cache_hits}`,
    committed feature-only fixtures) and `match/analyze.py` (YIN f0 <0.35¢ error, Hz→MIDI,
    ADSR segmentation). **Phase-A loop is wired**: `session` now probes at the target's
    detected pitch (`analyze.probe_note`) instead of hardwired C3 — the `driver.py:17`
    `PROBE_NOTE=48` bug is fixed at the call site. **Corpus audio is a labeled PLACEHOLDER,
    not the S-1; the perceptual-metric validation against Tyler's ears is NOT done — still
    the gate before any twin optimization (FABLE's #1 rule).**
  - **Contract hardening (5 groups, seam audit → parallel):** new `Protocol`s pin every
    seam — `InstrumentBackend`(+`DifferentiableBackend`) in `backend_protocol.py`,
    `Driver`/`FeatureExtractor`/`DistanceMetric`/re-exported `Matcher` in `match/protocols.py`,
    `SequencerLike`/`EventCb` in the engine, `EngineFacade` in `web/facade.py`. Parametrized
    contract tests run real+fake side by side (`MidiBackend`/`FakeBackend`,
    `SynthDriver`/`FakeDriver`). FABLE debt cleared: `callable`/string-literal type
    annotations fixed; `midi_backend` no longer swallows unexpected errors silently
    (unplug quiet, real bug logged); `ParamState` listener iteration lock-guarded;
    `web/state.py` private reaches (`_snapshot`/`_best_clip`) replaced with public
    `MatchSession.snapshot()`/`best_clip()`. The real untested `SynthDriver` got direct
    unit coverage. Known leftover (flagged, engine-side): `server.py` still reaches
    `engine._mido`/`_tick_audio()` — a future engine-side promotion.
  - **Still needs Tyler/hardware (cannot be agent-closed):** M0 finish (save the `S-1 Rig`
    template + MIDI-clock-transmit test drive) · M3 Retro Synth knob eval (your ears) ·
    the twin itself (M5 Phase B — needs a torch install decision + the physical S-1 to
    calibrate + the ears-metric validation) · M5b (BlackHole) · M6.
- **2026-08-07 (earlier) — the chassis spec (`docs/chassis-spec.md`):** Logic Pro becomes
  the chassis. See the entry below for the architecture; the build above executes its
  code milestones.
- **2026-08-01 — the CC table became data, and connect stopped shouting:**
  - **`synth/data/s1.json` is now the canonical S-1 device file** (54 params: CC, range,
    default, labels, access, menu code, control type, and the k/s tag the music project
    needs). `schema.py` loads it at import and builds the same `S1_PARAMS` it always
    exposed — public API unchanged. The music project vendors a byte-equal `params` copy
    at `music/music/instrument/backends/s1.json`; a drift test on each side compares them.
    Edit the JSON, never a Python literal. The PRM-only tier stays in Python (no CC, no
    second consumer).
  - **Chord-voice key shifts CC 85/86/87 default to 64, not the factory 76/71/69.** The
    factory values overlay a transposed copy on every note in chord mode — the bug found
    live 2026-07-28. `test_prm.py` now pins this as the *only* sanctioned departure from
    the device dump.
  - **Connect is LISTEN-ONLY.** Auto-pushing app state stomped the hardware's live patch
    on every reconnect/power cycle (audible wobble/chop). New sync state `listening`
    (cyan chip) sits between `connecting` and `synced`; pushing is explicit —
    `POST /api/push-all` or the cockpit's **PUSH TO S-1** button, which is what now
    marks the session `synced`. This matches `music/music/instrument/service.py`'s policy.
  - `SYNTH_PORT` env overrides the 8766 bind (`--port` still wins over both). 360 tests.
- **2026-08-07 — keyboard forwarding is instant (input callbacks, not the tick):** external
  keyboard notes were only forwarded on the engine's 0.5 s watch tick — up to half a second
  of MIDI latency, and a quick tap's note_on+note_off arrived back-to-back as a zero-length
  (silent) note, which read as "keys not going through". `_tick_keyboards` now opens inputs
  with `callback=self._forward_keyboard`, so notes forward on the MIDI driver thread the
  moment a key moves (backend's send lock makes this safe); the tick-side `iter_pending`
  drain stays as a fallback for ports without callback support. Same session: forwarding
  grew pitch bend + the 'external' CC tier (Mod Wheel 1, Damper 64; `PERFORMANCE_CCS`) —
  all other keyboard CCs are blocked so controller knobs can't rewrite patch params.
  `send_pitchwheel` joined the backend. Found live 2026-08-07 playing the Keystation 49
  MK3 through the cockpit. 363 tests.
- **2026-08-07 — the chassis spec (`docs/chassis-spec.md`):** Logic Pro becomes the
  chassis; written from the brainstorm session — architecture (5 layers, 4 modes:
  couch/cockpit/practice/logic), 9 contracts, milestones M0–M6 + a pinned skip list.
  Load-bearing decisions: **cockpit owns the patch, Logic owns the performance, music
  owns the judgment**; one clock master per mode (Logic never slaves); S-1 enters Logic
  as an External Instrument over aggregate "S1 Rig" with IAC buses `HQ Clock/Bridge/Twin`;
  patterns travel PRM ⇄ `.mid` via `sequence.py`'s 480-PPQN writer + motion lanes → CC
  automation (M2); Retro Synth is tier-2 only (M3 knob experiment, timeboxed, no preset
  converter); the twin stays FABLE workstream #1 unchanged, then joins the same template
  via `HQ Twin` + BlackHole (M5b) — and becomes practice's optional sound module; drums
  are Logic natives for now (`s1_drums` stays the couch companion). Next Tyler ceremony:
  **M0 rig day-zero** (~90 min: IAC buses + aggregate + template + the chassis ceremony).
