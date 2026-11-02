"""N2 Console v0：以程式方式驅動指令層（A0 與 fake 可跑的情境）；需要 PostgreSQL（UEP_TEST_DATABASE_URL）。"""
from __future__ import annotations

import threading
import time
import wave

import pytest
from sqlalchemy import select

from uep.console import app
from uep.console.commands import Console
from uep.console.rig import ConsoleError, build_fake_runtime, build_runtime, connect_database
from uep.events.models import EventRow
from uep.output.models import DeliveryRow


class Capture:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text: str) -> None:
        self.lines.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def clear(self) -> None:
        self.lines.clear()


@pytest.fixture
def make_console(session_factory, tmp_path):
    made = []

    def _make(**over):
        rt = build_fake_runtime(session_factory, tmp_path / "e2e", **over)
        out = Capture()
        made.append(rt)
        return Console(rt, out=out, wait_s=10.0), rt, out

    yield _make
    for rt in made:
        rt.core.shutdown()


def _wav_seconds(path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


# ---- A0：文字 -> fake think -> fake speak -> wav ----

def test_a0_say_runs_whole_core_flow_and_lands_wav(make_console, session_factory):
    console, rt, out = make_console()
    assert console.execute("/say 你好") is True
    assert "turn.completed" in out.text and "回應：" in out.text
    turn_id = console.last_turn_id
    assert turn_id is not None
    wavs = rt.wavs[turn_id]
    assert len(wavs) == 1 and wavs[0].exists() and wavs[0].name == "1.wav" and wavs[0].parent.name == turn_id
    assert _wav_seconds(wavs[0]) > 0
    with session_factory() as s:
        stmt = select(EventRow.event_type).where(EventRow.turn_id == turn_id).order_by(EventRow.journal_seq)
        kinds = list(s.scalars(stmt))
        utterances = list(s.scalars(select(EventRow).where(EventRow.event_type == "input.utterance_final")))
        deliveries = list(s.scalars(select(DeliveryRow)))
    assert [k for k in kinds if k.startswith("turn.")] == ["turn.started", "turn.generation_completed", "turn.completed"]
    assert len(utterances) == 1 and len(deliveries) == 2  # 一入；fake 回覆兩句各一條 delivery
    assert rt.think.stream_calls == 1 and rt.speak.speak_calls == 1


def test_plain_line_is_say(make_console):
    console, rt, out = make_console()
    console.execute("今天好嗎")
    assert "turn.completed" in out.text and rt.think.stream_calls == 1


def test_say_without_text_shows_usage(make_console):
    console, rt, out = make_console()
    console.execute("/say")
    assert "用法" in out.text and rt.think.stream_calls == 0


def test_no_tts_discards_audio_but_text_flow_completes(make_console):
    console, rt, out = make_console(with_tts=False)
    console.execute("/say 你好")
    assert "turn.completed" in out.text and "回應：" in out.text
    assert rt.wavs == {} and "音訊：" not in out.text


# ---- /wav（fake STT）----

def test_wav_uses_fake_stt_to_produce_utterance(make_console, tmp_path):
    console, rt, out = make_console()
    src = tmp_path / "in.wav"
    with wave.open(str(src), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(bytes(32000))  # 1 秒靜音
    console.execute(f"/wav {src}")
    assert "fake STT" in out.text and "1.0 秒" in out.text and "turn.completed" in out.text


def test_wav_missing_file_reports_error_without_turn(make_console, tmp_path):
    console, rt, out = make_console()
    console.execute(f"/wav {tmp_path / 'nope.wav'}")
    assert "無法讀取 wav" in out.text and rt.think.stream_calls == 0


# ---- /status、/trace ----

def test_status_reports_axes_and_turn_state(make_console):
    console, rt, out = make_console()
    console.execute("/say 哈囉")
    out.clear()
    console.execute("/status")
    text = out.text
    assert "Presence：ACTIVE" in text and "Interaction：" in text and "Runtime：READY" in text
    assert "進行中 0" in text and "turn.completed" in text and "Host 已啟動：True" in text


def test_trace_shows_latest_turn_events(make_console):
    console, rt, out = make_console()
    console.execute("/trace")
    assert "尚無" in out.text
    console.execute("/say 哈囉")
    out.clear()
    console.execute("/trace")
    assert "延遲報告" in out.text and "TTFA" in out.text and "turn.started" in out.text
    out.clear()
    console.execute("/trace all")
    assert out.text


# ---- /cancel ----

def test_cancel_without_live_turn(make_console):
    console, rt, out = make_console()
    console.execute("/cancel")
    assert "沒有進行中" in out.text


def test_cancel_stops_inflight_turn(make_console):
    console, rt, out = make_console()
    rt.think.first_token_delay_s = 5.0  # 讓 think 卡住，才有進行中的 turn 可取消
    worker = threading.Thread(target=console.execute, args=("/say 講很久",))
    worker.start()
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            coordinator = rt.host.coordinator
            if coordinator is not None and coordinator.live_turns():
                break
            time.sleep(0.02)
        else:
            pytest.fail("等不到進行中的 turn")
        console.execute("/cancel")
    finally:
        worker.join(timeout=15)
    assert not worker.is_alive()
    assert "turn.cancelled" in out.text and "已認領" in out.text


# ---- 骨架與錯誤 ----

@pytest.mark.parametrize("name", ["tts", "check"])
def test_subsystem_namespaces_are_stubs(make_console, name):
    console, rt, out = make_console()
    console.execute(f"/{name}")
    assert "尚未支援" in out.text and rt.think.stream_calls == 0


def test_mem_mic_help_unknown_and_quit(make_console):
    console, rt, out = make_console()
    console.execute("/mem 貓")
    console.execute("/mic")
    console.execute("/help")
    console.execute("/nope")
    assert "記憶" in out.text and "尚未實作" in out.text and "/say" in out.text and "未知指令 /nope" in out.text
    assert console.execute("/quit") is False
    assert console.execute("") is True


# ---- 啟動：模式、資料庫、main ----

def test_real_mode_requires_llm_key(monkeypatch, session_factory, tmp_path):
    from uep.config import UepSettings

    monkeypatch.delenv("UEP_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("uep.config.get_settings", lambda: UepSettings(_env_file=None))
    with pytest.raises(ConsoleError, match="UEP_LLM_API_KEY"):
        build_runtime("real", session_factory, tmp_path)
    with pytest.raises(ConsoleError, match="UEP_LLM_API_KEY"):
        build_runtime("fake", session_factory, tmp_path, think="real")
    with pytest.raises(ConsoleError, match="未知的模式"):
        build_runtime("bogus", session_factory, tmp_path)


def test_connect_database_errors_are_clear_and_leak_no_password():
    with pytest.raises(ConsoleError) as exc:
        connect_database("postgresql://user:s3cret-pw@127.0.0.1:1/nodb")
    assert "無法連線 PostgreSQL" in str(exc.value) and "s3cret-pw" not in str(exc.value)


def test_connect_database_requires_configuration(monkeypatch):
    from uep.config import get_settings

    monkeypatch.setattr(get_settings(), "database_url", None)
    with pytest.raises(ConsoleError, match="UEP_DATABASE_URL"):
        connect_database()


def test_main_runs_scripted_session(monkeypatch, session_factory, tmp_path):
    monkeypatch.setattr(app, "connect_database", lambda: session_factory)
    lines = iter(["/say 你好", "/status", "/quit"])
    out = Capture()
    code = app.main(["--session-dir", str(tmp_path / "e2e")], input_fn=lambda _p: next(lines), out=out)
    assert code == 0
    assert "turn.completed" in out.text and "Presence：ACTIVE" in out.text


def test_main_real_mode_without_key_exits_with_message(monkeypatch, session_factory, tmp_path):
    from uep.config import UepSettings

    monkeypatch.delenv("UEP_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("uep.config.get_settings", lambda: UepSettings(_env_file=None))
    monkeypatch.setattr(app, "connect_database", lambda: session_factory)
    out = Capture()
    code = app.main(["--mode", "real", "--session-dir", str(tmp_path)], input_fn=lambda _p: "/quit", out=out)
    assert code == 2 and "UEP_LLM_API_KEY" in out.text and "sk-" not in out.text
