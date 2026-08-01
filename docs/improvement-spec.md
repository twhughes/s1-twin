# s1tui Improvement Spec

Implementation spec derived from a full-codebase analysis (2026-07-01). Work through
phases in order — Phase 1 items are bugs that corrupt data or crash; later phases are
quality and tooling. Each item lists the files involved, the fix, and how to verify.

Run the full suite (`.venv/bin/python -m pytest -q`, 199 passing at time of writing)
after every item. Add the new tests called out in each item as you go, not at the end.

---

## Phase 1 — Correctness & safety bugs

### 1.1 Make `MidiBackend` thread-safe and error-tolerant

**Files:** `s1tui/midi_backend.py`, `s1tui/sequencer_engine.py`, `s1tui/app.py`

**Problem:** Three threads call `_output.send()` concurrently with no lock: the UI
thread (`send_cc` via `_on_param_changed`, `app.py:247`), the sequencer thread
(`sequencer_engine.py:121,149`), and the test-note worker (`app.py:406-411`). mido
ports are not thread-safe; concurrent sends interleave bytes. Additionally, no
`send()` call anywhere is wrapped in error handling — if the device disappears
mid-playback, the sequencer thread dies silently, `_playing` stays `True`, and notes
are left stuck on.

**Fix:**
- Add a `threading.Lock` in `MidiBackend`; acquire it around every `_output.send()`
  (`send_cc`, `send_note_on`, `send_note_off`, `send_start`, `send_stop`).
- Wrap sends so a port failure raises a single well-defined exception (or returns
  False) instead of propagating raw rtmidi errors; on failure, mark the backend
  disconnected so the UI can show "No MIDI" instead of crashing.
- In `SequencerEngine._run`, catch send failures: stop playback cleanly, clear
  `_playing`, and attempt note-offs for `_active_notes`.
- Replace `MidiBackend.__del__` (`midi_backend.py:128-129`) with explicit `close()`;
  `disconnect()` must send all-notes-off (see 2.4) before closing the port.

**Verify:** unit test that concurrent `send_cc`/`send_note_on` from multiple threads
against a mock port serializes calls; test that a raising mock port stops playback
and clears `_playing` without an unhandled exception.

### 1.2 Guard shared sequence state during playback

**Files:** `s1tui/sequencer_engine.py`, `s1tui/widgets/piano_roll.py`

**Problem:** The engine thread iterates `seq.notes` (`sequencer_engine.py:120,145,102`)
while the UI thread mutates the same list via piano-roll edits
(`piano_roll.py:216,243,250,262`) — can raise "list changed size during iteration" or
drop note-offs. `pause()` (`sequencer_engine.py:86-91`) mutates `_active_notes` from
the main thread while `_run` reads/adds to it.

**Fix:** Take a snapshot (`list(seq.notes)`) at the top of each step in `_run` /
`_process_note_offs` / `notes_at_step`, or protect `notes` with a lock shared by the
piano roll. Protect `_active_notes` with a lock (or have `pause()` set a flag the
engine thread services, like `stop()` does via join).

**Verify:** test that toggles notes rapidly from one thread while the engine loop runs
with a mock backend; no exceptions, no stuck notes (every note-on has a matching
note-off after stop).

### 1.3 CMA-ES crash on mid-generation stop

**Files:** `s1tui/match/session.py:146-163`

**Problem:** The generation loop breaks early on `self._stop`, then calls
`opt.tell(solutions[:len(losses)], losses)` with a partial population.
`cma.CMAEvolutionStrategy.tell` requires the full lambda and raises. The existing
test passes only because it uses `RandomOptimizer`.

**Fix:** On early exit from a generation, skip `tell()` entirely (best-so-far tracking
already happened per-candidate). Only call `tell()` with a complete population.

**Verify:** test that constructs a session with `CMAESOptimizer` (small popsize, mock
driver) calls `stop()` mid-generation, and asserts `run()` returns cleanly.

### 1.4 Path traversal in web patch/recording names

**Files:** `s1tui/web/server.py:266-276`, `s1tui/web/state.py:156-164`,
`s1tui/patches.py`

**Problem:** Request-body `name` flows directly into `PATCH_DIR / f"{name}.json"` and
`RECORDING_DIR / f"{name}.wav"`. `{"name": "../../../.zshrc", "overwrite": true}`
writes an arbitrary file.

**Fix:** Add a single `sanitize_patch_name(name)` helper (in `patches.py`): reject or
strip path separators and `..`, reject empty results, then belt-and-braces verify
`path.resolve()` is inside the target dir before writing. Use it in `save_patch`,
`delete_patch`, `load_patch`-by-name, and the recording path.

**Verify:** tests posting traversal names to `/api/patches/save` and record-stop
expect 400 and assert no file was created outside the bank dir.

