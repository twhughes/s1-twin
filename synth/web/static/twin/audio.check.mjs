// audio.check.mjs — node checks for audio.js's pure parts (exit 0 = pass).
//   node synth/web/static/twin/audio.check.mjs
// createTwin() itself needs a browser (Web Audio); this checks the control table the
// Synth view dims by: modeled(cc) = twin.py models it, audible(cc) = the browser twin
// sounds different when it moves. It prints the table for every S-1 CC.

import { readFileSync } from 'node:fs';

import { FX_CCS, STAGES, VOICE_CCS, supportOf } from './audio.js';

const curves = JSON.parse(readFileSync(new URL('./curves.json', import.meta.url), 'utf8'));
const device = JSON.parse(readFileSync(new URL('../../../data/s1.json', import.meta.url), 'utf8'));
let failures = 0;
const check = (name, ok, detail = '') => {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${name}${detail ? ` — ${detail}` : ''}`);
  if (!ok) failures++;
};

const rows = device.params.map((p) => ({ cc: p.cc, name: `${p.section} ${p.name}`, why: supportOf(p.cc, curves) }));
rows.sort((a, b) => a.cc - b.cc);
console.log('  CC  control                     modeled  audible  why');
for (const r of rows) {
  const yes = (b) => (b ? 'yes' : '-');
  console.log(`  ${String(r.cc).padStart(3)} ${r.name.padEnd(28)} ${yes(r.why === 'model').padEnd(8)} ${yes(r.why !== null).padEnd(8)} ${r.why ?? ''}`);
}

const modeled = rows.filter((r) => r.why === 'model').map((r) => r.cc);
const audible = rows.filter((r) => r.why !== null).map((r) => r.cc);
const kS = [...curves.k_params.map((p) => p[1]), ...curves.s_params.map((p) => p[1])].sort((a, b) => a - b);
check('modeled = the 18 k + 3 s CCs twin.py renders', JSON.stringify(modeled) === JSON.stringify(kS), `${modeled.length}`);
check('Range, glide, polyphony, chord voices, damper and every effect are audible',
  [14, 5, 31, 65, 64, 80, 81, 82, 83, 85, 86, 87, 89, 90, 91, 92, 93].every((cc) => audible.includes(cc)));
check('Draw / Chop (102-104, 107) are not audible without the S-1', [102, 103, 104, 107].every((cc) => !audible.includes(cc)));
check('every voice / effect CC exists on the S-1', [...VOICE_CCS, ...FX_CCS].every((cc) => rows.some((r) => r.cc === cc)));
check('effects are audible but not modeled', FX_CCS.every((cc) => supportOf(cc, curves) === 'extra'));
check('five stage taps, in signal order', STAGES.join(' ') === 'osc filter amp fx out');
console.log(`  ${modeled.length} modeled, ${audible.length} audible, ${rows.length - audible.length} silent without the S-1`);

if (failures) {
  console.log(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log('\nall checks passed');
