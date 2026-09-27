// core/transport.js — one transport for the whole page (docs/design/ROUND2.md §2). The shell creates it
// once and attaches it as ctx.transport, so a pattern keeps playing while you switch views and turn
// knobs: on the browser twin when no S-1 sounds, with the plate's keys lit as it goes. It owns the
// sequence and the engine; the Sequencer view is its UI (it edits `seq`, then calls edited()).
//
//   ctx.transport.state        {playing, paused, position, steps, bpm, step_resolution} (a fresh copy)
//   ctx.transport.seq          {steps, bpm, step_resolution, notes}: the ONE copy the view edits
//   ctx.transport.perf         {gate, shuffle, probability, clock}
//   ctx.transport.play() / pause() / stop() / toggle()   toggle: playing and not paused -> pause, else play
//   ctx.transport.on(fn) -> off   fn(state, what) on every change, position ticks included. `what` is
//                                 "transport", "position", "sequence" (replaced from outside: the app,
//                                 another tab, a loaded bank), "tempo", "poly" or "perf".
// Additive (the Sequencer view and the shortcuts use them):
//   poly                         steps that start more notes than the S-1 stores (the app's word, or ours)
//   edited({now})                the view changed seq: send it to the app (debounced unless now)
//   setTempo(bpm, {now}) -> bpm  20 to 300 BPM; nudgeTempo(delta) -> bpm (the − and = keys)
//   setSteps(n) -> n             the pattern length; notes past the end go, as the app does
//   setPerf({gate, shuffle, probability, clock})
//   replace(s)                   adopt a sequence the app answered with (a loaded bank)
//   hold(on)                     a drag is live: the app's echoes do not replace the notes under it
//   refresh()                    read the sequence and the performance settings again (server mode)
//
// "playing" means the steps run now; a paused pattern is {playing: false, paused: true}, as the app
// reports it (synth/sequencer_engine.py). Server mode (ctx.server): play/pause/stop go to
// POST /api/transport, one at a time, and the state follows the app through ctx.on("server", …) (the
// shell's one /ws/state). Each "position" step is voiced on the twin while ctx.soundSource is "twin";
// while the S-1 sounds the app already plays it, so nothing is voiced. Static mode: a local clock with
// the app's rules (Play resumes a pause; odd steps swing late by shuffle × a step; a step fires with
// `probability`; a note holds max(0.05, duration × gate) steps). Notes go through ctx.note(), so the
// keys light and the pitch halos show.
// Pure (no DOM): core/transport.check.mjs drives it with a fake clock and a fake app.

// ── the rules (the app's: synth/sequence.py, synth/sequencer_engine.py) ─────────────────
export const MAX_STEPS = 64;
export const MAX_NOTES_PER_STEP = 4;
export const TEMPO_MIN = 20;
export const TEMPO_MAX = 300;

/** Seconds per step for a tempo and grid ("1/16", or "16t" for sixteenth triplets). */
export function stepSeconds(bpm, res) {
  const b = Math.max(1, Number(bpm) || 120);
  const frac = /^(\d+)\/(\d+)$/.exec(String(res)), trip = /^(\d+)t$/.exec(String(res));
  let beats = 1;                                             // unknown grid: a quarter note (as the server)
  if (frac && +frac[2] > 0) beats = (4 * +frac[1]) / +frac[2];
  else if (trip && +trip[1] > 0) beats = (4 / +trip[1]) * (2 / 3);
  return (60 / b) * beats;
}

/** Steps that start more notes than the S-1 can store (four per step). */
export function polySteps(notes, max = MAX_NOTES_PER_STEP) {
  const count = new Map();
  for (const n of notes) count.set(n.step, (count.get(n.step) || 0) + 1);
  return [...count].filter(([, c]) => c > max).map(([s]) => s).sort((a, b) => a - b);
}

/** How long a new or dragged note may be at `step` (1 .. to the end of the pattern). */
export const clampDuration = (d, step, steps) => Math.max(1, Math.min(steps - step, Math.round(d) || 1));

/** What the sequencer fires at `step`: [{pitch, velocity, hold}] with hold in seconds (the server's
 *  rule: max(0.05, duration × gate) steps), or nothing when the probability roll fails. */
