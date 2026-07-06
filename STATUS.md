# STATUS — s1tui
*updated 2026-07-05 (software-domain-spec shipped)*

- **state:** active
- **what:** The Roland S-1 hardware synth, fully present in software: one `s1` command starts a local web cockpit (FastAPI) with every panel knob and menu setting live-synced both directions, a piano-roll sequencer with MIDI clock out, auto-monitored USB audio, MIDI-keyboard forwarding, .PRM export ("Save to S-1"), and a full REST/WS agent API. The Textual TUI is retired. Plus the CMA-ES sound-matching engine behind `[studio]`. 330 tests.
- **interesting:** the "digital twin" — a differentiable software model of the S-1's fully-known signal chain, calibrated on a few hundred hardware probes, so sound-matching becomes offline gradient descent instead of 15 minutes of blind hardware probing. It's also the seed of the standalone soft synth.
- **last mile:** the joint plug-in ceremony (Tyler + hardware: knob-twist→UI, HW keyboard, disk-mode write, replug resync) confirms the ship; then the FABLE north star — validate the perceptual distance metric against Tyler's ears, then build the twin (docs/match-v3-spec.md).
- **cluster:** creative
