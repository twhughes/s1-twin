# UX Spec — "a synth plugin that syncs to a real device"

Goal: the TUI and web app should feel like a great software instrument —
immediate, playable, visibly in lockstep with the hardware — while staying
simple. Keep the synthwave-neon identity everywhere; no flat/dead space.

Ordered by how much they change the *feel*.

## 1. Playable keyboard (TUI)

You can't feel a synth you can't play. Add an Ableton-style QWERTY keyboard:

- Keys `a w s e d f t g y h u j k` = C→C chromatic around the current octave;
  `z`/`x` shift octave down/up. Active while a new `KeyboardBar` widget (docked
  above the status bar, always visible on the Panel tab) has focus; `` ` ``
  (backtick) toggles focus to/from it.
- Terminals have no key-up events, so each press sends note-on and schedules
  note-off after a gate time derived from the current Release CC (min 150 ms).
  Retrigger on repeat is fine.
- The bar renders a mini keyboard with pressed keys lit in accent color, and
  shows the current octave. Velocity fixed at 100 (display it).
- Move the existing `t` test-note behavior to "press any keyboard key".

## 2. Real device sync

Make "what you see is what the hardware has" a first-class, visible property:

- **Push-on-connect:** after connecting (auto or manual), send the full
  current param state to the synth (reuse `_apply_values(send_midi=True)`).
  The S-1 can't be read back, so pushing is the only way to make UI==device
  true; do it automatically and notify "Patch pushed — in sync".
- **Sync chip in the status bar:** `⛓ SYNC` in lime while every change flows
  both ways (it always does once connected); amber `⛓ …` while a bulk push is
  in flight; red `✂ OFFLINE` when disconnected. Replaces the plain ◉/○ dot.
- **MIDI clock out:** while the sequencer plays, send MIDI clock (24 ppqn,
  `mido.Message("clock")`) from the engine thread using the same absolute
  scheduler, so the S-1's delay/LFO/arp tempo-sync features lock to the TUI's
  BPM. Start/Stop/Continue are already sent. Add engine tests with a mock
  backend asserting ~24 clocks per beat and clock cadence tracking live BPM.

## 3. Snappy rendering (TUI)

- `PianoRoll.render()` rebuilds the full grid Text on every playhead tick and
  cursor move (up to 40×/s while playing). Cache the rendered rows keyed on
  (notes-version, cursor, steps) and only invalidate rows the playhead/cursor
  actually crossed; bump a `notes_version` counter in the edit actions.
  Target: render under ~2 ms at 64 steps.
- Sliders: add mouse affordances — click on the meter sets the value at that
  position, mouse wheel = ±1 (shift+wheel = ±10), double-click resets to the
  param default. (Textual `MouseScrollUp/Down` + `Click.chain` for
  double-click.)
- Keep every param change → CC send on the message-pump path (already the
  case); never sleep in handlers.

## 4. Simplify the chrome (TUI)

- Footer is crowded and half the bindings are hidden. Trim the footer to
  `c connect · s/l patch · space play · ? help` and add a `?` help overlay
  (modal listing every binding, grouped: Global / Params / Piano roll /
  Keyboard). README table stays the reference; the overlay is generated from
  `BINDINGS` so it can't drift.
- Patch load modal: highlight-to-preview — moving the cursor over a patch
  name sends its CCs immediately (throttled), Enter keeps it, Esc restores
  the pre-browse snapshot (reuse the undo stack). Audition patches by ear,
  like a plugin preset browser.

## 5. Web studio flow

- **Zero-click start:** on page load, if diagnostics find exactly one S-1
  port (and BlackHole/an input), auto-connect, auto-start the monitor, and
  land on the Studio tab with Setup collapsed to a green summary row.
  Setup expands only when something's missing.
- **Live progress feel:** show the new `cache_hits` and an ETA
  (`evals_done/evals_total × measured s/eval`) under the closeness meter;
  during the (future) v2 estimate stage, render the analytic guess as soon
  as it exists.
- **A/B listen:** one button/keyboard shortcut (`b`) toggles playback between
  target and best clip at the same position — the fastest way to judge a
  match by ear.
- **Best-so-far on the synth:** an "audition best live" toggle that applies
  the current best patch to the hardware whenever it improves (through the
  already-running monitor), so the match literally converges out loud.
- Patch bank rows: click name = apply to synth + play; closeness shown as a
  small neon meter, not text.

## Non-goals

- No SysEx patch read-back (hardware doesn't expose it).
- No theme rework — synthwave-neon stays; new widgets reuse `theme.py`
  accents.

## Verify

- Engine clock test (mock backend, count clock messages per beat at two BPMs).
- KeyboardBar unit tests: key → correct note-on/off pitch, octave shift.
- Piano-roll render benchmark test (fail if a full render regresses past
  ~5 ms on CI hardware, marked slow/optional).
- Pilot tests: `?` overlay opens/closes; preview-restore restores the
  snapshot; push-on-connect sends all 54 CCs.
- Web: run the app, exercise auto-connect and A/B by hand (hardware needed).
