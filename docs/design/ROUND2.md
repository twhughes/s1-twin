# ROUND2.md — Tyler's first-use fixes (2026-09-27)

*Written by the lead (Claude, art director). Read `DIRECTION.md` (design law) and `BUILD.md` (the build contract:
ctx, twin API, `/ws/match` frames, rules §3, verification §4) first. This file adds only what round 2 changes.*

Tyler played the build and asked for four things: keyboard shortcuts for the sequencer (start, stop,
pause, and more); a Synth view that shows every control and the keys on one screen at 100% zoom (he had
to zoom out); recording audio as a match target; and a test that matches a note made with the synth's
current settings.

His screen: a 13-inch MacBook Air (2560×1664, "looks like" 1470×956). A maximized browser window leaves a
viewport of about **1470 × 760**. That is the fit target.

## 1. Ownership (touch only your files)

| Owner | Files | Must not touch |
|---|---|---|
| **lead** | `core/layout.js`, `core/layout.check.mjs`, `core/app.css`, `views/synth.js`, `design/**`, `docs/design/*`, merges | — |
| **W-keys** | `core/transport.js` (new), `core/shortcuts.js` (new), `*.check.mjs` next to them, `views/sequencer.js`, `views/sequencer.check.mjs`, `app.js` (boot wiring and the `hints` function only), `core/keys.js` (only if a shortcut needs it) | `views/synth.js`, `views/match.js`, `core/layout.js`, `core/app.css`, `design/**`, Python |
| **W-rec** | `views/match.js`, `views/match.check.mjs`, `synth/web/match_ws.py` (add routes to its router), `synth/match/twin_session.py` (additive), new `synth/web/static/core/wav.js` (+ check), `tests/test_match_record.py` (new) | `app.js`, `core/**` except `core/wav.js`, `views/synth.js`, `views/sequencer.js`, `design/**`, `server.py` |

Static files live under `synth/web/static/`. If you need a change in a file you do not own, write it in your
report as a **request** with the exact diff; the lead applies it.

## 2. W-keys: one transport for the whole app, and keyboard shortcuts

**The problem today.** The sequencer's clock (static mode) and its twin voicing (server mode, no S-1) live
inside `views/sequencer.js`. Leave the view and the pattern goes silent, so you cannot play a pattern and turn
knobs on the Synth view at the same time. That is the main thing a sequencer is for.

**Build `core/transport.js`**: `createTransport(ctx)`; the shell attaches it as `ctx.transport` for the page's
life. It owns the sequence model and the engine; the Sequencer view becomes its UI.

```js
ctx.transport.state        // {playing, paused, position, steps, bpm, step_resolution}
ctx.transport.seq          // the sequence {steps, bpm, step_resolution, notes}: the ONE copy the view edits
ctx.transport.perf         // {gate, shuffle, probability, clock}
ctx.transport.play() / pause() / stop() / toggle()   // toggle: playing and not paused -> pause, else play
ctx.transport.on(fn) -> off                          // fn(state) on every change, position ticks included
```

