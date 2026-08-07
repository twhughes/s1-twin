"""The instrument backend protocol — canon in ``music/CONTRACTS.md`` §6.

This module *pins* (does not restate) music's backend contract so synth's own
engines can be type-checked against it. chassis-spec C9 names the two tiers:

- **Tier 1 — the S-1 dialect.** An engine speaks tier 1 when it lives in the
  54-parameter space of ``s1.json`` and implements the five methods of
  :class:`InstrumentBackend`: ``connect / disconnect / push_all / send /
  on_incoming``. Params split **k** (continuous, gradient-eligible) / **s**
  (discrete, enumerated). Tier-1 speakers today: the S-1 hardware (via
  :class:`~synth.midi_backend.MidiBackend`) and the twin.

- **The optional differentiable tier.** A tier-1 engine *may additionally*
  expose the differentiable forward model ``render(k, s, note) -> audio``
  (:class:`DifferentiableBackend`). The twin does; the hardware S-1 does not —
  its twin stands in, verified by hardware probes. ``render`` is split out into
  a sub-protocol precisely so the hardware backend conforms to
  :class:`InstrumentBackend` without pretending to a forward model it lacks.

Optimizers and cockpit features see an instrument ONLY through these methods
(+ the (k, s) schema in ``s1.json``) — never through device specifics.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable


@runtime_checkable
class InstrumentBackend(Protocol):
    """The five-method instrument backend every tier-1 engine implements.

    Pinned to ``music/CONTRACTS.md`` §6. ``@runtime_checkable`` so tests can
    assert structural conformance with ``isinstance``. A concrete backend adds
    its own device policy on top (the S-1: CC map + explicit push-all sync +
    Program Change pattern select) but must honour these signatures.
    """

    def connect(self, port: str) -> None:
        """Open the connection to ``port`` (a named transport endpoint).

        Listen-only by contract: opening a port never pushes patch state to the
        device (that is :meth:`push_all`). Opening replaces any prior
        connection. Never raises for a *missing* device on the input side —
        input feedback is best-effort.
        """
        ...

    def disconnect(self) -> None:
        """Close the connection and mark the backend disconnected.

        Idempotent: calling it while already disconnected is a no-op. After it
        returns, :meth:`send` reports ``False`` until the next :meth:`connect`.
        """
        ...

    def push_all(self, patch: dict[int, int]) -> None:
        """Push a whole patch to the device — the EXPLICIT full-state sync.

        ``patch`` maps parameter id (the S-1's CC number) to value. This
        overwrites whatever the hardware currently holds, so it is never called
        implicitly on connect. NOTE: for the S-1 the authoritative patch-push
        also lives on the engine (:meth:`synth.engine.S1Engine.push_all`), which
        owns app state and marks the session SYNCED; the backend method is the
        thin transport half of that act.
        """
        ...

    def send(self, param: int, value: int) -> bool:
        """Send one parameter change (one CC) to the device.

        Returns ``True`` if the message went out, ``False`` if the backend is
        disconnected or the port vanished mid-send. Never raises on a device
        unplug — callers on any thread must survive it.
        """
        ...

    def on_incoming(self, cb: Callable[[int, int], None]) -> None:
        """Register ``cb`` to receive incoming ``(param, value)`` changes.

        Fires when the device reports a parameter move (a physical knob turn on
        the S-1). Multiple callbacks may be registered; each is invoked with the
        parameter id and its new value. The backend drives them from its poll
        loop / input callback thread.
        """
        ...


@runtime_checkable
class DifferentiableBackend(InstrumentBackend, Protocol):
    """A tier-1 backend that ALSO exposes the differentiable forward model.

    The optional tier of chassis-spec C9 / music §6. The twin implements this;
    the hardware S-1 backend does not (it conforms to :class:`InstrumentBackend`
    only, and its twin stands in for ``render`` under hardware verification).
    """

    def render(self, k: Any, s: Any, note: int) -> Any:
        """Render ``note`` under continuous params ``k`` and discrete params
        ``s`` to audio — the differentiable forward model (torch, ``[dsp]``
        extra). Gradient-eligible in ``k``; ``s`` is optimized by enumeration.
        """
        ...
