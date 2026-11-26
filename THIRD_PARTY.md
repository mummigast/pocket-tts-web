# Third-party software, models and audio

This repository contains only code, a voice catalog (file paths, names, tags) and credits. **No audio and no model
weights are included**; they are downloaded from their original sources when you set up or use the app, under their
own licences listed here. If you publish audio made with this app, check the licence of the voice you used.

## Software (installed by `setup.sh`, not included)

| Component | Licence | Source |
|---|---|---|
| Pocket TTS (`pocket-tts`) | MIT | https://github.com/kyutai-labs/pocket-tts |
| PyTorch | BSD-3-Clause | https://pytorch.org |
| FastAPI, Uvicorn | MIT / BSD-3-Clause | https://fastapi.tiangolo.com |
| huggingface_hub | Apache-2.0 | https://github.com/huggingface/huggingface_hub |
| soundfile / libsndfile | BSD-3-Clause / LGPL-2.1 | https://github.com/bastibe/python-soundfile |
| yt-dlp | Unlicense | https://github.com/yt-dlp/yt-dlp |
| FFmpeg (system package) | LGPL-2.1+ / GPL depending on build | https://ffmpeg.org |

## Models

| Model | Licence | Notes |
|---|---|---|
| `kyutai/pocket-tts` (with voice cloning) | CC BY 4.0 + Kyutai's prohibited-use terms | Gated: accept the terms on Hugging Face. Prohibits, among other things, **voice impersonation or cloning without explicit and lawful consent**, and presenting generated audio as genuine recordings of real people. |
| `kyutai/pocket-tts-without-voice-cloning` | CC BY 4.0 | Tokenizers and built-in voice embeddings. |

Credit: Kyutai, https://kyutai.org

## Voice library (`kyutai/tts-voices`, listed in `web/voices.json`)

Licences as stated in https://huggingface.co/kyutai/tts-voices

| Set (tab in the page) | Licence | Credit |
|---|---|---|
| `voice-donations/` (Community) | CC0 | Kyutai Unmute Voice Donation Project volunteers |
| `vctk/` (Accents) | CC BY 4.0 | CSTR VCTK Corpus, University of Edinburgh, https://datashare.ed.ac.uk/handle/10283/3443 |
| `expresso/` (Moods, Picks) | **CC BY-NC 4.0, non-commercial only** | Expresso dataset, Meta AI, https://speechbot.github.io/expresso/ |
| `ears/` (Emotions, Speakers) | **CC BY-NC 4.0, non-commercial only** | EARS dataset, https://sp-uhh.github.io/ears_dataset/ |
| `cml-tts/fr/` (French) | CC BY 4.0 | CML-TTS dataset, https://openslr.org/146/ |
| `alba-mackenna/` | CC BY 4.0 | Voice actor Alba MacKenna |
| `voice-zero/` | CC0 | LibriVox readers via Voice-Zero, https://github.com/OwenTyme/voice-zero |
| `unmute-prod-website/` | CC0 (Kyutai's own), except: `degaulle-2` (1940 Appeal of 18 June, assumed public domain by Kyutai), `ex04_narration_longform` (Expresso, CC BY-NC 4.0), `p329_022` (VCTK, CC BY 4.0) | Kyutai |
| Built-in voices (`alba`, `anna`, …) | Licence of the clip each was made from (VCTK, EARS, Expresso, donations, Voice-Zero, Alba MacKenna, Common Voice CC0) | Kyutai |

### Voice tags (`web/meta/`, `web/voices.json`)

- `web/meta/vctk-speaker-info.txt`: speaker list from the VCTK Corpus (CC BY 4.0, CSTR, University of Edinburgh).
- Gender / age / native-language tags for EARS voices come from the EARS speaker statistics
  (https://github.com/facebookresearch/ears_dataset), downloaded when the catalog is rebuilt; the file itself is
  not redistributed here.
- `web/meta/pitch.json`: our own measurement of each clip's median pitch.

## Background sounds (`noise/`)

Downloaded from Wikimedia Commons by `noise/fetch.sh`; titles, authors and licences are in `noise/credits.json`
and are shown in the page under the background picker.

| Sound | Licence | Author |
|---|---|---|
| Street (Prague) | Public domain | Juan de Vojníkov |
| Rainy street | CC0 | Extemporalist |
| Café / restaurant | Public domain | stephan |
| Shopping mall | Public domain | thore |
| Office / classroom | Public domain | rcrossley |
| Train station | CC BY 4.0 | Work With Sounds / Werstas |
| Subway ride | CC BY 4.0 | PascalHD |
| Park, birds and breeze | Public domain | ezwa |
| Room tone | generated (pink noise) | |

## YouTube

The YouTube feature downloads audio with yt-dlp. Whether that is allowed depends on YouTube's terms and the rights
of the uploader and the speaker; that is the responsibility of whoever runs the app.
