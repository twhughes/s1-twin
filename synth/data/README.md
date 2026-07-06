# Bundled device data

`init_pattern.prm` — a real Roland S-1 disk-mode dump of the factory init
pattern (device-generated settings data, via the denzlobin/S1Utility project).
It serves two roles:

1. **Template for the PRM writer** (`synth/prm.py`): every key the writer
   touches already exists in this device-authored file, so exported patterns
   never contain keys the firmware didn't write itself.
2. **Ground truth in tests**: parse → serialize round-trips byte-identically,
   and the schema's defaults are cross-checked against it.

Replace freely with a dump from your own device's BACKUP/ folder — any real
`S1_PTN<bank>-<slot>.PRM` works.
