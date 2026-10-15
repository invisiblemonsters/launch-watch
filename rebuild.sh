#!/usr/bin/env bash
# Rebuild hook for the Launch Watch 30-min loop. Regenerates ./docs from all runs.
# Local only: it never commits, pushes or publishes anything.
# Call it from /workspace/launchwatch/loop.sh right after ./run.sh (see README).
set -uo pipefail
SITE_DIR="$(cd "$(dirname "$0")" && pwd)"
export LW_DATA="${LW_DATA:-/workspace/launchwatch}"
exec 8>/tmp/launchwatch-site.lock
flock -n 8 || { echo "$(date '+%F %T %Z') site build already running, skipped"; exit 0; }
echo "$(date '+%F %T %Z') site rebuild"
python3 "$SITE_DIR/build_site.py" --data "$LW_DATA" --out "$SITE_DIR/docs" || { echo "site build FAILED (previous docs/ may be partial)"; exit 1; }
