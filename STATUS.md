# STATUS — s1tui
*audited 2026-07-05 (M3)*

- **state:** active
- **what:** Terminal (Textual) editor + piano-roll sequencer for the Roland S-1 hardware synth — all 54 CC params, patch bank, two-way MIDI — plus a CMA-ES sound-matching engine and a synthwave web studio that drive the real hardware. (NOT an SEC S-1 filing reader.) 238 tests; commits and FABLE.md updated today.
- **interesting:** the "digital twin" — a differentiable software model of the S-1's fully-known signal chain, calibrated on a few hundred hardware probes, so sound-matching becomes offline gradient descent instead of 15 minutes of blind hardware probing.
- **last mile:** validate the perceptual distance metric against Tyler's ears (play pairs, confirm the number agrees) — FABLE flags the matcher as "slow and doesn't work well," possibly because it faithfully minimizes the wrong distance.
- **cluster:** creative
