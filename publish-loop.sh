#!/bin/bash
# Push fresh scores every 6 hours through Oct 21.
cd /workspace/launchwatch-site
while [ "$(date +%Y%m%d)" -le 20261021 ]; do
  ./publish.sh >> /workspace/launchwatch/runs/publish.log 2>&1
  sleep 21600
done
