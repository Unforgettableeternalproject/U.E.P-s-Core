"""Console 指令層：以程式方式呼叫 ``Console.execute(line)``，測試與互動 REPL 共用同一條路徑。

層級（D105 Q3）：(b) Core 層隔離與 (c) 端到端的 fake 版由本檔指令驅動；(a) 子系統隔離群
（``/stt``、``/tts``、``/llm``、``/mem``、``/check``）中 ``/stt`` 已於 M3、``/llm`` 已於 N3 接線，其餘只留骨架，待 M5。
"""
from __future__ import annotations

import time
import wave
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select

from uep.console.rig import CONSOLE_SPEAKER, ConsoleRuntime
from uep.events.models import EventRow
from uep.state.coordinator import Axis
from uep.turn.models import UtteranceFact

TERMINAL_EVENTS = ("turn.completed", "turn.cancelled", "turn.skipped")
DEFAULT_WAIT_S = 15.0
SUBSYSTEM_NAMESPACES = ("tts", "check")

HELP_TEXT = """\
指令：
  /say <文字>        送出一句話（無指令的輸入行等同 /say），等該 turn 結束並顯示結果
  /wav <路徑>        以音檔當輸入；fake 模式由 fake STT 產 utterance（不做真辨識）
  /cancel            取消進行中的 turn（宿主取消）
  /status            Presence／Interaction 軸、turn 狀態、host 健康
  /trace [all]       最近 turn 的 tracer 事件與延遲（all＝全部 turn 摘要）
  /mem [查詢]        記憶（v0 fake 模式未組裝記憶）
  /mic               麥克風輸入（尚未實作）
  /stt <wav> [--model 名] [--lang 碼]   (a) 隔離：以 Core adapter＋echo_stt 辨識 wav，印文字與耗時
                     （未設 UEP_E2E_ALLOW_GPU=1 時強制 CPU＋base 模型）
  /llm <文字>        (a) 隔離：直接呼叫 Think（真實 LLM），印切句與耗時（需 UEP_LLM_API_KEY）
  /tts /check        (a) 子系統隔離群（待 M5）
  /help              本說明
  /quit              結束"""


