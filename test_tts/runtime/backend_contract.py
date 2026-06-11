from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BackendInfo:
    name: str
    source_path: Path
    supports_emotion_vector: bool
    supports_stage_profiling: bool


def legacy_backend_info(project_root: str | Path | None = None) -> BackendInfo:
    root = Path(project_root or Path.cwd())
    return BackendInfo(
        name="legacy_indextts2",
        source_path=root / "legacy_tts" / "modules" / "tts_module",
        supports_emotion_vector=True,
        supports_stage_profiling=False,
    )
