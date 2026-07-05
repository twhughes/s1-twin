# FABLE.md — a brief for the AI that fixes it all up

You're a state-of-the-art model with as much time as you need. This is the Roland
S-1 project I've been building: `s1tui`, a Textual terminal editor for the S-1 bass
synth, plus a sound-matching engine and a web studio that drives the real hardware.
238 tests pass, it's lint-clean, it's already pretty good. I don't want you to make
it *tidy*. I want you to make it **great** — the thing I'd show someone and they'd
say "wait, it can do *that*?"

Read this whole file, then read the code, then form your own opinion. If you find a
better plan than mine, take it — but tell me what changed and why. Work in the spec
→ `/goal` style: land changes behind passing tests, keep the synthwave energy, and
don't let scope creep past what's written here without flagging it.

---

## The one thing that matters most

**Build the digital twin — a software S-1 — and prove it by making the sound
matcher actually work: fast, accurate, and magical.**

Let me be precise about the relationship, because it sets the build order: the
*twin* is the product. The *matcher* is its first application and its proof of
correctness. Everything downstream — generative exploration, preference learning,
machine synesthesia, the standalone soft synth — rides on the twin. The matcher is
how we know the twin is real and not a toy.

That said, the matcher is also the weakest thing in the repo today. My own verdict
from using it: *"slow and doesn't work well."* It blind-searches CC values with
CMA-ES, probing the real hardware ~500+ times at ~2.3s each — 15-20 minutes for a
match that's often not close. It also probes a hardwired C3 (`match/driver.py:17`,
`PROBE_NOTE = 48`) regardless of the target's actual pitch, which quietly wrecks
accuracy on anything that isn't a C3.

One more thing, and it might be the single highest-leverage insight in this file:
**the distance metric is unvalidated.** Everything — the matcher, the benchmarks,
eventually preference learning — bottoms out in a "closeness" number, and nobody has
ever checked that it correlates with what sounds similar *to me*. Some of "doesn't
work well" may not be search failure at all, but the optimizer faithfully minimizing
a distance that doesn't measure perceptual similarity. If that's true, a perfect twin
will efficiently converge to the wrong sound. Validate the metric early: play me
pairs, ask which is closer, and confirm the number agrees with my ears before
trusting it for anything.

If you do nothing else, do this section.

---

## The North Star (think bigger than my backlog)

The S-1 is an SH-101 clone: one VCO (saw/square/sub/noise), one 4-pole filter, one
envelope, one LFO. That's a *tiny, fully-known* signal chain. A blind optimizer is
the wrong tool for a problem where we know the physics.

So the real move — the one I'd love a superintelligent model to just *build* — is a
**software model of the S-1's signal path** ("digital twin"): a differentiable,
cheaply-evaluable synth that, once calibrated against a few hundred real hardware
probes, predicts the S-1's output for any CC vector *offline*. With that:

- Matching becomes: analyze target → gradient-descend the model to the target →
  do a *short* hardware-refinement pass to correct model error. Minutes, not
  tens of minutes, and most of the search touches zero hardware.
- A/B, preset morphing, and "show me this patch" all work without the synth
  plugged in.
- You can search from a *stem* or even a text description, not just a clean clip.

Two commitments so nobody re-litigates them mid-build:

**Implementation: differentiable, not just cheap.** Build the twin in JAX (or
PyTorch — implementer's choice, but pick one and commit). Pure NumPy gets you
neither speed nor gradients; a differentiable renderer gets you both for the same
effort, and gradients are what make "descend the model to the target" and the
future preference-learning work first-class instead of bolted on.

**Sim-to-real is the research risk — treat it like one.** "Calibrate against a few
hundred probes" is one clause doing a lot of work. The S-1's analog character —
filter nonlinearity, resonance behavior near self-oscillation, envelope curve
shapes — is exactly what a textbook ladder-filter model gets wrong, and exactly
what the ear keys on. Budget real effort for calibration: measure the actual CC→Hz
and CC→seconds curves from hardware, fit per-module correction terms, and always
report twin-vs-hardware error on a held-out probe set so we know the gap instead
of assuming it away.

`docs/match-v2-spec.md` sketched the analytic half of this (estimate → seed →
refine) and `docs/match-v3-spec.md` carries it forward — but note the ordering
consequence of "the twin is the product": **build the twin before the analytic
estimation, and treat analytics as optional.** A good twin searched at thousands of
evals/sec may not need NNLS osc-mix fitting or spectral-knee analysis to seed it.
Build analytics only if they measurably beat twin-only search on the benchmark —
don't build them out of loyalty to an older spec. (Workstream #1 below is the
authoritative phase order; where `match-v3-spec.md` disagrees, revise it to match
this file before running it.)

