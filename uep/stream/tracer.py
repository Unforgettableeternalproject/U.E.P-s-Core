"""LatencyTracer——逐段打點。

## 為什麼這個元件跟三個 Stage 一樣重要

上次 Discord 慢的時候，我們手上只有「30-40 秒」這一個數字，**不知道該修哪裡**。
四段耗時相加，但不知道哪一段是主兇，只能猜。結果猜「先做 TTS 串流」——
而 TTS 只佔其中 5-15s，前面 25s 照樣要等。

沒有量測就沒有優化。這個檔案的存在就是為了不再重蹈覆轍。

## 關鍵指標是 TTFA，不是總處理時間

TTFA（Time To First Audio）= 使用者**說完話**到**聽見第一個字**的間隔。

人類對話的自然停頓約 200-500ms，超過 2s 明顯尷尬，超過 5s 對話就斷了。
總時長可以長（回答本來就可能很長），但 TTFA 必須短。

所以 t0 取 :attr:`Utterance.ended_at`（使用者說完的時刻），不是系統收到
轉錄的時刻——**STT 的轉錄耗時是使用者的等待，必須算進去**。

## 延遲預算（設計文件 §5，2026-08-20 二次修訂）

| 階段 | 預算 | 依據 |
|------|------|------|
| STT 最終轉錄 | 700ms | 增量串流 + LocalAgreement 的實際量級 |
| Memory retrieve | 300ms | 與 LLM prefill 平行，**不計入 TTFA** |
| LLM 首句 | 800ms | 串流後首 token ~200-400ms |
| TTS 首段 | 1800ms | **實測**：固定開銷約 1.8s，切短首句也跨不過 |
| **TTFA 合計** | **3500ms** | 暫訂值 |

兩次上修都是**被實測打臉**的結果，記在這裡免得日後又拿舊數字當目標：

* **STT 400ms → 700ms**：400ms 是雲端專用串流 ASR（AssemblyAI P50
  ~150ms、Deepgram Flux <300ms）的量級。Whisper 是 encoder-decoder
  attention 架構，即使 turbo + int8 也先天不利。
* **TTS 1000ms → 1800ms**：實測首句 16 字 / 9 字 / 1 字分別是
  3411 / 2845 / 1788ms，解出「每次合成約 1.8 秒固定開銷 + 每字 80ms」。
  **首句就算只有一個字也要 1.8 秒**，這是當前 IndexTTS2 實作的硬牆。

預算是**暫訂**的：導入 TensorRT 加速（Faster IndexTTS-2 論文的端到端
3.46-3.60×）後 TTS 段可望回到 ~500ms，屆時 TTFA 目標可以拉回 2.5s 以下。
在那之前拿 2.5s 當目標只會讓每份報告都顯示紅字，預算就失去意義了。

Memory 不計入總和——它與 LLM prefill 平行跑（§7.4），
列在表中只是為了看它有沒有慢到連第二句都趕不上。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .contracts.types import TurnPhase

# --- 標準打點名稱 ---

MARK_TURN_START = "turn_start"
"""使用者開始說話。"""

MARK_SPEECH_END = "speech_end"
"""使用者說完（VAD 判定）。**TTFA 的起算點。**"""

MARK_INPUT_FINAL = "input_final"
"""STT 最終轉錄就緒。"""

MARK_MEMORY_START = "memory_start"
MARK_MEMORY_DONE = "memory_done"
"""Memory retrieve 起訖。§7.4 定案與 LLM prefill 平行，
所以 memory_done 晚於 think_first_token 是**正常的**，不是異常。"""

MARK_TOOL_START = "tool_start"
MARK_TOOL_DONE = "tool_done"
"""工具呼叫（記憶 deep_recall / force_remember）的區間。多次往返只記第一次開始、
最後一次結束——報告要的是「這輪被工具吃掉多久」。"""
MARK_THINK_FIRST_TOKEN = "think_first_token"
"""LLM 吐出第一個 token。"""

MARK_THINK_FIRST_SENTENCE = "think_first_sentence"
"""SentenceSplitter 切出第一句。"""

MARK_THINK_DONE = "think_done"
MARK_QUEUE_ACQUIRED = "queue_acquired"
"""輪到這一輪開始生成（取得 think 鎖）的時刻。排隊時間 = 這個減 input_final。"""
MARK_FIRST_FRAME = "first_frame"
"""sink 的第一個真音訊 frame 交給 player 的時刻（取自 sink.first_frame_at）。"""
MARK_GAP_FIRST_AUDIO = "gap_first_audio"
"""墊片音（GapFiller）第一段寫進 sink 的時刻。感知 TTFA = min(這個, speak_first_chunk)。"""
MARK_SPEAK_FIRST_CHUNK = "speak_first_chunk"
"""第一段音訊就緒。**TTFA 的終點。**"""

MARK_SPEAK_LAST_CHUNK = "speak_last_chunk"
MARK_TURN_END = "turn_end"

# --- 預算（毫秒），對應設計文件 §5 ---

BUDGET_MS: dict[str, float] = {
    "stt": 700.0,
    "memory": 300.0,
    "llm_first_sentence": 800.0,
    "tts_first_chunk": 1800.0,
    "ttfa": 3500.0,
}


@dataclass(slots=True)
class TurnTrace:
    """單一 turn 的打點記錄。

    所有時刻都是 ``time.perf_counter()`` 的絕對值，輸出時才換算成
    相對 t0 的毫秒——perf_counter 的絕對值沒有意義，但差值精度高且單調。
    """

    turn_id: str
    marks: dict[str, float] = field(default_factory=dict)
    phase: TurnPhase = TurnPhase.LISTENING
    cancel_reason: str | None = None
    error: str | None = None
    sentence_count: int = 0
    chunk_count: int = 0
    split_reasons: dict[str, int] = field(default_factory=dict)
    """切點成因分布。``max_length`` 佔比過高 → 切分閾值要調。"""

    spoken_duration_s: float = 0.0
    extra: dict = field(default_factory=dict)

    @property
    def t0(self) -> float | None:
        """TTFA 起算點：使用者說完的時刻。

        沒有 speech_end（純文字輸入的場景）就退回 turn_start。

        注意用 ``in`` 判斷而不是 ``or``——打點值是 0.0 時 ``or`` 會誤判成
        「沒有這個打點」而退回 turn_start，讓整個 TTFA 算成負數。
        """
        if MARK_SPEECH_END in self.marks:
            return self.marks[MARK_SPEECH_END]
        return self.marks.get(MARK_TURN_START)

    def rel_ms(self, mark: str) -> float | None:
        """某個打點相對 t0 的毫秒數。"""
        t0 = self.t0
        if t0 is None or mark not in self.marks:
            return None
        return (self.marks[mark] - t0) * 1000.0

    def span_ms(self, start: str, end: str) -> float | None:
        """兩個打點之間的毫秒數。"""
        if start not in self.marks or end not in self.marks:
            return None
        return (self.marks[end] - self.marks[start]) * 1000.0

    @property
    def ttfa_ms(self) -> float | None:
        """Time To First Audio。這是唯一真正決定體感的數字。"""
        return self.rel_ms(MARK_SPEAK_FIRST_CHUNK)

    def perceived_ttfa_ms(self) -> float | None:
        """使用者第一次聽到任何聲音——墊片音或真音訊，哪個先算哪個。
        與 ``ttfa`` 並列，不取代：TTFA 量的是管線、這個量的是體感。"""
        candidates = [
            v for v in (self.rel_ms(MARK_GAP_FIRST_AUDIO), self.rel_ms(MARK_SPEAK_FIRST_CHUNK))
            if v is not None
        ]
        return min(candidates) if candidates else None

    def segments_ms(self) -> dict[str, float | None]:
        """拆解成設計文件 §5 的五個欄位。"""
        return {
            "stt": self.span_ms(MARK_SPEECH_END, MARK_INPUT_FINAL),
            "memory": self.span_ms(MARK_MEMORY_START, MARK_MEMORY_DONE),
            "tool": self.span_ms(MARK_TOOL_START, MARK_TOOL_DONE),
            "llm_first_token": self.span_ms(MARK_INPUT_FINAL, MARK_THINK_FIRST_TOKEN),
            "llm_first_sentence": self.span_ms(
                MARK_INPUT_FINAL, MARK_THINK_FIRST_SENTENCE
            ),
            "tts_first_chunk": self.span_ms(
                MARK_THINK_FIRST_SENTENCE, MARK_SPEAK_FIRST_CHUNK
            ),
            "ttfa": self.ttfa_ms,
            "gap_audio": self.rel_ms(MARK_GAP_FIRST_AUDIO),
            "perceived_ttfa": self.perceived_ttfa_ms(),
            "total": self.rel_ms(MARK_TURN_END),
        }

    def to_dict(self) -> dict:
        return {
            "turn_id": self.turn_id,
            "phase": self.phase.value,
            "cancel_reason": self.cancel_reason,
            "error": self.error,
            "sentence_count": self.sentence_count,
            "chunk_count": self.chunk_count,
            "split_reasons": dict(self.split_reasons),
            "spoken_duration_s": round(self.spoken_duration_s, 3),
            "segments_ms": {
                k: (round(v, 1) if v is not None else None)
                for k, v in self.segments_ms().items()
            },
            "marks_ms": {
                k: (round(v, 1) if (v := self.rel_ms(k)) is not None else None)
                for k in self.marks
            },
            "extra": self.extra,
        }


class LatencyTracer:
    """打點收集器。

    一個 tracer 服務整個 session，內部依 turn_id 分帳——因為 barge-in
    會讓多個 turn 短暫重疊（舊 turn 還在收尾、新 turn 已經開始）。
    """

    def __init__(self, output_path: str | Path | None = None) -> None:
        self._traces: dict[str, TurnTrace] = {}
        self._order: list[str] = []
        self._output_path = Path(output_path) if output_path else None
        if self._output_path is not None:
            self._output_path.parent.mkdir(parents=True, exist_ok=True)

    def start(self, turn_id: str) -> TurnTrace:
        trace = TurnTrace(turn_id=turn_id)
        self._traces[turn_id] = trace
        self._order.append(turn_id)
        self.mark(turn_id, MARK_TURN_START)
        return trace

    def get(self, turn_id: str) -> TurnTrace | None:
        return self._traces.get(turn_id)

    def mark(
        self, turn_id: str, name: str, at: float | None = None, *, overwrite: bool = False
    ) -> None:
        """打點。**同一個名稱只記第一次**——``think_first_token`` 這類
        「首次」語意的打點，重複呼叫必須不覆蓋。

        ``overwrite=True`` 給「最後一次」語意的打點用（``tool_done``：
        多次工具往返要記最後一次結束）。"""
        trace = self._traces.get(turn_id)
        if trace is None:
            trace = self.start(turn_id)
        if name in trace.marks and not overwrite:
            return
        trace.marks[name] = at if at is not None else time.perf_counter()

    def count_sentence(self, turn_id: str, split_reason: str) -> None:
        trace = self._traces.get(turn_id)
        if trace is None:
            return
        trace.sentence_count += 1
        trace.split_reasons[split_reason] = trace.split_reasons.get(split_reason, 0) + 1

    def count_chunk(self, turn_id: str, duration_s: float = 0.0) -> None:
        trace = self._traces.get(turn_id)
        if trace is None:
            return
        trace.chunk_count += 1
        trace.spoken_duration_s += duration_s

    def finish(
        self,
        turn_id: str,
        phase: TurnPhase = TurnPhase.DONE,
        cancel_reason: str | None = None,
        error: str | None = None,
    ) -> TurnTrace | None:
        trace = self._traces.get(turn_id)
        if trace is None:
            return None
        self.mark(turn_id, MARK_TURN_END)
        trace.phase = phase
        trace.cancel_reason = cancel_reason
        trace.error = error
        if self._output_path is not None:
            with self._output_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(trace.to_dict(), ensure_ascii=False) + "\n")
        return trace

    # --- 報告 ---

    def report(self, turn_id: str | None = None) -> str:
        """人可讀的延遲報告，含預算對照。

        對照預算是重點——只看數字不知道 800ms 算好算壞，
        看到「超支 3.2×」才知道要修哪裡。
        """
        if turn_id is None:
            if not self._order:
                return "（沒有任何 turn 記錄）"
            turn_id = self._order[-1]
        trace = self._traces.get(turn_id)
        if trace is None:
            return f"（找不到 turn {turn_id}）"

        segs = trace.segments_ms()
        lines = [
            f"─── 延遲報告  turn={trace.turn_id}  phase={trace.phase.value} ───",
        ]
        if trace.cancel_reason:
            lines.append(f"  取消原因：{trace.cancel_reason}")
        if trace.error:
            lines.append(f"  錯誤：{trace.error}")

        rows = [
            ("STT 最終轉錄", "stt", "stt"),
            ("Memory retrieve", "memory", "memory"),
            ("工具呼叫", "tool", None),
            ("LLM 首 token", "llm_first_token", None),
            ("LLM 首句", "llm_first_sentence", "llm_first_sentence"),
            ("TTS 首段", "tts_first_chunk", "tts_first_chunk"),
        ]
        lines.append(f"  {'階段':<18}{'實測':>10}{'預算':>10}   {'狀態'}")
        for label, key, budget_key in rows:
            value = segs.get(key)
            budget = BUDGET_MS.get(budget_key) if budget_key else None
            lines.append(
                f"  {label:<18}{_fmt_ms(value):>10}{_fmt_ms(budget):>10}"
                f"   {_verdict(value, budget)}"
            )

        lines.append("  " + "─" * 46)
        lines.append(
            f"  {'TTFA':<18}{_fmt_ms(segs['ttfa']):>10}{_fmt_ms(BUDGET_MS['ttfa']):>10}"
            f"   {_verdict(segs['ttfa'], BUDGET_MS['ttfa'])}"
        )
        if segs.get("gap_audio") is not None:
            lines.append(
                f"  {'感知 TTFA':<18}{_fmt_ms(segs['perceived_ttfa']):>10}{'':>10}"
                f"   墊片音 @ {_fmt_ms(segs['gap_audio'])}"
            )
        lines.append(f"  {'總時長':<18}{_fmt_ms(segs['total']):>10}")
        lines.append(
            f"  句數 {trace.sentence_count} / 音訊段 {trace.chunk_count}"
            f" / 播出 {trace.spoken_duration_s:.1f}s"
        )
        if trace.split_reasons:
            detail = "  ".join(f"{k}={v}" for k, v in sorted(trace.split_reasons.items()))
            lines.append(f"  切點成因：{detail}")
        return "\n".join(lines)

    def summary(self) -> str:
        """多 turn 的彙總（p50 / max TTFA）。"""
        ttfas = [
            t.ttfa_ms
            for t in self._traces.values()
            if t.ttfa_ms is not None and t.phase is TurnPhase.DONE
        ]
        if not ttfas:
            return "（沒有完成的 turn）"
        ttfas.sort()
        p50 = ttfas[len(ttfas) // 2]
        return (
            f"完成 {len(ttfas)} turn｜TTFA p50 {p50:.0f}ms"
            f"｜最快 {ttfas[0]:.0f}ms｜最慢 {ttfas[-1]:.0f}ms"
            f"｜預算 {BUDGET_MS['ttfa']:.0f}ms"
        )


def _fmt_ms(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}ms"


def _verdict(value: float | None, budget: float | None) -> str:
    if value is None or budget is None:
        return ""
    if value <= budget:
        return "✓"
    return f"✗ 超支 {value / budget:.1f}×"
