"""Textual TUI app for the Roland S-1."""

from __future__ import annotations

from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.widgets import Footer, Label, TabbedContent, TabPane

from . import theme as T
from .midi_backend import MidiBackend
from .patches import load_patch, sanitize_name, save_patch
from .schema import S1_PARAMS, SeqParam, seq_param_by_key
from .screens import (
    LoadMidiScreen,
    LoadPatchScreen,
    PortSelectScreen,
    SaveMidiScreen,
    SavePatchScreen,
)
from .sequence import Sequence, load_midi, save_midi
from .sequencer_engine import SequencerEngine
from .state import ParamState
from .theme import SYNTHWAVE
from .views import MenuView, PanelView, SequencerView
from .widgets import CCSelector, CCSlider, CCToggle
from .widgets.brand_header import BrandHeader
from .widgets.param_widget import ParamChanged
from .widgets.piano_roll import PianoRoll
from .widgets.wave_scope import WaveScope


class S1App(App):
    """Roland S-1 Terminal Controller."""

    TITLE = "S-1 TUI"
    SUB_TITLE = "Roland S-1 Synth Controller"

    CSS = """
    Screen {
        background: $background;
    }
    Tabs {
        background: $surface;
    }
    #status-bar {
        dock: bottom;
        height: 1;
        background: $surface;
        color: $text;
        padding: 0 1;
    }
    #status-bar .status-port {
        width: 1fr;
    }
    #status-bar .status-ch {
        width: 14;
        text-align: right;
    }
    TabPane {
        padding: 0;
    }
    """

    BINDINGS = [
        Binding("c", "connect", "Connect"),
        Binding("s", "save_patch", "Save"),
        Binding("l", "load_patch", "Load"),
        Binding("r", "randomize", "Random"),
        Binding("d", "defaults", "Defaults"),
        Binding("0", "zero_all", "Zero"),
        Binding("u", "undo", "Undo", show=False),
        Binding("t", "test_note", "Test"),
        Binding("m", "load_midi", "MIDI"),
        Binding("M", "save_midi", "Save MIDI", show=False),
        Binding("exclamation_mark", "panic", "Panic", show=False),
        Binding("space", "transport_toggle", "Play/Stop", show=False),
        Binding("p", "transport_play", "Play", show=False),
        Binding("x", "transport_stop", "Stop", show=False),
        Binding("j", "focus_next_slider", "Next", show=False),
        Binding("k", "focus_prev_slider", "Prev", show=False),
        Binding("down", "focus_next_slider", "Next", show=False),
        Binding("up", "focus_prev_slider", "Prev", show=False),
        Binding("ctrl+c", "quit", "Quit", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.midi = MidiBackend()
        self.state = ParamState()
        # All param mutations flow through the state store; this listener is
        # the single sync path to widgets, scope, and hardware.
        self.state.add_listener(self._on_state_changed)
        self._widgets: dict[int, CCSlider | CCToggle | CCSelector] = {}
        self._seq_widgets: dict[str, CCSlider | CCToggle | CCSelector] = {}
        self._suppress_midi_send = False
        # True only while the raw MIDI clock is running with no sequence
        # loaded; sequence playback state lives on the engine alone.
        self._clock_running = False
        self._undo_stack: list[dict[int, int]] = []
        self.engine: SequencerEngine | None = None
        self._midi_file_path: Path | None = None

    def compose(self) -> ComposeResult:
        yield BrandHeader()
        with TabbedContent(id="top-tabs"):
            with TabPane("Panel", id="tab-panel"):
                yield PanelView()
            with TabPane("Menu", id="tab-menu"):
                yield MenuView()
            with TabPane("Seq", id="tab-seq"):
                yield SequencerView()
        with Horizontal(id="status-bar"):
            yield Label("", classes="status-port", id="status-port")
            yield Label("", classes="status-ch", id="status-ch")
        yield Footer()

    def on_mount(self) -> None:
        # Apply the synthwave-neon theme
        self.register_theme(SYNTHWAVE)
        self.theme = "synthwave"
        # Index all param widgets by CC number (or key for seq params)
        for widget in self.query("CCSlider, CCToggle, CCSelector"):
            if hasattr(widget, "param"):
                param = widget.param
                if isinstance(param, SeqParam):
                    self._seq_widgets[param.key] = widget
                    widget.value = param.default
                else:
                    self._widgets[param.cc] = widget
                    widget.value = self.state.get(param.cc)
        # Auto-connect to first available S-1 port
        self._auto_connect()
        self._update_status()
        self._refresh_scope()
        # Start MIDI polling
        self.set_interval(0.05, self._poll_midi_input)

    # ── Oscilloscope ──

    _OSC_CCS = frozenset({15, 19, 20, 21})  # PW, Square, Saw, Sub levels

    def _refresh_scope(self) -> None:
        """Push the current oscillator mix to the scope widget."""
        try:
            scope = self.query_one("#wave-scope", WaveScope)
        except NoMatches:
            return
        scope.set_mix(
            saw=self.state.get(20) / 127.0,
            square=self.state.get(19) / 127.0,
            sub=self.state.get(21) / 127.0,
            pw=self.state.get(15) / 127.0,
        )

    def _auto_connect(self) -> None:
        """Try to auto-connect to an S-1 or the first available port."""
        if self.midi.connected:
            return
        try:
            ports = MidiBackend.list_output_ports()
        except Exception as e:  # rtmidi raises backend-specific errors
            self.notify(f"MIDI unavailable: {e}", severity="warning")
            return
        if not ports:
            return
        # Prefer a port with "S-1" in the name (case-insensitive), else first
        preferred = [p for p in ports if "s-1" in p.lower() or "s1" in p.lower()]
        last_error: Exception | None = None
        for port in preferred + [ports[0]]:
            try:
                self.midi.connect(port)
                self.notify(f"Connected: {port}", severity="information")
                return
            except Exception as e:  # rtmidi raises backend-specific errors
                last_error = e
        if last_error is not None:
            self.notify(f"Auto-connect failed: {last_error}", severity="warning")

    def _update_status(self) -> None:
        port_label = self.query_one("#status-port", Label)
        ch_label = self.query_one("#status-ch", Label)
        if self.engine and self.engine.playing:
            step = self.engine.position + 1
            bpm = self.engine.sequence.bpm
            transport = (
                f"[b {T.MAGENTA}]▶ PLAY[/]  [{T.MUTED}]step[/] {step}  "
                f"[b {T.CYAN}]{bpm:.0f} BPM[/]"
            )
        elif self._clock_running:
            transport = f"[b {T.MAGENTA}]▶ PLAY[/]"
        else:
            transport = f"[{T.MUTED}]■ STOP[/]"
        if self.midi.connected:
            port_label.update(f"[{T.LIME}]◉[/] [b]{self.midi.port_name}[/]   {transport}")
        else:
            port_label.update(
                f"[{T.MUTED}]○ no midi[/]  [b {T.CYAN}]c[/] connect   {transport}"
            )
        ch_label.update(f"[{T.MUTED}]CH[/] [b {T.GOLD}]{self.midi.channel + 1}[/]")

    def _get_piano_roll(self) -> PianoRoll | None:
        """Get the piano roll widget if it exists."""
        try:
            return self.query_one("#piano-roll", PianoRoll)
        except NoMatches:
            return None

    def _ensure_engine(self) -> SequencerEngine:
        """Get or create the sequencer engine."""
        if self.engine is None:
            piano_roll = self._get_piano_roll()
            seq = piano_roll.sequence if piano_roll else Sequence()
            self.engine = SequencerEngine(self.midi, seq)
            self.engine.set_position_callback(self._on_playhead_advance)
        return self.engine

    def _on_playhead_advance(self, step: int) -> None:
        """Called from engine thread on each step — update piano roll playhead."""
        self.call_from_thread(self._update_playhead, step)

    def _update_playhead(self, step: int) -> None:
        """Update piano roll playhead on the main thread."""
        piano_roll = self._get_piano_roll()
        if piano_roll:
            piano_roll.playhead = step
        self._update_status()

    # ── MIDI port connection ──

    def action_connect(self) -> None:
        ports = MidiBackend.list_output_ports()
        self.push_screen(PortSelectScreen(ports), self._on_port_selected)

    def _on_port_selected(self, port_name: str | None) -> None:
        if port_name:
            try:
                self.midi.connect(port_name)
                self.notify(f"Connected to {port_name}", severity="information")
            except Exception as e:
                self.notify(f"Failed: {e}", severity="error")
        self._update_status()

    # ── Widget → MIDI (unified handler for all widget types) ──

    @on(ParamChanged)
    def _on_param_changed(self, event: ParamChanged) -> None:
        # SeqParam widgets don't have CCs — they steer the sequencer engine
        if isinstance(event.param, SeqParam):
            self._apply_seq_param(event.param.key, event.cc_value)
            return
        # The state listener (_on_state_changed) syncs widget/scope/hardware
        self.state.set(event.param.cc, event.cc_value, source="ui")

    def _on_state_changed(self, cc: int, value: int, source: str) -> None:
        """Single sync path for every param change, whatever its origin.

        Programmatic widget.value assignment does not re-post ParamChanged
        (only user interaction does), so this cannot loop.
        """
        widget = self._widgets.get(cc)
        if widget is not None:
            widget.value = value
        if cc in self._OSC_CCS:
            self._refresh_scope()
        # Echo to hardware unless the change *came from* the hardware
        if source != "midi" and not self._suppress_midi_send:
            self.midi.send_cc(cc, value)

    def _apply_seq_param(self, key: str, value: int) -> None:
        """Route a sequencer-card change to the engine / sequence (live)."""
        if key == "seq_tempo":
            self._sync_tempo_from_widget(value)
        elif key == "seq_gate":
            # Gate length ratio: 0 -> barely a blip, 127 -> full duration
            self._ensure_engine().gate = 0.05 + (value / 127.0) * 0.95
        elif key == "seq_shuffle":
            # Swing 50-75%: delay offbeats by up to half a step
            self._ensure_engine().shuffle = (value / 127.0) * 0.5
        elif key == "last_step":
            self._ensure_engine().last_step = max(1, value)
        elif key == "master_prob":
            self._ensure_engine().probability = value / 127.0
        elif key == "seq_scale":
            param = seq_param_by_key("seq_scale")
            resolution = param.label_for_value(value) if param else "1/16"
            self._set_step_resolution(resolution)

    def _set_step_resolution(self, resolution: str) -> None:
        piano_roll = self._get_piano_roll()
        if piano_roll:
            piano_roll.sequence.step_resolution = resolution
            piano_roll.refresh()
        if self.engine:
            self.engine.sequence.step_resolution = resolution

    # ── Tempo sync ──

    @staticmethod
    def _cc_to_bpm(cc_val: int) -> float:
        """Map 0-127 to 20-300 BPM (S-1 tempo range)."""
        return 20.0 + (cc_val / 127.0) * 280.0

    @staticmethod
    def _bpm_to_cc(bpm: float) -> int:
        """Map 20-300 BPM back to 0-127."""
        return max(0, min(127, round((bpm - 20.0) / 280.0 * 127.0)))

    def _sync_tempo_from_widget(self, cc_val: int) -> None:
        """Update sequence and engine BPM when seq_tempo widget changes."""
        bpm = self._cc_to_bpm(cc_val)
        piano_roll = self._get_piano_roll()
        if piano_roll:
            piano_roll.sequence.bpm = bpm
            piano_roll.refresh()
        if self.engine:
            self.engine.sequence.bpm = bpm

    def _sync_tempo_to_widget(self, bpm: float) -> None:
        """Update the seq_tempo widget to reflect the sequence BPM."""
        cc_val = self._bpm_to_cc(bpm)
        widget = self._seq_widgets.get("seq_tempo")
        if widget:
            widget.value = cc_val

    # ── Patch management ──

    def _current_values(self) -> dict[int, int]:
        return self.state.snapshot()

    def _apply_values(self, values: dict[int, int], send_midi: bool = True) -> None:
        self._suppress_midi_send = not send_midi
        try:
            self.state.load(values, source="patch")
        finally:
            self._suppress_midi_send = False

    def action_save_patch(self) -> None:
        self.push_screen(SavePatchScreen(), self._on_save_patch)

    def _on_save_patch(self, name: str | None) -> None:
        if name:
            path = save_patch(name, self._current_values())
            self.notify(f"Saved: {path.name}", severity="information")

    def action_load_patch(self) -> None:
        self.push_screen(LoadPatchScreen(), self._on_load_patch)

    def _on_load_patch(self, path: Path | None) -> None:
        if path:
            values = load_patch(path)
            self._apply_values(values)
            self.notify(f"Loaded: {path.stem}", severity="information")

    # ── MIDI file loading ──

    def action_load_midi(self) -> None:
        self.push_screen(LoadMidiScreen(), self._on_midi_loaded)

    def _on_midi_loaded(self, path: Path | None) -> None:
        if path is None:
            return
        try:
            seq = load_midi(path)
        except Exception as e:
            self.notify(f"Failed to load MIDI: {e}", severity="error")
            return

        self._midi_file_path = path

        # Update piano roll
        piano_roll = self._get_piano_roll()
        if piano_roll:
            piano_roll.set_sequence(seq)

        # Update engine
        engine = self._ensure_engine()
        engine.sequence = seq

        # Sync tempo widget to MIDI file's BPM
        self._sync_tempo_to_widget(seq.bpm)

        # Update file info label
        try:
            file_label = self.query_one("#file-info", Label)
            file_label.update(
                f"[b]{path.name}[/b] — {len(seq.notes)} notes, "
                f"{seq.steps} steps, {seq.bpm:.0f} BPM"
            )
        except NoMatches:
            pass

        if seq.dropped_notes:
            self.notify(
                f"{seq.dropped_notes} notes past the 64-step limit were dropped",
                severity="warning",
            )
        self.notify(f"Loaded: {path.name} ({len(seq.notes)} notes)", severity="information")

    # ── MIDI file saving ──

    MIDI_DIR = Path.home() / ".s1tui" / "midi"

    def action_save_midi(self) -> None:
        self.push_screen(SaveMidiScreen(), self._on_save_midi)

    def _on_save_midi(self, name: str | None) -> None:
        if not name:
            return
        piano_roll = self._get_piano_roll()
        seq = piano_roll.sequence if piano_roll else (self.engine.sequence if self.engine else None)
        if seq is None or not seq.notes:
            self.notify("Nothing to save — the piano roll is empty", severity="warning")
            return
        try:
            name = sanitize_name(name)
        except ValueError as e:
            self.notify(f"Bad name: {e}", severity="error")
            return
        # Save next to the file that was loaded, else to the s1tui MIDI dir
        directory = self._midi_file_path.parent if self._midi_file_path else self.MIDI_DIR
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.mid"
        try:
            save_midi(seq, path)
        except OSError as e:
            self.notify(f"Save failed: {e}", severity="error")
            return
        self._midi_file_path = path
        self.notify(f"Saved: {path}", severity="information")

    # ── Utilities ──

    _UNDO_DEPTH = 20

    def _push_undo(self) -> None:
        """Snapshot the current params so a destructive action can be undone."""
        self._undo_stack.append(self.state.snapshot())
        if len(self._undo_stack) > self._UNDO_DEPTH:
            self._undo_stack.pop(0)

    def action_undo(self) -> None:
        if not self._undo_stack:
            self.notify("Nothing to undo")
            return
        self._apply_values(self._undo_stack.pop())
        self.notify("Restored previous parameters")

    def action_randomize(self) -> None:
        import random
        self._push_undo()
        values = {p.cc: random.randint(0, 127) for p in S1_PARAMS}
        self._apply_values(values)
        self.notify("Randomized all parameters ([b]u[/] to undo)", severity="warning")

    def action_zero_all(self) -> None:
        self._push_undo()
        values = {p.cc: 0 for p in S1_PARAMS}
        self._apply_values(values)
        self.notify("All parameters set to 0 ([b]u[/] to undo)")

    def action_defaults(self) -> None:
        self._push_undo()
        values = {p.cc: p.default for p in S1_PARAMS}
        self._apply_values(values)
        self.notify("All parameters set to defaults ([b]u[/] to undo)")

    # ── Transport ──

    @property
    def _transport_active(self) -> bool:
        """Single source of truth: the engine owns sequence playback; the
        _clock_running flag only covers the raw-MIDI-clock fallback."""
        engine_active = self.engine is not None and (self.engine.playing or self.engine.paused)
        return engine_active or self._clock_running

    def action_transport_toggle(self) -> None:
        if self._transport_active:
            self.action_transport_stop()
        else:
            self.action_transport_play()

    def action_transport_play(self) -> None:
        engine = self._ensure_engine()
        # Sync engine with piano roll sequence
        piano_roll = self._get_piano_roll()
        if piano_roll and engine.sequence is not piano_roll.sequence:
            engine.sequence = piano_roll.sequence
        if engine.sequence.notes:
            engine.play()
        else:
            # No sequence loaded — fall back to raw MIDI start
            self.midi.send_start()
            self._clock_running = True
        self._update_status()

    def action_transport_stop(self) -> None:
        if self.engine and (self.engine.playing or self.engine.paused):
            self.engine.stop()
            # Reset playhead
            piano_roll = self._get_piano_roll()
            if piano_roll:
                piano_roll.playhead = -1
        else:
            self.midi.send_stop()
        self._clock_running = False
        self._update_status()

    def action_panic(self) -> None:
        """Kill anything sounding: engine note-offs plus CC 123."""
        if self.engine:
            self.engine.all_notes_off()
        self.midi.all_notes_off()
        self.notify("Panic — all notes off", severity="warning")

    def action_test_note(self) -> None:
        self._play_test_note()

    @work(thread=True)
    def _play_test_note(self) -> None:
        import time
        self.midi.send_note_on(60, 100)
        time.sleep(0.5)
        self.midi.send_note_off(60)

    def action_quit(self) -> None:
        if self.engine:
            self.engine.stop()
        self.midi.close()  # sends all-notes-off before releasing the port
        self.exit()

    # ── Navigation ──

    def action_focus_next_slider(self) -> None:
        self.screen.focus_next("CCSlider, CCToggle, CCSelector, PianoRoll")

    def action_focus_prev_slider(self) -> None:
        self.screen.focus_previous("CCSlider, CCToggle, CCSelector, PianoRoll")

    # ── MIDI input polling ──

    def _poll_midi_input(self) -> None:
        if not self.midi.connected:
            return
        for msg in self.midi.poll_input():
            if msg.type == "control_change" and msg.channel == self.midi.channel:
                # The state listener syncs widget + scope (no hardware echo)
                self.state.set(msg.control, msg.value, source="midi")
