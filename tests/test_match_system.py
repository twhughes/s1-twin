"""This Mac's own sound as a Match target (round 13, W-sys): ``GET /api/match/system/sources``,
``POST /api/match/system/start`` and ``/stop`` (synth/web/match_ws.py, synth/native/systap.py).

Driven through the real cockpit app with FastAPI's TestClient and a FAKE helper
(tests/fixtures/fake_systap.py; tests/conftest.py puts it in place for every test). It prints what the
real one prints, writes a known take when it is stopped, and exits with the real exit codes, so no
test touches real audio and the suite runs on Linux too. The Swift helper itself is compiled where
swiftc and macOS 14.2 are there, and never run for real (that would record, and macOS would ask).
"""

from __future__ import annotations

import io
import os
import re
import runpy
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

import synth.web.server as server_mod
from synth.native import systap

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_systap.py"
BASE_URL = f"http://127.0.0.1:{server_mod.PORT}"
SOURCES, START, STOP = "/api/match/system/sources", "/api/match/system/start", "/api/match/system/stop"
LOGIC = "com.apple.logic10"
EVERY_APP = {"app": None, "label": "This Mac's sound (all apps)"}


@pytest.fixture
def client() -> TestClient:
    # No context manager: the lifespan (the real engine watcher) stays off.
    return TestClient(server_mod.app, base_url=BASE_URL)


@pytest.fixture
def folders(monkeypatch) -> list[Path]:
    """Every temporary folder a take makes."""
    made: list[Path] = []
    real = systap.tempfile.mkdtemp

    def mkdtemp(*args, **kwargs):
        made.append(Path(real(*args, **kwargs)))
        return str(made[-1])

    monkeypatch.setattr(systap.tempfile, "mkdtemp", mkdtemp)
    return made


@pytest.fixture
def spawned(monkeypatch) -> list[subprocess.Popen]:
    """Every helper process a take starts."""
    procs: list[subprocess.Popen] = []

    class Recorded(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            procs.append(self)

    monkeypatch.setattr(systap.subprocess, "Popen", Recorded)
    return procs


def fake(monkeypatch, *flags: str) -> None:
    """The fake helper with its own flags: what goes wrong (--mode), the running apps (--apps)."""
    monkeypatch.setattr(systap, "TAP_COMMAND", [sys.executable, str(FAKE), *flags])


def plain(r, status: int, words: str) -> str:
    assert r.status_code == status, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, str) and words in detail, detail
    sentence = detail[0].isupper() or detail.startswith("macOS")
    assert sentence and detail.endswith("."), f"one plain sentence or more: {detail}"
    return detail


# ── what the picker can offer ────────────────────────────────────────────────────────────────────────
def test_sources_are_every_app_and_logic_while_it_runs(client, monkeypatch):
    assert client.get(SOURCES).json() == {"available": True, "detail": None, "permission": "granted",
                                           "sources": [EVERY_APP, {"app": LOGIC, "label": "Logic Pro"}]}
    fake(monkeypatch, "--apps", "com.apple.Safari")
    assert client.get(SOURCES).json()["sources"] == [EVERY_APP], "Logic Pro closed: every app only"


def test_a_computer_that_is_not_a_mac_offers_nothing(client, monkeypatch):
    monkeypatch.setattr(systap, "TAP_COMMAND", None)
    monkeypatch.setattr(systap, "macos_version", lambda: None)
    got = client.get(SOURCES).json()
    assert got == {"available": False, "detail": systap.NOT_A_MAC, "permission": None, "sources": []}
    plain(client.post(START, json={}), 503, "works only on a Mac")


@pytest.mark.parametrize("version, swiftc, words", [
    ((14, 1), "/usr/bin/swiftc", "needs macOS 14.2 or later"),
    ((15, 7, 9), None, "xcode-select --install"),
])
def test_an_old_macos_or_no_compiler_is_said_at_record(client, monkeypatch, version, swiftc, words):
    monkeypatch.setattr(systap, "TAP_COMMAND", None)
    monkeypatch.setattr(systap, "macos_version", lambda: version)
    monkeypatch.setattr(systap, "compiler", lambda: swiftc)
    monkeypatch.setattr(systap, "build", lambda *a, **k: pytest.fail("nothing is built"))
    got = client.get(SOURCES).json()
    assert got["available"] is False and words in got["detail"]
    assert got["sources"] == [EVERY_APP], "still offered, so Record can say why it cannot"
    plain(client.post(START, json={}), 503, words)


