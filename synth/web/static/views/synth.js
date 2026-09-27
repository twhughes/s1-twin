// views/synth.js — the plate: the S-1's signal chain as a cyanotype specimen plate
// (docs/design/DIRECTION.md; visual and copy spec: docs/design/menura-comp.html).
//
// Row 1 is the chain, one well per stage on one signal line: Oscillator, Filter, Amplifier, Effects,
// Output (the plume). Row 2 holds the modulators (LFO under Oscillator, Envelope under Filter and
// Amplifier) and the Keys; dotted leaders run from a modulator's top edge up to each control it
// drives, their weight the amount. Every control comes from the schema through core/layout.js.
//
// The wells draw ctx.twin.taps live while the twin sounds a note, and ctx.twin.renderStages() the rest
// of the time (re-rendered, debounced, on every change). When the S-1 is the sound, the Output well
// draws its real signal (GET /api/monitor/raw) as the bronze plume with the twin's prediction dotted
// over it. A held note lends the chain its pitch color as a halo.

import * as draw from "../design/draw.js";
import { rgbOf, rgba, noteName } from "../design/colors.js";
import { plateLayout, linkWeights } from "../core/layout.js";
import { buildControl, bindControls } from "../core/controls.js";
import { readHash, isStill } from "../core/flags.js";

export const id = "synth";
export const title = "Synth";

const IDLE_NOTE = 45;                          // A2: what the wells draw while nothing is held
const AMP_T = 1.5, FX_T = 2.8, GATE = 0.62;    // seconds in the Amplifier / Effects wells; key up
const STAGES = ["osc", "filter", "amp", "fx", "out"];
const RAW_MS = 66;                             // the S-1's live plume: about 15 polls a second
const RENDER_MS = 80;                          // while a knob moves, the wells re-render this often
const LINKED = new Set([13, 15, 16, 24, 25, 28]);
const FIT_MIN = 0.7;                           // the plate scales down to fit a small window, not below this
const STACKED = 1180;                          // at or below this width the plate stacks and scrolls (app.css)
const ENV_CCS = new Set([73, 75, 30, 72]);
// The envelope drawing's time curves mirror synth/match/twin.py DEFAULT_CURVES (seconds).
const expCurve = (lo, hi) => (v) => lo * (hi / lo) ** (v / 127);
const ATTACK = expCurve(0.001, 2), DECAY = expCurve(0.005, 4), RELEASE = expCurve(0.005, 4);
let developed = false;                         // the page-load exposure happens once per page

let plate = null;
export function mount(root, ctx) { plate = new Plate(root, ctx); }
export function unmount() { plate?.destroy(); plate = null; }
export default { id, title, mount, unmount };

const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
};

// ── signal helpers ─────────────────────────────────────────────────────────
function peakOf(a, from = 0, to = a.length) {
  let m = 0;
  for (let i = Math.max(0, from); i < Math.min(a.length, to); i++) { const v = Math.abs(a[i]); if (v > m) m = v; }
  return m;
}
function risingZero(a, from, span) {
  for (let i = Math.max(1, from); i < Math.min(a.length - 1, from + span); i++) if (a[i - 1] <= 0 && a[i] > 0) return i;
  return Math.max(2, from);
}
/** Peak per column between samples i0 and i1 (not normalized). `reach` widens each column's window
 *  to at least that many samples, so a column narrower than a cycle still finds the cycle's peak:
 *  an envelope follower, not a flicker. */
function peaks(a, cols, i0, i1, reach = 0) {
  const out = new Float32Array(cols), step = (i1 - i0) / cols, pad = Math.max(0, (reach - step) / 2);
  for (let c = 0; c < cols; c++) {
    out[c] = peakOf(a, Math.floor(i0 + c * step - pad), Math.floor(i0 + (c + 1) * step + pad));
  }
  return out;
}
/** A steady window of a periodic signal: {data, from, spc, cycles}, or null if it has no clear pitch. */
function windowOf(data, { from = 2, spc = 0, want = 3 } = {}) {
  if (!data || data.length < 64) return null;
  if (peakOf(data) < 1e-4) return null;
  const period = spc || draw.period(data, 8, Math.min(2048, Math.floor(data.length / 2)));
  if (!period) return null;
  const start = risingZero(data, from, period + 2);
  const cycles = Math.min(want, Math.floor((data.length - start - 3) / period));
  return cycles >= 1 ? { data, from: start, spc: period, cycles } : null;
}

/** The plume's specimen: several periods folded into one averaged cycle (a synchronous average), so
 *  what repeats (the tone) stays and what does not (reverb, noise, a passing echo) averages away.
 *  Returns the cycle tiled three times with {from, spc} pointing at the middle copy, or null. */
