// design/draw.js — how the signal is drawn in a well: waveforms, engraving hatch, the plume.
// Pure canvas helpers (no app state). The math helpers run in node too (kit.check.mjs).
// Colors mirror tokens.css (kit.check.mjs asserts they agree): ink by default, a held note's
// pitch color as a halo, bronze for the hardware.

export const FIELD = "#0E2A52", DEEP = "#081C3A";
export const INK = "#EEF3FA", INK2 = "#A3B9DA", BRONZE = "#E6A94F";
export const INK3 = "rgba(238,243,250,0.26)", INK4 = "rgba(238,243,250,0.11)";
const PAD = 10;

/** Size a canvas's backing store to its CSS box and DPR; returns [ctx, w, h] with a cleared, scaled ctx. */
export function fit(canvas) {
  const dpr = (typeof window !== "undefined" && window.devicePixelRatio) || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  const W = Math.round(w * dpr), H = Math.round(h * dpr);
  if (canvas.width !== W || canvas.height !== H) { canvas.width = W; canvas.height = H; }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return [ctx, w, h];
}

/** The zero line: the signal line of the plate continues through every well as this axis. */
export function axis(ctx, w, h, { cross = false } = {}) {
  ctx.save();
  ctx.strokeStyle = INK4; ctx.lineWidth = 1; ctx.setLineDash([]);
  ctx.beginPath(); ctx.moveTo(0, h / 2 + .5); ctx.lineTo(w, h / 2 + .5);
  if (cross) { ctx.moveTo(w / 2 + .5, PAD); ctx.lineTo(w / 2 + .5, h - PAD); }
  ctx.stroke(); ctx.restore();
}

const rgbaStr = (rgb, a) => `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${a})`;

/** Stroke a polyline. halo = [r,g,b] pitch color drawn under the line; glow = live-trace blur px. */
export function stroke(ctx, pts, { color = INK, width = 1.5, glow = 0, dash = null, halo = null, reveal = 1, w = 0, h = 0 } = {}) {
  if (!pts.length) return;
  ctx.save();
  if (w && h && reveal < 1) { ctx.beginPath(); ctx.rect(0, 0, w * reveal, h); ctx.clip(); }
  ctx.lineJoin = "round"; ctx.lineCap = "round";
  const path = () => {
    ctx.beginPath();
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
  };
  if (halo) {
    ctx.strokeStyle = rgbaStr(halo, .5); ctx.lineWidth = width + 5;
    ctx.shadowColor = rgbaStr(halo, .9); ctx.shadowBlur = 16;
    path(); ctx.stroke(); ctx.shadowBlur = 0;
  }
  ctx.strokeStyle = color; ctx.lineWidth = width;
  if (dash) ctx.setLineDash(dash);
  if (glow) { ctx.shadowColor = color === BRONZE ? "rgba(230,169,79,.6)" : "rgba(238,243,250,.55)"; ctx.shadowBlur = glow; }
  path(); ctx.stroke();
  ctx.restore();
}

/** Points for a waveform across the well: `len` samples from `from`, peak-normalized to gain*h. */
export function wavePts(w, h, data, { from = 0, len = data.length - from, gain = .36, floor = 1e-6 } = {}) {
  let peak = floor;
  for (let i = from; i < from + len; i++) peak = Math.max(peak, Math.abs(data[i]));
  const sc = (h * gain) / peak, pts = new Array(len);
  for (let i = 0; i < len; i++) pts[i] = [PAD + (i / Math.max(1, len - 1)) * (w - PAD * 2), h / 2 - data[from + i] * sc];
  return pts;
}

/** Period in samples of a periodic signal by normalized autocorrelation, or 0 if none is clear. */
export function period(data, minLag = 8, maxLag = Math.floor(data.length / 2)) {
  const n = data.length;
  let mean = 0; for (let i = 0; i < n; i++) mean += data[i]; mean /= n;
  let e0 = 0; for (let i = 0; i < n; i++) e0 += (data[i] - mean) ** 2;
  if (e0 < 1e-12) return 0;
  let best = 0, bestR = 0.3;              // demand a clear repeat before calling it periodic
  let prev = 1, rising = false;
  for (let lag = minLag; lag <= maxLag; lag++) {
    let s = 0;
    for (let i = 0; i + lag < n; i++) s += (data[i] - mean) * (data[i + lag] - mean);
    const r = s / e0 * n / (n - lag);
    if (r > prev) rising = true;
    if (rising && r < prev && prev > bestR) { best = lag - 1; bestR = prev; break; }  // first strong peak
    prev = r;
  }
  return best;
}

