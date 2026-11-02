"""Console 指令層：以程式方式呼叫 ``Console.execute(line)``，測試與互動 REPL 共用同一條路徑。

層級（D105 Q3）：(b) Core 層隔離與 (c) 端到端的 fake 版由本檔指令驅動；(a) 子系統隔離群
（``/stt``、``/tts``、``/llm``、``/mem``、``/check``）中 ``/stt`` 已於 M3、``/llm`` 已於 N3、``/tts`` 已於 M5 接線，其餘只留骨架。
"""
from __future__ import annotations

import time
import wave
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select

from uep.console.rig import CONSOLE_ACTOR, CONSOLE_SPEAKER, PRIVATE_SCOPE, ConsoleRuntime
from uep.events.models import EventRow
from uep.state.coordinator import Axis
from uep.turn.models import UtteranceFact

TERMINAL_EVENTS = ("turn.completed", "turn.cancelled", "turn.skipped")
DEFAULT_WAIT_S = 15.0
STT_WAIT_S = 60.0
"""語音路徑等「辨識完成並開 turn」的上限（首次辨識含 CUDA 暖機餘波）。"""
FEED_CHUNK_S = 0.1
FEED_TAIL_SILENCE_S = 1.6
"""餵完音訊後補的靜音長度，讓 VAD／turn 判定看到足夠的停頓而結束這句話。"""
SUBSYSTEM_NAMESPACES = ("check",)

HELP_TEXT = """\
指令：
  /say <文字>        送出一句話（無指令的輸入行等同 /say），等該 turn 結束並顯示結果
  /wav <路徑>        以音檔當輸入；real 模式經真實 STT（餵進 Host）跑整條 turn，fake 模式由 fake STT 產 utterance
  /cancel            取消進行中的 turn（宿主取消）
  /status            Presence／Interaction 軸、turn 狀態、host 健康
  /trace [all]       最近 turn 的 tracer 事件與延遲（all＝全部 turn 摘要）
  /mem               real 模式：目前 namespace、episode 數、最近寫入（fake 模式未組裝記憶）
  /mem recall <文字> real 模式：經 Core Access Layer 檢索（不排除任何 session）
  /session new       開新 session（新 conversation／segment：清 Think 歷史，記憶不再因同 session 被排除）
  /mem embed <文字> [--device cpu|cuda] [--n 次數]   (a) 隔離：量 query embedding 載入／首查／p50／p95（cuda 需 UEP_E2E_ALLOW_GPU=1）
  /mic               麥克風輸入：Enter 開始、再按 Enter 結束，錄音送進 STT 跑整條 turn（需 real STT 與 sounddevice）
  /stt <wav> [--model 名] [--lang 碼]   (a) 隔離：以 Core adapter＋echo_stt 辨識 wav，印文字與耗時
                     （未設 UEP_E2E_ALLOW_GPU=1 時強制 CPU＋base 模型）
  /llm <文字>        (a) 隔離：直接呼叫 Think（真實 LLM），印切句與耗時（需 UEP_LLM_API_KEY）
  /tts <文字> [--engine vox|indextts2]   (a) 隔離：以 Core Speak adapter 合成並寫 wav，印首音延遲與總耗時
                     （需 GPU；未設 UEP_E2E_ALLOW_GPU=1 時提示並結束該指令）
  /check             (a) 子系統隔離群（尚未支援）
  /help              本說明
  /quit              結束"""


class Console:
    """單一 Core 執行期的指令介面；``execute`` 回傳 False 表示要結束。"""

    def __init__(
        self, runtime: ConsoleRuntime, out: Callable[[str], None] = print, wait_s: float = DEFAULT_WAIT_S,
        *, input_fn: Callable[[str], str] = input, mic_factory: Callable[[], object] | None = None,
        stt_wait_s: float = STT_WAIT_S, feed_sleep: bool = True,
    ) -> None:
        self.rt = runtime
        self.out = out
        self.wait_s = wait_s
        self.input_fn = input_fn
        self.mic_factory = mic_factory
        self.stt_wait_s = stt_wait_s
        self.feed_sleep = feed_sleep
        self.last_turn_id: str | None = None
        self._commands: dict[str, Callable[[str], None]] = {
            "say": self._say, "wav": self._wav, "cancel": self._cancel, "status": self._status,
            "trace": self._trace, "mem": self._mem, "session": self._session, "mic": self._mic, "help": self._help, "stt": self._stt, "llm": self._llm, "tts": self._tts,
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
        self.out(f"真實 STT：{path.name}（{seconds:.1f} 秒）餵入 Host…")
        self._feed_audio(pcm)

    def _feed_audio(self, pcm: bytes) -> None:
        """16 kHz mono int16 PCM 分塊餵進 Host 的輸入 stage（含尾端靜音），等 STT 產生 utterance 與整個 turn 結束。"""
        rt = self.rt
        before = len(rt.submissions)
        chunk = int(16000 * FEED_CHUNK_S) * 2
        pcm = pcm + bytes(int(16000 * FEED_TAIL_SILENCE_S) * 2)
        t0 = time.perf_counter()
        for i in range(0, len(pcm), chunk):
            if not rt.host.feed(CONSOLE_SPEAKER, pcm[i:i + chunk]):
                self.out("音訊未被接受（Presence 非 ACTIVE 或輸入 stage 不可用）。")
                return
            if self.feed_sleep:
                time.sleep(FEED_CHUNK_S / 10)  # 不即時，但避免瞬間塞滿聲道佇列
        deadline = time.monotonic() + self.stt_wait_s
        while len(rt.submissions) <= before and time.monotonic() < deadline:
            time.sleep(0.05)
        if len(rt.submissions) <= before:
            self.out(f"STT 在 {self.stt_wait_s:.0f} 秒內沒有產生 utterance（可能被幻覺過濾或為純雜訊）。")
            return
        fact, result = rt.submissions[before]
        self.out(f"STT 辨識（餵入後 {time.perf_counter() - t0:.2f} 秒）：{fact.text}")
        self._report_turn(result)

    def _submit(self, text: str, **over) -> None:
        rt = self.rt
        rt.seq += 1
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
        reply = self._reply_text(turn_id) if kinds[-1] == "turn.completed" else None  # 被取消的輪沒有（完整）回應
        if reply:
            self.out(f"回應：{reply}")
        trace = rt.tracer.get(turn_id)
        if trace is not None and trace.ttfa_ms is not None:
            self.out(f"TTFA：{trace.ttfa_ms:.0f} ms")
        for wav in rt.wavs.get(turn_id, []):
            self.out(f"音訊：{wav}（{_wav_seconds(wav):.1f} 秒）")

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
        rt = self.rt
        if rt.input_stage is None:
            self.out("/mic：需要真實 STT（--mode real 或 --stt real）。")
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
        self.out(f"錄到 {len(pcm) / 32000:.1f} 秒，送進 STT…")
        self._feed_audio(pcm)

    def _stt(self, arg: str) -> None:
        from uep.console.stt_cmd import run_stt

        run_stt(arg, self.out)

    def _llm(self, arg: str) -> None:
        from uep.console.llm_cmd import run_llm

        run_llm(arg, self.out)

    def _tts(self, arg: str) -> None:
        from uep.console.tts_cmd import run_tts

        run_tts(arg, self.out, out_dir=self.rt.session_dir)


def _wav_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except (OSError, wave.Error, EOFError):
        return 0.0
