"use strict";

/* The S-1 cockpit. Every control is generated from /api/schema — nothing is
 * hand-coded per parameter. Live state flows over /ws/state in both
 * directions; the match studio keeps its own /ws progress channel. */

const $ = (id) => document.getElementById(id);

const APP = {
  schema: null,          // /api/schema payload
  params: {},            // cc -> value (live)
  controls: {},          // cc -> {input, valueEl, param}
  status: null,
  octave: 4,             // QWERTY base octave (C4 = 60)
  heldKeys: {},          // keyboard code -> midi note
  seq: { steps: 16, bpm: 120, step_resolution: "1/16", notes: [] },
  polyWarnings: [],
  playing: false,
  position: -1,
  selectedNote: null,
  studio: false,
};

// ── helpers ───────────────────────────────────────────────
function toast(msg, isErr = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast show" + (isErr ? " err" : "");
  setTimeout(() => (t.className = "toast"), 2800);
}

async function api(method, path, body, isForm = false) {
  const opts = { method };
  if (body && !isForm) { opts.headers = { "Content-Type": "application/json" }; opts.body = JSON.stringify(body); }
  if (body && isForm) opts.body = body;
  const r = await fetch(path, opts);
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return r.headers.get("content-type")?.includes("json") ? r.json() : r;
}

function switchView(name) {
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
}

// ── value formatting (schema display_format hints) ────────
function fmtValue(p, v) {
  if (p.labels && p.labels[String(v)] !== undefined) return p.labels[String(v)];
  if (p.format === "signed64") { const s = v - 64; return s > 0 ? `+${s}` : String(s); }
  if (p.format === "mult") return "×" + (Math.round((1 + (v - 3) * 31 / 124) * 2) / 2).toFixed(1);
  if (p.format === "chop200") return String(Math.min(200, Math.round(v * 255 / 127)));
  return String(v);
}

// ── generated param controls ──────────────────────────────
const SECTION_ACCENTS = {
  LFO: "violet", OSC: "magenta", FILTER: "cyan", AMP: "gold",
  ENV: "lime", EFX: "violet", CONTROLLER: "gold", MIDI: "muted",
};

function sendParam(cc, value) {
  APP.params[cc] = value;
  if (stateWS && stateWS.readyState === WebSocket.OPEN) {
    stateWS.send(JSON.stringify({ type: "param", cc, value }));
  } else {
    api("PUT", `/api/params/${cc}`, { value }).catch((e) => toast(e.message, true));
  }
}

function buildParamRow(p) {
  const row = document.createElement("div");
  row.className = "param";
  row.dataset.cc = p.cc;

  const name = document.createElement("span");
  name.className = "pname";
  name.title = `CC ${p.cc}${p.description ? " — " + p.description : ""}`;
  name.textContent = p.name;
  if (p.access === "shift") {
    const b = document.createElement("span");
    b.className = "shift-badge"; b.textContent = "⇧";
    name.appendChild(b);
  }
  if (p.menu_item) {
    const b = document.createElement("span");
    b.className = "menu-code"; b.textContent = p.menu_item;
    name.appendChild(b);
  }

  const valueEl = document.createElement("span");
  valueEl.className = "pval";

  let input;
  if (p.type === "switch") {
    input = document.createElement("button");
    input.className = "switch";
    input.onclick = () => {
      const v = APP.params[p.cc] >= 64 ? 0 : 127;
      sendParam(p.cc, v);
      updateControl(p.cc, v);
    };
  } else if (p.type === "discrete") {
    input = document.createElement("select");
    for (const [k, label] of Object.entries(p.labels)) {
      const opt = document.createElement("option");
      opt.value = k; opt.textContent = label;
      input.appendChild(opt);
    }
    input.onchange = () => {
      const v = parseInt(input.value, 10);
      sendParam(p.cc, v);
      updateControl(p.cc, v);
    };
  } else {
    input = document.createElement("input");
    input.type = "range";
    input.min = p.min; input.max = p.max; input.step = 1;
    input.oninput = () => {
      const v = parseInt(input.value, 10);
      sendParam(p.cc, v);
      paintRange(input, p);
      valueEl.textContent = fmtValue(p, v);
    };
  }

  row.append(name, input, valueEl);
  APP.controls[p.cc] = { input, valueEl, param: p };
  updateControl(p.cc, p.value);
  return row;
}

function paintRange(input, p) {
  const pct = ((input.value - p.min) / Math.max(1, p.max - p.min)) * 100;
  input.style.setProperty("--fill", pct + "%");
}

function updateControl(cc, value) {
  APP.params[cc] = value;
  const c = APP.controls[cc];
  if (!c) return;
  const { input, valueEl, param } = c;
  valueEl.textContent = fmtValue(param, value);
  if (param.type === "switch") {
    input.classList.toggle("on", value >= 64);
    input.textContent = value >= 64 ? "ON" : "OFF";
  } else if (param.type === "discrete") {
    if (document.activeElement !== input) input.value = String(value);
  } else {
    if (document.activeElement !== input) input.value = value;
    paintRange(input, param);
  }
}

function renderCockpit() {
  const wrap = $("sections");
  wrap.replaceChildren();
  for (const section of APP.schema.sections) {
    const card = document.createElement("section");
    card.className = "card";
    card.dataset.accent = SECTION_ACCENTS[section.name] || "cyan";
    const h2 = document.createElement("h2");
    h2.textContent = section.name;
    card.appendChild(h2);
    for (const p of section.params) card.appendChild(buildParamRow(p));
    wrap.appendChild(card);
  }
  const menu = $("menu-params");
  menu.replaceChildren(...APP.schema.menu.map(buildParamRow));
  const midi = $("midi-params");
  midi.replaceChildren(...APP.schema.midi.map(buildParamRow));

  // PRM-only tier: informational (no CC — lives in .PRM files).
  const prm = $("prm-params");
  prm.replaceChildren(...APP.schema.prm.map((p) => {
    const div = document.createElement("div");
    div.className = "prm-item";
    div.title = p.description || p.key;
    const name = document.createElement("span");
    name.textContent = p.name;
    const val = document.createElement("b");
    const label = p.labels && p.labels[String(p.default)];
    val.textContent = label !== undefined ? label : String(p.default);
    div.append(name, val);
    return div;
  }));
}

