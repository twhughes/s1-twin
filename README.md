# Twin — a software twin for the Roland S-1

*Working title; the name will change. Not affiliated with Roland Corporation. Roland and S-1 are
trademarks of Roland Corporation, used here only to say what this works with.*

![The Synth view: the S-1's signal chain drawn stage by stage, a held A2 tinting the chain](docs/images/synth.png)

A differentiable model of the Roland S-1, the small SH-101-style synth, that you can play, look
inside, and teach. Play it in the browser. Drop in a sound and watch gradient descent turn its
knobs until it sounds like the sound. Plug in a real S-1 and it syncs: every knob, both ways.

**Try it in your browser:** *(link goes live with the first release)*

## What it does

- **Play the twin.** The browser runs the same equations as the Python model: band-limited
  saw, pulse, sub and noise into a 4-pole ladder filter, an envelope, an LFO. Every stage has a
  window, so you see what each knob does to the wave. The output is drawn as a *plume*: each
  loop is one cycle of the sound, and sharper turns mean a brighter tone. Play it with A–K on
  your computer keyboard or a MIDI keyboard (Chrome).
- **Match a sound.** Give it a recording of one note or a chord of up to four. The matcher
  descends the model's gradient (Adam over 18 continuous knobs, plus the 3 switches tried in
  turn) against a multi-resolution spectral and envelope loss, and streams every step so the
  knobs move as it learns. A quick search takes seconds; a thorough one, a couple of minutes.
  It finds a patch that *sounds like* the target, not always the patch that made it.
- **Sync a real S-1.** Plug the S-1 in over USB and the page becomes its front panel: every
  knob and menu setting live in both directions, the S-1's audio on your speakers with no DAW,
  a sequencer with MIDI clock, a patch library, and **Save to S-1** for patterns. When the S-1
  is connected, the output window draws its real signal in bronze with the twin's prediction
  dotted over it, so you can see how close the twin is.

## Status, honestly

- **The twin is not yet calibrated against real hardware.** Its knob-to-sound curves are
  informed guesses. A calibration run fits them from a few hundred recorded probes of a real
  S-1, and an ear test checks that the matcher's idea of "closer" agrees with a listener.
  Until that session runs, a match is only as true as the twin.
- The browser twin matches the Python model to 0.001 log-mel offline and 0.07 in real time
  (two different patches are at least 1.9 apart), checked on every test run.
- Delay, reverb, chorus and the voice modes are browser extras outside the model: they sound,
  but the matcher does not fit them. Draw and Chop need the real S-1.
- Tested in Chrome. Safari is untested.

## Install

Python 3.10 or newer (3.12 recommended):

```bash
python3.12 -m venv .venv
.venv/bin/pip install "synth[studio,twin] @ git+https://github.com/twhughes/s1tui"
.venv/bin/s1            # opens the page; plug in an S-1 any time
```

## How sync works (and why)

The S-1 has **no SysEx**: its state cannot be read back. The only live signals are CCs: knobs
transmit when moved, and the synth accepts CCs in. So the app treats **its own state as the
truth**: on every (re)connect it pushes all 54 CC parameters to the device, then knob twists
stream in and win over the page. Unplug and replug freely; it reconnects and re-pushes by itself.

The S-1 **is** class-compliant USB audio over the same cable (a 2-in "S-1" device in CoreAudio).
The app routes it to the default output, which is how you hear it with no DAW.

## Save to S-1 (patterns into the hardware)

Patterns transfer through USB disk mode, not MIDI. The Library builds a device-ready
`S1_PTN<bank>-<slot>.PRM` from the live patch and sequence (the format is community-decoded
plain text; the writer templates off a real device dump so it never invents keys):

1. Power the S-1 off. Hold **Play** while you power it on.
2. Wait one to two minutes for a drive named `S-1` (the app notices, and can write the file into
   `RESTORE/` for you; otherwise download it and copy it yourself).
3. Press **Hold** on the S-1, wait for `dOnE`, then power-cycle it.

It also reads the other way: **Load from the S-1** lists the mounted device's saved patterns and
loads one, patch and sequence, live and editable.

## The agent door

Everything the page does is a documented endpoint (interactive docs at
`http://127.0.0.1:8766/docs`): read or set any parameter, patches, sequences, transport, notes,
device patterns, `.prm` export, and the twin matcher over `/ws/match`. Live state, including
physical knob twists, streams over `/ws/state`.

```bash
curl -X PUT localhost:8766/api/params/74 -H 'content-type: application/json' -d '{"value": 90}'
curl -X POST localhost:8766/api/notes -H 'content-type: application/json' -d '{"note": 60, "on": true}'
```

## Commands

```bash
s1                  # the page (opens the browser); same as `synth`
s1 --no-browser     # just the server, on 127.0.0.1:8766
s1 --list-ports     # the MIDI ports the Mac sees
synth-match         # the matcher's command line
synth-prm           # read and write .PRM pattern files
```

## Development

```bash
/usr/local/bin/python3.12 -m venv .venv          # Homebrew python3 may be newer than the deps support
.venv/bin/pip install -e ".[studio,twin,dev]"
.venv/bin/python -m pytest                      # the whole suite runs on fake MIDI and audio: no hardware
.venv/bin/ruff check .
node synth/web/static/design/kit.check.mjs      # front-end checks live next to their modules (*.check.mjs)
.venv/bin/python tools/build_site.py            # the static page (browser twin only) into site/
```

Layout: `synth/schema.py` and `synth/data/s1.json` (every parameter, audited against the official
MIDI chart), `synth/engine.py` (port watcher, sync, keyboards), `synth/audio.py` (S-1 USB audio to
speakers), `synth/match/twin.py` (the differentiable model), `synth/match/twin_session.py` (the
streaming matcher), `synth/web/` (FastAPI) and `synth/web/static/` (the one front-end: the design
kit in `design/`, the browser twin in `twin/`, views in `views/`). The design direction lives in
`docs/design/`.

## Credits

MIT licensed. Fonts: Old Standard TT and Libre Franklin, under the SIL Open Font License (texts in
`synth/web/static/design/fonts/`).
