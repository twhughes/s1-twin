"""The public page build (tools/build_site.py): one front-end, marked static, nothing dev-only shipped."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _builder():
    spec = importlib.util.spec_from_file_location("build_site", ROOT / "tools" / "build_site.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_build_marks_static_and_ships_what_the_page_loads(tmp_path):
    out = tmp_path / "site"
    copied = _builder().build(out)
    html = (out / "index.html").read_text()
    assert '<meta name="twin-static" content="1">' in html
    assert html.index("twin-static") < html.index("</head>")
    for rel in ("app.js", "core/schema.json", "twin/curves.json", "twin/worklet.js",
                "matches/index.json", "design/fonts/OFL-OldStandardTT.txt", ".nojekyll"):
        assert (out / rel).exists(), rel
    assert len(copied) > 20


def test_build_leaves_out_dev_and_server_only_files(tmp_path):
    out = tmp_path / "site"
    _builder().build(out)
    shipped = [p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()]
    assert not [p for p in shipped if p.endswith(".check.mjs")]
    assert "design/kit.html" not in shipped
    assert not [p for p in shipped if p.startswith("eartest/")]
    assert not [p for p in shipped if "__pycache__" in p]


def test_build_is_repeatable(tmp_path):
    out = tmp_path / "site"
    b = _builder()
    first = sorted(b.build(out))
    second = sorted(b.build(out))
    assert first == second
    assert (out / "index.html").read_text().count("twin-static") == 1
