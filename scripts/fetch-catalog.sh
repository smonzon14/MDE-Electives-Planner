#!/usr/bin/env bash
# Fetch the current catalog into data/courses.db for local development.
#
# The catalog is not in git (see scripts/vercel-build.sh for why), so a fresh
# clone has no database and the app will 503 until you either run this or crawl
# my.harvard yourself with `python -m ingest.crawl`.
#
# Uses the gh CLI's existing auth, so no token to manage:
#     ./scripts/fetch-catalog.sh
set -euo pipefail
REPO="${CATALOG_REPO:-smonzon14/MDE-Electives-Planner}"
mkdir -p data
echo "fetching courses.db from the rolling 'catalog' release of $REPO"
gh release download catalog --repo "$REPO" --pattern courses.db --dir data --clobber
printf 'release-asset\n' > data/CATALOG_SOURCE
python3 - <<'PY'
import sqlite3
c = sqlite3.connect("file:data/courses.db?mode=ro", uri=True)
n = c.execute("select count(*) from courses").fetchone()[0]
terms = [r[0] for r in c.execute("select distinct term from courses order by term")]
mode = c.execute("PRAGMA journal_mode").fetchone()[0]
print(f"  {n:,} sections across {terms} (journal_mode={mode})")
assert mode == "delete", "catalog is not sealed -- it will not open read-only"
PY