export function stepPlan(notes, step, { gate = 1, probability = 1, stepSec = 0.125 } = {}, rand = Math.random) {
  if (!(rand() < probability)) return [];
  return notes.filter((n) => n.step === step)
    .map((n) => ({ pitch: n.pitch, velocity: n.velocity, hold: Math.max(0.05, n.duration * gate) * stepSec }));
}

/** Swing: even-numbered steps (1-indexed; odd indices) start late by shuffle × a step. */
export const swingDelay = (step, shuffle, stepSec) => (step % 2 === 1 ? Math.max(0, shuffle) * stepSec : 0);

/** The steps as the server sees them (only the fields it accepts). */
export function sequencePayload(seq) {
  return { steps: seq.steps, bpm: seq.bpm, step_resolution: seq.step_resolution,
    notes: seq.notes.map((n) => ({ step: n.step, pitch: n.pitch, velocity: n.velocity, duration: n.duration })) };
}

/** What Space asks for: "pause" while the steps run (or while a play is on its way to the app),
 *  else "play" (from step 1, or on from a pause). `intent` is the last action not yet answered. */
export function toggleAction({ playing, paused }, intent = null) {
  const running = intent ? intent === "play" : Boolean(playing) && !paused;
  return running ? "pause" : "play";
}

/** The static page's clock, with the app's timing: steps in order from `next`, each on its grid time,
 *  an odd step late by shuffle × a step. The grid advances by the step length in force at each step,
 *  so a tempo change never jumps; a tick far behind (a throttled tab) restarts the grid instead of
 *  bursting the steps it missed. `later`/`cancel` are setTimeout/clearTimeout (a fake in the check). */
export function createClock({ now, later, cancel, stepSec, steps, shuffle, onStep }) {
  let timer = 0, next = 0, grid = 0, running = false;
  const wait = () => grid + swingDelay(next, shuffle(), stepSec()) * 1000 - now();
  function fire() {
    timer = 0;
    if (!running) return;
    const sec = stepSec(), n = Math.max(1, steps());
    if (now() - grid > 2 * sec * 1000) grid = now();
    if (next >= n) next = 0;
    const step = next;
    next = (step + 1) % n;
    grid += sec * 1000;
    onStep(step);
    if (running && !timer) timer = later(fire, Math.max(0, wait()));
  }
  return {
    get running() { return running; },
    get next() { return next; },
    /** Run from step 1 (`fromTop`) or on from where a pause left it. A step that is due fires at once. */
    start(fromTop) {
      cancel(timer);
      timer = 0;
      running = true;
      if (fromTop) next = 0;
      grid = now();
      const w = wait();
      if (w <= 0) fire(); else timer = later(fire, w);
    },
    /** Stop the steps; `reset` goes back to step 1. */
    halt(reset) {
      cancel(timer);
      timer = 0;
      running = false;
      if (reset) next = 0;
    },
  };
}

const systemClock = () => ({
  now: () => globalThis.performance.now(),
  later: (fn, ms) => globalThis.setTimeout(fn, ms),
  cancel: (t) => globalThis.clearTimeout(t),
});