// ── header chips ──────────────────────────────────────────
function setSyncChip(state, port) {
  const chip = $("sync-chip");
  chip.className = "chip sync " + (state === "synced" ? "on" : state === "connecting" ? "connecting" : "off");
  $("sync-label").textContent = state.toUpperCase();
  chip.title = port || "no S-1 MIDI port";
}

function setAudioChip(mon) {
  const chip = $("audio-chip");
  chip.classList.toggle("off", !mon.running);
  chip.title = mon.running ? `S-1 audio → speakers (peak ${mon.peak_db} dB)` : "no S-1 audio input";
  $("mute").textContent = mon.muted ? "🔇" : "🔊";
  const pct = mon.running ? Math.max(0, Math.min(100, ((mon.peak_db + 60) / 60) * 100)) : 0;
  $("mini-fill").style.width = pct + "%";
}

function setKbdChip(names) {
  const chip = $("kbd-chip");
  chip.classList.toggle("hidden", !names.length);
  $("kbd-count").textContent = names.length;
  chip.title = names.join("\n") || "";
}

function applyStatus(st) {
  APP.status = st;
  setSyncChip(st.sync, st.port);
  setAudioChip(st.monitor);
  setKbdChip(st.keyboards || []);
  if (st.transport) applyTransport(st.transport);
}

// ── live state WebSocket ──────────────────────────────────
let stateWS = null, stateRetryMs = 1000;
function connectStateWS() {
  stateWS = new WebSocket(`ws://${location.host}/ws/state`);
  stateWS.onopen = () => { stateRetryMs = 1000; };
  stateWS.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    switch (m.type) {
      case "hello":
        for (const [cc, v] of Object.entries(m.params)) updateControl(parseInt(cc, 10), v);
        applyStatus(m.status);
        applySequence(m.sequence);
        break;
      case "param":
        updateControl(m.cc, m.value);
        break;
      case "sync":
        setSyncChip(m.state, m.port);
        break;
      case "monitor":
        setAudioChip(m);
        break;
      case "keyboards":
        setKbdChip(m.names);
        break;
      case "position":
        APP.position = m.step;
        $("t-pos").textContent = `step ${String(m.step + 1).padStart(2, "0")}`;
        drawRoll();
        break;
      case "transport":
        applyTransport(m);
        break;
      case "sequence":
        loadSequenceFromServer();
        break;
    }
  };
  stateWS.onclose = () => {
    setSyncChip("disconnected", null);
    setTimeout(connectStateWS, stateRetryMs);
    stateRetryMs = Math.min(stateRetryMs * 2, 10000);
  };
}

// ── synesthesia note colors ───────────────────────────────
/* Tyler's note-name → color mapping (candidate hex — tune by ear/eye):
 * A red · B brown · C white-blue · D blue-white · E neon green ·
 * F pastel red · G blue; sharps run brighter. "legible" trades fidelity
 * for distinctness: 12 evenly-spaced hues, octave-invariant, A anchored
 * red. "off" restores the plain neon roll. Persisted in localStorage. */
const TRUE_BASE = {
  A: [224, 16, 16], B: [107, 74, 43], C: [220, 232, 255], D: [169, 199, 255],
  E: [57, 255, 20], F: [255, 138, 138], G: [43, 91, 255],
};
// pitch class (0 = C) -> [letter, sharp?]
const PC_LETTER = [
  ["C", 0], ["C", 1], ["D", 0], ["D", 1], ["E", 0], ["F", 0],
  ["F", 1], ["G", 0], ["G", 1], ["A", 0], ["A", 1], ["B", 0],
];

function noteColor(pitch, alpha = 1) {
  const pc = ((pitch % 12) + 12) % 12;
  if (APP.noteColors === "legible") {
    const hue = ((pc - 9 + 12) % 12) * 30;  // A = 0° = red
    return `hsla(${hue}, 92%, 58%, ${alpha})`;
  }
  if (APP.noteColors === "true") {
    const [letter, sharp] = PC_LETTER[pc];
    let [r, g, b] = TRUE_BASE[letter];
    if (sharp) {  // sharps a bit brighter
      r = Math.round(r + (255 - r) * 0.35);
      g = Math.round(g + (255 - g) * 0.35);
      b = Math.round(b + (255 - b) * 0.35);
    }
    return `rgba(${r},${g},${b},${alpha})`;
  }
  return null;  // off
}

function bindNoteColors() {
  APP.noteColors = localStorage.getItem("s1.noteColors") || "true";
  const sel = $("note-colors");
  sel.value = APP.noteColors;
  sel.onchange = () => {
    APP.noteColors = sel.value;
    localStorage.setItem("s1.noteColors", sel.value);
    renderKeyboard();
    drawRoll();
  };
}

// ── QWERTY + on-screen keyboard ───────────────────────────
// Key row -> semitone offset from the base octave's C.
const KEYMAP = { KeyA: 0, KeyW: 1, KeyS: 2, KeyE: 3, KeyD: 4, KeyF: 5, KeyT: 6,
  KeyG: 7, KeyY: 8, KeyH: 9, KeyU: 10, KeyJ: 11, KeyK: 12, KeyO: 13, KeyL: 14, KeyP: 15 };
const KEY_LABELS = ["A", "W", "S", "E", "D", "F", "T", "G", "Y", "H", "U", "J", "K", "O", "L", "P"];
const BLACK = new Set([1, 3, 6, 8, 10]);
const NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];

