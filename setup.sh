#!/bin/bash
# One-shot setup for pocket-tts-web. Safe to run again (skips what is already done).
#   ./setup.sh             install everything
#   ./setup.sh --service   also install + start a systemd user service (Linux)
#   ./setup.sh --no-noise  skip the background sounds (~100 MB download)
set -e
cd "$(dirname "$0")"
DIR=$(pwd)
SERVICE=0; NOISE=1; PORT=${PORT:-18700}
for a in "$@"; do
  case $a in
    --service) SERVICE=1 ;;
    --no-noise) NOISE=0 ;;
    *) echo "unknown option $a"; exit 1 ;;
  esac
done
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

say "1/5 Checking tools"
command -v ffmpeg >/dev/null || { echo "ffmpeg is missing. Install it first (apt install ffmpeg / dnf install ffmpeg / brew install ffmpeg)."; exit 1; }
UV=$(command -v uv || ls ~/.local/bin/uv 2>/dev/null || true)
if [ -z "$UV" ]; then
  PY=""
  for v in 3.12 3.13 3.11 3.14 3.10; do command -v python$v >/dev/null && { PY=python$v; break; }; done
  [ -n "$PY" ] || { echo "Need Python 3.10–3.14, or uv (https://docs.astral.sh/uv/) which fetches Python for you."; exit 1; }
  echo "using $PY (install uv for a faster setup)"
else
  echo "using uv ($UV)"
fi

say "2/5 Python environment (.venv) and packages"
if [ ! -x .venv/bin/python ]; then
  if [ -n "$UV" ]; then "$UV" venv -q --python 3.12 .venv; else $PY -m venv .venv; fi
fi
if [ -n "$UV" ]; then
  "$UV" pip install -q --python .venv/bin/python -r requirements.txt --index-strategy unsafe-best-match
else
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -r requirements.txt
fi
.venv/bin/python -c "import pocket_tts, torch; print('pocket-tts ok, torch', torch.__version__)"

say "3/5 Hugging Face access (voice cloning weights)"
check_hf() {
  .venv/bin/python - <<'PY'
import sys
from huggingface_hub import auth_check, whoami
try:
    who = whoami()["name"]
except Exception:
    print("NOT_LOGGED_IN"); sys.exit(2)
try:
    auth_check("kyutai/pocket-tts")
    print(f"ok: logged in as {who}, access to kyutai/pocket-tts granted")
except Exception:
    print(f"NO_ACCESS: logged in as {who}, but the terms of kyutai/pocket-tts are not accepted yet"); sys.exit(3)
PY
}
set +e; out=$(check_hf); rc=$?; set -e
echo "$out"
if [ $rc -eq 2 ]; then
  echo
  echo "Voice cloning uses a gated model. You need a free Hugging Face account:"
  echo "  1. Sign up at https://huggingface.co/join"
  echo "  2. Accept the terms at https://huggingface.co/kyutai/pocket-tts"
  echo "  3. Log in here:"
  if [ -t 0 ]; then .venv/bin/hf auth login; set +e; out=$(check_hf); rc=$?; set -e; echo "$out"
  else echo "     .venv/bin/hf auth login     (then run ./setup.sh again)"; fi
fi
if [ $rc -eq 3 ]; then
  echo "Open https://huggingface.co/kyutai/pocket-tts while logged in, accept the terms, then run ./setup.sh again."
  echo "(Everything except cloning your own voices works without it.)"
fi

say "4/5 Background sounds"
if [ $NOISE -eq 1 ]; then
  if ls noise/*.wav >/dev/null 2>&1 && [ "$(ls noise/*.wav | wc -l)" -ge 9 ]; then echo "already built"
  else noise/fetch.sh && noise/build.sh; fi
else
  echo "skipped (--no-noise); the Effects & speed card will only offer 'None' as background"
fi

say "5/5 Service"
if [ $SERVICE -eq 1 ]; then
  command -v systemctl >/dev/null || { echo "no systemd here; start it by hand (see below)"; exit 0; }
  mkdir -p ~/.config/systemd/user
  cat > ~/.config/systemd/user/pocket-tts.service <<EOF
[Unit]
Description=Pocket TTS web UI ($DIR)
After=network-online.target

[Service]
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python $DIR/web/app.py --host 127.0.0.1 --port $PORT
Restart=on-failure
RestartSec=10
Nice=5

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now pocket-tts
  echo "service pocket-tts started on 127.0.0.1:$PORT (logs: journalctl --user -u pocket-tts -f)"
  echo "to keep it running while logged out: loginctl enable-linger $USER"
else
  echo "not installed (use ./setup.sh --service for a systemd user service)"
fi

say "Done"
echo "Run it:   .venv/bin/python web/app.py --port $PORT"
echo "Open:     http://127.0.0.1:$PORT   (first start downloads the English model, ~200 MB)"
echo "Online:   put a reverse proxy with a login in front; only /s/* may skip it. See README.md."
