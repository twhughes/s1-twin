"""Textual TUI app for the Roland S-1."""

from __future__ import annotations

from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Footer, Header, Label, TabbedContent, TabPane

from .midi_backend import MidiBackend
from .patches import save_patch, load_patch
from .schema import S1_PARAMS, SEQ_PARAMS, SeqParam, param_by_cc
from .state import ParamState
from .screens import PortSelectScreen, SavePatchScreen, LoadPatchScreen, LoadMidiScreen
from .sequence import Sequence, load_midi, save_midi
from .sequencer_engine import SequencerEngine
from .views import PanelView, MenuView, SequencerView
from .widgets import CCSlider, CCToggle, CCSelector
from .widgets.piano_roll import PianoRoll


class S1App(App):
    """Roland S-1 Terminal Controller."""

    TITLE = "S-1 TUI"
    SUB_TITLE = "Roland S-1 Synth Controller"

    CSS = """
    Screen {
        background: $surface-darken-1;
    }
    #status-bar {
        dock: bottom;
        height: 1;
        background: $primary-background;
        color: $text;
        padding: 0 2;
    }
    #status-bar .status-port {
        width: 1fr;
    }
    #status-bar .status-ch {
        width: 12;
        text-align: right;
    }
    TabPane {
        padding: 0;
    }
    CCSlider:focus, CCToggle:focus, CCSelector:focus {
        background: $boost;
    }
    CCSlider:focus .slider-label,
    CCToggle:focus .toggle-label,
    CCSelector:focus .selector-label {
        color: $text;
        text-style: bold;
    }
    """

    BINDINGS = [
        Binding("c", "connect", "Connect"),
        Binding("s", "save_patch", "Save"),
        Binding("l", "load_patch", "Load"),
        Binding("r", "randomize", "Random"),
        Binding("d", "defaults", "Defaults"),
        Binding("0", "zero_all", "Zero"),
        Binding("t", "test_note", "Test"),
        Binding("m", "load_midi", "MIDI"),
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
        self._widgets: dict[int, CCSlider | CCToggle | CCSelector] = {}
        self._seq_widgets: dict[str, CCSlider | CCToggle | CCSelector] = {}
        self._suppress_midi_send = False
        self._transport_playing = False
        self.engine: SequencerEngine | None = None
        self._midi_file_path: Path | None = None

    def compose(self) -> ComposeResult:
        yield Header()
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
        # Start MIDI polling
        self.set_interval(0.05, self._poll_midi_input)

    def _auto_connect(self) -> None:
        """Try to auto-connect to an S-1 or the first available port."""
        if self.midi.connected:
            return
        try:
            ports = MidiBackend.list_output_ports()
        except Exception:
            return
        if not ports:
            return
        # Prefer a port with "S-1" in the name (case-insensitive)
        for port in ports:
            if "s-1" in port.lower() or "s1" in port.lower():
                try:
                    self.midi.connect(port)
                    self.notify(f"Connected: {port}", severity="information")
                    return
                except Exception:
                    pass
        # Fall back to first port
        try:
            self.midi.connect(ports[0])
            self.notify(f"Connected: {ports[0]}", severity="information")
        except Exception:
            pass

    def _update_status(self) -> None:
        port_label = self.query_one("#status-port", Label)
        ch_label = self.query_one("#status-ch", Label)
        if self.engine and self.engine.playing:
            step = self.engine.position + 1
            bpm = self.engine.sequence.bpm
            transport = f"▶ Playing step {step} ({bpm:.0f} BPM)"
        elif self._transport_playing:
            transport = "▶ Playing"
        else:
            transport = "■ Stopped"
        if self.midi.connected:
            port_label.update(f"✓ {self.midi.port_name}  {transport}")
        else:
            port_label.update("⚡ No MIDI — [b]c[/b] to connect")
        ch_label.update(f"CH {self.midi.channel + 1}")

    def _get_piano_roll(self) -> PianoRoll | None:
        """Get the piano roll widget if it exists."""
        try:
            return self.query_one("#piano-roll", PianoRoll)
        except Exception:
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

    @on(CCSlider.Changed)
    @on(CCToggle.Changed)
    @on(CCSelector.Changed)
    def _on_param_changed(self, event: CCSlider.Changed | CCToggle.Changed | CCSelector.Changed) -> None:
        # SeqParam widgets don't have CCs — skip MIDI CC send
        if isinstance(event.param, SeqParam):
            if event.param.key == "seq_tempo":
                self._sync_tempo_from_widget(event.cc_value)
            return
        self.state.set(event.param.cc, event.cc_value, source="ui")
        if not self._suppress_midi_send:
            self.midi.send_cc(event.param.cc, event.cc_value)

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
        for cc, val in values.items():
            self.state.set(cc, val, source="patch")
            if cc in self._widgets:
                self._widgets[cc].value = val
            if send_midi:
                self.midi.send_cc(cc, val)
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
        except Exception:
            pass

        self.notify(f"Loaded: {path.name} ({len(seq.notes)} notes)", severity="information")

    # ── Utilities ──

    def action_randomize(self) -> None:
        import random
        values = {p.cc: random.randint(0, 127) for p in S1_PARAMS}
        self._apply_values(values)
        self.notify("Randomized all parameters", severity="warning")

    def action_zero_all(self) -> None:
        values = {p.cc: 0 for p in S1_PARAMS}
        self._apply_values(values)
        self.notify("All parameters set to 0")

    def action_defaults(self) -> None:
        values = {p.cc: p.default for p in S1_PARAMS}
        self._apply_values(values)
        self.notify("All parameters set to defaults")

    # ── Transport ──

    def action_transport_toggle(self) -> None:
        if self._transport_playing:
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
        self._transport_playing = True
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
        self._transport_playing = False
        self._update_status()

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
        self.midi.disconnect()
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
        messages = self.midi.poll_input()
        for msg in messages:
            if msg.type == "control_change" and msg.channel == self.midi.channel:
                cc = msg.control
                value = msg.value
                self.state.set(cc, value, source="midi")
                if cc in self._widgets:
                    self._widgets[cc].value = value
