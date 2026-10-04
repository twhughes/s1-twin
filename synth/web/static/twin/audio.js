// audio.js — the browser twin: createTwin() (docs/design/BUILD.md §2.3).
//
// It runs synth/match/twin.py's model (dsp.js) in an AudioWorklet (worklet.js), with
// the S-1's voice modes around it and the effects (fx.js) after it, and exposes one
// AnalyserNode per stage so the page can draw each window:
//
//   const twin = await createTwin();          // curves: /api/twin/curves, else bundled
//   twin.set(74, 90); twin.noteOn(48); ...     // CC space 0..127, like the S-1
//   twin.taps.filter.getFloatTimeDomainData(buf)
//   const st = await twin.renderStages({note: 48});   // deterministic stage buffers
//                                                      // ({exact: true}: Twin.render itself)
//   twin.setVoices(8);                         // 8 notes at once (the S-1 has 4)
//
// Honesty: the default curves are UNCALIBRATED stand-ins (twin.calibrated === false)
// until the hardware session fits them. modeled(cc) is true only for the CCs twin.py
// models (what the matcher can fit); audible(cc) is true for every control that changes
// what the browser twin sounds like (the model, plus voice modes, Range, damper and the
// effects). The Synth view dims only controls where audible(cc) is false. The voice
// count is the same kind of browser extra: 4 like the S-1 unless the page asks for more.

import { Engine, S1_VOICES, VOICE_COUNTS, mergeCurves, modulators, renderNote } from './dsp.js';
import { FX_DEFAULTS, Fx, master } from './fx.js';

export { S1_VOICES, VOICE_COUNTS };
export const STAGES = ['osc', 'filter', 'amp', 'fx', 'out'];
/**
 * Controls the browser plays around the model: portamento time / mode / switch (5, 31,
 * 65), Range (14), damper (64), polyphony (80), chord voice switches and key shifts
 * (81-83, 85-87).
 */
export const VOICE_CCS = [5, 14, 31, 64, 65, 80, 81, 82, 83, 85, 86, 87];
/** Browser-only effects after the twin: reverb time/level, delay time/level, chorus. */
export const FX_CCS = Object.keys(FX_DEFAULTS).map(Number);

/**
 * Why a control does (or does not) change the browser twin's sound: 'model' (twin.py
 * models it: the matcher can fit it), 'voice' (the voice layer around the model),
 * 'extra' (browser-only effects), or null (it does nothing without the S-1).
 */
export function supportOf(cc, curves) {
  const k = Number(cc);
  if (curves.k_params.some((p) => p[1] === k) || curves.s_params.some((p) => p[1] === k)) return 'model';
  if (VOICE_CCS.includes(k)) return 'voice';
  if (FX_CCS.includes(k)) return 'extra';
  return null;
}

const BUNDLED_CURVES = new URL('./curves.json', import.meta.url);
const WORKLET_URL = new URL('./worklet.js', import.meta.url);
const SERVER_CURVES = '/api/twin/curves';

async function fetchJson(url) {
  const r = await fetch(url, { cache: 'no-store' });
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return r.json();
}

/**
 * The curves the twin runs. `curves` may be an object (merged over the bundled file),
 * a URL, 'bundled', or undefined (the server's /api/twin/curves when one answers —
 * calibrated there once the hardware session has run — else the bundled defaults).
 */
export async function loadCurves(curves) {
  const bundled = await fetchJson(BUNDLED_CURVES);
  if (curves === 'bundled') return bundled;
  if (curves && typeof curves === 'object') return mergeCurves(bundled, curves);
  const url = typeof curves === 'string' ? curves : SERVER_CURVES;
  const onServer = typeof location !== 'undefined' && /^https?:$/.test(location.protocol);
  if (typeof curves !== 'string' && !onServer) return bundled;
  try {
    return mergeCurves(bundled, await fetchJson(url));
  } catch (err) {
    if (typeof curves === 'string') throw err;
    return bundled; // no server (static page): the bundled defaults
  }
}

function rms(buf) {
  let s = 0;
  for (let i = 0; i < buf.length; i++) s += buf[i] * buf[i];
  return Math.sqrt(s / buf.length);
}

