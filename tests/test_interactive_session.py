from pathlib import Path

from echo_tts.runtime.session import TTSInteractiveSession


class FakeBackend:
    def __init__(self):
        self.load_calls = 0
        self.generate_calls = []
        self.loaded = False

    def load(self):
        self.load_calls += 1
        self.loaded = True

    def generate(self, text: str, output_path: Path, on_segment_audio=None, **kwargs) -> Path:
        if not self.loaded:
            raise RuntimeError("backend must be loaded before generate")
        self.generate_calls.append((text, output_path))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake wav")
        return output_path

    def unload(self):
        self.loaded = False


class FakePlayer:
    def __init__(self):
        self.play_calls = []
        self.tensor_play_calls = []

    def play_tensor(self, audio_tensor, sample_rate=22050):
        self.tensor_play_calls.append(audio_tensor)

    def play(self, audio_path: Path):
        self.play_calls.append(audio_path)


def test_session_reuses_loaded_backend_for_multiple_say_calls(tmp_path):
    backend = FakeBackend()
    player = FakePlayer()
    session = TTSInteractiveSession(backend=backend, output_dir=tmp_path, player=player)

    first = session.say("hello")
    second = session.say("world")

    assert backend.load_calls == 1
    assert first.output_path.parent == tmp_path
    assert second.output_path.parent == tmp_path
    assert [call[0] for call in backend.generate_calls] == ["hello", "world"]
    assert player.play_calls == [first.output_path, second.output_path]
    assert not first.output_path.exists()
    assert not second.output_path.exists()
    assert session.status()["loaded"] is True
    assert session.status()["play_count"] == 2


def test_session_save_generates_file_without_playing(tmp_path):
    backend = FakeBackend()
    player = FakePlayer()
    session = TTSInteractiveSession(backend=backend, output_dir=tmp_path, player=player)

    result = session.save("hello", "saved.wav")

    assert result.output_path == tmp_path / "saved.wav"
    assert result.output_path.exists()
    assert player.play_calls == []
    assert session.status()["save_count"] == 1


class StreamingFakeBackend:
    """Backend that invokes on_segment_audio callback to simulate multi-segment streaming."""

    def __init__(self):
        self.load_calls = 0
        self.loaded = False

    def load(self):
        self.load_calls += 1
        self.loaded = True

    def generate(self, text, output_path, on_segment_audio=None, **kwargs):
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake wav")
        # Simulate 2 segments
        if on_segment_audio is not None:
            on_segment_audio("fake_tensor_seg_0", 0, 2)
            on_segment_audio("fake_tensor_seg_1", 1, 2)
        return output_path

    def unload(self):
        self.loaded = False


def test_say_streams_per_segment_when_callback_supported(tmp_path):
    backend = StreamingFakeBackend()
    player = FakePlayer()
    session = TTSInteractiveSession(backend=backend, output_dir=tmp_path, player=player)

    session.say("hello streaming")

    # play_tensor should have been called for each segment
    assert len(player.tensor_play_calls) == 2
    assert player.tensor_play_calls == ["fake_tensor_seg_0", "fake_tensor_seg_1"]
    # play(file) should NOT have been called (streaming handled it)
    assert player.play_calls == []


def test_say_falls_back_to_file_play_when_no_segments_streamed(tmp_path):
    """When backend doesn't invoke callback, say falls back to playing the file."""
    backend = FakeBackend()  # FakeBackend.generate doesn't invoke on_segment_audio
    player = FakePlayer()
    session = TTSInteractiveSession(backend=backend, output_dir=tmp_path, player=player)

    session.say("hello fallback")

    # No tensor plays, file play should have happened
    assert len(player.tensor_play_calls) == 0
    assert len(player.play_calls) == 1


def test_session_unload_resets_loaded_state(tmp_path):
    backend = FakeBackend()
    player = FakePlayer()
    session = TTSInteractiveSession(backend=backend, output_dir=tmp_path, player=player)

    session.say("hello")
    session.unload()

    assert session.status()["loaded"] is False
    assert backend.loaded is False
