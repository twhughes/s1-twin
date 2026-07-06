"""Local web app for synth — FastAPI backend + synthwave frontend.

Serves the automated sound-design UI on localhost. Wraps the existing synth core
(MIDI, schema, patch bank) and the :mod:`synth.match` engine. Requires the
``studio`` optional extra.
"""
