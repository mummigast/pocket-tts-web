"""Rebuild web/voices.json from kyutai/tts-voices (every voice clip, enhanced version preferred).
Run: .venv/bin/python web/build_catalog.py"""
import json, re
from collections import defaultdict
from pathlib import Path
from huggingface_hub import HfApi
from pocket_tts.utils.utils import _ORIGINS_OF_PREDEFINED_VOICES

R = "hf://kyutai/tts-voices/"
AUD = (".wav", ".mp3", ".flac", ".ogg", ".m4a")
files = [f for f in HfApi().list_repo_files("kyutai/tts-voices") if f.lower().endswith(AUD)]
fileset = set(files)

def pick(fs):
    """Drop originals that have an _enhanced twin."""
    out = []
    for f in fs:
        stem, ext = f.rsplit(".", 1)
        if not stem.endswith("_enhanced") and f"{stem}_enhanced.{ext}" in fileset:
            continue
        out.append(f)
    return sorted(out)

def nice(s):
    s = re.sub(r"_enhanced$", "", s).replace("_", " ").replace("-", " ").strip()
    return s[:1].upper() + s[1:]

def slug(f):
    return re.sub(r"[^a-z0-9]+", "-", f.lower().rsplit(".", 1)[0]).strip("-")

groups = []
def group(name, note, voices):
    if voices:
        groups.append({"group": name, "note": note, "voices": voices})

# Built-ins: precomputed, always installed.
builtin_lang = {"giovanni": "Italian", "lola": "Spanish", "juergen": "German", "rafael": "Portuguese", "daan": "Dutch", "estelle": "French"}
group("Built-in (fast)", "Kyutai's ready-made voices: instant, work with every model (sources: VCTK, EARS, Expresso, donations…).",
      [{"id": n, "label": n.replace("_", " ").title() + (f" ({builtin_lang[n]})" if n in builtin_lang else ""), "src": n, "builtin": True}
       for n in _ORIGINS_OF_PREDEFINED_VOICES])

def coll(prefix):
    return pick([f for f in files if f.startswith(prefix)])

# Hand-picked moods with friendly names (same clips as in the Expresso group below).
PICKS = Path(__file__).with_name("picks.json")
if PICKS.exists():
    for g in json.loads(PICKS.read_text()):
        group(g["group"], g["note"], [{**v, "id": "pick-" + v["id"]} for v in g["voices"]])

group("Alba – characters", "Alba MacKenna, one actor in different roles (CC BY 4.0).",
      [{"id": slug(f), "label": "Alba – " + nice(Path(f).stem), "src": R + f} for f in coll("alba-mackenna/")])
group("Kyutai demo voices", "From Kyutai's Unmute demo site (mostly CC0; De Gaulle 1940 recording, one VCTK and one Expresso clip).",
      [{"id": slug(f), "label": nice(Path(f).stem), "src": R + f} for f in coll("unmute-prod-website/")])
group("Voice-zero", "LibriVox readers via Voice-Zero (CC0).", [{"id": slug(f), "label": nice(Path(f).stem).title(), "src": R + f} for f in coll("voice-zero/")])

# Expresso: label "<speaker> – <style>", speaker = channel's actor.
ex = []
for f in coll("expresso/"):
    m = re.match(r"expresso/(ex\d\d)-(ex\d\d)_([a-z-]+)_(\d+)_channel(\d)", f)
    if not m:
        continue
    spk = m.group(1) if m.group(5) == "1" else m.group(2)
    ex.append({"id": slug(f), "label": f"{spk.upper()} – {m.group(3).replace('-', ' / ')} {int(m.group(4))}", "src": R + f, "_k": (spk, m.group(3), f)})
ex.sort(key=lambda v: v.pop("_k"))
group("Moods (Expresso)", "Four actors acting moods. Non-commercial use only.", ex)

# EARS: two actors with many emotions, and 107 speakers' free speech.
for spk in ("p003", "p031"):
    group(f"Emotions – EARS {spk}", "One speaker, many emotions and styles (CC BY-NC 4.0).",
          [{"id": slug(f), "label": nice(Path(f).stem).replace("Emo ", "").replace(" freeform", "").replace(" sentences", " (read)").capitalize(), "src": R + f} for f in coll(f"ears/{spk}/")])
by = defaultdict(list)
for f in coll("ears/"):
    by[f.split("/")[1]].append(f)
group("Speakers – EARS", "107 speakers, free speech (CC BY-NC 4.0).",
      [{"id": "spk-" + slug(sorted(fs)[0]), "label": f"EARS {s}", "src": R + sorted(fs)[0]} for s, fs in sorted(by.items())])