function fold(win, maxCycles = 8) {
  if (!win) return null;
  const { data, from, spc } = win;
  const k = Math.min(maxCycles, Math.floor((data.length - from - 3) / spc));
  if (k < 1 || spc < 8) return null;
  const cyc = new Float32Array(spc);
  for (let c = 0; c < k; c++) for (let j = 0; j < spc; j++) cyc[j] += data[from + c * spc + j];
  let rms = 0;
  for (let j = 0; j < spc; j++) { cyc[j] /= k; rms += cyc[j] * cyc[j]; }
  if (Math.sqrt(rms / spc) < 1e-5) return null;
  // A gentle circular smoothing (binomial passes, about a 48th of a cycle wide): the plume's y axis
  // is the derivative, which weights each partial by its number, so a bright resonance would
  // otherwise scribble. The loop's shape and its sharp turns stay.
  const passes = Math.min(60, Math.round(2 * (spc / 96) ** 2));
  const tmp = new Float32Array(spc);
  for (let m = 0; m < passes; m++) {
    for (let j = 0; j < spc; j++) tmp[j] = 0.25 * cyc[(j - 1 + spc) % spc] + 0.5 * cyc[j] + 0.25 * cyc[(j + 1) % spc];
    cyc.set(tmp);
  }
  const tiled = new Float32Array(spc * 3);
  tiled.set(cyc, 0); tiled.set(cyc, spc); tiled.set(cyc, 2 * spc);
  return { data: tiled, from: spc, spc };
}

class Plate {
  constructor(root, ctx) {
    this.ctx = ctx;
    this.root = root;
    this.alive = true;
    this.flags = readHash().flags;
    this.still = isStill(this.flags);
    this.layout = plateLayout(ctx.schema);
    this.controls = new Map();
    this.wells = {};
    this.blocks = {};
    this.down = [];                 // held notes, oldest first: [{note, t0}]
    this.released = null;           // {note, t0, off, at} while a released note rings
    this.stages = null;             // the latest renderStages() result, prepared for drawing
    this.real = null;               // the S-1's latest window (bronze plume)
    this.dirty = true;
    this.revealT0 = null;
    this.tapBufs = {};
    this.offs = [];

    this.build();
    this.offs.push(bindControls(ctx, this.controls));
    this.offs.push(ctx.on("param", (e) => this.onParam(e)));
    this.offs.push(ctx.on("status", () => this.onStatus()));
    this.offs.push(ctx.on("note", (e) => this.onNote(e)));
    if (ctx.keys) {
      this.board = ctx.keys.mount(this.kbd);
      ctx.keys.qwerty(true);
      this.offs.push(ctx.keys.onChange(() => this.keysHint()));
    }
    this.keysHint();
    this.caption();
    this.renderSoon(IDLE_NOTE, 0);
    this.onStatus();

    this.resize = new ResizeObserver(() => { this.fitSoon(); this.drawADSR(); this.dirty = true; });
    this.resize.observe(this.grid);
    this.onWindow = () => this.fitSoon();
    window.addEventListener("resize", this.onWindow);
    const ready = document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve();
    ready.then(() => {
      if (!this.alive) return;
      this.fit();
      this.drawOverlay();
      this.drawADSR();
      this.develop();
      this.demo();
    });
    this.loop = this.loop.bind(this);
    this.raf = requestAnimationFrame(this.loop);
  }

  // ── the page ─────────────────────────────────────────────────────────────
  build() {
    const page = el("div", "plate");
    this.el = page;
    this.overlay = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    this.overlay.setAttribute("class", "overlay");
    this.overlay.setAttribute("aria-hidden", "true");
    this.overlay.innerHTML = '<path class="sig"/><path class="heads"/><g class="leads"></g>';
    this.grid = el("section", "grid");
    this.grid.setAttribute("aria-label", "The synth, in signal order");
    for (const b of this.layout.plate) this.grid.append(this.block(b));
    page.append(this.overlay, this.grid);
    this.box = el("div", "plate-box");            // holds the plate's scaled size (see fit())
    this.box.append(page);
    this.root.append(this.box);
  }

