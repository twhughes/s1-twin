"""Tests for patches.py — save/load/list patch files."""

import json
from unittest.mock import patch

from synth.patches import list_patches, load_patch, save_patch


class TestPatches:
    def test_save_and_load_roundtrip(self, tmp_path):
        with patch("synth.patches.PATCH_DIR", tmp_path):
            values = {74: 100, 73: 50, 3: 127}
            path = save_patch("test-patch", values)
            assert path.exists()
            assert path.suffix == ".json"

            loaded = load_patch(path)
            assert loaded == values

    def test_save_creates_json(self, tmp_path):
        with patch("synth.patches.PATCH_DIR", tmp_path):
            save_patch("my-sound", {74: 64})
            data = json.loads((tmp_path / "my-sound.json").read_text())
            assert data["name"] == "my-sound"
            assert data["cc_values"]["74"] == 64

    def test_list_patches_empty(self, tmp_path):
        with patch("synth.patches.PATCH_DIR", tmp_path / "nonexistent"):
            assert list_patches() == []

    def test_list_patches_finds_files(self, tmp_path):
        with patch("synth.patches.PATCH_DIR", tmp_path):
            save_patch("a-patch", {74: 50})
            save_patch("b-patch", {74: 60})
            patches = list_patches()
            assert len(patches) == 2
            stems = [p.stem for p in patches]
            assert "a-patch" in stems
            assert "b-patch" in stems

    def test_load_preserves_int_keys(self, tmp_path):
        with patch("synth.patches.PATCH_DIR", tmp_path):
            save_patch("int-keys", {1: 10, 74: 127, 127: 0})
            loaded = load_patch(tmp_path / "int-keys.json")
            assert all(isinstance(k, int) for k in loaded.keys())

    def test_overwrite_existing_patch(self, tmp_path):
        with patch("synth.patches.PATCH_DIR", tmp_path):
            save_patch("same-name", {74: 50})
            save_patch("same-name", {74: 100})
            loaded = load_patch(tmp_path / "same-name.json")
            assert loaded[74] == 100
            assert len(list_patches()) == 1

    def test_backward_compat_with_existing_format(self, tmp_path):
        """Verify we can load patches saved in the original format."""
        patch_data = {
            "name": "old-patch",
            "cc_values": {"74": 100, "3": 50, "12": 64}
        }
        path = tmp_path / "old-patch.json"
        path.write_text(json.dumps(patch_data))
        loaded = load_patch(path)
        assert loaded == {74: 100, 3: 50, 12: 64}


class TestSanitizeName:
    def test_valid_names_pass_through(self):
        from synth.patches import sanitize_name

        assert sanitize_name("my-sound") == "my-sound"
        assert sanitize_name("  padded  ") == "padded"
        assert sanitize_name("bass 01 (fat)") == "bass 01 (fat)"

    def test_rejects_traversal_and_separators(self):
        import pytest

        from synth.patches import sanitize_name

        for bad in ["../x", "a/b", "a\\b", "..", ".", "", "  ", ".hidden", "a\x00b"]:
            with pytest.raises(ValueError):
                sanitize_name(bad)

    def test_save_patch_rejects_traversal(self, tmp_path):
        import pytest

        from synth.patches import save_patch

        with patch("synth.patches.PATCH_DIR", tmp_path / "bank"):
            with pytest.raises(ValueError):
                save_patch("../evil", {74: 0})
            assert not (tmp_path / "evil.json").exists()

    def test_delete_patch_rejects_traversal(self, tmp_path):
        import pytest

        from synth.patches import delete_patch

        with patch("synth.patches.PATCH_DIR", tmp_path):
            with pytest.raises(ValueError):
                delete_patch("../../etc/passwd")
