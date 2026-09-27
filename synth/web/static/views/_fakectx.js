// views/_fakectx.js — W-match's stand-in for core/ctx.js, so match.js and sequencer.js can be
// mounted and screenshotted before the shell exists. The lead deletes it at integration.
// It implements docs/design/BUILD.md §2.2 against the real cockpit routes (/api/*, /ws/state),
// with a small fake twin (§2.3 surface; a plain Web Audio voice, not the real twin DSP).

const SYNC = { disconnected: "offline", connecting: "pending", listening: "pending", synced: "synced" };
const MODELED = new Set([20, 19, 21, 23, 15, 74, 71, 24, 25, 26, 73, 75, 30, 72, 3, 13, 17, 76, 22, 12, 28]);

export async function createCtx({ staticMode = false, toastEl = null } = {}) {
  const handlers = new Map();
  const emit = (ev, payload) => {
    for (const fn of handlers.get(ev) || []) { try { fn(payload); } catch (e) { console.error(e); } }
  };
  const params = new Map();
  const twin = createFakeTwin();
  let status = { sync: "offline", port: null, monitor: null, keyboards: [], mode: "solo" };
  let schema = { sections: [], menu: [], midi: [], prm: [] };
  let server = null, stateWS = null, closed = false, retry = 1000;

  const mapStatus = (st = {}) => ({
    sync: SYNC[st.sync] || "offline", port: st.port ?? null, monitor: st.monitor ?? null,
    keyboards: st.keyboards || [], mode: st.mode || "solo",
  });

  if (!staticMode) {
    const api = async (method, path, body) => {
      const opts = { method, headers: {} };
      if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
      const r = await fetch(path, opts);
      if (!r.ok) {
        let detail = r.statusText;
        try { detail = (await r.json()).detail || detail; } catch (_) { /* not JSON */ }
        const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
        err.status = r.status;
        throw err;
      }
      return (r.headers.get("content-type") || "").includes("json") ? r.json() : r;
    };
    const ws = (path) => new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${path}`);
    server = { api, ws };
    schema = await api("GET", "/api/schema");
    const st = await api("GET", "/api/state");
    for (const [cc, v] of Object.entries(st.params)) params.set(Number(cc), v);
    status = mapStatus(st.status);
    const connect = () => {
      if (closed) return;
      stateWS = ws("/ws/state");
      stateWS.onopen = () => { retry = 1000; };
      stateWS.onmessage = (ev) => {
        let m; try { m = JSON.parse(ev.data); } catch (_) { return; }
        if (m.type === "hello") {
          for (const [cc, v] of Object.entries(m.params || {})) setLocal(Number(cc), v, "patch");
          status = mapStatus(m.status); emit("status", status);
        } else if (m.type === "param") {
          if (params.get(m.cc) !== m.value) setLocal(m.cc, m.value, m.source === "midi" ? "midi" : "patch");
        } else if (m.type === "sync") {
          status = { ...status, sync: SYNC[m.state] || "offline", port: m.port ?? null }; emit("status", status);
        } else if (m.type === "monitor") {
          status = { ...status, monitor: m }; emit("status", status);
        } else if (m.type === "keyboards") {
          status = { ...status, keyboards: m.names || [] }; emit("status", status);
        } else if (m.type === "mode") {
          status = { ...status, mode: m.mode }; emit("status", status);
        }
      };
      stateWS.onclose = () => {
        if (closed) return;
        setTimeout(connect, retry);
        retry = Math.min(retry * 2, 10000);
      };
    };
    connect();
  }
  twin.setAll(Object.fromEntries(params));

  function setLocal(cc, v, source) {
    params.set(cc, v);
    twin.set(cc, v);
    emit("param", { cc, value: v, source });
  }

  let toastTimer = 0;
  const ctx = {
    schema, params, twin, server,
    set(cc, v, { source = "ui" } = {}) {
      v = Math.round(v);
      setLocal(cc, v, source);
      if (!server || source === "midi") return;
      if (stateWS && stateWS.readyState === WebSocket.OPEN) stateWS.send(JSON.stringify({ type: "param", cc, value: v }));
      else server.api("PUT", `/api/params/${cc}`, { value: v }).catch((e) => ctx.toast(e.message));
    },
    on(ev, fn) {
      if (!handlers.has(ev)) handlers.set(ev, new Set());
      handlers.get(ev).add(fn);
      return () => handlers.get(ev)?.delete(fn);
    },
    off(ev, fn) { handlers.get(ev)?.delete(fn); },
    note(n, on, vel = 100) {
      if (ctx.soundSource === "s1" && stateWS && stateWS.readyState === WebSocket.OPEN) {
        stateWS.send(JSON.stringify({ type: "note", note: n, velocity: vel, on }));
      } else if (on) twin.noteOn(n, vel);
      else twin.noteOff(n);
    },
    toast(msg) {
      if (!toastEl) { console.info("[toast]", msg); return; }
      toastEl.textContent = msg;
      toastEl.classList.add("on");
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => toastEl.classList.remove("on"), 2600);
    },
    get soundSource() { return status.sync === "synced" ? "s1" : "twin"; },
    get status() { return status; },
    destroy() { closed = true; try { stateWS?.close(); } catch (_) { /* closed */ } twin.allOff(); },
  };
  return ctx;
}

// The fake twin: the §2.3 surface with a plain saw → lowpass → gain voice. Not the real twin.
function createFakeTwin() {
  const p = new Map();
  let ac = null, out = null;
  const voices = new Map();
  const calls = [];
  const log = (c) => { calls.push(c); if (calls.length > 200) calls.shift(); };
  const ensure = () => {
    if (!ac) {
      ac = new (window.AudioContext || window.webkitAudioContext)();
      out = ac.createGain(); out.gain.value = 0.18; out.connect(ac.destination);
    }
    return ac;
  };
  const hz = (n) => 440 * Math.pow(2, (n - 69) / 12);
  const secs = (v) => 0.002 * Math.pow(1500, (v ?? 0) / 127);
  function noteOff(note) {
    log(["off", note]);
    const v = voices.get(note);
    if (!v) return;
    voices.delete(note);
    const t = ac.currentTime, r = secs(p.get(72) ?? 21);
    v.g.gain.cancelScheduledValues(t);
    v.g.gain.setTargetAtTime(0, t, r / 3);
    v.o.stop(t + r + 0.1);
  }
  return {
    fake: true,
    calls,
    set(cc, v) { p.set(Number(cc), v); },
    setAll(map) { for (const [cc, v] of Object.entries(map || {})) p.set(Number(cc), v); },
    noteOn(note, vel = 100) {
      log(["on", note, vel]);
      const A = ensure();
      if (A.state !== "running") return;   // no user gesture yet
      if (voices.has(note)) noteOff(note);
      const t = A.currentTime;
      const o = A.createOscillator(); o.type = (p.get(20) ?? 0) >= (p.get(19) ?? 127) ? "sawtooth" : "square";
      o.frequency.value = hz(note);
      const f = A.createBiquadFilter(); f.type = "lowpass";
      f.frequency.value = 40 * Math.pow(400, (p.get(74) ?? 127) / 127);
      f.Q.value = 0.7 + 12 * (p.get(71) ?? 0) / 127;
      const g = A.createGain(); g.gain.value = 0;
      const a = secs(p.get(73) ?? 0), s = (p.get(30) ?? 25) / 127, d = secs(p.get(75) ?? 42);
      const peak = 0.25 + 0.75 * vel / 127;
      g.gain.setTargetAtTime(peak, t, a / 3 + 0.001);
      g.gain.setTargetAtTime(peak * s, t + a, d / 3 + 0.001);
      o.connect(f); f.connect(g); g.connect(out);
      o.start(t);
      voices.set(note, { o, g });
    },
    noteOff,
    allOff() { for (const n of [...voices.keys()]) noteOff(n); },
    resume() { return ensure().resume(); },
    taps: null,
    async renderStages({ seconds = 1 } = {}) {
      const sr = 22050, n = Math.round(sr * seconds), z = () => new Float32Array(n);
      return { sr, osc: z(), filter: z(), amp: z(), fx: z(), out: z() };
    },
    modeled: (cc) => MODELED.has(Number(cc)),
    level: () => 0,
  };
}
