from __future__ import annotations

import sys
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class GenerationPaths:
    project_root: Path
    engine_root: Path
    model_dir: Path
    cfg_path: Path
    character_path: Path

    @property
    def legacy_root(self) -> Path:
        """向後相容屬性。

        原 LegacyGenerationPaths 有 legacy_root 欄位（指向 legacy_tts/）；
        遷移後對應的路徑為 engine_root（echo_tts/）。
        存取此屬性會觸發 DeprecationWarning。
        """
        warnings.warn(
            "GenerationPaths.legacy_root 已廢棄，請改用 engine_root。",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.engine_root


def default_generation_paths(project_root: str | Path | None = None) -> GenerationPaths:
    root = Path(project_root or Path.cwd()).resolve()
    engine_root = root / "echo_tts"
    model_dir = engine_root / "modules" / "tts_module" / "checkpoints"
    return GenerationPaths(
        project_root=root,
        engine_root=engine_root,
        model_dir=model_dir,
        cfg_path=model_dir / "config.yaml",
        character_path=engine_root / "models" / "tts" / "uep.pt",
    )


def validate_generation_paths(paths: GenerationPaths) -> None:
    missing = [
        path for path in [paths.engine_root, paths.model_dir, paths.cfg_path, paths.character_path]
        if not path.exists()
    ]
    if missing:
        formatted = "\n".join(str(path) for path in missing)
        raise FileNotFoundError(f"Missing IndexTTS2 TTS assets:\n{formatted}")


def generate_with_indextts2(
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

    engine_root = str(paths.engine_root)
    if engine_root not in sys.path:
        sys.path.insert(0, engine_root)

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
