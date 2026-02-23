# S-1 TUI

```
 ____  _   _____ _   _ ___
/ ___|| | |_   _| | | |_ _|
\___ \| |   | | | | | || |
 ___) | |   | | | |_| || |
|____/|_|   |_|  \___/|___|
```

**Your Roland S-1 deserves a terminal.** A full-featured TUI synth controller
with real-time sliders, patch management, and a piano roll sequencer — all
from the comfort of your command line.

No Electron. No browser. No 200 MB download. Just you, your terminal, and a
tiny analog synth.

## What it does

- **54 CC parameters** with real-time sliders, toggles, and selectors
- **Tabbed interface** — Panel, Menu, and Seq views mirror the S-1's layout
- **Piano roll sequencer** — load MIDI files, click to edit notes, play them
  back through the S-1 in real-time
- **Patch save/load** — store and recall complete synth states as JSON
- **Bidirectional MIDI** — turn a knob on the S-1, see it move in the TUI
- **Auto-connect** — finds your S-1 automatically

## Install

```bash
pip install -e .
```

> **System MIDI libraries:**
> - **macOS**: Works out of the box (CoreMIDI)
> - **Linux**: `sudo apt install libasound2-dev`
> - **Windows**: Works out of the box (WinMM)

## Usage

```bash
s1tui                          # launch (auto-connects to S-1)
s1tui --port "S-1"             # connect to a specific port
s1tui --channel 2              # MIDI channel (default: 1)
s1tui --list-ports             # show available MIDI ports
```

## Keyboard Controls

### Global

| Key              | Action                            |
|------------------|-----------------------------------|
| `c`              | Connect to MIDI port              |
| `s`              | Save patch                        |
| `l`              | Load patch                        |
| `r`              | Randomize all parameters          |
| `d`              | Reset to defaults                 |
| `0`              | Zero all parameters               |
| `t`              | Send test note (C4)               |
| `m`              | Load MIDI file                    |
| `space`          | Play / Stop                       |
| `p` / `x`        | Play / Stop                       |
| `q`              | Quit                              |

### Parameter Widgets

| Key              | Action                            |
|------------------|-----------------------------------|
| `left` / `right` | Adjust value +/- 1               |
| `Shift+left/right` | Adjust value +/- 10           |
| `Home` / `End`   | Min / Max                         |
| `j` / `k`        | Next / previous widget            |

### Piano Roll (Seq tab)

| Key              | Action                            |
|------------------|-----------------------------------|
| Arrow keys       | Move cursor                       |
| `z` / `Enter`    | Toggle note at cursor             |
| `+` / `-`        | Adjust velocity +/- 10            |
| `]` / `[`        | Adjust duration +/- 1             |
| Click            | Toggle note at mouse position     |

## Piano Roll Sequencer

The S-1's internal 64-step sequencer can't be programmed over MIDI (no SysEx
for step data). So S-1 TUI becomes the sequencer instead — load a MIDI file,
edit notes in the piano roll, and play them back as real-time MIDI note data.

```
 C3 ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·
 B2 ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·
 A2 ·  ·  ██ ·  ·  ·  ·  ·  ·  ·  ██ ·  ·  ·  ·  ·
 G2 ·  ·  ·  ·  ██ ·  ·  ·  ·  ·  ·  ·  ██ ·  ·  ·
 F2 ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·
 E2 ██ ·  ·  ·  ·  ·  ██ ·  ██ ·  ·  ·  ·  ·  ██ ·
 D2 ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·
 C2 ·  ██ ·  ·  ·  ██ ·  ·  ·  ██ ·  ·  ·  ██ ·  ·
     1  2  3  4  5  6  7  8  9  10 11 12 13 14 15 16
```

- Load any `.mid` file with `m`
- Notes quantize to the grid automatically
- Tempo syncs with the SEQ Tempo parameter
- Playhead shows current position during playback

## Patch Storage

Patches live in `~/.s1tui/patches/` as JSON:

```json
{
  "name": "fat-bass",
  "cc_values": { "74": 45, "71": 90, "73": 10 }
}
```

## Architecture

```
s1tui/
├── app.py                # Main Textual app
├── midi_backend.py       # MIDI I/O (mido + python-rtmidi)
├── sequence.py           # Note/Sequence model, MIDI file I/O
├── sequencer_engine.py   # Threaded playback engine
├── schema.py             # All 54 CC + 10 seq parameter definitions
├── state.py              # Centralized parameter state
├── widgets/              # CCSlider, CCToggle, CCSelector, PianoRoll
├── views/                # Panel, Menu, Sequencer tab views
└── screens/              # Modal dialogs (port select, patch, MIDI load)
```

## Extending

```python
from s1tui.midi_backend import MidiBackend

midi = MidiBackend()
midi.connect("S-1")

# Sweep filter cutoff
import time
for v in range(128):
    midi.send_cc(74, v)
    time.sleep(0.01)
```

## License

MIT

## Reference

CC mappings sourced from [midi.guide/d/roland/s-1](https://midi.guide/d/roland/s-1/).
