"""Tests for patches.py — save/load/list patch files."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from s1tui.patches import save_patch, load_patch, list_patches


class TestPatches:
    def test_save_and_load_roundtrip(self, tmp_path):
        with patch("s1tui.patches.PATCH_DIR", tmp_path):
            values = {74: 100, 73: 50, 3: 127}
            path = save_patch("test-patch", values)
            assert path.exists()
            assert path.suffix == ".json"

            loaded = load_patch(path)
            assert loaded == values

    def test_save_creates_json(self, tmp_path):
        with patch("s1tui.patches.PATCH_DIR", tmp_path):
            save_patch("my-sound", {74: 64})
            data = json.loads((tmp_path / "my-sound.json").read_text())
            assert data["name"] == "my-sound"
            assert data["cc_values"]["74"] == 64

    def test_list_patches_empty(self, tmp_path):
        with patch("s1tui.patches.PATCH_DIR", tmp_path / "nonexistent"):
            assert list_patches() == []

    def test_list_patches_finds_files(self, tmp_path):
        with patch("s1tui.patches.PATCH_DIR", tmp_path):
            save_patch("a-patch", {74: 50})
            save_patch("b-patch", {74: 60})
            patches = list_patches()
            assert len(patches) == 2
            stems = [p.stem for p in patches]
            assert "a-patch" in stems
            assert "b-patch" in stems

    def test_load_preserves_int_keys(self, tmp_path):
        with patch("s1tui.patches.PATCH_DIR", tmp_path):
            save_patch("int-keys", {1: 10, 74: 127, 127: 0})
            loaded = load_patch(tmp_path / "int-keys.json")
            assert all(isinstance(k, int) for k in loaded.keys())

    def test_overwrite_existing_patch(self, tmp_path):
        with patch("s1tui.patches.PATCH_DIR", tmp_path):
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
