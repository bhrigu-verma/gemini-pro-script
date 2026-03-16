#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

CDP_URL="http://127.0.0.1:9222"

if ! curl -fsS "$CDP_URL/json/version" >/dev/null 2>&1; then
  echo "Starting Google Chrome with remote debugging on port 9222..."
  open -na "Google Chrome" --args --remote-debugging-port=9222 --user-data-dir="$HOME/chrome-cdp-profile"
  echo
  echo "Please sign in to Gemini + ChatGPT in that Chrome window, then press Enter."
  read -r
fi

echo "Running main.py with existing Chrome session..."
USE_EXISTING_CHROME=1 /usr/bin/python3 main.py
