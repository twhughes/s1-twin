"""This Mac's own sound, recorded: every app, or just Logic Pro (round 13, W-sys; Tyler: "also can we
record from system audio perhaps? like me playing a logic pro instrument").

The recording is done by a small Swift helper, ``systap.swift`` beside this file, on Core Audio's
process taps (macOS 14.2 and later): a tap on every app's output (or one app's, found by its bundle
id), read through a private device that holds only the tap, written as a mono 16-bit WAV at the
device's rate (the same kind of file the browser writes for a microphone take). Nothing is played.

The helper is built on first use (``swiftc -O``, a few seconds) into ``~/.synth/bin/``, named by a
hash of its source and its Info.plist, so an edit rebuilds it. On a computer that is not a Mac, or with
a macOS older than 14.2, or without swiftc, the routes say so plainly and the rest of the app is
unaffected.

macOS asks once whether the helper may record system audio (System Settings > Privacy & Security >
Screen & System Audio Recording). It asks about the helper itself, not about the app that started the
cockpit: a terminal or Alfred does not say why it would record system audio, and macOS refuses such an
app without asking (seen on this Mac: iTerm, authReason 8), so every take would be silence. The Swift
file's ``becomeResponsible`` has the details.

The routes (``synth/web/match_ws.py``) call :func:`sources`, :func:`start` and :func:`stop`. One take
at a time; it lives in a temporary folder that is deleted before the take is answered. Tests put a
fake helper in :data:`TAP_COMMAND` (``tests/fixtures/fake_systap.py``), so they never touch real audio.
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import io
import json
import os
import platform
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..paths import data_dir

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "systap.swift"
PLIST = HERE / "systap-Info.plist"      # linked in: why the helper records system audio

TAP_COMMAND: list[str] | None = None    # tests: a fake helper's argv prefix; None: the built helper
MIN_MACOS = (14, 2)                     # Core Audio process taps
CAP_SECONDS = 30                        # the helper stops by itself here, should Stop never come
START_TIMEOUT = 20.0                    # s for the tap to run: the first time, macOS asks first
STOP_TIMEOUT = 5.0
LIST_TIMEOUT = 15.0                     # a new build's first run waits for macOS to check it
BUILD_TIMEOUT = 300.0
SILENCE_PEAK = 1e-4                     # a take whose peak is below this (-80 dBFS) is silence
WHY_CHARS = 60                          # the helper's own words in an answer, shortened to this, and a
BUILD_WHY_CHARS = 40                    # compiler's to this: every answer fits the Match view's three lines

KNOWN_APPS = {"com.apple.logic10": "Logic Pro"}   # offered when running (Logic Pro is enough)
ALL_APPS = "This Mac's sound (all apps)"
BUNDLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,254}$")
BLOCKED, NO_APP, TOO_OLD = 3, 4, 5      # the helper's exit codes (systap.swift)

NOT_A_MAC = "Recording this computer's own sound works only on a Mac."
OLD_MACOS = "Recording this Mac's sound needs macOS 14.2 or later. Update macOS, then press Record again."
NO_SWIFTC = ("Recording this Mac's sound needs Apple's Swift compiler. Install it with "
             "xcode-select --install, then press Record again.")
BUILD_FAILED = ("The helper for this Mac's sound did not build ({why}). Check Xcode's command line tools, "
                "then press Record again.")
BLOCKED_WORDS = ("macOS blocked system audio. Allow it in System Settings > Privacy & Security > "
                 "Screen & System Audio Recording, then press Record again.")
NOT_RUNNING = "{name} is not running. Open it, then press Record again."
BAD_APP = "That is not an app this Mac can record. Choose one from the list, then press Record again."
BUSY = "This Mac's sound is already recording. Press Stop first."
NOT_RECORDING = "Nothing is recording this Mac's sound. Press Record first."
DID_NOT_START = ("The recording did not start. If macOS asked about system audio, allow it, "
                 "then press Record again.")
FAILED = "This Mac's sound could not be recorded ({why}). Press Record again."
EMPTY = "The recording is empty. Press Record, play the sound, then press Stop."
SILENT = ("The recording is silent. Play the sound while it records. If macOS asked about system "
          "audio, allow it, then press Record again.")


class TapError(Exception):
    """A plain reason, and the HTTP status the route answers with it."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def label(app: str | None) -> str:
    """What a source is called: "Logic Pro", or the whole Mac."""
    return ALL_APPS if app is None else KNOWN_APPS.get(app, app)


