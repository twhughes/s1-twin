// core/controls.js — a spec from core/layout.js becomes a kit control (design/knob.js, design/seg.js)
// bound to ctx. Shared by the plate and the drawers, so every parameter behaves the same everywhere:
// a hand on the control calls ctx.set(…, {source: "ui"}); a change from anywhere else moves it
// (a "midi" change draws the bronze trail); in twin mode the controls the twin does not model dim.

import { knob } from "../design/knob.js";
import { seg, GLYPHS } from "../design/seg.js";
import { formatValue } from "./layout.js";

export function buildControl(spec, ctx) {
  const value = ctx.params.get(spec.cc) ?? spec.value;
  const onInput = (v) => ctx.set(spec.cc, v, { source: "ui" });
  if (spec.kind === "seg") {
    const options = spec.options.map((o) => ({ value: o.value, label: o.label, ...(o.glyph ? { glyph: GLYPHS[o.glyph] } : {}) }));
    const s = seg({ label: spec.label, options, value, onInput });
    return { spec, el: s.el, set: (v) => s.set(v), get: s.get, modeled: (ok) => s.modeled(ok) };
  }
  const format = formatValue(spec, spec.min) === null ? null : (v) => formatValue(spec, v);
  const k = knob({
    label: spec.label, min: spec.min, max: spec.max, value, def: spec.def,
    bipolar: spec.bipolar, size: spec.size, format, onInput,
  });
  return { spec, el: k.el, set: (v, opts) => k.set(v, opts), get: k.get, modeled: (ok) => k.modeled(ok), knob: k };
}

/** Keep controls (Map cc -> control) in step with ctx. Returns off(). */
export function bindControls(ctx, controls) {
  // Dim only what does nothing without the S-1. audible() covers the twin's model plus the browser's
  // voice layer and effects (they work, so they stay bright); modeled() is stricter (what the
  // matcher can fit) and is only the fallback for a twin without audible().
  const audible = (cc) => {
    try {
      const t = ctx.twin;
      return (typeof t.audible === "function" ? t.audible(cc) : t.modeled(cc)) !== false;
    } catch { return true; }
  };
  const dim = () => {
    const twinMode = ctx.soundSource === "twin";
    for (const [cc, c] of controls) c.modeled(!twinMode || audible(cc));
  };
  const offs = [
    ctx.on("param", ({ cc, value, source }) => controls.get(cc)?.set(value, { source })),
    ctx.on("status", dim),
  ];
  dim();
  return () => offs.forEach((off) => off());
}
