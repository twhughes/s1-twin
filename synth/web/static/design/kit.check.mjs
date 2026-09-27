// node synth/web/static/design/kit.check.mjs — exit 0 = the design kit's pure parts hold.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

import { rgbOf, noteName, lum, PC_NAMES } from "./colors.js";
import * as draw from "./draw.js";
import { arcPath, knob } from "./knob.js";
import { seg, GLYPHS } from "./seg.js";
import { NAME, TAGLINE, DISCLAIMER } from "./brand.js";

const here = dirname(fileURLToPath(import.meta.url));
let checks = 0;
const ok = (cond, msg) => { assert.ok(cond, msg); checks++; };

// colors: the palette authority's values, sharps brightened by 0.35 toward white
ok(rgbOf(9).join() === "235,92,92", "A is coral");
ok(rgbOf(4).join() === "57,255,20", "E is neon green");
ok(rgbOf(10).join() === [235, 92, 92].map((c) => Math.round(c + (255 - c) * 0.35)).join(), "A♯ brightens A");
ok(rgbOf(-3).join() === rgbOf(9).join(), "negative pitch classes wrap");
ok(noteName(60) === "C4" && noteName(45) === "A2", "MIDI 60 is C4");
ok(PC_NAMES.length === 12 && lum([255, 255, 255]) > .99, "names and luminance");

// draw.js mirrors tokens.css exactly (one look, two places, asserted)
const css = readFileSync(join(here, "tokens.css"), "utf8");
const token = (name) => css.match(new RegExp(`--${name}:\\s*([^;]+);`))[1].trim().replace(/\s+/g, "");
ok(token("field").toUpperCase() === draw.FIELD, "field matches tokens.css");
ok(token("deep").toUpperCase() === draw.DEEP, "deep matches tokens.css");
ok(token("ink").toUpperCase() === draw.INK, "ink matches tokens.css");
ok(token("ink-2").toUpperCase() === draw.INK2, "ink-2 matches tokens.css");
ok(token("bronze").toUpperCase() === draw.BRONZE, "bronze matches tokens.css");
const rgbaNum = (s) => s.replace(/rgba\(|\)/g, "").split(",").map(Number).join();
ok(rgbaNum(token("ink-3")) === rgbaNum(draw.INK3), "ink-3 matches tokens.css");
ok(rgbaNum(token("ink-4")) === rgbaNum(draw.INK4), "ink-4 matches tokens.css");

// period(): finds the period of a clean tone and refuses silence
const spc = 97, sine = Float32Array.from({ length: 2048 }, (_, i) => Math.sin(2 * Math.PI * i / spc));
ok(Math.abs(draw.period(sine) - spc) <= 1, `period of a sine (${draw.period(sine)} vs ${spc})`);
const saw = Float32Array.from({ length: 2048 }, (_, i) => 2 * ((i / 150) % 1) - 1);
ok(Math.abs(draw.period(saw) - 150) <= 1, "period of a saw");
ok(draw.period(new Float32Array(2048)) === 0, "silence has no period");

// wavePts / plumePts stay inside the well
const W = 320, H = 200, inside = (pts) => pts.every(([x, y]) => x >= 0 && x <= W && y >= 0 && y <= H && Number.isFinite(x + y));
ok(inside(draw.wavePts(W, H, saw, { len: 450 })), "wave points inside the well");
ok(inside(draw.plumePts(W, H, sine, spc, { cycles: 2 })), "plume inside the well");
ok(inside(draw.plumePts(W, H, saw, 150, { cycles: 1, level: .4 })), "quiet plume inside the well");
const p = draw.peaksPerColumn(Float32Array.from({ length: 1000 }, (_, i) => Math.sin(i) * (1 - i / 1000)), 50);
ok(p.length === 50 && Math.max(...p) === 1 && p[49] < p[0], "peaks per column normalize and decay");

// knob/seg/brand import cleanly without a DOM (they touch document only when called)
ok(typeof knob === "function" && typeof seg === "function" && Object.keys(GLYPHS).length === 6, "components export");
ok(arcPath(36, -135, -135) === "" && arcPath(36, -135, 0).startsWith("M"), "arc path");
ok(NAME.length > 0 && TAGLINE.includes("S-1") && DISCLAIMER.includes("not affiliated".replace("not", "Not")), "brand strings");

console.log(`kit: ${checks} checks passed`);
