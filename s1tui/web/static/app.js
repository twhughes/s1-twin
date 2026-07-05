"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  connected: false, hasTarget: false, running: false, paused: false,
  hasBest: false, signalOk: false,
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
  if (name !== "setup") stopPolling();
}

// ── spectrogram rendering ─────────────────────────────────
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

// ── optimization curve ────────────────────────────────────
let curve = [], lastEvals = -1;
function resetCurve() { curve = []; lastEvals = -1; drawCurve(); }
function drawCurve() {
  const cv = $("curve"); if (!cv) return;
  const ctx = cv.getContext("2d"), W = cv.width, H = cv.height;
  ctx.clearRect(0, 0, W, H);
  const padL = 30, padB = 16, padT = 8, padR = 8;
  const x0 = padL, y0 = H - padB, x1 = W - padR, y1 = padT;
  ctx.font = "10px ui-monospace, monospace";
  // horizontal grid + y labels
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
  // candidate dots
  ctx.fillStyle = "#ff2e97"; ctx.globalAlpha = 0.55;
  for (const p of curve) { ctx.beginPath(); ctx.arc(sx(p.x), sy(p.last), 2.2, 0, 6.3); ctx.fill(); }
  ctx.globalAlpha = 1;
  // best-so-far line + glow
  ctx.strokeStyle = "#36f9b3"; ctx.lineWidth = 2; ctx.shadowColor = "#36f9b3"; ctx.shadowBlur = 6;
  ctx.beginPath();
  curve.forEach((p, i) => { const X = sx(p.x), Y = sy(p.best); i ? ctx.lineTo(X, Y) : ctx.moveTo(X, Y); });
  ctx.stroke(); ctx.shadowBlur = 0;
  ctx.fillStyle = "#716c9c"; ctx.fillText("evals " + (n ? curve[n - 1].x : 0), x1 - 64, y0 + 13);
}

// ── status / buttons ──────────────────────────────────────
function setConn(connected, port, channel) {
  state.connected = connected;
  $("dot").className = "dot " + (connected ? "on" : "off");
  const label = connected ? `${port} · ch ${channel ?? ""}`.trim() : "disconnected";
  $("conn-label").textContent = label;
  $("studio-status").innerHTML = connected
    ? `● connected to <b>${port}</b>`
    : `not connected — head to <a href="#" data-view="setup">Setup</a>`;
  $("studio-status").className = "studio-status" + (connected ? " ok" : "");
}

function updateReady() {
  $("ready-banner").classList.toggle("hidden", !(state.connected && state.signalOk));
}

function refreshButtons() {
  $("start").disabled = !(state.connected && state.hasTarget && !state.running);
  $("pause").disabled = !(state.running && !state.paused);
  $("resume").disabled = !(state.running && state.paused);
  $("stop").disabled = !state.running;
  $("save").disabled = !state.hasBest;
  document.querySelector('[data-clip="target"]').disabled = !state.hasTarget;
  document.querySelector('[data-clip="best"]').disabled = !state.hasBest;
  document.querySelector('[data-clip="last"]').disabled = !(state.running || state.hasBest);
}

// ── diagnostics / setup ───────────────────────────────────
function renderCheck(elId, ok, text, cls = "warn") {
  const el = $(elId);
  el.className = "check-row " + (ok ? "ok" : cls);
  el.innerHTML = (ok ? "✓ " : "✗ ") + text;
}

async function loadDiagnostics() {
  try {
    const d = await api("GET", "/api/diagnostics");
    // MIDI
    $("port").innerHTML = d.midi.all.map((p) => `<option ${p === d.midi.port ? "selected" : ""}>${p}</option>`).join("")
      || `<option value="">— no MIDI ports —</option>`;
    renderCheck("chk-midi", d.midi.found,
      d.midi.found ? `S-1 detected: <b>${d.midi.port}</b>` : "No S-1 MIDI port found — plug it in and power it on, then ⟳");
    // Audio
    const devs = d.audio.devices;
    const bhIdx = d.audio.blackhole ? d.audio.blackhole.index : null;
    $("device").innerHTML = devs.map((x) =>
      `<option value="${x.index}" ${x.index === bhIdx ? "selected" : ""}>[${x.index}] ${x.name}</option>`).join("")
      || `<option value="">— no inputs —</option>`;
    // outputs (headphones)
    const outs = d.audio.outputs || [];
    const prefOut = outs.find((o) => /headphone|speaker/i.test(o.name)) || outs[0];
    $("output").innerHTML = outs.map((o) =>
      `<option value="${o.index}" ${prefOut && o.index === prefOut.index ? "selected" : ""}>[${o.index}] ${o.name}</option>`).join("")
      || `<option value="">— no outputs —</option>`;
    // an S-1 audio input would show up here if it did USB audio
    const s1audio = devs.find((x) => /s-?1|aira/i.test(x.name));
    if (s1audio) {
      renderCheck("chk-audio", true, `S-1 audio input detected: <b>[${s1audio.index}] ${s1audio.name}</b> — USB audio works!`);
    } else if (d.audio.blackhole) {
      renderCheck("chk-audio", true, `BlackHole found: <b>[${bhIdx}] ${d.audio.blackhole.name}</b>`);
    } else {
      renderCheck("chk-audio", false,
        `No S-1 audio input. The S-1 sends only MIDI over USB-C — its sound needs a hardware path into an input (see notes).`);
    }
    if (d.monitor && d.monitor.running) setMonitorUI(true);
    setConn(d.connected, d.connected_port, $("channel").value);
    updateReady();
    refreshButtons();
  } catch (e) { toast(e.message, true); }
}

