"""Local web app for s1tui — FastAPI backend + synthwave frontend.

Serves the automated sound-design UI on localhost. Wraps the existing s1tui core
(MIDI, schema, patch bank) and the :mod:`s1tui.match` engine. Requires the
``studio`` optional extra.
"""
