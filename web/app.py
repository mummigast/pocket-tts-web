"""Pocket TTS web UI: a self-hosted page around Kyutai Pocket TTS.

Wraps pocket_tts (same models + streaming), but
- the page is ours (web/index.html): language, model and voice dropdowns,
- models come only from web/models.json, loaded on first use and kept in memory,
- voices come only from web/voices.json or our own clips in voices/ (the stock server
  fetches any http(s) URL it is given; we never do),
- own clones are stored as audio clips, because a voice state only fits the model that
  made it; the state is rebuilt (and cached) per model.

Run: .venv/bin/python web/app.py --port 18700   (pocket-tts.service)
"""
import argparse
import hashlib
import secrets
import io
import json
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path
from queue import Queue

import numpy as np
import torch
import uvicorn
from fastapi import FastAPI
from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from pocket_tts.data.audio import stream_audio_chunks
from pocket_tts.default_parameters import DEFAULT_TEXT_FOR_LANGUAGE, get_default_voice_for_language
from pocket_tts.models.tts_model import TTSModel
import pocket_tts
from pocket_tts.utils.utils import _ORIGINS_OF_PREDEFINED_VOICES
import yaml
from huggingface_hub import hf_hub_download, try_to_load_from_cache

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
VOICES = ROOT / "voices"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
AUDIO_EXT = {".wav", ".mp3", ".flac", ".ogg", ".oga", ".m4a", ".aac", ".opus", ".webm", ".mp4", ".amr", ".3gp", ".wma"}
MAX_UPLOAD = 30 * 1024 * 1024
MAX_MODELS = 6
MAX_STATES = 40
DEFAULT_MODEL = "english"
log = logging.getLogger("tts-web")

