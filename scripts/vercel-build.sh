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
# Files written here reach the serverless bundle (verified empirically), so the
# asset on the rolling `catalog` release is fetched into place.
#
# NO TOKEN IS NEEDED. The repository is public, so the release asset is served
# from the unauthenticated redirect URL below. That also means no GitHub API
# call, and therefore no exposure to the 60-requests/hour unauthenticated API
# rate limit that Vercel's shared build IPs would otherwise share. (This used to
# require a CATALOG_TOKEN fine-grained PAT plus an asset-id lookup through the
# API; making the repo public deleted both.)

set -euo pipefail

REPO="${CATALOG_REPO:-smonzon14/MDE-Electives-Planner}"
TAG="${CATALOG_TAG:-catalog}"
DEST="data/courses.db"
URL="https://github.com/${REPO}/releases/download/${TAG}/courses.db"

mkdir -p data

note() { echo "vercel-build: $*"; }

# Records which source won, surfaced by /api/health. Without this a failed
# download silently falls back to whatever is in the repo, and the deploy looks
# healthy while serving a stale catalog.
record() { printf '%s\n' "$1" > data/CATALOG_SOURCE; }

fetch_asset() {
  note "downloading ${URL}"
  # --location: the release URL is a redirect to GitHub's asset CDN.
  if ! curl -sSL --fail-with-body --retry 3 --retry-delay 2 \
            "$URL" -o "${DEST}.tmp"; then
    note "asset download failed"
    rm -f "${DEST}.tmp"
    return 1
  fi

  # A truncated download or an error page would otherwise be served as the
  # catalog. Check the SQLite magic header before accepting it.
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
  note "  Check that the '${TAG}' release still has a courses.db asset,"
  note "  and that ${REPO} is still public."
  exit 1
fi
