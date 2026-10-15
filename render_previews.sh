#!/usr/bin/env bash
# Render preview PNGs of the built site with headless Chrome/Chromium (local files only).
set -euo pipefail
cd "$(dirname "$0")"
BROWSER="$(command -v google-chrome || command -v chromium || command -v chromium-browser)"
LAUNCH="${1:-docs/launch/robinhood-0xcdb4ee224ca4db25767ffad5b922f014a4905300.html}"
shot() { "$BROWSER" --headless=new --no-sandbox --disable-gpu --hide-scrollbars --force-device-scale-factor=1 \
  --window-size="$3" --screenshot="$PWD/$2" "file://$PWD/$1" 2>/dev/null; }
shot docs/index.html preview-index.png 1280,3200
shot "$LAUNCH" preview-launch.png 1280,3000
ls -la preview-*.png