function noteMsg(note, on) {
  const velocity = parseInt($("qwerty-vel").value, 10);
  if (stateWS && stateWS.readyState === WebSocket.OPEN) {
    stateWS.send(JSON.stringify({ type: "note", note, velocity, on }));
  } else {
    api("POST", "/api/notes", { note, velocity, on }).catch(() => {});
  }
  const el = document.querySelector(`.key[data-note="${note}"]`);
  if (el) el.classList.toggle("held", on);
}

function baseNote() { return (APP.octave + 1) * 12; }  // C4 = 60 at octave 4

function renderKeyboard() {
  const kb = $("keyboard");
  kb.replaceChildren();
  $("oct-label").textContent = "C" + APP.octave;
  const whiteCount = 15;  // 2 octaves + 1
  const whiteW = 100 / whiteCount;
  let whiteIdx = 0;
  for (let i = 0; i < 25; i++) {
    const note = baseNote() + i;
    const semitone = i % 12;
    const key = document.createElement("div");
    const isBlack = BLACK.has(semitone);
    key.className = "key " + (isBlack ? "black" : "white");
    key.dataset.note = note;
    if (isBlack) {
      key.style.left = `calc(${whiteIdx * whiteW}% - 1.4%)`;
      key.style.width = "2.8%";
    } else {
      key.style.left = `${whiteIdx * whiteW}%`;
      key.style.width = `${whiteW}%`;
      whiteIdx++;
    }
    const label = document.createElement("span");
    label.className = "klabel";
    label.textContent = i < KEY_LABELS.length ? KEY_LABELS[i] :
      (semitone === 0 ? "C" + (Math.floor(note / 12) - 1) : "");
    key.appendChild(label);
    const c = noteColor(note, 1);
    if (c) {
      const band = document.createElement("i");
      band.className = "kband";
      band.style.background = c;
      band.style.boxShadow = `0 0 6px ${c}`;
      key.appendChild(band);
    }
    key.onpointerdown = (e) => { e.preventDefault(); key.setPointerCapture(e.pointerId); noteMsg(note, true); };
    key.onpointerup = () => noteMsg(note, false);
    key.onpointercancel = () => noteMsg(note, false);
    kb.appendChild(key);
  }
}

function isTyping(e) {
  const t = e.target.tagName;
  return t === "INPUT" || t === "SELECT" || t === "TEXTAREA";
}

function bindQwerty() {
  document.addEventListener("keydown", (e) => {
    if (isTyping(e) || e.repeat || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.code === "KeyZ") { APP.octave = Math.max(0, APP.octave - 1); renderKeyboard(); return; }
    if (e.code === "KeyX") { APP.octave = Math.min(8, APP.octave + 1); renderKeyboard(); return; }
    const offset = KEYMAP[e.code];
    if (offset === undefined || APP.heldKeys[e.code]) return;
    const note = baseNote() + offset;
    if (note > 127) return;
    APP.heldKeys[e.code] = note;
    noteMsg(note, true);
  });
  document.addEventListener("keyup", (e) => {
    const note = APP.heldKeys[e.code];
    if (note !== undefined) {
      delete APP.heldKeys[e.code];
      noteMsg(note, false);
    }
  });
  window.addEventListener("blur", () => {
    for (const note of Object.values(APP.heldKeys)) noteMsg(note, false);
    APP.heldKeys = {};
  });
  $("oct-down").onclick = () => { APP.octave = Math.max(0, APP.octave - 1); renderKeyboard(); };
  $("oct-up").onclick = () => { APP.octave = Math.min(8, APP.octave + 1); renderKeyboard(); };
}

// ── piano roll ────────────────────────────────────────────
const ROLL = { cellW: 26, cellH: 16, labelW: 42, hiPitch: 96, loPitch: 24 };

function rollRows() { return ROLL.hiPitch - ROLL.loPitch + 1; }
function pitchToRow(p) { return ROLL.hiPitch - p; }
function rowToPitch(r) { return ROLL.hiPitch - r; }

function noteAt(step, pitch) {
  return APP.seq.notes.find((n) => n.pitch === pitch && step >= n.step && step < n.step + n.duration);
}

