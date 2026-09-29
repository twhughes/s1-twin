# BUILD.md — the cyanotype build contract (v1: the S-1 twin you can play and teach)

*Written 2026-09-27 by the lead (Claude, art director); updated the same day after integration. Read `DIRECTION.md` first: it is the design law.
This file is the engineering law for the parallel build. Where they disagree, ask the lead; do not guess.*

## 0. What we are building

**One UI, three contexts.** The same front-end in `synth/web/static/` runs:

| Context | How it starts | Sound comes from | Match |
|---|---|---|---|
| **Cockpit + S-1** | `s1` (the cockpit server), S-1 plugged in | the S-1 (notes and CCs go to hardware) | twin matcher on the server |
| **Cockpit, no S-1** | `s1`, nothing plugged in | the **browser twin** (Web Audio, same model as `twin.py`) | twin matcher on the server |
| **Static page** | GitHub Pages, no server (phase D, later) | the browser twin | recorded matches, replayed |

The product sentence: *a software S-1 you can play in the browser, that learns any sound by gradient descent,
and that syncs to a real S-1 when you plug one in.*

"Twin" is a claim. It is only honest if the browser twin runs **the same model** as `synth/match/twin.py`
(same curves, same oscillator/ladder/envelope equations). Until the hardware calibration session runs, the UI
says the curves are **uncalibrated** where it matters (the Output caption and the README), and never claims
hardware accuracy.

## 1. Ownership — touch only your files

| Owner | Files (create or edit) | Must not touch |
|---|---|---|
| **lead** | `docs/design/*`, `synth/web/static/design/**`, the router `include` lines in `synth/web/server.py`, integration merges | — |
| **W-twin** | `synth/web/static/twin/**`, `tools/twin_render.mjs`, `tests/test_twin_parity.py`, `tests/test_twin_curves.py`, one new function `export_curves()` in `synth/match/twin.py` (additive only) | the rest of `twin.py`, anything else |
| **W-plate** | `synth/web/static/{index.html,app.js}`, `synth/web/static/core/**`, `synth/web/static/views/synth.js`, `synth/web/static/drawers/**`, `synth/web/static/keyhint/**`, `synth/web/plate_routes.py`, additive methods in `synth/audio.py`, `tests/test_plate_*.py`, `tools/export_schema.py` | `views/match.js`, `views/sequencer.js`, `twin/**`, `design/**` |
| **W-match** | `synth/match/twin_session.py` (new; `session.py` is the hardware CMA-ES matcher), `synth/web/match_ws.py`, `soft/server.py` (refactor onto `session.py`), `synth/web/static/views/{match.js,sequencer.js}`, `tests/test_match_session.py`, `tests/test_match_ws.py` | `index.html`, `app.js`, `core/**`, `design/**` |
| **W-hardware** | `synth/match/calibrate_cli.py` (new), `synth/web/eartest.py`, `synth/web/static/eartest/**`, `tools/eartest_report.py`, `tests/test_calibrate_cli.py`, `tests/test_eartest.py`, `[project.scripts]` lines for its two commands in `pyproject.toml` | everything else |

If you need a change in a file you don't own, write it down in your final report as a **request** with the
exact diff. The lead applies it.

## 2. Interfaces (the seams — build to these, even before the other side exists)

### 2.1 Design kit — `synth/web/static/design/` (lead; ready before W-plate and W-match start)

- `tokens.css` — custom properties `--field --deep --lift --ink --ink-2 --ink-3 --ink-4 --bronze --serif --sans`,
  `@font-face` for the vendored fonts, base `body` (field + sun-print texture), `:focus-visible`, reduced motion,
  and the shared classes: `.caption .note .well .row .pair .quiet .pill .linkish .drawer .scrim .toast`.
