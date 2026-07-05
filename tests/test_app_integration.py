"""Integration tests for the full app using Textual's test runner."""

from unittest.mock import MagicMock, patch

import pytest

from s1tui.app import S1App
from s1tui.schema import S1_PARAMS, SEQ_PARAMS
from s1tui.sequence import Note, Sequence
from s1tui.widgets.piano_roll import PianoRoll


@pytest.fixture
def mock_midi():
    """Patch MidiBackend so no real MIDI hardware is needed."""
    with patch("s1tui.app.MidiBackend") as MockMidi:
        instance = MagicMock()
        instance.connected = False
        instance.port_name = None
        instance.channel = 2  # Channel 3 (0-indexed)
        instance.poll_input.return_value = []
        MockMidi.return_value = instance
        MockMidi.list_output_ports.return_value = ["Test Port"]
        yield instance


@pytest.mark.asyncio
async def test_app_launches(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)):
        # App should render without errors
        assert app.title == "S-1 TUI"


@pytest.mark.asyncio
async def test_panel_tab_is_default(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)):
        # Panel view should be visible by default
        panel_views = app.query("PanelView")
        assert len(panel_views) > 0


@pytest.mark.asyncio
async def test_panel_view_has_widgets(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)):
        # Should have param widgets
        sliders = app.query("CCSlider")
        toggles = app.query("CCToggle")
        selectors = app.query("CCSelector")
        total = len(sliders) + len(toggles) + len(selectors)
        # Panel shows 19 params, Menu shows 35, but SHIFT params (2) appear in both views
        assert total >= 54, f"Expected at least 54 widgets, got {total}"


@pytest.mark.asyncio
async def test_menu_view_exists(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)):
        menu_views = app.query("MenuView")
        assert len(menu_views) > 0


@pytest.mark.asyncio
async def test_status_bar_shows_not_connected(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)):
        label = app.query_one("#status-port")
        # Check the label's update content (textual 8+ uses _content)
        text = repr(label._content) if hasattr(label, "_content") else str(label.render())
        assert "no midi" in text.lower()


@pytest.mark.asyncio
async def test_widget_index_built(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)):
        # _widgets dict should be populated after mount
        assert len(app._widgets) == 54


@pytest.mark.asyncio
async def test_randomize_changes_values(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)) as pilot:
        # Get initial snapshot
        initial = app.state.snapshot()
        # Randomize
        app.action_randomize()
        await pilot.pause()
        after = app.state.snapshot()
        # At least some values should differ (astronomically unlikely all stay same)
        changed = sum(1 for cc in initial if initial[cc] != after[cc])
        assert changed > 10


@pytest.mark.asyncio
async def test_zero_all(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)) as pilot:
        app.action_zero_all()
        await pilot.pause()
        for cc, val in app.state.snapshot().items():
            assert val == 0, f"CC {cc} should be 0, got {val}"


@pytest.mark.asyncio
async def test_defaults(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)) as pilot:
        # First zero everything
        app.action_zero_all()
        await pilot.pause()
        # Then restore defaults
        app.action_defaults()
        await pilot.pause()
        for p in S1_PARAMS:
            assert app.state.get(p.cc) == p.default, (
                f"{p.name} (CC {p.cc}) expected default {p.default}, got {app.state.get(p.cc)}"
            )


@pytest.mark.asyncio
async def test_defaults_updates_widgets(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)) as pilot:
        app.action_defaults()
        await pilot.pause()
        # Check a known widget
        freq_widget = app._widgets.get(74)
        assert freq_widget is not None
        assert freq_widget.value == 127  # Filter Frequency default


@pytest.mark.asyncio
async def test_zero_updates_widgets(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)) as pilot:
        app.action_zero_all()
        await pilot.pause()
        for cc, widget in app._widgets.items():
            assert widget.value == 0, f"Widget CC {cc} should be 0"


@pytest.mark.asyncio
async def test_midi_send_on_randomize(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)) as pilot:
        mock_midi.send_cc.reset_mock()
        app.action_randomize()
        await pilot.pause()
        assert mock_midi.send_cc.call_count == 54


@pytest.mark.asyncio
async def test_slider_key_right(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)) as pilot:
        # Find the first CCSlider and focus it
        sliders = list(app.query("CCSlider"))
        if sliders:
            slider = sliders[0]
            slider.focus()
            await pilot.pause()
            initial = slider.value
            await pilot.press("right")
            await pilot.pause()
            assert slider.value == initial + 1