app = FastAPI(title="Pocket TTS", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def same_origin_only(request, call_next):
    """Block cross-site requests that change things (CSRF): with basic auth the browser would send the password
    along with a form posted from another site. Our own page always sends same-origin requests."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        site = request.headers.get("sec-fetch-site")
        origin = request.headers.get("origin")
        host = request.headers.get("host", "")
        if site == "cross-site" or (origin and origin.split("://", 1)[-1] != host):
            return JSONResponse({"detail": "Cross-site request blocked"}, status_code=403)
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    return resp


def load_json(name: str):
    return json.loads((WEB / name).read_text())


def model_ids() -> set[str]:
    return {m[0] for lang in load_json("models.json") for m in lang["models"]}


VOICE_REPO = "kyutai/tts-voices"
_voice_lock = threading.Lock()
_group_jobs: dict[str, str] = {}   # group name → "installing" | "done" | "failed"


def catalog() -> dict[str, str]:
    return {v["id"]: v["src"] for g in load_json("voices.json") for v in g["voices"]}


def repo_path(src: str) -> str | None:
    pre = f"hf://{VOICE_REPO}/"
    return src[len(pre):] if src.startswith(pre) else None


def voice_file(src: str) -> Path | None:
    """Local cached clip for a repo voice, or None if not installed."""
    rp = repo_path(src)
    if rp is None:
        return None
    hit = try_to_load_from_cache(VOICE_REPO, rp)
    return Path(hit) if isinstance(hit, str) else None


def voice_installed(src: str) -> bool:
    return "://" not in src or voice_file(src) is not None


def install_voice_src(src: str) -> None:
    rp = repo_path(src)
    if rp is None:
        raise HTTPException(400, "Voice source not allowed")
    with _voice_lock:
        hf_hub_download(VOICE_REPO, rp)


CONFIGS = Path(pocket_tts.__file__).parent / "config"
_installing: dict[str, str] = {}   # model id → "installing" | "failed: ..."


def installed(mid: str) -> bool:
    """Weights already in the Hugging Face cache (downloaded once, then offline)."""
    try:
        w = yaml.safe_load((CONFIGS / f"{mid}.yaml").read_text())["weights_path"][len("hf://"):]
        repo_path, rev = w.split("@")
        owner, repo, path = repo_path.split("/", 2)
        return isinstance(try_to_load_from_cache(f"{owner}/{repo}", path, revision=rev), str)
    except Exception:
        return False


def _install(mid: str) -> None:
    try:
        get_model(mid)
        _installing.pop(mid, None)
        log.info("installed model %s", mid)
    except Exception as e:
        log.exception("install of %s failed", mid)
        _installing[mid] = f"failed: {type(e).__name__}"


def own_clip(name: str) -> Path | None:
    if not NAME_RE.match(name):
        return None
    for p in VOICES.glob(name + ".*"):
        if p.stem == name and p.suffix.lower() in AUDIO_EXT:
            return p
    return None


# ---- models and voice states (LRU caches) ----
_models: "OrderedDict[str, TTSModel]" = OrderedDict()
_states: "OrderedDict[tuple[str, str], object]" = OrderedDict()
_lock = threading.Lock()
_load_lock = threading.Lock()


def get_model(mid: str) -> TTSModel:
    if mid not in model_ids():
        raise HTTPException(400, "Unknown model")
    with _load_lock:
        with _lock:
            if mid in _models:
                _models.move_to_end(mid)
                return _models[mid]
        log.info("loading model %s", mid)
        m = TTSModel.load_model(language=mid)
        with _lock:
            _models[mid] = m
            while len(_models) > MAX_MODELS:
                old, _ = _models.popitem(last=False)
                for k in [k for k in _states if k[0] == old]:
                    del _states[k]
        return m


# ---- sharing: login-less guest links (/s/<token>/; your reverse proxy must let /s/* through without login) ----
SHARES = ROOT / "state" / "shares.json"
SHARED_OWN = ROOT / "state" / "shared-voices.json"   # own clones guests may use
GUESTS = VOICES / "guests"                            # guest clips, one folder per link
GUEST_TEXT_MAX = 3000
GUEST_MAX_PER_LINK = 300   # generations + YouTube clips per link
GUEST_MAX_CLIPS = 30       # saved clips per link (uploads + YouTube)
_guest_busy: set[str] = set()
_guest_count: dict[str, int] = {}
_guest_sem = threading.BoundedSemaphore(2)            # at most 2 guest generations at once
_shares_lock = threading.Lock()


def load_shares() -> dict:
    try:
        return json.loads(SHARES.read_text())
    except Exception:
        return {}


def save_shares(d: dict) -> None:
    SHARES.parent.mkdir(exist_ok=True)
    tmp = SHARES.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    tmp.replace(SHARES)


def shared_own() -> list[str]:
    try:
        return json.loads(SHARED_OWN.read_text())
    except Exception:
        return []


def guest_key(token: str) -> str:
    """Short folder id for a link (not the secret itself)."""
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def check_token(token: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", token):
        raise HTTPException(404, "No such link")
    s = load_shares().get(token)
    if not s:
        raise HTTPException(404, "No such link")
    if s["expires"] < time.time():
        raise HTTPException(410, "This link has expired")
    return guest_key(token)


def guest_quota(g: str, clip: bool) -> None:
    if _guest_count.get(g, 0) >= GUEST_MAX_PER_LINK:
        raise HTTPException(429, "This link has been used up")
    if clip and len(clip_names(GUESTS / g)) >= GUEST_MAX_CLIPS:
        raise HTTPException(429, f"This link can save at most {GUEST_MAX_CLIPS} clips")


def guest_clip(g: str, name: str) -> Path | None:
    if not NAME_RE.match(name):
        return None
    for p in (GUESTS / g).glob(name + ".*"):
        if p.stem == name and p.suffix.lower() in AUDIO_EXT:
            return p
    return None


# ---- voice resolution shared by admin and guests ----
def resolve_src(voice: str, model_id: str, guest: str | None):
    if voice == "":
        return get_default_voice_for_language(model_id)
    if voice.startswith("own:"):
        name = voice[4:]
        if guest is not None and name not in shared_own():
            raise HTTPException(404, "Unknown voice")
        p = own_clip(name)
    elif voice.startswith("guest:"):
        p = guest_clip(guest, voice[6:]) if guest else None
    else:
        src = catalog().get(voice)
        if src is None:
            raise HTTPException(404, "Unknown voice")
        if "://" not in src:
            return src
        p = voice_file(src)
        if p is None:
            raise HTTPException(409, "That voice is not installed yet")
    if p is None:
        raise HTTPException(404, "Unknown voice")
    return p


def get_state(mid: str, model: TTSModel, voice: str, guest: str | None = None):
    key = (mid, f"{guest}/{voice}" if voice.startswith("guest:") else voice)
    with _lock:
        if key in _states:
            _states.move_to_end(key)
            return _states[key]
    state = model.get_state_for_audio_prompt(resolve_src(voice, mid, guest), truncate=True)
    with _lock:
        _states[key] = state
        while len(_states) > MAX_STATES:
            _states.popitem(last=False)
    return state


def forget_voice(voice_key: str) -> None:
    with _lock:
        for k in [k for k in _states if k[1] == voice_key]:
            del _states[k]


# ---- background ambience + tail: mixed into the stream as chunks arrive ----
NOISE = ROOT / "noise"
SR_OUT = 24000
# Speech comes out at about -20 LUFS, the loops are normalised to -30 LUFS. Gain on the loop (dB):
BG_GAIN_DB = {"low": -14.0, "medium": -7.0, "high": -1.0}   # = 24 / 17 / 11 dB under the voice
_noise_cache: dict[str, np.ndarray] = {}


def noise_catalog() -> list[dict]:
    try:
        return [n for n in json.loads((NOISE / "credits.json").read_text()) if (NOISE / f"{n['id']}.wav").exists()]
    except Exception:
        return []


def noise_loop(nid: str) -> np.ndarray:
    """The loop as float32, with its end crossfaded into its start so it repeats without a click."""
    if nid in _noise_cache:
        return _noise_cache[nid]
    if nid not in {n["id"] for n in noise_catalog()}:
        raise HTTPException(400, "Unknown background")
    import soundfile as sf
    x, sr = sf.read(NOISE / f"{nid}.wav", dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    cf = min(SR_OUT, len(x) // 4)
    y = x[: len(x) - cf].copy()
    ramp = np.linspace(0, 1, cf, dtype=np.float32)
    y[:cf] = x[:cf] * ramp + x[len(x) - cf:] * (1 - ramp)
    _noise_cache[nid] = y
    return y


def with_background(chunks, nid: str, level: str, tail: float):
    loop = noise_loop(nid) if nid else None
    gain = 10 ** (BG_GAIN_DB.get(level, BG_GAIN_DB["low"]) / 20)
    pos = int(np.random.randint(0, len(loop))) if loop is not None else 0

    def take(n: int) -> np.ndarray:
        nonlocal pos
        out = np.empty(n, dtype=np.float32)
        i = 0
        while i < n:
            k = min(n - i, len(loop) - pos)
            out[i:i + k] = loop[pos:pos + k]
            i += k
            pos = (pos + k) % len(loop)
        return out * gain

    first = True
    for ch in chunks:
        a = ch.detach().float().cpu().numpy().reshape(-1)
        if loop is not None:
            bg = take(len(a))
            if first:  # fade the background in over 0.15 s
                f = min(len(bg), int(0.15 * SR_OUT))
                bg[:f] *= np.linspace(0, 1, f, dtype=np.float32)
            a = a + bg
        first = False
        yield torch.from_numpy(np.clip(a, -1, 1))
    n = int(tail * SR_OUT)
    if n > 0:
        a = take(n) if loop is not None else np.zeros(n, dtype=np.float32)
        f = min(n, int(0.7 * SR_OUT))   # fade out over the last 0.7 s
        a[n - f:] *= np.linspace(1, 0, f, dtype=np.float32)
        yield torch.from_numpy(a)


def clip_names(folder: Path) -> list[str]:
    return sorted({p.stem for p in folder.glob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXT and NAME_RE.match(p.stem)})


def label_of(n: str) -> str:
    return n.replace("_", " ").replace("-", " ").title()


def build_config(guest: str | None) -> dict:
    groups = load_json("voices.json")
    for g in groups:
        for v in g["voices"]:
            v["installed"] = voice_installed(v.pop("src"))
            v.pop("builtin", None)
        g["job"] = _group_jobs.get(g["group"], "")
    if guest is None:
        own = clip_names(VOICES)
        if own:
            groups.insert(0, {"group": "My cloned voices", "note": "Your own clips; they work with every model.",
                              "voices": [{"id": f"own:{n}", "tab": "mine", "installed": True, "label": label_of(n),
                                          "tags": ["shared with guests"] if n in shared_own() else []} for n in own]})
    else:
        shared = [n for n in clip_names(VOICES) if n in shared_own()]
        mine = clip_names(GUESTS / guest)
        if mine:
            groups.insert(0, {"group": "My cloned voices", "note": "Clips you uploaded with this link.",
                              "voices": [{"id": f"guest:{n}", "tab": "mine", "installed": True, "label": label_of(n)} for n in mine]})
        if shared:
            groups.insert(0, {"group": "Featured", "note": "Cloned voices shared with you.",
                              "voices": [{"id": f"own:{n}", "tab": "featured", "installed": True, "label": label_of(n)} for n in shared]})
    langs = load_json("models.json")
    for lang in langs:
        ms = [{"id": i, "label": l, "installed": installed(i) and _installing.get(i) != "installing",
               "status": _installing.get(i, "")} for i, l in lang["models"]]
        if guest is not None:
            ms = [m for m in ms if m["installed"]]   # guests can't install: hide what isn't there
        lang["models"] = ms
    langs = [l for l in langs if l["models"]]
    return {"languages": langs, "favourites": favourites() if guest is None else [], "voices": groups,
            "texts": DEFAULT_TEXT_FOR_LANGUAGE, "guest": guest is not None,
            "backgrounds": [{"id": n["id"], "label": n["label"], "credit": n["credit"]} for n in noise_catalog()]}


def save_upload(voice_wav: UploadFile, m: TTSModel, model: str, save_as: str | None, folder: Path, prefix: str,
                trim_start: float | None = None, trim_end: float | None = None):
    name = "clip-" + time.strftime("%Y%m%d-%H%M%S")
    if save_as:
        name = save_as.strip().lower().replace(" ", "-")
        if not NAME_RE.match(name):
            raise HTTPException(400, "Name: letters, digits, - and _ only (max 40)")
    data = voice_wav.file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Clip too large (max 30 MB)")
    suffix = Path(voice_wav.filename or "").suffix.lower()
    if suffix not in AUDIO_EXT:
        suffix = ".wav"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=ROOT / "tmp") as f:
        f.write(data)
        orig = Path(f.name)
    # Any format a phone records (ogg/opus voice notes, m4a, webm, mp3…) → mono 24 kHz 16-bit WAV.
    tmp = orig.with_suffix(".conv.wav")
    try:
        trim = []
        if trim_start is not None and trim_end is not None and trim_end - trim_start >= 1:
            trim = ["-ss", f"{max(0.0, trim_start):.2f}", "-to", f"{trim_end:.2f}"]
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(orig), *trim, "-vn", "-ac", "1", "-ar", "24000",
                        "-c:a", "pcm_s16le", str(tmp)], check=True, capture_output=True, timeout=120)
        state = m.get_state_for_audio_prompt(tmp, truncate=True)
    except Exception:
        orig.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)
        log.exception("could not read uploaded clip")
        raise HTTPException(400, "Could not read that audio file")
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    for old in folder.glob(name + ".*"):
        if old.stem == name:  # keep the replaced clip too
            (folder / ".replaced").mkdir(exist_ok=True)
            shutil.move(old, folder / ".replaced" / f"{name}-{stamp}{old.suffix}")
    (folder / ".originals").mkdir(exist_ok=True)   # the file exactly as uploaded
    shutil.move(orig, folder / ".originals" / f"{name}-{stamp}{suffix}")
    shutil.move(tmp, folder / f"{name}.wav")
    forget_voice(prefix + name)
    log.info("saved clip %s in %s", name, folder)
    return state


def check_bg(bg: str, bg_level: str, tail: float):
    if bg and bg not in {n["id"] for n in noise_catalog()}:
        raise HTTPException(400, "Unknown background")
    if bg_level not in BG_GAIN_DB:
        raise HTTPException(400, "Level must be low, medium or high")
    if not 0 <= tail <= 5:
        raise HTTPException(400, "Extra time must be 0–5 s")
    return bg, tail


def check_model(model: str) -> TTSModel:
    if model not in model_ids():
        raise HTTPException(400, "Unknown model")
    if not installed(model):
        raise HTTPException(409, "That model is not installed yet")
    return get_model(model)


def wav_response(gen) -> StreamingResponse:
    return StreamingResponse(gen, media_type="audio/wav",
                             headers={"Content-Disposition": "attachment; filename=tts.wav", "Cache-Control": "no-store"})


def sample_path(vid: str, guest: str | None) -> Path:
    if vid.startswith("own:") or vid.startswith("guest:"):
        p = resolve_src(vid, DEFAULT_MODEL, guest)
    else:
        src = catalog().get(vid)
        if src and "://" not in src:  # built-in: preview its source clip
            src = _ORIGINS_OF_PREDEFINED_VOICES.get(src, "").split("@")[0]
            if repo_path(src) and voice_file(src) is None:
                install_voice_src(src)
        p = voice_file(src) if src else None
    if not isinstance(p, Path):
        raise HTTPException(404, "No clip for that voice")
    return p


# ---- text → parts: [pause …] markers and "Name: line" conversations ----
PAUSE_RE = re.compile(r"\[(?:pause|break)(?:\s+(\d+(?:[.,]\d+)?)\s*(ms|s)?)?\]", re.I)
LINE_RE = re.compile(r"^\s*([^\W\d_][\w .'’-]{0,39}?)\s*:\s*(\S.*)$")
TURN_GAP = 0.35          # silence between speakers in a conversation
MAX_PARTS = 400


def voice_names(guest: str | None) -> dict[str, str]:
    """lower-case name → voice id. Clones first, then the catalog in page order (built-ins first)."""
    names: dict[str, str] = {}

    def add(key: str, vid: str):
        names.setdefault(key.lower().strip(), vid)

    if guest is None:
        for n in clip_names(VOICES):
            add(label_of(n), f"own:{n}"); add(n, f"own:{n}")
    else:
        for n in clip_names(VOICES):
            if n in shared_own():
                add(label_of(n), f"own:{n}"); add(n, f"own:{n}")
        for n in clip_names(GUESTS / guest):
            add(label_of(n), f"guest:{n}"); add(n, f"guest:{n}")
    for g in load_json("voices.json"):
        for v in g["voices"]:
            add(v["label"], v["id"]); add(v["id"], v["id"])
    return names


def parse_text(text: str, guest: str | None) -> dict:
    """→ {"parts": [("say", voice|None, text) | ("pause", seconds)], "cast": [...], "unknown": [...]}"""
    names = voice_names(guest)
    lines = text.splitlines()
    matches = [LINE_RE.match(l) for l in lines]
    script = any(m and m.group(1).lower().strip() in names for m in matches)
    blocks: list[tuple[str | None, str]] = []
    unknown: list[str] = []
    if script:
        for line, m in zip(lines, matches):
            if m and m.group(1).lower().strip() in names:
                blocks.append((names[m.group(1).lower().strip()], m.group(2)))
            elif line.strip():
                if m and m.group(1) not in unknown and len(m.group(1).split()) <= 3:
                    unknown.append(m.group(1))
                if blocks:   # a line without a known name continues the previous speaker
                    blocks[-1] = (blocks[-1][0], blocks[-1][1] + " " + line.strip())
                else:
                    blocks.append((None, line.strip()))
    else:
        blocks = [(None, text)]
    parts: list[tuple] = []
    for voice, t in blocks:
        pos = 0
        for m in PAUSE_RE.finditer(t):
            if t[pos:m.start()].strip():
                parts.append(("say", voice, t[pos:m.start()].strip()))
            secs = float((m.group(1) or "0.7").replace(",", "."))
            if (m.group(2) or "").lower() == "ms":
                secs /= 1000
            parts.append(("pause", max(0.05, min(secs, 10.0))))
            pos = m.end()
        if t[pos:].strip():
            parts.append(("say", voice, t[pos:].strip()))
        if script:
            parts.append(("pause", TURN_GAP))
    while parts and parts[-1][0] == "pause" and script:
        parts.pop()
    if len(parts) > MAX_PARTS:
        raise HTTPException(400, f"Too many lines/pauses (max {MAX_PARTS})")
    if not any(p[0] == "say" for p in parts):
        raise HTTPException(400, "Nothing to say")
    cast = list(dict.fromkeys(p[1] for p in parts if p[0] == "say" and p[1]))
    return {"parts": parts, "cast": cast, "unknown": unknown, "script": script}


# ---- history: the last generations, per admin / per share link ----
HISTORY = ROOT / "state" / "history"
HISTORY_KEEP = 20
HID_RE = re.compile(r"^\d{13,16}$")


def history_dir(who: str) -> Path:
    return HISTORY / ("admin" if who == "admin" else re.sub(r"[^a-z0-9]", "", who))


def save_history(who: str, audio: np.ndarray, sr: int, meta: dict) -> None:
    d = history_dir(who)
    d.mkdir(parents=True, exist_ok=True)
    hid = str(int(time.time() * 1000))
    import soundfile as sf
    sf.write(d / f"{hid}.wav", audio, sr, subtype="PCM_16")
    (d / f"{hid}.json").write_text(json.dumps({**meta, "id": hid, "time": time.time(), "seconds": round(len(audio) / sr, 1)}))
    old = sorted(p.stem for p in d.glob("*.json"))[:-HISTORY_KEEP]
    for h in old:
        (d / f"{h}.json").unlink(missing_ok=True)
        (d / f"{h}.wav").unlink(missing_ok=True)


def list_history(who: str) -> list:
    d = history_dir(who)
    out = []
    for p in sorted(d.glob("*.json"), reverse=True):
        try:
            out.append(json.loads(p.read_text()))
        except Exception:
            pass
    return out


def history_file(who: str, hid: str) -> Path:
    p = history_dir(who) / f"{hid}.wav"
    if not HID_RE.match(hid) or not p.exists():
        raise HTTPException(404, "Not in history")
    return p


# ---- generation: parts → chunks → background → speed → stream (+ history) ----
def chunk_stream(model: TTSModel, mid: str, parts: list, default_state, guest: str | None, stop: threading.Event):
    sr = model.config.mimi.sample_rate
    for p in parts:
        if stop.is_set():
            return
        if p[0] == "pause":
            yield torch.zeros(int(p[1] * sr))
        else:
            state = default_state if p[1] is None else get_state(mid, model, p[1], guest)
            yield from model.generate_audio_stream(model_state=state, text_to_generate=p[2], stop=stop)


def apply_speed(audio: np.ndarray, sr: int, speed: float) -> np.ndarray:
    """Tempo change that keeps the pitch (ffmpeg atempo)."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", "pipe:0",
                        "-filter:a", f"atempo={speed:.3f}", "-f", "f32le", "-ar", str(sr), "-ac", "1", "pipe:1"],
                       input=audio.astype(np.float32).tobytes(), capture_output=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode()[-200:])
    return np.frombuffer(r.stdout, dtype=np.float32)