### 1.5 Web: escape patch names, add origin check, cap upload size

**Files:** `s1tui/web/static/app.js:341-346`, `s1tui/web/server.py`

**Problem:** (a) `refreshPatches` interpolates `p.name` into `innerHTML` and into
`data-*` attributes unescaped — stored XSS. (b) No CORS/origin policy: any web page
can drive `localhost:8766` (DNS rebinding / CSRF). (c) `/api/target` reads the whole
upload into memory with no size cap (`server.py:201-208`).

**Fix:**
- Build patch rows with `document.createElement` + `textContent` (no innerHTML for
  user data).
- Add middleware rejecting requests whose `Host`/`Origin` isn't
  `127.0.0.1:8766`/`localhost:8766` (allow missing Origin for same-origin GETs).
- Reject uploads over ~50 MB (check `Content-Length` and/or read in chunks).

**Verify:** test that a request with `Origin: https://evil.example` gets 403; test
that an oversized upload gets 413.

### 1.6 Propagate match-thread errors; validate start params

**Files:** `s1tui/web/state.py:191-211`, `s1tui/web/server.py:60-68,213-227`,
`s1tui/web/static/app.js`

**Problem:** `_run` executes calibrate/run on a thread with no try/except and no error
channel — any exception (including an invalid `optimizer` string, which is never
validated) kills the thread silently and the frontend shows "running…" forever. Also
the `if self.running` check-then-spawn in `start_match` is not under `self._lock`
(double-start race).

**Fix:**
- Wrap the thread body; on exception store `last_error` and emit a final progress
  tick with `done=True, error=str(e)` so the WS client can display it.
- Validate `optimizer` ∈ {cmaes, random}, `mode` ∈ {auto, interactive}, and
  `max_iters`/popsize bounds in `StartReq` (pydantic `Literal`/`Field`), returning 422
  before any thread starts.
- Move the running-check and thread assignment under `self._lock`.
- Frontend: render the error state (stop spinner, show message).

**Verify:** test that a driver raising in `run()` results in a `done` tick containing
an error and `running=False`; test that `optimizer="bogus"` returns 422; test that two
concurrent starts yield one 200 and one 409.

---

## Phase 2 — Sequencer musicality

### 2.1 Live tempo + drift-free scheduling

**Files:** `s1tui/sequencer_engine.py:99-140`

**Problem:** `step_duration` is computed once before the loop, so BPM changes during
playback do nothing; and the loop sleeps a fixed interval *after* doing per-step work,
so processing time accumulates as tempo drift.

**Fix:** Recompute step duration each step from `self.sequence.bpm`; schedule against
an absolute clock: `next_tick += step_duration; self._stop_event.wait(max(0, next_tick
- time.monotonic()))`.

**Verify:** test with a mock clock (or generous real-time tolerance) that changing
`sequence.bpm` mid-playback changes inter-step spacing; test that N steps complete in
~N×duration, not N×(duration+overhead).

### 2.2 Fix same-pitch overlap note-off handling

**Files:** `s1tui/sequencer_engine.py:23,122,142-156`

**Problem:** `_active_notes` is a set of bare pitches, so overlapping same-pitch notes
kill each other early; `_process_note_offs` rescans all notes every step with fragile
wrap-around math.

**Fix:** Track active notes as `(pitch, off_step)` entries (or note identity).
Precompute a `step → notes to turn off` map once per pattern (invalidate on edit) and
consult it per step.

**Verify:** test with two same-pitch notes where one sustains across the other's
note-off; the sustaining note must not be cut.

### 2.3 Wire up (or remove) the dead sequencer params

**Files:** `s1tui/schema.py:177-215`, `s1tui/sequencer_engine.py`, `s1tui/app.py:261-269`

**Problem:** `last_step`, `seq_gate`, `seq_shuffle`, `master_prob`, `seq_scale`, and
both ARP params render as cards but the engine reads none of them — only tempo is
wired.

**Fix (in value order):**
1. `last_step` — engine wraps at `min(seq.steps, last_step)`.
2. `seq_gate` — scale each note's off time by gate fraction.
3. `seq_shuffle` — delay even-numbered steps by the swing fraction of a step.
4. `master_prob` — per-step random skip (seedable for tests).
5. `seq_scale` / ARP params — implement or remove the cards; do not leave dead UI.

**Verify:** unit tests per behavior against a mock backend with a controllable RNG.

### 2.4 Panic key + clean disconnect

**Files:** `s1tui/midi_backend.py`, `s1tui/app.py`

**Fix:** Add `MidiBackend.all_notes_off()` (CC 123 on the active channel, plus
explicit note-offs for engine `_active_notes`). Bind it to a key (suggest `!` or
`Escape`), call it from `disconnect()` and on app exit. Document in README key table.