@pytest.mark.asyncio
async def test_slider_key_left_at_zero(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)) as pilot:
        sliders = list(app.query("CCSlider"))
        if sliders:
            slider = sliders[0]
            slider.value = 0
            slider.focus()
            await pilot.pause()
            await pilot.press("left")
            await pilot.pause()
            assert slider.value == 0  # Should not go below 0


@pytest.mark.asyncio
async def test_toggle_space_toggles(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)) as pilot:
        toggles = list(app.query("CCToggle"))
        if toggles:
            toggle = toggles[0]
            toggle.focus()
            await pilot.pause()
            assert toggle.value == 0
            await pilot.press("space")
            await pilot.pause()
            assert toggle.value == 127
            await pilot.press("space")
            await pilot.pause()
            assert toggle.value == 0


@pytest.mark.asyncio
async def test_selector_right_cycles(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)) as pilot:
        selectors = list(app.query("CCSelector"))
        if selectors:
            sel = selectors[0]
            sel.value = sel._choices[0]
            sel.focus()
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            assert sel.value == sel._choices[1]


@pytest.mark.asyncio
async def test_selector_left_at_first(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)) as pilot:
        selectors = list(app.query("CCSelector"))
        if selectors:
            sel = selectors[0]
            sel.value = sel._choices[0]
            sel.focus()
            await pilot.pause()
            await pilot.press("left")
            await pilot.pause()
            assert sel.value == sel._choices[0]  # Should stay at first


@pytest.mark.asyncio
async def test_midi_input_updates_widget(mock_midi):
    """Simulate incoming MIDI CC updating a widget."""
    import mido
    app = S1App()
    async with app.run_test(size=(120, 40)) as pilot:
        mock_midi.connected = True
        mock_midi.channel = 2
        msg = mido.Message("control_change", channel=2, control=74, value=99)
        mock_midi.poll_input.return_value = [msg]
        # Trigger one poll cycle
        app._poll_midi_input()
        await pilot.pause()
        assert app._widgets[74].value == 99
        assert app.state.get(74) == 99
        # Reset so it doesn't keep replaying
        mock_midi.poll_input.return_value = []


@pytest.mark.asyncio
async def test_midi_input_wrong_channel_ignored(mock_midi):
    """MIDI on wrong channel should be ignored."""
    import mido
    app = S1App()
    async with app.run_test(size=(120, 40)) as pilot:
        mock_midi.connected = True
        mock_midi.channel = 2
        # Set known value first
        app._widgets[74].value = 50
        msg = mido.Message("control_change", channel=5, control=74, value=99)
        mock_midi.poll_input.return_value = [msg]
        app._poll_midi_input()
        await pilot.pause()
        assert app._widgets[74].value == 50  # Unchanged
        mock_midi.poll_input.return_value = []


@pytest.mark.asyncio
async def test_keybinding_quit(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("ctrl+c")
        # App should have called disconnect
        mock_midi.close.assert_called()


# ── Sequencer tab tests ──


@pytest.mark.asyncio
async def test_sequencer_tab_exists(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)):
        seq_views = app.query("SequencerView")
        assert len(seq_views) > 0


@pytest.mark.asyncio
async def test_sequencer_widgets_indexed(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)):
        assert len(app._seq_widgets) == len(SEQ_PARAMS)


@pytest.mark.asyncio
async def test_transport_play_sends_start(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)) as pilot:
        app.action_transport_play()
        await pilot.pause()
        mock_midi.send_start.assert_called_once()
        assert app._transport_active is True


@pytest.mark.asyncio
async def test_transport_stop_sends_stop(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)) as pilot:
        app.action_transport_play()
        await pilot.pause()
        app.action_transport_stop()
        await pilot.pause()
        mock_midi.send_stop.assert_called_once()
        assert app._transport_active is False


@pytest.mark.asyncio
async def test_transport_toggle(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)) as pilot:
        assert app._transport_active is False
        app.action_transport_toggle()
        await pilot.pause()
        assert app._transport_active is True
        mock_midi.send_start.assert_called_once()
        app.action_transport_toggle()
        await pilot.pause()
        assert app._transport_active is False
        mock_midi.send_stop.assert_called_once()