/** The plume: the output as a phase portrait. x = the signal, y = how fast it moves; one loop = one cycle.
 *  Draws `cycles` periods starting at `from`; level (0..1) scales the loop so loudness still reads.
 *  Both axes normalize to their 98th percentile and clip the rest, so one sharp edge (a raw saw's reset,
 *  a click) cannot flatten the whole loop. */
export function plumePts(w, h, data, spc, { from = 2, cycles = 2, level = 1 } = {}) {
  const n = Math.max(2, Math.min(Math.round(spc * cycles), data.length - from - 2));
  const xs = new Float32Array(n), ys = new Float32Array(n);
  const k = spc / (4 * Math.PI);
  for (let i = 0; i < n; i++) {
    const j = from + i;
    xs[i] = data[j];
    // 5-point derivative: steadier than a 2-point difference on real (noisy) input
    ys[i] = (-data[j + 2] + 8 * data[j + 1] - 8 * data[j - 1] + data[j - 2]) / 12 * k;
  }
  const px = pct98(xs), py = pct98(ys);
  const rx = (w / 2 - 18) * level, ry = (h / 2 - 16) * level, pts = new Array(n);
  const c = (v) => Math.max(-1.12, Math.min(1.12, v));
  for (let i = 0; i < n; i++) pts[i] = [w / 2 + c(xs[i] / px) * rx / 1.12, h / 2 - c(ys[i] / py) * ry / 1.12];
  return pts;
}
function pct98(a) {
  const s = Float32Array.from(a, Math.abs).sort();
  return Math.max(1e-6, s[Math.min(s.length - 1, Math.floor(s.length * .98))]);
}

/** Peak level per column (0..1 after normalizing to the loudest column): a note's shape over time. */
export function peaksPerColumn(samples, cols) {
  const out = new Float32Array(cols), step = samples.length / cols;
  let top = 1e-9;
  for (let c = 0; c < cols; c++) {
    let m = 0;
    for (let i = Math.floor(c * step), e = Math.floor((c + 1) * step); i < e; i++) m = Math.max(m, Math.abs(samples[i]));
    out[c] = m; top = Math.max(top, m);
  }
  for (let c = 0; c < cols; c++) out[c] /= top;
  return out;
}

/** A level-over-time shape mirrored about the axis and filled with engraving hatch. */
export function hatchShape(ctx, w, h, top, { step = 2, color = INK2, alpha = .55, edge = INK, reveal = 1 } = {}) {
  const cy = h / 2, amp = h * .42, n = top.length;
  const X = (i) => PAD + (i / Math.max(1, n - 1)) * (w - PAD * 2);
  ctx.save();
  if (reveal < 1) { ctx.beginPath(); ctx.rect(0, 0, w * reveal, h); ctx.clip(); }
  ctx.strokeStyle = color; ctx.globalAlpha = alpha; ctx.lineWidth = 1;
  ctx.beginPath();
  for (let i = 0; i < n; i += step) {
    const a = top[i] * amp;
    if (a < .6) continue;
    ctx.moveTo(X(i) + .5, cy - a); ctx.lineTo(X(i) + .5, cy + a);
  }
  ctx.stroke();
  ctx.globalAlpha = 1; ctx.strokeStyle = edge; ctx.lineWidth = 1.25; ctx.lineJoin = "round";
  for (const sgn of [-1, 1]) {
    ctx.beginPath();
    for (let i = 0; i < n; i++) { const x = X(i), y = cy + sgn * top[i] * amp; i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }
    ctx.stroke();
  }
  ctx.restore();
}

/** A vertical playhead at fraction `frac` of the well's time span. */
export function playhead(ctx, w, h, frac, color = INK) {
  const x = PAD + frac * (w - PAD * 2);
  ctx.save(); ctx.strokeStyle = color; ctx.lineWidth = 1.25;
  ctx.beginPath(); ctx.moveTo(x + .5, 6); ctx.lineTo(x + .5, h - 6); ctx.stroke(); ctx.restore();
}

/** A dotted "key up" marker with a small label. */
export function keyUpMark(ctx, w, h, frac, label = "key up") {
  const x = PAD + frac * (w - PAD * 2);
  ctx.save();
  ctx.strokeStyle = INK3; ctx.setLineDash([2, 3]);
  ctx.beginPath(); ctx.moveTo(x + .5, 8); ctx.lineTo(x + .5, h - 8); ctx.stroke();
  ctx.setLineDash([]); ctx.fillStyle = INK2; ctx.font = "400 10.5px 'Libre Franklin', sans-serif";
  ctx.fillText(label, x + 5, 17);
  ctx.restore();
}
