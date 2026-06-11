from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from test_tts.runtime.audio import AudioPlayer, NullAudioPlayer


class PersistentTTSBackend(Protocol):
    def load(self) -> None:
        ...

    def generate(self, text: str, output_path: Path) -> Path:
        ...

    def unload(self) -> None:
        ...


@dataclass(frozen=True)
class SayResult:
    text: str
    output_path: Path


class TTSInteractiveSession:
    """Keeps a TTS backend loaded across multiple CLI commands."""

    def __init__(self, backend: PersistentTTSBackend, output_dir: str | Path, player: AudioPlayer | None = None):
        self.backend = backend
        self.output_dir = Path(output_dir)
        self.player = player or NullAudioPlayer()
        self._loaded = False
        self._say_count = 0
        self._save_count = 0
        self._play_count = 0
        self._last_output_path: Path | None = None

    def load(self) -> None:
        if self._loaded:
            return
        self.backend.load()
        self._loaded = True

    def say(self, text: str) -> SayResult:
        generated = self._generate(text, self._resolve_auto_output_path(prefix="say"))
        self.player.play(generated)
        self._say_count += 1
        self._play_count += 1
        self._last_output_path = generated
        self._cleanup_temporary_output(generated)
        return SayResult(text=text, output_path=generated)

    def save(self, text: str, output_path: str | Path) -> SayResult:
        generated = self._generate(text, self._resolve_output_path(output_path))
        self._save_count += 1
        self._last_output_path = generated
        return SayResult(text=text, output_path=generated)

    def unload(self) -> None:
        if not self._loaded:
            return
        self.backend.unload()
        self._loaded = False

    def status(self) -> dict[str, object]:
        return {
            "loaded": self._loaded,
            "say_count": self._say_count,
            "save_count": self._save_count,
            "play_count": self._play_count,
            "last_output_path": str(self._last_output_path) if self._last_output_path else None,
            "output_dir": str(self.output_dir),
        }

    def _generate(self, text: str, output_path: Path) -> Path:
        if not text.strip():
            raise ValueError("text must not be empty")

        self.load()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        return self.backend.generate(text, output_path)

    def _resolve_output_path(self, output_path: str | Path) -> Path:
        path = Path(output_path)
        if not path.is_absolute():
            path = self.output_dir / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _resolve_auto_output_path(self, prefix: str) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        index = self._say_count + self._save_count + 1
        return self.output_dir / f"{prefix}-{timestamp}-{index:03d}.wav"

    @staticmethod
    def _cleanup_temporary_output(output_path: Path) -> None:
        if output_path.exists():
            output_path.unlink()
