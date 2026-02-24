# S-1 TUI

```
 ____  _   _____ _   _ ___
/ ___|| | |_   _| | | |_ _|
\___ \| |   | | | | | || |
 ___) | |   | | | |_| || |
|____/|_|   |_|  \___/|___|
```

Terminal synth editor for the Roland S-1. Sliders, patch management, and a
piano roll sequencer.

<p align="center">
  <img src="docs/screenshot.svg" alt="Piano roll sequencer" width="100%">
</p>

<p align="center">
  <img src="docs/panel.svg" alt="Parameter sliders" width="100%">
</p>

## What's in the box

- All 54 CC parameters as sliders/toggles/selectors across three tabs
- Piano roll with click-to-edit and MIDI file import
- Real-time playback — the TUI streams notes to the S-1 over MIDI
- Patches saved as JSON in `~/.s1tui/patches/`
- Two-way MIDI: tweak a knob on the hardware, watch the slider move

The S-1's built-in sequencer can't be programmed over MIDI (Roland didn't
expose any SysEx for writing step data — I checked). So this thing _is_ the
sequencer. It loads `.mid` files, lets you draw notes on a grid, and plays
them back as MIDI note-on/off messages. The S-1's internal sequencer is
bypassed entirely.

## Install

```bash
pip install -e .
```

You'll need system MIDI libs — macOS and Windows ship them, Linux wants
`sudo apt install libasound2-dev`.

## Usage

```bash
s1tui                          # auto-connects to S-1 if it sees one
s1tui --port "S-1" --channel 3 # port + channel (the S-1 defaults to ch 3)
s1tui --list-ports             # what's plugged in?
```

It tries to auto-connect to any port with "S-1" in the name. If it doesn't
find one, you'll see "No MIDI" in the status bar — press `c` to pick a port
manually. Run `--list-ports` first if you're not sure what yours is called.

The S-1 ships on MIDI channel 3 — if notes aren't going through, try
`--channel 3`.

### Hearing audio

The S-1 over USB is MIDI only — no audio. You have two options:

1. **Direct**: plug the S-1's headphone or line out into speakers/headphones.
   This is the simplest setup, the S-1 is its own synth.
2. **Through a DAW**: open Logic/Ableton/etc, create a track that receives
   MIDI from the S-1 port, and route it to a software instrument or back to
   the S-1 as an external instrument. This is what's happening if you can
   only hear sound with Logic open.

If you're not hearing anything at all, check that the S-1 is on the right
MIDI channel and that `s1tui --list-ports` shows it.

## Keys

`Ctrl+C` to quit. Everything else:

| Key | What it does |
|-----|-------------|
| `c` | Connect to MIDI port |
| `s` / `l` | Save / load patch |
| `r` | Randomize all params (have fun) |
| `d` | Reset to defaults |
| `0` | Zero everything |
| `t` | Test note (C4) |
| `m` | Load a `.mid` file |
| `space` | Play / stop |
| `p` / `x` | Play / stop (alternate) |
| `j` / `k` | Navigate widgets |

### Param widgets

`left`/`right` adjusts by 1, `Shift` for 10, `Home`/`End` for min/max.

### Piano roll

| Key | What it does |
|-----|-------------|
| Arrows | Move cursor |
| `z` or `Enter` | Toggle note on/off |
| `+` / `-` | Velocity +/- 10 |
| `]` / `[` | Duration +/- 1 step |
| Click | Toggle note at mouse pos |

Tempo is shared between the piano roll and the SEQ Tempo widget — change
one and the other follows.

## The piano roll

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
 120 BPM | Step 1/16 | C2 | (empty)
```

Hit `m` to load a MIDI file, or just click around to place notes. Press
`space` to hear it. Notes are quantized to the grid on import. The default
view is centered on C2 since, y'know, bass synth.

## Scripting

The MIDI backend works standalone if you want to do weird things:

```python
from s1tui.midi_backend import MidiBackend
import time

midi = MidiBackend()
midi.connect("S-1")

# filter sweep
for v in range(128):
    midi.send_cc(74, v)
    time.sleep(0.01)
```

## Project layout

```
s1tui/
├── app.py               # main Textual app
├── midi_backend.py      # MIDI I/O
├── sequence.py          # Note/Sequence + MIDI file read/write
├── sequencer_engine.py  # threaded playback
├── schema.py            # 54 CC + 10 seq param definitions
├── state.py             # param state store
├── widgets/             # sliders, toggles, selectors, piano roll
├── views/               # Panel, Menu, Seq tabs
└── screens/             # modal dialogs
```

## License

MIT — see [LICENSE](LICENSE).

CC mappings from [midi.guide/d/roland/s-1](https://midi.guide/d/roland/s-1/).
