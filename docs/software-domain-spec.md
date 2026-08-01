# software-domain-spec — the S-1 fully enters the software domain

**Status:** ready to run with `/goal implement @docs/software-domain-spec.md`.
**Format:** goal spec, not an implementation plan — each goal states an *outcome* and
a *verify* clause; the implementation is free to find its own path. Optional items
are marked; scope does not creep past this document.

## Intent (the 6pm bar)

Plug the S-1 into the Mac with one USB-C cable. Start one command. A browser tab
pops up showing the whole synth — every panel knob, every menu setting, the
sequencer — live-synced both directions: twist a physical knob and the UI moves,
drag a slider and the hardware changes. The S-1's audio comes out of the Mac
speakers with no DAW and no config. Plug a MIDI keyboard into the Mac and play the
S-1 through the app. An AI agent can drive all of it through a documented API.
That's the whole goal: **the hardware synth, fully present in software, zero
friction.**

## Hardware ground truth (constrains everything; researched 2026-07-05, sourced)

- **The S-1 has NO SysEx** (official MIDI chart v1.02: SysEx x/x). Its state cannot
  be queried. The only live signals: it **transmits CCs when knobs move** and
  **accepts CCs in** (o/o). Therefore sync is: app-side state as truth,
  **push-all-CCs-on-connect**, then CC-listening keeps it live. No other
  architecture is possible; do not burn time looking for one.
- **Patterns/patches transfer via USB disk mode, not MIDI.** Hold [PLAY] while
  powering on → S-1 mounts as a USB drive ("S-1", takes 1–2 min) with `BACKUP/` and
  `RESTORE/` folders holding 64 files `S1_PTN<bank>-<num>.PRM` (4 banks × 16). To
  write: copy file into `RESTORE/`, press [HOLD], wait for "dOnE". The `.PRM`
  format is **plain text `KEY=VALUE`**, community-decoded (reference:
  `denzlobin/S1Utility` on GitHub, `PrmFileParser.cs`/`SequencerData.cs` — study
  it, reimplement cleanly in Python): patch params + full sequence (STEP count
  1–64; per-step NOTE1..NOTE4, VELO, LENG — 4-note poly; tempo/transpose/shuffle;
  8 motion lanes MOTION_CC1..8 + pitch bend).
- **Program Change (o/o, values 0–63) selects the 64 internal pattern slots live**,
  on a dedicated PC channel (device default: PC channel 16, synth channel 3).
- **Some patch parameters exist only in .PRM with no CC equivalent** — full patch
  fidelity ultimately requires the PRM layer (librarian is a milestone, not today).
- The S-1 is **class-compliant USB audio** (2ch 44.1kHz input "S-1" in CoreAudio)
  over the same cable as MIDI. No driver exists or is needed — "plug-and-play
  audio" means auto-starting our monitor (S-1 input → default output).
- Official knob CC list: manual "Knob assignments" page + midi.guide/d/roland/s-1.

## Decisions already made (do not re-litigate)

1. **The TUI is dead — delete it today.** Remove the Textual UI (`app.py`, `views/`,
   `widgets/`, `screens/`, `__main__.py` TUI launch path), its tests, and the
   `textual` dependency. The UI-agnostic core survives: `schema.py`, `state.py`,
   `midi_backend.py`, `sequence.py`, `sequencer_engine.py`, `patches.py`,
   `match/`, `web/`. Git history is the archive.
2. **Rename is a milestone, not today** (M4). Keep the `s1tui` package name; add a
   `s1` console command as the primary launcher (keep `s1tui-web` as an alias).
