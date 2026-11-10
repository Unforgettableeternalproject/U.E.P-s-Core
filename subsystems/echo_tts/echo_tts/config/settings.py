from __future__ import annotations

from pathlib import Path

import yaml


DEFAULT_CONFIG = {
    "backend": "vox",
    "target_first_audio_ms": 1000,
    "chunking": {
        "first_chunk_chars": 40,
        "followup_chunk_chars": 100,
    },
    "profiling": {
        "enabled": True,
        "record_stage_timings": True,
    },
}


def load_config(path: str | Path | None = None) -> dict:
    if path is None:
        return DEFAULT_CONFIG.copy()

    with Path(path).open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}

    config = DEFAULT_CONFIG.copy()
    config.update(loaded)
    return config
