"""Median voice pitch (Hz) of every catalog clip → web/meta/pitch.json. Clips must be in the HF cache.
Run: .venv/bin/python web/pitch.py"""
import json, subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import numpy as np
from huggingface_hub import try_to_load_from_cache
from pocket_tts.utils.utils import _ORIGINS_OF_PREDEFINED_VOICES

WEB = Path(__file__).parent
PRE = "hf://kyutai/tts-voices/"
SR = 16000

def f0(path: str) -> float | None:
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-t", "30", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    x = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768
    n, hop = 1024, 320
    rms_all = np.sqrt(np.mean(x ** 2)) + 1e-9
    out = []
    for i in range(0, len(x) - n, hop):
        fr = x[i:i + n] * np.hanning(n)
        if np.sqrt(np.mean(fr ** 2)) < 0.5 * rms_all:
            continue
        ac = np.fft.irfft(np.abs(np.fft.rfft(fr, 2 * n)) ** 2)[:n]
        lo, hi = SR // 400, SR // 65
        k = lo + int(np.argmax(ac[lo:hi]))
        if ac[k] > 0.45 * ac[0]:
            out.append(SR / k)
    return float(np.median(out)) if len(out) > 20 else None

srcs = set()
for g in json.loads((WEB / "voices.json").read_text()):
    for v in g["voices"]:
        s = v["src"] if "://" in v["src"] else _ORIGINS_OF_PREDEFINED_VOICES[v["src"]].split("@")[0]
        if s.startswith(PRE):
            srcs.add(s)

def one(s):
    p = try_to_load_from_cache("kyutai/tts-voices", s[len(PRE):])
    try:
        return s, (round(f0(p), 1) if isinstance(p, str) else None)
    except Exception:
        return s, None

with ThreadPoolExecutor(12) as ex:
    res = dict(ex.map(one, sorted(srcs)))
(WEB / "meta" / "pitch.json").write_text(json.dumps(res, indent=0, sort_keys=True))
print(len(res), "clips,", sum(v is None for v in res.values()), "unmeasured")
