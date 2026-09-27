"""The ear test: does the twin's distance agree with Tyler's ears? (FABLE rule 1)

FABLE: *"Validate the metric early: play me pairs, ask which is closer, and confirm the
number agrees with my ears before trusting it for anything."* Each trial renders three
twin patches — a reference and two candidates at controlled distances from it under
:func:`synth.match.twin.spectral_loss` (the matcher's objective) — and asks which
candidate is closer. The metric's answer is the candidate with the lower loss. The gap
between the two losses runs from easy (the far one is 2.2-3x farther) to very hard
(1.1-1.3x); the page never learns which side is closer.

Answers go to ``<dir>/<session>.jsonl`` (``~/.synth/eartest/`` unless ``SYNTH_EARTEST_DIR``
is set), one line per answer, carrying the metric's side, both losses and what each
candidate changed — so ``synth-eartest-report`` scores agreement without re-rendering.

Routes (this router is mounted by ``server.py``):

- ``GET  /eartest`` — the page (``static/eartest/index.html``).
- ``GET  /api/eartest/trial?session=S`` — the next unanswered trial of session S, or
  ``&i=N`` for trial N (the page prefetches N+1). ``{id, session, index, total,
  answered, done, mime, reference, a, b}``; the three clips are base64 WAV. When every
  trial is answered: ``{done: true, session, total, answered, path}``.
- ``POST /api/eartest/answer`` — ``{trial_id, choice: "A"|"B"|"same", ms}``; appends
  one line (a repeated answer to the same trial is ignored).

Trials are deterministic: trial N of session S is always the same audio, so a reload,
a server restart or a prefetch race cannot change what was asked.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import re
import threading
import wave
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..match import ANALYSIS_SECONDS, WORKING_SR
from ..paths import data_dir

router = APIRouter()

STATIC_DIR = Path(__file__).parent / "static" / "eartest"

TRIALS = 40                      # about 10 minutes at ~15 s a pair
SR = WORKING_SR                  # the twin's own rate; the metric is computed at it
SECONDS = ANALYSIS_SECONDS       # 2.0 s clips, note-off at 1.2 s (the twin's default gate)

# Difficulty tiers: the far candidate's spectral_loss is this many times the near one's.
TIERS: tuple[tuple[str, float, float], ...] = (
    ("easy", 2.2, 3.0),
    ("medium", 1.6, 2.2),
    ("hard", 1.3, 1.6),
    ("very hard", 1.1, 1.3),
)
TIER_BOUNDS = {name: (lo, hi) for name, lo, hi in TIERS}
NEAR_LOSS = (0.6, 1.6)           # the closer candidate's loss (log-uniform); a random patch is ~5-13
LOSS_TOL = 0.04                  # a candidate lands within 4% of its target loss
TARGET_RMS = 0.1                 # every clip plays at the same loudness (-20 dBFS RMS)
PEAK_CEILING = 0.9
FADE_S = 0.02                    # fade the last 20 ms so a long release does not click

# Where musical reference patches live, per k param (normalized). Fine tune stays at the
# center: this test is about timbre, and pitch is held fixed exactly as the matcher holds it.
REF_RANGES: dict[str, tuple[float, float]] = {
    "saw_lvl": (0.0, 1.0), "square_lvl": (0.0, 1.0), "sub_lvl": (0.0, 1.0), "noise_lvl": (0.0, 0.3),
    "pulse_width": (0.0, 1.0), "cutoff": (0.35, 0.95), "resonance": (0.0, 0.7),
    "env_to_cutoff": (0.0, 0.6), "lfo_to_cutoff": (0.0, 0.3), "key_follow": (0.0, 1.0),
    "attack": (0.0, 0.45), "decay": (0.2, 0.9), "sustain": (0.0, 1.0), "release": (0.1, 0.7),
    "lfo_rate": (0.2, 0.8), "lfo_to_pitch": (0.0, 0.12), "lfo_depth": (0.0, 1.0),
    "fine_tune": (0.5, 0.5),
}
REF_NOTES = (36, 55)             # bass range, inclusive
FIXED = ("fine_tune",)           # never perturbed

_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CACHE_LOCK = threading.Lock()          # guards the cache dicts; held only briefly
_GEN_LOCKS: dict[str, threading.Lock] = {}   # one per trial being rendered
_FILE_LOCK = threading.Lock()
_CACHE: OrderedDict[str, "Trial"] = OrderedDict()
_CACHE_MAX = 48


def eartest_dir() -> Path:
    """Where answer files live: ``$SYNTH_EARTEST_DIR`` or ``~/.synth/eartest``."""
    env = os.environ.get("SYNTH_EARTEST_DIR")
    return Path(env).expanduser() if env else data_dir() / "eartest"


def session_path(session: str) -> Path:
    return eartest_dir() / f"{session}.jsonl"


def check_session(session: str) -> str:
    if not _SESSION_RE.match(session or ""):
        raise HTTPException(400, "A session name uses letters, digits, '-' and '_' only (at most 64).")
    return session


def session_seed(session: str) -> int:
    return int.from_bytes(hashlib.sha256(session.encode("utf-8")).digest()[:8], "big")


def trial_id(session: str, index: int) -> str:
    return f"{session}.{int(index)}"


def parse_trial_id(tid: str) -> tuple[str, int]:
    session, _, idx = (tid or "").rpartition(".")
    if not _SESSION_RE.match(session) or not idx.isdigit():
        raise HTTPException(400, f"Not a trial id: {tid!r}")
    return session, int(idx)


def schedule(session: str, total: int | None = None) -> list[str]:
    """The tier of every trial. Blocks of eight hold two of each tier in a shuffled
    order, so fatigue and learning spread evenly over the tiers; trial 0 is easy."""
    total = TRIALS if total is None else int(total)
    rng = np.random.default_rng([session_seed(session), 7])
    block = [name for name, _lo, _hi in TIERS for _ in range(2)]
    order: list[str] = []
    while len(order) < total:
        b = list(block)
        rng.shuffle(b)
        order += b
    order = order[:total]
    if order and order[0] != "easy" and "easy" in order:
        j = order.index("easy")
        order[0], order[j] = order[j], order[0]
    return order


# ─────────────────────────────────────────────────────────────────────────────
# Trial generation
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Candidate:
    k: np.ndarray
    loss: float
    audio: np.ndarray


@dataclass
class Trial:
    id: str
    session: str
    index: int
    tier: str
    note: int
    s: dict[str, int]
    reference: np.ndarray
    a: np.ndarray
    b: np.ndarray
    loss_a: float
    loss_b: float
    closer: str                           # "A" or "B": the metric's answer
    target_ratio: float
    a_dk: dict[str, float] = field(default_factory=dict)
    b_dk: dict[str, float] = field(default_factory=dict)

    @property
    def ratio(self) -> float:
        lo, hi = sorted((self.loss_a, self.loss_b))
        return float(hi / max(lo, 1e-12))

    def truth(self) -> dict[str, Any]:
        """What the answer line records about this trial (never sent to the page)."""
        from ..match.calibrate_cli import SWEEP_BY_CURVE

        def module(dk: dict[str, float]) -> str | None:
            if not dk:
                return None
            name = max(dk, key=lambda n: abs(dk[n]))
            spec = SWEEP_BY_CURVE.get(name)
            return spec.module if spec else None

        return {
            "tier": self.tier, "closer": self.closer, "loss_a": round(self.loss_a, 5),
            "loss_b": round(self.loss_b, 5), "ratio": round(self.ratio, 4),
            "target_ratio": round(self.target_ratio, 4), "note": self.note, "s": self.s,
            "a_dk": self.a_dk, "b_dk": self.b_dk, "a_module": module(self.a_dk),
            "b_module": module(self.b_dk),
        }


def _twin():
    try:
        from ..match.twin import Twin
    except ImportError as e:  # pragma: no cover - the twin extra is missing
        raise HTTPException(503, "The ear test needs the twin extra. Install it with "
                                 ".venv/bin/pip install -e '.[studio,twin]' and restart.") from e
    return Twin(sr=SR, seconds=SECONDS)


def _finish(x: np.ndarray) -> np.ndarray:
    """The exact samples the page will play: loudness-matched (every clip at the same
    RMS), faded out at the end, and quantized to 16 bits. The gap search and the truth
    both use these samples, so what is scored is what is heard."""
    x = np.asarray(x, dtype=np.float64)
    rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
    if rms > 1e-9:
        x = x * (TARGET_RMS / rms)
    n = int(FADE_S * SR)
    if 0 < n < x.size:
        x = x.copy()
        x[-n:] *= np.linspace(1.0, 0.0, n)
    return _quantize(x)


def _quantize(x: np.ndarray) -> np.ndarray:
    """Exactly what the 16-bit WAV will carry. The metric reacts to content ~80 dB down
    (a release tail), so the truth is computed on these samples, not on the floats."""
    return np.clip(np.round(np.asarray(x, dtype=np.float64) * 32767.0), -32768, 32767) / 32767.0


def _loss(a: np.ndarray, b: np.ndarray) -> float:
    from ..match.twin import spectral_loss

    return float(spectral_loss(a, b, SR))


def _sample_reference(rng: np.random.Generator, names: tuple[str, ...]) -> np.ndarray:
    k = np.array([rng.uniform(*REF_RANGES[n]) for n in names])
    oscs = [names.index(n) for n in ("saw_lvl", "square_lvl", "sub_lvl")]
    if max(k[i] for i in oscs) < 0.5:
        k[oscs[int(rng.integers(0, 3))]] = rng.uniform(0.6, 1.0)
    return k


def _direction(rng: np.random.Generator, k_ref: np.ndarray, names: tuple[str, ...]) -> np.ndarray:
    """A sparse unit direction: 1-3 knobs, each pushed toward the middle of its range
    when it sits near an end, so a small step always changes the sound."""
    free = [i for i, n in enumerate(names) if n not in FIXED]
    m = int(rng.integers(1, 4))
    dims = rng.choice(free, size=m, replace=False)
    d = np.zeros(len(names))
    for i in dims:
        sign = -1.0 if k_ref[i] > 0.8 else 1.0 if k_ref[i] < 0.2 else float(rng.choice([-1.0, 1.0]))
        d[i] = sign * rng.uniform(0.5, 1.0)
    return d / np.linalg.norm(d)


def _candidate(tw: Any, k_ref: np.ndarray, s: dict[str, int], note: int, ref: np.ndarray,
               direction: np.ndarray, target: float) -> Candidate | None:
    """Walk from the reference along ``direction`` until spectral_loss hits ``target``
    (within LOSS_TOL): double the step until it passes the target, then bisect."""
    def at(t: float) -> Candidate:
        k = np.clip(k_ref + t * direction, 0.0, 1.0)
        audio = _finish(np.asarray(tw.render(k, s, note)))
        return Candidate(k=k, loss=_loss(audio, ref), audio=audio)

    lo_t, hi_t = 0.0, 0.03
    hi = at(hi_t)
    while hi.loss < target:
        if hi_t > 1.5:
            return None                     # this direction saturates below the target
        lo_t, hi_t = hi_t, hi_t * 2.0
        hi = at(hi_t)
    best = hi
    for _ in range(16):
        if abs(best.loss - target) <= LOSS_TOL * target:
            return best
        mid = at(0.5 * (lo_t + hi_t))
        if abs(mid.loss - target) < abs(best.loss - target):
            best = mid
        if mid.loss < target:
            lo_t = 0.5 * (lo_t + hi_t)
        else:
            hi_t = 0.5 * (lo_t + hi_t)
    return best if abs(best.loss - target) <= 3 * LOSS_TOL * target else None


def make_trial(session: str, index: int) -> Trial:
    """Build trial ``index`` of ``session`` — deterministic for the pair."""
    tw = _twin()                      # first: a missing twin extra becomes a clear 503
    from ..match.twin import K_NAMES, S_PARAMS

    names = tuple(K_NAMES)
    tier = schedule(session)[index]
    lo_r, hi_r = TIER_BOUNDS[tier]
    rng = np.random.default_rng([session_seed(session), int(index)])
    for _attempt in range(12):
        k_ref = _sample_reference(rng, names)
        s = {sp.name: int(rng.choice(sp.choices)) for sp in S_PARAMS}
        note = int(rng.integers(REF_NOTES[0], REF_NOTES[1] + 1))
        ref = _finish(np.asarray(tw.render(k_ref, s, note)))
        near_t = float(math.exp(rng.uniform(math.log(NEAR_LOSS[0]), math.log(NEAR_LOSS[1]))))
        ratio_t = float(rng.uniform(lo_r, hi_r))
        near = far = None
        for _d in range(4):
            near = _candidate(tw, k_ref, s, note, ref, _direction(rng, k_ref, names), near_t)
            if near is not None:
                break
        for _d in range(4):
            far = _candidate(tw, k_ref, s, note, ref, _direction(rng, k_ref, names), near_t * ratio_t)
            if far is not None:
                break
        if near is None or far is None or far.loss <= near.loss * 1.05:
            continue
        a, b = (near, far) if rng.random() < 0.5 else (far, near)

        def dk(c: Candidate) -> dict[str, float]:
            delta = c.k - k_ref
            return {n: round(float(v), 3) for n, v in zip(names, delta) if abs(v) > 0.005}

        if max(float(np.abs(x).max()) for x in (ref, a.audio, b.audio)) > PEAK_CEILING:
            continue                        # too spiky to play at the shared loudness: draw again
        loss_a, loss_b = _loss(a.audio, ref), _loss(b.audio, ref)
        return Trial(id=trial_id(session, index), session=session, index=int(index), tier=tier,
                     note=note, s=s, reference=ref, a=a.audio, b=b.audio, loss_a=loss_a,
                     loss_b=loss_b, closer="A" if loss_a < loss_b else "B", target_ratio=ratio_t,
                     a_dk=dk(a), b_dk=dk(b))
    raise HTTPException(500, f"Could not build trial {index} of {session}; try another session name.")


def get_trial(session: str, index: int) -> Trial:
    """The trial from the cache, or rendered once. A cached read never waits for another
    trial's render (the page prefetches the next pair while an answer is being saved)."""
    tid = trial_id(session, index)
    with _CACHE_LOCK:
        if tid in _CACHE:
            _CACHE.move_to_end(tid)
            return _CACHE[tid]
        gen = _GEN_LOCKS.setdefault(tid, threading.Lock())
    with gen:
        with _CACHE_LOCK:
            if tid in _CACHE:
                return _CACHE[tid]
        trial = make_trial(session, index)
        with _CACHE_LOCK:
            _CACHE[tid] = trial
            while len(_CACHE) > _CACHE_MAX:
                _CACHE.popitem(last=False)
            _GEN_LOCKS.pop(tid, None)
        return trial


