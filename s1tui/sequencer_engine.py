"""Threaded sequencer engine that streams notes to the S-1 in real-time."""

from __future__ import annotations

import threading
import time

from .midi_backend import MidiBackend
from .sequence import Note, Sequence


class SequencerEngine:
    """Plays a Sequence by sending MIDI note-on/off in a background thread."""

    def __init__(self, midi: MidiBackend, sequence: Sequence | None = None) -> None:
        self._midi = midi
        self._sequence = sequence or Sequence()
        self._playing = False
        self._paused = False
        self._position = 0  # current step
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._active_notes: set[int] = set()  # pitches currently sounding
        self._position_callback: callable | None = None

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

    def set_position_callback(self, callback: callable) -> None:
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

    def _all_notes_off(self) -> None:
        """Send note-off for all currently sounding notes."""
        for pitch in list(self._active_notes):
            self._midi.send_note_off(pitch)
        self._active_notes.clear()

    def _run(self) -> None:
        """Main playback loop running in a background thread."""
        seq = self._sequence
        if not seq.notes or seq.steps == 0:
            self._playing = False
            return

        step_duration = self._step_duration_seconds()

        while not self._stop_event.is_set():
            if self._paused:
                # Spin-wait while paused
                time.sleep(0.01)
                continue

            step = self._position

            # Turn off notes that should end on this step
            self._process_note_offs(step)

            # Turn on notes that start on this step
            for note in seq.notes_at_step(step):
                self._midi.send_note_on(note.pitch, note.velocity)
                self._active_notes.add(note.pitch)

            # Notify UI of position change
            if self._position_callback is not None:
                try:
                    self._position_callback(step)
                except Exception:
                    pass

            # Wait for next step
            self._stop_event.wait(step_duration)
            if self._stop_event.is_set():
                break

            # Advance position, loop at end
            self._position = (step + 1) % seq.steps

        self._all_notes_off()
        self._playing = False

    def _process_note_offs(self, current_step: int) -> None:
        """Send note-off for notes whose duration has expired."""
        seq = self._sequence
        for note in seq.notes:
            note_end = note.step + note.duration
            # Note should turn off at this step
            if note_end == current_step and note.pitch in self._active_notes:
                self._midi.send_note_off(note.pitch)
                self._active_notes.discard(note.pitch)
            # Handle wrap-around for looping
            if note_end >= seq.steps:
                wrapped_end = note_end % seq.steps
                if wrapped_end == current_step and note.pitch in self._active_notes:
                    self._midi.send_note_off(note.pitch)
                    self._active_notes.discard(note.pitch)

    def _step_duration_seconds(self) -> float:
        """Calculate the duration of one step in seconds from BPM and resolution."""
        seq = self._sequence
        # Parse resolution like "1/16"
        parts = seq.step_resolution.split("/")
        if len(parts) == 2:
            numerator, denominator = int(parts[0]), int(parts[1])
        else:
            numerator, denominator = 1, 4  # quarter note fallback

        # beats per second
        bps = seq.bpm / 60.0
        # steps per beat: e.g., 1/16 = 4 steps per beat
        steps_per_beat = denominator / (4 * numerator)
        return 1.0 / (bps * steps_per_beat)