- `knob.js` — `knob({label, min, max, value, def, bipolar=false, size=50, onInput}) → {el, set(v, {source}),
  get(), trail(from, to), modeled(bool)}`. Drag vertical, wheel, double-click = default, arrows/PageUp/Home/End,
  `role="slider"`. `set(v, {source:"midi"})` draws the bronze trail (the hardware's hand).
- `seg.js` — `seg({label, options:[{value, label, glyph?}], value, onInput}) → {el, set(v), get()}`; `GLYPHS` has
  the six LFO shapes.
- `draw.js` — `fit(canvas)`, `axis()`, `stroke(ctx, pts, {color, width, glow, dash, halo, reveal, w, h})`,
  `wavePts()`, `plumePts()`, `hatchShape()`, `playhead()`, `keyUpMark()`, colors `INK INK2 INK3 INK4 BRONZE`.
- `colors.js` — Tyler's pitch palette: `rgbOf(pc)`, `PC_NAMES`, `lum(rgb)`, `rgba(rgb, a)` (vendored, provenance).
- `brand.js` — `NAME` (the one place the product name lives; working title until Tyler names it), `TAGLINE`.
- `mark.svg`, `favicon.svg`.

### 2.2 The app context — `core/ctx.js` (W-plate owns; W-match builds against it)

```js
ctx.schema                 // /api/schema JSON (server) or core/schema.json (static)
ctx.params                 // Map cc -> value (0..127), the single client-side source of truth
ctx.set(cc, v, {source})   // source: "ui" | "midi" | "patch" | "match"; updates params, notifies, sends upstream
ctx.on("param", fn)        // fn({cc, value, source}) — every change from any source
ctx.on("status", fn)       // fn(status) — {sync: "synced"|"pending"|"offline", port, monitor, keyboards, mode}
ctx.note(n, on, vel=100)   // routes to the S-1 when synced, else to the browser twin
ctx.twin                   // the browser twin (2.3), always present
ctx.server                 // null in static mode; else {api(method, path, body), ws(path) -> WebSocket}
ctx.toast(msg)
ctx.soundSource            // "s1" | "twin"
```

View modules (`views/*.js`) export `{id, title, mount(root, ctx), unmount()}`. The shell (`app.js`) owns the
header, nav, drawers, and routing (`#synth`, `#sequencer`, `#match`). Views never import each other.

### 2.3 The browser twin — `twin/audio.js` (W-twin owns; W-plate uses)

```js
const twin = await createTwin({curves})      // curves = twin/curves.json unless calibrated curves are served
twin.set(cc, v) / twin.setAll({cc: v})       // CC space 0..127 — the same numbers the S-1 uses
twin.noteOn(note, vel) / twin.noteOff(note) / twin.allOff()
twin.resume()                                // call from a user gesture
twin.taps                                    // {osc, filter, amp, fx, out}: AnalyserNode per stage (live windows)
await twin.renderStages({note, seconds, gate}) // {sr, osc, filter, amp, fx, out}: deterministic; gate = key-up time in s
twin.modeled(cc)                             // true if twin.py models this CC (what the matcher can fit)
twin.audible(cc)                             // true if it changes what the browser twin plays; the UI dims only !audible
twin.support(cc)                             // 'model' | 'voice' | 'extra' | null
twin.level()                                 // output RMS 0..1
```

`twin/dsp.js` is **pure** (no DOM, no Web Audio; runs in node and inside the AudioWorklet). It implements
`twin.py`'s `render()`: band-limited saw/pulse/sub + noise → 4-pole ladder (cutoff in log2 space, env and LFO
modulation, key follow, soft bounds) → ADSR or gate VCA, LFO to pitch. Same curves (from `curves.json`, exported
by `twin.export_curves()`; a drift test asserts JSON == `DEFAULT_CURVES`). Effects (delay, reverb, chorus) are
**browser-only extras** outside the twin; `modeled()` returns false for them.

**Parity gate:** `tests/test_twin_parity.py` renders ≥6 fixed patches × 3 notes with `node tools/twin_render.mjs`
and with `Twin.render`, and asserts `logmel_loss(js, py)` stays under a threshold that you justify in the test
docstring (relative to the loss between two different patches). Skip cleanly if `node` is missing.

### 2.4 The twin matcher over WebSocket — `/ws/match` on the cockpit server (W-match)

Same phases and frame schema as `soft/server.py` documents, with one change: every frame's candidate is in
**CC space** — `"cc": {cc: value}` on every frame (not only `done`), so any knob in the UI can animate. Query
params as today (`throttle`, `notes`, `quality`, `init` — `init` becomes a CC map). `soft/server.py` keeps its
page-unit frames by converting from the shared session, and its tests stay green.

