from pathlib import Path

from test_tts.runtime.session import TTSInteractiveSession


class FakeBackend:
    def __init__(self):
        self.load_calls = 0
        self.generate_calls = []
        self.loaded = False

    def load(self):
        self.load_calls += 1
        self.loaded = True

    def generate(self, text: str, output_path: Path) -> Path:
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


def test_session_unload_resets_loaded_state(tmp_path):
    backend = FakeBackend()
    player = FakePlayer()
    session = TTSInteractiveSession(backend=backend, output_dir=tmp_path, player=player)

    session.say("hello")
    session.unload()

    assert session.status()["loaded"] is False
    assert backend.loaded is False
