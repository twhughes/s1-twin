"""Match session orchestrator.

Ties the target, the :class:`~synth.match.driver.SynthDriver`, the
:class:`~synth.match.space.ParamSpace`, and an :mod:`~synth.match.optimizer`
together into one loop: ask candidates -> apply + probe + score -> tell. Emits a
:class:`Progress` snapshot through a callback so a CLI or the web app can render
it. Supports automated and interactive (pause-to-listen) modes, plus pause/resume/
stop from another thread.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .capture import AudioClip, is_silent, load_audio, prepare
from .distance import Weights, closeness, loss, reference_scales
from .driver import SynthDriver
from .features import Features, extract
from .optimizer import make_optimizer
from .space import ParamSpace

# Loss assigned to silent candidates and failed probes. Orders of magnitude
# above any real (normalized) loss, so silence is never rewarded and a
# penalized candidate can never become the best patch.
SILENCE_PENALTY_LOSS = 1000.0


@dataclass
class MatchConfig:
    max_iters: int = 40
    popsize: int | None = None        # None -> optimizer default
    include_effects: bool = True
    optimizer: str = "cma"            # "cma" | "random"
    mode: str = "auto"               # "auto" | "interactive"
    weights: Weights = field(default_factory=Weights)
    seed: int | None = None


@dataclass
class Progress:
    iteration: int
    evals: int
    max_iters: int
    best_closeness: float
    best_loss: float
    best_params: dict[int, int]
    last_closeness: float = 0.0
    generation_done: bool = False
    done: bool = False
    error: str | None = None
    cache_hits: int = 0
    target_clip: AudioClip | None = None
    best_clip: AudioClip | None = None
    last_clip: AudioClip | None = None


ProgressCb = "callable"  # (Progress) -> None


class MatchSession:
    def __init__(self, driver: SynthDriver, config: MatchConfig | None = None):
        self.driver = driver
        self.config = config or MatchConfig()
        self.space = ParamSpace(include_effects=self.config.include_effects)

        self.target_clip: AudioClip | None = None
        self.target_feat: Features | None = None
        self._scales: dict[str, float] | None = None

        self._iteration = 0
        self._evals = 0
        self._best_loss = float("inf")
        self._best_params: dict[int, int] = {}
        self._best_clip: AudioClip | None = None
        # Discrete snapping means CMA often re-samples vectors that decode to
        # an already-probed CC dict; each hardware probe costs ~2 s, a memo
        # lookup costs nothing.
        self._eval_cache: dict[tuple, float] = {}
        self._cache_hits = 0

        self._stop = threading.Event()
        self._resume = threading.Event()
        self._resume.set()

    # ── setup ────────────────────────────────────────────────
    def load_target(self, path) -> None:
        self.target_clip = prepare(load_audio(path))
        self.target_feat = extract(self.target_clip)
        self._scales = reference_scales(self.target_feat)

    def set_target_clip(self, clip: AudioClip) -> None:
        """Use an already-captured clip as the target (e.g. recorded live)."""
        self.target_clip = prepare(clip)
        self.target_feat = extract(self.target_clip)
        self._scales = reference_scales(self.target_feat)

    def calibrate(self) -> float:
        return self.driver.calibrate()

    # ── controls (thread-safe) ───────────────────────────────
    def pause(self) -> None:
        self._resume.clear()

    def resume(self) -> None:
        self._resume.set()

    def stop(self) -> None:
        self._stop.set()
        self._resume.set()

    @property
    def paused(self) -> bool:
        return not self._resume.is_set()

    # ── results ──────────────────────────────────────────────
    @property
    def best_closeness(self) -> float:
        return closeness(self._best_loss, self.config.weights) if self._best_params else 0.0

    def best_patch(self) -> dict[int, int]:
        return dict(self._best_params)

    def apply_best(self) -> None:
        if self._best_params:
            self.driver.apply(self._best_params)

    # ── run loop ─────────────────────────────────────────────
    def run(self, on_progress=None) -> dict[int, int]:
        if self.target_feat is None:
            raise RuntimeError("call load_target()/set_target_clip() before run()")

        x0 = self.space.default_vector()
        opt = make_optimizer(
            self.config.optimizer,
            dim=self.space.dim,
            x0=x0,
            popsize=self.config.popsize,
            max_iters=self.config.max_iters,
            seed=self.config.seed,
        )

        while not self._stop.is_set() and not opt.done:
            self._resume.wait()
            if self._stop.is_set():
                break
            self._run_generation(opt, on_progress)
            if self.config.mode == "interactive":
                self.pause()  # auto-pause so the user can listen, then resume()

        self.apply_best()
        if on_progress:
            on_progress(self._snapshot(done=True))
        return self.best_patch()

    def _evaluate(self, params: dict[int, int]) -> tuple[float, AudioClip | None]:
        """Probe the hardware for one candidate and score it.

        Returns (loss, clip). Cached candidates return (loss, None) without
        touching the synth. Silent candidates score the penalty loss (silence
        must never look like a match). A probe that fails twice (device
        hiccup) also scores the penalty but is NOT cached, so the run survives
        transient errors without permanently writing off that candidate.
        """
        key = tuple(sorted(params.items()))
        if key in self._eval_cache:
            self._cache_hits += 1
            return self._eval_cache[key], None

        clip: AudioClip | None = None
        for attempt in (0, 1):
            try:
                clip = self.driver.probe(params)
                break
            except Exception:  # noqa: BLE001 - device errors vary by backend
                if attempt == 1:
                    return SILENCE_PENALTY_LOSS, None

        if is_silent(clip):
            l = SILENCE_PENALTY_LOSS
        else:
            feat = extract(clip)
            l = loss(self.target_feat, feat, self.config.weights, scales=self._scales)
        self._eval_cache[key] = l
        return l, clip

    def _run_generation(self, opt, on_progress) -> None:
        solutions = opt.ask()
        losses: list[float] = []
        for x in solutions:
            if self._stop.is_set():
                break
            params = self.space.decode(x)
            l, clip = self._evaluate(params)
            losses.append(l)
            self._evals += 1
            if l < self._best_loss and l < SILENCE_PENALTY_LOSS and clip is not None:
                self._best_loss = l
                self._best_params = params
                self._best_clip = clip
            if on_progress:
                on_progress(self._snapshot(last_clip=clip, last_loss=l))

        # Only report a complete population: CMA-ES requires the full sample
        # set, so a generation cut short by stop() is dropped (best-so-far
        # tracking already happened per candidate above).
        if len(losses) == len(solutions):
            opt.tell(solutions, losses)
        self._iteration += 1
        if on_progress:
            on_progress(self._snapshot(generation_done=True))

    def _snapshot(
        self,
        last_clip: AudioClip | None = None,
        last_loss: float | None = None,
        generation_done: bool = False,
        done: bool = False,
    ) -> Progress:
        return Progress(
            iteration=self._iteration,
            evals=self._evals,
            max_iters=self.config.max_iters,
            best_closeness=self.best_closeness,
            best_loss=self._best_loss,
            best_params=dict(self._best_params),
            last_closeness=closeness(last_loss, self.config.weights) if last_loss is not None else 0.0,
            generation_done=generation_done,
            done=done,
            cache_hits=self._cache_hits,
            target_clip=self.target_clip,
            best_clip=self._best_clip,
            last_clip=last_clip,
        )