function drawRoll() {
  const cv = $("roll");
  const W = ROLL.labelW + APP.seq.steps * ROLL.cellW;
  const H = rollRows() * ROLL.cellH;
  if (cv.width !== W) cv.width = W;
  if (cv.height !== H) cv.height = H;
  const ctx = cv.getContext("2d");
  ctx.clearRect(0, 0, W, H);

  // rows
  for (let r = 0; r < rollRows(); r++) {
    const pitch = rowToPitch(r);
    const y = r * ROLL.cellH;
    const semitone = pitch % 12;
    ctx.fillStyle = BLACK.has(semitone) ? "#0a0816" : "#131024";
    if (semitone === 0) ctx.fillStyle = "#1a1530";
    ctx.fillRect(ROLL.labelW, y, W - ROLL.labelW, ROLL.cellH);
    // label octave Cs and row separators
    if (semitone === 0) {
      ctx.fillStyle = "#716c9c";
      ctx.font = "10px ui-monospace, monospace";
      ctx.fillText("C" + (Math.floor(pitch / 12) - 1), 8, y + 12);
    }
  }
  // grid lines
  for (let s = 0; s <= APP.seq.steps; s++) {
    const x = ROLL.labelW + s * ROLL.cellW;
    ctx.strokeStyle = s % 4 === 0 ? "#383258" : "#221d40";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x + 0.5, 0); ctx.lineTo(x + 0.5, H); ctx.stroke();
  }
  for (let r = 0; r <= rollRows(); r++) {
    const y = r * ROLL.cellH;
    ctx.strokeStyle = "#1b1735";
    ctx.beginPath(); ctx.moveTo(ROLL.labelW, y + 0.5); ctx.lineTo(W, y + 0.5); ctx.stroke();
  }
  // poly-warning columns
  for (const s of APP.polyWarnings) {
    ctx.fillStyle = "rgba(255,184,108,0.10)";
    ctx.fillRect(ROLL.labelW + s * ROLL.cellW, 0, ROLL.cellW, H);
  }
  // playhead
  if (APP.playing && APP.position >= 0 && APP.position < APP.seq.steps) {
    ctx.fillStyle = "rgba(45,226,230,0.14)";
    ctx.fillRect(ROLL.labelW + APP.position * ROLL.cellW, 0, ROLL.cellW, H);
  }
  // notes
  for (const n of APP.seq.notes) {
    const r = pitchToRow(n.pitch);
    if (r < 0 || r >= rollRows()) continue;
    const x = ROLL.labelW + n.step * ROLL.cellW + 1;
    const y = r * ROLL.cellH + 1;
    const w = n.duration * ROLL.cellW - 2;
    const h = ROLL.cellH - 2;
    const alpha = 0.45 + (n.velocity / 127) * 0.55;
    const selected = APP.selectedNote === n;
    const colored = noteColor(n.pitch, alpha);
    const glow = noteColor(n.pitch, 1);
    if (colored) {
      ctx.fillStyle = colored;
      ctx.shadowColor = selected ? "#36f9b3" : glow;
    } else {
      ctx.fillStyle = selected ? `rgba(54,249,179,${alpha})` : `rgba(255,46,151,${alpha})`;
      ctx.shadowColor = selected ? "#36f9b3" : "#ff2e97";
    }
    ctx.shadowBlur = selected ? 12 : 8;
    ctx.beginPath();
    ctx.roundRect(x, y, w, h, 4);
    ctx.fill();
    if (colored && selected) {
      ctx.strokeStyle = "#36f9b3";
      ctx.lineWidth = 2;
      ctx.stroke();
    }
    ctx.shadowBlur = 0;
    // velocity notch
    ctx.fillStyle = "rgba(255,255,255,0.65)";
    ctx.fillRect(x + 2, y + h - 3, Math.max(2, (w - 4) * (n.velocity / 127)), 2);
  }
}

let rollDrag = null;       // {note, mode: "create"|"extend"}
let seqPushTimer = null;

function pushSequence(immediate = false) {
  clearTimeout(seqPushTimer);
  const doPush = async () => {
    try {
      const r = await api("PUT", "/api/sequence", {
        steps: APP.seq.steps,
        bpm: APP.seq.bpm,
        step_resolution: APP.seq.step_resolution,
        notes: APP.seq.notes,
      });
      APP.polyWarnings = r.poly_warnings || [];
      updatePolyWarning();
      drawRoll();
    } catch (e) { toast(e.message, true); }
  };
  if (immediate) doPush(); else seqPushTimer = setTimeout(doPush, 180);
}

function updatePolyWarning() {
  const el = $("poly-warning");
  if (APP.polyWarnings.length) {
    el.textContent = `⚠ steps ${APP.polyWarnings.map((s) => s + 1).join(", ")} hold more than 4 notes — the S-1 plays at most 4 per step (extras are dropped on Save to S-1)`;
    el.classList.remove("hidden");
  } else {
    el.classList.add("hidden");
  }
}

function cellFromEvent(e) {
  const cv = $("roll");
  const rect = cv.getBoundingClientRect();
  const x = e.clientX - rect.left - ROLL.labelW;
  const y = e.clientY - rect.top;
  if (x < 0) return null;
  const step = Math.floor(x / ROLL.cellW);
  const pitch = rowToPitch(Math.floor(y / ROLL.cellH));
  if (step < 0 || step >= APP.seq.steps || pitch < 0 || pitch > 127) return null;
  return { step, pitch };
}

function selectNote(n) {
  APP.selectedNote = n;
  const ins = $("note-inspector");
  if (!n) { ins.classList.add("hidden"); drawRoll(); return; }
  ins.classList.remove("hidden");
  $("ni-label").textContent = `${NOTE_NAMES[n.pitch % 12]}${Math.floor(n.pitch / 12) - 1} @ step ${n.step + 1}`;
  $("ni-label").style.color = noteColor(n.pitch, 1) || "";
  $("ni-vel").value = n.velocity;
  $("ni-len").value = n.duration;
  drawRoll();
}

function bindRoll() {
  const cv = $("roll");
  cv.addEventListener("pointerdown", (e) => {
    const cell = cellFromEvent(e);
    if (!cell) return;
    cv.setPointerCapture(e.pointerId);
    const existing = noteAt(cell.step, cell.pitch);
    if (existing) {
      selectNote(existing);
      rollDrag = { note: existing, mode: "extend" };
    } else {
      const n = { step: cell.step, pitch: cell.pitch,
                  velocity: parseInt($("qwerty-vel").value, 10), duration: 1 };
      APP.seq.notes.push(n);
      selectNote(n);
      rollDrag = { note: n, mode: "create" };
      drawRoll();
    }
  });
  cv.addEventListener("pointermove", (e) => {
    if (!rollDrag) return;
    const cell = cellFromEvent(e);
    if (!cell) return;
    const n = rollDrag.note;
    const dur = Math.max(1, Math.min(APP.seq.steps - n.step, cell.step - n.step + 1));
    if (dur !== n.duration) { n.duration = dur; $("ni-len").value = dur; drawRoll(); }
  });
  cv.addEventListener("pointerup", () => {
    if (rollDrag) { rollDrag = null; pushSequence(); }
  });
  cv.addEventListener("dblclick", (e) => {
    const cell = cellFromEvent(e);
    if (!cell) return;
    const existing = noteAt(cell.step, cell.pitch);
    if (existing) {
      APP.seq.notes = APP.seq.notes.filter((n) => n !== existing);
      if (APP.selectedNote === existing) selectNote(null);
      drawRoll();
      pushSequence();
    }
  });

  $("ni-vel").oninput = () => {
    if (APP.selectedNote) { APP.selectedNote.velocity = parseInt($("ni-vel").value, 10); drawRoll(); pushSequence(); }
  };
  $("ni-len").onchange = () => {
    if (APP.selectedNote) {
      const n = APP.selectedNote;
      n.duration = Math.max(1, Math.min(APP.seq.steps - n.step, parseInt($("ni-len").value, 10) || 1));
      drawRoll(); pushSequence();
    }
  };
  $("ni-del").onclick = () => {
    if (APP.selectedNote) {
      APP.seq.notes = APP.seq.notes.filter((n) => n !== APP.selectedNote);
      selectNote(null);
      drawRoll(); pushSequence();
    }
  };
}

