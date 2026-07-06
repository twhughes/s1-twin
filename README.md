# s1 — the Roland S-1, fully present in software

Plug the S-1 into the Mac with one USB-C cable. Start one command. A browser
tab opens showing the whole synth — every panel knob, every menu setting, the
sequencer — live-synced in both directions. The S-1's audio comes out of the
Mac speakers with no DAW and no config. An AI agent can drive all of it
through a documented API.

> The package is still named `s1tui` for historical reasons (it began life as
> a terminal UI, long since retired). The rename is on the roadmap; the `s1`
> command is the identity that will survive it.

## Quickstart

```bash
pip install -e .
```

1. Fresh terminal: `s1` → the browser opens to the cockpit.
2. Plug in the S-1 (its data cable) → the sync chip goes **SYNCED** without
   touching anything → the S-1's audio is audible from the Mac speakers.
3. Twist a physical knob → the UI moves. Drag a slider → the hardware changes.

That's the whole setup. Plug in a MIDI keyboard and play; or play from the
computer keyboard (`A`–`K` rows, `Z`/`X` for octave) right in the browser.

## What's in the tab

- **COCKPIT** — every S-1 parameter as a live control, generated straight
  from the schema and organized exactly like the hardware: the faceplate
  sections (LFO / OSC / FILTER / AMP / ENV / EFX / CONTROLLER, with ⇧ badges
  for SHIFT combos), the settings menu in the manual's order, and the
  MIDI-only performance controls. Patch bank (JSON, `~/.s1tui/patches/`),
  on-screen keyboard, and **Save to S-1**.
- **SEQUENCER** — a piano roll driving the S-1 live: click to add notes, drag
  for length, velocity editing, transport with gate/shuffle/probability, and
  MIDI clock out so the S-1's delay and LFO tempo-sync follow the app.
  Patterns live app-side (`~/.s1tui/sequences/`), respecting device limits
  (64 steps, 4 notes per step). A Program Change control switches the S-1's
  64 internal patterns live.
- **STUDIO** — the automated sound-matcher: drop in a target sound and let
  the optimizer drive the synth until it sounds like the target. Needs the
  heavy extras: `pip install -e ".[studio]"`.

## How sync works (and why)

The S-1 has **no SysEx** — its state cannot be queried. The only live signals
are CCs: knobs transmit when moved, and the synth accepts CCs in. So the app
treats **its own state as the truth**: on every (re)connect it pushes all 54
CC parameters to the device, then knob twists stream in and win over the UI.
Unplug and replug freely; the watcher reconnects and re-pushes by itself.

The S-1 **is** class-compliant USB audio over the same cable (a 2-in "S-1"
device in CoreAudio). The app auto-routes it to the default output — that's
the no-DAW monitoring path — with a level meter and mute in the header.

## Save to S-1 (patterns into the hardware)

Patterns/patches transfer via USB disk mode, not MIDI. The cockpit's
**SAVE TO S-1** card builds a device-ready `S1_PTN<bank>-<slot>.PRM` file
from the live patch + sequence (the format is community-decoded plain text;
the writer templates off a real device dump so it never invents keys). The
ritual, guided in the UI:

1. Power the S-1 off; hold **[PLAY]** while powering on.
2. Wait 1–2 min — a drive named `S-1` mounts (the app notices and can write
   the file into `RESTORE/` for you; otherwise download and copy manually).
3. Press **[HOLD]** on the S-1, wait for `dOnE`, power-cycle.

The librarian also works the other way: **LOAD FROM S-1** lists the mounted
device's `BACKUP/` patterns (plus any dumps in `~/.s1tui/backups/`) and reads
one back into the app — patch and sequence, live and editable. Any `.PRM`
file can also be uploaded directly.

## The agent door

Everything a human can do in the UI is a documented endpoint — interactive
docs at `http://127.0.0.1:8765/docs`. Read/set any parameter, patch and
sequence CRUD, transport, play notes, select device patterns, export .PRM.
Live state (including physical knob twists) streams over the `/ws/state`
WebSocket, which also accepts `param` and `note` messages back.

```bash
curl -X PUT localhost:8765/api/params/74 -H 'content-type: application/json' -d '{"value": 90}'
curl -X POST localhost:8765/api/notes -H 'content-type: application/json' -d '{"note": 60, "on": true}'
```

## Commands

```bash
s1                 # start the cockpit (opens the browser)
s1 --no-browser    # just the server
s1 --list-ports    # what MIDI ports does the Mac see?
s1tui-web          # alias for s1
s1tui-match        # the matcher's CLI (needs [studio])
```

## Development

```bash
pip install -e ".[studio,dev]"
pytest             # the whole suite runs against fake MIDI/audio — no hardware
ruff check s1tui tests
```

Architecture: `schema.py` (the single source of truth for every parameter,
audited against the official MIDI chart), `engine.py` (port watcher, sync,
keyboard forwarding, monitor auto-start), `audio.py` (S-1 USB audio →
speakers), `sequencer_engine.py` (playback + MIDI clock), `prm.py` (.PRM
parse/write), `web/` (FastAPI + the generated cockpit UI), `match/` (the
sound-matching engine).
