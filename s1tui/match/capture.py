"""Audio I/O for the matching engine: load target files, record the S-1.

All clips are normalized to mono float32 at :data:`s1tui.match.WORKING_SR`, onset-
trimmed, and clamped to :data:`s1tui.match.ANALYSIS_SECONDS` so that any two clips
are directly comparable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import ANALYSIS_SECONDS, WORKING_SR


@dataclass
class AudioClip:
    """A mono audio buffer at a known samplerate."""

    samples: np.ndarray  # float32, shape (n,)
    samplerate: int

    @property
    def duration(self) -> float:
        return len(self.samples) / self.samplerate

    def resample(self, target_sr: int) -> "AudioClip":
        if self.samplerate == target_sr:
            return self
        from scipy.signal import resample_poly

        g = np.gcd(self.samplerate, target_sr)
        up, down = target_sr // g, self.samplerate // g
        out = resample_poly(self.samples, up, down).astype(np.float32)
        return AudioClip(out, target_sr)


def _to_mono(data: np.ndarray) -> np.ndarray:
    if data.ndim == 2:
        data = data.mean(axis=1)
    return np.asarray(data, dtype=np.float32)


def refresh_devices() -> None:
    """Force PortAudio to re-enumerate devices so hot-plugged hardware (like the
    S-1) appears without restarting the process. No-op if a stream is open."""
    import sounddevice as sd

    try:
        sd._terminate()
        sd._initialize()
    except Exception:  # noqa: BLE001
        pass


def list_input_devices() -> list[dict]:
    """Return selectable audio input devices as ``{index, name, channels, sr}``."""
    import sounddevice as sd

    out = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            out.append(
                {
                    "index": idx,
                    "name": dev["name"],
                    "channels": dev["max_input_channels"],
                    "samplerate": int(dev["default_samplerate"]),
                }
            )
    return out


def list_output_devices() -> list[dict]:
    """Return selectable audio output devices as ``{index, name, channels, sr}``."""
    import sounddevice as sd

    out = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_output_channels"] > 0:
            out.append(
                {
                    "index": idx,
                    "name": dev["name"],
                    "channels": dev["max_output_channels"],
                    "samplerate": int(dev["default_samplerate"]),
                }
            )
    return out


def load_audio(path: str | Path, sr: int = WORKING_SR) -> AudioClip:
    """Load a target audio file (wav/aiff/flac/ogg) as a mono clip at ``sr``."""
    import soundfile as sf

    data, file_sr = sf.read(str(path), always_2d=False)
    return AudioClip(_to_mono(data), int(file_sr)).resample(sr)


def record(device: int | str | None, duration: float, sr: int = WORKING_SR) -> AudioClip:
    """Record ``duration`` seconds from ``device`` and return a clip at ``sr``.

    Records at the device's native samplerate, then resamples — avoids "unsupported
    samplerate" errors on interfaces that only run at 44.1/48 kHz.
    """
    import sounddevice as sd

    info = sd.query_devices(device, "input")
    rec_sr = int(info["default_samplerate"])
    frames = int(duration * rec_sr)
    data = sd.rec(frames, samplerate=rec_sr, channels=1, device=device, dtype="float32")
    sd.wait()
    return AudioClip(_to_mono(data), rec_sr).resample(sr)


def find_onset(samples: np.ndarray, sr: int, threshold_db: float = -45.0) -> int:
    """Return the sample index where energy first crosses ``threshold_db`` (rel. peak)."""
    if samples.size == 0:
        return 0
    win = max(1, sr // 1000)  # ~1 ms RMS window
    sq = samples.astype(np.float64) ** 2
    kernel = np.ones(win) / win
    rms = np.sqrt(np.convolve(sq, kernel, mode="same") + 1e-12)
    peak = rms.max()
    if peak <= 0:
        return 0
    thresh = peak * (10.0 ** (threshold_db / 20.0))
    above = np.nonzero(rms >= thresh)[0]
    return int(above[0]) if above.size else 0


def is_silent(clip: AudioClip, threshold_db: float = -60.0) -> bool:
    """True if the clip never crosses an absolute dBFS floor.

    ``prepare()`` skips peak-normalization for near-zero audio, so a silent
    probe (closed VCA/filter — common early in a search) stays near zero and
    is detectable here. Callers use this to penalize silence instead of
    letting its degenerate features score a misleadingly low loss.
    """
    if clip.samples.size == 0:
        return True
    peak = float(np.abs(clip.samples).max())
    return peak < 10.0 ** (threshold_db / 20.0)


def prepare(clip: AudioClip, seconds: float = ANALYSIS_SECONDS) -> AudioClip:
    """Onset-trim, fix length to ``seconds``, and peak-normalize.

    Peak-normalizing removes loudness differences so the score reflects *timbre*,
    not how hot the signal was recorded.
    """
    sr = clip.samplerate
    start = find_onset(clip.samples, sr)
    samples = clip.samples[start:]

    target_len = int(seconds * sr)
    if len(samples) < target_len:
        samples = np.pad(samples, (0, target_len - len(samples)))
    else:
        samples = samples[:target_len]

    peak = np.abs(samples).max()
    if peak > 1e-6:
        samples = samples / peak
    return AudioClip(samples.astype(np.float32), sr)
