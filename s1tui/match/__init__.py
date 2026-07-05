"""Automated sound-design engine for the Roland S-1.

Records the synth's *actual* audio output, scores it against a target clip, and
drives the S-1's MIDI CC parameters with a derivative-free optimizer until the
sound matches. Pure-Python and UI-agnostic — used by both the CLI
(``s1tui-match``) and the web app.

The heavy audio/optimizer dependencies (soundfile, sounddevice, cma) live behind
the ``studio`` optional extra; importing this package does not pull them in until
a submodule that needs them is imported.
"""

from __future__ import annotations

WORKING_SR = 22050
"""Internal analysis samplerate. Captures content up to ~11 kHz, which covers
the S-1's audible range while keeping feature extraction fast and light."""

ANALYSIS_SECONDS = 2.0
"""Fixed analysis window (post onset-trim). Guarantees target and candidate
features share an identical shape so they can be compared directly."""