async function connect() {
  try {
    const r = await api("POST", "/api/connect", {
      port: $("port").value,
      channel: parseInt($("channel").value, 10),
      device: parseInt($("device").value, 10),
    });
    setConn(r.connected, r.port, r.channel);
    toast("connected to " + r.port);
    updateReady();
    refreshButtons();
  } catch (e) { toast(e.message, true); }
}

// ── live monitor + level meter ────────────────────────────
let pollTimer = null, monitorOn = false, recOn = false;
function setLevel(db) {
  const pct = Math.max(0, Math.min(100, (db + 60) / 60 * 100));
  $("level-fill").style.width = pct + "%";
  $("level-val").textContent = db <= -119 ? "—" : db.toFixed(0) + " dB";
}
async function pollLevel() {
  const dev = $("device").value;
  if (dev === "") return;
  try { setLevel((await api("GET", `/api/level?device=${dev}`)).peak_db); } catch (_) {}
}
function startPolling() { if (!pollTimer) pollTimer = setInterval(pollLevel, 200); }
function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } setLevel(-120); }

// Don't burn 5 requests/sec on a hidden tab — the meter isn't visible anyway.
document.addEventListener("visibilitychange", () => {
  if (document.hidden) { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }
  else if (monitorOn && document.querySelector("#view-setup.active")) startPolling();
});

function setMonitorUI(on) {
  monitorOn = on;
  $("monitor").classList.toggle("active", on);
  $("monitor").textContent = on ? "■ STOP MONITOR" : "▸ START MONITOR";
  $("record").disabled = !on;
  if (on) startPolling(); else stopPolling();
}
async function toggleMonitor() {
  try {
    if (monitorOn) { await api("POST", "/api/monitor/stop"); setMonitorUI(false); toast("monitor stopped"); }
    else {
      await api("POST", "/api/monitor/start", { input: parseInt($("device").value, 10), output: parseInt($("output").value, 10), gain: parseFloat($("gain").value) });
      setMonitorUI(true); toast("monitor on — you should hear the S-1");
    }
  } catch (e) { toast(e.message, true); }
}
async function toggleRecord() {
  try {
    if (recOn) {
      const name = $("rec-name").value.trim() || "s1-take";
      const r = await api("POST", "/api/monitor/record/stop", { name, as_target: $("rec-target").checked });
      recOn = false; $("record").classList.remove("active"); $("record").textContent = "● RECORD";
      toast(`saved ${name} (${r.duration}s)`);
      if (r.spectrogram) { drawSpec("spec-target", r.spectrogram); state.hasTarget = true; refreshButtons(); toast("set as match target"); }
    } else {
      await api("POST", "/api/monitor/record/start");
      recOn = true; $("record").classList.add("active"); $("record").textContent = "■ STOP REC";
    }
  } catch (e) { toast(e.message, true); }
}

// ── signal test ───────────────────────────────────────────
async function testSignal() {
  const btn = $("test-signal"), res = $("test-result");
  btn.disabled = true; btn.textContent = "TESTING…"; res.className = "test-result";
  res.textContent = "playing a note and listening…";
  try {
    const r = await api("POST", "/api/test/signal", { device: parseInt($("device").value, 10) });
    if (r.detected) {
      state.signalOk = true;
      res.className = "test-result ok";
      res.innerHTML = `✓ Heard the S-1! &nbsp; peak <b>${r.peak_db} dB</b> &nbsp;·&nbsp; latency <b>${r.latency_ms} ms</b>`;
    } else {
      state.signalOk = false;
      res.className = "test-result err";
      res.innerHTML = `✗ No audio detected (peak ${r.peak_db} dB). Check Logic is routing to this device — see the guide below.`;
    }
  } catch (e) {
    state.signalOk = false;
    res.className = "test-result err";
    res.textContent = "✗ " + e.message;
  } finally {
    btn.disabled = false; btn.textContent = "TEST SIGNAL";
    updateReady();
  }
}

// ── target upload ─────────────────────────────────────────
async function uploadFile(file) {
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await api("POST", "/api/target", fd, true);
    state.hasTarget = true;
    drawSpec("spec-target", r.spectrogram);
    $("drop").querySelector("strong").textContent = file.name;
    toast(`target loaded (${r.duration}s)`);
    refreshButtons();
  } catch (e) { toast(e.message, true); }
}

