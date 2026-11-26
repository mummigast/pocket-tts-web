#!/bin/bash
# The pocket-tts command line from this project's environment, e.g.: ./pt.sh generate --text "Hello" --voice alba
exec "$(dirname "$0")/.venv/bin/pocket-tts" "$@"
