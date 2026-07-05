"""Timbre feature extraction (numpy + scipy only — no librosa).

Computes a small bundle of features on a prepared :class:`~s1tui.match.capture.AudioClip`:
a log-mel spectrogram (the workhorse), MFCCs, per-frame spectral descriptors, and
an RMS amplitude envelope (captures the ADSR shape). Target and candidate clips are
fixed-length, so every field has a deterministic shape and can be compared directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from .capture import AudioClip

N_FFT = 1024
HOP = 256
N_MELS = 64
N_MFCC = 20
FMIN = 30.0


@dataclass
class Features:
    logmel: np.ndarray   # (n_mels, frames), dB scaled to [-1, 0]
    mfcc: np.ndarray     # (N_MFCC, frames)
    centroid: np.ndarray  # (frames,), normalized to [0, 1] of Nyquist
    flatness: np.ndarray  # (frames,), [0, 1]
    env: np.ndarray      # (frames,), peak-normalized RMS envelope


def _hz_to_mel(hz: np.ndarray | float) -> np.ndarray | float:
    return 2595.0 * np.log10(1.0 + np.asarray(hz) / 700.0)


def _mel_to_hz(mel: np.ndarray) -> np.ndarray:
    return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)


@lru_cache(maxsize=8)
def _mel_filterbank(sr: int, n_fft: int, n_mels: int, fmin: float) -> np.ndarray:
    """Slaney-style triangular mel filterbank, shape (n_mels, n_fft//2 + 1)."""
    fmax = sr / 2.0
    n_bins = n_fft // 2 + 1
    fft_freqs = np.linspace(0, fmax, n_bins)
    mel_pts = np.linspace(_hz_to_mel(fmin), _hz_to_mel(fmax), n_mels + 2)
    hz_pts = _mel_to_hz(mel_pts)

    fb = np.zeros((n_mels, n_bins), dtype=np.float64)
    for m in range(n_mels):
        lo, ctr, hi = hz_pts[m], hz_pts[m + 1], hz_pts[m + 2]
        left = (fft_freqs - lo) / max(ctr - lo, 1e-9)
        right = (hi - fft_freqs) / max(hi - ctr, 1e-9)
        fb[m] = np.clip(np.minimum(left, right), 0, None)
    return fb


def _stft_mag(samples: np.ndarray, n_fft: int, hop: int) -> np.ndarray:
    """Magnitude STFT via framing + Hann window, shape (n_fft//2 + 1, frames)."""
    if len(samples) < n_fft:
        samples = np.pad(samples, (0, n_fft - len(samples)))
    window = np.hanning(n_fft).astype(np.float64)
    n_frames = 1 + (len(samples) - n_fft) // hop
    idx = np.arange(n_fft)[:, None] + hop * np.arange(n_frames)[None, :]
    frames = samples[idx] * window[:, None]
    spec = np.fft.rfft(frames, n=n_fft, axis=0)
    return np.abs(spec)


def extract(clip: AudioClip) -> Features:
    sr = clip.samplerate
    mag = _stft_mag(clip.samples.astype(np.float64), N_FFT, HOP)  # (bins, frames)
    power = mag ** 2

    fb = _mel_filterbank(sr, N_FFT, N_MELS, FMIN)
    mel = fb @ power  # (n_mels, frames)
    logmel_db = 10.0 * np.log10(mel + 1e-10)
    logmel = np.clip((logmel_db - logmel_db.max()) / 80.0, -1.0, 0.0)

    # MFCC: DCT-II of log-mel along the mel axis.
    from scipy.fftpack import dct

    mfcc = dct(logmel, axis=0, type=2, norm="ortho")[:N_MFCC]

    freqs = np.linspace(0, sr / 2.0, mag.shape[0])[:, None]
    mag_sum = mag.sum(axis=0) + 1e-10
    centroid = (freqs * mag).sum(axis=0) / mag_sum
    centroid = np.clip(centroid / (sr / 2.0), 0.0, 1.0)

    geo_mean = np.exp(np.mean(np.log(mag + 1e-10), axis=0))
    arith_mean = mag.mean(axis=0) + 1e-10
    flatness = np.clip(geo_mean / arith_mean, 0.0, 1.0)

    # RMS amplitude envelope per frame, peak-normalized.
    n_frames = mag.shape[1]
    idx = np.arange(N_FFT)[:, None] + HOP * np.arange(n_frames)[None, :]
    padded = clip.samples.astype(np.float64)
    if len(padded) < N_FFT + HOP * (n_frames - 1):
        padded = np.pad(padded, (0, N_FFT + HOP * (n_frames - 1) - len(padded)))
    env = np.sqrt((padded[idx] ** 2).mean(axis=0))
    if env.max() > 1e-9:
        env = env / env.max()

    return Features(
        logmel=logmel.astype(np.float32),
        mfcc=mfcc.astype(np.float32),
        centroid=centroid.astype(np.float32),
        flatness=flatness.astype(np.float32),
        env=env.astype(np.float32),
    )
