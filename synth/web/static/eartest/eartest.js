// The ear test (FABLE rule 1): does the twin's distance agree with Tyler's ears?
// Plays a reference, then A, then B, and records which one he hears as closer.
// Server: GET /api/eartest/trial, POST /api/eartest/answer (synth/web/eartest.py).
// The page never learns which side the metric calls closer, and it draws no waveforms:
// the answer must come from listening, not from looking.

const $ = (id) => document.getElementById(id);
const el = {
  progress: $("progress"), intro: $("intro"), start: $("start"), trial: $("trial"),
  finish: $("finish"), finishText: $("finish-text"), finishCmd: $("finish-cmd"),
  status: $("status"), keys: $("keys-fallback"),
};
const clips = [...document.querySelectorAll(".et-clip")];
const choices = [...document.querySelectorAll(".et-choice")];
const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const ORDER = ["reference", "a", "b"];
const GAP_S = 0.35;

const state = {
  session: sessionName(), ctx: null, trial: null, buffers: null, next: null,
  busy: false, shownAt: 0, source: null, playToken: 0, seqToken: 0,
};

function sessionName() {
  const url = new URL(window.location.href);
  let s = url.searchParams.get("session");
  if (!s || !/^[A-Za-z0-9_-]{1,64}$/.test(s)) {
    const d = new Date();
    const p = (n) => String(n).padStart(2, "0");
    s = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;
    url.searchParams.set("session", s);
    window.history.replaceState(null, "", url);
  }
  return s;
}

// ── status line and key hints ────────────────────────────────
function status(text, action) {
  el.status.replaceChildren(document.createTextNode(text || ""));
  if (action) {
    const b = document.createElement("button");
    b.className = "linkish";
    b.type = "button";
    b.textContent = action.label;
    b.addEventListener("click", action.run);
    el.status.append(" ", b);
  }
}

function hints(items) {
  if (window.KeyHint) { window.KeyHint.set(items); return; }
  el.keys.replaceChildren(...items.map(({ key, label }) => {
    const span = document.createElement("span");
    const b = document.createElement("b");
    b.textContent = key;
    span.append(b, label);
    return span;
  }));
  el.keys.hidden = items.length === 0;
}

const TRIAL_HINTS = [
  { key: "space", label: "Reference" }, { key: "a", label: "Hear A" }, { key: "b", label: "Hear B" },
  { key: "r", label: "All three" }, { key: "1", label: "A" }, { key: "2", label: "B" },
  { key: "0", label: "Can't tell" },
];

// ── server ───────────────────────────────────────────────────
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch { /* not JSON */ }
    throw new Error(msg || `HTTP ${r.status}`);
  }
  return r.json();
}

function arrayBuffer(b64) {
  const bin = window.atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out.buffer;
}

async function fetchTrial(i) {
  const q = new URLSearchParams({ session: state.session });
  if (i !== undefined) q.set("i", String(i));
  const t = await api(`/api/eartest/trial?${q}`);
  if (t.done) return { t };
  const dec = (b64) => state.ctx.decodeAudioData(arrayBuffer(b64));
  const [reference, a, b] = await Promise.all([dec(t.reference), dec(t.a), dec(t.b)]);
  return { t, buffers: { reference, a, b } };
}

// ── playback ─────────────────────────────────────────────────
function clearPlaying() {
  for (const c of clips) {
    c.classList.remove("playing");
    c.querySelector(".et-bar").style.setProperty("--played", "0%");
  }
}

function stop() {
  state.playToken++;
  if (state.source) {
    try { state.source.stop(); } catch { /* already stopped */ }
    state.source = null;
  }
  clearPlaying();
}

function play(name) {
  stop();
  const buf = state.buffers && state.buffers[name];
  if (!buf) return Promise.resolve(false);
  const token = state.playToken;
  const src = state.ctx.createBufferSource();
  src.buffer = buf;
  src.connect(state.ctx.destination);
  state.source = src;
  const btn = clips.find((c) => c.dataset.clip === name);
  const bar = btn.querySelector(".et-bar");
  btn.classList.add("playing");
  const t0 = state.ctx.currentTime;
  if (!reduced) {
    const tick = () => {
      if (token !== state.playToken) return;
      const f = Math.min(1, (state.ctx.currentTime - t0) / buf.duration);
      bar.style.setProperty("--played", `${(f * 100).toFixed(1)}%`);
      if (f < 1) window.requestAnimationFrame(tick);
    };
    window.requestAnimationFrame(tick);
  }
  return new Promise((resolve) => {
    src.onended = () => {
      if (token === state.playToken) { state.source = null; clearPlaying(); }
      resolve(token === state.playToken);
    };
    src.start();
  });
}