group("Accents – VCTK", "British Isles and other English accents (CC BY 4.0).",
      [{"id": slug(f), "label": "VCTK " + Path(f).stem.split("_")[0], "src": R + f} for f in coll("vctk/")])
group("Community donations", "Volunteers' voices from Kyutai's donation project (CC0).",
      [{"id": slug(f), "label": nice(Path(f).stem), "src": R + f} for f in coll("voice-donations/")])
group("French (CML-TTS)", "French speakers from CML-TTS (CC BY 4.0), best with a French model.",
      [{"id": slug(f), "label": "French " + Path(f).stem.split("_")[0], "src": R + f} for f in coll("cml-tts/")])

# ---- tags: tab, gender/age/accent where the dataset says, pitch for everyone, mood ----
META = Path(__file__).with_name("meta")
pitch = json.loads((META / "pitch.json").read_text()) if (META / "pitch.json").exists() else {}
vctk = {}
for line in (META / "vctk-speaker-info.txt").read_text().splitlines()[1:]:
    f = line.split()
    if len(f) >= 4:
        vctk[f[0]] = {"age": f[1], "gender": f[2], "accent": f[3], "region": " ".join(f[4:])}
# EARS speaker statistics: no clear licence for redistribution, so downloaded when the catalog is rebuilt (not in git).
EARS_URL = "https://raw.githubusercontent.com/facebookresearch/ears_dataset/main/speaker_statistics.json"
ears_file = META / "ears-speaker-statistics.json"
if not ears_file.exists():
    import urllib.request
    ears_file.write_bytes(urllib.request.urlopen(EARS_URL, timeout=60).read())
ears = json.loads(ears_file.read_text())
BUILTIN_GENDER = {**{n: "F" for n in "alba anna azelma caro_davy cosette eponine eve fantine jane mary vera lola estelle".split()},
                  **{n: "M" for n in "bill_boerst charles george javert jean marius michael paul peter_yearsley stuart_bell giovanni juergen rafael daan".split()}}
TAB = {"Built-in (fast)": "builtin", "Accents – VCTK": "accents", "Community donations": "community", "French (CML-TTS)": "french",
       "Speakers – EARS": "speakers", "Alba – characters": "speakers", "Kyutai demo voices": "speakers", "Voice-zero": "speakers"}
for g in groups:
    tab = TAB.get(g["group"], "moods")
    for v in g["voices"]:
        v["tab"] = tab
        src = v["src"] if "://" in v["src"] else _ORIGINS_OF_PREDEFINED_VOICES[v["src"]].split("@")[0]
        hz = pitch.get(src)
        if hz:
            v["hz"] = round(hz)
            v["pitch"] = "low" if hz < 135 else "high" if hz >= 170 else "mid"
        tags = []
        m = re.search(r"vctk/(p\d+|s\d+)_", src)
        if m and m.group(1) in vctk:
            i = vctk[m.group(1)]
            v["gender"] = i["gender"]
            i["accent"] = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", i["accent"])
            i["region"] = re.sub(r"\s*\(.*?\)", "", i["region"]).strip()
            if tab != "builtin":
                v["label"] = f"{i['accent']} – {i['region'] or m.group(1)}" + (f" ({m.group(1)})" if i["region"] else "")
            tags += [i["accent"]] + ([i["region"]] if i["region"] else []) + [f"age {i['age']}"]
        m = re.search(r"ears/(p\d+)/", src)
        if m and m.group(1) in ears:
            i = ears[m.group(1)]
            v["gender"] = {"male": "M", "female": "F"}.get(i.get("gender"))
            if tab == "speakers":
                v["label"] = f"EARS {m.group(1)} – native {i.get('native language', '?')}"
            tags += [f"native {i.get('native language', '?')}", f"age {i.get('age', '?')}"]
        if v["src"] in BUILTIN_GENDER:
            v["gender"] = BUILTIN_GENDER[v["src"]]
        if "alba-mackenna" in src:
            v["gender"] = "F"
        m = re.search(r"expresso/ex\d\d-ex\d\d_([a-z-]+)_", src)
        if m:
            tags.append(m.group(1).replace("-", " / "))
        if tab == "moods" and "ears/" in src:
            tags.append(v["label"].lower())
        if g["group"].startswith("Picks"):
            tags.append(v["label"].lower())
        v["tags"] = [t for t in dict.fromkeys(tags) if t]
        if not v.get("gender"):
            v.pop("gender", None)

ids = [v["id"] for g in groups for v in g["voices"]]
assert len(ids) == len(set(ids)), "duplicate ids"
Path(__file__).with_name("voices.json").write_text(json.dumps(groups, indent=0, ensure_ascii=False))
print(len(ids), "voices in", len(groups), "groups")
