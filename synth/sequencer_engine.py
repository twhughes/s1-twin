"""Threaded sequencer engine that streams notes to the S-1 in real-time."""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from .midi_backend import MidiBackend
from .sequence import Sequence

# The step-advance callback: called with the current step index.
PositionCb = Callable[[int], None]


@runtime_checkable
class SequencerLike(Protocol):
    """The exact surface :class:`~synth.engine.S1Engine` consumes from its
    sequencer — the engine↔sequencer seam, pinned.

    Making the contract explicit (rather than leaning on the concrete
    ``SequencerEngine``) lets tests inject a fake and keeps the engine honest
    about what it actually touches: the clock-out toggle, the transport-status
    reads, ``stop()`` on shutdown, and the position callback. Any object with
    these members satisfies it structurally — ``SequencerEngine`` does.
    """

    clock_enabled: bool

    @property
    def playing(self) -> bool: ...

    @property
    def paused(self) -> bool: ...

    @property
    def position(self) -> int: ...

    @property
    def sequence(self) -> Sequence: ...

    def stop(self) -> None: ...

    def set_position_callback(self, callback: PositionCb) -> None: ...


class SequencerEngine:
    """Plays a Sequence by sending MIDI note-on/off in a background thread.

    Performance controls (set from the UI, read live by the playback thread):

    - ``gate``: fraction of each note's duration actually held (0-1].
    - ``shuffle``: swing — even-numbered steps (1-indexed) are delayed by this
      fraction of a step (0-1).
    - ``last_step``: truncate the pattern to this many steps (None = full).
    - ``probability``: chance each step's notes fire (0-1).
    - ``clock_enabled``: emit MIDI clock (24 ticks per quarter note) while
      playing, so the device's delay/LFO tempo-sync follows the app.
    """

    def __init__(
        self,
        midi: MidiBackend,
        sequence: Sequence | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._midi = midi
        self._sequence = sequence or Sequence()
        self._playing = False
        self._paused = False
        self._position = 0  # current step
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # (pitch, off_time) for notes currently sounding; off_time is
        # time.monotonic()-based so gate fractions and loop wrap-around need
        # no special casing. Guarded by _active_lock — pause() touches it
        # from the UI thread while _run() owns playback.
        self._active: list[tuple[int, float]] = []
        self._active_lock = threading.Lock()
        self._position_callback: PositionCb | None = None
        self.gate: float = 1.0
        self.shuffle: float = 0.0
        self.last_step: int | None = None
        self.probability: float = 1.0
        self.clock_enabled: bool = True
        # MIDI clock scheduling (playback thread only).
        self._next_clock: float = 0.0
        self._rng = rng or random.Random()

    @property
    def sequence(self) -> Sequence:
        return self._sequence

    @sequence.setter
    def sequence(self, seq: Sequence) -> None:
        was_playing = self._playing
        if was_playing:
            self.stop()
        self._sequence = seq
        self._position = 0

    @property
    def playing(self) -> bool:
        return self._playing and not self._paused

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def position(self) -> int:
        return self._position

    def set_position_callback(self, callback: PositionCb) -> None:
        """Set a callback invoked with (step: int) on each step advance."""
        self._position_callback = callback

    def play(self) -> None:
        """Start or resume playback in a background thread."""
        if self._playing and self._paused:
            # Resume from pause
            self._paused = False
            self._midi.send_continue()
            return

        if self._playing:
            return  # already playing

        self._playing = True
        self._paused = False
        self._stop_event.clear()
        self._position = 0
        self._midi.send_start()

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop playback and reset position."""
        self._playing = False
        self._paused = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._all_notes_off()
        self._midi.send_stop()
        self._position = 0

    def pause(self) -> None:
        """Pause playback without resetting position."""
        if self._playing and not self._paused:
            self._paused = True
            self._all_notes_off()
            self._midi.send_stop()

    def all_notes_off(self) -> None:
        """Panic: turn off everything this engine has sounding."""
        self._all_notes_off()

    def _all_notes_off(self) -> None:
        """Send note-off for all currently sounding notes."""
        with self._active_lock:
            pitches = {pitch for pitch, _ in self._active}
            self._active.clear()
        for pitch in pitches:
            self._midi.send_note_off(pitch)

    def _effective_steps(self) -> int:
        steps = self._sequence.steps
        if self.last_step is not None:
            steps = min(steps, self.last_step)
        return max(1, steps)

    def _run(self) -> None:
        """Main playback loop running in a background thread."""
        try:
            seq = self._sequence
            if not seq.notes or seq.steps == 0:
                return

            next_tick = time.monotonic()
            self._next_clock = next_tick
            while not self._stop_event.is_set():
                if self._paused:
                    # Spin-wait while paused; restart the clock on resume
                    time.sleep(0.01)
                    next_tick = time.monotonic()
                    self._next_clock = next_tick
                    continue

                step = self._position
                step_duration = self._step_duration_seconds()
                # Snapshot: the UI thread edits the live note list mid-play
                notes = list(seq.notes)

                # Swing: delay even-numbered steps (1-indexed → odd index)
                if self.shuffle > 0.0 and step % 2 == 1:
                    self._wait_until(time.monotonic() + self.shuffle * step_duration)
                    if self._stop_event.is_set():
                        break

                connected_before = self._midi.connected
                now = time.monotonic()
                self._flush_note_offs(now)

                if self._rng.random() < self.probability:
                    for note in notes:
                        if note.step != step:
                            continue
                        self._midi.send_note_on(note.pitch, note.velocity)
                        held = max(0.05, note.duration * self.gate)
                        with self._active_lock:
                            self._active.append((note.pitch, now + held * step_duration))

                if connected_before and not self._midi.connected:
                    # Device vanished mid-send — stop cleanly
                    break

                # Notify UI of position change
                if self._position_callback is not None:
                    try:
                        self._position_callback(step)
                    except Exception:
                        pass

                # Absolute-clock scheduling: no per-step processing drift
                next_tick += step_duration
                self._wait_until(next_tick)
                if self._stop_event.is_set():
                    break

                # Advance position, loop at end
                self._position = (step + 1) % self._effective_steps()
        except Exception:
            # The real MidiBackend never raises from sends, but a broken
            # backend or callback must not leave _playing set or notes stuck.
            pass
        finally:
            try:
                self._all_notes_off()
            except Exception:
                pass
            self._playing = False

    def _wait_until(self, deadline: float) -> None:
        """Sleep until *deadline*, waking early for due note-offs and clock ticks."""
        while not self._stop_event.is_set():
            now = time.monotonic()
            self._flush_note_offs(now)
            self._flush_clock(now)
            if now >= deadline:
                return
            target = deadline
            with self._active_lock:
                for _, off_time in self._active:
                    if off_time < target:
                        target = off_time
            if self.clock_enabled and self._next_clock < target:
                target = self._next_clock
            remaining = target - now
            if remaining > 0:
                self._stop_event.wait(remaining)

    def _clock_period_seconds(self) -> float:
        """One MIDI clock tick: 24 per quarter note at the sequence BPM."""
        bpm = max(self._sequence.bpm, 1.0)
        return 60.0 / (bpm * 24.0)

    def _flush_clock(self, now: float) -> None:
        """Emit any due MIDI clock ticks (absolute-clock scheduled)."""
        if not self.clock_enabled or self._paused:
            return
        period = self._clock_period_seconds()
        while self._next_clock <= now and not self._stop_event.is_set():
            self._midi.send_clock()
            self._next_clock += period

    def _flush_note_offs(self, now: float) -> None:
        """Send note-off for notes whose hold time has expired.

        A pitch still held by a later, overlapping note is not turned off —
        its expiry is dropped silently so the sustaining note keeps sounding.
        """
        with self._active_lock:
            due = {pitch for pitch, off_time in self._active if off_time <= now}
            if not due:
                return
            self._active = [(p, t) for p, t in self._active if t > now]
            still_held = {pitch for pitch, _ in self._active}
        for pitch in due:
            if pitch not in still_held:
                self._midi.send_note_off(pitch)

    def _step_duration_seconds(self) -> float:
        """Calculate the duration of one step in seconds from BPM and resolution."""
        seq = self._sequence
        return step_duration_seconds(seq.bpm, seq.step_resolution)


def step_duration_seconds(bpm: float, resolution: str) -> float:
    """Duration of one step in seconds for a BPM and resolution like '1/16'."""
    from .sequence import parse_resolution

    numerator, denominator = parse_resolution(resolution)
    # beats per second
    bps = bpm / 60.0
    # steps per beat: e.g., 1/16 = 4 steps per beat
    steps_per_beat = denominator / (4 * numerator)
    return 1.0 / (bps * steps_per_beat)