@pytest.mark.asyncio
async def test_seq_param_change_no_cc_send(mock_midi):
    """Changing a seq param should NOT send a CC message."""
    app = S1App()
    async with app.run_test(size=(120, 40)) as pilot:
        mock_midi.send_cc.reset_mock()
        # Find a seq widget and change it
        if app._seq_widgets:
            key = next(iter(app._seq_widgets))
            widget = app._seq_widgets[key]
            widget.value = 100
            widget.focus()
            await pilot.pause()
        # No CC should have been sent for seq params
        assert mock_midi.send_cc.call_count == 0


# ── Piano roll tests ──


@pytest.mark.asyncio
async def test_piano_roll_exists_in_seq_tab(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)):
        rolls = app.query("PianoRoll")
        assert len(rolls) > 0


@pytest.mark.asyncio
async def test_piano_roll_default_sequence(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)):
        roll = app.query_one("#piano-roll", PianoRoll)
        assert roll.sequence.steps == 16
        assert roll.sequence.notes == []


@pytest.mark.asyncio
async def test_piano_roll_set_sequence(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)) as pilot:
        roll = app.query_one("#piano-roll", PianoRoll)
        seq = Sequence(notes=[Note(step=0, pitch=60)], steps=16)
        roll.set_sequence(seq)
        await pilot.pause()
        assert roll.sequence is seq
        assert len(roll.sequence.notes) == 1


@pytest.mark.asyncio
async def test_engine_created_on_transport(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)) as pilot:
        assert app.engine is None
        app.action_transport_play()
        await pilot.pause()
        # Engine should have been created (falls back to raw MIDI start for empty seq)
        assert app.engine is not None
        app.action_transport_stop()
        await pilot.pause()


@pytest.mark.asyncio
async def test_transport_with_sequence(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)) as pilot:
        roll = app.query_one("#piano-roll", PianoRoll)
        seq = Sequence(
            notes=[Note(step=0, pitch=60, velocity=100, duration=1)],
            steps=4,
            bpm=600.0,
        )
        roll.set_sequence(seq)
        await pilot.pause()
        app.action_transport_play()
        await pilot.pause()
        assert app._transport_active is True
        assert app.engine is not None
        app.action_transport_stop()
        await pilot.pause()
        assert app._transport_active is False


# ── Phase 2: panic, MIDI save, seq-param wiring ──


@pytest.mark.asyncio
async def test_panic_sends_all_notes_off(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)):
        app.action_panic()
        mock_midi.all_notes_off.assert_called()


@pytest.mark.asyncio
async def test_save_midi_roundtrip(mock_midi, tmp_path):
    app = S1App()
    async with app.run_test(size=(120, 40)):
        roll = app.query_one("#piano-roll", PianoRoll)
        roll.sequence.toggle_note(0, 48)
        roll.sequence.toggle_note(4, 52)
        app.MIDI_DIR = tmp_path
        app._on_save_midi("pattern-x")
        path = tmp_path / "pattern-x.mid"
        assert path.exists()
        from s1tui.sequence import load_midi

        seq2 = load_midi(path)
        assert {(n.step, n.pitch) for n in seq2.notes} == {(0, 48), (4, 52)}


@pytest.mark.asyncio
async def test_save_midi_rejects_bad_names(mock_midi, tmp_path):
    app = S1App()
    async with app.run_test(size=(120, 40)):
        roll = app.query_one("#piano-roll", PianoRoll)
        roll.sequence.toggle_note(0, 48)
        app.MIDI_DIR = tmp_path
        app._on_save_midi("../evil")
        assert not (tmp_path.parent / "evil.mid").exists()


@pytest.mark.asyncio
async def test_seq_params_steer_engine(mock_midi):
    app = S1App()
    async with app.run_test(size=(120, 40)):
        app._apply_seq_param("seq_gate", 127)
        assert abs(app.engine.gate - 1.0) < 1e-6
        app._apply_seq_param("seq_gate", 0)
        assert abs(app.engine.gate - 0.05) < 1e-6
        app._apply_seq_param("seq_shuffle", 127)
        assert abs(app.engine.shuffle - 0.5) < 1e-6
        app._apply_seq_param("last_step", 12)
        assert app.engine.last_step == 12
        app._apply_seq_param("master_prob", 0)
        assert app.engine.probability == 0.0
        app._apply_seq_param("seq_scale", 32)
        roll = app.query_one("#piano-roll", PianoRoll)
        assert roll.sequence.step_resolution == "1/8"