class Console:
    """單一 Core 執行期的指令介面；``execute`` 回傳 False 表示要結束。"""

    def __init__(
        self, runtime: ConsoleRuntime, out: Callable[[str], None] = print, wait_s: float = DEFAULT_WAIT_S
    ) -> None:
        self.rt = runtime
        self.out = out
        self.wait_s = wait_s
        self.last_turn_id: str | None = None
        self._commands: dict[str, Callable[[str], None]] = {
            "say": self._say, "wav": self._wav, "cancel": self._cancel, "status": self._status,
            "trace": self._trace, "mem": self._mem, "mic": self._mic, "help": self._help, "stt": self._stt, "llm": self._llm,
        }

    def execute(self, line: str) -> bool:
        line = line.strip()
        if not line:
            return True
        if not line.startswith("/"):
            self._say(line)
            return True
        name, _, arg = line[1:].partition(" ")
        name = name.lower()
        if name in ("quit", "exit", "q"):
            return False
        if name in SUBSYSTEM_NAMESPACES:
            self.out(f"/{name}：(a) 子系統隔離尚未支援（待 M5 接線）。")
            return True
        handler = self._commands.get(name)
        if handler is None:
            self.out(f"未知指令 /{name}，輸入 /help 查看可用指令。")
            return True
        try:
            handler(arg.strip())
        except Exception as exc:  # noqa: BLE001 - console 不因單一指令失敗而中止
            self.out(f"指令失敗：{type(exc).__name__}：{exc}")
        return True

    # ---- 指令 ----

    def _help(self, _arg: str) -> None:
        self.out(HELP_TEXT)

    def _say(self, text: str) -> None:
        if not text:
            self.out("用法：/say <文字>")
            return
        self._submit(text)

    def _wav(self, arg: str) -> None:
        if not arg:
            self.out("用法：/wav <路徑>")
            return
        path = Path(arg.strip('"'))
        try:
            with wave.open(str(path), "rb") as w:
                seconds = w.getnframes() / float(w.getframerate())
        except (OSError, wave.Error, EOFError) as exc:
            self.out(f"無法讀取 wav：{path}（{type(exc).__name__}）")
            return
        # fake STT：不做辨識，只依音檔產生一句固定格式的 utterance
        text = f"（fake STT）{path.name}，{seconds:.1f} 秒"
        self.out(f"fake STT：{text}")
        self._submit(text, duration_ms=int(seconds * 1000))

    def _submit(self, text: str, **over) -> None:
        rt = self.rt
        rt.seq += 1
        fact = UtteranceFact(speaker_ref=CONSOLE_SPEAKER, text=text, seq=rt.seq, **over)
        result = rt.host.submit_utterance(fact)
        if result.ticket is None:
            self.out(f"未開 turn：{result.reason_code}")
            return
        turn_id = result.ticket.turn_id
        self.last_turn_id = turn_id
        kinds = self._wait_terminal(turn_id)
        if not kinds or kinds[-1] not in TERMINAL_EVENTS:
            self.out(f"turn {turn_id} 逾時未結束（{self.wait_s:.0f} 秒），事件：{' > '.join(kinds)}")
            return
        self.out(f"turn {turn_id}：{kinds[-1]}")
        reply = next((spoken for tid, spoken, _gen in rt.think.committed if tid == turn_id), None)
        if reply:
            self.out(f"回應：{reply}")
        for wav in rt.wavs.get(turn_id, []):
            self.out(f"音訊：{wav}（{_wav_seconds(wav):.1f} 秒）")

    def _wait_terminal(self, turn_id: str) -> list[str]:
        deadline = time.monotonic() + self.wait_s
        kinds = self._turn_kinds(turn_id)
        while time.monotonic() < deadline:
            kinds = self._turn_kinds(turn_id)
            if set(TERMINAL_EVENTS) & set(kinds):
                self.rt.host._worker.drain(max(0.1, deadline - time.monotonic()))
                return self._turn_kinds(turn_id)
            time.sleep(0.02)
        return kinds

    def _turn_kinds(self, turn_id: str) -> list[str]:
        with self.rt.session_factory() as s:
            stmt = select(EventRow.event_type).where(EventRow.turn_id == turn_id).order_by(EventRow.journal_seq)
            return list(s.scalars(stmt))

    def _cancel(self, _arg: str) -> None:
        coordinator = self.rt.host.coordinator
        live = list(coordinator.live_turns()) if coordinator is not None else []
        if not live:
            self.out("目前沒有進行中的 turn。")
            return
        for view in live:
            claimed = coordinator.cancel_turn(view.turn_id, reason_code="console_cancel", detail="/cancel")
            self.out(f"取消 {view.turn_id}：{'已認領' if claimed else '未認領（可能已結束）'}")

    def _status(self, _arg: str) -> None:
        rt = self.rt
        presence = rt.core.presence.current()
        self.out(f"模式：{rt.mode}　Core 已啟動：{rt.core.started}　Host 已啟動：{rt.host.started}")
        self.out(
            f"Presence：{presence.status.value}（endpoint={presence.endpoint_id}，generation={presence.generation}，"
            f"epoch={presence.core_epoch}/{presence.fencing_epoch}）"
        )
        runtime_axis = rt.core.state.current(Axis.RUNTIME).state
        interaction = rt.core.state.current(Axis.INTERACTION).state
        self.out(f"Runtime：{runtime_axis}　Interaction：{interaction}")
        health = rt.core.state.health()
        self.out("Health：" + ("、".join(f"{k}={v}" for k, v in sorted(health.items())) or "（無投影）"))
        coordinator = rt.host.coordinator
        live = [v.turn_id for v in coordinator.live_turns()] if coordinator is not None else []
        self.out(f"Turn：進行中 {len(live)}（{', '.join(live) or '無'}）；最近 {self.last_turn_id or '無'}")
        if self.last_turn_id:
            self.out("　最近 turn 事件：" + " > ".join(self._turn_kinds(self.last_turn_id)))
        metrics = {k: v for k, v in sorted(rt.host.metrics.items()) if v}
        self.out("Host 指標：" + (", ".join(f"{k}={v}" for k, v in metrics.items()) or "（空）"))

    def _trace(self, arg: str) -> None:
        tracer = self.rt.tracer
        if arg == "all":
            self.out(tracer.summary())
            return
        turn_id = self.last_turn_id
        if turn_id is None or tracer.get(turn_id) is None:
            self.out("尚無 turn 的 trace，先用 /say。")
            return
        self.out(tracer.report(turn_id))
        self.out("journal 事件：" + " > ".join(self._turn_kinds(turn_id)))

    def _mem(self, arg: str) -> None:
        self.out("/mem：fake 模式未組裝記憶（A5 待 N5）。" + (f"查詢「{arg}」略過。" if arg else ""))

    def _mic(self, _arg: str) -> None:
        self.out("/mic：麥克風輸入尚未實作（A4，待 M5）。")

    def _stt(self, arg: str) -> None:
        from uep.console.stt_cmd import run_stt

        run_stt(arg, self.out)

    def _llm(self, arg: str) -> None:
        from uep.console.llm_cmd import run_llm

        run_llm(arg, self.out)


def _wav_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except (OSError, wave.Error, EOFError):
        return 0.0
