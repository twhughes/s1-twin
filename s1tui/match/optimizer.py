"""Derivative-free optimizers over the normalized parameter vector.

The synth is a black box reached through hardware, so gradients are unavailable
and each evaluation is expensive (~1-2 s). The default is CMA-ES — sample-efficient
and well suited to ~30 continuous dimensions. Everything goes through the
:class:`Optimizer` protocol so other strategies (random search, Bayesian, RL) can
be dropped in without touching the rest of the engine.
"""

from __future__ import annotations

from typing import Protocol

import numpy as np


class Optimizer(Protocol):
    """Ask/tell interface: request candidates, report their losses."""

    def ask(self) -> list[np.ndarray]: ...
    def tell(self, solutions: list[np.ndarray], losses: list[float]) -> None: ...
    @property
    def best(self) -> tuple[np.ndarray, float]: ...
    @property
    def done(self) -> bool: ...


class RandomOptimizer:
    """Uniform random search — a baseline and a dependency-free fallback."""

    def __init__(self, dim: int, popsize: int = 8, max_iters: int = 50, seed: int | None = None):
        self.dim = dim
        self.popsize = popsize
        self.max_iters = max_iters
        self._rng = np.random.default_rng(seed)
        self._iters = 0
        self._best_x: np.ndarray | None = None
        self._best_f = float("inf")

    def ask(self) -> list[np.ndarray]:
        return [self._rng.random(self.dim) for _ in range(self.popsize)]

    def tell(self, solutions: list[np.ndarray], losses: list[float]) -> None:
        self._iters += 1
        for x, f in zip(solutions, losses):
            if f < self._best_f:
                self._best_f, self._best_x = f, np.array(x)

    @property
    def best(self) -> tuple[np.ndarray, float]:
        x = self._best_x if self._best_x is not None else np.full(self.dim, 0.5)
        return x, self._best_f

    @property
    def done(self) -> bool:
        return self._iters >= self.max_iters


class CMAESOptimizer:
    """CMA-ES wrapper (requires the ``cma`` package)."""

    def __init__(
        self,
        dim: int,
        x0: np.ndarray | None = None,
        sigma0: float = 0.3,
        popsize: int | None = None,
        max_iters: int = 50,
        seed: int | None = None,
    ):
        import cma

        x0 = np.full(dim, 0.5) if x0 is None else np.clip(x0, 0.0, 1.0)
        opts = {"bounds": [0.0, 1.0], "maxiter": max_iters, "verbose": -9}
        if popsize is not None:
            opts["popsize"] = popsize
        if seed is not None:
            opts["seed"] = seed
        self._es = cma.CMAEvolutionStrategy(list(x0), sigma0, opts)

    def ask(self) -> list[np.ndarray]:
        return [np.asarray(x) for x in self._es.ask()]

    def tell(self, solutions: list[np.ndarray], losses: list[float]) -> None:
        self._es.tell([list(s) for s in solutions], list(losses))

    @property
    def best(self) -> tuple[np.ndarray, float]:
        r = self._es.result
        x = np.asarray(r.xbest) if r.xbest is not None else np.full(self._es.N, 0.5)
        f = float(r.fbest) if r.fbest is not None else float("inf")
        return x, f

    @property
    def done(self) -> bool:
        return bool(self._es.stop())


def make_optimizer(kind: str, dim: int, x0: np.ndarray | None = None, **kw) -> Optimizer:
    """Factory: ``"cma"`` (default) or ``"random"``."""
    if kind == "random":
        return RandomOptimizer(dim, **{k: v for k, v in kw.items() if k != "sigma0"})
    return CMAESOptimizer(dim, x0=x0, **kw)