// ── match lifecycle ───────────────────────────────────────
async function start() {
  try {
    await api("POST", "/api/match/start", {
      max_iters: parseInt($("iters").value, 10),
      include_effects: $("fx").checked,
      optimizer: $("optimizer").value,
      mode: $("mode").value,
      calibrate: $("calib").checked,
    });
    state.running = true; state.paused = false;
    state.hasBest = false;
    resetCurve();
    $("state-label").textContent = $("calib").checked ? "calibrating…" : "running…";
    refreshButtons();
  } catch (e) { toast(e.message, true); }
}
const pause = () => api("POST", "/api/match/pause").catch((e) => toast(e.message, true));
const resume = () => api("POST", "/api/match/resume").catch((e) => toast(e.message, true));
const stop = () => api("POST", "/api/match/stop").catch((e) => toast(e.message, true));
let currentAudio = null;
function playClip(which) {
  if (currentAudio) { currentAudio.pause(); currentAudio.src = ""; }
  currentAudio = new Audio(`/api/clip/${which}?t=${Date.now()}`);
  currentAudio.play().catch((e) => toast("playback failed: " + e.message, true));
}

// ── live progress (WebSocket) ─────────────────────────────
let wsRetryMs = 1000, lastShownError = null;
function connectWS() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => { wsRetryMs = 1000; };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    state.running = !!m.running;
    state.paused = !!m.paused;
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
    if (m.target_spec) drawSpec("spec-target", m.target_spec);
    if (m.best_spec) { drawSpec("spec-best", m.best_spec); state.hasBest = true; }
    if (m.error) {
      $("state-label").textContent = "error: " + m.error;
      if (m.error !== lastShownError) { toast(m.error, true); lastShownError = m.error; }
    } else if (m.done) $("state-label").textContent = "done";
    else if (state.paused) $("state-label").textContent = "paused · listen";
    else if (state.running) $("state-label").textContent = "running…";
    refreshButtons();
  };
  ws.onclose = () => {
    $("state-label").textContent = "connection lost — retrying…";
    setTimeout(connectWS, wsRetryMs);
    wsRetryMs = Math.min(wsRetryMs * 2, 10000);
  };
}

// ── bank ──────────────────────────────────────────────────
async function refreshPatches() {
  const list = await api("GET", "/api/patches");
  const ul = $("patch-list");
  ul.className = "patch-list" + (list.length ? "" : " empty");
  // Built with textContent/dataset, never innerHTML — patch names are user
  // data and must not be parsed as markup.
  ul.replaceChildren(...list.map((p) => {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.className = "pname";
    name.textContent = p.name;
    const meta = document.createElement("span");
    meta.className = "pmeta";
    const c = p.metadata?.closeness;
    meta.textContent = c !== undefined ? `${c}%` : "";
    const btns = document.createElement("span");
    btns.className = "pbtns";
    const play = document.createElement("button");
    play.className = "btn tiny";
    play.dataset.play = p.name;
    play.textContent = "▸";
    const del = document.createElement("button");
    del.className = "btn tiny danger";
    del.dataset.del = p.name;
    del.textContent = "✕";
    btns.append(play, del);
    li.append(name, meta, btns);
    return li;
  }));
}
async function saveBest() {
  const name = $("save-name").value.trim();
  if (!name) { toast("name the patch first", true); return; }
  try {
    await api("POST", "/api/patches", { name, overwrite: false });
    toast(`saved ${name}`);
  } catch (e) {
    if (String(e.message).includes("exists")) {
      if (confirm(`Overwrite "${name}"?`)) {
        await api("POST", "/api/patches", { name, overwrite: true });
        toast(`overwrote ${name}`);
      } else return;
    } else { toast(e.message, true); return; }
  }
  $("save-name").value = "";
  refreshPatches();
}

// ── wiring ────────────────────────────────────────────────
function init() {
  loadDiagnostics();
  refreshPatches();
  connectWS();
  drawCurve();

  $("connect").onclick = connect;
  $("refresh").onclick = loadDiagnostics;
  $("monitor").onclick = toggleMonitor;
  $("gain").oninput = () => { if (monitorOn) api("POST", "/api/monitor/gain", { gain: parseFloat($("gain").value) }).catch(() => {}); };
  $("record").onclick = toggleRecord;
  $("test-signal").onclick = testSignal;
  $("start").onclick = start;
  $("pause").onclick = pause;
  $("resume").onclick = resume;
  $("stop").onclick = stop;
  $("save").onclick = saveBest;

  const drop = $("drop"), file = $("file");
  drop.onclick = () => file.click();
  file.onchange = () => uploadFile(file.files[0]);
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); uploadFile(e.dataTransfer.files[0]); };

  document.body.addEventListener("click", (e) => {
    const view = e.target.dataset.view;
    if (view) { e.preventDefault(); return switchView(view); }
    const clip = e.target.dataset.clip;
    if (clip) return playClip(clip);
    const play = e.target.dataset.play;
    if (play) return api("POST", `/api/patches/${encodeURIComponent(play)}/play`).then(() => toast(`▸ ${play}`)).catch((err) => toast(err.message, true));
    const del = e.target.dataset.del;
    if (del && confirm(`Delete "${del}"?`)) {
      api("DELETE", `/api/patches/${encodeURIComponent(del)}`).then(() => { toast(`deleted ${del}`); refreshPatches(); }).catch((err) => toast(err.message, true));
    }
  });

  refreshButtons();
}

init();
