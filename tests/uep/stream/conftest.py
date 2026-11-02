"""測試共用元件。"""

from __future__ import annotations

import math
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from uep.stream.contracts.types import Sentence


@pytest.fixture(autouse=True)
def _isolate_emotion_presets():
    """StreamService.prepare() 會把 echo_tts 的 preset 表註冊進全域
    EMOTION_PRESETS（TTS repo 在同層時就讀得到）；每個測試結束都退回內建表，
    避免測試結果依賴執行順序。"""
    yield
    from uep.stream import emotion

    emotion.reset_presets()


class FakeTTSBackend:
    """模擬 ``IndexTTS2Backend`` 的同步 push callback 行為。

    存在的理由：Phase 1 的多數 bug 會出在 push→pull 橋接，不是模型。
    橋接邏輯必須能在沒有 GPU、沒有 echo_tts 的環境下秒級迭代。
    """

    def __init__(
        self,
        segments_per_sentence: int = 2,
        segment_seconds: float = 0.5,
        synth_delay_s: float = 0.0,
        sample_rate: int = 22050,
    ) -> None:
        self.segments_per_sentence = segments_per_sentence
        self.segment_seconds = segment_seconds
        self.synth_delay_s = synth_delay_s
        self.sample_rate = sample_rate
        self.calls: list[str] = []
        self.loaded = False
        self.completed: list[str] = []
        """完整跑完 generate 的句子。被取消時不會進來——
        用來驗證取消確實中止了上游，而不是只丟棄了結果。"""

        self.emotion_vector: list[float] | None = None
        self.speed_offset: float | None = None

    def load(self) -> None:
        self.loaded = True

    def set_speed(self, value: float) -> float:
        self.speed_offset = value
        return value

    def unload(self) -> None:
        self.loaded = False

    def generate(
        self,
        text,
        output_path,
        on_segment_audio=None,
        language=None,
        emotion_overrides=None,
    ):
        import numpy as np

        self.calls.append(text)
        total = self.segments_per_sentence
        samples = int(self.sample_rate * self.segment_seconds)
        for idx in range(total):
            if self.synth_delay_s:
                time.sleep(self.synth_delay_s)
            wave = (
                np.sin(2 * math.pi * 220.0 * np.arange(samples) / self.sample_rate).astype(
                    "float32"
                )
                * 0.2
            )
            if on_segment_audio is not None:
                on_segment_audio(wave, idx, total)
        self.completed.append(text)
        return output_path


@pytest.fixture
def fake_tts_backend():
    """預設設定的假 TTS backend。需要自訂參數時直接用 :class:`FakeTTSBackend`。"""
    return FakeTTSBackend()


async def sentence_stream(
    texts: list[str], turn_id: str = "t1", style=None
) -> AsyncIterator[Sentence]:
    """把字串列表變成 Sentence 流。"""
    for i, text in enumerate(texts):
        yield Sentence(
            text=text,
            turn_id=turn_id,
            index=i,
            is_first=i == 0,
            is_last=i == len(texts) - 1,
            style=style,
        )


# ─── 守門：測試不得往 outputs/ 寫東西 ──────────────────────────────
# outputs/web_logs 是真對話的分析材料；pytest 每跑一次噴一批 session_*.jsonl
# 進去，艾斯維爾得手動清（2026-08-22）。任何新測試若建 StreamService 忘了傳
# log_dir=tmp_path，這裡會直接 fail 而不是默默污染。

_OUTPUTS = Path(__file__).resolve().parents[1] / "outputs"


def _outputs_snapshot() -> set[Path]:
    return set(_OUTPUTS.rglob("*")) if _OUTPUTS.exists() else set()


@pytest.fixture(autouse=True)
def _no_writes_to_outputs():
    before = _outputs_snapshot()
    yield
    leaked = sorted(p for p in _outputs_snapshot() - before if p.is_file())
    if leaked:
        for p in leaked:
            p.unlink(missing_ok=True)
        names = ", ".join(p.name for p in leaked[:5])
        pytest.fail(
            f"測試往 outputs/ 寫了 {len(leaked)} 個檔（已清掉）：{names} —— "
            "StreamService 要傳 log_dir=tmp_path"
        )
