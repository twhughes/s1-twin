"""Direct unit tests for the drift-servo numerics in ``synth.audio``.

The output callback (``out_cb``) resamples the input ring at a continuously
servo'd ratio to hold the ring depth near TARGET_SECONDS. These tests feed the
callback synthetic ring-buffer states and assert the ratio moves toward the
target, clamps sanely, and that the underrun path HOLDS the read position
(emits silence) rather than replaying stale samples.

The callback is driven directly (not through ``FakeSounddevice.pump``) so the
ring state — ``_w`` (samples written), ``_rf`` (fractional read head),
``_integ`` (learned drift) — can be set precisely for each scenario.
"""

from __future__ import annotations

import math
import sys

import numpy as np
import pytest

from synth.audio import RATIO_MAX, RING_SECONDS, TARGET_SECONDS
from tests.fakes import FakeSounddevice

SR = 44100  # the S-1's native samplerate in the fake device table
TARGET = int(TARGET_SECONDS * SR)
CAP = int(RING_SECONDS * SR)


@pytest.fixture
def fake_sd(monkeypatch):
    fake = FakeSounddevice()
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    monkeypatch.setattr("synth.audio.PREFILL_SECONDS", 0.0)
    return fake


@pytest.fixture
def running(fake_sd):
    """A started monitor plus the live output callback, torn down after."""
    from synth.audio import AudioMonitor

    m = AudioMonitor()
    s1 = fake_sd.add_s1()
    m.start(s1, 1)
    out_stream = fake_sd.output_streams[-1]
    yield m, out_stream
    m.stop()


def _set_ring(m, *, w, rf, integ=0.0, fill=1.0):
    """Overwrite the servo ring state under the lock."""
    with m._block:
        m._buf[:] = fill
        m._w = w
        m._rf = float(rf)
        m._integ = float(integ)


def _drive(out_stream, frames=512):
    """Fire the output callback once; return the block it wrote."""
    buf = np.zeros((frames, out_stream.channels), dtype=np.float32)
    out_stream.callback(buf, frames, None, None)
    return buf


def _effective_ratio(m, rf_before, frames):
    """out_cb advances _rf by exactly frames*ratio; recover that ratio."""
    return (m._rf - rf_before) / frames


# ── ratio servo direction ────────────────────────────────────
class TestRatioDirection:
    def test_overfull_reads_faster(self, running):
        m, out = running
        _set_ring(m, w=TARGET * 4, rf=0.0)  # depth well above target
        rf0 = m._rf
        _drive(out, 512)
        ratio = _effective_ratio(m, rf0, 512)
        assert ratio > 1.0            # drains the ring toward target
        assert m._integ > 0.0         # integrator learned "too full"

    def test_underfilled_reads_slower(self, running):
        m, out = running
        # depth below target but above the underrun threshold
        depth = TARGET - 300
        _set_ring(m, w=depth, rf=0.0)
        rf0 = m._rf
        _drive(out, 512)
        ratio = _effective_ratio(m, rf0, 512)
        assert ratio < 1.0            # slows the reader to refill
        assert m._integ < 0.0

    def test_at_target_stays_near_unity(self, running):
        m, out = running
        _set_ring(m, w=TARGET, rf=0.0, integ=0.0)  # err == 0
        rf0 = m._rf
        _drive(out, 512)
        ratio = _effective_ratio(m, rf0, 512)
        assert ratio == pytest.approx(1.0, abs=1e-4)
        assert m._integ == pytest.approx(0.0, abs=1e-9)


