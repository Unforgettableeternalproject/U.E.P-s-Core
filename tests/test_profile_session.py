from pathlib import Path

from test_tts.benchmarks.metrics import ProfileReport, SynthesisProfile, InitProfile
from test_tts.runtime.session import TTSInteractiveSession, ProfileResult


class FakeBackendWithProfile:
    def __init__(self):
        self.load_calls = 0
        self.generate_calls = []
        self.profile_calls = []
        self.loaded = False

    def load(self):
        self.load_calls += 1
        self.loaded = True

    def generate(self, text, output_path, on_segment_audio=None):
        if not self.loaded:
            raise RuntimeError("not loaded")
        self.generate_calls.append((text, output_path))
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"fake wav")
        return Path(output_path)

    def profile_generate(self, text, output_path):
        if not self.loaded:
            raise RuntimeError("not loaded")
        self.profile_calls.append((text, output_path))
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"fake wav")
        report = ProfileReport(
            init=InitProfile(gpt_ms=1000),
            synthesis=SynthesisProfile(
                text_chars=len(text),
                gpt_semantic_ms=100,
                total_wall_ms=300,
                audio_duration_ms=1500,
            ),
        )
        return Path(output_path), report

    def unload(self):
        self.loaded = False


class FakeBackendWithoutProfile:
    """Backend that does not support profile_generate."""

    def __init__(self):
        self.loaded = False

    def load(self):
        self.loaded = True

    def generate(self, text, output_path, on_segment_audio=None):
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"fake wav")
        return Path(output_path)

    def unload(self):
        self.loaded = False


class FakePlayer:
    def __init__(self):
        self.play_calls = []

    def play(self, audio_path):
        self.play_calls.append(audio_path)


def test_session_profile_returns_profile_result(tmp_path):
    backend = FakeBackendWithProfile()
    session = TTSInteractiveSession(backend=backend, output_dir=tmp_path)

    result = session.profile("hello profiler")

    assert isinstance(result, ProfileResult)
    assert result.text == "hello profiler"
    assert result.report.synthesis.gpt_semantic_ms == 100
    assert result.report.init.gpt_ms == 1000
    assert backend.load_calls == 1
    # Temporary file should be cleaned up
    assert not result.output_path.exists()


def test_session_profile_auto_loads_backend(tmp_path):
    backend = FakeBackendWithProfile()
    session = TTSInteractiveSession(backend=backend, output_dir=tmp_path)

    session.profile("test")

    assert backend.loaded is True
    assert backend.load_calls == 1


def test_session_profile_fallback_when_no_profile_generate(tmp_path):
    backend = FakeBackendWithoutProfile()
    session = TTSInteractiveSession(backend=backend, output_dir=tmp_path)

    result = session.profile("fallback test")

    assert isinstance(result, ProfileResult)
    # Should still work, just with empty profiling data
    assert result.report.synthesis.total_wall_ms == 0.0


def test_session_say_still_works_alongside_profile(tmp_path):
    backend = FakeBackendWithProfile()
    player = FakePlayer()
    session = TTSInteractiveSession(backend=backend, output_dir=tmp_path, player=player)

    say_result = session.say("hello")
    profile_result = session.profile("world")

    assert not say_result.output_path.exists()  # say cleans up
    assert not profile_result.output_path.exists()  # profile also cleans up
    assert len(player.play_calls) == 1  # only say triggers playback
