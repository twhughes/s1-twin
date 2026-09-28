// node synth/web/static/core/fit.check.mjs — exit 0 = the one-screen scale rule holds (core/fit.js).
import assert from "node:assert/strict";
import { fitScale, DESIGN_W, STACKED, FIT_MIN } from "./fit.js";

let checks = 0;
const ok = (cond, msg) => { assert.ok(cond, msg); checks++; };
const near = (a, b) => Math.abs(a - b) < 1e-9;

ok(DESIGN_W === 1470 && STACKED === 1180 && FIT_MIN === 0.7, "the design size, the stacking width and the floor");
ok(fitScale(1470, 670, 1470, 700) === 1, "a page that fits stays at 100%");
ok(fitScale(1470, 670, 1920, 1080) === 1, "never scaled up on a big screen");
ok(near(fitScale(1470, 700, 1470, 630), 0.9), "a short window scales by height");
ok(near(fitScale(1470, 600, 1280, 900), 1280 / 1470), "a narrow window scales by width");
ok(near(fitScale(1470, 700, 1300, 560), 0.8), "the tighter side wins");
ok(fitScale(1470, 700, 800, 300) === FIT_MIN, "never below the floor (the page scrolls instead)");
ok(fitScale(0, 700, 1470, 700) === 1 && fitScale(1470, 0, 1470, 700) === 1, "an unmeasured page is left alone");

console.log(`fit: ${checks} checks passed`);
