// node synth/web/static/core/layout.check.mjs — exit 0 = every parameter has exactly one home.
// BUILD.md §3 rule 5: the layout comes from the schema, every schema parameter lands somewhere
// (unknown ones in the Settings drawer), and every leader line points at a real plate control.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

import { plateLayout, linkWeights, allParams, sentence, specFor, formatValue, PLATE, LINKS, WORDS } from "./layout.js";

const here = dirname(fileURLToPath(import.meta.url));
const schema = JSON.parse(readFileSync(join(here, "schema.json"), "utf8"));
let checks = 0;
const ok = (cond, msg) => { assert.ok(cond, msg); checks++; };

/** Every control in a resolved block tree, with where it sits. */
function controls(blocks, out = []) {
  const walkRows = (rows, id) => {
    for (const r of rows) {
      if (r.type === "more") { walkRows(r.rows, id); continue; }
      for (const it of r.items) {
        if (it.type === "pair") it.items.forEach((c) => out.push({ ...c, block: id }));
        else out.push({ ...it, block: id });
      }
    }
  };
  for (const b of blocks) { walkRows(b.rows, b.id); controls(b.blocks, out); }
  return out;
}

// ── the live schema: every CC parameter exactly once ──────────────────────
const L = plateLayout(schema);
const ccs = allParams(schema).map((p) => p.cc);
const placed = [...controls(L.plate), ...controls(L.settings)].map((c) => c.cc);
ok(ccs.length === 54, `the S-1 has 54 CC parameters (schema has ${ccs.length})`);
ok(new Set(ccs).size === ccs.length, "the schema lists no CC twice");
ok(placed.length === ccs.length, `placed ${placed.length} controls for ${ccs.length} parameters`);
ok(new Set(placed).size === placed.length, "no parameter is placed twice");
for (const cc of ccs) ok(placed.includes(cc), `CC ${cc} has a home`);
ok(L.duplicates.length === 0, `the tables name no CC twice (${L.duplicates})`);
ok(!L.settings.some((g) => g.id === "more"), "with today's schema nothing falls back: every CC has a chosen home");
ok(L.specs.size === ccs.length, "one spec per parameter");

// the plate carries the instrument; the drawer carries the rest (DIRECTION rule 3)
const onPlate = new Set(controls(L.plate).map((c) => c.cc));
for (const cc of [74, 71, 24, 25, 73, 75, 30, 72, 19, 20, 21, 23, 3, 12, 80]) ok(onPlate.has(cc), `CC ${cc} is on the plate`);
for (const cc of [76, 17, 18, 27, 85, 86, 87, 1, 10, 11, 64]) ok(!onPlate.has(cc), `CC ${cc} lives in Settings`);
ok(L.placement.get(76) === "settings:tuning", "Fine tune lives in Settings (comp)");

// the comp's words
const label = (cc) => L.specs.get(cc).label;
ok(label(74) === "Cutoff" && label(13) === "Vibrato" && label(24) === "Env amount" && label(25) === "LFO amount", "filter and vibrato words");
ok(label(16) === "Width set by" && label(22) === "Sub octave" && label(28) === "Volume shape" && label(29) === "Triggered by", "switch words");
ok(label(79) === "Speed" && label(106) === "Tempo sync" && label(105) === "Restart on key" && label(5) === "Glide time", "LFO and glide words");
const vals = (cc) => L.specs.get(cc).options.map((o) => o.value).join();
ok(vals(16) === "1,2,0" && L.specs.get(16).options.map((o) => o.label).join() === "Knob,LFO,Envelope", "Width set by = Knob, LFO, Envelope = 1, 2, 0");
ok(vals(22) === "2,1,0" && L.specs.get(22).options[2].label === "−2 asym", "Sub octave = −1, −2, −2 asym = 2, 1, 0");
ok(vals(29) === "2,1,0", "Triggered by = Gate + trig, Gate, LFO = 2, 1, 0");
ok(L.specs.get(12).options.every((o) => o.glyph), "every LFO wave has a glyph");
ok(L.specs.get(74).size === 88 && L.specs.get(71).size === 50, "Cutoff is the hero knob");

// every switch reaches every value the schema names, and only legal values
for (const spec of L.specs.values()) {
  const p = allParams(schema).find((x) => x.cc === spec.cc);
  if (spec.kind === "seg") {
    const v = spec.options.map((o) => o.value);
    ok(new Set(v).size === v.length, `CC ${spec.cc} options are distinct`);
    ok(v.every((x) => x >= spec.min && x <= spec.max), `CC ${spec.cc} options are in range`);
    for (const k of Object.keys(p.labels || {})) ok(v.includes(Number(k)), `CC ${spec.cc} reaches value ${k}`);
  } else {
    ok(spec.kind === "knob" && p.type === "continuous", `CC ${spec.cc} is a knob because it is continuous`);
  }
  // sentence case, no all-caps labels (BUILD §3 rule 4)
  ok(/^[A-Z0-9−]/.test(spec.label) && !/^[A-Z ]{4,}$/.test(spec.label), `CC ${spec.cc} label "${spec.label}" is sentence case`);
  ok(spec.bipolar === (p.format === "signed64"), `CC ${spec.cc} is bipolar exactly when the schema says signed64`);
}
ok(formatValue(L.specs.get(102), 3) === "×1.0" && formatValue(L.specs.get(102), 127) === "×32.0", "multiply reads ×1.0 to ×32.0");
ok(formatValue(L.specs.get(103), 127) === "200" && formatValue(L.specs.get(74), 50) === null, "overtone reads 0 to 200; plain knobs read raw");