def render(model, mid, parts, default_state, guest, opts) -> tuple[np.ndarray, int]:
    """Whole clip at once (API, speed ≠ 1)."""
    stop = threading.Event()
    sr = model.config.mimi.sample_rate
    chunks = chunk_stream(model, mid, parts, default_state, guest, stop)
    if opts["bg"] or opts["tail"] > 0:
        chunks = with_background(chunks, opts["bg"], opts["bg_level"], opts["tail"])
    audio = np.concatenate([c.detach().float().cpu().numpy().reshape(-1) for c in chunks] or [np.zeros(1, np.float32)])
    if abs(opts["speed"] - 1.0) > 0.01:
        audio = apply_speed(audio, sr, opts["speed"])
    return np.clip(audio, -1, 1), sr


def stream(model, mid, parts, default_state, guest, opts, on_done=None, record=None):
    """Streams a WAV while generating (speed 1.0), or renders first and then sends it (other speeds)."""
    q: Queue = Queue()
    stop = threading.Event()
    sr = model.config.mimi.sample_rate

    class ToQueue(io.IOBase):
        def write(self, data):
            q.put(bytes(data))

        def flush(self):
            pass

        def close(self):
            q.put(None)

    def work():
        kept: list[np.ndarray] = []
        try:
            if abs(opts["speed"] - 1.0) > 0.01:
                audio, _ = render(model, mid, parts, default_state, guest, opts)
                kept = [audio]
                stream_audio_chunks(ToQueue(), iter([torch.from_numpy(audio)]), sr)
            else:
                chunks = chunk_stream(model, mid, parts, default_state, guest, stop)
                if opts["bg"] or opts["tail"] > 0:
                    chunks = with_background(chunks, opts["bg"], opts["bg_level"], opts["tail"])

                def tee(cs):
                    for c in cs:
                        kept.append(c.detach().float().cpu().numpy().reshape(-1))
                        yield c
                stream_audio_chunks(ToQueue(), tee(chunks), sr)
            if record and kept and not stop.is_set():
                record(np.concatenate(kept), sr)
        except Exception:
            log.exception("generation failed")
            q.put(None)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    try:
        while (d := q.get()) is not None:
            yield d
    finally:
        stop.set()
        t.join()
        if on_done:
            on_done()


