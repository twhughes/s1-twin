# Changelog

## 0.2.x — 2026-07-06 (autonomous polish run)

- **The librarian**: patterns read back *from* the S-1. `LOAD FROM S-1` card
  lists the mounted device's `BACKUP/` and `~/.s1tui/backups/`; loading one
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
