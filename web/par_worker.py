"""Worker process for parallel conversation lines (see app.py: parallel_pool / chunk_stream).

Kept tiny on purpose: spawned workers import this module, not app.py. Each worker keeps its own copy of the
models it has been asked for (LRU, small) and generates one whole line per call, single-threaded.
"""
from collections import OrderedDict

import numpy as np
import torch

torch.set_num_threads(1)
_models: "OrderedDict[str, object]" = OrderedDict()
KEEP = 2


def _model(mid: str):
    from pocket_tts.models.tts_model import TTSModel
    if mid in _models:
        _models.move_to_end(mid)
        return _models[mid]
    m = TTSModel.load_model(language=mid)
    m.default_temp = m.temp
    _models[mid] = m
    while len(_models) > KEEP:
        _models.popitem(last=False)
    return m


def warm(mid: str) -> None:
    _model(mid)


def say(mid: str, state, text: str, temp: float | None) -> np.ndarray:
    m = _model(mid)
    m.temp = temp if temp is not None else m.default_temp
    audio = m.generate_audio(state, text)
    return audio.detach().float().cpu().numpy().reshape(-1)

