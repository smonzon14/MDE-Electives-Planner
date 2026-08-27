#!/usr/bin/env bash
#
# Fetch the sealed catalog at build time, so it never has to live in git.
#
# The catalog is an ~11 MB SQLite file that the daily refresh rewrites in full.
# Committing it added ~11 MB of permanent, undeltifiable history per changed
# day, and git cannot prune that without rewriting history. It is also pure
# derived data -- reproducible from a crawl -- so old copies have no value.
# Only the current one is idempotent with my.harvard.
#
# Git LFS is deliberately NOT used: it still stores every version (billed
# against a 1 GB allowance), consumes a separate bandwidth quota on every
# checkout, and -- decisively -- Vercel does not fetch LFS objects, so the
# build would deploy the ~130-byte pointer file and SQLite would report the
# catalog as corrupt.
#
# Files written here reach the serverless bundle (verified empirically), so a
# GitHub release asset on the rolling `catalog` tag is fetched into place.
#
# Requires the CATALOG_TOKEN environment variable in Vercel: a fine-grained PAT
# scoped to this one repository with Contents: Read-only. Needed because the
# repo is private; a private repo's release assets are not publicly readable.

set -euo pipefail

REPO="${CATALOG_REPO:-smonzon14/MDE-Electives-Planner}"
TAG="${CATALOG_TAG:-catalog}"
DEST="data/courses.db"
API="https://api.github.com/repos/${REPO}"

mkdir -p data

note() { echo "vercel-build: $*"; }

# Records which source won, surfaced by /api/health. Without this a failed
# download silently falls back to whatever is in the repo, and the deploy looks
# healthy while serving a stale catalog.
record() { printf '%s\n' "$1" > data/CATALOG_SOURCE; }

fetch_asset() {
  if [ -z "${CATALOG_TOKEN:-}" ]; then
    note "CATALOG_TOKEN is not set; cannot read a private repo's release assets"
    return 1
  fi

  note "looking up ${TAG}/courses.db in ${REPO}"
  local meta asset_id
  meta="$(curl -sS --fail-with-body \
            -H "Authorization: Bearer ${CATALOG_TOKEN}" \
            -H "Accept: application/vnd.github+json" \
            "${API}/releases/tags/${TAG}")" || { note "release lookup failed"; return 1; }

  # python3 is present for the Python runtime; grep/sed is the fallback so a
  # build-image change cannot break this silently.
  asset_id="$(printf '%s' "$meta" | python3 -c '
import json, sys
try:
    rel = json.load(sys.stdin)
except Exception:
    sys.exit(1)
for a in rel.get("assets", []):
    if a.get("name") == "courses.db":
        print(a["id"]); break
' 2>/dev/null)" || asset_id=""

  if [ -z "$asset_id" ]; then
    asset_id="$(printf '%s' "$meta" | tr ',' '\n' | grep -B5 '"name": *"courses.db"' \
                 | grep -o '"id": *[0-9]*' | head -1 | grep -o '[0-9]*' || true)"
  fi

  if [ -z "$asset_id" ]; then
    note "no courses.db asset on tag ${TAG}"
    return 1
  fi

  note "downloading asset ${asset_id}"
  curl -sSL --fail-with-body \
       -H "Authorization: Bearer ${CATALOG_TOKEN}" \
       -H "Accept: application/octet-stream" \
       "${API}/releases/assets/${asset_id}" -o "${DEST}.tmp" \
    || { note "asset download failed"; rm -f "${DEST}.tmp"; return 1; }

  # A truncated download or an error page would otherwise be served as the
  # catalog. Check the SQLite magic header and that the schema is readable.
  if ! head -c 15 "${DEST}.tmp" | grep -q "SQLite format 3"; then
    note "downloaded file is not a SQLite database ($(wc -c < "${DEST}.tmp") bytes)"
    rm -f "${DEST}.tmp"
    return 1
  fi
  mv "${DEST}.tmp" "$DEST"
  note "catalog in place: $(wc -c < "$DEST") bytes"
  return 0
}

if fetch_asset; then
  record "release-asset"
else
  # No fallback by design. The catalog is no longer in git, so a failed
  # download means there is nothing to serve -- and failing the build is far
  # better than deploying an app whose every request 503s, or silently
  # shipping a stale copy that looks healthy.
  note "ERROR: could not fetch the catalog."
  note "  Check CATALOG_TOKEN in Vercel (fine-grained PAT, Contents: Read-only)."
  note "  Check that the '${TAG}' release still has a courses.db asset."
  exit 1
fi