  block(b) {
    const node = el("div", `${b.kind === "stage" ? "stage" : "mod"} blk-${b.id}`);
    this.blocks[b.id] = node;
    if (b.kind === "stage") {
      const wrap = el("div", "well-wrap");
      const canvas = el("canvas", "well");
      canvas.setAttribute("role", "img");
      canvas.setAttribute("aria-label", b.well);
      wrap.append(canvas);
      this.wells[b.id] = canvas;
      const cap = el("h2", "caption", b.title);
      if (b.id === "out") { this.outNote = el("span", "note"); cap.append(this.outNote); }
      node.append(wrap, cap);
      if (b.rows.length) node.append(this.rows(b.rows, el("div", "controls")));
      for (const sub of b.blocks) {
        const inner = el("div", `mod blk-${sub.id}`);
        this.blocks[sub.id] = inner;
        inner.append(el("h3", null, sub.title), this.rows(sub.rows, el("div", "controls")));
        node.append(inner);
      }
      return node;
    }
    if (b.kind === "keys") {
      node.setAttribute("aria-label", "Keyboard");
      const head = el("div", "keys-head");
      const oct = el("span", "octave");
      const lower = el("button", "quiet", "Octave down");
      const upper = el("button", "quiet", "Octave up");
      this.octRange = el("span", "oct-range");
      lower.type = upper.type = "button";
      lower.addEventListener("click", () => this.ctx.keys?.octave(-1));
      upper.addEventListener("click", () => this.ctx.keys?.octave(1));
      oct.append(lower, this.octRange, upper);
      head.append(el("h3", null, b.title), oct);
      this.hint = el("p", "keys-hint");
      this.kbd = el("div", "keyboard");
      node.append(head, this.hint, this.kbd);
      return node;
    }
    if (b.head) {
      const head = el("div", "mod-head");
      head.append(el("h3", null, b.title), this.rows([b.head], el("div", "head-controls")));
      node.append(head);
    } else {
      node.append(el("h3", null, b.title));
    }
    if (b.adsr) {
      this.adsr = el("canvas", "adsr");
      this.adsr.setAttribute("role", "img");
      this.adsr.setAttribute("aria-label", "Envelope shape");
      node.append(this.adsr);
    }
    node.append(this.rows(b.rows, el("div", "controls")));
    return node;
  }

  rows(rows, into) {
    for (const r of rows) {
      if (r.type === "more") {
        const det = el("details", "more");
        det.append(el("summary", null, r.title));
        this.rows(r.rows, det);
        det.addEventListener("toggle", () => { this.drawOverlay(); this.fitSoon(); });
        const above = into.lastElementChild;
        (above && above.classList.contains("row") ? above : into).append(det);
        continue;
      }
      const row = el("div", r.wide ? "row wide" : "row");
      for (const it of r.items) {
        if (it.type === "pair") {
          const pair = el("div", "pair");
          it.items.forEach((c) => pair.append(this.control(c)));
          row.append(pair);
        } else {
          const node = this.control(it);
          if (it.gap) node.classList.add("gap");
          row.append(node);
        }
      }
      into.append(row);
    }
    return into;
  }

  control(item) {
    const c = buildControl(item.spec, this.ctx);
    this.controls.set(item.cc, c);
    return c.el;
  }

  // ── words ────────────────────────────────────────────────────────────────
  hardware() { return this.ctx.soundSource === "s1" || Boolean(this.ctx.status.demo); }

  caption() {
    if (!this.outNote) return;
    const top = this.top();
    let words;
    if (this.hardware()) {
      const monitor = this.ctx.status.demo || (this.ctx.status.monitor && this.ctx.status.monitor.running);
      words = monitor
        ? "From the S-1, live. The dotted line is the twin's prediction."
        : "The S-1's audio is not reaching this Mac, so only the twin's prediction is drawn (dotted).";
    } else {
      words = this.ctx.twinInfo && this.ctx.twinInfo.curves === "calibrated"
        ? "Each loop is one cycle of the sound. The twin draws it, calibrated against a real S-1."
        : "Each loop is one cycle of the sound. The twin draws it and is uncalibrated until it is measured against a real S-1.";
    }
    this.outNote.textContent = top ? `${noteName(top.note)}. ${words}` : words;
  }

  keysHint() {
    if (!this.hint) return;
    const touch = typeof matchMedia === "function" && matchMedia("(hover: none)").matches;
    this.hint.textContent = touch
      ? "Touch the keys to play."
      : "Play with A to K. Z and X change the octave. Space plays the sequence, and ? shows every key.";
    if (this.ctx.keys) this.octRange.textContent = this.ctx.keys.range();
  }

  // ── events ───────────────────────────────────────────────────────────────
  onParam({ cc }) {
    this.renderSoon(this.noteForStages());
    if (LINKED.has(cc)) this.drawOverlay();
    if (ENV_CCS.has(cc)) this.drawADSR();
  }

