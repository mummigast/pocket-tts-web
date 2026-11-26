# pocket-tts-web

A self-hosted web page for [Kyutai Pocket TTS](https://github.com/kyutai-labs/pocket-tts): fast text-to-speech
that runs on an ordinary CPU, with voice cloning.

- **~690 voices** from Kyutai's voice library, with a browser: tabs, search, pitch / gender / accent / mood filters,
  ▶ previews, favourites
- **Clone a voice** from an uploaded clip (any format a phone records, e.g. WhatsApp `.ogg` voice notes) or from a
  **YouTube** segment picked with a slider
- **7 languages** (English, French, German, Spanish, Italian, Portuguese, Dutch); extra models install from the page
- **Background ambience** (street, café, rain, …) at low / medium / high, plus a little extra time at the end
- **Share links**: give a friend a login-free link that expires (1 h – 7 days), revoke it any time
- Download as **WAV** or **OGG** (Opus, voice-note size). Audio streams while it is generated.

Everything runs on your machine; nothing is sent to an outside service except downloads (models, voices, YouTube).

## Requirements

- Linux or macOS, 2+ CPU cores, ~4 GB disk, ~1 GB RAM (+0.2–0.5 GB per extra model in use). No GPU needed.
- `ffmpeg`
- [uv](https://docs.astral.sh/uv/) (recommended, it also fetches the right Python) or Python 3.10–3.14
- A free **Hugging Face account** for voice cloning (see below)

## 1. Hugging Face access (for voice cloning)

The built-in voices work without an account. Cloning voices from your own clips uses Kyutai's gated model, so:

1. Create an account at <https://huggingface.co/join>
2. Open <https://huggingface.co/kyutai/pocket-tts> while logged in and **accept the terms**
3. Log in on the machine (the setup script offers to do this, or run it yourself after setup):

   ```bash
   .venv/bin/hf auth login
   ```

   It shows a short code to enter at <https://hf.co/oauth/device>, or you can paste an access token
   (<https://huggingface.co/settings/tokens>, "Read" is enough). The token is stored in `~/.cache/huggingface/`.

The account is only used to download the model files once; generation runs locally.

## 2. Install

```bash
git clone <this repo> pocket-tts-web
cd pocket-tts-web
./setup.sh              # or ./setup.sh --service to also install a systemd user service
```

`setup.sh` creates `.venv` with CPU-only PyTorch, checks your Hugging Face access, and downloads + builds the
background sounds (~100 MB from Wikimedia Commons; skip with `--no-noise`). It is safe to run again.

<details><summary>Manual install</summary>

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt --index-strategy unsafe-best-match
.venv/bin/hf auth login
noise/fetch.sh && noise/build.sh
```
</details>

## 3. Run

```bash
.venv/bin/python web/app.py --port 18700
```

Open <http://127.0.0.1:18700>. The first start downloads the English model (~200 MB); voices download the first time
you preview or use them. With `./setup.sh --service` it runs as `pocket-tts` under `systemctl --user`
(logs: `journalctl --user -u pocket-tts -f`; `loginctl enable-linger $USER` keeps it running when you log out).

## 4. Putting it online

> **The app has no login of its own.** It listens on `127.0.0.1` only. If you make it reachable from the internet,
> put a reverse proxy with a login in front of it, and let **only `/s/*`** (the share links) through without login.

[`deploy/Caddyfile.example`](deploy/Caddyfile.example) shows this with Caddy and basic auth; forward auth to
Authentik, oauth2-proxy etc. works the same way. Allow uploads of ~50 MB in the proxy.

Share-link guests can use the installed models and the voice library, upload / YouTube-clone their own voices
(kept in `voices/guests/`), max 3000 characters per text, one generation at a time per link. They cannot see your
own clones unless you mark one as shared (👥), install models, or delete anything.

## Files

| Path | What |
|---|---|
| `web/app.py` | the server (FastAPI around `pocket_tts`) |
| `web/index.html` | the page |
| `web/voices.json` | voice catalog, rebuilt by `web/build_catalog.py` (+ `web/pitch.py`, `web/meta/`) |
| `web/models.json` | models shown in the page |
| `noise/` | background sounds: `fetch.sh` downloads, `build.sh` builds loops, `credits.json` = sources + licences |
| `voices/` | your clones (not in git). Deleting in the page moves files to `voices/.deleted/` |
| `state/` | share links, favourites (not in git) |
| `pt.sh`, `yt-voice.sh` | command-line helpers |

## Use responsibly

Only clone voices of people who have agreed to it, and don't use cloned voices to impersonate or deceive anyone.
This is also a condition of Kyutai's voice-cloning model. Downloading from YouTube may be against YouTube's terms
or the uploader's rights; that is your responsibility.

## Licences and credits

This project's code is [MIT](LICENSE). Third-party parts: see [THIRD_PARTY.md](THIRD_PARTY.md). In short: Pocket TTS is MIT; the voices and background recordings keep their own
licences (some voice sets are **non-commercial only**), and this repo does not contain any of the audio itself.
