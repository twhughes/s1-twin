"""Build the public page: the twin in the browser, with no server behind it.

    python tools/build_site.py [--out site]

Copies ``synth/web/static/`` (the one front-end; there is no second UI to keep in sync) into
OUT, drops what only makes sense next to the cockpit server or in development (node checks,
the design specimen, the ear test), and marks ``index.html`` with
``<meta name="twin-static" content="1">`` so the app goes straight to static mode without
probing ``/api``. In static mode the app plays the browser twin, the Sequencer runs its own
clock, and Match replays the recorded runs listed in ``matches/index.json``.

GitHub Pages serves OUT as-is under tylerwhughes.com/<repo>/: every path the app loads is
relative, and ``.nojekyll`` stops Pages from hiding underscore paths.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "synth" / "web" / "static"
META = '<meta name="twin-static" content="1">'

SKIP_TOP = {"eartest", "__pycache__"}               # server-only or build litter
SKIP_FILES = {"design/kit.html"}                    # the kit's specimen page is a dev tool
SKIP_SUFFIXES = (".check.mjs", ".DS_Store", ".pyc")
REQUIRED = (
    "index.html", "app.js", "design/tokens.css", "core/schema.json",
    "twin/curves.json", "twin/worklet.js", "matches/index.json",
)


def build(out: Path) -> list[str]:
    """Write the static page into ``out`` (replacing it) and return the copied paths."""
    if out.exists():
        shutil.rmtree(out)
    copied: list[str] = []
    for src in sorted(STATIC.rglob("*")):
        rel = src.relative_to(STATIC).as_posix()
        if src.is_dir() or rel.split("/", 1)[0] in SKIP_TOP or "/__pycache__/" in f"/{rel}":
            continue
        if rel in SKIP_FILES or rel.endswith(SKIP_SUFFIXES):
            continue
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(rel)

    index = out / "index.html"
    html = index.read_text(encoding="utf-8")
    if META not in html:
        if "<head>\n" not in html:
            raise SystemExit("index.html has no <head> line to mark")
        html = html.replace("<head>\n", f"<head>\n{META}\n", 1)
    index.write_text(html, encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")

    missing = [r for r in REQUIRED if not (out / r).exists()]
    if missing:
        raise SystemExit(f"the page would be broken, missing: {', '.join(missing)}")
    return copied


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=ROOT / "site", help="output directory (default: site/)")
    args = ap.parse_args()
    copied = build(args.out)
    print(f"built {len(copied)} files into {args.out}")


if __name__ == "__main__":
    main()
