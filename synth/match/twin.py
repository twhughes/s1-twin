"""twin.py — the differentiable digital twin of the Roland S-1.

This is FABLE's centerpiece: a *software* model of the S-1's signal path that is
cheap to evaluate and **differentiable in its continuous parameters**, so a match
becomes "gradient-descend the model to the target" instead of blind-searching the
hardware. It conforms to the chassis-spec C9 tier-1 backend protocol
(:class:`~synth.backend_protocol.DifferentiableBackend`) — the twin and the real
S-1 speak the same 54-CC dialect; the twin additionally exposes ``render``.

Provenance (HQ sharing rule: reimplement, credit, never import across the repo
boundary). The DSP math below is a clean autograd.numpy reimplementation of the
torch kernel the *music* project proved gradcheck-exact:

    * oscillators / ADSR / LFO / 4-pole ladder  <- music/music/dsp/modules.py
    * the S-1-shaped subtractive voice           <- music/music/dsp/voice.py
    * multi-resolution STFT + envelope loss       <- music/music/dsp/loss.py
    * calibratable normalized->physical curves    <- music/music/dsp/mapping.py
    * the k/s (continuous/discrete) param split   <- music/music/dsp/params.py

**Why autograd, not torch.** A spike verified autograd differentiates exactly
through ``autograd.numpy.fft.rfft`` + a magnitude feature + a parameter inside a
frequency-domain filter transfer function. So the filter is an *analytic
frequency-domain transfer function*, never an unrolled per-sample recurrence (a
long recurrence would blow up autograd's tape). Every op on the gradient path uses
``import autograd.numpy as anp``; constants (windows, indices, noise, FFT bin
frequencies) use plain ``numpy`` because they do not depend on ``k``.

**Two honesty rules from FABLE, obeyed here loudly.**

1. *Never trust an unvalidated metric.* The differentiable spectral loss below is a
   perceptually-*motivated* distance. It is **NOT** perceptually validated — that
   needs Tyler's ears in a later ceremony. Nothing here claims otherwise.
2. *The twin must ultimately be calibrated to the REAL S-1.* We have no hardware in
   this session, so :func:`calibrate` fits the free constants against
   **synthetic/self-consistent** targets only, and says so. The
   ``calibrate(real_probes)`` seam is where real hardware probes replace them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Sequence

import autograd.numpy as anp
import numpy as np
from autograd import grad

from . import ANALYSIS_SECONDS, WORKING_SR
from .capture import AudioClip, prepare
from .distance import Weights, closeness, reference_scales
from .distance import loss as plain_loss
from .features import extract

__all__ = [
    "Curve",
    "Mapping",
    "DEFAULT_MAPPING",
    "Twin",
    "TwinMatcher",
    "K_PARAMS",
    "S_PARAMS",
    "spectral_loss",
    "multiscale_stft_loss",
    "envelope_loss",
    "calibrate",
    "CalibrationReport",
    "midi_to_hz",
    "export_curves",
]

MAX_HARMONICS = 64
_TWO_PI = 2.0 * math.pi


def midi_to_hz(note: float) -> float:
    """MIDI note number -> frequency in Hz (A4 = 69 = 440 Hz)."""
    return 440.0 * 2.0 ** ((float(note) - 69.0) / 12.0)


# ─────────────────────────────────────────────────────────────────────────────
# Calibratable normalized->physical curves  (credit: music/music/dsp/mapping.py)
#
# These curves ARE the twin's free calibration constants. The k vector is the
# CC-normalized value in [0,1]; a curve turns it into a physical unit (Hz,
# seconds, octaves, ...). Calibration against real S-1 probes swaps a curve here
# and touches nothing else — see :func:`calibrate`.
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Curve:
    """Monotone, invertible map [0,1] -> [lo, hi] in physical units.

    ``linear``  x = lo + k*(hi-lo)          ``exp``  x = lo*(hi/lo)**k  (lo,hi>0)

    Pure arithmetic in ``autograd.numpy``, so it is differentiable w.r.t. ``k``
    for autograd arrays and works unchanged for plain floats.
    """

    lo: float
    hi: float
    kind: str = "linear"
    unit: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ("linear", "exp"):
            raise ValueError(f"unknown curve kind {self.kind!r}")
        if self.kind == "exp" and (self.lo <= 0.0 or self.hi <= 0.0):
            raise ValueError("exp curves need strictly positive lo/hi")
        if self.hi == self.lo:
            raise ValueError("degenerate curve (lo == hi)")

    def __call__(self, k: Any) -> Any:
        if self.kind == "linear":
            return self.lo + (self.hi - self.lo) * k
        # exp written via anp.exp so the gradient path stays clean for autograd.
        return self.lo * anp.exp(k * math.log(self.hi / self.lo))

    def invert(self, x: Any) -> Any:
        """Physical -> normalized (the calibration direction)."""
        if self.kind == "linear":
            return (x - self.lo) / (self.hi - self.lo)
        return math.log(float(x) / self.lo) / math.log(self.hi / self.lo)


@dataclass(frozen=True)
class Mapping:
    """A named bundle of curves — the twin's calibration unit."""

    curves: dict[str, Curve]
    name: str = "default (uncalibrated — synthetic only)"

    def curve(self, param: str) -> Curve:
        try:
            return self.curves[param]
        except KeyError as exc:  # pragma: no cover - defensive
            raise KeyError(f"no curve for {param!r} in mapping {self.name!r}") from exc

    def __call__(self, param: str, k: Any) -> Any:
        return self.curve(param)(k)

    def calibrated(self, name: str = "calibrated", **curves: Curve) -> "Mapping":
        merged = dict(self.curves)
        for key, cur in curves.items():
            if key not in merged:
                raise KeyError(f"unknown param {key!r}")
            merged[key] = cur
        return replace(self, curves=merged, name=name)


