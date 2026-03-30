#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

HEADLESS_FLAG=""
if [[ "${GEMINI_HEADLESS:-0}" == "1" ]]; then
  HEADLESS_FLAG="--headless=new"
fi

if [[ "${GEMINI_XVFB:-0}" == "1" ]]; then
  if command -v Xvfb >/dev/null 2>&1; then
    if ! pgrep -f "Xvfb :99" >/dev/null 2>&1; then
      echo "Starting Xvfb on :99 ..."
      Xvfb :99 -screen 0 1920x1080x24 >/tmp/gemini_xvfb.log 2>&1 &
      sleep 1
    fi
    export DISPLAY=:99
  else
    echo "GEMINI_XVFB=1 set but Xvfb not found; continuing without virtual display."
  fi
fi

if ! lsof -i :9222 >/dev/null 2>&1; then
  echo "Starting Chrome with remote debugging on :9222 ..."
  /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
    --remote-debugging-port=9222 \
    --user-data-dir=/tmp/chrome-debug-profile \
    --disable-blink-features=AutomationControlled \
    --disable-focus-on-load \
    --disable-background-networking \
    --disable-infobars \
    --disable-renderer-backgrounding \
    --disable-backgrounding-occluded-windows \
    ${HEADLESS_FLAG} >/dev/null 2>&1 &
  sleep 3
fi

echo "Running Gemini research loop ..."
python3 gemini_research_loop.py
