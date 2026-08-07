# chassis-spec — Logic is the chassis, the cockpit is the patch brain

**Status:** architecture + contracts decided 2026-08-07 (brainstorm session). M0 and M3
are Tyler-at-the-Mac ceremonies (no code). M1, M2, M3b, M4 are `/goal`-runnable code
slices. M5 *is* FABLE workstream #1 with two Logic hooks appended — this spec does not
touch the twin's priority or phasing.

**Format:** goal spec — each milestone states an *outcome* and a *verify* clause; the
implementation finds its own path. Scope does not creep past this document.

**Relationship to the other authorities:**
- `FABLE.md` — intent + the twin north star. Unchanged. Where `match-v3-spec.md`'s
  phase order disagrees with FABLE (analytics before twin), FABLE wins; revise that doc
  before running it.
- `music/CONTRACTS.md` §6 — the instrument abstraction (schema + the (k,s)
  differentiability split + backend protocol). This spec *adopts* it, never restates it.
- `synth/data/s1.json` — the canonical S-1 device file (already drift-tested against
  music's vendored copy). This spec adds no parameters to it.

---

## Intent (the 6pm bar)

Open Logic, load the **S-1 Rig** template, press play. The MK3 plays whatever track is
selected: the real S-1 on its instrument track, Logic drums, a bass instrument, Retro
Synth. One transport, one tempo, everything locked. A pattern built on the S-1 drags
into Logic as a MIDI region — knob motion included, as automation. The cockpit stays
open beside Logic as the S-1's patch brain — librarian, menus, matcher — and never
fights Logic over notes, clock, or audio. Practice — the music cockpit's trainer,
player, and exams, MIDI in → judgment out — stays first-class with no Logic and no S-1
in the room. Later, the twin appears in the same template as a second S-1 that happens
to be software — and becomes practice's sound module too.

---

## The architecture

Five layers. Named routes on the transport layer are the only allowed data paths.

```
UI         Logic Pro ............ arrangement · mixing · automation · Retro Synth UI
           cockpit :8766 ........ every S-1 knob + menu · librarian · sequencer ·
                                  scope · matcher/studio · REST/WS agent API
           music cockpit :8768 .. trainer · player · exams · S-1 panel · 6-6 · viz

hubs       logic mode ........... Logic owns transport, clock, routing, recording
           couch/cockpit modes .. the cockpit (or the S-1 itself) owns them
           practice mode ........ the music cockpit owns the keyboard + the verdicts

engines    S-1 internal ......... the hardware voice (4-voice SH-101 chain)
           twin ................. calibrated differentiable S-1 (torch) — FABLE ws#1
           Retro Synth .......... Logic-native AU, reachable only inside Logic
           Logic natives ........ Drum Machine Designer, Sampler, Alchemy, …

transport  CoreMIDI ............. "S-1" in/out · "Keystation 49 MK3" · IAC buses:
                                  "HQ Clock" · "HQ Bridge" · "HQ Twin"     (C5)
           CoreAudio ............ aggregate device "S1 Rig":
                                  main interface + S-1 input [+ BlackHole at M5b] (C6)
           USB disk mode ........ .PRM pattern/patch files (C2)

hardware   Mac · Roland S-1 · Keystation 49 MK3 · interface/speakers
```

**The ownership split — the sentence that prevents every fight:**

> The **cockpit owns the patch** — CC state, menu settings, PRM files, the bank — in
> every mode. **Logic owns the performance** — notes, clock, arrangement, audio —
> whenever it is running. **The music cockpit owns the judgment** — grading,
> curriculum, exam records — regardless of what makes the sound. None may claim
> another's third.

Corollaries: Logic never edits S-1 patch state on its own (no CC automation on patch
params unless deliberately recorded — see C7); the cockpit never forwards notes, emits
clock, or monitors audio while Logic is the hub (see C8/M1); the chord matcher exists
only in music's `theory/` (its rule) — no other component grades playing.

### Modes

Exactly one mode at a time. Each row has exactly one owner per mode — that exclusivity
is the whole point (double-forwarded notes and double-monitored audio are the two
failure modes this table exists to prevent).

| | **couch** (portable) | **cockpit** (solo studio) | **practice** (learning) | **logic** (full experience) |
|---|---|---|---|---|
| hub | S-1 / rig script | synth cockpit :8766 | music cockpit :8768 | Logic Pro |
| clock master | S-1 tempo knob | cockpit sequencer clock-out | none — free time | Logic transport (C4) |
| MK3 notes → | S-1, via cockpit/rig forwarding | S-1, via cockpit forwarding | music (graded; optional forward to a sound module) | Logic → selected track |
| S-1 audio → | cockpit/rig monitor (~35 ms) | cockpit monitor | rig monitor, if the S-1 is the sound module at all | External Instrument return (C7) |
| sequencer | S-1 internal patterns | cockpit piano roll | — | Logic regions |
| drums | `s1_drums` (slaves to S-1 clock) | — | optional `s1_drums` | Logic instrument tracks |
| patch authority | cockpit | cockpit | cockpit (music's S-1 panel follows the same listen-only + explicit-push policy) | cockpit (unchanged) |

The couch, cockpit, and practice columns already work today. This spec builds the
logic column. Practice needs no synth engine at all — grading is silent judgment on
MIDI in (music CONTRACTS §1's capture stream); a sound module (S-1 now, twin after
M5b) is an optional passenger, never a dependency.

### Engines — the two-tier abstraction

The answer to "can the engines be abstracted?" is: **yes, at two tiers — and forcing
everything into one tier is the trap this section exists to prevent.**

**Tier 1 — the S-1 dialect (rich).** An engine speaks tier 1 when it lives in the
54-parameter space of `s1.json` and implements the instrument backend protocol pinned
in `music/CONTRACTS.md` §6: `connect / disconnect / push_all / send(param, value) /
on_incoming(cb)`, params split **k** (continuous, gradient-eligible) / **s** (discrete,
enumerated). A tier-1 engine *may* additionally expose the differentiable forward model
`render(k, s, note) -> audio`. Tier-1 speakers: the S-1 hardware (no `render` — the
twin stands in, verified by hardware probes) and the twin (full `render`). Everything
the cockpit does — librarian, matcher, macros, morphs — operates on tier 1 and
therefore works identically on hardware and twin.

**Tier 2 — a plain MIDI instrument (lowest common denominator).** Anything that plays
from notes + optional CCs + program change under the mode's clock, sitting on a Logic
track. Tier-2 speakers: *everything* — the S-1 (via External Instrument), the twin (via
IAC + BlackHole, M5b), Retro Synth, Logic natives, drums. Tier 2 is what makes Logic
the chassis: every sound source is just a track.

| engine | tier 1 schema | `render` (differentiable) | live CC control | host |
|---|---|---|---|---|
| S-1 internal | yes (`s1.json`) | no — twin stands in | yes (CC in/out) | hardware |
| twin | yes (`s1.json`) | yes (torch) | yes (post-M5b) | synth service |
| Retro Synth | **no — by design** | no | only via learned Smart Controls | Logic only |
| Logic natives | no | no | Smart Controls / automation | Logic only |

**Retro Synth is tier-2 only, permanently.** It is closed, Logic-bound, and its engine
does not match the S-1's (two oscillators vs. the saw+square+sub+noise mixer). The
knob-bridge experiment (M3) treats it as a tier-2 instrument with a learned CC map.
A "preset converter" that pretends it speaks tier 1 is on the skip list.

**Canonical homes (HQ sharing rule — dedupe authority, not bytes):**
- `s1.json` → canon in `synth/synth/data/`; music vendors a byte-equal copy (exists,
  drift-tested).
- The instrument abstraction ((k,s) + backend protocol) → canon in
  `music/CONTRACTS.md` §6; this spec references it.
- **The calibrated twin → canon in `synth/` (`match/twin.py`, per `match-v3-spec.md`).**
  The gradient technique was proven in music's `[dsp]` kernel (analytic frequency-domain
  4-pole filter, gradcheck-exact); the twin *copies* what it needs with a provenance
  comment pointing at `music/music/dsp/` — never imports across the repo boundary — and
  adds a parity test if the copies ever both move.
- `sequence.py`'s `.mid` writer → canon in `synth/` (C3).
- Chord grading + theory → canon in `music/theory/` (music's own rule: the matcher
  exists only there). Practice-side capture/replay/exams are music CONTRACTS §1–§3;
  this spec consumes them as-is.

---

## The contracts

Change rule for all of them, mirrored from music's CONTRACTS.md: **change a contract →
bump it here and migrate every producer/consumer in the same session.** Where a
contract is testable, the test is named; where it lives in Logic or macOS settings, the
verify is a ceremony step (M0).

### C1 — `s1.json`, the S-1 dialect *(exists — restated for completeness)*
54 params: cc, range, default, labels, access, menu code, control kind, k/s tag.
Canon: `synth/synth/data/s1.json`. Music vendors a byte-equal copy; drift tests on both
sides. The PRM-only tier (params with no CC) stays in Python. The only sanctioned
departure from the device dump: chord-voice key shifts CC 85/86/87 default 64
(`test_prm.py` pins it).

### C2 — PRM disk-mode files *(exists — device ground truth)*
Patterns/patches transfer **only** via USB disk mode (hold [PLAY] on power-up → mounts
as "S-1", `BACKUP/` + `RESTORE/`, 64 × `S1_PTN<bank>-<slot>.PRM`, 4 banks × 16). Plain
`KEY=VALUE` text; patch params + full sequence (≤64 steps, 4-note poly, VELO/LENG) +
8 motion lanes (`MOTION_CC1..8`) + pitch bend. `prm.py` is the only reader/writer;
writes start from a real device dump (`data/init_pattern.prm`). The S-1 has **no
SysEx** — state cannot be queried; app-side state is truth; push is explicit.

### C3 — pattern interchange is a standard MIDI file
`Sequence ⇄ .mid` via `sequence.py`: **480 PPQN**, single track, `set_tempo` meta,
note_on/note_off pairs, note_off sorted before note_on at equal ticks. Extension (M2):
motion lanes export as CC events at step-boundary ticks on the same track, channel =
the S-1's synth channel. Round-trip law: `load_midi(save_midi(seq))` reproduces notes,
bpm, and resolution exactly (test-pinned). This one format is how patterns reach Logic,
how Logic phrases reach the S-1, and how morphs/motion reach automation lanes (M6c).
`.mid` in, `.mid` out — no bespoke interchange format, ever.

### C4 — MIDI clock: exactly one master per mode
24 ppqn + start/continue/stop (+ SPP from Logic). Masters by mode: couch = S-1 tempo
knob · cockpit = cockpit sequencer · logic = Logic (Project Settings → Synchronization
→ MIDI → Transmit MIDI Clock; destination the S-1 port, second destination "HQ Clock"
if the Logic version offers two). Followers never emit clock. Logic **never** follows
MIDI clock (it can't) — no component may be built assuming otherwise. Known behavior to
accept, not fix: Logic sends clock only while its transport runs; the S-1 free-runs at
its last tempo when clock stops.

### C5 — the IAC namespace
Three virtual buses, created once in Audio MIDI Setup, referenced by exact name.
Tools that need a missing bus fail loud with a one-line setup pointer.

| bus | direction | carries | first used |
|---|---|---|---|
| `HQ Clock` | Logic → tools | MIDI clock + transport, MMC in (M4) | M0 |
| `HQ Bridge` | cockpit → Logic | remapped controller CCs (S-1 knobs → Smart Controls) | M3b |
| `HQ Twin` | Logic → twin service | notes/CC for the soft S-1 | M5b |

### C6 — the aggregate device "S1 Rig"
Members: main interface + S-1 input (+ BlackHole 2ch from M5b). Sample rate 44.1 kHz
everywhere (the S-1 is 44.1-fixed). **Drift Correction ON for the S-1** (and BlackHole),
clock source = main interface. Logic's audio device is always "S1 Rig" (the template
pins it). The cockpit's own monitor keeps using the raw "S-1" input device — it never
opens the aggregate (that's Logic's).

### C7 — the External Instrument track (one per hardware/soft engine)
The Logic-side face of any non-native engine, pinned by the **"S-1 Rig" template**:

- **S-1 track:** External Instrument plugin — MIDI To = the S-1 port on the S-1's synth
  channel (per the cockpit's channel config); Input = the S-1's channels in "S1 Rig";
  input monitoring on. Track MIDI input = **MK3 only**.
- **The S-1's MIDI OUT is not an input to any track by default.** Knob CCs belong to
  the cockpit (patch state), not the arrangement. Recording knob automation into Logic
  is a deliberate act: enable the S-1 input on that track for the take, then disable.
- **Pattern select from the arrangement:** Program Change 0–63 on the S-1's PC channel
  selects hardware pattern slots; the External Instrument channel is set so region PCs
  pass through (ceremony-verified, M0).
- **Twin track (M5b):** same plugin, MIDI To = `HQ Twin`, Input = BlackHole channels.
  The soft S-1 enters Logic through the *same contract as the hardware* — that symmetry
  is the point of this whole spec.
- Template also carries: Transmit MIDI Clock config (C4), "Listen to MMC" ON (M4),
  project sample rate 44.1 kHz, plus ready tracks: Drum Machine Designer, a bass
  instrument, a Retro Synth experiment track.

The template is a binary Logic artifact (lives in Logic's Project Templates folder);
**this checklist is its canon.** If template and checklist disagree, the checklist wins
and the template gets rebuilt (M0 ceremony re-run).

### C8 — modes and hub exclusivity
The mode table above is normative. The general law: **at most one hub holds the MK3
route, and at most one service holds the S-1 connection, at any moment.** Passive
observers are always allowed (CoreMIDI multicasts; music's JSONL capture may listen in
any mode) — the exclusivity is about *routing and sounding*, the two things that double.
Each hub polices its own side: the synth cockpit via this contract's mode switch,
music via its `midio` mode router, Logic via the template (C7). Both cockpits already
share the S-1 connection policy — listen-only connect, explicit push — so a mode
handoff never stomps the hardware's live patch.

Cockpit implementation (M1): `mode ∈ {solo, logic}` (couch is solo with the rig script
instead of the cockpit; practice means the cockpit is simply not the hub): REST
(`GET/POST /api/mode`) + a UI chip. In `logic` mode the cockpit MUST suppress: MK3
forwarding, the audio monitor, and sequencer clock-out. Everything else — CC sync both
directions, librarian, bank, push-all — stays live in every mode. Default on startup:
`solo` (current behavior).

### C9 — engine tiers
The tier table above is normative: tier 1 = `s1.json` + music CONTRACTS §6 backend
protocol (+ optional `render`); tier 2 = notes + CC + PC under the mode's clock. Every
cockpit feature targets tier 1; every Logic track targets tier 2; **no feature may
require Retro Synth or any Logic native to speak tier 1.**

---

## Milestones

Priority order, but not a serial schedule (FABLE's rule): M0–M3 are an evening each and
land this week; M5 is the grind — interleave. Estimates assume the S-1 + interface are
plugged in.

### M0 — rig day-zero *(ceremony, no code — ~90 min)*
**Outcome:** the transport layer exists and the template plays. Steps: create the three
IAC buses (C5) → build aggregate "S1 Rig" (C6) → build the "S-1 Rig" template per C7
(External Instrument + clock transmit + drums/bass/Retro Synth tracks) → save as
template.
**Verify — the chassis ceremony:** record 8 bars of MK3-played S-1 next to a Logic drum
track on one transport; change Logic's tempo mid-take and hear the S-1's sequencer
follow; drop a Program Change in a region and watch the hardware switch patterns;
bounce the take. All four pass → the logic column of the mode table is real. Log the
result in STATUS.md.

### M1 — the cockpit mode switch *(code — half a day)*
**Outcome:** C8 implemented: `mode` in the engine + `GET/POST /api/mode` + a cockpit UI
chip; `logic` mode suppresses forwarding, monitor, and clock-out atomically; `solo`
restores them.
**Verify:** engine-level tests with the existing fakes (forwarding callback registered
in solo, absent in logic; monitor stopped; no clock messages emitted); WS broadcasts
the mode change; 363 tests stay green.

### M2 — the pattern bridge *(code — a day)*
**Outcome:** patterns move both ways between the S-1 and Logic through C3. CLI verbs on
the existing entry points (shape free, e.g. `synth prm export-mid <PRM> [out.mid]` /
`synth prm import-mid <in.mid> --slot <bank>-<slot>`): PRM → `to_sequence` → `save_midi`
with motion lanes as CC events; `.mid` → `load_midi` → `build_pattern` → a
RESTORE-ready PRM (4-note-poly + 64-step limits surfaced via `poly_warnings` /
`dropped_notes`, never silent).
**Verify:** round-trip tests (C3 law + a motion-lane fixture asserting expected CC
events); ceremony: drag an exported pattern onto the S-1 Rig track — the hardware plays
back its own pattern from Logic, knob motion replaying as CC automation.

### M3 — the Retro Synth knob experiment *(ceremony, no code — an evening, timeboxed)*
**Outcome:** an answer, not a feature. In the template's Retro Synth track, Smart
Controls learn the S-1's knobs (cutoff, resonance, env, LFO rate/depth, glide);
S-1 audio return muted while twisting (its engine still reacts locally). Play it for an
evening.
**Verify:** a written verdict in STATUS.md — *keep* (→ M3b) or *drop* (Retro Synth
stays a plain tier-2 instrument, no bridge built). Both are wins; the timebox is the
contract.

### M3b — the knob bridge *(code — a day; only on a "keep" verdict)*
**Outcome:** cockpit "controller mode": the CC stream the cockpit already owns is
remapped through a data-file table (source CC → target CC + curve) and forwarded to
`HQ Bridge`; Logic Smart Controls learn the bridged CCs once. Curves are data, not
code, so calibration (M6) can refine them.
**Verify:** remap-math unit tests (curves, passthrough, blocklist); ceremony: S-1
cutoff knob sweeps Retro Synth's filter with a curve that *feels* right.

### M4 — headless Logic transport *(code — half a day, optional)*
**Outcome:** `synth logic play|stop|record` (and REST equivalents) sends MMC over
`HQ Clock`; with the template's "Listen to MMC" on, the cockpit — and therefore any
agent on the REST API — can drive Logic's transport. No tempo-set (no such MIDI
message; tempo is typed in Logic, which then broadcasts it — C4).
**Verify:** unit test on the MMC bytes; ceremony: `synth logic play` rolls Logic.

### M5 — the twin *(FABLE workstream #1, unchanged — weeks; interleave)*
**Outcome:** exactly FABLE's phases, in FABLE's order: Phase 0 (benchmark harness +
**validate the perceptual metric against Tyler's ears**) → Phase A (pitch-correct
probing) → Phase B (the calibrated differentiable twin, torch — committed; copy from
music's `[dsp]` kernel with provenance, never import) → conditional analytics → magic.
This spec adds nothing to it and takes nothing from it; revise `match-v3-spec.md`'s
phase labels to FABLE's order before running.
**Verify:** FABLE's own clauses (metric agreement recorded; median match ≤ 4 min at
closeness ≥ best-ever; twin-vs-hardware error reported on held-out probes).

### M5b — the twin joins the template *(code — ~2 days, after M5's Phase B)*
**Outcome:** a real-time twin voice service (streamed synthesis; `s1_drums`' live numpy
kit is the in-house precedent) listening on `HQ Twin`, sounding out through BlackHole
into "S1 Rig"; an "S-1 Twin" External Instrument track in the template per C7; the
cockpit can point its panel at hardware or twin (same tier-1 backend protocol).
Optional rider: music's practice forwarding gains `HQ Twin` as a sound-module target,
so drills and the player have an S-1 voice with no hardware in the room.
**Verify:** ceremony: play the MK3 into the twin track next to the hardware track;
A/B a matched patch on both; freeze/bounce the twin track like any instrument.

### M6 — gradient garnish *(code — 1–3 days each, post-M5, each independently shippable)*
- **a. Steal-that-sound loop:** record any target in Logic → match offline on the twin
  → `push-all` to hardware → re-record on the S-1 track. Verify: end-to-end ceremony
  from a Logic region to a hardware take, wall-clock logged.
- **b. Jacobian macros:** at the current patch, SVD of ∂features/∂k → the top 3–4
  directions become cockpit macro sliders (`/api/macros`); optionally routed through
  `HQ Bridge` so chosen S-1 knobs *drive their own macro directions*. Verify: macro
  sweep audibly spans more timbre than any single CC; unit test on the linear algebra.
- **c. Morph-to-automation:** interpolate two bank patches along a perceptually-even
  path on the twin; render as CC events via C3; drop on the S-1 track as an
  8-bar automation clip. Verify: round-trip test + the clip replays on hardware.

### The skip list *(pinned — re-litigating requires editing this spec)*
1. Logic as clock follower — impossible, stop wanting it.
2. An S-1 → Retro Synth preset converter (tier-1 pretence; M3's learned knobs are the
   ceiling).
3. AU/plugin packaging of the twin before M5b proves it inside Logic via IAC+BlackHole.
4. Building a DAW. Logic is the chassis; `music/` stays a practice/theory platform.
5. `s1_drums` in logic mode — drums are Logic natives for now (Tyler, 2026-08-07);
   the script stays the couch companion. Revisit only if Logic drums disappoint.

---

## Guardrails

- **Tests are the contract** (363 green today). Every code milestone lands green and
  adds its own; ceremonies land as a dated STATUS.md entry with the verdict.
- **One clock master, one keyboard route, one monitor path** — any bug report that
  smells like doubled notes or phasey audio starts at the mode table.
- **Contracts move only with migration** — bump + migrate producers/consumers in the
  same session (music's rule, adopted).
- **No silent caps:** poly/step limits, dropped notes, clock assumptions — surfaced in
  CLI output and logs, never swallowed.
- **Keep the soul.** The cockpit stays a neon instrument; Logic integration must never
  make it feel like a config panel for a DAW.