# Default ranges bracket an SH-101/S-1-class voice. Uncalibrated: honest stand-ins
# until real-hardware probes fit them in :func:`calibrate`.
DEFAULT_CURVES: dict[str, Curve] = {
    "saw_lvl": Curve(0.0, 1.0, "linear", "amp"),
    "square_lvl": Curve(0.0, 1.0, "linear", "amp"),
    "sub_lvl": Curve(0.0, 1.0, "linear", "amp"),
    "noise_lvl": Curve(0.0, 0.5, "linear", "amp"),
    # CC15 = 0 is a plain 50% square (SH-101 convention, and s1.json: CC15 is the static width in
    # Manual mode or the PWM depth under LFO/Env, the S-1 default). Higher narrows the pulse.
    # It was 0.05 -> 0.5, which rendered the S-1's init patch as a 5% sliver and would send
    # CC15=127 to the hardware for a matched square. Calibration confirms the far end.
    "pulse_width": Curve(0.5, 0.05, "linear", "duty"),
    "cutoff": Curve(30.0, 12000.0, "exp", "Hz"),
    "resonance": Curve(0.0, 3.8, "linear", "ladder k (4==self-osc)"),
    "env_to_cutoff": Curve(0.0, 6.0, "linear", "octaves"),
    "lfo_to_cutoff": Curve(0.0, 4.0, "linear", "octaves"),
    "key_follow": Curve(0.0, 1.0, "linear", "oct/oct"),
    "attack": Curve(0.001, 2.0, "exp", "s"),
    "decay": Curve(0.005, 4.0, "exp", "s"),
    "sustain": Curve(0.0, 1.0, "linear", "level"),
    "release": Curve(0.005, 4.0, "exp", "s"),
    "lfo_rate": Curve(0.05, 30.0, "exp", "Hz"),
    "lfo_to_pitch": Curve(0.0, 12.0, "linear", "semitones"),
    "lfo_depth": Curve(0.0, 1.0, "linear", "amt"),
    "fine_tune": Curve(-100.0, 100.0, "linear", "cents"),
}

DEFAULT_MAPPING = Mapping(DEFAULT_CURVES)


# ─────────────────────────────────────────────────────────────────────────────
# k / s parameter schema  (credit: music/music/dsp/params.py + voice.py)
#
# Continuous k params ARE the normalized S-1 CC values; discrete s params are the
# S-1's option-index selectors, enumerated (never gradient-descended). CC numbers
# and labels are READ from schema.py, never hardcoded here.
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class KParam:
    name: str
    cc: int


@dataclass(frozen=True)
class SParam:
    name: str
    cc: int
    # ordered option indices this twin enumerates during matching
    choices: tuple[int, ...]


# k params, in vector order. Each maps to one continuous S-1 CC (match-v3 chain).
K_PARAMS: tuple[KParam, ...] = (
    KParam("saw_lvl", 20),
    KParam("square_lvl", 19),
    KParam("sub_lvl", 21),
    KParam("noise_lvl", 23),
    KParam("pulse_width", 15),
    KParam("cutoff", 74),
    KParam("resonance", 71),
    KParam("env_to_cutoff", 24),
    KParam("lfo_to_cutoff", 25),
    KParam("key_follow", 26),
    KParam("attack", 73),
    KParam("decay", 75),
    KParam("sustain", 30),
    KParam("release", 72),
    KParam("lfo_rate", 3),
    KParam("lfo_to_pitch", 13),
    KParam("lfo_depth", 17),
    KParam("fine_tune", 76),
)

# Discrete s params the twin enumerates: sub octave type (CC22), LFO waveform
# (CC12), amp env mode (CC28). Choice tuples are option indices per s1.json.
S_PARAMS: tuple[SParam, ...] = (
    SParam("sub_octave", 22, (0, 1, 2)),        # -2 Asym / -2 / -1 Oct
    SParam("lfo_shape", 12, (2, 3, 0)),          # Triangle / Square / Saw
    SParam("amp_env_mode", 28, (0, 1)),          # Gate / Envelope
)

_K_INDEX = {p.name: i for i, p in enumerate(K_PARAMS)}
K_NAMES = tuple(p.name for p in K_PARAMS)


def _default_s() -> dict[str, int]:
    """The factory-ish default s config the twin renders when none is given."""
    return {"sub_octave": 2, "lfo_shape": 2, "amp_env_mode": 1}


# LFO waveform option index (CC12) -> twin shape name.
_LFO_SHAPE_NAME = {0: "saw", 1: "inv_saw", 2: "tri", 3: "square", 4: "noise", 5: "noise"}
# Sub octave option index (CC22) -> (octave shift, duty cycle). Asym uses a narrow
# duty to read as the S-1's "-2 Oct Asym" pulse; the others are square sub-octaves.
_SUB_OCTAVE = {0: (-2, 0.25), 1: (-2, 0.5), 2: (-1, 0.5)}


# ─────────────────────────────────────────────────────────────────────────────
# Differentiable DSP modules  (credit: music/music/dsp/modules.py)
# All operate in physical units; every op is autograd.numpy.
# ─────────────────────────────────────────────────────────────────────────────
def _softplus(z: Any) -> Any:
    """Numerically stable softplus: max(z,0) + log1p(exp(-|z|)). Differentiable,
    never overflows (a naive log1p(exp(z)) blows up for z >~ 700)."""
    return anp.maximum(z, 0.0) + anp.log1p(anp.exp(-anp.abs(z)))


def _harmonic_count(f0: float, sr: float) -> int:
    """How many harmonics fit below Nyquist. Pitch is *given*, so this integer is a
    constant w.r.t. k and never breaks the gradient (mirrors modules._harmonic_count)."""
    top = f0 * 1.06  # a semitone of headroom for pitch modulation
    if top <= 0.0:
        return 1
    return int(max(1, min(MAX_HARMONICS, math.floor(0.5 * sr / top))))


def _phase(freq: Any, n: int, sr: float, timevarying: bool) -> Any:
    """Instantaneous phase (radians), (n,). ``freq`` may be scalar or per-sample
    (integrated by cumsum — this is how the pitch LFO gets in)."""
    if timevarying:
        return anp.cumsum(_TWO_PI * freq / float(sr))
    t = np.arange(n) / float(sr)
    return _TWO_PI * freq * t


