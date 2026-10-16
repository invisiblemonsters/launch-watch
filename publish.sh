#!/bin/bash
# Rebuild Launch Watch site and push docs/ only. Approved by user 2026-10-15.
cd /workspace/launchwatch-site || exit 1
./rebuild.sh >> /workspace/launchwatch/runs/site-build.log 2>&1
git add docs
git diff --cached --quiet && exit 0
git commit -qm "Update scores $(date '+%Y-%m-%d %H:%M %Z')" && git push -q origin main