function applySequence(seq) {
  if (rollDrag) return;  // don't fight a live edit
  const sel = APP.selectedNote ? { step: APP.selectedNote.step, pitch: APP.selectedNote.pitch } : null;
  APP.seq.steps = seq.steps;
  APP.seq.bpm = seq.bpm;
  APP.seq.step_resolution = seq.step_resolution;
  APP.seq.notes = seq.notes.map((n) => ({ ...n }));
  APP.polyWarnings = seq.poly_warnings || [];
  // Re-bind the inspector to the same note in the fresh list (server echoes
  // replace note objects; a stale reference would silently eat edits).
  if (sel) {
    const again = APP.seq.notes.find((n) => n.step === sel.step && n.pitch === sel.pitch);
    APP.selectedNote = again || null;
    if (!again) $("note-inspector").classList.add("hidden");
  }
  $("t-bpm").value = seq.bpm;
  $("t-res").value = seq.step_resolution;
  $("t-steps").value = seq.steps;
  updatePolyWarning();
  drawRoll();
}

async function loadSequenceFromServer() {
  try { applySequence(await api("GET", "/api/sequence")); } catch (_) {}
}

function applyTransport(t) {
  APP.playing = t.playing;
  if (!t.playing) { APP.position = -1; $("t-pos").textContent = t.paused ? "paused" : "—"; }
  $("t-play").classList.toggle("active", t.playing);
  drawRoll();
}

function bindTransport() {
  $("t-play").onclick = () => api("POST", "/api/transport", { action: "play" }).catch((e) => toast(e.message, true));
  $("t-pause").onclick = () => api("POST", "/api/transport", { action: "pause" }).catch((e) => toast(e.message, true));
  $("t-stop").onclick = () => api("POST", "/api/transport", { action: "stop" }).catch((e) => toast(e.message, true));
  $("t-bpm").onchange = () => { APP.seq.bpm = parseFloat($("t-bpm").value) || 120; pushSequence(true); };
  $("t-res").onchange = () => { APP.seq.step_resolution = $("t-res").value; pushSequence(true); };
  $("t-steps").onchange = () => {
    APP.seq.steps = Math.max(1, Math.min(64, parseInt($("t-steps").value, 10) || 16));
    $("t-steps").value = APP.seq.steps;
    pushSequence(true);
  };
  $("t-clock").onchange = () => api("PUT", "/api/transport", { clock_enabled: $("t-clock").checked }).catch(() => {});
  const perf = () => api("PUT", "/api/transport", {
    gate: parseInt($("t-gate").value, 10) / 100,
    shuffle: parseInt($("t-shuffle").value, 10) / 100,
    probability: parseInt($("t-prob").value, 10) / 100,
  }).catch(() => {});
  $("t-gate").oninput = perf;
  $("t-shuffle").oninput = perf;
  $("t-prob").oninput = perf;
}

// ── banks ─────────────────────────────────────────────────
function bankRow(name, meta, actions) {
  const li = document.createElement("li");
  const nameEl = document.createElement("span");
  nameEl.className = "pname";
  nameEl.textContent = name;
  const metaEl = document.createElement("span");
  metaEl.className = "pmeta";
  metaEl.textContent = meta || "";
  const btns = document.createElement("span");
  btns.className = "pbtns";
  for (const [label, cls, fn] of actions) {
    const b = document.createElement("button");
    b.className = "btn tiny" + (cls ? " " + cls : "");
    b.textContent = label;
    b.onclick = fn;
    btns.appendChild(b);
  }
  li.append(nameEl, metaEl, btns);
  return li;
}

async function refreshPatches() {
  const list = await api("GET", "/api/patches");
  const ul = $("patch-list");
  ul.className = "patch-list" + (list.length ? "" : " empty");
  ul.replaceChildren(...list.map((p) => bankRow(
    p.name,
    p.metadata?.closeness !== undefined ? `${p.metadata.closeness}%` : "",
    [
      ["LOAD", "", () => api("POST", `/api/patches/${encodeURIComponent(p.name)}/load`)
        .then(() => toast(`loaded ${p.name}`)).catch((e) => toast(e.message, true))],
      ["▸", "", () => api("POST", `/api/patches/${encodeURIComponent(p.name)}/play`)
        .then(() => toast(`▸ ${p.name}`)).catch((e) => toast(e.message, true))],
      ["✕", "danger", () => {
        if (confirm(`Delete "${p.name}"?`)) {
          api("DELETE", `/api/patches/${encodeURIComponent(p.name)}`)
            .then(() => { toast(`deleted ${p.name}`); refreshPatches(); })
            .catch((e) => toast(e.message, true));
        }
      }],
    ],
  )));
}

async function savePatch(nameInputId, endpoint, refresh) {
  const name = $(nameInputId).value.trim();
  if (!name) { toast("name it first", true); return; }
  try {
    await api("POST", endpoint, { name, overwrite: false });
    toast(`saved ${name}`);
  } catch (e) {
    if (String(e.message).includes("exists")) {
      if (!confirm(`Overwrite "${name}"?`)) return;
      await api("POST", endpoint, { name, overwrite: true });
      toast(`overwrote ${name}`);
    } else { toast(e.message, true); return; }
  }
  $(nameInputId).value = "";
  refresh();
}