def _osc_saw(phase: Any, H: int) -> Any:
    """Band-limited sawtooth, additive: (2/pi) sum_h (-1)^(h+1) sin(h*phi)/h.

    Harmonics are summed as one ``(H, n)`` op (not a Python loop) — far fewer tape
    nodes for autograd and much faster (credit: modules.osc_saw)."""
    h = np.arange(1, H + 1)
    amp = ((-1.0) ** (h + 1) / h)                              # (H,) constants
    partials = anp.sin(h[:, None] * phase[None, :])            # (H, n)
    return (2.0 / math.pi) * anp.dot(amp, partials)


def _osc_pulse(phase: Any, width: Any, H: int) -> Any:
    """Band-limited pulse, differentiable in duty ``width``: DC-free Fourier series
    sum_h (4/(pi*h)) sin(pi*h*d) cos(h*phi). d=0.5 is a square. Vectorized over H."""
    d = anp.clip(width, 0.001, 0.999)
    h = np.arange(1, H + 1)
    amp = (4.0 / (math.pi * h)) * anp.sin(math.pi * h * d)     # (H,) depends on d
    partials = anp.cos(h[:, None] * phase[None, :])            # (H, n)
    return anp.dot(amp, partials)


def _lfo_wave(rate: Any, shape: str, n: int, sr: float) -> Any:
    """LFO in [-1,1], differentiable in ``rate``. ``shape`` is a discrete s choice."""
    ph = _phase(rate, n, sr, timevarying=False)
    if shape == "tri":  # smooth additive triangle (gradient-friendly)
        acc = anp.zeros_like(ph)
        for j, h in enumerate(range(1, 18, 2)):
            acc = acc + ((-1.0) ** j / (h * h)) * anp.sin(h * ph)
        return (8.0 / (math.pi ** 2)) * acc
    if shape == "square":  # a few odd harmonics — band-limited square
        acc = anp.zeros_like(ph)
        for h in range(1, 18, 2):
            acc = acc + anp.sin(h * ph) / h
        return (4.0 / math.pi) * acc
    if shape in ("saw", "inv_saw"):
        acc = anp.zeros_like(ph)
        for h in range(1, 12):
            acc = acc + ((-1.0) ** (h + 1) / h) * anp.sin(h * ph)
        sign = 1.0 if shape == "saw" else -1.0
        return sign * (2.0 / math.pi) * acc
    # "noise": a fixed slow random control (constant w.r.t. k).
    rng = np.random.default_rng(1234)
    steps = max(2, int(round(float(rate) * n / sr)) + 1)
    raw = rng.uniform(-1.0, 1.0, size=steps)
    idx = np.clip((np.arange(n) * (steps - 1) // max(1, n)).astype(int), 0, steps - 1)
    return anp.array(raw[idx])


def _adsr(A: Any, D: Any, S: Any, R: Any, note_len_s: float, n: int, sr: float) -> Any:
    """Analog-style ADSR, differentiable in all four params (credit: modules.adsr).

    att = 1 - exp(-3*held/A);  dec = S + (1-S)*exp(-relu(held-A)/D);
    rel = exp(-relu(t-T)/R);  env = att*dec*rel. The exponential-saturating attack
    keeps d(env)/dA non-zero everywhere (a linear ramp strands the optimizer)."""
    eps = 1e-4
    A = eps * _softplus(A / eps)  # softplus floor: strictly positive, smooth
    D = eps * _softplus(D / eps)
    R = eps * _softplus(R / eps)
    t = np.arange(n) / float(sr)
    T = float(note_len_s)
    held = anp.clip(t, 0.0, T)
    att = -anp.expm1(-3.0 * held / A)
    dec = S + (1.0 - S) * anp.exp(-anp.clip(held - A, 0.0, None) / D)
    rel = anp.exp(-anp.clip(t - T, 0.0, None) / R)
    return att * dec * rel


def _ladder_response(freqs: np.ndarray, fc: Any, k: Any) -> Any:
    """Analog 4-pole ladder transfer function sampled at ``freqs`` (Hz).

    H(s) = (1+k) / ((1+s)^4 + k), s = j f/fc. The (1+k) numerator normalizes DC
    gain to 1 so resonance changes timbre, not level (credit: ladder_response)."""
    fc = anp.clip(fc, 1e-3, None)
    k = anp.clip(k, 0.0, 3.98)
    r = freqs / fc
    s = 1j * r
    u = s + 1.0
    den = u * u * u * u + k
    return (1.0 + k) / den


def _ladder_static(x: Any, fc: Any, k: Any, sr: float) -> Any:
    """Static-cutoff 4-pole lowpass by spectral multiplication (one rFFT).

    Zero-pad to >= 2n so the product is a linear (not circular) convolution."""
    n = x.shape[-1]
    nfft = 1 << max(1, 2 * n - 1).bit_length()
    freqs = np.fft.rfftfreq(nfft, 1.0 / float(sr))
    H = _ladder_response(freqs, fc, k)
    # Zero-pad MANUALLY: autograd's rfft VJP is wrong when its ``n`` exceeds the
    # input length (implicit padding); an explicit concatenate keeps it exact.
    xp = anp.concatenate([x, anp.zeros(nfft - n)])
    return anp.fft.irfft(anp.fft.rfft(xp, n=nfft) * H, n=nfft)[:n]


def _ladder_tv(x: Any, fc_t: Any, k: Any, sr: float, block: int) -> Any:
    """Time-varying-cutoff 4-pole lowpass: overlap-add of per-frame static ladders.

    Hann window at 50% overlap, FFT size 4*win so each frame keeps its convolution tail
    (at 2*win, resonant ringing longer than one window wrapped onto the frame's start —
    up to 0.48 logmel on modulated low notes; found by the browser-twin parity work,
    2026-09-27). Each frame uses its *mean* cutoff — itself differentiable, so
    gradients reach env_to_cutoff / lfo_to_cutoff through the modulation signal.
    This is the frequency-domain stand-in for a per-sample recurrence, exactly so
    autograd's tape stays shallow (credit: modules.ladder4)."""
    n = x.shape[-1]
    block = int(max(8, block))
    win, hop = 2 * block, block
    nfft = 4 * win
    pad = hop
    n_frames = 1 + -(-(pad + n) // hop)
    total = (n_frames - 1) * hop + win
    xp = anp.concatenate([anp.zeros(pad), x, anp.zeros(total - pad - n)])
    cp = anp.concatenate([anp.full(pad, fc_t[0]), fc_t, anp.full(total - pad - n, fc_t[-1])])

    idx = np.arange(win)[None, :] + hop * np.arange(n_frames)[:, None]
    window = np.hanning(win)
    frames = xp[idx] * window                                  # (n_frames, win)
    fc_frame = anp.mean(cp[idx], axis=-1)                       # (n_frames,)

    freqs = np.fft.rfftfreq(nfft, 1.0 / float(sr))             # (nfft//2+1,)
    H = _ladder_response(freqs[None, :], fc_frame[:, None], k)  # (n_frames, bins)
    # Manual zero-pad (win -> nfft) so autograd's rfft VJP stays exact (see
    # _ladder_static): implicit ``n>len`` padding has a wrong adjoint.
    frames_p = anp.concatenate([frames, anp.zeros((frames.shape[0], nfft - win))], axis=-1)
    y = anp.fft.irfft(anp.fft.rfft(frames_p, n=nfft, axis=-1) * H, n=nfft, axis=-1)

    # Overlap-add, vectorized: frame i spans hop-blocks [i .. i+ncol). Reshape each
    # frame into ``ncol`` hop-length columns and sum the ncol diagonally-shifted
    # stacks — a loop over the overlap factor (4), not over every frame.
    ncol = nfft // hop
    yr = y.reshape(n_frames, ncol, hop)
    n_blocks = n_frames + ncol - 1
    out_blocks = anp.zeros((n_blocks, hop))
    for c in range(ncol):
        top = anp.zeros((c, hop))
        bot = anp.zeros((ncol - 1 - c, hop))
        out_blocks = out_blocks + anp.concatenate([top, yr[:, c, :], bot], axis=0)
    out = out_blocks.reshape(-1)
    return out[pad:pad + n]


# ─────────────────────────────────────────────────────────────────────────────
# The twin — a tier-1 differentiable backend
# ─────────────────────────────────────────────────────────────────────────────
class Twin:
    """A differentiable software S-1. Tier-1 backend + the optional ``render``.

    ``render(k, s, note)`` follows the S-1 chain: band-limited saw/square/sub +
    noise mix -> analytic 4-pole ladder (cutoff/resonance, env- & LFO-modulated) ->
    ADSR VCA, with an LFO that also modulates pitch. ``k`` is the normalized [0,1]
    continuous vector (``K_PARAMS`` order); ``s`` selects discrete choices.
    """

    def __init__(
        self,
        mapping: Mapping = DEFAULT_MAPPING,
        sr: int = WORKING_SR,
        seconds: float = ANALYSIS_SECONDS,
        gate_fraction: float = 0.6,
    ) -> None:
        self.mapping = mapping
        self.sr = int(sr)
        self.seconds = float(seconds)
        self.gate_fraction = float(gate_fraction)
        self.k_dim = len(K_PARAMS)
        self.k_names = K_NAMES
        # tier-1 backend state (structural conformance to InstrumentBackend)
        self._connected = False
        self._port: str | None = None
        self._patch: dict[int, int] = {}
        self._callbacks: list[Callable[[int, int], None]] = []

    # -- k <-> physical -------------------------------------------------------
    def physical(self, k: Any, s: dict[str, int] | None = None) -> dict[str, Any]:
        """(k, s) -> {param: physical value}. The ONLY place units appear."""
        out: dict[str, Any] = {}
        for i, name in enumerate(self.k_names):
            out[name] = self.mapping(name, k[i])
        return out

    # -- the differentiable forward model ------------------------------------
    def render(self, k: Any, s: dict[str, int] | None = None, note: int = 60) -> Any:
        """Render ``note`` under continuous ``k`` / discrete ``s`` to audio (n,).

        Differentiable w.r.t. every element of ``k`` (autograd). ``note`` is a MIDI
        number; velocity and gate length are fixed (pitch/velocity are *given*,
        never optimized). ``s`` defaults to the twin's factory-ish config."""
        s = {**_default_s(), **(s or {})}
        p = self.physical(k, s)
        sr, n = self.sr, max(1, int(round(self.seconds * self.sr)))
        note_len = self.seconds * self.gate_fraction
        cents = p["fine_tune"]          # fine tune (cents -> ratio), differentiable in k
        f0_base = midi_to_hz(note)
        H = _harmonic_count(f0_base * (2.0 ** (100.0 / 1200.0)), sr)

        # -- modulators -------------------------------------------------------
        env = _adsr(p["attack"], p["decay"], p["sustain"], p["release"], note_len, n, sr)
        lfo = _lfo_wave(p["lfo_rate"], _LFO_SHAPE_NAME[s["lfo_shape"]], n, sr)
        lfo = p["lfo_depth"] * lfo

        # -- oscillators (pitch = f0 * detune * LFO-vibrato) ------------------
        semis = p["lfo_to_pitch"] * lfo
        ratio = anp.exp((cents / 1200.0 + semis / 12.0) * math.log(2.0))
        f_t = f0_base * ratio                                 # per-sample (n,)
        ph = _phase(f_t, n, sr, timevarying=True)
        saw = _osc_saw(ph, H)
        pulse = _osc_pulse(ph, p["pulse_width"], H)
        oct_shift, sub_duty = _SUB_OCTAVE[s["sub_octave"]]
        sub_ph = ph * (2.0 ** oct_shift)
        sub = _osc_pulse(sub_ph, sub_duty, max(1, H // 2))
        nz = np.random.default_rng(0).standard_normal(n)

        mix = (
            p["saw_lvl"] * saw
            + p["square_lvl"] * pulse
            + p["sub_lvl"] * sub
            + p["noise_lvl"] * nz
        )

        # -- filter: cutoff in log2 space, env- & LFO-modulated ---------------
        key_oct = p["key_follow"] * (note - 60) / 12.0
        log2_fc = anp.log2(anp.clip(p["cutoff"], 1e-3, None)) + key_oct
        mod = p["env_to_cutoff"] * env + p["lfo_to_cutoff"] * lfo
        log2_fc_t = log2_fc + mod
        # soft two-sided bound to [20 Hz, 0.45*sr]
        lo, hi = math.log2(20.0), math.log2(0.45 * sr)
        log2_fc_t = hi - _softplus(hi - log2_fc_t)
        log2_fc_t = lo + _softplus(log2_fc_t - lo)
        cutoff_t = 2.0 ** log2_fc_t

        modulated = bool(np.any(np.abs(np.asarray(_to_value(mod))) > 1e-6))
        if modulated:
            block = max(8, sr // 50)
            filtered = _ladder_tv(mix, cutoff_t, p["resonance"], sr, block)
        else:
            fc0 = cutoff_t if np.ndim(_to_value(cutoff_t)) == 0 else cutoff_t[0]
            filtered = _ladder_static(mix, fc0, p["resonance"], sr)

        # -- VCA: full ADSR (Envelope) or a held gate (Gate mode) -------------
        if s["amp_env_mode"] == 1:
            vca = env
        else:
            gate = _adsr(p["attack"], p["decay"] * 0.0 + 1e-3, p["sustain"] * 0.0 + 1.0,
                         p["release"], note_len, n, sr)
            vca = gate
        return filtered * vca

    def render_chord(
        self, k: Any, s: dict[str, int] | None = None, notes: Sequence[int] = (60,)
    ) -> Any:
        """Render the SAME ``(k, s)`` patch on each MIDI note in ``notes`` and sum
        the voices — a polyphonic chord on this one patch (for matching chords).

        Stays autograd-differentiable in ``k`` (it is a plain sum of
        :meth:`render` calls). The sum is normalized by ``sqrt(len(notes))`` so a
        thick chord does not clip relative to a single note. A single-element
        ``notes`` therefore equals the mono :meth:`render` (``sqrt(1) == 1``)."""
        note_list = [int(n) for n in notes]
        if not note_list:
            raise ValueError("render_chord needs at least one note")
        total = self.render(k, s, note_list[0])
        for note in note_list[1:]:
            total = total + self.render(k, s, note)
        return total / math.sqrt(len(note_list))

    # -- CC <-> k conversions -------------------------------------------------
    def cc_to_k(self, cc: dict[int, int]) -> np.ndarray:
        """Normalize a CC vector to the twin's k in [0,1] (uses schema CC ranges)."""
        from ..schema import param_by_cc

        k = np.zeros(self.k_dim, dtype=np.float64)
        for i, kp in enumerate(K_PARAMS):
            sp = param_by_cc(kp.cc)
            lo, hi = (sp.min_val, sp.max_val) if sp else (0, 127)
            v = cc.get(kp.cc, sp.default if sp else 0)
            k[i] = (v - lo) / max(1, (hi - lo))
        return np.clip(k, 0.0, 1.0)

    def k_to_cc(self, k: Any, s: dict[str, int] | None = None) -> dict[int, int]:
        """Denormalize k (+ s option indices) to a full integer CC vector."""
        from ..schema import param_by_cc

        s = {**_default_s(), **(s or {})}
        cc: dict[int, int] = {}
        for i, kp in enumerate(K_PARAMS):
            sp = param_by_cc(kp.cc)
            lo, hi = (sp.min_val, sp.max_val) if sp else (0, 127)
            cc[kp.cc] = int(round(lo + float(_to_value(k[i])) * (hi - lo)))
        for spar in S_PARAMS:
            cc[spar.cc] = int(s[spar.name])
        return cc

    def s_configs(
        self, which: Sequence[str] = ("sub_octave", "lfo_shape", "amp_env_mode")
    ) -> list[dict[str, int]]:
        """Cartesian product of the enumerated s choices (for the search sweep)."""
        from itertools import product

        pars = [sp for sp in S_PARAMS if sp.name in which]
        combos = product(*[sp.choices for sp in pars])
        return [dict(zip([sp.name for sp in pars], combo)) for combo in combos]

    # -- InstrumentBackend (tier-1 structural conformance) --------------------
    def connect(self, port: str) -> None:
        self._connected = True
        self._port = port

    def disconnect(self) -> None:
        self._connected = False

    def push_all(self, patch: dict[int, int]) -> None:
        self._patch = dict(patch)

    def send(self, param: int, value: int) -> bool:
        if not self._connected:
            return False
        self._patch[param] = value
        return True

    def on_incoming(self, cb: Callable[[int, int], None]) -> None:
        self._callbacks.append(cb)


def _to_value(x: Any) -> Any:
    """Peel an autograd box to its concrete numpy value (for control-flow only)."""
    return getattr(x, "_value", x)


# ─────────────────────────────────────────────────────────────────────────────
# Differentiable spectral loss  (credit: music/music/dsp/loss.py)
# NOTE: features.py / distance.py are plain numpy and NOT autograd-differentiable;
# this is the twin's OWN differentiable objective for the gradient search. It is
# perceptually MOTIVATED, not perceptually VALIDATED (FABLE).
# ─────────────────────────────────────────────────────────────────────────────
_EPS = 1e-7


def _stft_mag(x: Any, n_fft: int, hop: int) -> Any:
    n = x.shape[-1]
    if n < n_fft:
        x = anp.concatenate([x, anp.zeros(n_fft - n)])
        n = n_fft
    window = np.hanning(n_fft)
    n_frames = 1 + (n - n_fft) // hop
    idx = np.arange(n_fft)[:, None] + hop * np.arange(n_frames)[None, :]
    frames = x[idx] * window[:, None]                          # (n_fft, frames)
    spec = anp.fft.rfft(frames, n=n_fft, axis=0)
    return anp.abs(spec)


def _hz_to_mel(hz: np.ndarray | float) -> np.ndarray:
    return 2595.0 * np.log10(1.0 + np.asarray(hz) / 700.0)


def _mel_to_hz(mel: np.ndarray) -> np.ndarray:
    return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)


_MEL_CACHE: dict[tuple, np.ndarray] = {}


def _mel_fb(sr: float, n_fft: int, n_mels: int = 64, fmin: float = 30.0) -> np.ndarray:
    """Slaney-style triangular mel filterbank (n_mels, n_fft//2+1) — a constant
    matrix mirroring features.py, so the twin's differentiable log-mel loss lives
    in the same perceptual bands the plain scoring metric uses."""
    key = (round(sr), n_fft, n_mels, fmin)
    if key in _MEL_CACHE:
        return _MEL_CACHE[key]
    fmax = sr / 2.0
    n_bins = n_fft // 2 + 1
    fft_freqs = np.linspace(0, fmax, n_bins)
    mel_pts = np.linspace(_hz_to_mel(fmin), _hz_to_mel(fmax), n_mels + 2)
    hz_pts = _mel_to_hz(mel_pts)
    fb = np.zeros((n_mels, n_bins))
    for m in range(n_mels):
        lo, ctr, hi = hz_pts[m], hz_pts[m + 1], hz_pts[m + 2]
        left = (fft_freqs - lo) / max(ctr - lo, 1e-9)
        right = (hi - fft_freqs) / max(hi - ctr, 1e-9)
        fb[m] = np.clip(np.minimum(left, right), 0, None)
    _MEL_CACHE[key] = fb
    return fb


def logmel_loss(a: Any, b: Any, sr: float = WORKING_SR, n_fft: int = 1024,
                n_mels: int = 64) -> Any:
    """Mean L1 between log-mel spectrograms — the differentiable twin of the plain
    metric's workhorse ``logmel`` term (credit: features.py mel path + loss.py L1).
    Each spectrogram is offset by its own max (level-invariant), matching how the
    plain metric normalizes log-mel dB to [-1, 0]."""
    n = a.shape[-1]
    n_fft = min(n_fft, 1 << int(math.floor(math.log2(max(2, n)))))
    hop = max(1, n_fft // 4)
    fb = _mel_fb(sr, n_fft, n_mels)
    ma = _stft_mag(a, n_fft, hop)
    mb = _stft_mag(b, n_fft, hop)
    la = anp.log(anp.dot(fb, ma * ma) + 1e-8)
    lb = anp.log(anp.dot(fb, mb * mb) + 1e-8)
    la = la - anp.max(la)
    lb = lb - anp.max(lb)
    return anp.mean(anp.abs(la - lb))


def multiscale_stft_loss(a: Any, b: Any, sr: float = WORKING_SR,
                         ffts: tuple[int, ...] = (2048, 512, 128)) -> Any:
    """Mean L1 distance between log-magnitude spectrograms at several scales."""
    n = a.shape[-1]
    total = anp.array(0.0)
    used = 0
    for n_fft in ffts:
        if n_fft > n:
            continue
        hop = max(1, n_fft // 4)
        la = anp.log(_stft_mag(a, n_fft, hop) + _EPS)
        lb = anp.log(_stft_mag(b, n_fft, hop) + _EPS)
        total = total + anp.mean(anp.abs(la - lb))
        used += 1
    if used == 0:  # pragma: no cover - signal shorter than every scale
        return anp.mean(anp.abs(a - b))
    return total / used


def _rms_env(x: Any, sr: float, frame_ms: float = 10.0) -> Any:
    n = x.shape[-1]
    frame = min(max(2, int(round(sr * frame_ms / 1000.0))), n)
    hop = max(1, frame // 2)
    n_frames = 1 + (n - frame) // hop
    idx = np.arange(frame)[:, None] + hop * np.arange(n_frames)[None, :]
    fr = x[idx]
    return anp.sqrt(anp.mean(fr * fr, axis=0) + _EPS)


def envelope_loss(a: Any, b: Any, sr: float = WORKING_SR, frame_ms: float = 10.0) -> Any:
    """L1 between RMS envelopes — the ADSR-shaped part of the objective."""
    return anp.mean(anp.abs(_rms_env(a, sr, frame_ms) - _rms_env(b, sr, frame_ms)))


def _rms_normalize(x: Any) -> Any:
    """Loudness-invariant scaling (differentiable) — score timbre, not level."""
    return x / (anp.sqrt(anp.mean(x * x) + _EPS))


def spectral_loss(cand: Any, target: Any, sr: float = WORKING_SR,
                  envelope_weight: float = 4.0, stft_weight: float = 0.3) -> Any:
    """The twin's default match objective. Primary term is a differentiable
    **log-mel** distance (aligned with the plain scoring metric's workhorse), plus
    a light multi-scale linear-STFT term for fine detail and a weighted RMS-envelope
    term for the ADSR shape. All on RMS-normalized audio (loudness-invariant).

    NOTE (FABLE): this is perceptually *motivated*, not perceptually *validated* —
    validating it against Tyler's ears is a later ceremony, not done here."""
    ca, ta = _rms_normalize(cand), _rms_normalize(target)
    return (
        logmel_loss(ca, ta, sr)
        + stft_weight * multiscale_stft_loss(ca, ta, sr)
        + envelope_weight * envelope_loss(ca, ta, sr)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Twin-guided match: gradient-descend k to a target
# ─────────────────────────────────────────────────────────────────────────────
def _adam_descend(
    objective: Callable[[np.ndarray], float],
    x0: np.ndarray,
    iters: int = 120,
    lr: float = 0.06,
) -> tuple[np.ndarray, float, int]:
    """Minimize ``objective`` (autograd-differentiable) over x in [0,1] with Adam.

    Returns (best_x, best_loss, evals). Projects x back into [0,1] each step."""
    g = grad(objective)
    x = np.clip(np.asarray(x0, dtype=np.float64), 0.0, 1.0)
    m = np.zeros_like(x)
    v = np.zeros_like(x)
    b1, b2, eps = 0.9, 0.999, 1e-8
    best_x, best_l, evals = x.copy(), float(objective(x)), 1
    for t in range(1, iters + 1):
        gr = g(x)
        gr = np.where(np.isfinite(gr), gr, 0.0)
        m = b1 * m + (1 - b1) * gr
        v = b2 * v + (1 - b2) * gr * gr
        mhat = m / (1 - b1 ** t)
        vhat = v / (1 - b2 ** t)
        x = np.clip(x - lr * mhat / (np.sqrt(vhat) + eps), 0.0, 1.0)
        cur = float(objective(x))
        evals += 2  # one forward + one grad (grad also does a forward)
        if cur < best_l:
            best_l, best_x = cur, x.copy()
    return best_x, best_l, evals


@dataclass
class TwinMatch:
    """Result of a twin-guided match (richer than the harness MatchResult)."""

    k: np.ndarray
    s: dict[str, int]
    cc: dict[int, int]
    audio: np.ndarray
    diff_loss: float
    closeness: float
    evals: int
    seconds: float


class TwinMatcher:
    """Conforms to :class:`~synth.match.corpus.Matcher`: target -> MatchResult.

    Gradient-descends the twin's k to the target's audio for each of a few
    enumerated s-configs, keeps the best by the plain (harness) distance, and
    returns a :class:`~synth.match.corpus.MatchResult` — so it plugs straight into
    ``benchmark()``. ``probe_count`` is 0: the twin touches NO hardware."""

    def __init__(
        self,
        twin: Twin | None = None,
        iters: int = 160,
        lr: float = 0.08,
        s_sweep: Sequence[str] = ("lfo_shape",),
        seed: int = 0,
        search_sr: int = 16000,
        search_seconds: float = 1.5,
    ) -> None:
        # ``twin`` renders the FINAL candidate at full resolution (for scoring);
        # a cheaper ``search_twin`` carries the many gradient evals. Align the
        # search twin's note-off to the SAME ABSOLUTE time as the full twin's, so a
        # shorter search render still matches the target's envelope shape (gate is a
        # fraction of the render length, so equal fractions would drift apart).
        self.twin = twin or Twin()
        note_off_s = self.twin.seconds * self.twin.gate_fraction
        search_gate = min(1.0, note_off_s / search_seconds)
        self.search_twin = Twin(sr=search_sr, seconds=search_seconds,
                                gate_fraction=search_gate)
        self.iters = iters
        self.lr = lr
        self.s_sweep = tuple(s_sweep)
        self.seed = seed

    def match(self, target_audio: np.ndarray, note: int) -> TwinMatch:
        import time as _time

        stw = self.search_twin
        # Resample the target to the (cheaper) search rate for the gradient loss.
        tgt_full = AudioClip(np.asarray(target_audio, dtype=np.float32), self.twin.sr)
        tgt_lo = tgt_full.resample(stw.sr).samples.astype(np.float64)
        n = max(1, int(round(stw.seconds * stw.sr)))
        if tgt_lo.shape[0] < n:
            tgt_lo = np.concatenate([tgt_lo, np.zeros(n - tgt_lo.shape[0])])
        else:
            tgt_lo = tgt_lo[:n]

        rng = np.random.default_rng(self.seed)
        start = _time.perf_counter()
        best: TwinMatch | None = None
        total_evals = 0
        for s in stw.s_configs(self.s_sweep):
            s_full = {**_default_s(), **s}

            def obj(k: np.ndarray, _s=s_full) -> float:
                return spectral_loss(stw.render(k, _s, note), tgt_lo, stw.sr)

            x0 = 0.5 + 0.05 * rng.standard_normal(stw.k_dim)
            xk, lk, evals = _adam_descend(obj, x0, self.iters, self.lr)
            total_evals += evals
            # Final candidate rendered at FULL resolution and scored by the plain metric.
            audio = np.asarray(self.twin.render(xk, s_full, note), dtype=np.float32)
            clos = _closeness_vs(audio, np.asarray(target_audio, np.float64), self.twin.sr)
            if best is None or clos > best.closeness:
                best = TwinMatch(
                    k=xk, s=s_full, cc=self.twin.k_to_cc(xk, s_full), audio=audio,
                    diff_loss=lk, closeness=clos, evals=evals, seconds=0.0,
                )
        assert best is not None
        best.evals = total_evals
        best.seconds = _time.perf_counter() - start
        return best

    def __call__(self, target: Any) -> Any:
        from .corpus import MatchResult

        # target audio: prefer the clip; fall back to nothing (features-only fixture)
        note = int(round(69.0 + 12.0 * math.log2(max(target.f0, 1e-6) / 440.0)))
        if getattr(target, "clip", None) is not None:
            audio = np.asarray(target.clip.samples, dtype=np.float64)
        else:  # pragma: no cover - fixtures without audio can't drive the twin
            raise ValueError(
                f"TwinMatcher needs target audio; {target.name!r} is features-only"
            )
        m = self.match(audio, note)
        feats = extract(prepare(AudioClip(m.audio, self.twin.sr)))
        return MatchResult(
            features=feats, probe_count=0, cache_hits=0, cc=m.cc,
            extra={"diff_loss": m.diff_loss, "evals": m.evals,
                   "twin_closeness": m.closeness, "s": m.s, "note": note},
        )


def _closeness_vs(cand_audio: np.ndarray, target_audio: np.ndarray, sr: int) -> float:
    """Plain-metric closeness between two raw audio buffers (apples-to-apples)."""
    tf = extract(prepare(AudioClip(np.asarray(target_audio, np.float32), sr)))
    cf = extract(prepare(AudioClip(np.asarray(cand_audio, np.float32), sr)))
    w = Weights()
    return closeness(plain_loss(tf, cf, w, scales=reference_scales(tf)), w)


# ─────────────────────────────────────────────────────────────────────────────
# Calibration seam  (credit: music/music/dsp/mapping.py — the swap-a-curve idea)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class CalibrationReport:
    """What :func:`calibrate` learned + the honest gap it left behind."""

    mapping: Mapping
    held_out_feature_error: float
    n_fit: int
    n_held_out: int
    is_synthetic: bool
    note: str


def calibrate(
    probes: Iterable[tuple[dict[int, int], np.ndarray]],
    twin: Twin | None = None,
    held_out_fraction: float = 0.3,
    seed: int = 0,
    note: int = 48,
) -> CalibrationReport:
    """Fit the twin's free constants to ``(cc, audio)`` probes; report the gap.

    **This session has no S-1 hardware.** So ``probes`` are SYNTHETIC/self-consistent
    (twin- or placeholder-rendered), and the returned mapping is labeled as such.
    The seam is real: when real S-1 probes exist, pass them here unchanged and this
    fits the same curves (e.g. cutoff Hz<->CC74) against measured hardware — the
    ``calibrate(real_probes)`` moment FABLE reserves for the hardware/ears session.

    We always report **held-out** twin-vs-target feature error (FABLE: report the
    gap, never assume it away). The default implementation keeps the default curves
    (they are already self-consistent for twin-rendered probes) and MEASURES the gap
    on a held-out split; a real-hardware fit would additionally invert the measured
    CC<->Hz / CC<->seconds curves via :meth:`Curve.invert` and rebuild the mapping.
    """
    tw = twin or Twin()
    items = list(probes)
    if not items:
        raise ValueError("calibrate needs at least one (cc, audio) probe")
    rng = np.random.default_rng(seed)
    order = rng.permutation(len(items))
    n_hold = max(1, int(round(len(items) * held_out_fraction))) if len(items) > 1 else 0
    held = set(order[:n_hold].tolist())

    errs: list[float] = []
    for i, (cc, audio) in enumerate(items):
        if i not in held:
            continue
        k = tw.cc_to_k(cc)
        s = {sp.name: int(cc.get(sp.cc, _default_s()[sp.name])) for sp in S_PARAMS}
        cand = np.asarray(tw.render(k, s, note), dtype=np.float32)
        errs.append(float(_feature_error(cand, np.asarray(audio, np.float32), tw.sr)))

    return CalibrationReport(
        mapping=tw.mapping,
        held_out_feature_error=float(np.mean(errs)) if errs else 0.0,
        n_fit=len(items) - len(held),
        n_held_out=len(held),
        is_synthetic=True,
        note="SYNTHETIC calibration only (no real S-1 in this session); "
             "replace probes with real hardware captures to close the sim-to-real gap.",
    )


def _feature_error(a: np.ndarray, b: np.ndarray, sr: int) -> float:
    """Plain-metric feature distance between two audio buffers (0 == identical)."""
    fa = extract(prepare(AudioClip(np.asarray(a, np.float32), sr)))
    fb = extract(prepare(AudioClip(np.asarray(b, np.float32), sr)))
    return plain_loss(fb, fa, Weights(), scales=reference_scales(fb))


# ─────────────────────────────────────────────────────────────────────────────
# Browser export — the constants the in-browser twin (synth/web/static/twin/)
# runs, so the page plays THIS model, not a look-alike. A drift test
# (tests/test_twin_curves.py) pins the committed curves.json to this function.
# ─────────────────────────────────────────────────────────────────────────────
def export_curves(
    mapping: Mapping | None = None,
    *,
    calibrated: bool = False,
    source: str | None = None,
) -> dict[str, Any]:
    """Everything the browser twin needs to run this model, as JSON-native data.

    ``curves`` carries one ``{"lo", "hi", "kind", "unit"}`` entry per k param —
    the calibration unit. A calibration run writes the same ``curves`` sub-schema
    with ``calibrated=True`` (pass its fitted ``mapping``); the browser merges such
    a file over the defaults, so it may carry ``curves`` alone.

    The rest is the model's structure, read from the objects ``render`` uses:
    the k/s CC tables, the discrete option maps, the render defaults (sr,
    seconds, gate fraction), and ``cc_ranges`` — ``[min, max, default]`` for every
    S-1 CC from ``s1.json``, which ``cc_to_k`` needs to normalize a CC map and the
    browser needs for the controls it plays itself (voice mode, glide, effects).
    """
    from ..schema import S1_PARAMS

    mp = mapping or DEFAULT_MAPPING
    tw = Twin(mapping=mp)
    return {
        "version": 1,
        "calibrated": bool(calibrated),
        "source": source or ("twin.DEFAULT_CURVES" if mapping is None else mp.name),
        "sr": tw.sr,
        "seconds": tw.seconds,
        "gate_fraction": tw.gate_fraction,
        "max_harmonics": MAX_HARMONICS,
        "curves": {
            name: {"lo": c.lo, "hi": c.hi, "kind": c.kind, "unit": c.unit}
            for name, c in mp.curves.items()
        },
        "k_params": [[p.name, p.cc] for p in K_PARAMS],
        "s_params": [[p.name, p.cc, list(p.choices)] for p in S_PARAMS],
        "sub_octave": {str(i): [shift, duty] for i, (shift, duty) in _SUB_OCTAVE.items()},
        "lfo_shape": {str(i): name for i, name in _LFO_SHAPE_NAME.items()},
        "default_s": _default_s(),
        "cc_ranges": {str(p.cc): [p.min_val, p.max_val, p.default] for p in S1_PARAMS},
    }


if __name__ == "__main__":  # pragma: no cover - thin CLI over export_curves()
    import argparse
    import json
    import sys

    _ap = argparse.ArgumentParser(
        prog="python -m synth.match.twin",
        description="Write the browser twin's curves.json (export_curves()).",
    )
    _ap.add_argument("--export-curves", metavar="PATH", required=True,
                     help="output path, or '-' for stdout")
    _args = _ap.parse_args()
    _text = json.dumps(export_curves(), indent=2) + "\n"
    if _args.export_curves == "-":
        sys.stdout.write(_text)
    else:
        with open(_args.export_curves, "w", encoding="utf-8") as _fh:
            _fh.write(_text)
