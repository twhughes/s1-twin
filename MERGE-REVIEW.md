# MERGE-REVIEW — `chassis-hardening` → `main`

*Prepared 2026-09-07. Nothing merged. This is the review pack so the merge is a one-word decision.*

## Verdict
- **Fast-forward.** `main` (`8c2fb90`, 456 tests) is an ancestor of `chassis-hardening`; no merge commit, no conflicts possible.
- **Suite on the branch today:** 638 collected · **638 passed · 0 failed** · ruff clean (fresh Python 3.12 venv, current PyPI deps: numpy 2.5, scipy 1.18, fastapi 0.141, autograd 1.9, pytest 9).
- **Size:** 6 commits · 54 files · +7,931 / −62 lines. Production code touched only in commits 1, 4, 5, 6; commits 2–3 are contracts + tests.

## The six commits (oldest first)

| # | hash | date | what | tests |
|---|---|---|---|---|
| 1 | `636cab8` | 08-07 | Checkpoint: chassis spec (`docs/chassis-spec.md`, contracts C1–C9) + M1 cockpit mode switch + M2 pattern bridge (PRM↔.mid, `synth-prm` CLI) + M4 headless Logic transport (`synth-logic` CLI) + matcher Phase 0/A scaffolding (`match/corpus.py`, `match/analyze.py`). Also bundles previously-uncommitted keyboard-forwarding work. | 456 |
| 2 | `7c21823` | 08-07 | Contract-hardening sweep: explicit Protocols at every seam (`backend_protocol.py`, `match/protocols.py`, `web/facade.py`), listener lock, silent-error swallowing removed in `midi_backend`, private reaches made public accessors, Phase-A probe-at-detected-pitch fix (was hardwired note 48). | 456→552 |
| 3 | `ebe228e` | 08-07 | Tests only: audio drift-servo numerics, capture device fns, `synth-match` CLI. No production files. | 552→601 |
| 4 | `64d3f55` | 08-08 | **The differentiable digital twin** (`match/twin.py`, HIPS autograd, `[twin]` extra): analytic 4-pole ladder, gradcheck ~3e-8, self-consistent match ~78 closeness, ~23 s/match, 0 probes. | 601→615 |
| 5 | `be54c28` | 08-08 | **Standalone soft synth + visual sound-matcher** (`soft/index.html`, `soft/server.py`, WS `/ws/match`): multi-pitch `detect_notes`, `render_chord`, silent-by-default matcher. | 620→638 |
| 6 | `f3fcfe4` | 08-11 | Soft matcher server port 8767 → **8816** (PORTS.md claim; clears the mashup collision). | — |

(+ today's `chassis-hardening` commit: venv recipe in README, `soft/run.sh` default port aligned to 8816, STATUS refresh — see `git log`.)

## Risks / known leftovers (all flagged in the commits themselves, none blocking)
1. `web/server.py` still reaches `engine._mido` / `engine._tick_audio()` — engine-side private reach, noted in commit 2 as a leftover.
2. The twin's **calibration is synthetic / self-consistent only**; `calibrate(probes)` seam is ready but the real S-1 probes are the tagged hardware session (FABLE Phase B remainder).
3. The perceptual distance metric is **not validated against Tyler's ears** (FABLE rule #1) — the branch does not claim it is.
4. Commit 1 bundles pre-existing uncommitted keyboard-forwarding work with the session's work — history is coarser than ideal but the checkpoint was green (456, ruff clean).
5. `soft/` has no `bin/` word of its own; it launches via `bash soft/run.sh` (port 8816). The cockpit's `bin/synth` (8766) is unchanged.

## What the merge does NOT need
- No dependency change on `main`'s side beyond `pyproject.toml` extras already on the branch.
- No hardware. No Tyler-ears validation (that gates Phase B, not the merge).

## The command (Tyler runs it, or says the word)
```bash
cd ~/Documents/hq/synth && git checkout main && git merge --ff-only chassis-hardening && git checkout chassis-hardening
```
(Or simply keep working on `chassis-hardening` and treat it as the new main — the branch is strictly ahead.)