async function refreshSequences() {
  const list = await api("GET", "/api/sequences");
  const ul = $("seq-list");
  ul.className = "patch-list" + (list.length ? "" : " empty");
  ul.replaceChildren(...list.map((s) => bankRow(s.name, "", [
    ["LOAD", "", () => api("POST", `/api/sequences/${encodeURIComponent(s.name)}/load`)
      .then((r) => { applySequence(r); toast(`loaded ${s.name}`); })
      .catch((e) => toast(e.message, true))],
    ["✕", "danger", () => {
      if (confirm(`Delete "${s.name}"?`)) {
        api("DELETE", `/api/sequences/${encodeURIComponent(s.name)}`)
          .then(() => { toast(`deleted ${s.name}`); refreshSequences(); })
          .catch((e) => toast(e.message, true));
      }
    }],
  ])));
}

// ── device pattern (PC) + Save to S-1 ─────────────────────
function fillSlots(sel) {
  sel.replaceChildren(...Array.from({ length: 16 }, (_, i) => {
    const o = document.createElement("option");
    o.textContent = i + 1;
    return o;
  }));
}

function bindDevice() {
  fillSlots($("pc-slot"));
  fillSlots($("prm-slot"));
  $("pc-send").onclick = () => {
    api("POST", "/api/device/pattern", {
      bank: parseInt($("pc-bank").value, 10),
      slot: parseInt($("pc-slot").value, 10),
    }).then((r) => toast(`pattern ${r.bank}-${r.slot} (PC ${r.program})`))
      .catch((e) => toast(e.message, true));
  };
  const updateDownload = () => {
    $("prm-download").href =
      `/api/export/prm?bank=${$("prm-bank").value}&slot=${$("prm-slot").value}`;
  };
  $("prm-bank").onchange = updateDownload;
  $("prm-slot").onchange = updateDownload;
  updateDownload();
  $("prm-write").onclick = () => {
    api("POST", "/api/export/device", {
      bank: parseInt($("prm-bank").value, 10),
      slot: parseInt($("prm-slot").value, 10),
    }).then((r) => { toast("written — press [HOLD] on the S-1"); $("prm-status").textContent = `✓ ${r.written} — ${r.next}`; })
      .catch((e) => toast(e.message, true));
  };
  pollDevice();
  setInterval(pollDevice, 5000);
}

async function pollDevice() {
  if (document.hidden) return;
  try {
    const r = await api("GET", "/api/export/device");
    $("prm-write").disabled = !r.mounted;
    $("prm-status").textContent = r.mounted
      ? `✓ S-1 mounted at ${r.volume} — ready to write`
      : "S-1 not mounted — do the disk-mode ritual below to write directly, or download the file";
  } catch (_) {}
}

// ── live oscilloscope (the S-1's actual signal) ───────────
const SCOPE_MS = 66;  // ~15 fps polling; the endpoint serves the last ~50 ms
let scopeGain = 1;    // auto-gain (smoothed) — the S-1's volume knob may be low

function drawScope(points, running) {
  const cv = $("live-scope");
  const wrap = cv.parentElement;
  const w = wrap.clientWidth;
  if (w && cv.width !== w) cv.width = w;
  const H = cv.height, W = cv.width;
  const ctx = cv.getContext("2d");
  ctx.clearRect(0, 0, W, H);
  const mid = H / 2;

  // midline
  ctx.strokeStyle = "#221d40";
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, mid + 0.5); ctx.lineTo(W, mid + 0.5); ctx.stroke();

  const maxAbs = points.reduce((m, v) => Math.max(m, Math.abs(v)), 0);
  const live = running && maxAbs > 0.0001;
  $("scope-label").textContent = running ? "" : "NO SIGNAL";
  $("scope-label").classList.toggle("hidden", running);
  // Auto-gain: fill ~85% of the strip whatever the hardware volume, smoothed
  // so the trace breathes instead of jumping.
  const target = live ? Math.min(0.85 / maxAbs, 2000) : 1;
  scopeGain += (target - scopeGain) * 0.25;

  const grad = ctx.createLinearGradient(0, 0, W, 0);
  grad.addColorStop(0, "#ff2e97");
  grad.addColorStop(0.5, "#2de2e6");
  grad.addColorStop(1, "#36f9b3");
  ctx.strokeStyle = grad;
  ctx.lineWidth = live ? 2 : 1;
  ctx.globalAlpha = live ? 1 : 0.25;
  ctx.shadowColor = "#2de2e6";
  ctx.shadowBlur = live ? 10 : 0;
  ctx.beginPath();
  const n = points.length;
  for (let i = 0; i < n; i++) {
    const x = (i / (n - 1)) * W;
    const v = Math.max(-1, Math.min(1, points[i] * scopeGain));
    const y = mid - v * (mid - 4);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  }
  ctx.stroke();
  ctx.shadowBlur = 0;
  ctx.globalAlpha = 1;
}

function bindScope() {
  drawScope(new Array(128).fill(0), false);
  setInterval(async () => {
    if (document.hidden) return;
    if (!$("view-cockpit").classList.contains("active")) return;
    try {
      const r = await api("GET", "/api/monitor/scope");
      drawScope(r.points, r.running);
    } catch (_) {}
  }, SCOPE_MS);
}

// ── load from S-1 (.PRM import — the librarian) ───────────
function importFlags() {
  return { load_patch: $("imp-patch").checked, load_sequence: $("imp-seq").checked };
}

function importedToast(r) {
  const bits = [];
  if (r.params !== undefined) bits.push(`${r.params} params`);
  if (r.sequence) bits.push(`${r.sequence.notes} notes @ ${r.sequence.bpm} bpm`);
  toast(`loaded ${r.loaded}${bits.length ? " — " + bits.join(" · ") : ""}`);
}