  onStatus() {
    this.caption();
    this.fitSoon();
    const poll = this.ctx.server && !this.ctx.status.demo && this.ctx.soundSource === "s1"
      && this.ctx.status.monitor && this.ctx.status.monitor.running;
    if (poll) this.startPolling(); else this.stopPolling();
    this.dirty = true;
  }

  top() {
    if (this.down.length) return this.down[this.down.length - 1];
    return this.released;
  }

  noteForStages() { const t = this.top(); return t ? t.note : IDLE_NOTE; }

  onNote({ note, on }) {
    const now = performance.now();
    const i = this.down.findIndex((d) => d.note === note);
    if (on) {
      if (i >= 0) this.down.splice(i, 1);
      this.down.push({ note, t0: now });
      this.released = null;
      if (!this.stages || this.stages.note !== note) this.renderSoon(note, 0);
    } else if (i >= 0) {
      const d = this.down.splice(i, 1)[0];
      if (!this.down.length) this.released = { note: d.note, t0: d.t0, off: (now - d.t0) / 1000, at: now };
    }
    this.caption();
    this.dirty = true;
  }

  // ── stages: render (debounced) and prepare for drawing ────────────────────
  /** Re-render the wells: a new note at once; a moving knob at most every RENDER_MS (a throttle, so
   *  the wells follow the hand during a drag; renderNow() coalesces anything that lands mid-render). */
  renderSoon(note, delay = RENDER_MS) {
    this.wantNote = note;
    if (delay === 0) { clearTimeout(this.renderTimer); this.renderTimer = 0; this.renderNow(); return; }
    if (!this.renderTimer) this.renderTimer = setTimeout(() => { this.renderTimer = 0; this.renderNow(); }, delay);
  }

  async renderNow() {
    if (this.rendering) { this.renderAgain = true; return; }
    this.rendering = true;
    const note = this.wantNote;
    try {
      const st = await this.ctx.twin.renderStages({ note, seconds: FX_T, gate: GATE });
      if (this.alive && st) { this.stages = this.prepare(st, note); this.dirty = true; }
    } catch {
      // keep the last drawing; the next change renders again
    } finally {
      this.rendering = false;
      if (this.renderAgain && this.alive) { this.renderAgain = false; this.renderNow(); }
    }
  }

  prepare(st, note) {
    const sr = st.sr, n = st.out.length, gateN = Math.min(n, Math.floor(GATE * sr)), early = Math.floor(0.03 * sr);
    const guess = Math.round(sr / (440 * 2 ** ((note - 69) / 12)));
    const probeAt = Math.max(0, gateN - Math.floor(sr * 0.12));
    const probe = st.osc.subarray(probeAt, Math.min(n, probeAt + Math.floor(sr * 0.1)));
    const spc = draw.period(probe, 8, Math.floor(probe.length / 2)) || guess;
    // The specimen is the settled note just before key up: the envelope has reached its sustain and
    // the filter has stopped sweeping. A note that has died away by then is drawn at its loudest.
    let waveAt = Math.max(early, gateN - Math.ceil(4 * spc) - 4);
    let plumeAt = Math.max(early, gateN - 9 * spc);
    if (peakOf(st.out, waveAt, waveAt + 3 * spc) < 0.15 * peakOf(st.out, 0, gateN)) {
      const env = peaks(st.amp, 64, 0, gateN, 2 * spc);
      let loud = 0;
      for (let c = 1; c < env.length; c++) if (env[c] > env[loud]) loud = c;
      waveAt = plumeAt = Math.max(early, Math.floor((loud + 0.5) * gateN / env.length));
    }
    const from = risingZero(st.osc, waveAt, spc + 2);
    const fxT = Math.min(n, Math.floor(FX_T * sr));
    const dry = peaks(st.amp, 256, 0, fxT, 2 * spc), wet = peaks(st.fx, 256, 0, fxT, 2 * spc);
    const top = Math.max(1e-9, ...dry, ...wet);
    const prepared = { ...st, note, win: { from, spc }, shape: dry.map((v) => v / top), tops: {} };
    // the plume: up to eight cycles of the output, folded into one (see fold())
    const plumeFrom = risingZero(st.out, plumeAt, spc + 2);
    prepared.plume = fold({ data: st.out, from: plumeFrom, spc });
    // the demo's "real" S-1: the twin's signal nudged (a hair duller, a touch of a stray partial)
    if (this.ctx.status.demo) {
      const len = Math.min(n - plumeFrom, 9 * spc + 8), fake = new Float32Array(len);
      const pk = peakOf(st.out, plumeFrom, plumeFrom + len);
      let y = 0;
      for (let i = 0; i < len; i++) {
        y += 0.5 * (st.out[plumeFrom + i] - y);
        fake[i] = 0.97 * y + 0.03 * pk * Math.sin((2 * Math.PI * 3 * i) / spc + 0.8);
      }
      prepared.fake = fold({ data: fake, from: risingZero(fake, 3, spc + 2), spc });
    }
    return prepared;
  }

