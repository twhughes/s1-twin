"""M2 — the pattern bridge: PRM <-> standard MIDI file (contracts C2/C3)."""

from pathlib import Path

import mido

from synth.engine import DEFAULT_SYNTH_CHANNEL
from synth.prm import PrmFile, load_template
from synth.prm_cli import main
from synth.sequence import Note, Sequence, load_midi, save_midi


# ── C3 round-trip law ────────────────────────────────────────
def test_c3_roundtrip_reproduces_notes_bpm_resolution(tmp_path):
    seq = Sequence(
        notes=[
            Note(step=0, pitch=48, velocity=100, duration=2),
            Note(step=0, pitch=60, velocity=90, duration=1),
            Note(step=3, pitch=55, velocity=110, duration=4),
        ],
        steps=16,
        bpm=98.5,
        step_resolution="1/16",
    )
    path = tmp_path / "roundtrip.mid"
    save_midi(seq, path)
    loaded = load_midi(path)

    assert abs(loaded.bpm - 98.5) < 0.1
    assert loaded.step_resolution == "1/16"
    assert sorted((n.step, n.pitch, n.velocity, n.duration) for n in loaded.notes) == [
        (0, 48, 100, 2), (0, 60, 90, 1), (3, 55, 110, 4),
    ]


def test_c3_roundtrip_survives_channel_and_motion(tmp_path):
    """Notes still round-trip when CC events and a non-zero channel are present."""
    seq = Sequence(notes=[Note(step=2, pitch=64, velocity=77, duration=1)], steps=16, bpm=120.0)
    path = tmp_path / "with_cc.mid"
    save_midi(seq, path, cc_events=[(0, 74, 100), (2, 74, 40)], channel=2)
    loaded = load_midi(path)
    assert len(loaded.notes) == 1
    n = loaded.notes[0]
    assert (n.step, n.pitch, n.velocity, n.duration) == (2, 64, 77, 1)


# ── motion-lane fixture ──────────────────────────────────────
def _pattern_with_motion() -> PrmFile:
    prm = load_template()
    prm.set("LENG", 4)
    prm.set("MOTION_CC1", 74)  # lane 1 records the cutoff knob
    for step_no, pitch, motion in ((1, 60, 100), (3, 64, 40)):
        st = prm.step(step_no)
        st.notes[0] = pitch
        st.velocities[0] = 100
        st.lengths[0] = 24
        st.motion = {1: motion}
        prm.set_step(step_no, st)
    return prm


def test_motion_lanes_and_events():
    prm = _pattern_with_motion()
    assert prm.motion_lanes() == {1: 74}
    # (step_index, cc, value), sorted; only assigned lanes with data.
    assert prm.to_motion_events() == [(0, 74, 100), (2, 74, 40)]


def test_unassigned_lanes_carry_no_data():
    prm = load_template()  # all MOTION_CC<n> = -1 in the device init dump
    assert prm.motion_lanes() == {}
    assert prm.to_motion_events() == []


def test_export_writes_cc_events_on_synth_channel(tmp_path):
    src = tmp_path / "S1_PTN1-01.PRM"
    _pattern_with_motion().save(src)

    assert main(["export-mid", str(src)]) == 0
    out = tmp_path / "S1_PTN1-01.mid"
    assert out.exists()

    mid = mido.MidiFile(str(out))
    ccs = [m for track in mid.tracks for m in track if m.type == "control_change"]
    assert len(ccs) == 2
    assert all(m.control == 74 for m in ccs)
    assert all(m.channel == DEFAULT_SYNTH_CHANNEL for m in ccs)  # default channel 3 -> 0-indexed 2
    assert [m.value for m in ccs] == [100, 40]

    # CC events sit at step-boundary ticks: step 0 (t=0) and step 2 (t=240 at 1/16).
    step_ticks = mid.ticks_per_beat // 4  # 480 // 4 = 120 per 1/16 step
    abs_ticks, t = [], 0
    for m in mid.tracks[0]:
        t += m.time
        if m.type == "control_change":
            abs_ticks.append(t)
    assert abs_ticks == [0, 2 * step_ticks]


def test_export_channel_flag_overrides(tmp_path):
    src = tmp_path / "S1_PTN1-02.PRM"
    _pattern_with_motion().save(src)
    assert main(["export-mid", str(src), str(tmp_path / "ch10.mid"), "--channel", "10"]) == 0
    mid = mido.MidiFile(str(tmp_path / "ch10.mid"))
    ccs = [m for track in mid.tracks for m in track if m.type == "control_change"]
    assert all(m.channel == 9 for m in ccs)  # 1-indexed 10 -> 0-indexed 9


# ── import: RESTORE-ready PRM + no silent caps ───────────────
def _write_midi(path: Path, notes: list[tuple[int, int, int]], bpm: float = 120.0) -> None:
    """notes: (pitch, start_tick, duration_tick) at 480 PPQN."""
    mid = mido.MidiFile(ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm), time=0))
    events = []
    for pitch, start, dur in notes:
        events.append((start, 0, "note_on", pitch, 100))
        events.append((start + dur, 1, "note_off", pitch, 0))
    events.sort(key=lambda e: (e[0], -e[1]))
    prev = 0
    for tick, _o, typ, pitch, vel in events:
        track.append(mido.Message(typ, note=pitch, velocity=vel, time=tick - prev))
        prev = tick
    track.append(mido.MetaMessage("end_of_track", time=0))
    mid.save(str(path))


def test_import_writes_restore_ready_prm(tmp_path):
    src = tmp_path / "phrase.mid"
    _write_midi(src, [(60, 0, 120), (64, 480, 240)], bpm=130.0)

    assert main(["import-mid", str(src), "--slot", "2-05", "--out", str(tmp_path)]) == 0
    out = tmp_path / "S1_PTN2-05.PRM"
    assert out.exists()

    # RESTORE-ready: parses as a real .PRM and reproduces the notes it imported.
    prm = PrmFile.parse(out.read_text())
    seq = prm.to_sequence()
    assert abs(seq.bpm - 130.0) < 0.1
    got = sorted((n.step, n.pitch) for n in seq.notes)
    assert got == [(0, 60), (4, 64)]
    assert prm.step(1) is not None and prm.step(64) is not None  # full 64-step file


def test_import_reports_poly_overflow(tmp_path, capsys):
    src = tmp_path / "chord.mid"
    # 5 notes stacked on step 0 -> exceeds the 4-per-step ceiling.
    _write_midi(src, [(60 + i, 0, 120) for i in range(5)])
    assert main(["import-mid", str(src), "--slot", "1-01", "--out", str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "more than 4 notes" in err
    assert "0" in err  # step 0 named


def test_import_reports_dropped_steps(tmp_path, capsys):
    src = tmp_path / "long.mid"
    # A note at step 70 is past the 64-step device limit -> dropped, not silent.
    _write_midi(src, [(60, 0, 120), (67, 120 * 70, 120)])
    assert main(["import-mid", str(src), "--slot", "1-01", "--out", str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "past the 64-step limit" in err
    assert "dropped" in err