3. **Sync model: push-on-connect.** App state is truth at the connect moment (this
   overwrites what's dialed on the hardware — accepted). Knob moves stream in
   afterward and win over stale UI.
4. **Visual bar today: complete over gorgeous.** Every param present and usable in
   the synthwave-neon baseline; the full "make it pop" pass is the next session (M2).
5. **Dependencies restructure:** web + audio-monitor deps (fastapi, uvicorn,
   websockets, sounddevice, numpy, soundfile) move to core install; the match
   engine's heavy extras (scipy, cma) stay behind `[studio]`. `textual` is removed.

## Goals

### G1 — One command, app in browser

`s1` starts the FastAPI server and auto-opens the browser tab. No arguments needed
for the happy path. The server is the only front-end; nothing references the TUI.
**Verify:** fresh `pip install -e .` in a clean venv → `s1` → browser opens to a
working UI; `grep -ri textual` over source and `pyproject.toml` finds nothing;
full test suite green.

### G2 — Schema mirrors the device 1:1

Audit `schema.py` against the official chart and manual. Add every missing CC —
at minimum: Pan (10), Chorus level (93), chord-mode voices (80–87), Draw/Chop
oscillator controls (102, 103, 104, 107), portamento set (5/31/65 if incomplete) —
with correct names, ranges, value labels, and defaults per the manual. Organize
exactly as the hardware does: PANEL params grouped by faceplate section
(LFO / OSC / FILTER / ENV / EFFECTS), MENU params grouped and named as in the
manual's settings menu. Add a third access tier for PRM-only parameters (define the
enum member and any PRM-only params we already know; populating all of them is M3).
**Verify:** a test asserts the schema's CC set equals the documented chart's CC set
(encode the chart's list in the test as ground truth); section/menu grouping
matches the manual's organization; suite green.

### G3 — The full cockpit

The browser UI renders **every** schema param — panel and menu — as editable
controls (slider / toggle / selector per `ControlType`), grouped per G2's
faceplate/menu structure, in the existing synthwave-neon language (deep indigo,
per-section neon accents, filled meters). Nothing is hand-coded per param: the UI
is generated from the schema, so a schema addition appears in the UI for free.
Patch save/load (the existing JSON bank) works from the browser.
**Verify:** an automated check (API or DOM) that every schema param has exactly one
control in the served UI; changing a control fires the corresponding CC (assert via
a mocked MIDI backend in tests); patch save → load round-trips through the browser
API.

### G4 — Two-way live sync, plug-and-go

The server watches MIDI ports continuously: when an S-1 appears, connect
automatically, push the full app state (all CCs) to the device, and mark the UI
"SYNCED" (a visible sync chip: disconnected / connecting / synced). Incoming CCs
from knob twists update app state and every open browser view live over WebSocket
(< ~100ms perceived). Unplug → chip shows disconnected, app keeps working;
replug → auto-reconnect + re-push without a restart.
**Verify:** unit tests with a fake MIDI port cover connect→push-all, CC-in→state→WS
broadcast, and reconnect; the real-hardware behavior is confirmed in the plug-in
ceremony below.

### G5 — Audio out of the Mac, no DAW

Promote the audio monitor out of `match/monitor.py` into a first-class module.
On server start (and on hot-plug), auto-detect the S-1 audio input and start
routing it to the default output. A live level meter renders in the UI. A UI
toggle mutes/unmutes monitoring. Give the monitor real tests (fake/loopback
streams — it currently has zero).
**Verify:** monitor unit tests pass without hardware; ceremony confirms sound from
Mac speakers within seconds of plug-in with no configuration.

### G6 — Play it: MIDI keyboard + QWERTY

Any non-S-1 MIDI input device on the Mac is auto-detected and its notes forwarded
to the S-1 (hot-plug aware). In the browser, the computer keyboard plays notes
(QWERTY→pitch, octave shift keys) so the synth is playable with zero extra gear.
**Verify:** unit tests route fake external-keyboard events through to the S-1 port;
QWERTY events from the browser produce note-on/off via the API; ceremony confirms
a real keyboard plays the real synth.

### G7 — Sequencer in the browser

Port the piano-roll editor to the web UI: click/drag note editing, velocity,
per-step length, transport (play/stop/tempo), loop. The existing
`sequencer_engine` drives the S-1 live, now also emitting **MIDI clock** so the
device's delay/LFO tempo-sync follows the app. Patterns are stored app-side (JSON,
like patches). Respect device limits at the data-model level: 64 steps max, warn
at >4 simultaneous notes per step (PRM's ceiling). Include a **Program Change
control** (bank 1–4 × slot 1–16) to switch the S-1's internal patterns live on the
PC channel.
**Verify:** engine tests extended for clock emission; sequence CRUD + transport
round-trip through the API; ceremony confirms live playback + audible tempo-synced
delay + PC switching real slots.

### G8 — "Save to S-1": PRM export

A Python PRM writer (`synth/prm.py`): serialize a patch + sequence into a valid
`S1_PTN<bank>-<num>.PRM` file. In the UI: a "Save to S-1" button → pick bank/slot →
download/write the file → a guided walkthrough of the disk-mode ritual (hold PLAY
on power-up, copy into `RESTORE/`, press HOLD, wait "dOnE"), ideally detecting the
mounted "S-1" volume and copying automatically. Import/librarian is **M3, not
today** — but write the exporter against a parsed real backup: read one file from
Tyler's device `BACKUP/` and unit-test round-trip fidelity (parse → serialize →
byte-identical or semantically-identical).
**Verify:** round-trip test against a real backup file; ceremony writes one
agent-or-hand-made pattern into the physical device and plays it from the
hardware afterward, app disconnected.

