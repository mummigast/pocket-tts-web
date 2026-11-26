#!/bin/bash
# Usage: ./yt-voice.sh <youtube-url> <start-sec> <end-sec> <name>
# Saves a YouTube segment as voices/<name>.wav (shows up under "Mine" in the web UI). The web UI has the same thing built in.
set -e; cd "$(dirname "$0")"; mkdir -p yt voices
[[ "$4" =~ ^[a-z0-9][a-z0-9_-]{0,39}$ ]] || { echo "name: lowercase letters, digits, - and _" >&2; exit 1; }
.venv/bin/yt-dlp -x --audio-format wav --download-sections "*$2-$3" --force-keyframes-at-cuts -o "yt/$4.%(ext)s" "$1"
ffmpeg -y -loglevel error -i "yt/$4.wav" -ac 1 -ar 24000 "voices/$4.wav"
echo "saved voices/$4.wav"
