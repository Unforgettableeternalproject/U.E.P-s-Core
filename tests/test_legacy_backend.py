from pathlib import Path

from test_tts.runtime.legacy_backend import LegacyIndexTTS2Backend
from test_tts.runtime.legacy_generate import default_generation_paths


class FakeEngine:
    def __init__(self):
        self.loaded_character = None
        self.synthesize_calls = []

    def load_character(self, character_path):
        self.loaded_character = Path(character_path)
        return True

    def synthesize(self, text, output_path, emotion_vector=None):
        self.synthesize_calls.append((text, Path(output_path), emotion_vector))
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"fake wav")
        return True


def test_legacy_backend_loads_engine_once_and_reuses_it(tmp_path):
    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        return FakeEngine()

    paths = default_generation_paths(Path.cwd())
    backend = LegacyIndexTTS2Backend(paths=paths, engine_factory=factory)

    backend.load()
    first = backend.generate("hello", tmp_path / "one.wav")
    second = backend.generate("world", tmp_path / "two.wav")

    assert len(calls) == 1
    assert first.exists()
    assert second.exists()
    assert backend.loaded is True
    assert backend.engine is not None
    assert len(backend.engine.synthesize_calls) == 2
    assert backend.engine.loaded_character == paths.character_path


def test_legacy_backend_generate_auto_loads(tmp_path):
    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        return FakeEngine()

    backend = LegacyIndexTTS2Backend(paths=default_generation_paths(Path.cwd()), engine_factory=factory)

    output = backend.generate("hello", tmp_path / "auto.wav")

    assert output.exists()
    assert len(calls) == 1
    assert backend.loaded is True