def wav_bytes(x: np.ndarray, sr: int | None = None) -> bytes:
    """16-bit mono WAV (stdlib ``wave``) at ``sr`` (default: the ear test's rate)."""
    sr = SR if sr is None else sr
    pcm = np.clip(np.round(np.asarray(x, dtype=np.float64) * 32767.0), -32768, 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _b64(x: np.ndarray) -> str:
    return base64.b64encode(wav_bytes(x)).decode("ascii")


# ─────────────────────────────────────────────────────────────────────────────
# Answers
# ─────────────────────────────────────────────────────────────────────────────
def read_answers(session: str) -> list[dict]:
    path = session_path(session)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


class AnswerIn(BaseModel):
    trial_id: str = Field(max_length=120)
    choice: Literal["A", "B", "same"]
    ms: int = Field(0, ge=0, le=3_600_000)


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/eartest", include_in_schema=False)
def eartest_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@router.get("/api/eartest/trial", tags=["eartest"],
            summary="The next unanswered ear-test trial (or trial i)")
def api_trial(session: str = Query(..., description="Session name: letters, digits, '-', '_'"),
              i: int | None = Query(None, ge=0, description="A specific trial index")) -> dict:
    check_session(session)
    answered = {r.get("trial_id") for r in read_answers(session)}
    if i is None:
        i = next((j for j in range(TRIALS) if trial_id(session, j) not in answered), None)
        if i is None:
            return {"done": True, "session": session, "total": TRIALS, "answered": len(answered),
                    "path": str(session_path(session))}
    if i >= TRIALS:
        raise HTTPException(404, f"This session has {TRIALS} trials (0 to {TRIALS - 1}).")
    t = get_trial(session, i)
    return {"id": t.id, "session": session, "index": t.index, "total": TRIALS,
            "answered": len(answered), "done": False, "mime": "audio/wav",
            "reference": _b64(t.reference), "a": _b64(t.a), "b": _b64(t.b)}


@router.post("/api/eartest/answer", tags=["eartest"], summary="Record one ear-test answer")
def api_answer(ans: AnswerIn) -> dict:
    session, index = parse_trial_id(ans.trial_id)
    if index >= TRIALS:
        raise HTTPException(400, f"This session has {TRIALS} trials.")
    t = get_trial(session, index)
    path = session_path(session)
    with _FILE_LOCK:
        rows = read_answers(session)
        if any(r.get("trial_id") == ans.trial_id for r in rows):
            n = len({r.get("trial_id") for r in rows})
            return {"ok": True, "duplicate": True, "answered": n, "total": TRIALS, "done": n >= TRIALS,
                    "path": str(path)}
        row = {"v": 1, "ts": datetime.now().isoformat(timespec="seconds"), "session": session,
               "trial_id": ans.trial_id, "index": index, "choice": ans.choice, "ms": int(ans.ms),
               **t.truth()}
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(row) + "\n")
        n = len({r.get("trial_id") for r in rows}) + 1
    return {"ok": True, "duplicate": False, "answered": n, "total": TRIALS, "done": n >= TRIALS,
            "path": str(path)}