**Verify:** test that disconnect sends CC 123 before closing the port.

### 2.5 Save piano-roll edits to MIDI

**Files:** `s1tui/app.py`, `s1tui/sequence.py:117`, `s1tui/screens/`

**Problem:** `save_midi` exists and is imported but nothing calls it — edits are lost.

**Fix:** Add a save binding (suggest `M` / shift-m, next to `m` for load) opening a
filename prompt (reuse the patch-save modal pattern in `screens/patch_save.py`),
writing via `save_midi`. Update README key table.

**Verify:** integration test: load → edit a note → save → reload → sequences equal.

---

## Phase 3 — Match engine quality

### 3.1 Silence penalty

**Files:** `s1tui/match/capture.py:150-153`, `s1tui/match/session.py`

**Problem:** A silent candidate (closed VCA — common early in a search) yields all-zero
features that can score a *low* loss against quiet targets, rewarding silence.

**Fix:** In the evaluation path, if the captured clip's peak is below an absolute floor
(e.g. −60 dBFS), skip feature extraction and assign a large fixed penalty loss.

**Verify:** test that a zeros clip gets the penalty and is never recorded as best.

### 3.2 Calibrate with a known-bright patch

**Files:** `s1tui/match/driver.py:98-115`

**Problem:** `calibrate()` measures latency using whatever patch is on the synth; a
slow-attack patch mis-measures latency and misaligns every subsequent probe.

**Fix:** Before the calibration note, `apply()` a fixed calibration patch (max VCF
cutoff, fast/gate amp env, full level); restore or proceed as normal after (the next
candidate apply overwrites anyway).

**Verify:** test asserting `calibrate()` sends the calibration CCs before `note_on`.

### 3.3 Evaluation cache

**Files:** `s1tui/match/session.py:143-160`, `s1tui/match/space.py`

**Problem:** Discrete snapping in `space.decode` means CMA repeatedly samples vectors
that decode to already-probed CC dicts; each redundant probe costs ~2 s of hardware
time.

**Fix:** Memoize `frozenset(cc_dict.items()) → loss` in the session; on cache hit,
reuse the loss without touching hardware. Log hit count in progress info.

**Verify:** test with a mock driver counting probes: duplicate decoded candidates
probe once.

### 3.4 Probe robustness (device errors, empty best)

**Files:** `s1tui/match/capture.py:102-115`, `s1tui/match/session.py:106-113`,
`s1tui/match/cli.py:117-128`

**Fix:**
- Wrap per-probe record/wait in try/except: retry once, then assign the penalty loss
  (3.1) and continue the run instead of losing all progress.
- `apply_best()`/CLI save: if `_best_params` is empty, print a warning, do not save,
  exit nonzero.
- CLI: wrap the session in `try/finally` so `midi.disconnect()` always runs.

**Verify:** mock driver that raises on probe N: run completes, probe N gets penalty;
empty-best run exits nonzero and writes no patch file.

### 3.5 Normalize distance terms

**Files:** `s1tui/match/distance.py:42-59`

**Problem:** Terms mix L1 mean, RMS, and an arbitrary `/4.0` MFCC divisor, so the
`Weights` don't express real relative importance.

**Fix:** Normalize each term by a reference scale (e.g. the same term computed between
the target and silence, or fixed empirical constants documented in code) so each term
is O(1) before weighting. Remove the magic `/4.0`. Keep the default *effective*
weighting roughly equivalent (re-tune weights once, note values in a comment).

**Verify:** existing distance tests updated; add a test that each term for
(target vs target) is 0 and (target vs silence) is ~1.

### 3.6 Optional (do last, only if desired): seeding + early stop

- Plumb an existing bank patch through as `x0` (`session.py:120` — optimizer already
  accepts it): CLI flag `--seed-patch NAME`, web dropdown.
- Add `target_closeness` early-stop to the session loop (`session.py:130-137`).
- Multi-note matching (average loss over e.g. C2/C3/G3) — bigger change, spec
  separately if wanted.

---

## Phase 4 — Web app lifecycle & UX

### 4.1 Per-match WebSocket state reset

**Files:** `s1tui/web/server.py:313-327`, `s1tui/web/state.py`

**Problem:** `sent_target` / `last_best_loss` are per-connection, but the client keeps
one WS across matches — second match never re-renders the target spectrogram and
best-spec updates are gated on the previous match's minimum.

**Fix:** Add a monotonically increasing `match_id` (or `started_at` token) to
`AppState`; include it in ticks; reset `sent_target`/`last_best_loss` in the WS loop
whenever it changes.

