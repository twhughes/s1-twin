"""Tests for s1tui.prm — parse/serialize fidelity against a real device dump.

The bundled template (s1tui/data/init_pattern.prm) is a real S-1 disk-mode
backup. If you've backed up your own device, drop .PRM files into
~/.s1tui/backups/ and they are round-trip-tested here automatically.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from s1tui.prm import (
    CC_PRM_KEYS,
    PRM_CC_KEYS,
    TEMPLATE_PATH,
    PrmFile,
    PrmParseError,
    PrmStep,
    build_pattern,
    cc_to_prm,
    find_s1_volume,
    load_template,
    pattern_filename,
    prm_to_cc,
    ticks_per_step,
    write_to_device,
)
from s1tui.schema import S1_PARAMS, param_by_cc
from s1tui.sequence import Note, Sequence

EXTRA_BACKUPS = sorted((Path.home() / ".s1tui" / "backups").glob("**/*.PRM")) + sorted(
    (Path.home() / ".s1tui" / "backups").glob("**/*.prm")
)
REAL_DUMPS = [TEMPLATE_PATH] + EXTRA_BACKUPS


# ── round-trip fidelity against real device dumps ────────────
class TestRealBackupRoundTrip:
    @pytest.mark.parametrize("path", REAL_DUMPS, ids=lambda p: p.name)
    def test_parse_serialize_byte_identical(self, path):
        raw = path.read_text(encoding="ascii", errors="replace")
        assert PrmFile.parse(raw).serialize() == raw

    @pytest.mark.parametrize("path", REAL_DUMPS, ids=lambda p: p.name)
    def test_semantic_roundtrip_survives_rewrite(self, path):
        """Apply the file's own values back onto itself: nothing may change."""
        prm = PrmFile.load(path)
        before = prm.serialize()
        prm.apply_cc_values(prm.to_cc_values())
        seq = prm.to_sequence()
        prm.set("LENG", seq.steps)
        assert prm.to_cc_values() == PrmFile.parse(before).to_cc_values()
        assert prm.to_sequence().steps == seq.steps

    def test_template_is_a_real_dump(self):
        prm = load_template()
        assert prm.get_int("TEMPO") == 10000  # 100.00 BPM, device-authored
        assert prm.step(64) is not None       # all 64 step lines present

    def test_template_matches_schema_defaults(self):
        """G2's defaults came from this device dump — they must agree."""
        for cc, value in load_template().to_cc_values().items():
            assert value == param_by_cc(cc).default, f"CC {cc}"


# ── scaling ──────────────────────────────────────────────────
class TestScaling:
    def test_every_cc_scaling_is_invertible(self):
        for cc in CC_PRM_KEYS:
            param = param_by_cc(cc)
            for value in range(param.min_val, param.max_val + 1):
                if param.control_type.value == "switch" and value not in (0, 127):
                    continue
                assert prm_to_cc(cc, cc_to_prm(cc, value)) == value, (cc, value)

    def test_known_pairs_from_format_doc(self):
        assert prm_to_cc(74, 255) == 127     # cutoff init
        assert cc_to_prm(74, 127) == 255
        assert prm_to_cc(76, 128) == 64      # fine tune center
        assert prm_to_cc(102, 7) == 3        # anchored draw multiply floor
        assert cc_to_prm(102, 3) == 7
        assert prm_to_cc(85, 12) == 76       # chord shift +12 semitones
        assert cc_to_prm(85, 76) == 12
        assert prm_to_cc(85, -64) == 0
        assert prm_to_cc(81, 1) == 127       # flag
        assert prm_to_cc(81, 0) == 0
        assert cc_to_prm(81, 127) == 1

    def test_all_54_ccs_covered_except_runtime_controls(self):
        mapped = set(CC_PRM_KEYS)
        all_ccs = {p.cc for p in S1_PARAMS}
        # Mod wheel, expression, damper are runtime performance state — the
        # device does not store them in pattern files.
        assert all_ccs - mapped == {1, 11, 64}
        assert PRM_CC_KEYS  # sanity: forward map non-empty


# ── step encoding ────────────────────────────────────────────
class TestStepEncoding:
    def test_parse_serialize_step(self):
        raw = ("NOTE1=60 VELO1=100 LENG1=24 NOTE2=-1 VELO2=0 LENG2=0 "
               "NOTE3=-1 VELO3=0 LENG3=0 NOTE4=-1 VELO4=0 LENG4=0 SUBSTEP=0 PROB=10")
        step = PrmStep.parse(raw)
        assert step.active_notes() == [(60, 100, 24)]
        assert step.serialize() == raw

    def test_four_note_poly(self):
        step = PrmStep(notes=[60, 64, 67, 71], velocities=[100] * 4, lengths=[24] * 4)
        assert len(step.active_notes()) == 4

    def test_ticks_per_step(self):
        assert ticks_per_step("1/16") == 24
        assert ticks_per_step("1/8") == 48
        assert ticks_per_step("1/4") == 96
        assert ticks_per_step("16t") == 16
        assert ticks_per_step("unknown") == 24