  /** Amplifier / Effects well columns for a canvas width (cached per render and width). */
  tops(w) {
    const st = this.stages, cols = Math.max(2, Math.floor(w));
    if (st.tops.w === cols) return st.tops;
    const ampN = Math.min(st.amp.length, Math.floor(AMP_T * st.sr)), fxN = Math.min(st.fx.length, Math.floor(FX_T * st.sr));
    const reach = 2 * st.win.spc;
    const amp = peaks(st.amp, cols, 0, ampN, reach);
    const dry = peaks(st.amp, cols, 0, fxN, reach), wet = peaks(st.fx, cols, 0, fxN, reach);
    const a = Math.max(1e-9, ...amp), f = Math.max(1e-9, ...dry, ...wet);
    st.tops = { w: cols, amp: amp.map((v) => v / a), dry: dry.map((v) => v / f), wet: wet.map((v) => v / f) };
    return st.tops;
  }

  /** 0..1 loudness of the canonical note at time t into the note (for the plume's size). */
  levelAt(t) {
    const st = this.stages;
    if (!st || t == null) return 1;
    let u = t;
    if (this.down.length) u = Math.min(t, GATE * 0.95);
    else if (this.released) u = GATE + (t - this.released.off);
    const i = Math.max(0, Math.min(st.shape.length - 1, Math.floor(u / FX_T * st.shape.length)));
    return st.shape[i];
  }

  // ── the live picture ──────────────────────────────────────────────────────
  /** The twin's taps while it sounds a note: {osc, filter, out} windows, or null. */
  liveTaps() {
    if (!this.top() || this.ctx.soundSource !== "twin") return null;
    let taps;
    try { taps = this.ctx.twin.taps; } catch { return null; }
    if (!taps || !taps.out) return null;
    const grab = (name) => {
      const a = taps[name];
      if (!a) return null;
      const buf = this.tapBufs[name] && this.tapBufs[name].length === a.fftSize ? this.tapBufs[name] : (this.tapBufs[name] = new Float32Array(a.fftSize));
      a.getFloatTimeDomainData(buf);
      return buf;
    };
    // one period per note for all three taps, re-measured at most four times a second
    const outBuf = grab("out");
    if (!outBuf || peakOf(outBuf) < 1e-4) return null;
    const note = this.top().note, now = performance.now(), lp = this.livePeriod;
    let spc = lp && lp.note === note && now - lp.at < 250 ? lp.spc : 0;
    if (!spc) {
      spc = draw.period(outBuf, 8, Math.floor(outBuf.length / 2));
      this.livePeriod = { note, spc, at: now };
    }
    if (!spc) return null;
    const out = fold(windowOf(outBuf, { spc }));
    if (!out) return null;
    return { osc: windowOf(grab("osc"), { spc }), filter: windowOf(grab("filter"), { spc }), out };
  }

  startPolling() {
    if (this.pollTimer) return;
    const tick = async () => {
      this.pollTimer = setTimeout(tick, RAW_MS);
      if (document.hidden || this.polling) return;
      this.polling = true;
      try {
        const r = await this.ctx.server.api("GET", "/api/monitor/raw?n=2048");
        if (!this.pollTimer) return;                    // polling stopped while this was in flight
        const data = Float32Array.from(r.samples || []);
        this.real = r.running ? fold(windowOf(data, { want: 2 })) : null;
        this.dirty = true;
      } catch {
        this.real = null;
      } finally {
        this.polling = false;
      }
    };
    tick();
  }

  stopPolling() { clearTimeout(this.pollTimer); this.pollTimer = 0; this.real = null; }

  reveal(i, now) {
    if (this.revealT0 == null) return 1;
    return Math.max(0, Math.min(1, (now - this.revealT0 - 250 - i * 110) / 520));
  }

  loop(now) {
    this.raf = requestAnimationFrame(this.loop);
    if (this.released && (now - this.released.at > 4000 || this.levelAt((now - this.released.t0) / 1000) < 0.005)) {
      this.released = null;
      this.caption();
      this.dirty = true;
    }
    const moving = this.top() || this.pollTimer || (this.revealT0 != null && now - this.revealT0 < 1500);
    if (!this.dirty && !moving) return;
    this.dirty = false;
    this.drawWells(now);
  }

