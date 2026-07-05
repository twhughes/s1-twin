"""Drive the S-1 and capture the result: apply CC values, play a probe note,
record the audio it produces.

This is the only hardware-touching piece of the engine. It bridges the existing
:class:`~s1tui.midi_backend.MidiBackend` with the audio :mod:`~s1tui.match.capture`
layer, plus a latency calibration so recordings line up with note-on.
"""

from __future__ import annotations

import time

from ..midi_backend import MidiBackend
from . import WORKING_SR
from .capture import AudioClip, find_onset, prepare

PROBE_NOTE = 48  # C3 — the S-1 is a bass synth
SETTLE_S = 0.05  # let CC changes apply before triggering

# A bright, immediate patch for latency calibration. Whatever sound is loaded
# on the synth may have a slow attack or a closed filter/VCA, which would make
# onset detection fire late (or on noise) and misalign every probe after it.
CALIBRATION_PATCH: dict[int, int] = {
    74: 127,  # filter fully open
    71: 0,    # no resonance
    73: 0,    # instant attack
    30: 127,  # full sustain
    72: 0,    # no release tail
    28: 0,    # amp env: Gate — audio starts the instant the note lands
    20: 127,  # saw fully up
    19: 0,    # square off
    21: 0,    # sub off
    23: 0,    # noise off
    24: 0,    # filter env depth off
    25: 0,    # filter LFO depth off
    13: 0,    # LFO pitch off
    17: 0,    # LFO mod depth off
    92: 0,    # delay dry
    91: 0,    # reverb dry
    11: 127,  # expression full
}


class SynthDriver:
    """Apply parameters to the S-1 and record single-note probes."""

    def __init__(
        self,
        midi: MidiBackend,
        device: int | str | None,
        note: int = PROBE_NOTE,
        hold_s: float = 1.2,
        tail_s: float = 1.0,
        sr: int = WORKING_SR,
        monitor=None,
    ):
        self.midi = midi
        self.device = device
        self.note = note
        self.hold_s = hold_s
        self.tail_s = tail_s
        self.sr = sr
        # Optional AudioMonitor. If running, probes pull from its live stream
        # instead of opening a competing recording on the same input device.
        self.monitor = monitor
        self.latency_s = 0.0  # filled in by calibrate()

    @property
    def _use_monitor(self) -> bool:
        return self.monitor is not None and self.monitor.running

    def apply(self, params: dict[int, int]) -> None:
        for cc, value in params.items():
            self.midi.send_cc(cc, value)

    def probe(self, params: dict[int, int] | None = None) -> AudioClip:
        """Apply ``params`` (if given), play the probe note, return the recording.

        Recording runs in the background while the note is held; the calibrated
        latency offset is trimmed from the front before onset detection.
        """
        if params is not None:
            self.apply(params)
            time.sleep(SETTLE_S)

        if self._use_monitor:
            self.monitor.begin_capture()
            self._play_note()
            clip = self.monitor.end_capture()
        else:
            clip = self._record_window(self.latency_s + self.hold_s + self.tail_s, play=True)

        # Drop the pre-note latency window before analysis.
        skip = int(self.latency_s * self.sr)
        if skip:
            clip = AudioClip(clip.samples[skip:], clip.samplerate)
        return prepare(clip)

    def _play_note(self) -> None:
        self.midi.send_note_on(self.note)
        time.sleep(self.hold_s)
        self.midi.send_note_off(self.note)
        time.sleep(self.tail_s)

    def _record_window(self, duration: float, play: bool) -> AudioClip:
        """Own-stream capture (used when no monitor is running)."""
        import sounddevice as sd

        info = sd.query_devices(self.device, "input")
        rec_sr = int(info["default_samplerate"])
        recording = sd.rec(
            int(duration * rec_sr), samplerate=rec_sr, channels=1, device=self.device, dtype="float32"
        )
        if play:
            self._play_note()
        else:
            self.midi.send_note_on(self.note)
        sd.wait()
        if not play:
            self.midi.send_note_off(self.note)
        return AudioClip(recording.reshape(-1), rec_sr).resample(self.sr)

    def calibrate(self) -> float:
        """Measure MIDI-note -> audio latency (seconds) by detecting first onset.

        Applies :data:`CALIBRATION_PATCH` (bright, instant attack), plays a
        note, and times how long until sound appears — accounting for
        buffering through an audio host like Logic.
        """
        self.apply(CALIBRATION_PATCH)
        time.sleep(SETTLE_S)
        if self._use_monitor:
            self.monitor.begin_capture()
            self.midi.send_note_on(self.note)
            time.sleep(1.0)
            self.midi.send_note_off(self.note)
            clip = self.monitor.end_capture()
        else:
            clip = self._record_window(1.0, play=False)

        onset = find_onset(clip.samples, clip.samplerate)
        self.latency_s = max(0.0, onset / clip.samplerate)
        return self.latency_s