# ── a take ───────────────────────────────────────────────────────────────────────────────────────────
def test_record_then_stop_answers_the_take_as_wav(client, folders, spawned):
    r = client.post(START, json={"app": LOGIC})
    assert r.status_code == 200, r.text
    assert r.json() == {"recording": True, "app": LOGIC, "label": "Logic Pro", "rate": 48000}
    assert spawned[0].args[-2:] == ["--app", LOGIC] and "--seconds" in spawned[0].args
    assert len(folders) == 1 and folders[0].is_dir()

    r = client.post(STOP)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "audio/wav" and r.headers["cache-control"] == "no-store"
    audio, sr = sf.read(io.BytesIO(r.content), dtype="float32")
    assert sr == 48000 and audio.ndim == 1 and len(audio) == int(1.8 * sr), "the whole take, as written"
    lead, note = audio[: int(0.3 * sr)], audio[int(0.3 * sr): int(0.4 * sr)]
    assert np.abs(lead).max() == 0 and np.abs(note).max() > 0.3, "digital silence, then C3"
    assert not folders[0].exists(), "the take's temporary file is deleted before the answer"
    assert spawned[0].returncode == 0, "Stop is SIGTERM: the helper writes its take and ends"


def test_no_body_records_every_app(client, spawned):
    r = client.post(START)
    assert r.status_code == 200 and r.json()["app"] is None and r.json()["label"] == EVERY_APP["label"]
    assert "--app" not in spawned[0].args
    assert client.post(STOP).status_code == 200


def test_one_take_at_a_time(client, spawned):
    assert client.post(START, json={}).status_code == 200
    plain(client.post(START, json={"app": LOGIC}), 409, "already recording")
    assert len(spawned) == 1, "no second helper"
    assert client.post(STOP).status_code == 200
    plain(client.post(STOP), 409, "Nothing is recording")
    assert client.post(START, json={}).status_code == 200, "free again after a stop"
    assert client.post(STOP).status_code == 200


def test_the_cap_ends_a_take_that_is_never_stopped(client, monkeypatch, folders):
    monkeypatch.setattr(systap, "CAP_SECONDS", 0.3)
    assert client.post(START, json={}).status_code == 200
    systap._take.proc.wait(timeout=20)            # the helper stopped by itself, and nobody collected it
    assert client.post(START, json={}).status_code == 200, "a finished take does not block the next"
    assert not folders[0].exists(), "...and its folder is gone"
    assert client.post(STOP).status_code == 200


def test_closing_the_cockpit_ends_a_running_take(client, spawned, folders):
    assert client.post(START, json={}).status_code == 200
    systap.shutdown()
    assert spawned[0].poll() is not None and not folders[0].exists() and systap._take is None


# ── when it cannot: 409, 422 and 503, in plain words ─────────────────────────────────────────────────
@pytest.mark.parametrize("app", ["", "-rf", "../etc", "com.apple logic", "x" * 300])
def test_an_app_id_that_is_not_one_is_refused(client, app, spawned):
    plain(client.post(START, json={"app": app}), 422, "not an app")
    assert spawned == [], "nothing started"


def test_logic_pro_is_not_running(client, monkeypatch, folders):
    fake(monkeypatch, "--apps", "com.apple.Safari")
    detail = plain(client.post(START, json={"app": LOGIC}), 409, "not running")
    assert detail == "Logic Pro is not running. Open it, then press Record again."
    assert systap._take is None and not folders[0].exists()


def test_macos_blocked_system_audio(client, monkeypatch, folders):
    fake(monkeypatch, "--mode", "blocked")
    assert plain(client.post(START, json={}), 503, "blocked") == (
        "macOS blocked system audio. Allow it in System Settings > Privacy & Security > "
        "Screen & System Audio Recording, then press Record again.")
    assert not folders[0].exists()