/** A voice count the twin offers (VOICE_COUNTS), or a RangeError that says which ones are. */
function checkVoices(n) {
  const v = Number(n);
  if (VOICE_COUNTS.includes(v)) return v;
  const offered = `${VOICE_COUNTS.slice(0, -1).join(', ')} or ${VOICE_COUNTS.at(-1)}`;
  throw new RangeError(`twin: voices must be ${offered}, not ${n}`);
}

/**
 * Build the twin. Options: {curves, context, latencyHint, voices, destination}.
 * `voices`: how many notes sound at once, 4 (the S-1's own, the default), 8 or 16.
 * `destination`: the node the 'out' stage plays into (default: the context's speakers).
 * Call twin.resume() from a user gesture before the first note (browsers start audio
 * suspended).
 */
export async function createTwin({ curves, context, latencyHint = 'interactive', voices = S1_VOICES, destination } = {}) {
  let nVoices = checkVoices(voices);
  const cv = await loadCurves(curves);
  const Ctx = globalThis.AudioContext || globalThis.webkitAudioContext;
  const ac = context || new Ctx({ latencyHint });
  await ac.audioWorklet.addModule(WORKLET_URL);

  const params = {};
  for (const [cc, r] of Object.entries(cv.cc_ranges || {})) params[cc] = r[2];
  let node = new AudioWorkletNode(ac, 's1-twin', {
    numberOfInputs: 0,
    numberOfOutputs: STAGES.length,
    outputChannelCount: STAGES.map(() => 1),
    processorOptions: { curves: cv, params, voices: nVoices },
  });
  const taps = {};
  STAGES.forEach((name, k) => {
    const an = ac.createAnalyser();
    an.fftSize = 2048;
    an.smoothingTimeConstant = 0;
    node.connect(an, k);
    taps[name] = an;
  });
  node.connect(destination || ac.destination, STAGES.indexOf('out'));
  const levelBuf = new Float32Array(taps.out.fftSize);
  let current = cv;

  const post = (msg) => node.port.postMessage(msg);

  const twin = {
    context: ac,
    node,
    taps,
    get curves() {
      return current;
    },
    /** False until the hardware calibration has run: the UI must say "uncalibrated". */
    get calibrated() {
      return !!current.calibrated;
    },

    /** Set one CC (0..127, the S-1's own numbers). */
    set(cc, v) {
      params[cc] = Number(v);
      post({ type: 'cc', cc: Number(cc), value: Number(v) });
    },
    /** Set many CCs at once: {cc: value}. */
    setAll(map) {
      const values = {};
      const entries = map instanceof Map ? [...map.entries()] : Object.entries(map || {});
      for (const [cc, v] of entries) {
        params[cc] = Number(v);
        values[cc] = Number(v);
      }
      post({ type: 'ccs', values });
    },
    noteOn(note, vel = 100) {
      post({ type: 'on', note: Number(note), vel: Number(vel) });
    },
    noteOff(note) {
      post({ type: 'off', note: Number(note) });
    },
    allOff() {
      post({ type: 'alloff' });
    },
    /** Silence now: no release tails, effects cleared. */
    panic() {
      post({ type: 'panic' });
    },
    /** Start audio; call from a user gesture. */
    resume() {
      return ac.resume();
    },
    /** Swap in new (e.g. freshly calibrated) curves; every knob keeps its value. */
    setCurves(next) {
      current = mergeCurves(current, next);
      post({ type: 'curves', curves: current });
    },
    /** How many notes sound at once: 4 (the S-1's), 8 or 16. */
    get voices() {
      return nVoices;
    },
    /**
     * Play n notes at once (4, 8 or 16; anything else is a RangeError). The voices are
     * rebuilt, like new curves: notes sounding now stop, and every knob keeps its value.
     */
    setVoices(n) {
      const v = checkVoices(n);
      if (v === nVoices) return;
      nVoices = v;
      post({ type: 'voices', voices: v });
    },

    /** True if twin.py models this CC: what the matcher can fit. */
    modeled(cc) {
      return supportOf(cc, current) === 'model';
    },
    /**
     * True if this CC changes what you hear from the browser twin: the model's params,
     * the voice layer (VOICE_CCS) and the effects (FX_CCS). False only for controls that
     * do nothing without the S-1 (Draw / Chop, noise mode, bend depths, ...). The Synth
     * view dims only these.
     */
    audible(cc) {
      return supportOf(cc, current) !== null;
    },
    /** 'model' | 'voice' | 'extra' | null — why a control does (or does not) sound. */
    support(cc) {
      return supportOf(cc, current);
    },

    /** Output RMS, 0..1 (the limiter keeps it below 1). */
    level() {
      taps.out.getFloatTimeDomainData(levelBuf);
      return Math.min(1, rms(levelBuf));
    },

    /**
     * Deterministic stage buffers for one key press, computed with dsp.js directly (not
     * the audio graph): the same Engine the worklet runs, fed in 128-sample blocks, with
     * the key released exactly at `gate` seconds (default: the twin's own, 60% of
     * `seconds`), at the twin's own rate. It is what the page plays, voice modes
     * included. `exact: true` instead sums dsp.renderNote — Twin.render line for line
     * (slower: FFT filters). fx / out run the worklet's effects code.
     * Returns {sr, osc, filter, amp, fx, out, env, lfo, cutoff} as Float32Arrays;
     * env / lfo / cutoff are twin.py's modulators for the key (cutoff in Hz, before the
     * ladder's frame averaging).
     */
    async renderStages({ note = 60, seconds, gate, exact = false } = {}) {
      const sr = current.sr;
      const secs = seconds ?? current.seconds;
      const gateSec = Math.min(secs, Math.max(0, gate ?? secs * current.gate_fraction));
      const gateFraction = secs > 0 ? gateSec / secs : 0;
      const n = Math.max(1, Math.round(secs * sr));
      const eng = new Engine({ sr, curves: current, maxVoices: nVoices });
      eng.setAll(params);
      const specs = eng.voicesFor(Number(note));
      const sum = { osc: new Float64Array(n), filter: new Float64Array(n), amp: new Float64Array(n) };
      if (exact) {
        for (const sp of specs) {
          const r = renderNote({ cc: params, curves: current, note: sp.note, sr, seconds: secs,
            gateFraction, detuneCents: sp.detune, stages: true });
          for (const k of ['osc', 'filter', 'amp']) {
            const src = r[k], dst = sum[k];
            for (let i = 0; i < n; i++) dst[i] += sp.gain * src[i];
          }
        }
      } else {
        eng.noteOn(Number(note), 100);
        eng.noteOff(Number(note), gateSec);
        const B = 128;
        const o = new Float64Array(B), f = new Float64Array(B), a = new Float64Array(B);
        for (let pos = 0; pos < n; pos += B) {
          const m = Math.min(B, n - pos);
          eng.process(o, f, a, m);
          sum.osc.set(o.subarray(0, m), pos);
          sum.filter.set(f.subarray(0, m), pos);
          sum.amp.set(a.subarray(0, m), pos);
        }
      }
      const mods = modulators({ cc: params, curves: current, note: specs[0].note, sr, seconds: secs, gateFraction });
      const fx = new Fx(sr);
      for (const [cc, v] of Object.entries(params)) fx.set(cc, v);
      const fxOut = new Float64Array(n);
      fx.process(sum.amp, fxOut, n);
      const out = new Float64Array(n);
      master(fxOut, out, n);
      const f32 = (x) => Float32Array.from(x);
      return {
        sr,
        osc: f32(sum.osc),
        filter: f32(sum.filter),
        amp: f32(sum.amp),
        fx: f32(fxOut),
        out: f32(out),
        env: f32(mods.env),
        lfo: f32(mods.lfo),
        cutoff: f32(mods.cutoff),
      };
    },

    /** Tear down the graph (and the context, if createTwin made it). The processor ends too. */
    async close() {
      post({ type: 'stop' });
      node.disconnect();
      for (const an of Object.values(taps)) an.disconnect();
      node = null;
      if (!context) await ac.close();
    },
  };
  return twin;
}