Frames also carry `wave` (the candidate's cycle, for the plume) on every frame and `target_wave` on
pitch and done.
Round 4 adds, all optional so old frames and recorded runs still read the same: `trying` (plain words, the
switch or LFO setting a re-descent tries), `starts` (how many starts the preset makes) on gd frames, and
`finished: true` on a done frame that a `"finish"` text message ended early (the search stops at the next
step and still renders, scores and returns the best so far). `quality` takes `quick`, `thorough` or `deep`. **Recorded match format** (for the static page): `matches/<slug>.json` =
`{"title", "target_url", "notes", "frames": [<the exact WS frames>], "recorded": "<ISO date>", "engine": "twin <git sha>"}`.
`views/match.js` must be able to replay one of these with no server. `matches/index.json` lists them:
`[{"slug", "title", "notes", "recorded", "closeness"}]` (ship `[]` when there are none).

### 2.5 New server routes

Routers are pre-wired in `server.py` (lead): `plate_routes.router` (W-plate), `match_ws.router` (W-match),
`eartest.router` (W-hardware). Add routes only inside your router module. Known need: W-plate adds
`GET /api/monitor/raw?n=2048` → `{running, sr, samples:[float]}` (the last n samples, for the live plume) via an
additive `AudioMonitor.scope_raw(n)` in `synth/audio.py`, and `GET /api/twin/curves` serving the curves the
twin should use (calibrated if `~/.synth/twin/curves.calibrated.json` exists, else the defaults).

W-rec (round 2) adds `POST /api/match/record-note` in `match_ws.router`: `{note, velocity, hold, tail}` →
`audio/wav`, one note of the S-1's current sound from the running monitor (`SynthDriver.probe(None)`); 409 and
a plain `detail` when the S-1 cannot play it now.

W-sys (round 13) adds this Mac's own sound as a target in `match_ws.router`: `GET /api/match/system/sources`
(every app; Logic Pro while it runs), `POST /api/match/system/start` `{app: null | "com.apple.logic10"}` and
`POST /api/match/system/stop` → `audio/wav`. The recording is a Core Audio process tap in a small Swift helper
(`synth/native/systap.swift`, built on first use into `~/.synth/bin/`); 409, 422 and 503 carry a plain `detail`.

## 3. Design rules (from DIRECTION.md, restated as checks)

1. Colors come only from `tokens.css`. Hues appear only for pitch (`colors.js`) and the hardware (`--bronze`).
2. No card grid, no gradients, no drop shadows. Glow only on live traces.
3. Old Standard TT at 18 px and up only; Libre Franklin for everything small; tabular figures for values.
4. Sentence case. Plain verbs. No all-caps labels. No "→" on buttons. Errors say what happened and what to do.
5. `index.html` never names a parameter or carries `data-cc` (the existing test enforces it): the layout comes
   from `/api/schema` through `core/layout.js`, and **every** schema parameter lands somewhere (unknown ones go to
   the Settings drawer), which a node check asserts.
6. No runtime requests off the machine (fonts are vendored). Works at 390 px wide with no sideways scroll.
   Visible keyboard focus. `prefers-reduced-motion` respected.

## 4. Verification — every worker, before reporting

- `cd <your worktree> && <repo>/.venv/bin/python -m pytest -q` → all green
  (the 638 existing tests plus yours). Run it from your worktree root so your code shadows the editable install.
- Pure JS modules get node checks: `*.check.mjs` next to the module, run by `node <file>`, exit code 0 = pass.
- UI work: start the cockpit on **your** port (`SYNTH_PORT=<port> …/.venv/bin/synth --no-browser`, or
  `--port`), screenshot it with headless Chrome and a throwaway profile, and look at the screenshot:
  `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --user-data-dir=$(mktemp -d)
  --window-size=1440,1000 --virtual-time-budget=5000 --screenshot=out.png http://127.0.0.1:<port>/`
  (wrap it in `perl -e 'alarm 45; exec @ARGV'` — headless Chrome can hang on open sockets — and kill leftovers).
  Ports: W-twin 18101, W-plate 18102, W-match 18103, W-hardware 18104.
- **Never** open a visible browser window, never push, never touch `main`.
- Commit on your branch with a message ending `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## 5. Report back (final message, short)

What you built (files), what the checks say (counts), screenshots you looked at (paths), what you could not do
and why, and any **requests** for files you don't own (exact diffs).
