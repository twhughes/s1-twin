// core/layout.js — where every parameter lives (docs/design/BUILD.md §3 rule 5).
// Pure: schema in, placement out; no DOM. `node core/layout.check.mjs` proves that every
// schema parameter lands exactly once, that unknown parameters fall back to the Settings
// drawer, and that every leader line points at a real control on the plate.
//
// The plate follows docs/design/menura-comp.html: the chain (Oscillator, Filter, Amplifier,
// Effects, Output) is row 1, the modulators (LFO, Envelope) and Keys are row 2. The words
// follow DIRECTION.md rule 5: say what a control does, in sentence case.

/** Words and controls per CC. `options` lists [value, label, glyph?] in display order. */
export const WORDS = {
  // LFO
  3: { label: "Rate" },
  12: { label: "Wave", options: [[0, "Saw", "saw"], [1, "Inverse saw", "isaw"], [2, "Triangle", "tri"],
    [3, "Square", "sq"], [4, "Random", "rnd"], [5, "Noise", "noise"]] },
  79: { label: "Speed", options: [[0, "Normal"], [1, "Fast"]] },
  106: { label: "Tempo sync", options: [[0, "Off"], [1, "On"]] },
  105: { label: "Restart on key", options: [[0, "Off"], [1, "On"]] },
  // Oscillator
  14: { label: "Range", options: [[0, "64′"], [1, "32′"], [2, "16′"], [3, "8′"], [4, "4′"], [5, "2′"]] },
  19: { label: "Square" },
  20: { label: "Saw" },
  21: { label: "Sub" },
  23: { label: "Noise" },
  22: { label: "Sub octave", options: [[2, "−1"], [1, "−2"], [0, "−2 asym"]] },
  78: { label: "Noise color", options: [[0, "Pink"], [1, "White"]] },
  15: { label: "Pulse width" },
  16: { label: "Width set by", options: [[1, "Knob"], [2, "LFO"], [0, "Envelope"]] },
  13: { label: "Vibrato" },
  107: { label: "Draw", options: [[0, "Off"], [1, "Step"], [2, "Slope"]] },
  102: { label: "Multiply" },
  103: { label: "Overtone" },
  104: { label: "Comb" },
  76: { label: "Fine tune" },
  // Filter
  74: { label: "Cutoff", size: 88 },
  71: { label: "Resonance" },
  26: { label: "Key follow" },
  24: { label: "Env amount" },
  25: { label: "LFO amount" },
  // Amplifier and envelope
  28: { label: "Volume shape", options: [[0, "Gate"], [1, "Envelope"]] },
  73: { label: "Attack" },
  75: { label: "Decay" },
  30: { label: "Sustain" },
  72: { label: "Release" },
  29: { label: "Triggered by", options: [[2, "Gate + trig"], [1, "Gate"], [0, "LFO"]] },
  // Effects
  92: { label: "Delay" },
  90: { label: "Delay time" },
  91: { label: "Reverb" },
  89: { label: "Reverb time" },
  93: { label: "Chorus", options: [[0, "Off"], [1, "1"], [2, "2"], [3, "3"], [4, "4"]] },
  // Voices
  80: { label: "Polyphony", options: [[0, "Mono"], [1, "Unison"], [2, "Poly"], [3, "Chord"]] },
  31: { label: "Glide", options: [[0, "Off"], [1, "Auto"], [2, "On"]] },
  5: { label: "Glide time" },
  77: { label: "Transpose" },
  // Settings drawer
  17: { label: "Mod wheel to LFO" },
  18: { label: "Bend to pitch" },
  27: { label: "Bend to cutoff" },
  81: { label: "Voice 2", options: [[0, "Off"], [127, "On"]] },
  82: { label: "Voice 3", options: [[0, "Off"], [127, "On"]] },
  83: { label: "Voice 4", options: [[0, "Off"], [127, "On"]] },
  85: { label: "Voice 2 offset" },
  86: { label: "Voice 3 offset" },
  87: { label: "Voice 4 offset" },
  1: { label: "Mod wheel" },
  10: { label: "Pan" },
  11: { label: "Expression" },
  64: { label: "Damper", options: [[0, "Off"], [127, "On"]] },
  65: { label: "Glide switch", options: [[0, "Off"], [127, "On"]] },
};