def check_opts(bg: str, bg_level: str, tail: float, speed: float) -> dict:
    bg, tail = check_bg(bg, bg_level, tail)
    if not 0.7 <= speed <= 1.5:
        raise HTTPException(400, "Speed must be 0.7–1.5")
    return {"bg": bg, "bg_level": bg_level, "tail": tail, "speed": speed}


def encode(audio: np.ndarray, sr: int, fmt: str) -> tuple[bytes, str]:
    if fmt == "wav":
        buf = io.BytesIO()
        import soundfile as sf
        sf.write(buf, audio, sr, format="WAV", subtype="PCM_16")
        return buf.getvalue(), "audio/wav"
    args = {"mp3": (["-c:a", "libmp3lame", "-b:a", "96k", "-f", "mp3"], "audio/mpeg"),
            "ogg": (["-c:a", "libopus", "-b:a", "48k", "-application", "voip", "-f", "ogg"], "audio/ogg")}
    if fmt not in args:
        raise HTTPException(400, "format: mp3, ogg or wav")
    a, mime = args[fmt]
    r = subprocess.run(["ffmpeg", "-v", "error", "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", "pipe:0", *a, "pipe:1"],
                       input=audio.astype(np.float32).tobytes(), capture_output=True, timeout=300)
    if r.returncode != 0:
        raise HTTPException(500, "Encoding failed")
    return r.stdout, mime