def test_blocked_while_it_recorded(client, monkeypatch):
    fake(monkeypatch, "--mode", "blocked-late")
    assert client.post(START, json={}).status_code == 200
    plain(client.post(STOP), 503, "macOS blocked system audio")


def test_macos_too_old_for_the_helper(client, monkeypatch):
    fake(monkeypatch, "--mode", "too-old")
    plain(client.post(START, json={}), 503, "macOS 14.2 or later")


def test_a_tap_that_does_not_start_in_time(client, monkeypatch, spawned, folders):
    fake(monkeypatch, "--mode", "slow")
    monkeypatch.setattr(systap, "START_TIMEOUT", 0.5)
    t0 = time.monotonic()
    plain(client.post(START, json={}), 503, "If macOS asked about system audio, allow it")
    assert time.monotonic() - t0 < 10
    assert spawned[0].poll() is not None, "the helper is ended, not left running"
    assert systap._take is None and not folders[0].exists()


def test_a_helper_that_fails_says_why(client, monkeypatch):
    fake(monkeypatch, "--mode", "crash")
    assert client.post(START, json={}).status_code == 200
    detail = plain(client.post(STOP), 503, "could not be recorded")
    assert "Core Audio error" in detail and detail.endswith("Press Record again.")
    assert client.post(START, json={}).status_code == 200, "and the next take can start"


@pytest.mark.parametrize("mode, words", [("silent", "The recording is silent"),
                                         ("empty", "The recording is empty")])
def test_a_silent_or_empty_take_is_refused(client, monkeypatch, mode, words, folders):
    fake(monkeypatch, "--mode", mode)
    assert client.post(START, json={}).status_code == 200
    plain(client.post(STOP), 422, words)
    assert not folders[0].exists()


@pytest.mark.parametrize("method, route", [("get", SOURCES), ("post", START), ("post", STOP)])
def test_foreign_origin_is_refused(client, method, route, spawned):
    r = getattr(client, method)(route, headers={"origin": "http://evil.example"})
    assert r.status_code == 403 and spawned == []


def test_every_answer_fits_the_three_lines_under_record():
    """The Match view has three lines under Record (about 150 characters at its 330 px; measured in the
    browser with the longest). Every answer fits, with a helper's own words at their longest."""
    why, name = "x" * systap.WHY_CHARS, "y" * systap.WHY_CHARS
    for key in ("NOT_A_MAC", "OLD_MACOS", "NO_SWIFTC", "BUILD_FAILED", "BLOCKED_WORDS", "NOT_RUNNING",
                "BAD_APP", "BUSY", "NOT_RECORDING", "DID_NOT_START", "FAILED", "EMPTY", "SILENT"):
        longest = why[: systap.BUILD_WHY_CHARS] if key == "BUILD_FAILED" else why
        words = getattr(systap, key).format(why=longest, name=name)
        assert len(words) <= 150 and words.endswith("."), (key, len(words))
    assert len(systap._first_line("z" * 500 + "\nmore")) == systap.WHY_CHARS
    assert systap._error(systap.NO_APP, "", "com." + "q" * 250).detail.startswith("com.qqq")
    assert len(systap._error(systap.NO_APP, "", "com." + "q" * 250).detail) <= 150


# ── then the take goes the way of a microphone take ──────────────────────────────────────────────────
def test_the_take_is_prepared_like_a_microphone_take(client):
    pytest.importorskip("autograd")
    pytest.importorskip("scipy")
    assert client.post(START, json={"app": LOGIC}).status_code == 200
    raw = client.post(STOP).content
    r = client.post("/api/match/prepare", content=raw, headers={"content-type": "application/octet-stream"})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["notes"] == [48], "C3, found in the crop"
    assert abs(got["onset"] - 0.3) < 0.01 and got["crop"][0] < got["onset"], "the silence before it: cropped"
    assert got["duration"] == pytest.approx(1.8, abs=0.01)