# ── building a pattern (the exporter) ────────────────────────
class TestBuildPattern:
    def make_sequence(self):
        return Sequence(
            notes=[
                Note(step=0, pitch=48, velocity=100, duration=2),
                Note(step=0, pitch=60, velocity=90, duration=1),
                Note(step=3, pitch=55, velocity=110, duration=4),
            ],
            steps=8,
            bpm=98.5,
            step_resolution="1/16",
        )

    def test_patch_and_sequence_roundtrip(self):
        cc_values = {74: 90, 71: 64, 73: 40, 92: 70, 89: 120, 12: 4, 80: 0, 85: 52}
        prm = build_pattern(cc_values, self.make_sequence())
        reparsed = PrmFile.parse(prm.serialize())

        got = reparsed.to_cc_values()
        for cc, value in cc_values.items():
            assert got[cc] == value, f"CC {cc}"

        seq = reparsed.to_sequence()
        assert seq.steps == 8
        assert seq.bpm == 98.5
        assert seq.step_resolution == "1/16"
        assert sorted((n.step, n.pitch, n.velocity, n.duration) for n in seq.notes) == [
            (0, 48, 100, 2), (0, 60, 90, 1), (3, 55, 110, 4),
        ]

    def test_leng_ticks_encoding(self):
        prm = build_pattern({}, self.make_sequence())
        step1 = prm.step(1)
        assert sorted(step1.active_notes()) == [(48, 100, 48), (60, 90, 24)]
        step4 = prm.step(4)
        assert step4.active_notes() == [(55, 110, 96)]

    def test_steps_beyond_length_cleared(self):
        prm = build_pattern({}, self.make_sequence())
        assert prm.get_int("LENG") == 8
        for n in range(9, 65):
            assert prm.step(n).active_notes() == []

    def test_tempo_written_x100(self):
        prm = build_pattern({}, self.make_sequence())
        assert prm.get_int("TEMPO") == 9850

    def test_truncates_over_poly_and_reports(self):
        seq = Sequence(
            notes=[Note(step=0, pitch=50 + i) for i in range(6)],
            steps=4, bpm=120.0,
        )
        prm = load_template()
        truncated = prm.apply_sequence(seq)
        assert truncated == [0]
        assert len(prm.step(1).active_notes()) == 4

    def test_scale_from_resolution(self):
        seq = self.make_sequence()
        seq.step_resolution = "1/8"
        prm = build_pattern({}, seq)
        assert prm.get_int("SCALE") == 0
        assert prm.to_sequence().step_resolution == "1/8"

    def test_unknown_ccs_reported_skipped(self):
        prm = load_template()
        skipped = prm.apply_cc_values({1: 64, 74: 100})  # mod wheel has no PRM key
        assert skipped == [1]
        assert prm.to_cc_values()[74] == 100

    def test_writer_never_adds_novel_keys(self):
        prm = build_pattern({10: 30, 74: 90})  # PAN's key absent from the init dump
        assert not prm.has("PAN")
        keys_before = set(load_template().keys())
        assert set(prm.keys()) == keys_before


# ── file naming / device volume ──────────────────────────────
class TestDeviceWrite:
    def test_pattern_filename(self):
        assert pattern_filename(1, 1) == "S1_PTN1-01.PRM"
        assert pattern_filename(2, 5) == "S1_PTN2-05.PRM"
        assert pattern_filename(4, 16) == "S1_PTN4-16.PRM"

    def test_pattern_filename_validation(self):
        with pytest.raises(ValueError):
            pattern_filename(0, 1)
        with pytest.raises(ValueError):
            pattern_filename(1, 17)

    def test_find_s1_volume(self, tmp_path):
        assert find_s1_volume(tmp_path) is None
        vol = tmp_path / "S-1"
        (vol / "RESTORE").mkdir(parents=True)
        assert find_s1_volume(tmp_path) == vol

    def test_volume_requires_restore_folder(self, tmp_path):
        (tmp_path / "S-1").mkdir()
        assert find_s1_volume(tmp_path) is None

    def test_write_to_device(self, tmp_path):
        (tmp_path / "S-1" / "RESTORE").mkdir(parents=True)
        prm = build_pattern({74: 90})
        path = write_to_device(prm, 2, 5, volumes_dir=tmp_path)
        assert path == tmp_path / "S-1" / "RESTORE" / "S1_PTN2-05.PRM"
        assert PrmFile.load(path).to_cc_values()[74] == 90

    def test_write_without_volume_returns_none(self, tmp_path):
        assert write_to_device(build_pattern({}), 1, 1, volumes_dir=tmp_path) is None


# ── parser robustness ────────────────────────────────────────
class TestParser:
    def test_rejects_garbage(self):
        with pytest.raises(PrmParseError):
            PrmFile.parse("just some words\nwith no entries\n")

    def test_rejects_oversized(self):
        with pytest.raises(PrmParseError):
            PrmFile.parse("A = 1\n" * 300_000)

    def test_comments_and_blanks_preserved(self):
        text = "; comment\n\nLFO_RATE = 10\n# other\nVCF_CUTOFF\t= 255\n"
        prm = PrmFile.parse(text)
        assert prm.get_int("LFO_RATE") == 10
        assert prm.get_int("VCF_CUTOFF") == 255
        assert prm.serialize() == text

    def test_keys_case_insensitive(self):
        prm = PrmFile.parse("lfo_rate = 10\n")
        assert prm.get_int("LFO_RATE") == 10

    def test_set_preserves_tab_layout(self):
        prm = PrmFile.parse("VCF_CUTOFF\t= 255\n")
        prm.set("VCF_CUTOFF", 100)
        assert prm.serialize() == "VCF_CUTOFF\t= 100\n"

    def test_set_missing_key_returns_false(self):
        prm = PrmFile.parse("LFO_RATE = 10\n")
        assert prm.set("NOT_THERE", 1) is False