def page_html(guest_info: dict | None) -> str:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    inject = f"<script>window.GUEST = {json.dumps(guest_info)};</script>"
    return html.replace("<!--GUEST-->", inject, 1)


# ---- admin routes (put these behind your reverse proxy's login) ----
@app.get("/", response_class=HTMLResponse)
async def page() -> str:
    return page_html(None)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "loaded": list(_models)}


@app.get("/config")
async def config() -> JSONResponse:
    return JSONResponse(build_config(None), headers={"Cache-Control": "no-store"})


def recorder(who: str, text: str, voice: str, model: str, opts: dict, cast: list):
    meta = {"text": text[:400], "voice": voice, "cast": cast, "model": model, **opts}
    return lambda audio, sr: save_history(who, audio, sr, meta)


def parse_summary(text: str, guest: str | None) -> dict:
    p = parse_text(text, guest) if text.strip() else {"cast": [], "unknown": [], "script": False, "parts": []}
    return {"cast": p["cast"], "unknown": p["unknown"], "script": p["script"],
            "pauses": sum(1 for x in p["parts"] if x[0] == "pause" and x[1] != TURN_GAP)}


@app.post("/tts")
def tts(text: str = Form(...), model: str = Form(DEFAULT_MODEL), voice: str = Form(""),
        voice_wav: UploadFile | None = File(None), save_as: str | None = Form(None),
        trim_start: float | None = Form(None), trim_end: float | None = Form(None),
        bg: str = Form(""), bg_level: str = Form("low"), tail: float = Form(1.0), speed: float = Form(1.0)) -> StreamingResponse:
    text = text.strip()
    if not text:
        raise HTTPException(400, "Text cannot be empty")
    if len(text) > 20000:
        raise HTTPException(400, "Text too long (max 20 000 characters)")
    opts = check_opts(bg, bg_level, tail, speed)
    parsed = parse_text(text, None)
    m = check_model(model)
    if voice_wav is not None and voice_wav.filename:
        state = save_upload(voice_wav, m, model, save_as, VOICES, "own:", trim_start, trim_end)
        voice = "(uploaded clip)"
    else:
        state = get_state(model, m, voice)
    return wav_response(stream(m, model, parsed["parts"], state, None, opts,
                               record=recorder("admin", text, voice, model, opts, parsed["cast"])))


@app.post("/parse")
def parse_route(text: str = Form(...)) -> dict:
    return parse_summary(text, None)


@app.get("/history")
def history_route() -> list:
    return list_history("admin")


@app.get("/history/{hid}.wav")
def history_wav(hid: str):
    return FileResponse(history_file("admin", hid), media_type="audio/wav", filename=f"tts-{hid}.wav")


@app.delete("/history/{hid}")
def history_delete(hid: str) -> dict:
    p = history_file("admin", hid)
    p.unlink(); p.with_suffix(".json").unlink(missing_ok=True)
    return {"deleted": hid}


@app.post("/install/{mid}")
def install(mid: str) -> dict:
    if mid not in model_ids():
        raise HTTPException(400, "Unknown model")
    if installed(mid) and mid in _models:
        return {"status": "installed"}
    if _installing.get(mid) != "installing":
        _installing[mid] = "installing"
        threading.Thread(target=_install, args=(mid,), daemon=True).start()
    return {"status": "installing"}