// ── leaders point at real plate controls, from real modulator blocks ──────
const mods = new Set(L.plate.filter((b) => b.kind === "mod").map((b) => b.id));
ok(mods.has("lfo") && mods.has("env"), "the LFO and Envelope blocks exist");
ok(L.links.length === LINKS.length, "every leader survives with today's schema");
for (const l of L.links) {
  ok(mods.has(l.from), `leader source ${l.from} is a modulator block`);
  ok(onPlate.has(l.to), `leader target CC ${l.to} is a control on the plate`);
  const target = controls(L.plate).find((c) => c.cc === l.to);
  ok(!mods.has(target.block), `leader target CC ${l.to} sits above the modulators, not inside one`);
}
const P = new Map(allParams(schema).map((p) => [p.cc, p.default]));
P.set(16, 0); P.set(24, 127); P.set(25, 0); P.set(13, 64); P.set(28, 1);
let w = linkWeights(L.links, P);
ok(w.some((x) => x.from === "env" && x.to === 15), "Width set by Envelope draws a leader from the envelope");
ok(!w.some((x) => x.to === 25), "a zero amount draws no leader");
ok(w.find((x) => x.to === 24).weight === 1 && Math.abs(w.find((x) => x.to === 13).weight - 64 / 127) < 1e-9, "weight = amount");
ok(w.some((x) => x.to === 28), "Volume shape = Envelope draws a leader from the envelope");
P.set(16, 1); P.set(28, 0);
w = linkWeights(L.links, P);
ok(!w.some((x) => x.to === 15 || x.to === 28), "Width set by Knob and a Gate volume draw no leader");
ok(w.every((x) => x.weight > 0 && x.weight <= 1), "weights stay in (0, 1]");

// ── fallback: a parameter nobody planned for still lands, once, in Settings ─
const extra = structuredClone(schema);
extra.sections.find((s) => s.name === "OSC").params.push({
  cc: 119, name: "Mystery Knob", section: "OSC", access: "panel", type: "continuous",
  min: 0, max: 127, default: 10, value: 10, labels: {}, format: "", menu_item: "", description: "",
});
extra.midi.push({ cc: 118, name: "Pedal Mode", section: "MIDI", access: "external", type: "discrete",
  min: 0, max: 2, default: 0, value: 0, labels: { 0: "Off", 1: "Hold", 2: "Latch" }, format: "", menu_item: "", description: "" });
const L2 = plateLayout(extra);
const more = L2.settings.find((g) => g.id === "more");
ok(more && controls([more]).map((c) => c.cc).join() === "119,118", "unknown parameters fall back to Settings, in schema order");
ok(L2.placement.get(119) === "settings:more" && L2.specs.get(119).label === "Mystery knob", "fallback words come from the schema, sentence-cased");
ok(L2.specs.get(118).kind === "seg" && L2.specs.get(118).options.map((o) => o.label).join() === "Off,Hold,Latch", "a fallback switch keeps its options");
ok(controls([...L2.plate, ...L2.settings]).length === ccs.length + 2, "still exactly one control per parameter");

// ── a parameter the schema drops disappears cleanly, with its leader ──────
const fewer = structuredClone(schema);
for (const s of fewer.sections) s.params = s.params.filter((p) => p.cc !== 13 && p.cc !== 19);
const L3 = plateLayout(fewer);
ok(!L3.specs.has(13) && !L3.links.some((l) => l.to === 13), "no control and no leader for a missing parameter");
ok(controls(L3.plate).length === controls(L.plate).length - 2, "the rest of the plate stands");

// ── words ─────────────────────────────────────────────────────────────────
ok(sentence("Voice 2 Key Shift") === "Voice 2 key shift" && sentence("PWM Source") === "PWM source", "sentence case keeps acronyms");
ok(specFor({ cc: 1, name: "Mod Wheel", type: "continuous", min: 0, max: 127, default: 0 }).label === "Mod wheel", "WORDS win over the schema name");
ok(Object.keys(WORDS).every((cc) => ccs.includes(Number(cc))), "WORDS name only real CCs");
ok(PLATE.map((b) => b.id).join() === "osc,filter,amp,fx,out,lfo,env,keys", "the plate's blocks in signal order");

console.log(`layout: ${checks} checks passed`);