def _short(text: str, limit: int = WHY_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _first_line(text: str | bytes | None, limit: int = WHY_CHARS) -> str:
    """The first non-empty line of a helper's or compiler's output, shortened to ``limit``, without its
    final period (the words around it add their own)."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return _short(lines[0].rstrip(".") if lines else "", limit)


# ── can this computer do it ──────────────────────────────────────────────────────────────────────────
def macos_version() -> tuple[int, ...] | None:
    """(15, 7, 9) on a Mac; None elsewhere."""
    if sys.platform != "darwin":
        return None
    try:
        return tuple(int(p) for p in platform.mac_ver()[0].split("."))
    except ValueError:
        return None


def compiler() -> str | None:
    """The Swift compiler, or None."""
    return shutil.which("swiftc")


def unavailable() -> str | None:
    """Why this computer cannot record its own sound, in plain words (None: it can, or a fake stands in)."""
    if TAP_COMMAND is not None:
        return None
    version = macos_version()
    if version is None:
        return NOT_A_MAC
    if version < MIN_MACOS:
        return OLD_MACOS
    if compiler() is None:
        return NO_SWIFTC
    return None


# ── the helper, built on first use ───────────────────────────────────────────────────────────────────
_build_lock = threading.Lock()


def helper_name() -> str:
    """``systap-<hash of the source and the Info.plist>``: an edit makes a new name, so a new build."""
    digest = hashlib.sha256(SOURCE.read_bytes() + b"\0" + PLIST.read_bytes()).hexdigest()[:12]
    return f"systap-{digest}"


def build(folder: Path | None = None) -> Path:
    """The built helper in ``folder`` (``~/.synth/bin/``), built now if it is not there yet; older builds
    there are removed. Raises :class:`TapError` (503) with a plain reason."""
    folder = folder or data_dir() / "bin"
    target = folder / helper_name()
    with _build_lock:
        if target.is_file() and os.access(target, os.X_OK):
            return target
        swiftc = compiler()
        if swiftc is None:
            raise TapError(503, NO_SWIFTC)
        folder.mkdir(parents=True, exist_ok=True)
        # Built under its own name in a folder of its own, then moved in: the linker signs the helper
        # with the name it is built as, and macOS knows the helper by that signature.
        with tempfile.TemporaryDirectory(prefix=".build-", dir=folder) as work:
            part = Path(work) / target.name
            cmd = [swiftc, "-O", "-o", str(part), str(SOURCE),
                   "-Xlinker", "-sectcreate", "-Xlinker", "__TEXT", "-Xlinker", "__info_plist",
                   "-Xlinker", str(PLIST)]
            try:
                done = subprocess.run(cmd, capture_output=True, text=True, timeout=BUILD_TIMEOUT,
                                      stdin=subprocess.DEVNULL)
            except (OSError, subprocess.TimeoutExpired) as exc:
                why = "it took too long" if isinstance(exc, subprocess.TimeoutExpired) else str(exc)
                raise TapError(503, BUILD_FAILED.format(why=_first_line(why, BUILD_WHY_CHARS))) from exc
            if done.returncode != 0 or not part.is_file():
                why = _first_line(done.stderr, BUILD_WHY_CHARS) or f"swiftc exit {done.returncode}"
                raise TapError(503, BUILD_FAILED.format(why=why))
            os.replace(part, target)
        for old in folder.glob("systap-*"):
            if old != target:
                old.unlink(missing_ok=True)
        return target


def command() -> list[str]:
    """How to run the helper: the fake one in tests, else the built one. Raises :class:`TapError` (503)."""
    if TAP_COMMAND is not None:
        return list(TAP_COMMAND)
    reason = unavailable()
    if reason:
        raise TapError(503, reason)
    return [str(build())]


# ── what can be recorded ─────────────────────────────────────────────────────────────────────────────
def _listing() -> dict:
    """The helper's ``--list``: ``{permission, apps: [{bundle, pid, output}]}``."""
    try:
        done = subprocess.run(command() + ["--list"], capture_output=True, text=True,
                              timeout=LIST_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TapError(503, FAILED.format(why="the helper did not answer")) from exc
    if done.returncode != 0:
        raise TapError(503, FAILED.format(why=_first_line(done.stderr) or f"exit {done.returncode}"))
    try:
        listing = json.loads(done.stdout)
    except ValueError as exc:
        raise TapError(503, FAILED.format(why="the helper's list was unreadable")) from exc
    return listing if isinstance(listing, dict) else {}


def sources() -> dict:
    """What the Match view's "From" picker can offer from this Mac (``GET /api/match/system/sources``):
    ``{available, detail, permission, sources: [{app, label}]}``. Every app's sound whenever this is a
    Mac (an old macOS or a missing compiler is said when Record is pressed), plus Logic Pro while it
    runs. ``permission`` is what macOS said about the helper: granted, denied, unknown (not asked yet),
    or null. Never raises."""
    reason = unavailable()
    if reason == NOT_A_MAC:
        return {"available": False, "detail": reason, "permission": None, "sources": []}
    offered: list[dict] = [{"app": None, "label": ALL_APPS}]
    if reason:
        return {"available": False, "detail": reason, "permission": None, "sources": offered}
    try:
        listing = _listing()
    except TapError as exc:
        return {"available": False, "detail": exc.detail, "permission": None, "sources": offered}
    running = {row.get("bundle") for row in listing.get("apps") or [] if isinstance(row, dict)}
    offered += [{"app": app, "label": name} for app, name in KNOWN_APPS.items() if app in running]
    permission = listing.get("permission")
    return {"available": True, "detail": None,
            "permission": permission if permission in ("granted", "denied", "unknown") else None,
            "sources": offered}


# ── one take ─────────────────────────────────────────────────────────────────────────────────────────
@dataclass
class Take:
    proc: subprocess.Popen
    folder: Path
    path: Path
    app: str | None
    rate: int = 0


_lock = threading.Lock()
_take: Take | None = None


def _read_line(proc: subprocess.Popen, timeout: float) -> str:
    """The helper's first line on stdout ("recording 48000"), or "" when it ended or took too long."""
    fd = proc.stdout.fileno()
    buf = b""
    deadline = time.monotonic() + timeout
    while b"\n" not in buf:
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            break
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        buf += chunk
    return buf.split(b"\n", 1)[0].decode("utf-8", "replace").strip()


def _kill(take: Take) -> None:
    """End the helper and its copy (they share their own process group), and wait for it."""
    with contextlib.suppress(OSError):
        os.killpg(take.proc.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        take.proc.kill()
    with contextlib.suppress(Exception):
        take.proc.communicate(timeout=2)


def _discard(take: Take | None) -> None:
    if take is None:
        return
    if take.proc.poll() is None:
        _kill(take)
    shutil.rmtree(take.folder, ignore_errors=True)


def _error(code: int | None, stderr: str | bytes | None, app: str | None) -> TapError:
    """The helper's exit code and words, as the route's answer."""
    if code == BLOCKED:
        return TapError(503, BLOCKED_WORDS)
    if code == NO_APP:
        return TapError(409, NOT_RUNNING.format(name=_short(label(app))))
    if code == TOO_OLD:
        return TapError(503, OLD_MACOS)
    return TapError(503, FAILED.format(why=_first_line(stderr) or f"exit {code}"))


def start(app: str | None = None) -> dict:
    """Start recording this Mac's sound: every app when ``app`` is None, else the app with that bundle id
    (Logic Pro: ``com.apple.logic10``). Answers ``{recording, app, label, rate}`` once the tap runs.
    Raises :class:`TapError`: 409 already recording, or the app is not running; 422 not an app id; 503
    this Mac cannot record (an old macOS, no compiler, macOS blocked system audio, no start in time)."""
    global _take
    if app is not None and not (isinstance(app, str) and BUNDLE_ID.match(app)):
        raise TapError(422, BAD_APP)
    with _lock:
        if _take is not None and _take.proc.poll() is None:
            raise TapError(409, BUSY)
        _discard(_take)                   # a take that stopped by itself and was never collected
        _take = None
        argv = command()
        folder = Path(tempfile.mkdtemp(prefix="synth-systap-"))
        path = folder / "take.wav"
        argv += ["--out", str(path), "--seconds", str(CAP_SECONDS)] + (["--app", app] if app else [])
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, start_new_session=True)
        except OSError as exc:
            shutil.rmtree(folder, ignore_errors=True)
            raise TapError(503, FAILED.format(why=_first_line(str(exc)))) from exc
        take = Take(proc, folder, path, app)
        line = _read_line(take.proc, START_TIMEOUT)
        if not line.startswith("recording"):
            try:                          # it ended (its words are on stderr), or it is still starting
                code = take.proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                code = None
            if code is None:              # still starting after all that: most likely macOS is asking
                _discard(take)
                raise TapError(503, DID_NOT_START)
            _, err = take.proc.communicate()
            shutil.rmtree(folder, ignore_errors=True)
            raise _error(code, err, app)
        with contextlib.suppress(IndexError, ValueError):
            take.rate = int(line.split()[1])
        _take = take
        return {"recording": True, "app": app, "label": label(app), "rate": take.rate}


def stop() -> bytes:
    """Stop the take and answer it as WAV bytes; its folder is deleted first. Raises :class:`TapError`:
    409 nothing is recording; 422 the take is empty or silent; 503 the helper failed."""
    global _take
    with _lock:
        take, _take = _take, None
        if take is None:
            raise TapError(409, NOT_RECORDING)
        try:
            if take.proc.poll() is None:
                with contextlib.suppress(OSError):
                    take.proc.send_signal(signal.SIGTERM)
            try:
                _, err = take.proc.communicate(timeout=STOP_TIMEOUT)
            except subprocess.TimeoutExpired as exc:
                _kill(take)
                raise TapError(503, FAILED.format(why="the helper did not stop")) from exc
            if take.proc.returncode != 0:
                raise _error(take.proc.returncode, err, take.app)
            try:
                raw = take.path.read_bytes()
            except OSError as exc:
                raise TapError(422, EMPTY) from exc
        finally:
            shutil.rmtree(take.folder, ignore_errors=True)
    frames, peak = _level(raw)
    if frames == 0:
        raise TapError(422, EMPTY)
    if peak < SILENCE_PEAK:
        raise TapError(422, SILENT)
    return raw


def _level(raw: bytes) -> tuple[int, float]:
    """A WAV's frame count and peak (0..1). Raises :class:`TapError` (503) when it cannot be read."""
    import numpy as np
    import soundfile as sf

    try:
        samples, _ = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
    except Exception as exc:  # noqa: BLE001 - any decode failure: the helper wrote a bad file
        raise TapError(503, FAILED.format(why="the take could not be read")) from exc
    return len(samples), float(np.abs(samples).max()) if samples.size else 0.0


@atexit.register
def shutdown() -> None:
    """The cockpit is closing: end a running take and delete its folder."""
    global _take
    take, _take = _take, None
    _discard(take)