FAVS = ROOT / "state" / "favourites.json"


def favourites() -> list[str]:
    try:
        return json.loads(FAVS.read_text())
    except Exception:
        return []


@app.post("/favourite")
def favourite(voice: str = Form(...), on: bool = Form(...)) -> dict:
    if len(voice) > 120 or not (voice in catalog() or (voice.startswith("own:") and own_clip(voice[4:]))):
        raise HTTPException(404, "Unknown voice")
    favs = [f for f in favourites() if f != voice] + ([voice] if on else [])
    FAVS.parent.mkdir(exist_ok=True)
    FAVS.write_text(json.dumps(favs))
    return {"favourites": favs}


@app.post("/install-voice/{vid}")
def install_voice(vid: str) -> dict:
    src = catalog().get(vid)
    if src is None:
        raise HTTPException(404, "Unknown voice")
    if not voice_installed(src):
        install_voice_src(src)
    return {"installed": vid}


@app.get("/sample/{vid}")
def sample(vid: str):
    return FileResponse(sample_path(vid, None), headers={"Cache-Control": "private, max-age=86400"})


@app.delete("/voices/{name}")
async def delete_voice(name: str) -> dict:
    p = own_clip(name)
    if p is None:
        raise HTTPException(404, "Unknown voice")
    (VOICES / ".deleted").mkdir(exist_ok=True)  # never really deleted: moved aside
    stamp = time.strftime('%Y%m%d-%H%M%S')
    shutil.move(p, VOICES / ".deleted" / f"{name}-{stamp}{p.suffix}")
    meta = VOICES / f".{name}.source.json"
    if meta.exists():
        shutil.move(meta, VOICES / ".deleted" / f"{name}-{stamp}.source.json")
    forget_voice(f"own:{name}")
    return {"deleted": name}


@app.post("/share-voice")
def share_voice(name: str = Form(...), on: bool = Form(...)) -> dict:
    if not own_clip(name):
        raise HTTPException(404, "Unknown voice")
    cur = [n for n in shared_own() if n != name] + ([name] if on else [])
    SHARED_OWN.parent.mkdir(exist_ok=True)
    SHARED_OWN.write_text(json.dumps(cur))
    return {"shared": cur}


@app.get("/shares")
def shares() -> list:
    now = time.time()
    out = []
    for tok, s in sorted(load_shares().items(), key=lambda kv: -kv[1]["created"]):
        if s["expires"] < now - 86400:   # hide links expired more than a day ago
            continue
        g = guest_key(tok)
        out.append({"token": tok, "label": s.get("label", ""), "created": s["created"], "expires": s["expires"],
                    "active": s["expires"] >= now, "uses": _guest_count.get(g, 0), "clips": len(clip_names(GUESTS / g))})
    return out


@app.post("/shares")
def create_share(hours: float = Form(24), label: str = Form("")) -> dict:
    if not 0.25 <= hours <= 24 * 30:
        raise HTTPException(400, "Between 15 minutes and 30 days")
    tok = secrets.token_urlsafe(24)
    now = time.time()
    with _shares_lock:
        d = load_shares()
        d[tok] = {"created": now, "expires": now + hours * 3600, "label": label.strip()[:60]}
        save_shares(d)
    log.info("share link created (%s h, %r)", hours, label)
    return {"token": tok, "path": f"/s/{tok}/", "expires": now + hours * 3600}


@app.delete("/shares/{tok}")
def revoke_share(tok: str) -> dict:
    with _shares_lock:
        d = load_shares()
        if tok not in d:
            raise HTTPException(404, "No such link")
        d[tok]["expires"] = min(d[tok]["expires"], time.time() - 1)
        save_shares(d)
    return {"revoked": True}


# ---- guest routes: /s/<token>/... (no login; the token is checked on every request) ----
@app.get("/s/{token}")
async def guest_slash(token: str):
    check_token(token)
    return RedirectResponse(f"/s/{token}/", status_code=302)


@app.get("/s/{token}/", response_class=HTMLResponse)
async def guest_page(token: str):
    try:
        check_token(token)
    except HTTPException as e:
        msg = "This link has expired." if e.status_code == 410 else "This link doesn't work."
        return HTMLResponse(f"<!doctype html><meta name=viewport content='width=device-width'><title>Pocket TTS</title>"
                            f"<body style='font:16px system-ui;padding:40px;text-align:center'><h2>{msg}</h2>"
                            f"<p>Ask for a new link.</p></body>", status_code=e.status_code)
    s = load_shares()[token]
    return page_html({"expires": s["expires"]})


@app.get("/s/{token}/config")
async def guest_config(token: str) -> JSONResponse:
    g = check_token(token)
    return JSONResponse(build_config(g), headers={"Cache-Control": "no-store"})


@app.get("/s/{token}/sample/{vid}")
def guest_sample(token: str, vid: str):
    g = check_token(token)
    if not (vid.startswith(("own:", "guest:")) or vid in catalog()):
        raise HTTPException(404, "Unknown voice")
    return FileResponse(sample_path(vid, g), headers={"Cache-Control": "private, max-age=3600"})


@app.post("/s/{token}/tts")
def guest_tts(token: str, text: str = Form(...), model: str = Form(DEFAULT_MODEL), voice: str = Form(""),
              voice_wav: UploadFile | None = File(None), save_as: str | None = Form(None),
              trim_start: float | None = Form(None), trim_end: float | None = Form(None),
              bg: str = Form(""), bg_level: str = Form("low"), tail: float = Form(1.0),
              speed: float = Form(1.0)) -> StreamingResponse:
    g = check_token(token)
    opts = check_opts(bg, bg_level, tail, speed)
    text = text.strip()
    if not text:
        raise HTTPException(400, "Text cannot be empty")
    if len(text) > GUEST_TEXT_MAX:
        raise HTTPException(400, f"Text too long (max {GUEST_TEXT_MAX} characters)")
    guest_quota(g, clip=voice_wav is not None and bool(voice_wav.filename))
    parsed = parse_text(text, g)
    if g in _guest_busy:
        raise HTTPException(429, "Wait for the current one to finish")
    if not _guest_sem.acquire(blocking=False):
        raise HTTPException(429, "Busy right now, try again in a moment")
    _guest_busy.add(g)

    def done():
        _guest_busy.discard(g)
        _guest_sem.release()

    try:
        m = check_model(model)
        if voice_wav is not None and voice_wav.filename:
            state = save_upload(voice_wav, m, model, save_as, GUESTS / g, f"{g}/guest:", trim_start, trim_end)
            voice = "(uploaded clip)"
        else:
            state = get_state(model, m, voice, g)
        _guest_count[g] = _guest_count.get(g, 0) + 1
        return wav_response(stream(m, model, parsed["parts"], state, g, opts, on_done=done,
                                   record=recorder(g, text, voice, model, opts, parsed["cast"])))
    except Exception:
        done()
        raise


