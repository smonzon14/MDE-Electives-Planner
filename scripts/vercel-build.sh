#!/usr/bin/env bash
# Runs on Vercel before the function is bundled. Probe: does a file created
# here survive into the serverless bundle? If it does, the catalog can be
# downloaded at build time instead of committed to git.
set -euo pipefail
mkdir -p data
{
  echo "built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "cwd=$(pwd)"
  echo "vercel=${VERCEL:-unset}"
} > data/BUILD_PROBE
echo "vercel-build: wrote data/BUILD_PROBE"
ls -la data/ || true
