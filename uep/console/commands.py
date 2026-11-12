"""Console 指令層：以程式方式呼叫 ``Console.execute(line)``，測試與互動 REPL 共用同一條路徑。

層級（D105 Q3）：(b) Core 層隔離與 (c) 端到端的 fake 版由本檔指令驅動；(a) 子系統隔離群
（``/stt``、``/tts``、``/llm``、``/mem``、``/check``）中 ``/stt`` 已於 M3、``/llm`` 已於 N3、``/tts`` 已於 M5 接線，其餘只留骨架。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import wave
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from uep.console.rig import (
    CONSOLE_ACTOR,
    CONSOLE_SPEAKER,
    PRIVATE_SCOPE,
    ConsoleRuntime,
)
from uep.contracts.presence import PresenceLocation
from uep.events.models import EventRow
from uep.log import describe_levels, set_console_level, set_namespace_level
from uep.state.coordinator import Axis
from uep.tasks.manager import AuthenticatedPrincipal
from uep.tasks.models import ActionIntentRow, RunRow, TaskRow
from uep.tasks.states import APPROVAL_REQUIRED, ActionStatus
from uep.turn.models import UtteranceFact

logger = logging.getLogger(__name__)

LATENCY_LOG_ENV = "UEP_CONSOLE_LATENCY_LOG"
"""延遲報告 JSONL 的路徑覆寫；未設＝``<session_dir>/latency.jsonl``，``off``／``0``／``false``／``none``＝關閉。"""
LATENCY_LOG_OFF = ("off", "0", "false", "none")

TERMINAL_EVENTS = ("turn.completed", "turn.cancelled", "turn.skipped")
DEFAULT_WAIT_S = 15.0
STT_WAIT_S = 60.0
"""語音路徑等「辨識完成並開 turn」的上限（首次辨識含 CUDA 暖機餘波）。"""
FEED_CHUNK_S = 0.1
FEED_TAIL_SILENCE_S = 1.6
"""餵完音訊後補的靜音長度，讓 VAD／turn 判定看到足夠的停頓而結束這句話。"""
FEED_SETTLE_S = 2.0
"""一個 turn 結束後，再等多久看有沒有同一段音訊的後續 utterance。長音檔中間的停頓超過 turn 門檻（0.7 秒）
會被切成多句，各句各開一個 turn、後句依「同 owner 新回覆取代舊回覆」取消前句（設計行為，見計畫 §5.5）；
console 要追到最後一個 turn 才算這個音檔跑完。"""
AGGREGATOR_TICK_MARGIN_S = 0.5
"""等聚合器交出緩衝中的 turn 時，在聚合等待之外加的餘裕（tick 間隔＋辨識延遲）。"""
MIC_SAMPLE_RATE = 16000
SUBSYSTEM_NAMESPACES = ("check",)

HELP_TEXT = """\
指令：
  /say <文字>        送出一句話（無指令的輸入行等同 /say），等該 turn 結束並顯示結果
  /loop <文字>       agent 路徑（UEP_TOOL_PATH=agent）：這一句導向 agent_loop，顯示 Run 狀態與工具呼叫（需 UEP_AGENT_LOOP_ENTRY=1）
  /approve [action_id]  核准等待中的工具呼叫（以本機使用者的已驗證人類身分記錄核准）並讓 Run 續跑；不帶參數列出等待核准的項目
  /reject <action_id>   否決等待中的工具呼叫並讓 Run 續跑（模型會看到被否決）
  /wav <路徑>       以音檔當輸入；real 模式經真實 STT（餵進 Host）跑整條 turn，以真實時間速度餵入（會花與音檔等長的時間）；
                     fake 模式由 fake STT 產 utterance
  /cancel            取消進行中的 turn（宿主取消）
  /status            Presence／Interaction 軸、turn 狀態、host 健康
  /trace [all]       最近 turn 的 tracer 事件與延遲（all＝全部 turn 摘要）
  /mem               real 模式：目前 namespace、episode 數、最近寫入（fake 模式未組裝記憶）
  /mem recall <文字> real 模式：經 Core Access Layer 檢索（不排除任何 session）
  /session new       開新 session（新 conversation／segment：清 Think 歷史，記憶不再因同 session 被排除）
  /mem embed <文字> [--device cpu|cuda] [--n 次數]   (a) 隔離：量 query embedding 載入／首查／p50／p95（cuda 需 UEP_E2E_ALLOW_GPU=1）
  /mic               麥克風輸入：Enter 開始、再按 Enter 結束；整段錄音（存成 wav）直接交給 STT 辨識成一句話跑 turn，
                     不經 VAD 與聚合器（需 real STT 與 sounddevice）
  /stream            即時麥克風輸入＋即時輸出（bot 對話形式）：持續收音，VAD 斷句、聚合成 turn、可插話；按 Enter 結束
                     （需 real STT 與 sounddevice；帶 --play 才有聲音輸出；輸入全程存成 wav）
  /stt <wav> [--model 名] [--lang 碼]   (a) 隔離：以 Core adapter＋echo_stt 辨識 wav，印文字與耗時
                     （未設 UEP_E2E_ALLOW_GPU=1 時強制 CPU＋base 模型）
  /llm <文字>        (a) 隔離：直接呼叫 Think（真實 LLM），印切句與耗時（需 UEP_LLM_API_KEY）
  /tts <文字> [--engine vox|indextts2]   (a) 隔離：以 Core Speak adapter 合成並寫 wav，印首音延遲與總耗時
                     （需 GPU；未設 UEP_E2E_ALLOW_GPU=1 時提示並結束該指令）
  /log [level <名稱空間> <等級> | console <等級>]   顯示或調整日誌等級（例：/log level uep.decision DEBUG）
  /check             (a) 子系統隔離群（尚未支援）
  /help              本說明
  /quit              結束"""


class Console:
    """單一 Core 執行期的指令介面；``execute`` 回傳 False 表示要結束。"""

    def __init__(
        self, runtime: ConsoleRuntime, out: Callable[[str], None] = print, wait_s: float = DEFAULT_WAIT_S,
        *, input_fn: Callable[[str], str] = input, mic_factory: Callable[[], object] | None = None,
        stt_wait_s: float = STT_WAIT_S, feed_sleep: bool = True, settle_s: float = FEED_SETTLE_S,
        stream_mic_factory: Callable[[], object] | None = None,
    ) -> None:
        self.rt = runtime
        self.out = out
        self.wait_s = wait_s
        self.input_fn = input_fn
        self.mic_factory = mic_factory
        self.stream_mic_factory = stream_mic_factory
        self.stt_wait_s = stt_wait_s
        self.feed_sleep = feed_sleep
        self.settle_s = settle_s
        self.last_turn_id: str | None = None
        self._commands: dict[str, Callable[[str], None]] = {
            "say": self._say, "loop": self._loop, "approve": self._approve, "reject": self._reject, "wav": self._wav, "cancel": self._cancel, "status": self._status,
            "trace": self._trace, "mem": self._mem, "session": self._session, "mic": self._mic, "stream": self._stream, "help": self._help, "log": self._log, "stt": self._stt, "llm": self._llm, "tts": self._tts,
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
            self.out(f"/{name}：(a) 子系統隔離尚未支援。")
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

    def _log(self, arg: str) -> None:
        parts = arg.split()
        if not parts:
            self.out(describe_levels())
            return
        try:
            if parts[0] == "level" and len(parts) == 3:
                set_namespace_level(parts[1], parts[2])
            elif parts[0] == "console" and len(parts) == 2:
                set_console_level(parts[1])
            else:
                self.out("用法：/log [level <名稱空間> <等級> | console <等級>]")
                return
        except (ValueError, RuntimeError) as exc:
            self.out(f"/log：{exc}")
            return
        self.out(describe_levels())

    def _say(self, text: str) -> None:
        if not text:
            self.out("用法：/say <文字>")
            return
        self._submit(text)

    def _loop(self, text: str) -> None:
        rt = self.rt
        if rt.loop_switch is None:
            self.out("/loop 只在 UEP_TOOL_PATH=agent 下可用（需 --think real）。")
            return
        if not text:
            self.out("用法：/loop <文字>")
            return
        if rt.core.loop_launcher is None:
            self.out("提醒：未啟用 UEP_AGENT_LOOP_ENTRY=1，這句會退回一般 direct_chat 回覆（不會跑 agent_loop）。")
        rt.loop_switch.arm()
        try:
            self._submit(text)
        finally:
            rt.loop_switch.disarm()  # 這句沒被採用（例如 pass、未開 turn）時不留到下一句

    def _wav(self, arg: str) -> None:
        if not arg:
            self.out("用法：/wav <路徑>")
            return
        path = Path(arg.strip('"'))
        if self.rt.input_stage is not None:
            self._wav_real(path)
            return
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

    def _wav_real(self, path: Path) -> None:
        from uep.console.stt_cmd import load_wav_16k_mono

        try:
            pcm, seconds = load_wav_16k_mono(path)
        except (OSError, wave.Error, EOFError, ValueError) as exc:
            self.out(f"無法讀取 wav：{path}（{type(exc).__name__}：{exc}）")
            return
        if self.feed_sleep:
            # 聚合器與 turn 判定靠牆鐘判斷停頓，加速餵入會失真：真實模式以真實時間速度餵入
            self.out(
                f"真實 STT：{path.name}（{seconds:.1f} 秒）以真實時間速度餵入 Host，"
                f"會花與音檔等長的時間（約 {seconds + FEED_TAIL_SILENCE_S:.0f} 秒，含尾端靜音）…"
            )
        else:
            self.out(f"真實 STT：{path.name}（{seconds:.1f} 秒）餵入 Host…")
        self._feed_audio(pcm)

    def _feed_audio(self, pcm: bytes) -> None:
        """16 kHz mono int16 PCM 分塊餵進 Host 的輸入 stage（含尾端靜音），等 STT 產生 utterance 與整個 turn 結束。

        ``feed_sleep`` 為真時以真實時間速度餵入（依截止時間配速，不累加 sleep 的誤差）；測試設為假則不等待。
        """
        rt = self.rt
        before = len(rt.intake_events)
        chunk = int(16000 * FEED_CHUNK_S) * 2
        pcm = pcm + bytes(int(16000 * FEED_TAIL_SILENCE_S) * 2)
        t0 = time.perf_counter()
        pace0 = time.monotonic()
        for n, i in enumerate(range(0, len(pcm), chunk)):
            if not rt.host.feed(CONSOLE_SPEAKER, pcm[i:i + chunk]):
                self.out("音訊未被接受（Presence 非 ACTIVE 或輸入 stage 不可用）。")
                return
            if self.feed_sleep:
                delay = pace0 + (n + 1) * FEED_CHUNK_S - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
        deadline = time.monotonic() + self.stt_wait_s
        while len(rt.intake_events) <= before and time.monotonic() < deadline:
            time.sleep(0.05)
        if len(rt.intake_events) <= before:
            self.out(f"STT 在 {self.stt_wait_s:.0f} 秒內沒有產生 utterance（可能被幻覺過濾或為純雜訊）。")
            return
        # 長音檔可能被 VAD 切成多句；有 aggregator 時句子先緩衝、由 tick 合併成 turn。逐個事件追到最後一個 turn 結束
        index = before
        while index < len(rt.intake_events):
            kind, fact, result = rt.intake_events[index]
            index += 1
            self.out(self._describe_event(kind, fact, result, time.perf_counter() - t0))
            if not _is_buffered(result):
                self._report_turn(result)
            # 句子還在聚合器等待時，turn 要等聚合逾時才會被 tick 交出（依當下頻道模式：被動最多 3 秒，主動約 0.25 秒，
            # 主動模式敘述中另需安靜 2.5 秒）：等待上限取聚合器目前的 pending_wait_s
            wait = self.settle_s + (self._aggregator_wait_s() if _is_buffered(result) else 0.0)
            settle_deadline = time.monotonic() + wait
            while len(rt.intake_events) <= index and time.monotonic() < settle_deadline:
                time.sleep(0.05)

    def _aggregator_wait_s(self) -> float:
        """語音聚合器依當下頻道模式最久還要等多久才會把緩衝中的 turn 交出（含 tick 間隔餘裕）；沒有聚合器為 0。"""
        orchestrator = getattr(self.rt.host, "orchestrator", None)
        pending = float(getattr(getattr(orchestrator, "aggregator", None), "pending_wait_s", 0.0) or 0.0)
        return pending + AGGREGATOR_TICK_MARGIN_S if pending > 0 else 0.0

    @staticmethod
    def _describe_event(kind: str, fact, result, elapsed_s: float) -> str:
        text = fact.text if fact is not None else ""
        if kind == "turn":
            return f"聚合 turn：{text}"
        suffix = "（聚合中，等待併入 turn）" if _is_buffered(result) else ""
        return f"STT 辨識（餵入後 {elapsed_s:.2f} 秒）：{text}{suffix}"

    def _submit(self, text: str, **over) -> None:
        rt = self.rt
        rt.seq += 1
        if "source_location" not in over:
            # 工具層預設：Console 就是本機 Host 的輸入；傳 source_location 可重現缺席端輸入的反例
            try:
                over["source_location"] = PresenceLocation(rt.host.location)
            except ValueError:
                pass
        fact = UtteranceFact(speaker_ref=CONSOLE_SPEAKER, text=text, seq=rt.seq, **over)
        self._report_turn(rt.host.submit_utterance(fact))

    def _report_turn(self, result) -> None:
        rt = self.rt
        if result.ticket is None:
            self.out(f"未開 turn：{result.reason_code}")
            return
        turn_id = result.ticket.turn_id
        self.last_turn_id = turn_id
        started = time.perf_counter()
        kinds = self._wait_terminal(turn_id)
        if not kinds or kinds[-1] not in TERMINAL_EVENTS:
            self.out(f"turn {turn_id} 逾時未結束（{self.wait_s:.0f} 秒），事件：{' > '.join(kinds)}")
            return
        self.out(f"turn {turn_id}：{kinds[-1]}（{time.perf_counter() - started:.2f} 秒）")
        self._write_latency(turn_id, kinds[-1])
        claimed = getattr(rt.host.orchestrator, "loop_claimed", None)
        if callable(claimed) and claimed(turn_id):
            # agent_loop 回合：沒有走 Think／Speak，回覆是端點在 Output Gate 放行後渲染的文字（系統訊息另標示）
            for item in getattr(rt.host.endpoint, "rendered_texts", lambda _t: [])(turn_id):
                self.out(f"{'系統訊息' if item.system else '回應'}：{item.text}")
            self._report_loop(turn_id)
            return
        reply = self._reply_text(turn_id) if kinds[-1] == "turn.completed" else None  # 被取消的輪沒有（完整）回應
        if reply:
            self.out(f"回應：{reply}")
        trace = rt.tracer.get(turn_id)
        if trace is not None and trace.ttfa_ms is not None:
            self.out(f"TTFA：{trace.ttfa_ms:.0f} ms")
        for wav in rt.wavs.get(turn_id, []):
            self.out(f"音訊：{wav}（{_wav_seconds(wav):.1f} 秒）")

    def _write_latency(self, turn_id: str, terminal: str) -> None:
        """每輪結束把 tracer 的延遲欄位（同 ``/trace`` 報告的資料）附加寫成一行 JSON 到 latency.jsonl，供 A8 事後彙整。
        只寫檔、不進正式 journal；關閉或寫檔失敗都不影響回合（失敗只記警告）。"""
        try:
            raw = os.environ.get(LATENCY_LOG_ENV, "").strip()
            if raw.lower() in LATENCY_LOG_OFF:
                return
            trace = self.rt.tracer.get(turn_id)
            if trace is None:
                return
            path = Path(raw) if raw else Path(self.rt.session_dir) / "latency.jsonl"
            record = {"ts": datetime.now(UTC).isoformat(), "terminal": terminal, **trace.to_dict()}
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001 - 延遲紀錄是旁路，任何失敗都不能讓回合失敗
            logger.warning("延遲報告寫檔失敗（turn %s）：%s", turn_id, exc)

    def _approve(self, arg: str) -> None:
        """核准等待中的 action：以 task 擁有者的已驗證人類身分呼叫 ``TaskManager.record_approval``（沿用既有規則：
        只收 human＋verified、hash／target／scope 取自 action 與 task 列、核准單次消耗且 10 分鐘內有效）。
        不帶參數時列出等待核准的 action。
        記錄核准後呼叫 launcher 的公開 ``resume`` 讓 park 中的 Run 續跑（重驗規則在 AgentLoop.resume 與 Manager），並印出結果。"""
        with self.rt.session_factory() as s:
            if not arg:
                pending = list(s.scalars(
                    select(ActionIntentRow).join(RunRow, RunRow.run_id == ActionIntentRow.run_id)
                    .where(ActionIntentRow.status == ActionStatus.INTENDED.value,
                           ActionIntentRow.side_effect_class.in_(sorted(APPROVAL_REQUIRED)))
                    .order_by(ActionIntentRow.created_at)
                ))
                if not pending:
                    self.out("目前沒有等待核准的工具呼叫。")
                for a in pending:
                    self.out(f"等待核准：{a.action_id}（{a.tool}，Run {a.run_id}）→ /approve {a.action_id}")
                return
            action_id = arg.split()[0]
            action = s.get(ActionIntentRow, action_id)
            if action is None:
                self.out(f"找不到 action：{action_id}")
                return
            if action.status != ActionStatus.INTENDED.value or action.side_effect_class not in APPROVAL_REQUIRED:
                self.out(f"action {action_id} 目前不需要核准（狀態 {action.status}，副作用 {action.side_effect_class}）。")
                return
            run = s.get(RunRow, action.run_id)
            task = s.get(TaskRow, run.task_id) if run is not None else None
            if task is None:
                self.out(f"找不到 action {action_id} 所屬的 Task。")
                return
            owner, scope, run_id = task.owner_actor_id, task.scope_ref, action.run_id
            params_hash, tool = action.params_hash, action.tool
        principal = AuthenticatedPrincipal(actor_id=owner, kind="human", verified=True)
        approval = self.rt.core.tasks.record_approval(
            principal=principal, action_hash=params_hash, target=tool, scope=scope,
            expiry=datetime.now(UTC) + timedelta(minutes=10),
        )
        self.out(f"已記錄核准 {approval.approval_id}（{tool}，10 分鐘內有效）。")
        self._resume(run_id, {action_id: approval.approval_id})

    def _reject(self, arg: str) -> None:
        """否決等待中的 action（``record_rejection``，同樣以 task 擁有者的已驗證人類身分），再讓 Run 續跑（模型會看到 rejected）。"""
        action_id = arg.split()[0] if arg else ""
        if not action_id:
            self.out("用法：/reject <action_id>")
            return
        with self.rt.session_factory() as s:
            action = s.get(ActionIntentRow, action_id)
            run = s.get(RunRow, action.run_id) if action is not None else None
            task = s.get(TaskRow, run.task_id) if run is not None else None
            if task is None:
                self.out(f"找不到 action：{action_id}")
                return
            owner, run_id = task.owner_actor_id, run.run_id
        self.rt.core.tasks.record_rejection(
            principal=AuthenticatedPrincipal(actor_id=owner, kind="human", verified=True), action_id=action_id,
        )
        self.out(f"已否決 {action_id}。")
        self._resume(run_id, {})

    def _resume(self, run_id: str, approvals: dict[str, str]) -> None:
        """透過 launcher 的公開 ``resume`` 續跑 park 中的 Run，等結果並印出（Console 只負責呼叫與顯示）。"""
        launcher = self.rt.core.loop_launcher
        if launcher is None:
            self.out("沒有 loop launcher（未啟用 UEP_AGENT_LOOP_ENTRY），無法續跑。")
            return
        try:
            result = launcher.resume(run_id, approvals).result(timeout=self.wait_s)
        except Exception as exc:  # noqa: BLE001 - 續跑被拒或逾時都只回報
            self.out(f"續跑失敗：{type(exc).__name__}：{exc}")
            return
        self.out(f"續跑結果：Run {result.run_id}：{result.state}" + (f"（{result.reason_code}）" if result.reason_code else ""))
        if result.text:
            self.out(f"回應（續跑）：{result.text}")
        with self.rt.session_factory() as s:
            run = s.get(RunRow, run_id)
            actions = list(s.scalars(
                select(ActionIntentRow).where(ActionIntentRow.run_id == run_id).order_by(ActionIntentRow.created_at)
            ))
            self.out(f"Run {run_id}：{run.status if run else '?'}　工具呼叫：" + " > ".join(f"{a.tool}[{a.status}]" for a in actions))

    def _report_loop(self, turn_id: str) -> None:
        """agent_loop 回合的 Task／Run 狀態與工具呼叫（action_intents：工具名稱與狀態）。"""
        with self.rt.session_factory() as s:
            tasks = list(s.scalars(select(TaskRow).where(TaskRow.origin_ref["ref"].astext == turn_id)))
            if not tasks:
                self.out("agent_loop：沒有對應的 Task。")
                return
            for task in tasks:
                for run in s.scalars(select(RunRow).where(RunRow.task_id == task.task_id).order_by(RunRow.attempt)):
                    self.out(f"Run {run.run_id}：{run.status}（Task {task.status}）")
                    actions = s.scalars(
                        select(ActionIntentRow).where(ActionIntentRow.run_id == run.run_id).order_by(ActionIntentRow.created_at)
                    )
                    rows = list(actions)
                    calls = [f"{a.tool}[{a.status}]" for a in rows]
                    self.out("工具呼叫：" + (" > ".join(calls) if calls else "（無）"))
                    for a in rows:
                        if a.status == ActionStatus.INTENDED.value and a.side_effect_class in APPROVAL_REQUIRED:
                            self.out(f"等待核准：{a.action_id}（{a.tool}）→ /approve {a.action_id}")

    def _reply_text(self, turn_id: str) -> str | None:
        """該 turn 的回應文字：fake Think 有 ``committed``；真實 Think 取歷史中最後一則 assistant（turn 已結束，輪是序列的）。"""
        think = self.rt.think
        committed = getattr(think, "committed", None)
        if committed is not None:
            return next((spoken for tid, spoken, _gen in committed if tid == turn_id), None)
        for message in reversed(getattr(think, "history", [])):
            if message.get("role") == "assistant":
                return str(message.get("content", ""))
        return None

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
        sub, _, rest = arg.strip().partition(" ")
        if sub.lower() == "embed":
            from uep.console.mem_cmd import run_mem_embed

            run_mem_embed(rest, self.out)
            return
        rt = self.rt
        if rt.memory_host is None:
            self.out("/mem：此模式未組裝記憶（用 --mode real 或 --memory real）。" + (f"查詢「{arg}」略過。" if arg else ""))
            return
        if sub.lower() == "recall":
            self._mem_recall(rest.strip())
            return
        from uep.console.real_rig import memory_status_lines

        for line in memory_status_lines(rt.memory_host, rt.memory_root):
            self.out(line)

    def _mem_recall(self, query: str) -> None:
        if not query:
            self.out("用法：/mem recall <文字>")
            return
        t0 = time.perf_counter()
        # session_id 給一個不存在的值：不排除任何既有 episode（展示記憶本身，不受目前 session 影響）
        res = self.rt.memory_host.access.retrieve(
            PRIVATE_SCOPE, query, PRIVATE_SCOPE, actor_id=CONSOLE_ACTOR, session_id="console_recall",
        )
        elapsed = time.perf_counter() - t0
        reason = f"　原因={res.degraded_reason}" if res.degraded_reason else ""
        self.out(f"recall（{elapsed * 1000:.0f} ms）狀態={res.memory_status.value}{reason}")
        self.out(res.text.strip() or "（無命中）")

    def _session(self, arg: str) -> None:
        if arg.strip().lower() != "new":
            self.out("用法：/session new")
            return
        from uep.ids import new_id
        from uep.turn.models import SessionContext

        old = self.rt.host._session
        ctx = SessionContext(
            conversation_id=new_id("conv"), segment_id=new_id("seg"), audience_scope=old.audience_scope,
            session_ref=f"console{int(time.time() * 1000)}", platform=old.platform,
        )
        self.rt.host.set_session(ctx)
        self.out(f"已開新 session：conversation={ctx.conversation_id}（Think 歷史已清，記憶不排除舊 session 的 episode）")

    def _mic(self, _arg: str) -> None:
        """按鍵控制的錄音：整段 PCM 直接交給已載入的 STT backend（不經 VAD、不開第二份模型），辨識結果當一句 utterance。"""
        rt = self.rt
        if rt.input_stage is None:
            self.out("/mic：需要真實 STT（--mode real 或 --stt real）。")
            return
        backend = getattr(rt.input_stage, "_backend", None)
        if backend is None:
            self.out("/mic：STT backend 尚未載入（Host 尚未完成啟動）。")
            return
        from uep.console.audio_io import AudioIoError, SoundDeviceMic

        mic = self.mic_factory() if self.mic_factory is not None else SoundDeviceMic()
        self.out("按 Enter 開始錄音…")
        self.input_fn("")
        try:
            mic.start()
        except AudioIoError as exc:
            self.out(f"/mic：{exc}")
            return
        self.out("錄音中，再按 Enter 結束（建議戴耳機，外放會被麥克風收回而自我觸發）。")
        try:
            self.input_fn("")
        finally:
            pcm = mic.stop()
        if not pcm:
            self.out("沒有錄到音訊。")
            return
        seconds = len(pcm) / 32000
        path = self._save_wav(pcm, "mic")
        self.out(f"錄到 {seconds:.1f} 秒，已存成 wav：{path}")
        limit = getattr(backend, "direct_timeout_s", None)
        if limit and seconds >= limit:
            self.out(
                f"提醒：錄音 {seconds:.1f} 秒已達 STT 整段辨識的逾時上限（{limit:.0f} 秒，"
                "echo_stt direct_transcribe_timeout_s），可能逾時而無結果；長段落建議改用 /stream。"
            )
        self.out("整段送進 STT（不經 VAD、不經聚合器）…")
        t0 = time.perf_counter()
        try:
            result = backend.transcribe(pcm, MIC_SAMPLE_RATE)
        except Exception as exc:  # noqa: BLE001 - 辨識失敗不中止 console
            self.out(f"STT 辨識失敗：{type(exc).__name__}：{exc}")
            return
        if result is None:
            self.out("辨識結果：無結果（可能逾時、被過濾或純雜訊）")
            return
        text = result.text.strip()
        if not text:
            self.out("辨識結果：（空：無可用轉錄，可能被幻覺過濾或為純雜訊）")
            return
        min_confidence = float(getattr(rt.input_stage, "min_confidence", 0.0) or 0.0)
        if result.confidence < min_confidence:
            self.out(f"辨識結果信心過低（{result.confidence:.2f} < {min_confidence:.2f}），丟棄：{text}")
            return
        self.out(f"STT 辨識（{time.perf_counter() - t0:.2f} 秒）：{text}")
        over: dict = {"duration_ms": int(seconds * 1000), "language": result.language, "confidence": result.confidence}
        declared = getattr(rt.input_stage, "source_location", None)
        if declared is not None:  # stage 有宣告輸入場域就用它，否則 _submit 預設為本機 Host 的 location
            over["source_location"] = declared
        self._submit(text, **over)

    def _save_wav(self, pcm: bytes, prefix: str) -> Path:
        """把 16 kHz mono int16 PCM 存成 session 目錄下的 wav 並回傳路徑。"""
        path = _unique_wav_path(Path(self.rt.session_dir), prefix)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(MIC_SAMPLE_RATE)
            w.writeframes(pcm)
        return path

    def _stream(self, _arg: str) -> None:
        """即時麥克風輸入＋即時輸出（模擬 bot 的對話形式）：麥克風 chunk 以真實時間持續 ``host.feed``，按 Enter 結束。

        執行緒模型：PortAudio callback 只入列；pump 執行緒取出後寫 wav 並呼叫 ``host.feed``（``ChannelEngine.feed`` 是
        thread-safe 入口，內部以 call_soon_threadsafe 交給 asyncio loop）；reporter 執行緒即時印出每句辨識與聚合 turn，
        turn 的結果報告在單一 worker 依序執行（等 turn 結束會阻塞，不能卡住辨識的即時顯示）；主執行緒等 Enter。
        """
        rt = self.rt
        if rt.input_stage is None:
            self.out("/stream：需要真實 STT（--mode real 或 --stt real）。")
            return
        if not callable(getattr(rt.input_stage, "feed", None)):
            self.out("/stream：此 STT 輸入 stage 不支援即時餵入（UEP_INFER_STT_MODE=single 時無 feed），請用 multichannel。")
            return
        from uep.console.audio_io import AudioIoError, SoundDeviceMicStream

        mic = self.stream_mic_factory() if self.stream_mic_factory is not None else SoundDeviceMicStream()
        if rt.player is None:
            self.out("提醒：未帶 --play，TTS 回應只有文字、不會出聲（音訊仍會寫成 wav）。")
        self.out("即時串流（建議戴耳機，外放會被麥克風收回而自我觸發）。")
        start_index = len(rt.intake_events)
        stop_pump = threading.Event()
        stop_report = threading.Event()
        state = {"bytes": 0, "rejected": 0, "last_event": time.monotonic()}
        turn_ids: set[str] = set()
        reports = ThreadPoolExecutor(max_workers=1, thread_name_prefix="console-stream-turn")
        t0 = time.perf_counter()
        wav: wave.Wave_write | None = None
        path: Path | None = None
        mic_open = False
        pump_thread: threading.Thread | None = None
        report_thread: threading.Thread | None = None

        def write_and_feed(chunk: bytes) -> bool:
            assert wav is not None
            wav.writeframes(chunk)
            state["bytes"] += len(chunk)
            return rt.host.feed(CONSOLE_SPEAKER, chunk)

        def pump() -> None:
            warned = False
            try:
                while not stop_pump.is_set():
                    chunk = mic.read(0.1)
                    if not chunk:
                        continue
                    if not write_and_feed(chunk):
                        state["rejected"] += 1
                        if not warned:
                            warned = True
                            self.out("音訊未被接受（Presence 非 ACTIVE 或輸入 stage 不可用），仍持續錄音。")
            except Exception as exc:  # noqa: BLE001 - 收音執行緒失敗要讓使用者知道，而不是靜默停止
                stop_pump.set()
                self.out(f"/stream：收音執行緒失敗（{type(exc).__name__}：{exc}），已停止收音；按 Enter 結束。")

        def report() -> None:
            index = start_index
            while True:
                if index >= len(rt.intake_events):
                    if stop_report.is_set():
                        return
                    time.sleep(0.05)
                    continue
                kind, fact, result = rt.intake_events[index]
                index += 1
                state["last_event"] = time.monotonic()
                try:
                    self.out(self._describe_event(kind, fact, result, time.perf_counter() - t0))
                    if result.ticket is not None:
                        turn_ids.add(result.ticket.turn_id)
                    if not _is_buffered(result):
                        reports.submit(self._report_turn_safe, result)  # 無 ticket 時只印「未開 turn：原因」
                except Exception as exc:  # noqa: BLE001 - 單一事件顯示失敗不中止報告
                    self.out(f"/stream：事件顯示失敗（{type(exc).__name__}：{exc}）")

        def step(name: str, fn: Callable[[], object]) -> None:
            """收尾的每一步各自獨立：一步失敗只印一行，不跳過後面的步驟。"""
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                self.out(f"/stream：收尾「{name}」失敗（{type(exc).__name__}：{exc}）")

        try:
            try:
                mic.start()
                mic_open = True
            except AudioIoError as exc:
                self.out(f"/stream：{exc}")
                return
            self.out("收音中，按 Enter 結束並回到指令列…")
            path = _unique_wav_path(Path(rt.session_dir), "stream")
            wav = wave.open(str(path), "wb")  # noqa: SIM115 - 生命週期橫跨執行緒，由下方 finally 的 step 關閉
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(MIC_SAMPLE_RATE)
            pump_thread = threading.Thread(target=pump, name="console-stream-pump", daemon=True)
            report_thread = threading.Thread(target=report, name="console-stream-report", daemon=True)
            pump_thread.start()
            report_thread.start()
            try:
                self.input_fn("")
            except KeyboardInterrupt:
                self.out("/stream：收到中斷，開始收尾…")
        finally:
            # 收尾順序：停 pump -> 先停麥克風 -> 排乾佇列（不掉最後一小段）-> 關 wav -> 補尾端靜音並等聚合器 -> 停 reporter
            stop_pump.set()
            pump_alive = False
            if pump_thread is not None:
                step("等待收音執行緒", lambda: pump_thread.join(timeout=5.0))
                pump_alive = pump_thread.is_alive()
                if pump_alive:
                    self.out("/stream：收音執行緒未能及時結束，不關閉 wav（避免寫入已關閉的檔案）。")
            if mic_open:
                step("停止麥克風", mic.stop)
            if wav is not None and not pump_alive:
                def drain() -> None:
                    while True:
                        chunk = mic.read(0.0)
                        if not chunk:
                            return
                        write_and_feed(chunk)

                step("排乾麥克風佇列", drain)
                step("關閉 wav", wav.close)
            if state["bytes"] and not pump_alive:
                def tail_and_wait() -> None:
                    # 補一段靜音（不寫進 wav）讓最後一句的 VAD／turn 判定結束，再等辨識與聚合器把緩衝中的 turn 交出
                    tail = bytes(int(MIC_SAMPLE_RATE * FEED_CHUNK_S) * 2)
                    for _ in range(int(FEED_TAIL_SILENCE_S / FEED_CHUNK_S)):
                        rt.host.feed(CONSOLE_SPEAKER, tail)
                    quiet = time.monotonic()
                    tail_wait = self.settle_s + self._aggregator_wait_s()
                    deadline = time.monotonic() + self.stt_wait_s
                    try:
                        while time.monotonic() < deadline and time.monotonic() - max(quiet, state["last_event"]) < tail_wait:
                            time.sleep(0.05)
                    except KeyboardInterrupt:
                        self.out("/stream：已中斷等待，直接收尾。")

                step("補尾端靜音並等待聚合", tail_and_wait)
            stop_report.set()
            if report_thread is not None:
                step("停止報告執行緒", lambda: report_thread.join(timeout=5.0))
            step("等待 turn 報告", lambda: reports.shutdown(wait=True))
        if path is None:
            return
        seconds = state["bytes"] / 32000
        events = rt.intake_events[start_index:]
        utterances = sum(1 for kind, _f, _r in events if kind == "utterance")
        self.out(
            f"/stream 結束：輸入錄音 {path}；共 {seconds:.1f} 秒；utterance {utterances} 句；turn {len(turn_ids)} 個"
            + (f"；{state['rejected']} 塊音訊未被 Host 接受" if state["rejected"] else "")
        )
        dropped = getattr(mic, "dropped", 0)
        if dropped:
            self.out(f"警告：消費端跟不上，麥克風佇列丟棄了 {dropped} 塊音訊。")

    def _report_turn_safe(self, result) -> None:
        try:
            self._report_turn(result)
        except Exception as exc:  # noqa: BLE001 - 單一 turn 的報告失敗不影響 /stream
            self.out(f"turn 報告失敗：{type(exc).__name__}：{exc}")

    def _stt(self, arg: str) -> None:
        from uep.console.stt_cmd import run_stt

        run_stt(arg, self.out)

    def _llm(self, arg: str) -> None:
        from uep.console.llm_cmd import run_llm

        run_llm(arg, self.out)

    def _tts(self, arg: str) -> None:
        from uep.console.tts_cmd import run_tts

        run_tts(arg, self.out, out_dir=self.rt.session_dir)


def _is_buffered(result) -> bool:
    """這句 utterance 還在 aggregator 等待併入 turn（尚未開 turn）。"""
    return result.ticket is None and result.reason_code == "buffered"


def _unique_wav_path(directory: Path, prefix: str) -> Path:
    """session 目錄下不重名的 wav 路徑（``<prefix>-HHMMSS[-n].wav``）。"""
    directory.mkdir(parents=True, exist_ok=True)
    base = f"{prefix}-{time.strftime('%H%M%S')}"
    path = directory / f"{base}.wav"
    n = 1
    while path.exists():
        n += 1
        path = directory / f"{base}-{n}.wav"
    return path


def _wav_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except (OSError, wave.Error, EOFError):
        return 0.0