# ---- download as OGG (Opus, voice-note format) ----
def to_ogg(audio: UploadFile):
    data = audio.file.read(200 * 1024 * 1024 + 1)
    if len(data) > 200 * 1024 * 1024:
        raise HTTPException(413, "Too large")
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-vn", "-ac", "1", "-c:a", "libopus", "-b:a", "48k",
                        "-application", "voip", "-f", "ogg", "pipe:1"], input=data, capture_output=True, timeout=120)
    if r.returncode != 0 or not r.stdout:
        raise HTTPException(400, "Could not convert that audio")
    from fastapi.responses import Response
    return Response(r.stdout, media_type="audio/ogg", headers={"Content-Disposition": "attachment; filename=tts.ogg"})


@app.post("/convert/ogg")
def convert_ogg(audio: UploadFile = File(...)):
    return to_ogg(audio)


@app.post("/s/{token}/convert/ogg")
def guest_convert_ogg(token: str, audio: UploadFile = File(...)):
    check_token(token)
    return to_ogg(audio)


# ---- YouTube clips: pick a segment of a video, save it as a voice ----
YTDLP = str(ROOT / ".venv" / "bin" / "yt-dlp")
YT_ID = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/|live/)|youtu\.be/)([A-Za-z0-9_-]{11})")
YT_MAX_SEG = 120
_yt_info: dict[str, dict] = {}


def yt_id(url: str) -> str:
    url = url.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return url
    m = YT_ID.search(url)
    if not m:
        raise HTTPException(400, "That doesn't look like a YouTube link")
    return m.group(1)