### G9 — The agent door

The REST/WS API covers **everything a human can do**: read full state, set any
param, patch CRUD, sequence CRUD, transport, play notes, trigger PRM export,
select device patterns (PC). Served API docs (FastAPI's own /docs is fine) with
descriptions good enough that an agent given only the OpenAPI spec can operate the
synth. Add `tests/test_agent_drive.py`: a scripted "agent session" using only
public HTTP/WS calls builds a short sequence, sets a dreamy patch (slow attack,
long reverb/delay), plays it, and saves it — against a mocked MIDI backend.
**Verify:** that test passes; every UI capability has a corresponding documented
endpoint (checklist in the test or docs).

### G10 — Hygiene holds

Suite green at every landing point; new modules tested; `ruff` clean; README
rewritten for the new reality (web app, no TUI, plug-and-go quickstart, corrected
USB-audio claim); `docs/ux-spec.md` marked superseded by this spec where they
overlap.
**Verify:** CI passes; README quickstart is literally the ceremony's first three
steps.

## The plug-in ceremony (joint verification, ~10 min, Tyler + hardware)

The spec is only DONE when this passes end-to-end on the real device:

1. Fresh terminal: `s1` → browser opens.
2. Plug in S-1 (data cable) → sync chip goes SYNCED without touching anything →
   audio audible from Mac speakers.
3. Twist physical filter knob → UI slider moves. Drag UI resonance → hardware
   sound changes.
4. Plug in MIDI keyboard → play the S-1.
5. Open sequencer, enter a 4-bar line (or have an agent do it via API), press
   play → S-1 plays it, delay tempo-synced.
6. "Save to S-1" → disk-mode ritual → power-cycle → the pattern plays from the
   hardware with the app closed.
7. Unplug mid-session → chip shows disconnected → replug → resyncs by itself.

## Milestones (aspirational — marked, not today)

- **M1 — MCP server.** The synth as native Claude tools over the G9 API.
  Acceptance: *"make me a Baby Got Back sequence on my synth in a dreamy
  reverb-heavy state"* → agent composes + patches → preview → tweak → Save to S-1.
- **M2 — Make it pop + synesthesia tier 1.** Full synthwave polish pass on the
  cockpit; Tyler's note→color palette (see memory: A red, B brown, C white-blue,
  D blue-white, E neon green, F pastel red, G blue; sharps brighter, flats darker —
  confirm hex with Tyler) across piano roll, keyboard, note labels; both "true"
  and "legible" palette modes.
- **M3 — PRM librarian.** Full import: browse/back-up all 64 slots, PRM-only
  parameter surfacing (completing G2's third tier), device⇄app patch diff.
- **M4 — Rename/rebrand.** New name, package, repo, README identity. The `s1`
  command survives whatever the name becomes.
- **M5 — The digital twin.** Per `FABLE.md` (authoritative phase order) and
  `docs/match-v3-spec.md` (revise to match FABLE before running).
- **M6 — Visualizer / machine synesthesia tier 2.** Audio-reactive canvas view:
  time left→right, pitch vertical, timbre as shape, palette colors.

## Non-goals today

Matcher/twin work, MCP server, visual polish beyond the existing neon baseline,
PRM import, the rename, multi-device support, any cloud anything.