**Verify:** WebSocket test (TestClient supports `websocket_connect`): run two mock
matches over one connection; assert the target spec is sent again for the second.

### 4.2 Misc server fixes

**Files:** `s1tui/web/server.py`, `s1tui/web/state.py`

- `read_level` (`state.py:77`) returns peak as `rms_db` — return actual RMS from the
  monitor.
- Replace deprecated `@app.on_event` with a lifespan handler; shutdown must call
  `STATE.stop()` (stop a running match) as well as `stop_monitor()`.
- Don't map every exception to `HTTPException(400)`; let unexpected errors be 500s and
  reserve 400/422 for validated client errors. Reuse `_as_device` instead of the two
  inline copies (`server.py:117-118,128-129`).
- Guard `_best_clip` read (`state.py:234`) with the same lock as `latest`.

### 4.3 Frontend polish

**Files:** `s1tui/web/static/app.js`

- Show a visible "connection lost" state when the WS drops (and cap/backoff the 1 s
  reconnect loop).
- Pause the 200 ms `/api/level` polling when the Setup tab is hidden
  (`document.visibilityState` / tab switch).
- `playClip`: stop the previous `Audio` before starting a new one.

---

## Phase 5 — Architecture cleanup (TUI)

Lower urgency; do after Phases 1–2 since those touch the same files.

- **Remove or adopt the dead `ParamState` listener API** (`state.py:17-23,40-43` —
  never called). Prefer adopting it: `app.py` currently hand-syncs widget dicts in
  four places (`app.py:283-292,437-441`); routing all param changes through listeners
  removes the duplicated sync paths.
- **Single transport source of truth:** drop `self._transport_playing`
  (`app.py:88,180,372,388,400`) in favor of `engine.playing`.
- **De-duplicate:** resolution parsing (`sequence.py:54-62` vs
  `sequencer_engine.py:158-172` — one shared helper); `label_for_value`
  (`schema.py:39-48` vs `167-174` — one mixin/function); section grouping (three
  reimplementations across `menu_view`, `panel_view`, `schema.params_by_section`).
- **Evaluate deleting `params.py`** (`params.py:11-54` backward-compat shim; check
  remaining importers first — `tests/test_params_compat.py` exists).
- **Undo for destructive actions:** before randomize/zero/defaults
  (`app.py:353-367`), push the current 54-param snapshot onto a small undo stack;
  bind `u` to restore (and send the CCs). Cheap insurance, no confirm dialog needed.
- **Narrow the `except Exception: pass` blocks** (`app.py:133-135,148-168,340-347`,
  `cards.py:47-50`): catch the specific expected exception, and surface connection
  failures in the status bar instead of swallowing them.
- Remove dead imports in `app.py:17,20` (`param_by_cc`, `SEQ_PARAMS`; `save_midi`
  becomes live in 2.5).
- `load_midi` (`sequence.py:85-104`): honor overlapping same-pitch notes per track
  (key `pending` by (track, pitch)), and count+report notes dropped for exceeding
  `max_steps` instead of silently discarding.

---

## Phase 6 — Tooling & test hygiene

### 6.1 Packaging

**Files:** `pyproject.toml`

- Add `[project.optional-dependencies] dev = ["pytest>=8", "ruff>=0.4", "httpx"]`
  (httpx is required by fastapi TestClient) — document
  `pip install -e ".[studio,dev]"` in README.
- Add a `[tool.ruff]` section (defaults are fine; enable `E,F,I`).

### 6.2 CI

**Files:** `.github/workflows/ci.yml` (new)

- GitHub Actions: on push/PR, matrix Python 3.10 and 3.12, `pip install -e
  ".[studio,dev]"`, run `ruff check` and `pytest -q`. Linux needs
  `sudo apt-get install -y libasound2-dev` for python-rtmidi.

### 6.3 Test hygiene

**Files:** `tests/test_web_api.py`, new tests from Phases 1–4

- Add an autouse fixture that resets the module-global `STATE` singleton between
  tests (currently mutated at `test_web_api.py:88,99` with no reset — order-dependent).
- Ensure the new tests from earlier phases cover: traversal names, origin rejection,
  oversized upload, invalid optimizer 422, double-start, match-thread error tick,
  WS second-match reset, save-overwrite 409, `/api/clip/{which}` 404, corrupt-audio
  400 on `/api/target`.

---

## Out of scope (noted, not spec'd)

- SysEx patch read-back from the S-1 (hardware reportedly exposes no bulk dump; the
  editor stays write-only).
- Multi-note/velocity matching and CMA restart strategies (spec separately if the
  single-note matcher plateaus).
- In-app MIDI channel switching UI (CLI flag exists; low value until requested).
- Frontend rewrite/modularization of `app.js` (409 lines is tolerable; revisit if it
  grows).