def yt_info(url: str) -> dict:
    vid = yt_id(url)
    if vid in _yt_info:
        return _yt_info[vid]
    r = subprocess.run([YTDLP, "-j", "--no-playlist", "--no-warnings", f"https://www.youtube.com/watch?v={vid}"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        log.warning("yt-dlp info failed: %s", r.stderr[-300:])
        raise HTTPException(400, "Couldn't read that video (private, removed or age-restricted?)")
    d = json.loads(r.stdout.strip().splitlines()[-1])
    if not d.get("duration"):
        raise HTTPException(400, "That video has no fixed length (a live stream?)")
    info = {"id": vid, "title": d.get("title", ""), "duration": int(d["duration"]), "channel": d.get("channel", "")}
    _yt_info[vid] = info
    return info


def yt_clip(url: str, start: float, end: float, name: str, folder: Path, prefix: str) -> str:
    info = yt_info(url)
    start, end = max(0.0, start), min(float(info["duration"]), end)
    if end - start < 3:
        raise HTTPException(400, "Pick at least 3 seconds")
    if end - start > YT_MAX_SEG:
        raise HTTPException(400, f"Pick at most {YT_MAX_SEG} seconds")
    name = (name or "").strip().lower().replace(" ", "-") or "yt-" + time.strftime("%Y%m%d-%H%M%S")
    if not NAME_RE.match(name):
        raise HTTPException(400, "Name: letters, digits, - and _ only (max 40)")
    work = Path(tempfile.mkdtemp(dir=ROOT / "tmp"))
    try:
        r = subprocess.run([YTDLP, "--no-playlist", "--no-warnings", "-f", "bestaudio/best", "-x", "--audio-format", "wav",
                            "--download-sections", f"*{start:.2f}-{end:.2f}", "--force-keyframes-at-cuts",
                            "-o", str(work / "seg.%(ext)s"), f"https://www.youtube.com/watch?v={info['id']}"],
                           capture_output=True, text=True, timeout=300)
        got = next(iter(work.glob("seg.*")), None)
        if r.returncode != 0 or got is None:
            log.warning("yt-dlp clip failed: %s", r.stderr[-300:])
            raise HTTPException(502, "Download failed, try again")
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob(name + ".*"):
            if old.stem == name:
                (folder / ".replaced").mkdir(exist_ok=True)
                shutil.move(old, folder / ".replaced" / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}{old.suffix}")
        out = folder / f"{name}.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(got), "-t", f"{end - start:.2f}", "-ac", "1", "-ar", "24000", str(out)],
                       check=True, timeout=120)
        (folder / f".{name}.source.json").write_text(json.dumps({**info, "start": start, "end": end, "saved": time.time()}))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    forget_voice(prefix + name)
    log.info("saved YouTube clip %s (%s %.0f-%.0f)", name, info["id"], start, end)
    return name


@app.post("/yt/info")
def yt_info_route(url: str = Form(...)) -> dict:
    return yt_info(url)


@app.post("/yt/clip")
def yt_clip_route(url: str = Form(...), start: float = Form(...), end: float = Form(...), name: str = Form("")) -> dict:
    return {"voice": "own:" + yt_clip(url, start, end, name, VOICES, "own:")}


@app.post("/s/{token}/yt/info")
def guest_yt_info(token: str, url: str = Form(...)) -> dict:
    check_token(token)
    return yt_info(url)


@app.post("/s/{token}/yt/clip")
def guest_yt_clip(token: str, url: str = Form(...), start: float = Form(...), end: float = Form(...), name: str = Form("")) -> dict:
    g = check_token(token)
    guest_quota(g, clip=True)
    if g in _guest_busy:
        raise HTTPException(429, "Wait for the current one to finish")
    _guest_busy.add(g)
    try:
        _guest_count[g] = _guest_count.get(g, 0) + 1
        return {"voice": "guest:" + yt_clip(url, start, end, name, GUESTS / g, f"{g}/guest:")}
    finally:
        _guest_busy.discard(g)


@app.post("/s/{token}/parse")
def guest_parse(token: str, text: str = Form(...)) -> dict:
    return parse_summary(text, check_token(token))


@app.get("/s/{token}/history")
def guest_history(token: str) -> list:
    return list_history(check_token(token))


@app.get("/s/{token}/history/{hid}.wav")
def guest_history_wav(token: str, hid: str):
    return FileResponse(history_file(check_token(token), hid), media_type="audio/wav", filename=f"tts-{hid}.wav")


@app.delete("/s/{token}/history/{hid}")
def guest_history_delete(token: str, hid: str) -> dict:
    p = history_file(check_token(token), hid)
    p.unlink(); p.with_suffix(".json").unlink(missing_ok=True)
    return {"deleted": hid}


# ---- API for scripts / Home Assistant: /api/* with a key (your proxy lets /api/* through without login) ----
API_KEYS = ROOT / "state" / "api-keys.json"
_api_lock = threading.Lock()


def load_keys() -> dict:
    try:
        return json.loads(API_KEYS.read_text())
    except Exception:
        return {}


def save_keys(d: dict) -> None:
    API_KEYS.parent.mkdir(exist_ok=True)
    tmp = API_KEYS.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    tmp.chmod(0o600)
    tmp.replace(API_KEYS)


def check_api_key(request: Request) -> str:
    key = request.query_params.get("key") or request.headers.get("x-api-key") or ""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        key = auth[7:].strip()
    if not key:
        raise HTTPException(401, "API key required (?key=, X-API-Key or Authorization: Bearer)")
    h = hashlib.sha256(key.encode()).hexdigest()
    with _api_lock:
        keys = load_keys()
        if h not in keys:
            raise HTTPException(401, "Invalid API key")
        keys[h]["last_used"] = time.time()
        keys[h]["uses"] = keys[h].get("uses", 0) + 1
        save_keys(keys)
    return h[:8]


@app.get("/api-keys")
def api_keys_list() -> list:
    return [{"id": h[:8], "label": k.get("label", ""), "created": k["created"], "last_used": k.get("last_used"),
             "uses": k.get("uses", 0)} for h, k in sorted(load_keys().items(), key=lambda kv: kv[1]["created"])]


@app.post("/api-keys")
def api_keys_create(label: str = Form("")) -> dict:
    key = "tts_" + secrets.token_urlsafe(24)
    with _api_lock:
        keys = load_keys()
        keys[hashlib.sha256(key.encode()).hexdigest()] = {"label": label.strip()[:60], "created": time.time()}
        save_keys(keys)
    return {"key": key}   # shown once; only its hash is stored


@app.delete("/api-keys/{kid}")
def api_keys_delete(kid: str) -> dict:
    with _api_lock:
        keys = load_keys()
        hit = [h for h in keys if h[:8] == kid]
        if not hit:
            raise HTTPException(404, "No such key")
        for h in hit:
            del keys[h]
        save_keys(keys)
    return {"deleted": kid}


async def api_params(request: Request) -> dict:
    params = dict(request.query_params)
    if request.method == "POST":
        if "application/json" in request.headers.get("content-type", ""):
            params.update({k: str(v) for k, v in (await request.json()).items()})
        else:
            params.update({k: v for k, v in (await request.form()).items() if isinstance(v, str)})
    params.pop("key", None)
    return params


def api_voice(v: str) -> str:
    """Accept a voice id or a name ("Alba", "David Attenborough")."""
    if not v or v in catalog() or v.startswith("own:"):
        return v
    vid = voice_names(None).get(v.lower().strip())
    if not vid:
        raise HTTPException(404, f"Unknown voice {v!r} (see /api/voices)")
    return vid


def api_render(params: dict, fmt: str):
    text = (params.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    if len(text) > 5000:
        raise HTTPException(400, "text too long (max 5000 characters)")
    model = params.get("model") or DEFAULT_MODEL
    voice = api_voice(params.get("voice") or "")
    try:
        opts = check_opts(params.get("bg", ""), params.get("bg_level", "low"),
                          float(params.get("tail", 0.5)), float(params.get("speed", 1.0)))
    except ValueError:
        raise HTTPException(400, "tail and speed must be numbers")
    parsed = parse_text(text, None)
    m = check_model(model)
    state = get_state(model, m, voice)
    audio, sr = render(m, model, parsed["parts"], state, None, opts)
    data, mime = encode(audio, sr, fmt)
    from fastapi.responses import Response
    return Response(data, media_type=mime, headers={"Content-Disposition": f"inline; filename=tts.{fmt}",
                                                    "Cache-Control": "no-store"})


@app.api_route("/api/tts", methods=["GET", "POST"])
async def api_tts(request: Request):
    """text, voice (id or name), model, format (mp3|ogg|wav, default mp3), bg, bg_level, tail, speed."""
    check_api_key(request)
    params = await api_params(request)
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(api_render, params, params.get("format", "mp3"))


@app.api_route("/api/tts.{fmt}", methods=["GET", "POST"])
async def api_tts_ext(request: Request, fmt: str):
    """Same, with the format in the URL (some players want a file extension, e.g. Cast speakers)."""
    check_api_key(request)
    params = await api_params(request)
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(api_render, params, fmt)


@app.get("/api/voices")
async def api_voices(request: Request) -> list:
    check_api_key(request)
    out = [{"id": f"own:{n}", "name": label_of(n), "group": "My cloned voices"} for n in clip_names(VOICES)]
    for g in load_json("voices.json"):
        out += [{"id": v["id"], "name": v["label"], "group": g["group"]} for v in g["voices"]]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18700)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    (ROOT / "tmp").mkdir(exist_ok=True)
    for p in (ROOT / "tmp").glob("*"):
        p.unlink(missing_ok=True)
    get_state(DEFAULT_MODEL, get_model(DEFAULT_MODEL), "")
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=False)


if __name__ == "__main__":
    main()
