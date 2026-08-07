"""The structural contracts (Protocols) the match engine is built around.

Every hardware- or algorithm-specific piece the engine talks to sits behind one
of these Protocols, so a later phase can swap the implementation without touching
the orchestration:

- :class:`Driver` — the thing that applies CC values and records a probe. The real
  :class:`~synth.match.driver.SynthDriver` and the test ``FakeDriver`` both conform.
- :class:`FeatureExtractor` — the ``clip -> Features`` step (``features.extract``).
  The digital twin / v3 feature set drops in here.
- :class:`DistanceMetric` — the ``loss``/``closeness`` scoring (``distance``). The
  perceptual/twin metric drops in here.
- :class:`Matcher` — the target -> :class:`MatchResult` benchmark interface. This is
  **re-exported** from :mod:`synth.match.corpus`, which owns the definition; there is
  exactly ONE ``Matcher`` contract and ``benchmark()`` scores anything that conforms.

Nothing here imports hardware or heavy deps at module load beyond what ``corpus``
already needs; importing this module is cheap.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# The Matcher contract + its result type live in corpus.py (which also ships
# benchmark()). Re-export them so callers have a single import home for the whole
# match contract surface without duplicating the definition.
from .capture import AudioClip
from .corpus import Matcher, MatcherFn, MatchResult, Target
from .distance import Weights
from .features import Features

__all__ = [
    "Driver",
    "FeatureExtractor",
    "DistanceMetric",
    "Matcher",
    "MatcherFn",
    "MatchResult",
    "Target",
]


@runtime_checkable
class Driver(Protocol):
    """The one hardware-touching seam: apply parameters, record a probe.

    Implementations: the real :class:`~synth.match.driver.SynthDriver` (MIDI + audio)
    and the ``FakeDriver`` used across the tests. A driver optionally carries a
    ``note`` attribute (the probe pitch); the session tunes it to the target's
    detected pitch, but it is not part of this minimal contract.
    """

    def apply(self, params: dict[int, int]) -> None:
        """Send ``params`` (CC -> value) to the instrument."""
        ...

    def probe(self, params: dict[int, int] | None = None) -> AudioClip:
        """Apply ``params`` (if given), play the probe note, return the recording."""
        ...

    def calibrate(self) -> float:
        """Measure and store note-to-audio latency in seconds; return it."""
        ...


@runtime_checkable
class FeatureExtractor(Protocol):
    """``clip -> Features``. The analysis front-end (``features.extract``).

    The digital twin / match-v3 feature set conforms here so it can replace the
    current extractor without the session or the metric changing.
    """

    def __call__(self, clip: AudioClip) -> Features:
        ...


@runtime_checkable
class DistanceMetric(Protocol):
    """The scoring seam: a feature-space ``loss`` and its ``closeness`` mapping.

    The :mod:`synth.match.distance` module conforms structurally (it exposes both
    ``loss`` and ``closeness``). A perceptual or twin-calibrated metric drops in
    here — the v3 swap point for how "close" two sounds are.
    """

    def loss(
        self,
        target: Features,
        cand: Features,
        w: Weights = Weights(),
        scales: dict[str, float] | None = None,
    ) -> float:
        ...

    def closeness(self, loss_value: float, w: Weights = Weights()) -> float:
        ...
