"""Sequence data model and MIDI file I/O for the piano roll."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import mido

# Device limits (the S-1's .PRM pattern format).
MAX_STEPS = 64
MAX_NOTES_PER_STEP = 4  # 4-note poly per step is the PRM ceiling


@dataclass
class Note:
    """A single note event in the sequence."""

    step: int  # step position (0-indexed, quantized to grid)
    pitch: int  # MIDI note number (0-127)
    velocity: int = 100  # 0-127
    duration: int = 1  # length in steps


@dataclass
class Sequence:
    """A pattern of notes with tempo and grid info."""

    notes: list[Note] = field(default_factory=list)
    steps: int = 16  # total pattern length
    bpm: float = 120.0
    step_resolution: str = "1/16"  # grid quantization
    dropped_notes: int = 0  # notes discarded on import (past max_steps)

    def note_at(self, step: int, pitch: int) -> Note | None:
        """Return the note at a given step and pitch, or None."""
        for note in self.notes:
            if note.step == step and note.pitch == pitch:
                return note
        return None

    def notes_at_step(self, step: int) -> list[Note]:
        """Return all notes starting at a given step."""
        return [n for n in self.notes if n.step == step]

    def toggle_note(self, step: int, pitch: int, velocity: int = 100, duration: int = 1) -> None:
        """Add or remove a note at step/pitch."""
        existing = self.note_at(step, pitch)
        if existing:
            self.notes.remove(existing)
        else:
            self.notes.append(Note(step=step, pitch=pitch, velocity=velocity, duration=duration))

    def clear(self) -> None:
        """Remove all notes."""
        self.notes.clear()

    def poly_warnings(self) -> list[int]:
        """Steps holding more notes than the device can store (PRM: 4/step)."""
        return sorted(
            step for step in {n.step for n in self.notes}
            if len(self.notes_at_step(step)) > MAX_NOTES_PER_STEP
        )


def parse_resolution(resolution: str) -> tuple[int, int]:
    """Parse a resolution string like '1/16' into (numerator, denominator).

    Falls back to a quarter note (1, 4) on anything unparseable.
    """
    parts = resolution.split("/")
    if len(parts) == 2:
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            return 1, 4
    return 1, 4


def _resolution_ticks(resolution: str, ticks_per_beat: int) -> int:
    """Convert a resolution string like '1/16' to ticks."""
    numerator, denominator = parse_resolution(resolution)
    # 1/4 = 1 beat, 1/8 = half beat, 1/16 = quarter beat
    return int(ticks_per_beat * 4 * numerator / denominator)


def load_midi(path: Path, quantize: str = "1/16", max_steps: int = 64) -> Sequence:
    """Parse a MIDI file and return a Sequence with quantized notes.

    Reads the first track with note events. Tempo is extracted from
    the file if present, defaulting to 120 BPM.
    """
    mid = mido.MidiFile(str(path))
    ticks_per_beat = mid.ticks_per_beat
    step_ticks = _resolution_ticks(quantize, ticks_per_beat)

    # Extract tempo from meta messages
    bpm = 120.0
    for track in mid.tracks:
        for msg in track:
            if msg.type == "set_tempo":
                bpm = mido.tempo2bpm(msg.tempo)
                break

    # Collect note-on/off pairs from all tracks. Pending on-events are stacked
    # per pitch so overlapping same-pitch notes within a track don't clobber
    # each other (first-on pairs with first-off).
    notes: list[Note] = []
    dropped = 0
    for track in mid.tracks:
        abs_time = 0
        pending: dict[int, list[tuple[int, int]]] = {}  # pitch -> [(start_tick, velocity)]
        for msg in track:
            abs_time += msg.time
            if msg.type == "note_on" and msg.velocity > 0:
                pending.setdefault(msg.note, []).append((abs_time, msg.velocity))
            elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                stack = pending.get(msg.note)
                if stack:
                    start_tick, velocity = stack.pop(0)
                    dur_ticks = abs_time - start_tick
                    step = round(start_tick / step_ticks)
                    duration = max(1, round(dur_ticks / step_ticks))
                    if step < max_steps:
                        notes.append(Note(
                            step=step,
                            pitch=msg.note,
                            velocity=velocity,
                            duration=duration,
                        ))
                    else:
                        dropped += 1

    # Determine pattern length
    if notes:
        last_end = max(n.step + n.duration for n in notes)
        # Round up to nearest multiple of 16
        steps = min(max_steps, max(16, ((last_end + 15) // 16) * 16))
    else:
        steps = 16

    return Sequence(
        notes=notes, steps=steps, bpm=bpm, step_resolution=quantize, dropped_notes=dropped
    )


def save_midi(seq: Sequence, path: Path) -> None:
    """Export a Sequence as a standard MIDI file."""
    ticks_per_beat = 480
    step_ticks = _resolution_ticks(seq.step_resolution, ticks_per_beat)

    mid = mido.MidiFile(ticks_per_beat=ticks_per_beat)
    track = mido.MidiTrack()
    mid.tracks.append(track)

    # Tempo meta message
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(seq.bpm), time=0))

    # Build a list of (abs_tick, type, pitch, velocity) events
    events: list[tuple[int, str, int, int]] = []
    for note in seq.notes:
        on_tick = note.step * step_ticks
        off_tick = on_tick + note.duration * step_ticks
        events.append((on_tick, "note_on", note.pitch, note.velocity))
        events.append((off_tick, "note_off", note.pitch, 0))

    # Sort by tick, with note_off before note_on at same tick
    events.sort(key=lambda e: (e[0], 0 if e[1] == "note_off" else 1))

    # Convert to delta times
    prev_tick = 0
    for tick, msg_type, pitch, vel in events:
        delta = tick - prev_tick
        track.append(mido.Message(msg_type, note=pitch, velocity=vel, time=delta))
        prev_tick = tick

    # End of track
    track.append(mido.MetaMessage("end_of_track", time=0))

    mid.save(str(path))
