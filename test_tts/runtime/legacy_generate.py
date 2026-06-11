from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class LegacyGenerationPaths:
    project_root: Path
    legacy_root: Path
    model_dir: Path
    cfg_path: Path
    character_path: Path


def default_generation_paths(project_root: str | Path | None = None) -> LegacyGenerationPaths:
    root = Path(project_root or Path.cwd()).resolve()
    legacy_root = root / "legacy_tts"
    model_dir = legacy_root / "modules" / "tts_module" / "checkpoints"
    return LegacyGenerationPaths(
        project_root=root,
        legacy_root=legacy_root,
        model_dir=model_dir,
        cfg_path=model_dir / "config.yaml",
        character_path=legacy_root / "models" / "tts" / "uep.pt",
    )


def validate_generation_paths(paths: LegacyGenerationPaths) -> None:
    missing = [
        path for path in [paths.legacy_root, paths.model_dir, paths.cfg_path, paths.character_path]
        if not path.exists()
    ]
    if missing:
        formatted = "\n".join(str(path) for path in missing)
        raise FileNotFoundError(f"Missing legacy TTS assets:\n{formatted}")


def generate_with_legacy_indextts2(
    text: str,
    output_path: str | Path,
    project_root: str | Path | None = None,
    device: str | None = "cuda",
    use_fp16: bool = True,
    use_cuda_kernel: bool = True,
    emotion_vector: Sequence[float] | None = None,
) -> Path:
    if not text.strip():
        raise ValueError("text must not be empty")

    paths = default_generation_paths(project_root)
    validate_generation_paths(paths)

    output = Path(output_path)
    if not output.is_absolute():
        output = paths.project_root / output
    output.parent.mkdir(parents=True, exist_ok=True)

    legacy_path = str(paths.legacy_root)
    if legacy_path not in sys.path:
        sys.path.insert(0, legacy_path)

    from modules.tts_module.lite_engine import IndexTTSLite

    engine = IndexTTSLite(
        cfg_path=str(paths.cfg_path),
        model_dir=str(paths.model_dir),
        use_fp16=use_fp16,
        device=device,
        use_cuda_kernel=use_cuda_kernel,
    )
    engine.load_character(paths.character_path)
    ok = engine.synthesize(text=text, output_path=str(output), emotion_vector=list(emotion_vector) if emotion_vector else None)
    if not ok or not output.exists():
        raise RuntimeError("IndexTTS2 generation did not produce an output file")
    return output