  drawWells(now) {
    const st = this.stages;
    const top = this.top();
    const halo = top ? rgbOf(top.note % 12) : null;
    const tint = halo ? rgba(halo, 0.9) : draw.INK2;
    const tNote = top ? (now - top.t0) / 1000 : null;
    const live = this.liveTaps();
    const rendered = st && { data: null, from: st.win.from, spc: st.win.spc, cycles: 3 };
    const src = (name) => (live && live[name]) || (rendered && { ...rendered, data: st[name] });

    { // Oscillator
      const [c, w, h] = draw.fit(this.wells.osc);
      draw.axis(c, w, h);
      const s = src("osc");
      if (s) draw.stroke(c, draw.wavePts(w, h, s.data, { from: s.from, len: Math.round(s.cycles * s.spc), gain: 0.36 }),
        { halo, reveal: this.reveal(0, now), w, h });
    }
    { // Filter: normalized against the oscillator, so a closing filter reads as quieter too
      const [c, w, h] = draw.fit(this.wells.filter);
      draw.axis(c, w, h);
      const s = src("filter"), o = src("osc");
      if (s) {
        const floor = 0.35 * (o ? peakOf(o.data, o.from, o.from + o.cycles * o.spc) : 1);
        draw.stroke(c, draw.wavePts(w, h, s.data, { from: s.from, len: Math.round(s.cycles * s.spc), gain: 0.36, floor }),
          { halo, reveal: this.reveal(1, now), w, h });
      }
    }
    { // Amplifier: the note's level over time, engraved
      const [c, w, h] = draw.fit(this.wells.amp);
      draw.axis(c, w, h);
      if (st) {
        draw.hatchShape(c, w, h, this.tops(w).amp, { color: tint, reveal: this.reveal(2, now) });
        draw.keyUpMark(c, w, h, GATE / AMP_T);
      }
      if (tNote != null && tNote < AMP_T) draw.playhead(c, w, h, tNote / AMP_T);
    }
    { // Effects: the dry note, and the delay and reverb around it
      const [c, w, h] = draw.fit(this.wells.fx);
      draw.axis(c, w, h);
      if (st) {
        const t = this.tops(w);
        draw.hatchShape(c, w, h, t.wet, { step: 4, alpha: 0.45, color: tint, edge: draw.INK2, reveal: this.reveal(3, now) });
        draw.hatchShape(c, w, h, t.dry, { step: 2, alpha: 0.6, color: tint, edge: draw.INK, reveal: this.reveal(3, now) });
      }
      if (tNote != null && tNote < FX_T) draw.playhead(c, w, h, tNote / FX_T);
    }
    { // Output: the plume
      const [c, w, h] = draw.fit(this.wells.out);
      draw.axis(c, w, h, { cross: true });
      const r = this.reveal(4, now);
      const level = top ? 0.3 + 0.7 * Math.sqrt(this.levelAt(tNote)) : 1;
      const twin = (live && live.out) || (st && st.plume);
      // one period plus one sample: the fold tiles the cycle, so the extra sample is the first one
      // again and the loop closes (a square's edge left a visible gap otherwise)
      const plume = (s, lv) => draw.plumePts(w, h, s.data, s.spc, { from: s.from, cycles: 1 + 1 / s.spc, level: lv });
      if (this.hardware()) {
        const real = this.ctx.status.demo ? st && st.fake : this.real;
        if (real) draw.stroke(c, plume(real, level), { color: draw.BRONZE, width: 1.7, glow: 10, halo, reveal: r, w, h });
        if (twin) draw.stroke(c, plume(twin, level), { color: draw.INK, width: 1.2, dash: [1.5, 4], reveal: r, w, h });
      } else if (twin) {
        draw.stroke(c, plume(twin, level), { color: draw.INK, width: 1.7, glow: 12, halo, reveal: r, w, h });
      }
    }
  }