---

## The far horizon — an AI that makes music it (and I) like

Here's where this is actually headed, and why the digital twin matters more than
"faster matching." I don't want a mashup bot. I want something experimental — closer
to audio diffusion in spirit — where an AI *has a synth and plugs away trying to make
something cool.* Three flavors, roughly in order of how far-out they are:

1. **Explore the sound space.** Once the twin (North Star / matcher Phase C) is a
   cheap, playable software S-1, you can evaluate *millions* of patches offline. Point
   an open-ended search at it — novelty search / quality-diversity (MAP-Elites) — and
   let it map out what this synth *can even do*, filling a gallery of maximally-varied
   sounds instead of chasing one target. This is the first real "AI is exploring"
   moment and it's mostly reachable the day the twin exists.

2. **Learn my taste.** Put me in the loop: it plays me sounds, I react (a rating, a
   "more like this," a keep/skip), and it fits a **preference model** over patch space
   and steers toward what I find cool. RLHF, but the reward is *me*, and the actions
   are synth patches. The open question I actually care about: does a consistent
   "what Tyler likes" surface even exist in this space? Build the loop and we find out.

3. **Invent its own synths from building blocks.** Don't fix the architecture at
   "SH-101." Give it modular DSP primitives — oscillators, filters, envelopes, LFOs,
   math nodes — and let it *wire up its own synth graphs* (program synthesis / genetic
   programming over a patch graph), then explore + learn taste over *those*. The S-1
   twin is just the first, hardwired instance of a much more general thing.

One correction to how all three are framed: **don't stop at timbre — music happens
in time.** Patch space alone yields a gallery of interesting *tones*; music needs
melody, rhythm, phrasing. We already own the other half of that equation — the
sequencer and piano roll (`sequence.py`, `sequencer_engine.py`). The generative AI
composes with **both**: the twin for what a sound *is*, the sequencer for when and
how it *moves*. Design the exploration/preference interfaces so a "candidate" can be
a patch, a phrase, or a patch-plus-phrase — not patches only. (This is also where
the audio-synesthesia mapping below gets something real to render: sound flowing
through time, not isolated notes.)

The thread tying it to the near-term work: **everything here rides on the twin.** A
differentiable-or-cheap, playable software synth is the substrate that makes search,
preference learning, and generative exploration tractable (you can't do RLHF-over-sound
at 2.3s per hardware probe). So when you build `twin.py` in the matcher, build it like
it's going to become this — clean, composable, fast, and playable on its own — not like
a throwaway optimizer inner loop. This is the emotional core of the project. The synth
editor is the on-ramp; *this* is the destination.

## Machine synesthesia — my colors, in the machine

I have synesthesia, so "cool visualization" isn't decoration to me — it's the point.
Music has a *look*, and I want this thing to render it. Two mappings, both real, both
mine — treat them as a perceptual spec, not a mood board:

**Note names have colors (this is key-based, discrete):**
A = red, B = brown, C = white-blue, D = blue-white (subtly bluer than C), E = neon
green, F = pastel red, G = blue. **Sharps** run a bit brighter, **flats** a bit
darker. (Candidate hex is in my notes — confirm the exact values with me before
committing; don't guess and ship.)

**Sound has a shape (this is audio, continuous):** time flows **left → right**, the
**vertical axis is pitch**, and **timbre reads as shape** — different timbres take
different forms. That's literally how I see a sound move.

Three tiers, near to far:

1. **Color the instrument (near-term, easy — folds into Workstream #3).** Use the
   note→color palette everywhere notes appear: the piano roll, the playable keyboard,
   note labels. Sharps brighter, flats darker. Suddenly the editor is *mine*.
2. **A visualizer / screensaver mode.** An audio-reactive view driven by the real S-1
   capture (or the twin): notes/partials flowing left→right, height = pitch, form =
   timbre, colored by the palette. Best built in the web canvas/WebGL where the
   rendering is rich — the braille scope is the TUI stand-in, this is the real thing.
   This is where "make it pop" and "make it mine" become the same feature.
3. **Machine synesthesia (far horizon, ties to the section above).** When the AI is
   off exploring sound with the twin, I want to *watch it think* — its generated music
   rendered the way I'd see it. The generative work and the visual work are one loop:
   AI makes sound → sound becomes image → I react to both. Build the visualizer with a
   clean "features/notes → visuals" seam so the same renderer works whether the sound
   came from me playing, the S-1, or the AI.

Get the mapping right and this stops being a synth editor with nice colors and starts
being an instrument that sees music the way I do.

## Ground truth (so you don't re-derive it)

Honest state, from a fresh audit — trust the code over this if they disagree:

- **Core is solid.** TUI, 54-CC schema (`schema.py`), patch bank, piano-roll
  sequencer, threaded playback engine, two-way MIDI sync — all built, all tested.
  238 tests pass in ~31s. `ruff` clean. CI runs on 3.10 + 3.12.
- **The `[studio]` layer (`match/`, `web/`) is real but less finished.**
  - `match/v2` (analytic estimation): **not started.**
  - `docs/ux-spec.md` (playable keyboard, MIDI clock sync, help overlay, web
    auto-connect + A/B listen): **mostly not started.**
  - `match/monitor.py` (186 LOC of realtime audio, its own lock): **0 tests.**
- **Known debt, small but real:**
  - `sequencer_engine.py:44,75` and `match/session.py:58` use the builtin
    `callable` / a string literal as a *type annotation*. These are wrong; no
    mypy in CI catches them.
  - `app.py` is a 552-line god object: one `S1App` class, 46 methods, 37 handlers,
    orchestrating MIDI + sequencer + patches + MIDI-file I/O + tempo + undo. Every
    new feature grows this file.
  - `midi_backend.py:64-116` swallows **all** MIDI send/port errors silently — a
    dead or misbehaving device fails invisibly.
  - `web/state.py:231,260` reach into `MatchSession` privates (`_snapshot`,
    `_best_clip`) across a module boundary.

---

## The one hardware fact that unlocks everything

**The S-1 is a USB *audio* device, not just MIDI.** Over one USB-C data cable it
shows up in CoreAudio as input "S-1" (2ch, 44.1kHz) *and* as "S-1 MIDI IN". The
README's "USB is MIDI-only" claim is wrong (a charge-only cable will fool you). This
is what makes closed-loop matching possible without an audio interface — lean on it,
and fix the README.

---

## Workstreams, in priority order

Each is written so you can lift it into a `docs/` spec and run it. #1 is worth more
than #2-#5 combined — but **priority order is not a serial schedule.** #1 is weeks of
grind; most of #2 and #3 (the playable keyboard, the note-colors, the visual pop) are
days of work and pure joy. Interleave them: land the quick wins while the twin work
grinds, so the instrument keeps getting more fun to touch the whole way through.

### 1. Rebuild the matcher around the twin (the whole point)

**Phase 0 — measure first, and validate the metric.** Build the benchmark harness
(corpus, `benchmark()`, baseline numbers) *and* run the perceptual check on the
distance metric: play me pairs, ask which is closer, confirm the closeness number
tracks my ears. If the metric is wrong, fix it *before* optimizing anything against
it — every later phase inherits this. Verify: documented baseline numbers + a
recorded metric-vs-ears agreement result.

**Phase A — pitch-correct + honest baseline.** Before anything clever: detect the
target's f0 (YIN/pYIN) and probe the S-1 at *that* pitch instead of hardwired C3.
Segment attack/sustain/release. This alone should meaningfully improve accuracy.
Verify: on a corpus of target clips at varied pitches, mean closeness score beats
the current C3-only baseline by a clear margin.

**Phase B — the digital twin (the centerpiece).** A differentiable forward model
`f(cc_vector, note) -> audio/features` (JAX or PyTorch), calibrated to real probes,
so the optimizer searches the model, not the synth. Keep a short hardware-refinement
pass to close the sim-to-real gap, and always report twin-vs-hardware error on
held-out probes. Target end-to-end: **2-4 minutes**, most of it offline. Verify:
median wall-clock per match under 4 min with closeness ≥ the current engine's
*best-ever*, on the same corpus. Build it clean, composable, and playable on its
own — it graduates to a standalone soft synth later.

**Phase C — analytic estimation (conditional).** The old v2 idea: harmonic
decomposition → osc mix via NNLS, filter cutoff/resonance from the spectral knee,
ADSR from the amplitude envelope, LFO from vibrato — a full CC estimate with *zero*
hardware probes, used to seed the twin search. **Only build this if it measurably
beats twin-only search** (better closeness or meaningfully faster convergence on the
benchmark) — the twin at thousands of evals/sec may not need it. Verify: seeded vs.
unseeded search compared head-to-head on the corpus; keep it only if it wins.

**Phase D — magic.** Match from a messy stem (source-separate first); "make it
brighter/fatter" text nudges on top of a match; save the matched patch straight to
the bank with a generated name. Verify: end-to-end demo from a real song stem to a
loadable patch.

Instrument all of it: log probe count, cache hits, wall-clock, and closeness so I
can *see* it getting better. If you cap or sample anything, say so in the logs.

### 2. Make it feel like a real instrument (`docs/ux-spec.md`)

- **Playable `KeyboardBar`** — QWERTY → notes, in both the TUI and web.
- **Real-device sync** — push-on-connect, a status-bar sync chip, and **MIDI clock
  out** from the sequencer engine so the S-1's tempo follows the app.
- **Web studio flow** — zero-click auto-connect, live ETA + cache-hits during a
  match, **A/B listen** (press `b` to flip target vs. best), audition-best-live.
- **`?` help overlay** and preview-on-hover in the patch browser.

Verify: I can plug in, hear myself play from the keyboard, start a match, and A/B
the result without touching a config file.

### 3. Make it *pop* harder (this is a synthwave instrument, not a form)

I care about this more than most people would. The neon direction in `theme.py` +
`param_widget.py` is the baseline, not the finish line. The braille scope, filled
neon meters, and signal-flow ribbon should feel alive — animate on real signal,
glow on activity, fill the screen edge-to-edge. No flat panels, no dead space. When
a match is running, I want it to look like the machine is *thinking*. Surprise me,
but keep it legible and fast.

### 4. Pay down the debt a superintelligence shouldn't tolerate

- Fix the `callable` / string-literal type annotations
  (`sequencer_engine.py:44,75`, `match/session.py:58`); add **mypy** to CI and make
  it pass.
- Break up the `app.py` god object — extract patch I/O, MIDI-file I/O, tempo
  conversion, and undo into their own modules. Keep every test green.
- Stop swallowing MIDI errors silently (`midi_backend.py:64-116`) — surface device
  failures to the UI.
- Give `match/monitor.py` real tests, or mark it explicitly as a prototype.
- Add a public accessor to `MatchSession` so `web/state.py` stops touching privates.

### 5. Ship it like it's real

Installable already (three entry points, hatchling). Take it the last mile: a
coverage gate + mypy in CI, a CHANGELOG, a demo GIF in the README that shows a
match happening, and a clean `pip install` path. If it's good enough to match sounds
in 3 minutes, it's good enough for other S-1 owners to want it.

---

## How I want you to work

- **Tests are the contract.** 238 pass today. Land every change with them green,
  and add tests for anything new — especially the matcher, where "it works" has to
  mean measured, not vibes.
- **Show your work in numbers.** For the matcher, I want before/after: probes,
  seconds, closeness. Don't tell me it's better — show me the delta.
- **Never trust an unvalidated metric.** Before optimizing against any similarity
  or preference score, check it against my ears. A number that doesn't track
  perception is worse than no number — it makes wrongness efficient.
- **Respect "exactly as written."** When you turn a section here into a `docs/`
  spec I run with `/goal`, optional stays optional and scope stays put. If you want
  to go past the spec, write a new one and say so.
- **Keep the soul.** It's a neon bass-synth cockpit, not an enterprise app. Every
  refactor should leave it *more* fun to look at and play, never less.

If you get all of this done and you're still bored: give me the digital twin as a
standalone thing — a software S-1 I can play and match against with the hardware
unplugged. That's the version of this project I daydream about.

— Tyler