# ── clamps ───────────────────────────────────────────────────
class TestClamps:
    def test_ratio_clamped_high(self, running):
        m, out = running
        _set_ring(m, w=TARGET * 4, rf=0.0)
        rf0 = m._rf
        _drive(out, 512)
        ratio = _effective_ratio(m, rf0, 512)
        assert ratio <= 1.0 + RATIO_MAX + 1e-6

    def test_integrator_clamped_high(self, running):
        m, out = running
        # pre-load the integrator past its ceiling; one step must clamp it
        _set_ring(m, w=TARGET, rf=0.0, integ=1.0)
        _drive(out, 512)
        assert m._integ == pytest.approx(RATIO_MAX)

    def test_integrator_clamped_low(self, running):
        m, out = running
        _set_ring(m, w=TARGET, rf=0.0, integ=-1.0)
        _drive(out, 512)
        assert m._integ == pytest.approx(-RATIO_MAX)

    def test_ratio_never_extreme(self, running):
        m, out = running
        # absurd depth: ratio must still land inside the authority band
        _set_ring(m, w=10_000_000, rf=0.0)
        rf0 = m._rf
        _drive(out, 512)
        ratio = _effective_ratio(m, rf0, 512)
        assert 1.0 - RATIO_MAX - 1e-6 <= ratio <= 1.0 + RATIO_MAX + 1e-6


# ── underrun holds position ──────────────────────────────────
class TestUnderrun:
    def test_underrun_emits_silence_and_holds(self, running):
        m, out = running
        _set_ring(m, w=100, rf=0.0, fill=0.9)  # depth 100 << frames
        rf0 = m._rf
        before = m.underruns
        buf = _drive(out, 512)
        assert m.underruns == before + 1
        assert not np.any(buf)          # pure silence, no stale replay
        assert m._rf == rf0             # read head held, not advanced

    def test_recovers_after_burst_lands(self, running):
        m, out = running
        _set_ring(m, w=100, rf=0.0, fill=0.5)
        _drive(out, 512)                # underruns, holds at rf=0
        assert m.underruns == 1
        # delayed burst lands: depth now healthy, reader resumes from held rf
        with m._block:
            m._w = TARGET * 3
        rf0 = m._rf
        buf = _drive(out, 512)
        assert m._rf > rf0              # resumed reading
        assert np.any(buf)             # real audio out now


# ── signal path through the resampler ────────────────────────
class TestSignalPath:
    def test_constant_ring_reads_constant(self, running):
        m, out = running
        _set_ring(m, w=TARGET * 4, rf=0.0, fill=0.7)
        buf = _drive(out, 512)
        # linear interpolation of a constant buffer is that constant (gain 1)
        assert buf == pytest.approx(0.7, abs=1e-4)

    def test_gain_and_clip_applied(self, running):
        m, out = running
        m.gain = 10.0
        _set_ring(m, w=TARGET * 4, rf=0.0, fill=0.9)
        buf = _drive(out, 512)
        assert buf.max() <= 1.0         # clipped, never exceeds full scale
        assert buf.min() >= -1.0


# ── meters on known inputs (incl. the silence floor) ─────────
class TestMeters:
    def test_peak_db_known(self):
        from synth.audio import AudioMonitor

        m = AudioMonitor()
        m.peak = 0.5
        assert m.peak_db == pytest.approx(-6.0, abs=0.1)

    def test_rms_db_known(self):
        from synth.audio import AudioMonitor

        m = AudioMonitor()
        m.rms = 0.5
        assert m.rms_db == pytest.approx(-6.0, abs=0.1)

    def test_silence_hits_floor_not_neg_inf(self):
        from synth.audio import AudioMonitor

        m = AudioMonitor()
        m.peak = 0.0
        m.rms = 0.0
        assert m.peak_db == -120.0      # documented floor
        assert m.rms_db == -120.0
        assert math.isfinite(m.peak_db)
        assert math.isfinite(m.rms_db)

    def test_full_scale_is_zero_db(self):
        from synth.audio import AudioMonitor

        m = AudioMonitor()
        m.peak = 1.0
        assert m.peak_db == pytest.approx(0.0, abs=0.05)


# ── healthy reflects stream liveness ─────────────────────────
class TestHealthy:
    def test_unhealthy_before_start(self):
        from synth.audio import AudioMonitor

        assert AudioMonitor().healthy is False

    def test_healthy_while_running(self, running):
        m, _out = running
        assert m.healthy is True

    def test_unhealthy_when_stream_inactive(self, running, fake_sd):
        m, _out = running
        fake_sd.output_streams[-1].active = False
        assert m.healthy is False
