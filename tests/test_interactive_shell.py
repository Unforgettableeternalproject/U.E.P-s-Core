from pathlib import Path

from test_tts.cli.interactive import InteractiveShell


class FakeSession:
    def __init__(self):
        self.load_calls = 0
        self.say_calls = []
        self.save_calls = []
        self.unload_calls = 0

    def load(self):
        self.load_calls += 1

    def say(self, text):
        self.say_calls.append(text)
        class Result:
            pass
        result = Result()
        result.output_path = Path("played.wav")
        return result

    def save(self, text, output_path):
        self.save_calls.append((text, output_path))
        class Result:
            pass
        result = Result()
        result.output_path = Path(output_path)
        return result

    def unload(self):
        self.unload_calls += 1

    def status(self):
        return {"loaded": True, "say_count": len(self.say_calls), "save_count": len(self.save_calls)}


def test_shell_dispatches_load_say_save_status_and_unload():
    session = FakeSession()
    messages = []
    shell = InteractiveShell(session=session, write=messages.append)

    assert shell.execute("load") is True
    assert shell.execute('say "hello world"') is True
    assert shell.execute('save --output out.wav "keep this"') is True
    assert shell.execute("status") is True
    assert shell.execute("unload") is True

    assert session.load_calls == 1
    assert session.say_calls == ["hello world"]
    assert session.save_calls == [("keep this", "out.wav")]
    assert session.unload_calls == 1
    assert any("loaded" in message for message in messages)


def test_shell_exit_returns_false():
    shell = InteractiveShell(session=FakeSession(), write=lambda message: None)

    assert shell.execute("exit") is False
