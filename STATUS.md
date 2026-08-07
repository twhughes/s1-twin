# STATUS — synth
*updated 2026-08-07 (chassis spec `docs/chassis-spec.md` — Logic-as-chassis architecture + contracts + milestones; earlier same day: keyboard forwarding moved off the watch tick to input callbacks; before that 2026-08-01 canonical `data/s1.json` + listen-only connect + `SYNTH_PORT`)*

- **state:** active
- **what:** The Roland S-1 hardware synth, fully present in software: one `s1` command starts a local web cockpit (FastAPI) with every panel knob and menu setting live-synced both directions, a piano-roll sequencer with MIDI clock out, auto-monitored USB audio with a live oscilloscope (drift-servo resampled passthrough, ~35 ms, glitch-free), MIDI-keyboard forwarding, .PRM export ("Save to S-1") *and* import (the librarian), synesthesia note-coloring, and a full REST/WS agent API. The Textual TUI is retired. Plus the CMA-ES sound-matching engine behind `[studio]`. 351 tests. A standalone headless twin of the monitor+forwarding lives at `music/tools/s1_rig.py` (launch: `music/rig.sh`).
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