// Row grammar: [cc, …] is a row; {wide, items} a row with more air; {pair: [cc, cc]} two controls
// stacked; {cc, gap: true} a control set apart; {more: title, rows} a fold-out (the plate draws its
// summary at the end of the row above it). A block's `head` holds controls that sit on its title line.
//
// The plate fits one laptop screen at 100% (docs/design/ROUND2.md §4): each stage's switches share one
// line, so no column is taller than about four rows.
export const PLATE = [
  { id: "osc", kind: "stage", title: "Oscillator", well: "Oscillator waveform", rows: [
    { wide: true, items: [14, 22, 78] },
    [19, 20, 21, 23],
    { more: "Draw and chop", rows: [[107, { cc: 102, gap: true }, 103, 104]] },
    [15, 16, 13],   // last row: the LFO/Env leaders to Pulse width and Vibrato rise without crossing text
  ] },
  { id: "filter", kind: "stage", title: "Filter", well: "Waveform after the filter", rows: [
    [74, 71],
    [26, 24, 25],
  ] },
  { id: "amp", kind: "stage", title: "Amplifier", well: "Volume over one note", rows: [[28]] },
  { id: "fx", kind: "stage", title: "Effects", well: "The note with delay and reverb", rows: [
    [92, 90],
    [91, 89],
    [93],
  ] },
  { id: "out", kind: "stage", title: "Output", well: "The output, drawn as a plume", rows: [], blocks: [
    { id: "voices", title: "Voices", rows: [{ wide: true, items: [80, 31] }, [5, 77]] },
  ] },
  { id: "lfo", kind: "mod", title: "LFO", rows: [
    { wide: true, items: [3, 12] },
    { wide: true, items: [79, 106, 105] },
  ] },
  { id: "env", kind: "mod", title: "Envelope", adsr: true, head: [29], rows: [[73, 75, 30, 72]] },
  { id: "keys", kind: "keys", title: "Keys", rows: [] },
];

export const SETTINGS = [
  { id: "tuning", title: "Tuning", rows: [[76]] },
  { id: "wheels", title: "Wheels and bend",
    note: "On the S-1 these live in the settings menu: hold Shift and press pad 15.",
    rows: [[17, 18, 27]] },
  { id: "chord", title: "Chord voices",
    note: "In Chord mode, each extra voice plays at its own offset from the key you press.",
    rows: [[{ pair: [81, 85] }, { pair: [82, 86] }, { pair: [83, 87] }]] },
  { id: "midi", title: "MIDI",
    note: "What a MIDI controller sends. The S-1 has no knob for these.",
    rows: [[1, 10, 11], { wide: true, items: [64, 65] }] },
];

/** Where a parameter goes when no table names it: never lost, always in the Settings drawer. */
export const FALLBACK = {
  id: "more", title: "More settings",
  note: "Settings this version of the app has no special place for yet.",
};

/** Modulation leaders: a dotted line from a modulator block up to the control it drives.
 *  `amount` = the CC whose value sets the weight; `when` = [cc, value] that must hold; `weight` = fixed. */
export const LINKS = [
  { from: "lfo", to: 13, amount: 13 },              // vibrato: the LFO moves the pitch
  { from: "lfo", to: 25, amount: 25 },              // the LFO moves the cutoff
  { from: "env", to: 24, amount: 24 },              // the envelope moves the cutoff
  { from: "env", to: 15, when: [16, 0], weight: 0.55 },   // the envelope sets the pulse width
  { from: "lfo", to: 15, when: [16, 2], weight: 0.55 },   // the LFO sets the pulse width
  { from: "env", to: 28, when: [28, 1], weight: 0.5 },    // the envelope shapes the volume
];

/** Every CC parameter in a schema payload, in schema order (faceplate, menu, MIDI). */
export function allParams(schema) {
  const out = [];
  for (const s of schema.sections || []) out.push(...s.params);
  out.push(...(schema.menu || []), ...(schema.midi || []));
  return out.filter((p) => Number.isInteger(p.cc));
}

/** "Voice 2 Key Shift" -> "Voice 2 key shift"; acronyms (LFO, PWM) stay as they are. */
export function sentence(name) {
  return String(name).split(/\s+/).filter(Boolean)
    .map((w, i) => (i === 0 || /^[A-Z0-9-]{2,}$/.test(w) ? w : w.toLowerCase()))
    .join(" ");
}

function optionsFor(p, words) {
  const labels = p.labels || {};
  const fromSchema = Object.keys(labels).map(Number).sort((a, b) => a - b)
    .map((v) => ({ value: v, label: sentence(labels[v]) }));
  if (!words || !words.options) {
    if (fromSchema.length) return fromSchema;
    const out = [];
    for (let v = p.min; v <= p.max; v++) out.push({ value: v, label: String(v) });
    return out;
  }
  const opts = words.options.map(([value, label, glyph]) => ({ value, label, ...(glyph ? { glyph } : {}) }));
  // a value the schema knows but the words forgot is still reachable (appended, schema words)
  for (const o of fromSchema) if (!opts.some((x) => x.value === o.value)) opts.push(o);
  return opts;
}

