# Changelog

## 0.3.x — 2026-08-01 (the table is data; connect is quiet)

- **Canonical device file** `synth/data/s1.json`: the 54 CC parameters moved out
  of a Python literal into JSON — CC, range, default, value labels, access
  level, menu code, control type, and the k/s tag the music project consumes.
  `schema.py` loads it at import and rebuilds the same `S1_PARAMS` (public API
  unchanged). The music project vendors a copy of the `params` array; a drift
  test on each side compares them. Edit the JSON, never a literal. The PRM-only
  tier stays in Python — it has no CC and no second consumer.
- **Chord-voice key shifts centered.** CC 85/86/87 now default to 64 instead of
  the factory init patch's 76/71/69 (+12/+7/+5). In chord mode those overlay a
  transposed copy on every note played — a synth that starts by transposing
  itself is broken (found live 2026-07-28). It is the only sanctioned departure
  from the device dump, and `test_prm.py` pins the exception list.
- **Listen-only connect.** An S-1 appearing no longer triggers a push of app
  state at the hardware: that stomped whatever patch the device was holding on
  every reconnect and power cycle (audible wobble/chop). New sync state
  `listening` (cyan chip) between `connecting` and `synced`; app state adopts
  the hardware via knob twists until you say otherwise. Pushing is explicit —
  `POST /api/push-all` or the cockpit's **PUSH TO S-1** button — and that is
  what marks the session `synced`. Matches the music project's policy.
- `SYNTH_PORT` env var overrides the 8766 bind; `--port` still wins over both.

## 0.2.x — 2026-07-28 (glitch-free monitor)

- **Drift-servo audio monitor**: the S-1→output bridge now resamples through
  the ring at a servo'd ratio (P + slow integrator on ring depth) instead of
  dropping/zero-padding whole blocks when the two devices' clocks drift
  (~0.5-1% measured). Kills the ~20 Hz grinding on held notes. Underruns emit
  silence and hold the read position — never replay stale samples (a looped
  block rings at SR/block Hz, an audible ghost tone). Streams open with 12 ms
  device-side buffers; steady latency ≈ 35 ms (was ~150 ms prefill + creep).
  Debugged live on the practice rig — standalone twin: `music/tools/s1_rig.py`.

## 0.2.x — 2026-07-06 (autonomous polish run)

- **The librarian**: patterns read back *from* the S-1. `LOAD FROM S-1` card
  lists the mounted device's `BACKUP/` and `~/.synth/backups/`; loading one
  applies patch + sequence live (and any `.PRM` file can be uploaded).
  API: `GET/POST /api/import/prm`, `POST /api/import/upload`.
- **Synesthesia note colors**: notes are colored by note name in the piano
  roll, on-screen keyboard, and note inspector. Three modes (persisted):
  `synesthesia` (Tyler's palette — candidate hex, tune by eye), `legible`
  (12-hue wheel anchored A=red), `off`.
- **Live scope**: a full-width neon oscilloscope across the cockpit renders
  the S-1's actual audio (~15 fps, auto-gain so a low hardware volume knob
  still draws a living trace). API: `GET /api/monitor/scope`.

## 0.2.0 — 2026-07-05 (software-domain-spec)

- The Textual TUI is retired; `s1` starts a local web cockpit (FastAPI) and
  opens the browser.
- Schema audited 1:1 against the official MIDI chart v1.02 (54 CCs), organized
  by faceplate + settings menu, plus a PRM-only tier.
- Every parameter rendered as a generated control; patch/sequence banks.
- Auto-connect two-way sync (push-all on connect, CC-listen back), hot
  unplug/replug; MIDI keyboards auto-forwarded; browser QWERTY.
- First-class audio monitor: S-1 USB audio → default output, auto-started.
- Web piano-roll sequencer with MIDI clock out; Program Change pattern select.
- `.PRM` exporter ("Save to S-1") templated on a real device dump;
  byte-identical round-trip.
- Full REST/WS agent API with OpenAPI docs and a scripted agent-session test.

## 0.1.x — the TUI era

- Textual terminal editor, CMA-ES sound matcher, match web studio.
