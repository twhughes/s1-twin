// worklet.js — the AudioWorkletProcessor that plays the twin in real time.
//
// It wraps dsp.js's Engine (4 Voices like the S-1, or 8 or 16 when the page asks:
// the twin's equations per voice, plus the S-1's voice modes) and fx.js (the
// browser-only effects). Five mono outputs, one per stage, so the page can hang an
// AnalyserNode on each (audio.js wires them):
//
//   0 osc     the oscillator + noise mix, summed over voices   (before the filter)
//   1 filter  after the 4-pole ladder
//   2 amp     after the VCA — the twin's own output
//   3 fx      after chorus / delay / reverb (browser extras)
//   4 out     after the output gain and limiter — what goes to the speakers
//
// processorOptions: {curves, params, voices = 4}. Messages on port:
// {type:'cc', cc, value} | {type:'ccs', values:{cc: value}} | {type:'on', note, vel} |
// {type:'off', note} | {type:'alloff'} | {type:'panic'} | {type:'curves', curves} |
// {type:'voices', voices} (4, 8 or 16) | {type:'stop'} (the page is done: the
// processor ends, so the browser can retire it).

import { Engine, voiceCount } from './dsp.js';
import { Fx, master } from './fx.js';

const STAGES = 5;

class TwinProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const o = (options && options.processorOptions) || {};
    this.params = { ...(o.params || {}) };
    this.curves = o.curves;
    this.voices = voiceCount(o.voices);
    this._build();
    this.fx = new Fx(sampleRate);
    for (const [cc, v] of Object.entries(this.params)) this.fx.set(cc, v);
    this.alive = true;
    this.bufs = [];
    this._alloc(128);
    this.port.onmessage = (ev) => this.onMessage(ev.data || {});
  }

  /** (Re)build the voices on the current curves and voice count; every knob keeps its value. */
  _build() {
    this.engine = new Engine({ sr: sampleRate, curves: this.curves, maxVoices: this.voices });
    this.engine.setAll(this.params);
  }

  _alloc(n) {
    this.n = n;
    this.bufs = [];
    for (let k = 0; k < STAGES; k++) this.bufs.push(new Float64Array(n));
  }

  onMessage(m) {
    const eng = this.engine;
    switch (m.type) {
      case 'cc':
        this.params[m.cc] = m.value;
        eng.set(m.cc, m.value);
        this.fx.set(m.cc, m.value);
        break;
      case 'ccs':
        Object.assign(this.params, m.values);
        eng.setAll(m.values);
        for (const [cc, v] of Object.entries(m.values || {})) this.fx.set(cc, v);
        break;
      case 'on':
        eng.noteOn(m.note, m.vel ?? 100);
        break;
      case 'off':
        eng.noteOff(m.note);
        break;
      case 'alloff':
        eng.allOff();
        break;
      case 'panic':
        eng.panic();
        this.fx.reset();
        break;
      case 'curves':
        // calibrated curves arrived: rebuild the voices on them, keep every knob
        this.curves = m.curves;
        this._build();
        break;
      case 'voices': {
        // another voice count: rebuild the voices like new curves (notes sounding now stop), keep every knob
        const n = voiceCount(m.voices, this.voices);
        if (n !== this.voices) {
          this.voices = n;
          this._build();
        }
        break;
      }
      case 'stop':
        this.alive = false;
        break;
      default:
        break;
    }
  }

  process(inputs, outputs) {
    if (!this.alive) return false;
    const n = outputs[0] && outputs[0][0] ? outputs[0][0].length : 128;
    if (n !== this.n) this._alloc(n);
    const [osc, filt, amp, fx, out] = this.bufs;
    this.engine.process(osc, filt, amp, n);
    this.fx.process(amp, fx, n);
    master(fx, out, n);
    for (let k = 0; k < STAGES; k++) {
      const ch = outputs[k];
      if (!ch) continue;
      const src = this.bufs[k];
      for (let c = 0; c < ch.length; c++) {
        const dst = ch[c];
        for (let i = 0; i < n; i++) dst[i] = src[i];
      }
    }
    return true;
  }
}

registerProcessor('s1-twin', TwinProcessor);
