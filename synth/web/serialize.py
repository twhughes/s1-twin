"""Turn engine objects into JSON / audio payloads for the web layer."""

from __future__ import annotations

import base64
import io

import numpy as np

from ..match.capture import AudioClip
from ..match.features import extract


def spectrogram_payload(clip: AudioClip) -> dict:
    """Log-mel spectrogram as a base64 grayscale bitmap for canvas rendering.

    Values map quiet -> 0 (dark) and loud -> 255 (bright). Rows are mel bands
    (low to high), columns are time frames.
    """
    logmel = extract(clip).logmel  # (n_mels, frames) in [-1, 0]
    img = np.clip((logmel + 1.0) * 255.0, 0, 255).astype(np.uint8)
    return {
        "h": int(img.shape[0]),
        "w": int(img.shape[1]),
        "data": base64.b64encode(img.tobytes()).decode("ascii"),
    }


def wav_bytes(clip: AudioClip) -> bytes:
    """Encode a clip as 16-bit PCM WAV."""
    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, clip.samples, clip.samplerate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def progress_payload(p) -> dict:
    """Numeric fields of a Progress snapshot (no heavy arrays)."""
    return {
        "iteration": p.iteration,
        "evals": p.evals,
        "max_iters": p.max_iters,
        "best_closeness": round(p.best_closeness, 2),
        "last_closeness": round(p.last_closeness, 2),
        "best_loss": round(p.best_loss, 4) if p.best_loss != float("inf") else None,
        "generation_done": p.generation_done,
        "done": p.done,
        "error": p.error,
        "cache_hits": p.cache_hits,
        "best_params": p.best_params,
    }
