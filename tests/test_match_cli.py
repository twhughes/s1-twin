"""Tests for the ``synth-match`` CLI (``synth.match.cli``).

Covers the pure helpers ``_auto_port`` and ``_bar`` and the ``main`` argument
dispatch, with MIDI/session/save all faked so nothing touches hardware. The
existing ``TestCliGuards`` in test_match_engine.py already covers the
empty-best guard; these add port resolution, the list/error exits, the
progress renderer, and the happy path.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from synth.match import cli


# ── _bar progress renderer ───────────────────────────────────
class TestBar:
    def test_empty(self):
        bar = cli._bar(0.0)
        assert bar == "░" * 30
        assert len(bar) == 30

    def test_full(self):
        assert cli._bar(100.0) == "█" * 30

    def test_half(self):
        bar = cli._bar(50.0)
        assert bar.count("█") == 15
        assert bar.count("░") == 15

    def test_custom_width(self):
        bar = cli._bar(50.0, width=10)
        assert len(bar) == 10
        assert bar.count("█") == 5

    def test_rounds_to_nearest_cell(self):
        # 3.4 % of 30 cells rounds to 1 filled cell
        assert cli._bar(3.4).count("█") == 1
        assert cli._bar(1.0).count("█") == 0


# ── _auto_port resolution ────────────────────────────────────
class TestAutoPort:
    def test_explicit_substring_matches(self, monkeypatch):
        monkeypatch.setattr(
            cli.MidiBackend, "list_output_ports",
            staticmethod(lambda: ["IAC Bus 1", "Roland S-1 MIDI"]),
        )
        assert cli._auto_port("s-1") == "Roland S-1 MIDI"

    def test_explicit_no_match_returns_verbatim(self, monkeypatch):
        # let connect() raise the clear error, don't silently drop it
        monkeypatch.setattr(
            cli.MidiBackend, "list_output_ports",
            staticmethod(lambda: ["IAC Bus 1"]),
        )
        assert cli._auto_port("nonexistent") == "nonexistent"

    def test_autodetects_s1_by_name(self, monkeypatch):
        monkeypatch.setattr(
            cli.MidiBackend, "list_output_ports",
            staticmethod(lambda: ["IAC Bus 1", "S-1"]),
        )
        assert cli._auto_port(None) == "S-1"

    def test_autodetects_s1_alias(self, monkeypatch):
        monkeypatch.setattr(
            cli.MidiBackend, "list_output_ports",
            staticmethod(lambda: ["My S1 Synth"]),
        )
        assert cli._auto_port(None) == "My S1 Synth"

    def test_none_when_no_s1_present(self, monkeypatch):
        monkeypatch.setattr(
            cli.MidiBackend, "list_output_ports",
            staticmethod(lambda: ["IAC Bus 1", "USB MIDI"]),
        )
        assert cli._auto_port(None) is None


# ── main: list/error exits ───────────────────────────────────
class TestMainListing:
    def test_list_devices(self, monkeypatch, capsys):
        import synth.match.capture as capture

        monkeypatch.setattr(
            capture, "list_input_devices",
            lambda: [{"index": 2, "name": "S-1", "channels": 2, "samplerate": 44100}],
        )
        rc = cli.main(["--list-devices"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "[2]" in out and "S-1" in out and "44100" in out

    def test_list_ports(self, monkeypatch, capsys):
        monkeypatch.setattr(
            cli.MidiBackend, "list_output_ports",
            staticmethod(lambda: ["Roland S-1"]),
        )
        rc = cli.main(["--list-ports"])
        assert rc == 0
        assert "Roland S-1" in capsys.readouterr().out

    def test_missing_target_errors(self, monkeypatch):
        # argparse .error() exits with code 2
        with pytest.raises(SystemExit) as exc:
            cli.main([])
        assert exc.value.code == 2

    def test_no_port_found_returns_2(self, monkeypatch, capsys):
        monkeypatch.setattr(
            cli.MidiBackend, "list_output_ports", staticmethod(lambda: [])
        )
        rc = cli.main(["target.wav"])
        assert rc == 2
        assert "No S-1 MIDI port" in capsys.readouterr().err

    def test_connect_failure_returns_2(self, monkeypatch, capsys):
        monkeypatch.setattr(
            cli.MidiBackend, "list_output_ports",
            staticmethod(lambda: ["S-1"]),
        )

        def bad_connect(self, port):
            raise OSError("port busy")

        monkeypatch.setattr(cli.MidiBackend, "connect", bad_connect)
        rc = cli.main(["target.wav"])
        assert rc == 2
        assert "Could not connect" in capsys.readouterr().err


# ── main: happy path ─────────────────────────────────────────
class _FakeSession:
    """A session that yields one progress tick and a non-empty best patch."""

    instances: list = []

    def __init__(self, driver, config):
        self.config = config
        _FakeSession.instances.append(self)
        self.best_closeness = 87.5
        self._stopped = False

    def load_target(self, path):
        self.target = path

    def calibrate(self):
        return 0.035  # 35 ms

    def run(self, on_progress=None):
        if on_progress:
            on_progress(SimpleNamespace(
                iteration=5, max_iters=40, evals=120,
                best_closeness=87.5, generation_done=True, done=False,
            ))
        return {21: 64, 22: 100}

    def best_patch(self):
        return {21: 64, 22: 100}

    def stop(self):
        self._stopped = True


@pytest.fixture
def happy(monkeypatch, tmp_path):
    _FakeSession.instances.clear()
    disconnects = {"n": 0}

    monkeypatch.setattr(
        cli.MidiBackend, "list_output_ports", staticmethod(lambda: ["S-1 MIDI"])
    )
    monkeypatch.setattr(cli.MidiBackend, "connect", lambda self, port: None)

    def _disc(self):
        disconnects["n"] += 1

    monkeypatch.setattr(cli.MidiBackend, "disconnect", _disc)
    monkeypatch.setattr(cli, "MatchSession", _FakeSession)
    monkeypatch.setattr(cli, "SynthDriver", lambda *a, **k: object())
    saved = {}

    def _save(name, patch):
        saved["name"] = name
        saved["patch"] = patch
        return tmp_path / f"{name}.json"

    monkeypatch.setattr(cli, "save_patch", _save)
    return SimpleNamespace(disconnects=disconnects, saved=saved)


class TestMainHappyPath:
    def test_runs_and_saves(self, happy, capsys):
        rc = cli.main(["song.wav", "--no-calibrate", "--out", "brass"])
        assert rc == 0
        assert happy.saved["name"] == "brass"
        assert happy.saved["patch"] == {21: 64, 22: 100}
        out = capsys.readouterr().out
        assert "Best closeness: 87.5%" in out
        assert "Saved patch" in out
        assert happy.disconnects["n"] == 1  # port always released

    def test_default_patch_name_is_match(self, happy):
        cli.main(["song.wav", "--no-calibrate"])
        assert happy.saved["name"] == "match"

    def test_calibrate_prints_latency(self, happy, capsys):
        cli.main(["song.wav"])  # calibration on
        assert "Latency calibration: 35 ms" in capsys.readouterr().out

    def test_progress_renders_bar(self, happy, capsys):
        cli.main(["song.wav", "--no-calibrate"])
        out = capsys.readouterr().out
        assert "iter" in out and "87.5%" in out
        assert "█" in out  # the _bar renderer fired through main

    def test_channel_offset_passed_to_backend(self, happy, monkeypatch):
        seen = {}
        real_connect = cli.MidiBackend.connect

        def spy_connect(self, port):
            seen["channel"] = self.channel
            return real_connect(self, port)

        monkeypatch.setattr(cli.MidiBackend, "connect", spy_connect)
        cli.main(["song.wav", "--no-calibrate", "--channel", "3"])
        assert seen["channel"] == 2  # 1-indexed CLI -> 0-indexed backend
