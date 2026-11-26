#!/bin/bash
# Downloads the original background recordings (Wikimedia Commons, PD/CC0/CC BY) listed in noise/credits.json
# into noise/src/, then run ./build.sh to make the loops. Skips files that are already there.
# Usage: noise/fetch.sh            (SRC=/other/dir noise/fetch.sh to download elsewhere)
set -e; cd "$(dirname "$0")"
SRC=${SRC:-src}; mkdir -p "$SRC"
UA="pocket-tts-web-noise-fetch/1.0"
python3 - "$SRC" "$UA" <<'PY'
import json, sys, time, urllib.parse, urllib.request
src, ua = sys.argv[1], sys.argv[2]
def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": ua}), timeout=120)
for n in json.load(open("credits.json")):
    page = n.get("source")
    if not page:
        continue                                    # generated (room tone), nothing to download
    title = urllib.parse.unquote(page.rsplit("/wiki/", 1)[1])          # File:Name.ogg
    name = title.split(":", 1)[1].replace("_", " ")
    dest = f"{src}/{name}"
    try:
        open(dest, "rb").close(); print(f"have  {name}"); continue
    except FileNotFoundError:
        pass
    q = urllib.parse.urlencode({"action": "query", "format": "json", "prop": "imageinfo", "iiprop": "url", "titles": title})
    for attempt in range(4):
        try:
            info = json.load(get("https://commons.wikimedia.org/w/api.php?" + q))
            url = next(iter(info["query"]["pages"].values()))["imageinfo"][0]["url"]
            with get(url) as r, open(dest + ".part", "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
            import os; os.replace(dest + ".part", dest)
            print(f"got   {name}"); break
        except Exception as e:
            print(f"retry {name}: {e}"); time.sleep(5 * (attempt + 1))
    else:
        sys.exit(f"failed {name}")
    time.sleep(2)                                   # be polite to Commons
print("done: now run ./build.sh")
PY