- Server mode: play/pause/stop go to `POST /api/transport` (as today). Follow the server through
  `ctx.on("server", fn)` (the shell's one `/ws/state`; every raw message arrives there), not a second socket.
  When `ctx.soundSource === "twin"`, voice each `position` step on the twin. When the S-1 sounds, the server
  already plays it: voice nothing.
- Static mode: the local clock (today's `local`) moves here, same semantics (Play resumes a pause).
- Voice through `ctx.note(n, true/false, vel)`, not `twin.noteOn` directly, so the plate's keys light and
  the pitch halos show while a pattern plays. Keep the hold/gate/probability/swing rules (`stepPlan`,
  `swingDelay`) exactly.

**Build `core/shortcuts.js`**: one global `keydown` handler (the shell creates it).

| Key | Does | Where |
|---|---|---|
| Space | play or pause the sequence | every view |
| Shift + Space | stop (back to step 1) | every view |
| 1, 2, 3 | go to Synth, Sequencer, Match | every view |
| ? | open the shortcuts sheet (a small dialog listing every key; Esc closes it) | every view |
| − and = | tempo down and up by 1 BPM (Shift: by 10) | Sequencer |
| Delete or Backspace | delete the selected note (today only while the roll has focus) | Sequencer |

Rules: never act while the user types (`input`, `textarea`, `select`, contenteditable) or when Cmd, Ctrl or
Alt is held (Shift is allowed where the table says so). Space must not also click a focused button: prevent
the default on keydown **and** keyup. Space in the roll stops adding notes (Enter still does). Keep every
existing key: A to K and Z/X play the synth (core/keys.js), arrows/Enter/[ ]/Esc in the roll, Esc closes a
drawer.

**Hints.** Each view module may export `hints` (an array of `{key, label}`); the shell's `hints()` shows them
in the KeyHint bar, with the drawer's `Esc Close` rule unchanged. **The Synth view shows no floating bar**
(`KeyHint.hide()`): on the one-screen plate its bottom-right corner is the keyboard, so the plate carries its
hints inline (lead). Sequencer hints: `Space Play/pause`, `⇧ Space Stop`, `? Keys`, plus the roll's.
The shortcuts sheet: sentence case, tokens only, the kit's `.drawer`-style surface or a small centered
panel, focus moves into it and back out, Esc and a Close button close it.

**Verify.** Node checks for the pure parts (key → action mapping, the typing guard, toggle semantics, the
static clock's step order with a fake timer). Headless screenshots of the sequencer and the sheet. Drive real
key presses with the CDP driver (`scratchpad/r2/shot.mjs`: steps `{"key": [[" ", "Space", 32]]}`) and assert
the transport state flips, on the Synth view too.

## 3. W-rec: record a target, and test with the synth's current sound

Server mode only (the static page has no matcher and keeps its recorded runs). Today the only target is a
dropped or chosen file. Add two more ways, in the same well:

1. **Record.** A `Record` button next to "choose a file", and an input picker (a `select` of the browser's
   audio inputs; the S-1 appears as one when plugged in; prefer it when present). Use `getUserMedia` with
   `echoCancellation`, `noiseSuppression` and `autoGainControl` all **false** (they wreck a synth's sound).
   While recording: a live level and the seconds; `Stop` ends it, and it stops by itself at 8 s. Capture raw
   PCM (an AudioWorklet recorder; a ScriptProcessor fallback is fine), trim the silence before the first
   onset (keep about 5 ms), encode a 16-bit mono WAV in `core/wav.js` (pure, node-checked), and load the
   result exactly like a dropped file ("Recording, 2.4 s"): hatch drawing, Play target, Match.
   If the browser says no, say what happened and what to do ("The browser blocked the microphone. Allow it
   in the address bar, then press Record again.").

2. **Test with the current sound** (a quiet button under the well: "Match the synth's current sound").
   It snapshots the 21 twin CCs from `ctx.params`, makes one note (the lowest marked note, else C3 = 48)
   with those settings, and runs the match on it, **from scratch** (never `init`: that would start at the
   answer) with the note seeded.
   - Twin sounding: `await ctx.twin.renderStages({note, seconds: 2.2, gate: 1.2})` and take **`amp`** (the
     twin.py model's output, before the browser-only effects). Encode at `sr`.
   - S-1 sounding: `POST /api/match/record-note` with `{note, velocity, hold, tail}` returns `audio/wav` of
     the S-1 playing that note, captured by the cockpit's audio monitor. Build it in `match_ws.py`'s router
     on `synth/match/driver.py`'s `SynthDriver.probe(None)` with the running `AudioMonitor` (never call
     `SynthDriver.calibrate()`: it overwrites the S-1's patch). Answer `409` with a plain `detail` when the
     S-1 port is closed, the monitor is not running, or the sequencer is playing; trim to the onset as
     above. Tests with a fake monitor and a fake MIDI backend.
   - When the match is done, show **how close it came back**: for each setting that shapes this sound,
     true value and found value; a one-line summary ("12 of 15 settings came back within 10"). A pure helper
     `relevantCCs(params)` decides which settings count (for example: no LFO settings when every LFO amount
     is zero; no sub octave when Sub is zero; no pulse width when Square is zero; no A/D/S/R when Volume
     shape is Gate and Env amount is zero). Node-check it. Say plainly that settings which do not change
     the sound cannot come back and are left out.

**Verify.** pytest for the route (fakes, no hardware), node checks for `wav.js`, `relevantCCs` and the
report, and a headless run of the twin path end to end on your own cockpit port (the full thing: button →
render → match frames → report). Recording from a real microphone cannot run headless: check it with Chrome's
`--use-fake-device-for-media-stream --use-fake-ui-for-media-stream` (a synthetic beep) and say so.

## 4. Lead: the one-screen plate

At 1470 × 760 every control and the keys show at 100% zoom, no scrolling; the static page's intro line and
smaller windows scale the plate down (a fit backstop) instead of scrolling. Below 1180 px wide the plate
stacks and scrolls as today.

## 5. Everyone

- Rules: BUILD.md §3 (tokens only, sentence case, no all-caps, no "→" on buttons, errors say what happened
  and what to do, 390 px with no sideways scroll, visible focus, reduced motion).
- Verify: BUILD.md §4. Run the tests for what you touched as you go and the full suite once at the end
  (`<repo>/.venv/bin/python -m pytest -q` from your worktree root). Machine load
  is high: one full run, not five.
- Ports for your own cockpit: W-keys 18111, W-rec 18112 (`SYNTH_PORT=<port> …/.venv/bin/synth --no-browser`,
  with `HOME` pointed at a scratch folder so you never touch `~/.synth`). Tyler's own cockpit runs on 8766:
  never stop it, never call it.
- Never open a visible browser window, never push, never touch `main` or `cyanotype`. Commit on
  your branch, message ending `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Report (short): files, check counts, screenshots you looked at, what you could not do and why, requests.
