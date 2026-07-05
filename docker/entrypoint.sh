#!/bin/sh
# Start the demo, making sure a Gemini API key is present.
#
# Precedence:
#   1. GOOGLE_API_KEY already in the environment (-e / --env-file) → used as-is.
#   2. Otherwise, if attached to a terminal (docker run -it) → prompt for it.
#   3. Otherwise → fail with instructions.
set -e

if [ -z "$GOOGLE_API_KEY" ]; then
  if [ -t 0 ]; then
    printf "Enter your Gemini (Google AI Studio) API key: " >&2
    # Hide the key while it is typed, then restore the terminal.
    stty -echo 2>/dev/null || true
    read -r GOOGLE_API_KEY
    stty echo 2>/dev/null || true
    printf "\n" >&2
    export GOOGLE_API_KEY
  fi
fi

if [ -z "$GOOGLE_API_KEY" ]; then
  echo "ERROR: no Gemini API key provided." >&2
  echo "  Interactive : docker run -it -p 8000:8000 lipstick-twin" >&2
  echo "  Non-interactive: docker run -p 8000:8000 -e GOOGLE_API_KEY=your-key lipstick-twin" >&2
  echo "Get a key at https://aistudio.google.com/apikey" >&2
  exit 1
fi

echo "Starting Find Your Lipstick Twin on http://localhost:8000 ..." >&2
exec python -m uvicorn main:app --host 0.0.0.0 --port 8000
