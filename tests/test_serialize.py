"""Direct payload-shape unit tests for :mod:`synth.web.serialize`.

These exercise the serializers straight — no FastAPI endpoint in the loop —
so the JSON/audio shapes the browser depends on are pinned at their source.
Needs the [studio] extras (numpy/scipy/soundfile), the same as the match code
they serialize.
"""

from __future__ import annotations

import base64
import io

import numpy as np
import pytest

pytest.importorskip("scipy")
pytest.importorskip("soundfile")

from synth.match import WORKING_SR
from synth.match.capture import AudioClip
from synth.match.session import Progress
from synth.web.serialize import progress_payload, spectrogram_payload, wav_bytes


def a440(seconds: float = 2.0, sr: int = WORKING_SR) -> AudioClip:
    """A plain sine clip long enough to yield real spectrogram frames."""
    t = np.linspace(0, seconds, int(seconds * sr), endpoint=False)
    return AudioClip((0.6 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), sr)


class TestSpectrogramPayload:
    def test_keys_and_types(self):
        payload = spectrogram_payload(a440())
        assert set(payload) == {"h", "w", "data"}
        assert isinstance(payload["h"], int)
        assert isinstance(payload["w"], int)
        assert isinstance(payload["data"], str)

    def test_dimensions_are_positive(self):
        payload = spectrogram_payload(a440())
        assert payload["h"] > 0 and payload["w"] > 0

    def test_data_is_uint8_bitmap_of_hw_bytes(self):
        payload = spectrogram_payload(a440())
        raw = base64.b64decode(payload["data"])
        # One uint8 per (mel band × time frame) cell.
        assert len(raw) == payload["h"] * payload["w"]
        assert max(raw) <= 255 and min(raw) >= 0


class TestProgressPayload:
    def _progress(self, **kw) -> Progress:
        base = dict(
            iteration=3,
            evals=12,
            max_iters=40,
            best_closeness=0.876,
            best_loss=0.01234,
            best_params={74: 90, 71: 40},
            last_closeness=0.5,
            generation_done=True,
            done=False,
            error=None,
            cache_hits=2,
        )
        base.update(kw)
        return Progress(**base)

    def test_keys_and_types(self):
        payload = progress_payload(self._progress())
        assert set(payload) == {
            "iteration", "evals", "max_iters", "best_closeness",
            "last_closeness", "best_loss", "generation_done", "done",
            "error", "cache_hits", "best_params",
        }
        assert isinstance(payload["iteration"], int)
        assert isinstance(payload["best_params"], dict)
        assert payload["best_params"] == {74: 90, 71: 40}

    def test_floats_are_rounded(self):
        payload = progress_payload(self._progress())
        assert payload["best_closeness"] == 0.88   # 2 dp
        assert payload["best_loss"] == 0.0123       # 4 dp

    def test_infinite_best_loss_becomes_none(self):
        payload = progress_payload(self._progress(best_loss=float("inf")))
        assert payload["best_loss"] is None

    def test_error_passes_through(self):
        payload = progress_payload(self._progress(error="boom", done=True))
        assert payload["error"] == "boom"
        assert payload["done"] is True


class TestWavBytes:
    def test_is_a_riff_wave(self):
        data = wav_bytes(a440(seconds=0.5))
        assert isinstance(data, bytes)
        assert data[:4] == b"RIFF"
        assert data[8:12] == b"WAVE"

    def test_roundtrips_to_the_same_samplerate_and_length(self):
        import soundfile as sf

        clip = a440(seconds=0.5)
        samples, sr = sf.read(io.BytesIO(wav_bytes(clip)))
        assert sr == clip.samplerate
        assert len(samples) == len(clip.samples)