async function refreshImports() {
  try {
    const r = await api("GET", "/api/import/prm");
    $("imp-device-status").textContent = r.device.mounted
      ? "✓ S-1 mounted — its BACKUP/ patterns are listed below"
      : "S-1 not in disk mode — patterns from ~/.synth/backups below";
    const rows = [];
    for (const [source, files, tag] of [["device", r.device.files, "S-1"], ["backups", r.backups, "local"]]) {
      for (const f of files) {
        rows.push(bankRow(
          f.bank ? `${f.bank}-${String(f.slot).padStart(2, "0")}  ${f.name}` : f.name,
          tag,
          [["LOAD", "", () => api("POST", "/api/import/prm", { source, name: f.name, ...importFlags() })
            .then(importedToast).catch((e) => toast(e.message, true))]],
        ));
      }
    }
    const ul = $("imp-list");
    ul.className = "patch-list" + (rows.length ? "" : " empty");
    ul.replaceChildren(...rows);
  } catch (_) {}
}

function bindImport() {
  $("imp-upload").onclick = () => $("imp-file").click();
  $("imp-file").onchange = async () => {
    const f = $("imp-file").files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    const flags = importFlags();
    try {
      const r = await api(
        "POST",
        `/api/import/upload?load_patch=${flags.load_patch}&load_sequence=${flags.load_sequence}`,
        fd, true,
      );
      importedToast(r);
    } catch (e) { toast(e.message, true); }
    $("imp-file").value = "";
  };
  refreshImports();
  setInterval(() => { if (!document.hidden) refreshImports(); }, 5000);
}

// ── studio (match engine) ─────────────────────────────────
const MATCH = { running: false, paused: false, hasTarget: false, hasBest: false };

const STOPS = [
  [0.0, [12, 10, 24]], [0.45, [255, 46, 151]],
  [0.75, [45, 226, 230]], [1.0, [232, 227, 255]],
];
function colormap(t) {
  for (let i = 1; i < STOPS.length; i++) {
    if (t <= STOPS[i][0]) {
      const [t0, c0] = STOPS[i - 1], [t1, c1] = STOPS[i];
      const f = (t - t0) / (t1 - t0 || 1);
      return [0, 1, 2].map((k) => Math.round(c0[k] + (c1[k] - c0[k]) * f));
    }
  }
  return STOPS[STOPS.length - 1][1];
}
function drawSpec(canvasId, spec) {
  if (!spec) return;
  const { w, h, data } = spec;
  const bytes = Uint8Array.from(atob(data), (c) => c.charCodeAt(0));
  const off = document.createElement("canvas");
  off.width = w; off.height = h;
  const octx = off.getContext("2d");
  const img = octx.createImageData(w, h);
  for (let r = 0; r < h; r++) {
    for (let c = 0; c < w; c++) {
      const v = bytes[r * w + c] / 255;
      const [R, G, B] = colormap(v);
      const dst = ((h - 1 - r) * w + c) * 4;
      img.data[dst] = R; img.data[dst + 1] = G; img.data[dst + 2] = B; img.data[dst + 3] = 255;
    }
  }
  octx.putImageData(img, 0, 0);
  const cv = $(canvasId), ctx = cv.getContext("2d");
  ctx.imageSmoothingEnabled = false;
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.drawImage(off, 0, 0, cv.width, cv.height);
}

let curve = [], lastEvals = -1;
function resetCurve() { curve = []; lastEvals = -1; drawCurve(); }
function drawCurve() {
  const cv = $("curve"); if (!cv) return;
  const ctx = cv.getContext("2d"), W = cv.width, H = cv.height;
  ctx.clearRect(0, 0, W, H);
  const padL = 30, padB = 16, padT = 8, padR = 8;
  const x0 = padL, y0 = H - padB, x1 = W - padR, y1 = padT;
  ctx.font = "10px ui-monospace, monospace";
  ctx.strokeStyle = "#383258"; ctx.lineWidth = 1;
  for (const p of [0, 25, 50, 75, 100]) {
    const y = y0 + (y1 - y0) * (p / 100);
    ctx.globalAlpha = 0.35; ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(x1, y); ctx.stroke();
    ctx.globalAlpha = 1; ctx.fillStyle = "#716c9c"; ctx.fillText(p, 6, y + 3);
  }
  const n = curve.length;
  const maxX = Math.max(10, n ? curve[n - 1].x : 10);
  const sx = (x) => x0 + (x1 - x0) * (x / maxX);
  const sy = (v) => y0 + (y1 - y0) * (Math.max(0, Math.min(100, v)) / 100);
  ctx.fillStyle = "#ff2e97"; ctx.globalAlpha = 0.55;
  for (const p of curve) { ctx.beginPath(); ctx.arc(sx(p.x), sy(p.last), 2.2, 0, 6.3); ctx.fill(); }
  ctx.globalAlpha = 1;
  ctx.strokeStyle = "#36f9b3"; ctx.lineWidth = 2; ctx.shadowColor = "#36f9b3"; ctx.shadowBlur = 6;
  ctx.beginPath();
  curve.forEach((p, i) => { const X = sx(p.x), Y = sy(p.best); i ? ctx.lineTo(X, Y) : ctx.moveTo(X, Y); });
  ctx.stroke(); ctx.shadowBlur = 0;
  ctx.fillStyle = "#716c9c"; ctx.fillText("evals " + (n ? curve[n - 1].x : 0), x1 - 64, y0 + 13);
}

function refreshMatchButtons() {
  $("start").disabled = !(MATCH.hasTarget && !MATCH.running) || !APP.studio;
  $("pause").disabled = !(MATCH.running && !MATCH.paused);
  $("resume").disabled = !(MATCH.running && MATCH.paused);
  $("stop").disabled = !MATCH.running;
  $("match-save").disabled = !MATCH.hasBest;
  document.querySelector('[data-clip="target"]').disabled = !MATCH.hasTarget;
  document.querySelector('[data-clip="best"]').disabled = !MATCH.hasBest;
  document.querySelector('[data-clip="last"]').disabled = !(MATCH.running || MATCH.hasBest);
}

