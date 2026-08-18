#!/usr/bin/env bash
# Deploy the page to GitHub Pages — the whole ritual, in order.
#
# This is the manual stand-in for scripts/pages-workflow.yml.parked. That
# workflow cannot be pushed until the gh token gains the `workflow` scope
# (one-time, interactive):  gh auth refresh -h github.com -s workflow
# Until then: rebuild the daily data, verify it, and push web/ as the
# gh-pages branch, which GitHub serves directly.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> rebuild daily data"
python3 tool/build_indicators.py
python3 tool/build_aliases.py

echo "==> tests"
python3 tool/test_indicators.py
python3 tool/test_proxy_parity.py
python3 tool/test_page_loads.py

echo "==> commit web/ (if changed)"
# Stage all of web/, not just web/data. Staging only the data directory once
# deployed a "successful" build that silently omitted an index.html change —
# the script reported done, the edge served the old page, and the only clue
# was the change simply not being there.
git add web
git diff --cached --quiet || git commit -m "web: refresh data and page for deploy"

# Anything else still uncommitted is not going out, so say so rather than
# letting the next deploy look like it shipped work it never saw.
if ! git diff --quiet -- . ':!web'; then
  echo "    note: uncommitted changes outside web/ are NOT deployed:"
  git diff --name-only -- . ':!web' | sed 's/^/      /'
fi

echo "==> push main + gh-pages"
git push origin main
# subtree split makes a branch whose root IS web/ — exactly what Pages wants.
git branch -D gh-pages-split 2>/dev/null || true
git subtree split --prefix web -b gh-pages-split >/dev/null
git push -f origin gh-pages-split:gh-pages
git branch -D gh-pages-split

echo "==> done — https://heysambeet.github.io/levels/"
