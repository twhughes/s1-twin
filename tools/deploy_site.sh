#!/bin/bash
# deploy_site.sh — publish the static page to the gh-pages branch.
# GitHub Pages serves that branch at tylerwhughes.com/<repo>/ (the custom domain comes from
# twhughes.github.io). This PUSHES: run it only with Tyler's OK for this release.
#   tools/deploy_site.sh            build site/ from the current checkout and push it
#   tools/deploy_site.sh --dry-run  build and commit into a throwaway worktree, push nothing
set -euo pipefail
cd "$(dirname "$0")/.."
DRY="${1:-}"

.venv/bin/python tools/thin_match.py synth/web/static/matches/*.json >/dev/null
.venv/bin/python tools/build_site.py
SRC_SHA="$(git rev-parse --short HEAD)"
WT="$(mktemp -d)"
trap 'git worktree remove --force "$WT" >/dev/null 2>&1 || true' EXIT

if git ls-remote --exit-code --heads origin gh-pages >/dev/null 2>&1; then
  git fetch -q origin gh-pages
  git worktree add -q -B gh-pages "$WT" origin/gh-pages
else
  git worktree add -q --detach "$WT"
  (cd "$WT" && git checkout -q --orphan gh-pages && git rm -rqf . >/dev/null 2>&1 || true)
fi
rsync -a --delete --exclude .git site/ "$WT/"
cd "$WT"
git add -A
if git diff --cached --quiet; then echo "gh-pages already matches this build"; exit 0; fi
git commit -q -m "Publish the page from ${SRC_SHA}"
if [ "$DRY" = "--dry-run" ]; then
  echo "dry run: committed $(git rev-parse --short HEAD) on a throwaway gh-pages worktree; nothing pushed"
else
  git push -q origin gh-pages
  echo "pushed gh-pages ($(git rev-parse --short HEAD)); Pages serves it in a minute or two"
fi