const pause = (s) => new Promise((r) => setTimeout(r, s * 1000));

async function playSequence() {
  const seq = ++state.seqToken;
  for (const name of ORDER) {
    if (seq !== state.seqToken) return;
    const finished = await play(name);
    if (!finished || seq !== state.seqToken) return;
    await pause(GAP_S);
  }
}

function manualPlay(name) {
  state.seqToken++;                 // any manual listen ends the automatic sequence
  play(name);
}

// ── the flow ─────────────────────────────────────────────────
function setChoices(enabled) {
  for (const c of choices) c.disabled = !enabled;
}

function showTrial(entry) {
  state.trial = entry.t;
  state.buffers = entry.buffers;
  el.progress.textContent = `Pair ${entry.t.index + 1} of ${entry.t.total}`;
  for (const c of choices) c.classList.remove("chosen");
  setChoices(true);
  status("");
  state.shownAt = performance.now();
  playSequence();
  const n = entry.t.index + 1;
  state.next = n < entry.t.total ? fetchTrial(n).catch(() => null) : null;
}

function finish(r) {
  stop();
  el.intro.hidden = true;
  el.trial.hidden = true;
  el.finish.hidden = false;
  el.progress.textContent = `${r.total} of ${r.total}`;
  el.finishText.textContent =
    `That is all ${r.total} pairs. Thank you. Your answers are saved in ${r.path}.`;
  el.finishCmd.textContent = `.venv/bin/synth-eartest-report ${state.session}`;
  status("Run it in the synth folder.");
  hints([]);
}

async function loadNext(afterIndex) {
  let entry = state.next ? await state.next : null;
  if (!entry || (entry.t && !entry.t.done && entry.t.index !== afterIndex + 1)) {
    status("Getting the next pair …");
    entry = await fetchTrial();
  }
  if (entry.t.done) { finish(entry.t); return; }
  showTrial(entry);
}

async function answer(choice) {
  if (state.busy || !state.trial || el.trial.hidden) return;
  state.busy = true;
  setChoices(false);
  choices.find((c) => c.dataset.choice === choice).classList.add("chosen");
  state.seqToken++;
  stop();
  const ms = Math.round(performance.now() - state.shownAt);
  const index = state.trial.index;
  try {
    const r = await api("/api/eartest/answer", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ trial_id: state.trial.id, choice, ms }),
    });
    if (r.done) { finish(r); return; }
    await loadNext(index);
  } catch (e) {
    setChoices(true);
    status(`That did not go through: ${e.message}. Is the cockpit still running?`,
      { label: "Try again", run: () => answer(choice) });
  } finally {
    state.busy = false;
  }
}

async function start() {
  if (state.ctx) return;
  el.start.disabled = true;
  const Ctx = window.AudioContext || window.webkitAudioContext;
  state.ctx = new Ctx();
  // Never wait on resume(): without a user gesture it can stay pending. Decoding works
  // on a suspended context, and a click anywhere resumes it.
  state.ctx.resume().catch(() => {});
  status("Getting the first pair …");
  try {
    const entry = await fetchTrial();
    if (entry.t.done) { finish(entry.t); return; }
    el.intro.hidden = true;
    el.trial.hidden = false;
    hints(TRIAL_HINTS);
    showTrial(entry);
  } catch (e) {
    state.ctx = null;
    el.start.disabled = false;
    status(`The server did not answer: ${e.message}. Is the cockpit running?`);
  }
}

// ── input ────────────────────────────────────────────────────
el.start.addEventListener("click", start);
for (const c of clips) c.addEventListener("click", () => manualPlay(c.dataset.clip));
for (const c of choices) c.addEventListener("click", () => answer(c.dataset.choice));

// Space replays the reference; it must never press a focused answer button.
document.addEventListener("keyup", (e) => { if (e.key === " ") e.preventDefault(); });
document.addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (!el.intro.hidden) {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); start(); }
    return;
  }
  if (el.trial.hidden) return;
  const k = e.key.toLowerCase();
  if (k === " ") { e.preventDefault(); manualPlay("reference"); }
  else if (k === "a") manualPlay("a");
  else if (k === "b") manualPlay("b");
  else if (k === "r") playSequence();
  else if (k === "1") answer("A");
  else if (k === "2") answer("B");
  else if (k === "0") answer("same");
});

document.addEventListener("pointerdown", () => { if (state.ctx) state.ctx.resume().catch(() => {}); });

el.progress.textContent = "40 pairs";
hints([{ key: "enter", label: "Start" }]);
// "#start" in the address skips the intro (screenshots, or coming back mid-session).
if (window.location.hash === "#start") start();
