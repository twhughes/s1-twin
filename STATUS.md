# STATUS — synth
*updated 2026-08-01 (canonical `data/s1.json` + listen-only connect + `SYNTH_PORT`; before that 2026-07-30 port 8765→8766 + `bin/synth`)*

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
