from pathlib import Path

from test_tts.benchmarks.metrics import InitProfile, ProfileReport
from test_tts.runtime.legacy_backend import LegacyIndexTTS2Backend
from test_tts.runtime.legacy_generate import default_generation_paths


class FakeEngine:
    def __init__(self):
        self.loaded_character = None
        self.synthesize_calls = []
        self._init_timings = {
            "gpt_ms": 1200.0,
            "semantic_codec_ms": 300.0,
            "s2mel_ms": 800.0,
            "bigvgan_ms": 400.0,
            "emo_matrix_ms": 50.0,
            "text_processor_ms": 100.0,
            "char_load_ms": 150.0,
            "warmup_ms": 3000.0,
        }

    def load_character(self, character_path):
        self.loaded_character = Path(character_path)
        return True

    def synthesize(self, text, output_path, emotion_vector=None):
        self.synthesize_calls.append((text, Path(output_path), emotion_vector))
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"fake wav")
        return True

    def synthesize_with_profile(self, text, output_path, emotion_vector=None):
        self.synthesize_calls.append((text, Path(output_path), emotion_vector))
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"fake wav")
        timings = {
            "text_chars": float(len(text)),
            "text_norm_ms": 5.0,
            "emotion_ms": 2.0,
            "gpt_semantic_ms": 120.0,
            "gpt_latent_ms": 60.0,
            "s2mel_cfm_ms": 250.0,
            "vocoder_ms": 80.0,
            "wav_save_ms": 10.0,
            "total_wall_ms": 530.0,
            "audio_duration_ms": 2500.0,
        }
        return True, timings


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


def test_legacy_backend_profile_generate_returns_report(tmp_path):
    def factory(**kwargs):
        return FakeEngine()

    paths = default_generation_paths(Path.cwd())
    backend = LegacyIndexTTS2Backend(paths=paths, engine_factory=factory)

    output, report = backend.profile_generate("hello profiler", tmp_path / "profiled.wav")

    assert output.exists()
    assert isinstance(report, ProfileReport)
    assert report.success is True

    # Check synthesis timings were populated from FakeEngine
    assert report.synthesis.gpt_semantic_ms == 120.0
    assert report.synthesis.vocoder_ms == 80.0
    assert report.synthesis.total_wall_ms == 530.0
    assert report.synthesis.audio_duration_ms == 2500.0
    assert report.synthesis.text_chars == len("hello profiler")

    # Check init timings were populated from FakeEngine._init_timings
    assert report.init.gpt_ms == 1200.0
    assert report.init.bigvgan_ms == 400.0
    assert report.init.warmup_ms == 3000.0
    assert report.init.total_cold_start_ms > 0
