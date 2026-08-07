"""The web↔engine seam, pinned as a typed contract.

The FastAPI routes in :mod:`synth.web.server` reach the live engine through the
module-global ``ENGINE`` (via the ``eng()`` accessor). This module names the
*exact* surface those routes touch as a :class:`~typing.Protocol`, so the engine
can be swapped for a fake in tests against a checked contract rather than an
implicit "whatever ``S1Engine`` happens to expose".

The Protocol is enumerated from the real call sites in ``server.py``; a
conformance test asserts the real :class:`~synth.engine.S1Engine` satisfies it.
Routes are NOT required to funnel through this type this pass — the deliverable
is the pinned contract plus its conformance check.

``@runtime_checkable`` lets ``isinstance(engine, EngineFacade)`` verify member
presence at runtime (it checks names, not signatures).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EngineFacade(Protocol):
    """Every attribute/method the web routes use on the module-global engine.

    Grouped by the sub-objects the routes dot into. The two leading-underscore
    members (``_mido``, ``_tick_audio``) are genuine reaches by ``server.py``
    into engine internals — recorded here so the contract stays truthful; a
    future engine-side pass should promote them to a public surface.
    """

    # ── sub-object state stores the routes dot into ──────────────
    params: Any        # ParamState: .get / .set / .snapshot
    midi: Any          # MidiBackend: .connected / .output_names / .input_names
    monitor: Any       # AudioMonitor: .running / .muted / .gain / .input / .stop
    sequencer: Any     # SequencerEngine: .sequence / .play / .stop / .pause / ...

    # ── scalar attributes read/written by routes ─────────────────
    mode: str          # cockpit mode (C8): "solo" | "logic"
    sync_state: str    # sync chip state (disconnected/connecting/listening/synced)
    audio_auto: bool   # monitor auto-start toggle (monitor start/stop routes)

    # ── methods the routes call ──────────────────────────────────
    def status(self) -> dict: ...
    def monitor_status(self) -> dict: ...
    def transport_status(self) -> dict: ...

    def set_param(self, cc: int, value: int, source: str = ...) -> int: ...
    def load_values(self, values: dict[int, int], source: str = ...) -> None: ...
    def push_all(self) -> None: ...

    def note_on(self, note: int, velocity: int = ...) -> bool: ...
    def note_off(self, note: int) -> bool: ...
    def all_notes_off(self) -> bool: ...
    def select_pattern(self, bank: int, slot: int) -> int: ...

    def set_mode(self, mode: str) -> str: ...

    def subscribe(self, callback: Any) -> None: ...
    def unsubscribe(self, callback: Any) -> None: ...
    def publish(self, event: dict) -> None: ...

    def start(self) -> None: ...
    def stop(self) -> None: ...

    # ── private reaches the routes still make (see class docstring) ──
    _mido: Any                     # logic_transport passes eng()._mido to logic
    def _tick_audio(self) -> None: ...   # monitor_start re-scans via this
