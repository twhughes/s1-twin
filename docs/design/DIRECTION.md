# Design direction — Menura

*2026-09-27. Art director: Claude, at Tyler's request ("be artistic director").
Status: **direction approved** (Tyler, same day: "otherwise it's awesome"). **Name parked**: he doesn't love
Menura, Lyrebird, Plume, or Sunprint; the name waits until scope is set (one S-1 app, or a family with the
music trainer and sketchpads). Nothing here is built into the app yet.*

Comp: `docs/design/menura-comp.html` (open it as a file; no server). Review flags in the URL hash:
`#play` (a held note), `#connected` (the S-1 plugged in), `#trail` (a knob the hardware moved),
`#library`, `#settings`, `#still` (no load animation). Combine them with `&`.

## Name (parked; first proposal kept for the record)

**Menura** — the genus of the lyrebird. The lyrebird copies any sound it hears: chainsaws, camera
shutters, other birds. This app learns to make any sound you play it on your S-1. The tagline stays
plain and descriptive: *A software twin for the Roland S-1.*

- Fallback: **Lyrebird** (instantly clear, but a known AI voice-cloning brand used it).
- Not checked yet: name collisions on GitHub and PyPI. That check needs a web search (Tyler's per-time OK).
- Roland and S-1 appear only as descriptive words ("for the Roland S-1"), never in the name or the logo.
  The README gets a "not affiliated with Roland" line.

## Concept: the cyanotype specimen plate

The S-1, studied the way a naturalist studies a bird. The page is a sun print: Prussian-blue field,
paper-white line art. The specimen is the signal. One line crosses the plate and shows the sound's
shape after every stage. The digital twin is a photogram: a same-size print of the real thing.

## Tokens

| Token | Value | Use |
|---|---|---|
| field | `#0E2A52` | the page |
| deep | `#081C3A` | wells, the keyboard bed |
| ink | `#EEF3FA` | lines, text |
| faded ink | `#A3B9DA` | secondary text, idle options |
| bronze | `#E6A94F` | the hardware, and nothing else |
| pitch colors | Tyler's 12 (from `music/web/static/colors.js`) | only while a note sounds |

Type: **Old Standard TT** (19th-century scientific books) for the wordmark, stage names, and headings,
italic, 18 px and up. **Libre Franklin** for controls and values, with tabular figures. Both are OFL;
the build vendors the files (no Google request at runtime).

Lines: 1.25 px hairlines, 1.8 px pointers, dotted leaders. No gradients, no drop shadows, no cards.
The only glow is on live traces.

## Five rules

1. **See the sound.** Every stage has a window. A knob's effect shows in its window at once.
2. **Color means pitch; bronze means the hardware.** A held note tints the whole chain in its color.
   A knob the S-1 moved leaves a bronze trail. Nothing else gets a hue.
3. **Instrument first.** Only the controls that shape the sound are on the plate. The patch library,
   Save to S-1, the settings menu, and MIDI live in two drawers.
4. **Leaders show routing.** A dotted line runs from each modulator (LFO, Envelope) to each control it
   drives. Its weight is the amount.
5. **Words say what things do.** Sentence case, plain verbs, no jargon where a word exists
   ("Width set by", "Restart on key", "Volume shape").

## Signature elements

- **The signal line**: Oscillator → Filter → Amplifier → Effects → Output, a window at each stage.
- **The plume**: the output drawn as a phase portrait. Each loop is one cycle; sharper turns mean a
  brighter tone. When the S-1 is connected, the real signal (bronze) and the twin's prediction
  (dotted) share the window. Their agreement is the twin's accuracy, made visible.
- **The mark**: the lyrebird's tail in display, read as a lyre whose strings are sine, saw, and square.
- **The load**: the plate develops once, like a sun print, and the signal line draws itself.

## Scope and build (decided 2026-09-27)

Tyler: publish **the synth first** — "an S-1 twin synth that has an autodiff matching feature". The flashcard
trainer (`music`) comes later. chordbox and loopbox are out (not in use).

v1 is one repo (today's public `twhughes/s1tui`, renamed once a name lands) and one page at
tylerwhughes.com/*name*:
- **Anyone:** play the twin in the browser (computer keys or a MIDI keyboard); install it locally to drop in a
  sound and watch gradient descent find the knobs. The static page cannot run the matcher, so it replays
  real recorded matches.
- **S-1 owners:** the cockpit as well — every knob synced both ways, the sequencer, patches, Save to S-1.

Order:
1. Merge `chassis-hardening` into `main`. The twin and `soft/` exist only on that branch. (Tyler's OK.)
2. Hardware and ears session with the real S-1 (about 90 minutes of Tyler): the plug-in check, calibration
   probes through `twin.calibrate()` (the default curves are uncalibrated stand-ins today), and the
   "which is closer?" ear test that FABLE rule 1 requires before any match is trusted. This makes "twin" true.
3. Build, in parallel with step 2: the cockpit and `soft/` in this design language, the public page with
   recorded-match replays, the README (with the "not affiliated with Roland" line), and a demo video.
4. Name, repo rename, push, GitHub Pages. (Tyler's OK for each outward step.)

## Open

- Tyler's verdict on the name and the direction.
- Sequencer and Match views are not drawn yet. They follow the same rules.
- The comp's waveforms come from a small model written for the page, not the real twin.