async function uploadFile(file) {
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await api("POST", "/api/target", fd, true);
    MATCH.hasTarget = true;
    drawSpec("spec-target", r.spectrogram);
    $("drop").querySelector("strong").textContent = file.name;
    toast(`target loaded (${r.duration}s)`);
    refreshMatchButtons();
  } catch (e) { toast(e.message, true); }
}

let matchWSStarted = false, lastShownError = null, matchRetryMs = 1000;
function connectMatchWS() {
  if (matchWSStarted) return;
  matchWSStarted = true;
  const open = () => {
    const ws = new WebSocket(`ws://${location.host}/ws`);
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      MATCH.running = !!m.running;
      MATCH.paused = !!m.paused;
      if (m.best_closeness !== undefined) {
        $("closeness-val").textContent = m.best_closeness.toFixed(1) + "%";
        $("bar-fill").style.width = m.best_closeness + "%";
        $("iter").textContent = m.iteration;
        $("maxiter").textContent = m.max_iters;
        $("evals").textContent = m.evals;
      }
      if (m.evals !== undefined && m.evals > lastEvals && m.last_closeness !== undefined) {
        curve.push({ x: m.evals, last: m.last_closeness, best: m.best_closeness });
        lastEvals = m.evals;
        drawCurve();
      }
      if (m.target_spec) { drawSpec("spec-target", m.target_spec); MATCH.hasTarget = true; }
      if (m.best_spec) { drawSpec("spec-best", m.best_spec); MATCH.hasBest = true; }
      if (m.error) {
        $("state-label").textContent = "error: " + m.error;
        if (m.error !== lastShownError) { toast(m.error, true); lastShownError = m.error; }
      } else if (m.done) $("state-label").textContent = "done";
      else if (MATCH.paused) $("state-label").textContent = "paused · listen";
      else if (MATCH.running) $("state-label").textContent = "running…";
      refreshMatchButtons();
    };
    ws.onopen = () => { matchRetryMs = 1000; };
    ws.onclose = () => { setTimeout(open, matchRetryMs); matchRetryMs = Math.min(matchRetryMs * 2, 10000); };
  };
  open();
}

let recOn = false;
function bindStudio() {
  $("start").onclick = async () => {
    try {
      await api("POST", "/api/match/start", {
        max_iters: parseInt($("iters").value, 10),
        include_effects: $("fx").checked,
        optimizer: $("optimizer").value,
        mode: $("mode").value,
        calibrate: $("calib").checked,
      });
      MATCH.running = true; MATCH.paused = false; MATCH.hasBest = false;
      resetCurve();
      $("state-label").textContent = $("calib").checked ? "calibrating…" : "running…";
      refreshMatchButtons();
    } catch (e) { toast(e.message, true); }
  };
  $("pause").onclick = () => api("POST", "/api/match/pause").catch((e) => toast(e.message, true));
  $("resume").onclick = () => api("POST", "/api/match/resume").catch((e) => toast(e.message, true));
  $("stop").onclick = () => api("POST", "/api/match/stop").catch((e) => toast(e.message, true));
  $("match-save").onclick = () => savePatch("match-save-name", "/api/match/save", refreshPatches);

  const drop = $("drop"), file = $("file");
  drop.onclick = () => file.click();
  file.onchange = () => uploadFile(file.files[0]);
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); uploadFile(e.dataTransfer.files[0]); };

  $("record").onclick = async () => {
    try {
      if (recOn) {
        const name = $("rec-name").value.trim() || "s1-take";
        const r = await api("POST", "/api/monitor/record/stop", { name, as_target: $("rec-target").checked });
        recOn = false; $("record").classList.remove("active"); $("record").textContent = "● RECORD";
        toast(`saved ${name} (${r.duration}s)`);
        if (r.spectrogram) { drawSpec("spec-target", r.spectrogram); MATCH.hasTarget = true; refreshMatchButtons(); }
      } else {
        await api("POST", "/api/monitor/record/start");
        recOn = true; $("record").classList.add("active"); $("record").textContent = "■ STOP REC";
      }
    } catch (e) { toast(e.message, true); }
  };

  let currentAudio = null;
  document.body.addEventListener("click", (e) => {
    const clip = e.target.dataset.clip;
    if (clip) {
      if (currentAudio) { currentAudio.pause(); currentAudio.src = ""; }
      currentAudio = new Audio(`/api/clip/${clip}?t=${Date.now()}`);
      currentAudio.play().catch((err) => toast("playback failed: " + err.message, true));
    }
  });
}

// ── init ──────────────────────────────────────────────────
async function init() {
  document.body.addEventListener("click", (e) => {
    const view = e.target.dataset.view;
    if (view) { e.preventDefault(); switchView(view); }
  });

  APP.schema = await api("GET", "/api/schema");
  renderCockpit();

  const status = await api("GET", "/api/status");
  APP.studio = !!status.studio;
  applyStatus(status);
  $("studio-unavailable").classList.toggle("hidden", APP.studio);

  connectStateWS();
  bindNoteColors();
  renderKeyboard();
  bindQwerty();
  bindRoll();
  bindTransport();
  bindDevice();
  bindImport();
  bindScope();
  bindStudio();
  if (APP.studio) connectMatchWS();

  $("mute").onclick = () =>
    api("POST", "/api/monitor/mute", { muted: !(APP.status?.monitor?.muted) })
      .then((r) => { APP.status.monitor = r; setAudioChip(r); })
      .catch((e) => toast(e.message, true));

  $("patch-save").onclick = () => savePatch("patch-name", "/api/patches", refreshPatches);
  $("seq-save").onclick = () => savePatch("seq-name", "/api/sequences", refreshSequences);

  await loadSequenceFromServer();
  refreshPatches();
  refreshSequences();
  refreshMatchButtons();
  drawRoll();
  // start the roll view around C4
  $("roll-wrap").scrollTop = (pitchToRow(72)) * ROLL.cellH - 60;
}

init().catch((e) => toast(e.message, true));