  drawADSR() {
    if (!this.adsr || !this.alive) return;
    const [c, w, h] = draw.fit(this.adsr);
    if (w < 20) return;
    const P = this.ctx.params;
    const A = Math.sqrt(ATTACK(P.get(73))), D = Math.sqrt(DECAY(P.get(75))), R = Math.sqrt(RELEASE(P.get(72)));
    const S = P.get(30) / 127, sus = 0.9, total = A + D + sus + R, pad = 8, usable = w - pad * 2;
    const X = (u) => pad + (u / total) * usable, Y = (lv) => h - 10 - lv * (h - 22);
    const pts = [[X(0), Y(0)], [X(A), Y(1)], [X(A + D), Y(S)], [X(A + D + sus), Y(S)], [X(total), Y(0)]];
    c.strokeStyle = draw.INK4; c.lineWidth = 1;
    c.beginPath(); c.moveTo(0, Y(0) + 0.5); c.lineTo(w, Y(0) + 0.5); c.stroke();
    const curve = () => {
      c.lineTo(pts[1][0], pts[1][1]);
      for (let i = 1; i <= 24; i++) { const u = i / 24; c.lineTo(pts[1][0] + u * (pts[2][0] - pts[1][0]), Y(S + (1 - S) * Math.exp(-u * 4))); }
      c.lineTo(pts[3][0], pts[3][1]);
      for (let i = 1; i <= 24; i++) { const u = i / 24; c.lineTo(pts[3][0] + u * (pts[4][0] - pts[3][0]), Y(S * Math.exp(-u * 4) * (1 - u * 0.02))); }
    };
    c.save();
    c.beginPath(); c.moveTo(pts[0][0], pts[0][1]); curve(); c.lineTo(pts[4][0], Y(0)); c.closePath(); c.clip();
    c.strokeStyle = draw.INK2; c.globalAlpha = 0.45; c.lineWidth = 1; c.beginPath();
    for (let x = 0; x < w; x += 3) { c.moveTo(x + 0.5, 0); c.lineTo(x + 0.5, h); }
    c.stroke(); c.restore();
    c.strokeStyle = draw.INK; c.lineWidth = 1.4; c.lineJoin = "round";
    c.beginPath(); c.moveTo(pts[0][0], pts[0][1]); curve(); c.stroke();
    for (const [x, y] of pts.slice(1, 4)) {
      c.beginPath(); c.arc(x, y, 3.4, 0, Math.PI * 2);
      c.fillStyle = draw.FIELD; c.fill(); c.strokeStyle = draw.INK; c.lineWidth = 1.25; c.stroke();
    }
    draw.keyUpMark(c, w, h, (pts[3][0] - 10) / (w - 20));
  }

  // ── one screen: the whole plate in the window, at 100% zoom ─────────────────
  /** A wide window shows every control and the keys at once (docs/design/ROUND2.md §4). Above the
   *  stacking width the plate has one design size (app.css: 1470 px wide, laid out to fit a laptop
   *  screen at 100%). A window narrower or shorter than that scales the whole plate down evenly, like
   *  a plugin window, never below FIT_MIN and never up; nothing reflows. A narrow window stacks the
   *  plate instead, and that scrolls. */
  fit() {
    if (!this.alive) return;
    const page = this.el, box = this.box;
    page.style.transform = "";
    page.style.marginLeft = "";
    box.style.height = "";
    this.scale = 1;
    if (window.innerWidth <= STACKED) return;
    const room = box.clientWidth || document.documentElement.clientWidth;
    const natural = page.offsetHeight, width = page.offsetWidth;
    const top = box.getBoundingClientRect().top + window.scrollY;
    const s = Math.max(FIT_MIN, Math.min(1, room / width, (window.innerHeight - top) / natural));
    page.style.marginLeft = `${Math.max(0, (room - width * s) / 2).toFixed(1)}px`;
    if (s > 0.9995) return;
    this.scale = s;
    page.style.transform = `scale(${s.toFixed(4)})`;
    box.style.height = `${(natural * s).toFixed(1)}px`;   // the page flows (and scrolls) by the scaled size
  }

  fitSoon() {
    if (this.fitRaf) return;
    this.fitRaf = requestAnimationFrame(() => { this.fitRaf = 0; this.fit(); this.drawOverlay(); });
  }