/** One resolved control: everything a view needs to build it (no DOM here). */
export function specFor(p, words = WORDS[p.cc]) {
  const seg = p.type === "discrete" || p.type === "switch" || Boolean(words && words.options);
  return {
    cc: p.cc,
    name: p.name,
    label: (words && words.label) || sentence(p.name),
    kind: seg ? "seg" : "knob",
    min: p.min,
    max: p.max,
    def: p.default,
    value: p.value ?? p.default,
    bipolar: p.format === "signed64",
    size: (words && words.size) || 50,
    format: p.format || "",
    options: seg ? optionsFor(p, words) : null,
    description: p.description || "",
    menu: p.menu_item || "",
    access: p.access,
  };
}

/** A knob's readout. signed64 is drawn by the knob itself (bipolar); the rest are schema hints. */
export function formatValue(spec, v) {
  if (spec.format === "mult") return "×" + (Math.round((1 + (v - 3) * 31 / 124) * 2) / 2).toFixed(1);
  if (spec.format === "chop200") return String(Math.min(200, Math.round(v * 255 / 127)));
  return null;
}

/** Schema -> {specs, plate, settings, prm, placement, links, duplicates}. Every CC param is in
 *  `specs` and placed exactly once: on the plate, in a Settings group, or in the fallback group. */
export function plateLayout(schema) {
  const params = allParams(schema);
  const byCc = new Map(params.map((p) => [p.cc, p]));
  const specs = new Map();
  const placement = new Map();
  const duplicates = [];

  const control = (item, home) => {
    const cc = typeof item === "number" ? item : item.cc;
    const p = byCc.get(cc);
    if (!p) return null;                        // the schema no longer has it: skip quietly
    if (placement.has(cc)) { duplicates.push(cc); return null; }
    const spec = specFor(p);
    specs.set(cc, spec);
    placement.set(cc, home);
    return { type: "control", cc, spec, gap: Boolean(item && item.gap) };
  };
  const row = (r, home) => {
    if (r && r.more) {
      const rows = r.rows.map((x) => row(x, home)).filter(Boolean);
      return rows.length ? { type: "more", title: r.more, rows } : null;
    }
    const items = (Array.isArray(r) ? r : r.items).map((it) => {
      if (it && it.pair) {
        const pair = it.pair.map((x) => control(x, home)).filter(Boolean);
        return pair.length ? { type: "pair", items: pair } : null;
      }
      return control(it, home);
    }).filter(Boolean);
    return items.length ? { type: "row", wide: Boolean(r.wide), items } : null;
  };
  const block = (b, where) => ({
    id: b.id, kind: b.kind || "block", title: b.title, well: b.well || null, note: b.note || null,
    adsr: Boolean(b.adsr),
    head: b.head ? row(b.head, `${where}:${b.id}`) : null,
    rows: (b.rows || []).map((r) => row(r, `${where}:${b.id}`)).filter(Boolean),
    blocks: (b.blocks || []).map((x) => block(x, where)),
  });

  const plate = PLATE.map((b) => block(b, "plate"));
  const settings = SETTINGS.map((g) => block(g, "settings"));
  const rest = params.filter((p) => !placement.has(p.cc));
  if (rest.length) settings.push(block({ ...FALLBACK, rows: [rest.map((p) => p.cc)] }, "settings"));
  const drawn = new Set([...placement].filter(([, home]) => home.startsWith("plate:")).map(([cc]) => cc));
  const links = LINKS.filter((l) => drawn.has(l.to) && (l.amount == null || byCc.has(l.amount))
    && (l.when == null || byCc.has(l.when[0])));
  return { specs, plate, settings, prm: schema.prm || [], placement, links, duplicates };
}

/** The leaders to draw for the current values: [{from, to, weight}] with 0 < weight <= 1. */
export function linkWeights(links, params) {
  const get = (cc) => (params instanceof Map ? params.get(cc) : params[cc]);
  const out = [];
  for (const l of links) {
    if (l.when && get(l.when[0]) !== l.when[1]) continue;
    const weight = l.amount != null ? (get(l.amount) || 0) / 127 : l.weight;
    if (weight > 0) out.push({ from: l.from, to: l.to, weight: Math.min(1, weight) });
  }
  return out;
}