# ── the helper: built once, again after an edit; the Swift source compiles ──────────────────────────
def _fake_swiftc(tmp_path: Path, fail: str = "") -> Path:
    """A stand-in compiler: logs its arguments, writes an executable at -o (or fails with `fail`)."""
    script = tmp_path / "swiftc"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        f"open({str(tmp_path / 'calls.txt')!r}, 'a').write(' '.join(args) + '\\n')\n"
        f"if {fail!r}:\n"
        f"    sys.stderr.write({fail!r} + '\\n'); sys.exit(1)\n"
        "out = args[args.index('-o') + 1]\n"
        "open(out, 'w').write('#!/bin/sh\\n')\n"
        "os.chmod(out, 0o755)\n")
    script.chmod(0o755)
    return script


def _sources(tmp_path: Path, monkeypatch) -> Path:
    src, plist = tmp_path / "systap.swift", tmp_path / "systap-Info.plist"
    src.write_text("// one\n")
    plist.write_text("<plist/>\n")
    monkeypatch.setattr(systap, "SOURCE", src)
    monkeypatch.setattr(systap, "PLIST", plist)
    return src


def test_the_helper_is_built_once_and_again_after_an_edit(tmp_path, monkeypatch):
    src = _sources(tmp_path, monkeypatch)
    swiftc = _fake_swiftc(tmp_path)
    monkeypatch.setattr(systap, "compiler", lambda: str(swiftc))
    folder = tmp_path / "bin"
    first = systap.build(folder)
    assert re.fullmatch(r"systap-[0-9a-f]{12}", first.name) and os.access(first, os.X_OK)
    assert systap.build(folder) == first
    calls = (tmp_path / "calls.txt").read_text().splitlines()
    assert len(calls) == 1, "built once"
    assert calls[0].startswith("-O -o ") and f"__info_plist -Xlinker {systap.PLIST}" in calls[0], \
        "optimized, and the Info.plist linked in (macOS reads why the helper records system audio)"
    src.write_text("// two\n")
    second = systap.build(folder)
    assert second != first and not first.exists(), "an edit builds anew; the old build goes"
    assert [p.name for p in folder.iterdir()] == [second.name], "nothing half-built stays"


def test_a_failed_build_is_said_plainly(tmp_path, monkeypatch):
    _sources(tmp_path, monkeypatch)
    swiftc = _fake_swiftc(tmp_path, fail="systap.swift:1:1: error: boom")
    monkeypatch.setattr(systap, "compiler", lambda: str(swiftc))
    with pytest.raises(systap.TapError) as caught:
        systap.build(tmp_path / "bin")
    assert caught.value.status == 503
    assert "did not build (systap.swift:1:1: error: boom)" in caught.value.detail
    assert list((tmp_path / "bin").iterdir()) == []


def test_the_swift_file_and_the_fake_agree_with_the_server():
    """One set of words and exit codes, in three places (the Swift helper, the fake, the server)."""
    swift = systap.SOURCE.read_text()
    parts = re.search(r'let blockedWords = "([^"]*)"\s*\+\s*"([^"]*)"', swift)
    assert parts and "".join(parts.groups()) == systap.BLOCKED_WORDS
    codes = dict(re.findall(r"(\w+) = (\d)", re.search(r"enum Code: Int32 \{([^}]*)\}", swift).group(1)))
    assert (int(codes["blocked"]), int(codes["noApp"]), int(codes["tooOld"])) == \
        (systap.BLOCKED, systap.NO_APP, systap.TOO_OLD)
    assert runpy.run_path(str(FAKE))["BLOCKED_WORDS"] == systap.BLOCKED_WORDS


REAL_MAC = (sys.platform == "darwin" and shutil.which("swiftc") is not None
            and (systap.macos_version() or (0,)) >= systap.MIN_MACOS)


@pytest.mark.skipif(not REAL_MAC, reason="the helper is for macOS 14.2 or later, built with swiftc")
def test_the_swift_helper_compiles(tmp_path):
    helper = systap.build(tmp_path / "bin")
    assert helper.is_file() and os.access(helper, os.X_OK)
    assert b"NSAudioCaptureUsageDescription" in helper.read_bytes(), "the Info.plist is linked in"
    # Only its usage error runs here: that touches no audio (a real run records, and macOS would ask).
    done = subprocess.run([str(helper), "--bogus"], capture_output=True, text=True, timeout=60)
    assert done.returncode == 2 and done.stderr.startswith("usage: systap")