// ── the transport ───────────────────────────────────────────────────────────────────────
export function createTransport(ctx, { clock = systemClock(), rand = Math.random, pushMs = 180, perfMs = 120 } = {}) {
  const { now, later, cancel } = clock;
  const server = ctx.server || null;
  const seq = { steps: 16, bpm: 120, step_resolution: "1/16", notes: [] };
  const perf = { gate: 1, shuffle: 0, probability: 1, clock: true };
  let playing = false, paused = false, position = -1;
  let poly = [];

  const fns = new Set();
  const snapshot = () => ({ playing, paused, position, steps: seq.steps, bpm: seq.bpm, step_resolution: seq.step_resolution });
  function emit(what) {
    const st = snapshot();
    for (const fn of [...fns]) {
      try { fn(st, what); } catch (e) { console.error(e); }
    }
  }
  const fail = (e) => ctx.toast?.(e?.message ? `The app said: ${e.message}` : "The app did not answer. Check that it is still running.");
  const wake = () => { try { ctx.twin?.resume?.()?.catch?.(() => {}); } catch (_) { /* audio starts on the next gesture */ } };

  // ── voicing: through ctx.note, so the keys light and the halos show ─────────────────
  const offAt = new Map();                          // pitch -> when its last hold ends (ms)
  const offTimers = new Set();
  function voice(step) {
    if (ctx.soundSource !== "twin") return;          // the S-1 plays the app's steps itself
    const stepSec = stepSeconds(seq.bpm, seq.step_resolution);
    for (const n of stepPlan(seq.notes, step, { gate: perf.gate, probability: perf.probability, stepSec }, rand)) {
      const ms = n.hold * 1000;
      // As the app does: a pitch still held by an overlapping note sounds until the last hold ends.
      offAt.set(n.pitch, Math.max(offAt.get(n.pitch) || 0, now() + ms));
      try { ctx.note(n.pitch, true, n.velocity); } catch (e) { console.error(e); }
      const t = later(() => {
        offTimers.delete(t);
        const end = offAt.get(n.pitch);
        if (end === undefined || end > now() + 2) return;   // silenced already, or a later note holds it
        offAt.delete(n.pitch);
        try { ctx.note(n.pitch, false); } catch (e) { console.error(e); }
      }, ms);
      offTimers.add(t);
    }
  }
  /** Release every note the pattern holds (pause, stop, a stopped app). */
  function silence() {
    for (const t of offTimers) cancel(t);
    offTimers.clear();
    const pitches = [...offAt.keys()];
    offAt.clear();
    for (const p of pitches) {
      try { ctx.note(p, false); } catch (e) { console.error(e); }
    }
  }

  // ── static mode: the local clock ────────────────────────────────────────────────────
  const local = createClock({
    now, later, cancel,
    stepSec: () => stepSeconds(seq.bpm, seq.step_resolution),
    steps: () => seq.steps,
    shuffle: () => perf.shuffle,
    onStep: (step) => { position = step; voice(step); emit("position"); },
  });

  // ── server mode: one request at a time, and the app's word wins ──────────────────────
  let chain = Promise.resolve(), asked = 0, intent = null;   // intent: the last action not yet answered
  function send(action) {
    const n = ++asked;
    intent = action;
    chain = chain.then(async () => {
      try { applyTransport(await server.api("POST", "/api/transport", { action })); }
      catch (e) { fail(e); }
      finally { if (n === asked) intent = null; }
    });
    return chain;
  }
  function applyTransport(t) {
    if (!t || typeof t !== "object") return;
    paused = Boolean(t.paused);
    playing = Boolean(t.playing) && !paused;
    if (typeof t.position === "number" && (playing || paused)) position = t.position;
    if (!playing && !paused) position = -1;
    if (!playing) silence();
    emit("transport");
  }

  // In server mode each returns the request's promise (the check awaits it); the app's answer sets the state.
  function play() {
    wake();                                         // inside the key press or click, so audio may start
    if (!seq.notes.length) { ctx.toast?.("The sequence is empty. Add a note in the Sequencer first."); return; }
    if (server) return send("play");
    if (playing) return;
    const resume = paused;
    playing = true;
    paused = false;
    local.start(!resume);
    emit("transport");
  }
  function pause() {
    if (server) return send("pause");
    if (!playing) return;
    local.halt(false);
    playing = false;
    paused = true;
    silence();
    emit("transport");
  }
  function stop() {
    if (server) return send("stop");
    local.halt(true);
    playing = false;
    paused = false;
    position = -1;
    silence();
    emit("transport");
  }
  const toggle = () => (toggleAction({ playing, paused }, intent) === "pause" ? pause() : play());

  // ── the sequence: edits go up, the app's changes come down ──────────────────────────
  let pushTimer = 0, perfTimer = 0, queued = false, inflight = 0, held = false;
  const busy = () => queued || inflight > 0 || held;     // a local edit must not be overwritten by an echo
  function adopt(s) {
    if (!s || typeof s !== "object") return;
    if (Number.isFinite(s.steps)) seq.steps = s.steps;
    if (Number.isFinite(s.bpm)) seq.bpm = s.bpm;
    if (typeof s.step_resolution === "string") seq.step_resolution = s.step_resolution;
    seq.notes = (Array.isArray(s.notes) ? s.notes : [])
      .map((n) => ({ step: n.step, pitch: n.pitch, velocity: n.velocity, duration: n.duration }));
    poly = Array.isArray(s.poly_warnings) ? s.poly_warnings : polySteps(seq.notes);
    emit("sequence");
  }
  function edited({ now: immediate = false } = {}) {
    if (!server) { poly = polySteps(seq.notes); emit("poly"); return; }
    cancel(pushTimer);
    pushTimer = 0;
    queued = true;
    const go = async () => {
      pushTimer = 0;
      queued = false;
      inflight += 1;
      try {
        const r = await server.api("PUT", "/api/sequence", sequencePayload(seq));
        if (r && Array.isArray(r.poly_warnings)) { poly = r.poly_warnings; emit("poly"); }
      } catch (e) { fail(e); } finally { inflight -= 1; }
    };
    if (immediate) go(); else pushTimer = later(go, pushMs);
  }
  function setTempo(v, { now: immediate = false } = {}) {
    const bpm = Math.max(TEMPO_MIN, Math.min(TEMPO_MAX, Number(v) || 120));
    if (bpm === seq.bpm) return bpm;
    seq.bpm = bpm;
    emit("tempo");
    edited({ now: immediate });
    return bpm;
  }
  function setSteps(v) {
    const n = Math.max(1, Math.min(MAX_STEPS, Math.round(Number(v)) || 16));
    seq.steps = n;
    seq.notes = seq.notes.filter((x) => x.step < n);      // as the server does: notes past the end go
    for (const x of seq.notes) x.duration = clampDuration(x.duration, x.step, n);
    edited({ now: true });
    return n;
  }
  function setPerf(patch = {}) {
    for (const k of ["gate", "shuffle", "probability"]) if (Number.isFinite(patch[k])) perf[k] = patch[k];
    if (typeof patch.clock === "boolean") perf.clock = patch.clock;
    if (!server) return;
    if (typeof patch.clock === "boolean") server.api("PUT", "/api/transport", { clock_enabled: perf.clock }).catch(fail);
    if (["gate", "shuffle", "probability"].some((k) => k in patch)) {
      cancel(perfTimer);
      perfTimer = later(() => {
        perfTimer = 0;
        server.api("PUT", "/api/transport", { gate: perf.gate, shuffle: perf.shuffle, probability: perf.probability }).catch(fail);
      }, perfMs);
    }
  }
  function adoptPerf(t) {
    if (!t || typeof t !== "object") return;
    for (const k of ["gate", "shuffle", "probability"]) if (Number.isFinite(t[k])) perf[k] = t[k];
    if (typeof t.clock_enabled === "boolean") perf.clock = t.clock_enabled;
    emit("perf");
  }
  function refresh() {
    if (!server) return Promise.resolve();
    const status = (t) => { if (!intent) applyTransport(t); };   // an answer to our own action is newer
    return Promise.all([
      server.api("GET", "/api/sequence").then((s) => { if (!busy()) adopt(s); status(s?.transport); }).catch(() => {}),
      server.api("PUT", "/api/transport", {}).then((t) => { adoptPerf(t); status(t); }).catch(() => {}),   // an empty PUT reads them
    ]);
  }

  // ── following the app: the shell's /ws/state, every raw message ─────────────────────
  function onServer(m) {
    switch (m && m.type) {
      case "hello":
        if (!busy() && m.sequence) adopt(m.sequence);
        applyTransport(m.status?.transport);
        break;
      case "sequence":                               // someone changed it (our own echo matches what we sent)
        if (!busy()) server.api("GET", "/api/sequence").then((s) => { if (!busy()) adopt(s); }).catch(() => {});
        break;
      case "transport": applyTransport(m); break;
      case "position": {
        // A tick counts while the app says the steps run, or while our play is on its way. One sent
        // just before a pause or a stop can land after it: it must neither restart the display nor sound.
        const live = intent ? intent === "play" : playing;
        if (typeof m.step !== "number" || !live) break;
        position = m.step;
        playing = true;
        paused = false;
        voice(m.step);
        emit("position");
        break;
      }
      default: break;
    }
  }
  const offServer = server && typeof ctx.on === "function" ? ctx.on("server", onServer) : null;
  if (server) refresh();

  return {
    get state() { return snapshot(); },
    seq,
    perf,
    get poly() { return poly; },
    play,
    pause,
    stop,
    toggle,
    on(fn) { fns.add(fn); return () => fns.delete(fn); },
    edited,
    setTempo,
    nudgeTempo: (delta) => setTempo(seq.bpm + Number(delta || 0)),
    setSteps,
    setPerf,
    replace: adopt,
    hold(on) { held = Boolean(on); },
    refresh,
    destroy() {
      local.halt(true);
      cancel(pushTimer);
      cancel(perfTimer);
      silence();
      offServer?.();
      fns.clear();
    },
  };
}
