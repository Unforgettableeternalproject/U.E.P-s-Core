from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from test_tts.runtime.legacy_generate import LegacyGenerationPaths, validate_generation_paths

EngineFactory = Callable[..., Any]


class LegacyIndexTTS2Backend:
    """Persistent backend wrapper around the copied IndexTTS2 Lite engine."""

    def __init__(
        self,
        paths: LegacyGenerationPaths,
        engine_factory: EngineFactory | None = None,
        device: str | None = "cuda",
        use_fp16: bool = True,
        use_cuda_kernel: bool = True,
        emotion_vector: Sequence[float] | None = None,
    ):
        self.paths = paths
        self.engine_factory = engine_factory
        self.device = device
        self.use_fp16 = use_fp16
        self.use_cuda_kernel = use_cuda_kernel
        self.emotion_vector = list(emotion_vector) if emotion_vector else None
        self.engine: Any | None = None
        self.loaded = False

    def load(self) -> None:
        if self.loaded:
            return

        validate_generation_paths(self.paths)
        legacy_path = str(self.paths.legacy_root)
        if legacy_path not in sys.path:
            sys.path.insert(0, legacy_path)

        factory = self.engine_factory or self._default_engine_factory()
        self.engine = factory(
            cfg_path=str(self.paths.cfg_path),
            model_dir=str(self.paths.model_dir),
            use_fp16=self.use_fp16,
            device=self.device,
            use_cuda_kernel=self.use_cuda_kernel,
        )
        self.engine.load_character(self.paths.character_path)
        self.loaded = True

    def generate(self, text: str, output_path: str | Path) -> Path:
        if not text.strip():
            raise ValueError("text must not be empty")

        self.load()
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        ok = self.engine.synthesize(
            text=text,
            output_path=str(output),
            emotion_vector=self.emotion_vector,
        )
        if not ok or not output.exists():
            raise RuntimeError("IndexTTS2 generation did not produce an output file")
        return output

    def unload(self) -> None:
        self.engine = None
        self.loaded = False

    @staticmethod
    def _default_engine_factory() -> EngineFactory:
        from modules.tts_module.lite_engine import IndexTTSLite

        return IndexTTSLite