  // ── the signal line and the leaders ───────────────────────────────────────
  drawOverlay() {
    if (!this.alive || getComputedStyle(this.overlay).display === "none") return;
    const box = this.el.getBoundingClientRect();
    const k = this.el.offsetWidth ? box.width / this.el.offsetWidth : 1;   // the fit's scale: the overlay draws unscaled
    const rel = (node) => {
      const r = node.getBoundingClientRect();
      return { l: (r.left - box.left) / k, t: (r.top - box.top) / k, r: (r.right - box.left) / k, b: (r.bottom - box.top) / k, w: r.width / k, h: r.height / k };
    };
    const wells = STAGES.map((s) => rel(this.wells[s]));
    const y = (wells[0].t + wells[0].h / 2).toFixed(1);
    let line = "", heads = "";
    for (let i = 0; i < wells.length - 1; i++) {
      const x0 = wells[i].r, x1 = wells[i + 1].l, xm = x1 - 7;
      if (x1 - x0 < 12) continue;
      line += `M${x0.toFixed(1)} ${y} L${x1.toFixed(1)} ${y} `;
      heads += `M${(xm - 5).toFixed(1)} ${y - 5} L${(xm + 1).toFixed(1)} ${y} L${(xm - 5).toFixed(1)} ${+y + 5} `;
    }
    this.overlay.querySelector(".sig").setAttribute("d", line);
    this.overlay.querySelector(".heads").setAttribute("d", heads);
    let leads = "";
    for (const { from, to, weight } of linkWeights(this.layout.links, this.ctx.params)) {
      const a = rel(this.blocks[from]), target = this.controls.get(to);
      if (!target) continue;
      const b = rel(target.el);
      if (!b.w) continue;                                  // folded away
      const tx = b.l + b.w / 2, ty = b.b + 6;
      const sx = Math.max(a.l + 14, Math.min(a.r - 14, tx)), sy = a.t - 8, my = (sy + ty) / 2;
      const d = Math.abs(sx - tx) < 1 ? `M${sx} ${sy} L${tx} ${ty}` : `M${sx} ${sy} C${sx} ${my} ${tx} ${my} ${tx} ${ty}`;
      const op = (0.22 + 0.6 * weight).toFixed(2), sw = (1 + 1.1 * weight).toFixed(2);
      leads += `<path class="lead" d="${d}" stroke-opacity="${op}" stroke-width="${sw}"/>`
        + `<circle class="lead-end" cx="${tx}" cy="${ty}" r="2.2" fill-opacity="${op}"/>`;
    }
    this.overlay.querySelector(".leads").innerHTML = leads;
  }

  // ── the page-load exposure, and the review demos ───────────────────────────
  develop() {
    const root = document.documentElement;
    // Only on page load: index.html starts the page pale ("developing") when it opens on this view.
    const fresh = !developed && !this.still && root.classList.contains("developing");
    developed = true;
    if (!fresh) { root.classList.remove("developing"); return; }
    this.revealT0 = performance.now();
    requestAnimationFrame(() => root.classList.remove("developing"));
    const sig = this.overlay.querySelector(".sig");
    if (sig && getComputedStyle(this.overlay).display !== "none" && sig.getTotalLength) {
      const L = sig.getTotalLength();
      if (L > 0) sig.animate([{ strokeDasharray: `${L} ${L}`, strokeDashoffset: L }, { strokeDasharray: `${L} ${L}`, strokeDashoffset: 0 }],
        { duration: 1000, easing: "cubic-bezier(.3,.6,.2,1)" });
    }
  }

  demo() {
    const which = this.flags.get("demo");
    if (which === "play") {
      // hold A2 without sound: the halos, the lit key and the caption show what a held note does
      this.ctx._noteSeen(IDLE_NOTE, true, 100);
      const d = this.down.find((x) => x.note === IDLE_NOTE);
      if (d) d.t0 -= 2600;
    }
    if (which === "connected") this.demoTwist(74);
  }

  /** The hardware's hand on a knob: it turns, leaves the bronze trail, and stays resting there. */
  demoTwist(cc) {
    const c = this.controls.get(cc);
    if (!c || !c.knob) return;
    const from = this.ctx.params.get(cc), to = from > 100 ? from - 26 : from + 26;
    const rest = () => { if (this.alive) c.knob.set(this.ctx.params.get(cc), { source: "midi" }); };
    const hold = () => { rest(); this.demoTimer = setInterval(rest, 400); };
    if (this.still) { this.ctx._receive(cc, to, "midi"); hold(); return; }
    const t0 = performance.now() + 650, ms = 1500;
    const step = () => {
      if (!this.alive) return;
      const u = Math.max(0, Math.min(1, (performance.now() - t0) / ms));
      const e = u < 0.5 ? 2 * u * u : 1 - (-2 * u + 2) ** 2 / 2;
      this.ctx._receive(cc, Math.round(from + (to - from) * e), "midi");
      if (u < 1) requestAnimationFrame(step); else hold();
    };
    requestAnimationFrame(step);
  }

  destroy() {
    this.alive = false;
    cancelAnimationFrame(this.raf);
    cancelAnimationFrame(this.fitRaf);
    window.removeEventListener("resize", this.onWindow);
    clearTimeout(this.renderTimer);
    clearInterval(this.demoTimer);
    this.stopPolling();
    this.resize.disconnect();
    this.offs.forEach((off) => off());
    this.ctx.keys?.qwerty(false);
    this.board?.destroy();
    this.box.remove();
  }
}
