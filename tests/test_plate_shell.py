"""The cockpit shell's static contract (docs/design/BUILD.md §2.2, §3): index.html points only at
files that exist; colors come only from the design kit; the pitch palette lives only in
design/colors.js; the product name lives only in design/brand.js; the old cockpit files are gone;
the views and drawers export the shapes the shell loads; the vendored key-hint pair keeps its
provenance (and, where the HQ canon is on disk, still agrees with it)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "synth" / "web" / "static"
# The files W-plate owns (BUILD.md §1). design/ is the lead's kit; twin/ and the other views are
# other workers'; keyhint/keyhint.css is vendored and themed from core/app.css.
OWN = [
    STATIC / "index.html",
    STATIC / "app.js",
    STATIC / "views" / "synth.js",
    *sorted((STATIC / "core").glob("*.js")),
    *sorted((STATIC / "core").glob("*.css")),
    *sorted((STATIC / "drawers").glob("*.js")),
]
COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b(?![-\w])|\brgba?\(\s*\d|\bhsla?\(\s*\d")


def test_own_files_exist():
    for path in OWN:
        assert path.exists(), path


def test_index_points_only_at_files_that_exist():
    html = (STATIC / "index.html").read_text()
    refs = re.findall(r'(?:src|href)="([^"#:?]+)"', html)
    assert refs, "index.html links nothing"
    for ref in refs:
        assert (STATIC / ref).is_file(), f"index.html points at a missing file: {ref}"


@pytest.mark.parametrize("path", OWN, ids=lambda p: str(p.relative_to(STATIC)))
def test_colors_come_only_from_the_kit(path):
    """BUILD.md §3 rule 1: no literal color outside design/ (tokens, draw.js, colors.js)."""
    text = path.read_text()
    found = COLOR.findall(text)
    assert not found, f"{path.name} carries literal colors {found}; use a token or the kit"


def test_the_pitch_palette_lives_only_in_colors_js():
    assert "const TRUE_BASE" in (STATIC / "design" / "colors.js").read_text()
    for path in OWN:
        assert "TRUE_BASE" not in path.read_text(), path


def test_the_product_name_lives_only_in_brand_js():
    name = re.search(r'export const NAME = "([^"]+)"', (STATIC / "design" / "brand.js").read_text()).group(1)
    html = (STATIC / "index.html").read_text()
    assert f">{name}<" not in html and f"<title>{name}" not in html, "index.html hard-codes the name"


def test_the_old_cockpit_is_gone():
    assert not (STATIC / "style.css").exists()
    for path in OWN:
        assert "style.css" not in path.read_text(), path


def test_views_and_drawers_export_what_the_shell_loads():
    view = (STATIC / "views" / "synth.js").read_text()
    for needle in ('export const id = "synth"', "export const title", "export function mount(root, ctx)",
                   "export function unmount()"):
        assert needle in view, needle
    for name in ("library", "settings"):
        src = (STATIC / "drawers" / f"{name}.js").read_text()
        for needle in (f'export const id = "{name}"', "export function mount(el, context)",
                       "export function open()", "export function close()"):
            assert needle in src, f"{name}: {needle}"


def test_ctx_names_every_field_of_the_contract():
    """BUILD.md §2.2 lists ctx's fields; each is defined in core/ctx.js."""
    src = (STATIC / "core" / "ctx.js").read_text()
    for field in ("schema,", "params,", "twin,", "server,", "toast,", "get soundSource()",
                  "set(cc, v, { source", "on(evt, fn)", "note(n, on, vel = 100)"):
        assert field in src, field


KEYHINT_CANON = ROOT.parent / "panel" / "keyhint"


@pytest.mark.parametrize("name", ["keyhint.js", "keyhint.css"])
def test_keyhint_is_vendored_with_provenance(name):
    ours = (STATIC / "keyhint" / name).read_text()
    assert ours.lstrip("/* ").startswith("vendored from hq/panel/keyhint/"), "provenance comment missing"
    canon = KEYHINT_CANON / name
    if not canon.exists():
        pytest.skip("the HQ canon is not beside this checkout")
    body = ours.split("\n\n", 1)[1]
    assert body == canon.read_text(), f"keyhint/{name} drifted from hq/panel/keyhint/{name}"
