"""TurnCoordinator——多輪輸出端的排隊、取消與結算（T-07b PR ②）。

把 bot 的「多輪排隊、讓位、regen、PASS、barge-in」執行機制收進 ES。
**機制在這裡，決策留宿主**：讓位、passive、PASS、regen 的判定都是策略插槽
（見 :mod:`uep.stream.contracts.output`），ES 只提供執行與同步呼叫點。

## 一輪的生命週期

``submit()`` 立即返回 :class:`TurnHandle`，背景 task 依序做：

1. 等 think 鎖（FIFO；等鎖期間被作廢 → ``VOIDED``，不產生任何生成／播放）
   取得鎖後、每一趟生成開始前呼叫宿主的 ``on_think_start``（鎖內；例外與逾時吞掉並記 log，
   摘要放 ``metadata["on_think_start_error"]``）
2. 生成：think → speak，音訊塊在「前一輪還沒播完」時先緩衝（合成不等前一輪），
   輪到自己才寫 sink；前一輪已結束就直接寫（**熱路徑不經緩衝、不多一次 await**）
3. 生成完成：EAGER 在釋放鎖**之前** commit；SETTLED 鎖一路持有到結算（串行語意）
4. drain（可被取消、有逾時）→ 結算 spoken → commit／settle → 恰好一個 outcome

## 取消（見草案 §7）

每輪有兩層 token：整輪 token（think 用）與其下游的語音 token（speak／sink 用）。
取消先「認領」（per turn 只成功一次），再同步 ``sink.stop()``，最後取消 token：

* barge-in：``BARGE_IN``，**只取消語音 token**（停播、停 TTS），think 照常生成到完，
  完整文字在 outcome 供補傳（同 bot 的 ``token.child()``）；排在後面還沒開播的輪
  的語音連帶取消（detail ``cascade``）。barge-in 之後仍可再整輪作廢
* 語音側（TTS／sink 寫入／drain）例外：同樣**只取消語音 token**（kind ``voice_error``），
  think 照常生成到完、文字照常 commit 與送出；outcome 為 ``phase=DONE``、
  ``status=SKIPPED``、``skipped_reason=voice_error``、``metadata["voice_error"]`` 帶例外摘要，
  ``cut_short=True``，不標 ``barge_in``／``aborted``、沒有 ``reason_code``（跟 PASS 一樣屬
  「這輪沒把語音唸完，文字照常處理」，既有只看 CANCELLED／aborted 的消費者不會把文字丟掉）。
  think 本身的例外維持 ``FAILED``
* 宿主單輪停語音 ``cancel_voice()``：同一條「只取消語音」機制（kind ``voice_cancel``），
  語意同 barge-in 但不標 ``barge_in``、不連帶取消後面排隊輪；outcome 為 ``phase=DONE``、
  ``status=SKIPPED``、``skipped_reason=voice_cancelled``、``metadata["voice_cancelled"]`` 為 detail
* 讓位 ``void()``：``SUPERSEDED`` + detail ``yielded``；已開播的輪不處理（回 False）
* 同 owner 新回覆：``SUPERSEDED`` + detail ``superseded_by_owner``
* ``cancel_all()``：``SHUTDOWN``，整輪作廢（think 也停），不 commit、不 settle
* 宿主單輪整輪取消 ``cancel_turn()``：kind ``host``，整輪作廢（think 也停），已開播也可、
  不看 ``flags.barge_in``、不連帶取消後面排隊的輪（同 ``cancel_voice``）。token 原因用
  ``HOST``（不 commit、不 settle，同 ``cancel_all``），``metadata["reason_code"]`` 是宿主傳入值
  （建議 ``host_cancelled``／``global_stop``／``presence_lost``）、``cancel_detail`` 為 detail；
  不標 ``barge_in``／``superseded``，``aborted=True``。等鎖期間被取消同其他整輪取消為 ``VOIDED``。
  歷史：預設與 ``cancel_all`` 相同，結算不 commit；EAGER 且生成已完成的輪，完整生成文字早已
  commit 並留在歷史。``commit="none"`` 則由 coordinator 在結算時呼叫 ``think.retract(turn_id)``
  把整輪（使用者話語與回答）撤出歷史，結果記在 ``metadata["history_retracted"]``

## 執行緒契約

| 入口 | 執行緒 | 備註 |
| --- | --- | --- |
| ``on_voice_event`` / ``void`` / ``cancel_voice`` | 任何執行緒 | 同步 stop |
| ``cancel_turn`` / ``cancel_all`` | 任何執行緒 | 同步 stop |
| ``bind_loop`` | 任何執行緒 | 須在第一次 ``submit`` 之前 |
| ``busy`` / ``waiting`` / ``live_turns`` | 建議 loop 內 | 其他執行緒讀到瞬時快照 |
| ``submit`` / ``join`` / ``aclose`` | 只能 loop 內 | loop 須與 ``bind_loop`` 一致 |
| ``sink_factory`` / ``on_think_start`` / ``on_sentence`` | loop 內執行 | 須快速 |
| ``on_generation_done`` / ``on_turn_ended`` | loop 內執行 | 不得阻塞 |
| 策略 ``refresh`` / ``SpeechTextFilter`` / ``AdmissionPolicy`` | loop 內執行 | 同步、快速 |
| ``PolicySource.current()`` / 策略 ``on_voiced`` | 呼叫端執行緒 | 純記憶體 |

* 跨執行緒入口：呼叫執行緒內只做同步 ``sink.stop()``，token 取消一律 ``call_soon_threadsafe``
  回 loop；每輪只認領一次（``_claim`` 以 ``threading.Lock`` 保護）。
* loop 內執行的回呼若阻塞（DB、同步 I/O），整條管線都會卡住；請丟進執行緒安全佇列，
  由宿主自己的工作緒處理。
* 呼叫端執行緒指 STT／接收執行緒，與 ``on_voice_event`` 同一個。
* 跨執行緒排回 loop 時若 loop 已關閉，吞掉 ``RuntimeError`` 並記 debug，不拋給呼叫端。
* ``on_turn_ended`` 在 ``DROPPED_BUSY`` 與關閉後的 submit 這兩條路徑，於 ``submit()`` 返回前、
  在 loop 執行緒同步觸發；其餘路徑在該輪的 task 內觸發。
* ``on_generation_done``：同步回呼保證早於該輪 outcome；非同步回呼只保證已排程，
  不保證與 outcome 的先後（``aclose`` 會等它收尾，逾時取消）。
* ``FAILED`` 的 ``reason_code`` 一律是 ``upstream_error``，包含「先被認領取消、之後才拋例外」
  的邊角（此時 ``cancel_reason`` 仍為 None，認領的種類不反映在 reason_code）。
* ``_closed`` 永久，``aclose`` 之後不能 reopen。

``void`` / ``cancel_turn`` / ``cancel_all`` / ``on_voice_event`` 會**無鎖**讀 ``_live`` 的快照
與 ``audio_started``。
非 loop 執行緒可能讀到過渡狀態（例如某輪恰好此刻開播，``void`` 因而回 False 而不是 True），
這個競態可接受：後果只是「這次讓位沒成功」，不會重複取消、不會漏掉 outcome；真正的狀態改變
（認領）仍由鎖保護、每輪恰好一次。
:meth:`aclose` 之後這些入口全部 no-op（``void`` / ``cancel_voice`` / ``cancel_turn`` 回 False）。

## 不做的事

不接 GapFiller（墊片音屬 ``PipelineRunner`` 單輪路徑）、不碰 ``run_turn``；
記憶寫入不在這裡（宿主依 :class:`~uep.stream.contracts.output.TurnOutcome` 自行決定）。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from typing import Literal

from .contracts.cancellation import CancellationToken, CancelledError, CancelReason
from .contracts.output import (
    AdmissionPolicy,
    BargeInPolicy,
    CommitMode,
    ModeFlags,
    OutcomeStatus,
    PlayedEvidence,
    PolicySnapshot,
    PolicySource,
    SkipReason,
    SpokenRule,
    TurnOutcome,
    TurnRequest,
    TurnView,
    YieldPolicy,
    resolve_spoken_rule,
    sink_first_frame_at,
    sink_is_active,
    sink_played_evidence,
)
from .contracts.stages import AudioSink, SpeakStage, ThinkStage
from .contracts.turn import VoiceEvent
from .contracts.types import AudioChunk, Sentence, TurnPhase
from . import tracer as marks
from .policies import (
    AcceptAllAdmission,
    AccumulatingBargeIn,
    NeverYield,
    StaticPolicySource,
    build_sentence_spans,
    partial_spoken,
    whole_sentence_spoken,
)
from .tracer import LatencyTracer

_log = logging.getLogger(__name__)

PLAY_TIMEOUT_S = 180.0
"""等一輪播完的上限，避免 TTS／播放卡死時鎖永遠不放（對應 bot 的 VOICE_WAIT_TIMEOUT_S）。"""

IDLE_TIMEOUT_S = 0.5
"""``sink.wait_idle()`` 的逾時：逾時放行並記 log（對應 bot 的 wait_vc_idle，Q7）。"""

_END = object()
_CANCELLED = object()
"""句子佇列的內部標記：結束、取消。"""

THINK_HOOK_TIMEOUT_S = 10.0
"""``on_think_start`` 的逾時：hook 卡住會一直持有 think 鎖，逾時視為 hook 失敗並照常生成。"""

BG_CALLBACK_GRACE_S = 1.0
"""``aclose`` 等非同步 ``on_generation_done`` 收尾的寬限；逾時就取消它。"""

THINK_HOOK_CANCEL_GRACE_S = 1.0
"""取消（逾時或整輪作廢）hook 之後等它收尾的寬限；hook 吞掉取消也不會繼續卡住鎖。"""

TEXT_TIMEOUT_S = 30.0
"""結算前等 ``on_sentence`` 送完文字的上限（逾時就放棄剩餘句子，不擋 outcome）。"""

REASON_CASCADE = "cascade"
REASON_OWNER = "superseded_by_owner"
REASON_YIELDED = "yielded"

KIND_YIELD = "yield"
KIND_OWNER = "owner"
KIND_BARGE_IN = "barge_in"
KIND_CASCADE = "cascade"
KIND_VOICE_ERROR = "voice_error"
KIND_VOICE_CANCEL = "voice_cancel"
KIND_HOST = "host"
"""取消種類（認領時記錄，決定 reason_code／superseded／cascade 旗標；``detail`` 只是自由字串）。
其餘種類沿用 ``CancelReason.value``（shutdown、timeout、upstream_error…）。"""


REASON_HOST_CANCELLED = "host_cancelled"
"""``cancel_turn`` 的預設 ``reason_code``。
其餘建議值 ``global_stop``／``presence_lost``（不限制值域）。"""

MAX_REASON_CODE_LEN = 64
"""``cancel_turn`` 的 ``reason_code`` 長度上限（strip 後）。"""

_SOFT_VOICE_KINDS = (KIND_VOICE_ERROR, KIND_VOICE_CANCEL)
"""只關語音、輪本身仍以 DONE 結算的語音取消種類（與 barge-in／cascade 的 CANCELLED 區分）。"""


class _VoiceSideError(Exception):
    """語音側呼叫（speak 產音訊、sink.write、flush、drain）拋出的例外。

    包一層是為了和 coordinator 自身的程式錯誤區分：只有它會走「只停語音」路徑。"""

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


def _stop_sink(sink: AudioSink | None) -> None:
    if sink is None:
        return
    with contextlib.suppress(Exception):
        sink.stop()


class _Attempt:
    """一趟生成（regen 時同一輪會有多趟）。每趟有自己的 sink、token 與記帳。"""

    __slots__ = (
        "utterance", "token", "voice", "sink", "attempt_id", "direct",
        "generated", "voiced", "sent_audio", "timeline", "emitted",
        "buffer", "first_write_at", "wrote_any", "idle_done",
        "passed", "regen_utt", "flusher", "text_q", "text_worker",
        "pump", "pump_error",
    )

    def __init__(self, utterance, token, voice, sink, attempt_id: int, direct: bool) -> None:
        self.utterance = utterance
        self.token: CancellationToken = token
        """整輪 token（think 用）：取代、讓位、shutdown 等整輪作廢時取消。"""

        self.voice: CancellationToken = voice
        """語音 token（speak、sink、drain 用），是整輪 token 的下游：
        barge-in 只取消它——停播與 TTS，但 think 照常生成到完（同 bot 的 token.child()）。"""

        self.sink: AudioSink | None = sink
        self.attempt_id = attempt_id
        self.direct = direct
        """True = 前一輪已結束，音訊塊直接寫 sink（不經緩衝）。"""

        self.generated: dict[int, Sentence] = {}
        self.voiced: dict[int, str] = {}
        """句序號 → 送進 TTS 的文字（過濾後）。"""

        self.sent_audio: dict[int, float] = {}
        """句序號 → 已交給 sink 的音訊秒數（寫入前累加，同 bot）。"""

        self.timeline: list[tuple[int, float]] = []
        self.emitted = 0.0
        self.buffer: deque[AudioChunk] = deque()
        self.first_write_at: float | None = None
        self.wrote_any = False
        self.idle_done = False
        self.passed = False
        self.regen_utt = None
        self.flusher: asyncio.Future | None = None
        self.text_q: asyncio.Queue | None = None
        self.text_worker: asyncio.Future | None = None
        self.pump: asyncio.Future | None = None
        self.pump_error: BaseException | None = None


class TurnHandle:
    """``submit`` 的回傳值，同時是策略看到的 :class:`~uep.stream.contracts.output.TurnView`。"""

    def __init__(
        self,
        request: TurnRequest,
        flags: ModeFlags,
        loop: asyncio.AbstractEventLoop,
        snapshot_version: int,
        stale: bool,
        coordinator: TurnCoordinator,
    ) -> None:
        ticket = request.ticket
        self.request = request
        self.ticket = ticket
        self.turn_id = ticket.turn_id
        self.owner_id = ticket.owner_id
        self.reply_mode = ticket.reply_mode
        self.meta = request.meta
        """宿主私有旗標（與 ``TurnRequest.meta`` 同一個 dict）。"""

        self.flags = flags
        self.generation_done: asyncio.Future[None] = loop.create_future()
        """文字生成結束（完成、取消或失敗都會 resolve，不拋例外）。"""

        self.settled: asyncio.Future[TurnOutcome] = loop.create_future()
        """這一輪的唯一 outcome。"""

        self.regenerated = 0
        self._coordinator = coordinator
        self._base_barge = {u for u in request.barge_users if u}
        self._snapshot_version = snapshot_version
        self._stale = stale
        self._root = CancellationToken()
        self._voice_root = CancellationToken(parent=self._root)
        self._voice_claimed = False
        self._voice_detail = ""
        self._att: _Attempt | None = None
        self._first_sink: AudioSink | None = None
        self._skip_reason: str | None = None
        self._predecessor: TurnHandle | None = None
        self._task: asyncio.Future | None = None
        self._lock_state = "none"
        self._play_done = asyncio.Event()
        self._done = False
        self._attempt_no = 1
        self._eager_committed = False
        self._queue_wait_s = 0.0
        self._cancel_lock = threading.Lock()
        self._cancel_claimed = False
        self._cancel_reason: CancelReason | None = None
        self._cancel_detail = ""
        self._cancel_kind: str | None = None
        self._host_reason_code: str | None = None
        self._host_commit = "default"
        self._concluding = False
        """結算已開始（``_conclude`` 取走取消快照）；之後 ``cancel_turn`` 不再認領。"""
        self._voice_kind: str | None = None
        self._voice_error: str | None = None
        self._hook_error: str | None = None
        self._gen_started = False
        """think 已開始生成（進過 ``_generate``）；``on_generation_done`` 只對有生成的輪呼叫。"""

        self._voice_settled = False
        """語音已播完（drain 返回）：之後 voice_cancel／voice_error 不能再認領。"""

        self._chaining = False

    # --- TurnView ---

    @property
    def audio_started(self) -> bool:
        att = self._att
        if att is None or att.sink is None:
            return False
        if hasattr(att.sink, "first_frame_at"):
            return sink_first_frame_at(att.sink) is not None
        return att.first_write_at is not None

    @property
    def skip_reason(self) -> str | None:
        """目前這趟的靜態跳過原因（mode_silent、sink 建立失敗等）；None = 無。"""
        return self._skip_reason

    @property
    def predecessor(self) -> TurnHandle | None:
        """排在前面、播放位置尚未讓出的上一輪（submit 時決定）；None = 直接輪到自己。"""
        return self._predecessor

    @property
    def behind_playing(self) -> bool:
        pred = self._predecessor
        return pred is not None and not pred._play_done.is_set() and pred.audio_started

    @property
    def sink_active(self) -> bool:
        att = self._att
        if att is None or att.sink is None:
            return False
        fallback = att.first_write_at is not None and not att.voice.is_cancelled
        return sink_is_active(att.sink, fallback=fallback)

    @property
    def barge_users(self) -> set[str]:
        users = set(self._base_barge)
        if not self._stale:
            users |= {u for u in getattr(self.request.text_filter, "addressee_ids", ()) if u}
        return users

    # --- 宿主操作 ---

    @property
    def attempt_id(self) -> int:
        """目前是第幾趟生成（regen 時 +1，turn_id 不變）。"""
        return self._attempt_no

    @property
    def token(self) -> CancellationToken:
        """目前這趟生成的 token（regen 時每趟新建）。"""
        return self._att.token if self._att is not None else self._root

    @property
    def voice_token(self) -> CancellationToken:
        """目前這趟的語音 token（barge-in 只取消它；think 用 :attr:`token`）。"""
        return self._att.voice if self._att is not None else self._voice_root

    def add_barge_user(self, user_id: str) -> None:
        """生成中追加可打斷這輪的說話者（對應 bot 的 addressee_ids）。"""
        if user_id:
            self._base_barge.add(user_id)

    async def wait(self) -> TurnOutcome:
        return await self.settled


class TurnCoordinator:
    """多輪輸出協調器。見模組 docstring。

    ``barge_in`` 預設 :class:`~uep.stream.policies.AccumulatingBargeIn`
    （門檻取自 ``policy_source`` 的快照）；``yield_policy`` 預設 ``NeverYield``。
    """

    def __init__(
        self,
        think_stage: ThinkStage,
        speak_stage: SpeakStage,
        *,
        tracer: LatencyTracer | None = None,
        policy_source: PolicySource | None = None,
        barge_in: BargeInPolicy | None = None,
        yield_policy: YieldPolicy | None = None,
        admission: AdmissionPolicy | None = None,
        on_turn_ended: Callable[[TurnOutcome], None] | None = None,
        supersede_same_owner: bool = True,
        cascade_cancel_queued: bool = True,
        play_timeout_s: float = PLAY_TIMEOUT_S,
        idle_timeout_s: float = IDLE_TIMEOUT_S,
        text_timeout_s: float = TEXT_TIMEOUT_S,
        think_hook_timeout_s: float | None = THINK_HOOK_TIMEOUT_S,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.think = think_stage
        self.speak = speak_stage
        self.tracer = tracer or LatencyTracer()
        self.policy_source: PolicySource = policy_source or StaticPolicySource()
        self._barge: BargeInPolicy = barge_in or AccumulatingBargeIn(self.policy_source)
        self._yield: YieldPolicy = yield_policy or NeverYield()
        self._admission: AdmissionPolicy = admission or AcceptAllAdmission()
        self._on_turn_ended = on_turn_ended
        self._supersede_same_owner = supersede_same_owner
        self._cascade = cascade_cancel_queued
        self._play_timeout_s = play_timeout_s
        self._idle_timeout_s = idle_timeout_s
        self._text_timeout_s = text_timeout_s
        self._think_hook_timeout_s = think_hook_timeout_s
        self._clock = clock

        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_bound = False
        self._bind_lock = threading.Lock()
        """True = 宿主以 :meth:`bind_loop` 明確綁定；submit 的 loop 必須與它一致。"""

        self._loop_thread: int | None = None
        self._closed = False
        """:meth:`aclose` 之後為 True：不再接新輪，跨執行緒入口一律 no-op。"""

        self._live: list[TurnHandle] = []
        self._bg_tasks: set[asyncio.Future] = set()
        """非同步 ``on_generation_done`` 的強參照（避免被 GC 掉）。"""

        self._think_lock = asyncio.Lock()
        self._lock_users = 0
        """持有或等待 think 鎖的輪數（admission 的 busy／waiting 依此）。"""

    # ───────────────────────── 對外介面 ─────────────────────────

    def _active_lock_users(self) -> int:
        """持有或等待 think 鎖、且尚未被認領取消的輪數（已認領取消的輪即將離開鎖系統）。"""
        return sum(
            1 for t in self._live
            if t._lock_state in ("held", "waiting") and not t._cancel_claimed
        )

    @property
    def busy(self) -> bool:
        """有輪持有或等待 think 鎖（不含已認領取消、尚未離開鎖的輪）。"""
        return self._active_lock_users() > 0

    @property
    def waiting(self) -> int:
        """排隊等 think 鎖的輪數（扣掉持鎖者；同 admission 的 ``waiting`` 參數）。"""
        return max(0, self._active_lock_users() - 1)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """在第一次 ``submit`` 之前綁定 loop（任何執行緒皆可，通常在宿主啟動時呼叫一次）。

        綁定後，從其他執行緒進來的 ``on_voice_event`` / ``void`` / ``cancel_voice`` /
        ``cancel_all`` 就有 loop 可以排回去，不必等第一輪才「恰好」可用。之後 ``submit``
        必須在這個 loop 內呼叫，否則拋 ``RuntimeError``。沒有綁定時維持舊行為：第一次
        ``submit`` 才擷取 loop。重複綁同一個 loop 無害；``_loop`` 已存在（含首次 submit
        懶擷取）且與傳入的不同，拋 ``RuntimeError``。
        """
        with self._bind_lock:
            if self._loop is not None and self._loop is not loop:
                raise RuntimeError("TurnCoordinator 已綁定（或已用過）另一個 event loop")
            self._loop = loop
            self._loop_bound = True
        try:
            if asyncio.get_running_loop() is loop:
                self._loop_thread = threading.get_ident()
        except RuntimeError:
            pass  # 不在任何 loop 內（例如宿主的啟動執行緒）：loop 執行緒留給 submit 設定

    def submit(self, req: TurnRequest) -> TurnHandle:
        """送出一輪。必須在 loop 內呼叫；立即返回，不等生成。

        ticket 與 utterance 不對應、或 turn_id 與還在處理的輪重複，是呼叫端的程式錯誤，
        拋 ``ValueError``（不算 submit，也沒有 outcome）。已用 :meth:`bind_loop` 綁定時，
        在別的 loop 內呼叫拋 ``RuntimeError``。:meth:`aclose` 之後的 submit 不會出聲：
        產生 ``VOIDED``（``skipped_reason="shutdown"``）outcome，``on_turn_ended`` 照常呼叫。
        其餘情況每個 submit 恰好一個 outcome。
        """
        if not req.ticket.matches(req.utterance):
            raise ValueError("ticket 與 utterance 不對應（source_turn_id != utterance.turn_id）")
        if any(t.turn_id == req.ticket.turn_id and not t._done for t in self._live):
            raise ValueError(f"turn_id 重複: {req.ticket.turn_id}")
        if req.ticket.turn_id != req.utterance.turn_id:
            # 不改行為，但 tracer／commit 用 ticket.turn_id、think 與音訊塊用 utterance.turn_id，
            # 兩者不同會讓記憶／工具打點落到孤兒 trace
            _log.warning(
                "ticket.turn_id（%s）≠ utterance.turn_id（%s），打點可能分裂，宿主應令兩者相等",
                req.ticket.turn_id, req.utterance.turn_id,
            )
        loop = asyncio.get_running_loop()
        if self._loop_bound and loop is not self._loop:
            raise RuntimeError("submit 必須在 bind_loop 綁定的 event loop 內呼叫")
        self._loop = loop
        self._loop_thread = threading.get_ident()

        snap = self.policy_source.current()
        stale = self._is_stale(snap)
        flags = self._flags_for(snap, req.ticket.reply_mode)
        t = TurnHandle(req, flags, loop, snap.version, stale, self)

        if self._closed:
            self._deliver_closed(t)
            return t

        try:
            admitted = self._admission.admit(req, self.busy, self.waiting)
        except Exception:  # noqa: BLE001 - 閘門壞掉不該擋住回覆
            _log.warning("admission 失敗，視為接受", exc_info=True)
            admitted = True
        if not admitted:
            self._deliver_dropped(t)
            return t

        tid = t.turn_id
        self.tracer.start(tid)
        self.tracer.mark(
            tid, marks.MARK_SPEECH_END,
            at=req.speech_end_at if req.speech_end_at is not None else req.utterance.ended_at,
        )
        self.tracer.mark(tid, marks.MARK_INPUT_FINAL, at=req.stt_done_at)
        for name, at in req.marks.items():
            self.tracer.mark(tid, name, at=at)

        self._supersede_owner(t)
        queue = [
            o for o in self._live
            if not o._play_done.is_set() and o._cancel_reason is not CancelReason.SUPERSEDED
        ]
        t._predecessor = queue[-1] if queue else None

        if not flags.speak:
            t._skip_reason = SkipReason.MODE_SILENT
        else:
            t._first_sink, t._skip_reason = self._make_sink(req, t)

        t._lock_state = "waiting"
        self._lock_users += 1
        self._live.append(t)
        t._task = asyncio.ensure_future(self._run(t))
        return t

    def on_voice_event(self, ev: VoiceEvent) -> None:
        """使用者聲音事件（任何執行緒）。依序餵 barge-in 與讓位策略。

        命中 barge-in 時，**呼叫執行緒**內同步 ``sink.stop()``，token 取消經
        ``call_soon_threadsafe`` 回 loop，且每輪只取消一次。
        """
        loop = self._loop
        if loop is None or self._closed:
            return
        live = [t for t in list(self._live) if not t._done and not t._play_done.is_set()]
        if not live:
            return
        for t in live:
            if t._cancel_claimed or t._voice_claimed or not t.flags.barge_in:
                continue
            try:
                decision = self._barge.on_voiced(t, ev)
            except Exception:  # noqa: BLE001 - 策略壞掉不能斷掉音訊執行緒
                _log.debug("barge-in 策略失敗", exc_info=True)
                continue
            if decision.should_interrupt:
                self._barge_in(t)

        if self._is_stale(self.policy_source.current()):
            return  # 快照過期：讓位停用（退保守），barge-in 停播是安全方向所以照常
        try:
            actions = self._yield.on_voiced(ev, live)
        except Exception:  # noqa: BLE001
            _log.debug("讓位策略失敗", exc_info=True)
            return
        for action in actions:
            self.void(action.turn_id, action.reason)

    def void(self, turn_id: str, reason: str = REASON_YIELDED) -> bool:
        """作廢一輪（讓位）：SUPERSEDED，detail 為 ``reason``。任何執行緒皆可。

        已開播的輪不處理（回 False，交給 barge-in）；已被認領取消的輪也回 False。
        """
        if self._closed:
            return False
        for t in list(self._live):
            if t.turn_id != turn_id:
                continue
            if t._done or t._play_done.is_set() or t.audio_started:
                return False
            if not self._claim(t, CancelReason.SUPERSEDED, reason, KIND_YIELD):
                return False
            self._dispatch(t)
            return True
        return False

    def cancel_voice(self, turn_id: str, detail: str = "host") -> bool:
        """只取消單輪的語音（停播、停 TTS），think 照常生成到完、文字照常 commit。任何執行緒皆可。

        語意同 :meth:`on_voice_event` 觸發的 barge-in，但不標 ``barge_in``、不連帶取消後面
        排隊輪的語音；outcome 為 ``SKIPPED`` + ``skipped_reason=voice_cancelled``
        （``metadata["voice_cancelled"]`` = ``detail``）。成功認領回 True；輪已結束、
        已無語音可取消（沒有語音支線、語音或整輪已被取消、語音已播完）、或 ``turn_id``
        不存在回 False。

        這是宿主的明確指令，**不看 ``flags.barge_in``**：不可被使用者插話打斷的輪，
        宿主仍可用它停語音。語音已播完（drain 返回）之後不能再取消，輪照常以完成結算。
        """
        if self._closed:
            return False
        for t in list(self._live):
            if t.turn_id != turn_id:
                continue
            att = t._att
            if (att.sink if att is not None else t._first_sink) is None:
                return False
            if t._done or t._play_done.is_set():
                return False
            if not self._claim_voice(t, detail, KIND_VOICE_CANCEL):
                return False
            self._dispatch_voice(t, cascade=False)
            return True
        return False

    def cancel_turn(
        self,
        turn_id: str,
        *,
        reason_code: str = REASON_HOST_CANCELLED,
        detail: str = "",
        commit: Literal["default", "none"] = "default",
    ) -> bool:
        """取消單一輪的**整輪**（含已開播；think 也停）。任何執行緒皆可。

        宿主的明確指令（例如 Core 拒絕該輪：``global_stop``／``presence_lost``／管制）：
        呼叫執行緒同步 ``sink.stop()``，token 取消經 ``call_soon_threadsafe`` 回 loop。
        **不看 ``flags.barge_in``**，不可打斷的輪也能取消；**不連帶**取消後面排隊的輪
        （與 :meth:`cancel_voice` 一致，要取消多輪請逐輪呼叫或用 :meth:`cancel_all`）。

        outcome：``CANCELLED``（等鎖期間被取消則同其他整輪取消為 ``VOIDED``）、
        ``metadata["reason_code"]`` = ``reason_code``、``metadata["cancel_detail"]`` = ``detail``、
        ``aborted=True``，不標 ``barge_in``／``superseded``；``outcome.cancel_reason`` 為
        ``CancelReason.HOST`` 的值（``"host_cancelled"``），細項請看 ``reason_code``。
        已生成但未結束的輪，``on_generation_done`` 以 ``ok=False`` 觸發。

        **回 True 即保證** outcome 是這次的宿主取消（含認領落在「生成剛完成、結算尚未開始」的
        窗口，結算會把它改判為 ``CANCELLED``，不會以 ``COMPLETED`` 收尾）；結算一旦開始
        （``_conclude`` 取走快照）就不再接受認領，回 False。

        歷史（commit）：與 :meth:`cancel_all`／``void`` 一致，結算時**不 commit**。但 EAGER 模式下
        生成已完成的輪，完整生成文字在生成完成時就已 commit，不會自動消失：

        * ``commit="default"``：保留（宿主可自行 ``think.retract(turn_id)``）。
        * ``commit="none"``：coordinator 在結算時優先呼叫 ``think.retract(turn_id)``
          （移除使用者話語與回答，並讓之後的晚到 commit 被丟棄；SETTLED 也呼叫以防晚到），
          沒有 ``retract`` 才退回 ``settle(turn_id, "")``——後者只移除 assistant 項，
          使用者話語會留在歷史，因此仍記為撤回失敗。

        結果記在 ``outcome.metadata["history_retracted"]``：``True`` = 這一輪確定不在歷史中
        （已撤回，或本來就沒 commit 過）；``False`` = 不保證（``commit="default"`` 且 EAGER
        已 commit，或撤回失敗）。撤回失敗（回 False、拋例外、think 兩者皆無）時另有
        ``history_retract_error`` 說明並記 warning。**回 False（輪已結束）不代表歷史乾淨**：
        宿主需自行呼叫 ``think.retract``。

        ``reason_code`` 去掉前後空白後須為 1～64 字元的非空字串（值域不限）、``commit`` 須為
        ``"default"``／``"none"``，否則 ``ValueError``；**驗證先於其他檢查**，所以 coordinator
        已關閉時參數不合法仍丟 ``ValueError``。成功認領回 True；``turn_id`` 不存在、輪已結束、
        已被整輪認領取消（讓位、同 owner 取代、``cancel_all``、另一次 ``cancel_turn``）、
        或 coordinator 已關閉回 False。已被 barge-in／``cancel_voice`` 取消語音的輪仍可整輪取消。
        """
        if not isinstance(reason_code, str) or not reason_code.strip():
            raise ValueError("reason_code 必須是非空字串")
        reason_code = reason_code.strip()
        if len(reason_code) > MAX_REASON_CODE_LEN:
            raise ValueError(f"reason_code 不可超過 {MAX_REASON_CODE_LEN} 字元")
        if commit not in ("default", "none"):
            raise ValueError(f"commit 必須是 'default' 或 'none'，收到 {commit!r}")
        if self._closed:
            return False
        for t in list(self._live):
            if t.turn_id != turn_id:
                continue
            if t._done or t._play_done.is_set():
                return False
            if not self._claim_host(t, reason_code, detail, commit):
                return False
            self._dispatch(t)
            return True
        return False

    def cancel_all(self, reason: str = "shutdown") -> None:
        """取消所有進行中與排隊中的輪（SHUTDOWN）：停播、不 commit、不 settle。

        :meth:`aclose` 之後為 no-op（關閉時已取消過所有輪）。"""
        if self._closed:
            return
        self._cancel_all(reason)

    def _cancel_all(self, reason: str) -> None:
        for t in list(self._live):
            if t._done or t._play_done.is_set():
                continue
            if self._claim(t, CancelReason.SHUTDOWN, reason):
                self._dispatch(t)

    def live_turns(self) -> Sequence[TurnView]:
        """還沒結束的輪（依排隊順序）。"""
        return [t for t in list(self._live) if not t._done]

    async def join(self) -> None:
        """等目前所有輪都處理完（測試與關閉用）。"""
        while True:
            tasks = [t._task for t in list(self._live) if t._task is not None]
            if not tasks:
                return
            await asyncio.wait(tasks)  # 不用 gather：外層被取消時不該連帶取消各輪 task

    async def aclose(self) -> None:
        """關閉：拒絕新輪（之後的 submit 產生 VOIDED/shutdown）→ 取消所有輪 → 等它們結束。

        關閉是永久的：``_closed`` 不會重設，不能 reopen；要再用請建新的 coordinator。"""
        self._closed = True
        self._cancel_all("shutdown")
        await self.join()
        pending = {task for task in self._bg_tasks if not task.done()}
        if pending:
            # 非同步 on_generation_done 可能還在跑：給一點時間收尾，逾時就取消並等它結束
            await asyncio.wait(pending, timeout=BG_CALLBACK_GRACE_S)
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    # ───────────────────────── 取消機制 ─────────────────────────

    def _on_loop_thread(self) -> bool:
        return threading.get_ident() == self._loop_thread

    def _claim(
        self, t: TurnHandle, reason: CancelReason, detail: str, kind: str | None = None
    ) -> bool:
        """認領取消權：每輪只會成功一次（跨執行緒安全）。``kind`` 缺省取 ``reason.value``。"""
        with t._cancel_lock:
            if t._cancel_claimed:
                return False
            t._cancel_claimed = True
            t._cancel_reason = reason
            t._cancel_detail = detail
            t._cancel_kind = kind or reason.value
        return True

    def _claim_host(self, t: TurnHandle, reason_code: str, detail: str, commit: str) -> bool:
        """認領宿主的整輪取消（同鎖內一併記下 reason_code／commit，結算時才讀得到）。"""
        with t._cancel_lock:
            if t._cancel_claimed or t._concluding:
                return False  # 結算已取走快照：認領了也來不及反映在 outcome
            t._cancel_claimed = True
            t._cancel_reason = CancelReason.HOST
            t._cancel_detail = detail
            t._cancel_kind = KIND_HOST
            t._host_reason_code = reason_code
            t._host_commit = commit
        return True

    def _dispatch(self, t: TurnHandle, *, cascade: bool = False) -> None:
        """已認領的取消：同步停 sink，token 取消在 loop 內做。"""
        if self._on_loop_thread():
            self._cancel_in_loop(t, cascade)
            return
        att = t._att
        _stop_sink(att.sink if att is not None else t._first_sink)
        self._call_soon(self._cancel_in_loop, t, cascade)

    def _call_soon(self, fn, *args) -> None:
        """跨執行緒排回 loop；loop 已關閉時吞掉（不能把例外拋給 STT／接收執行緒）。"""
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            _log.debug("event loop 已關閉，略過跨執行緒排程", exc_info=True)

    def _cancel_in_loop(self, t: TurnHandle, cascade: bool) -> None:
        att = t._att
        _stop_sink(att.sink if att is not None else t._first_sink)
        t._root.cancel(t._cancel_reason or CancelReason.SHUTDOWN, t._cancel_detail)
        if cascade and self._cascade:
            self._cancel_queued_after(t)

    def _claim_voice(self, t: TurnHandle, detail: str, kind: str = KIND_BARGE_IN) -> bool:
        """認領語音取消權（barge-in）：每輪只成功一次，與整輪取消的認領互不影響。"""
        with t._cancel_lock:
            if t._voice_claimed or t._cancel_claimed:
                return False
            if t._voice_settled and kind in _SOFT_VOICE_KINDS:
                return False  # 語音已播完，不能再把這輪標成被取消／出錯
            t._voice_claimed = True
            t._voice_detail = detail
            t._voice_kind = kind
        return True

    def _barge_in(self, t: TurnHandle) -> None:
        """插話：呼叫執行緒同步停 sink，語音 token 取消回 loop；think 不受影響。"""
        if not self._claim_voice(t, "voice"):
            return
        self._dispatch_voice(t, cascade=True)

    def _dispatch_voice(self, t: TurnHandle, *, cascade: bool) -> None:
        """已認領的語音取消：呼叫執行緒同步停 sink，token 取消回 loop。"""
        if self._on_loop_thread():
            self._cancel_voice_in_loop(t, cascade)
            return
        att = t._att
        _stop_sink(att.sink if att is not None else t._first_sink)
        self._call_soon(self._cancel_voice_in_loop, t, cascade)

    def _cancel_voice_in_loop(self, t: TurnHandle, cascade: bool) -> None:
        att = t._att
        _stop_sink(att.sink if att is not None else t._first_sink)
        reason = (
            CancelReason.UPSTREAM_ERROR if t._voice_kind == KIND_VOICE_ERROR
            else CancelReason.BARGE_IN
        )
        t._voice_root.cancel(reason, t._voice_detail)
        if cascade and self._cascade:
            self._cancel_queued_after(t)

    def _cancel_queued_after(self, interrupted: TurnHandle) -> None:
        """正在播的輪被插話：排在後面、還沒開播的輪的語音一併取消（文字照常生成）。"""
        if interrupted not in self._live:
            return
        later = self._live[self._live.index(interrupted) + 1:]
        for o in later:
            if o._done or o._play_done.is_set() or o.audio_started:
                continue
            if not o.flags.barge_in:
                continue  # 不可打斷的輪不被連帶取消語音
            if self._claim_voice(o, REASON_CASCADE, KIND_CASCADE):
                self._cancel_voice_in_loop(o, False)

    def _supersede_owner(self, new: TurnHandle) -> None:
        if not (self._supersede_same_owner and new.flags.supersede_same_owner):
            return
        if new.owner_id is None:
            return
        for old in list(self._live):
            if (
                old._done or old._play_done.is_set()
                or old.owner_id != new.owner_id
                or old.reply_mode != new.reply_mode
                or not old.flags.supersede_same_owner
            ):
                continue
            if self._claim(old, CancelReason.SUPERSEDED, REASON_OWNER, KIND_OWNER):
                self._dispatch(old)

    # ───────────────────────── 快照／旗標 ─────────────────────────

    @staticmethod
    def _is_stale(snap: PolicySnapshot) -> bool:
        return snap.is_stale()

    @staticmethod
    def _flags_for(snap: PolicySnapshot, reply_mode: str | None) -> ModeFlags:
        try:
            return snap.flags_for(reply_mode)
        except Exception:  # noqa: BLE001
            _log.warning("ModeRules 失敗，退回預設旗標", exc_info=True)
            return ModeFlags()

    def _make_sink(self, req: TurnRequest, t: TurnHandle) -> tuple[AudioSink | None, str | None]:
        try:
            result = req.sink_factory(t)
        except Exception as exc:  # noqa: BLE001
            _log.warning("建立 sink 失敗: %s", exc)
            return None, f"sink_error: {exc}"
        if isinstance(result, str):
            return None, result
        return result, None

    # ───────────────────────── 每輪主流程 ─────────────────────────

    async def _run(self, t: TurnHandle) -> None:
        try:
            if not await self._acquire_think(t):
                self._conclude_voided(t)
                self._mark_play_done(t)
            else:
                await self._produce(t)
        except asyncio.CancelledError:
            self._fallback_outcome(t, "task_cancelled", cancelled=True)
            raise
        except Exception as exc:  # noqa: BLE001 - 單輪失敗不該拖垮其他輪
            _log.exception("TurnCoordinator 內部錯誤")
            history_meta = await self._fallback_history(t)
            self._fallback_outcome(t, f"{type(exc).__name__}: {exc}", history_meta=history_meta)
        finally:
            self._lock_done(t)
            self._resolve_gen_done(t)
            self._mark_play_done(t)
            try:
                await t._play_done.wait()
            finally:
                if t in self._live:
                    self._live.remove(t)
                forget = getattr(self._barge, "forget", None)
                if forget is not None:
                    with contextlib.suppress(Exception):
                        forget(t.turn_id)

    async def _acquire_think(self, t: TurnHandle) -> bool:
        """取得 think 鎖。等待期間被取消回 False（未開始生成）。"""
        lock = self._think_lock
        if self._lock_users == 1 and not t._root.is_cancelled:
            await lock.acquire()  # 只有自己時不會讓出 event loop
            t._lock_state = "held"
            return True
        acq = asyncio.ensure_future(lock.acquire())
        cancel = asyncio.ensure_future(t._root.wait())
        try:
            await asyncio.wait({acq, cancel}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            cancel.cancel()
            if not acq.done():
                acq.cancel()
            await asyncio.gather(acq, cancel, return_exceptions=True)
            if acq.done() and not acq.cancelled() and acq.exception() is None:
                # 就算這裡正被外層取消，已拿到的鎖也要記帳，_run 的 finally 才會放掉
                t._lock_state = "held"
        if t._lock_state != "held":
            return False
        if t._root.is_cancelled:
            self._lock_done(t)
            return False
        return True

    def _lock_done(self, t: TurnHandle) -> None:
        """離開 think 鎖系統（釋放或放棄等待），冪等。"""
        state = t._lock_state
        if state == "held":
            self._think_lock.release()
        if state in ("held", "waiting"):
            self._lock_users -= 1
        t._lock_state = "released"

    def _new_attempt(self, t: TurnHandle, utterance) -> _Attempt:
        if t._att is None:
            sink, skip = t._first_sink, t._skip_reason
        else:
            # regen：舊 sink 已 stop（stop 是永久的），每趟重新建
            sink, skip = (
                self._make_sink(t.request, t) if t.flags.speak else (None, SkipReason.MODE_SILENT)
            )
            t._skip_reason = skip
        pred = t._predecessor
        direct = pred is None or pred._play_done.is_set()
        return _Attempt(
            utterance, t._root.child(), t._voice_root.child(), sink, t._attempt_no, direct
        )

    async def _produce(self, t: TurnHandle) -> None:
        req = t.request
        tid = t.turn_id
        self.tracer.mark(tid, marks.MARK_QUEUE_ACQUIRED)
        trace = self.tracer.get(tid)
        if trace is not None:
            trace.phase = TurnPhase.THINKING

        phase = TurnPhase.DONE
        error: str | None = None
        utt = req.utterance
        att: _Attempt | None = None
        try:
            while True:
                att = self._new_attempt(t, utt)
                t._att = att
                await self._run_think_hook(t, att)
                result = await self._run_attempt(t, att)
                if result == "barged" and t._voice_kind not in _SOFT_VOICE_KINDS:
                    phase = TurnPhase.CANCELLED  # 語音被插話截斷，文字已生成完
                if result != "regen":
                    break
                _stop_sink(att.sink)
                t.regenerated += 1
                t._attempt_no += 1
                utt = att.regen_utt
        except CancelledError as exc:
            phase = TurnPhase.CANCELLED
            if self._claim(t, exc.reason, exc.detail):
                self._cancel_in_loop(t, False)
        except Exception as exc:  # noqa: BLE001 - 單一 turn 失敗不該拖垮 session
            phase = TurnPhase.FAILED
            error = f"{type(exc).__name__}: {exc}"
            if self._claim(t, CancelReason.UPSTREAM_ERROR, error):
                self._cancel_in_loop(t, False)

        assert att is not None
        if req.commit_mode is CommitMode.EAGER:
            self._lock_done(t)  # EAGER：commit 之後下一輪就能開始生成
        self._resolve_gen_done(t, phase is TurnPhase.DONE)
        self._mark_play_done(t)
        await self._conclude(t, att, phase, error)

    async def _run_think_hook(self, t: TurnHandle, att: _Attempt) -> None:
        """鎖內、這一趟生成開始前呼叫宿主 hook。

        只吞 hook 的 ``Exception``（含逾時），記到 ``metadata["on_think_start_error"]``；
        取消照常傳遞：hook 期間整輪被取消就中止並收尾（hook 被取消，不等它）。
        """
        hook = t.request.on_think_start
        if hook is None:
            return

        async def _call() -> None:
            await hook(t, att.attempt_id)

        task = asyncio.ensure_future(_call())
        # 被放生（吞掉取消）的 hook 之後才失敗也要取走例外，免得 "never retrieved" 警告
        task.add_done_callback(lambda f: f.cancelled() or f.exception())
        canceller = asyncio.ensure_future(t._root.wait())
        try:
            done, _ = await asyncio.wait(
                {task, canceller}, timeout=self._think_hook_timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            pending = {p for p in (task, canceller) if not p.done()}
            for p in pending:
                p.cancel()
            if pending:
                # 有限等待：hook 吞掉取消時放生它，不讓 think 鎖永遠不放
                await asyncio.wait(pending, timeout=THINK_HOOK_CANCEL_GRACE_S)
        if t._root.is_cancelled:
            # 走和「生成中被取消」同一條收尾路徑（認領早已完成，_produce 會直接結算 CANCELLED）
            raise CancelledError(t._root.reason or CancelReason.SHUTDOWN, t._root.detail)
        error: str | None = None
        if task not in done:
            error = "timeout"
            _log.warning(
                "on_think_start 逾時（turn %s，第 %d 趟，%ss），照常生成",
                t.turn_id, att.attempt_id, self._think_hook_timeout_s,
            )
        elif task.cancelled():
            error = "CancelledError: hook 自行取消"
            _log.warning("on_think_start 被取消（turn %s），照常生成", t.turn_id)
        elif (exc := task.exception()) is not None:
            error = f"{type(exc).__name__}: {exc}"
            _log.warning(
                "on_think_start 失敗（turn %s，第 %d 趟），照常生成: %s",
                t.turn_id, att.attempt_id, error, exc_info=exc,
            )
        if error is not None:
            t._hook_error = error

    async def _run_attempt(self, t: TurnHandle, att: _Attempt) -> str:
        """跑一趟生成與播放。回傳 ``"done"`` 或 ``"regen"``。

        think 在獨立的 pump task 裡跑到底（輸出進無界佇列），speak 從佇列拉：
        文字生成完成與 TTS 消化速度脫鉤，鎖才能在「文字完成」時釋放，
        下一輪的 LLM 不必等這一輪的合成與播放。
        """
        tid = t.turn_id
        token = att.voice
        sink = att.sink
        if sink is not None:
            token.on_cancel(lambda _r, s=sink: _stop_sink(s))
        feed = None
        chunk_stream = None
        try:
            try:
                if sink is None:
                    await self._generate(t, att, None)
                else:
                    queue: asyncio.Queue = asyncio.Queue()
                    # 取消時喚醒可能卡在 queue.get() 的 speak（token.cancel 只在 loop 內呼叫）
                    token.on_cancel(lambda _r: queue.put_nowait(_CANCELLED))
                    att.pump = asyncio.ensure_future(self._pump(t, att, queue))
                    feed = self._feed(att, queue)
                    if not att.direct:
                        att.flusher = asyncio.ensure_future(self._flush(t, att))
                    chunk_stream = self.speak.stream(feed, token)
                    chunks = chunk_stream.__aiter__()
                    while True:
                        try:
                            chunk = await self._voice_side(chunks.__anext__())
                        except StopAsyncIteration:
                            break
                        token.raise_if_cancelled()
                        self.tracer.mark(tid, marks.MARK_SPEAK_FIRST_CHUNK)
                        if att.direct:
                            await self._voice_side(self._write(t, att, chunk))
                        else:
                            att.buffer.append(chunk)
                    self.tracer.mark(tid, marks.MARK_SPEAK_LAST_CHUNK)
                    if att.pump is not None:
                        await att.pump

                if att.regen_utt is not None and not t._root.is_cancelled:
                    return "regen"
                if att.flusher is not None:
                    await self._voice_side(att.flusher)
                token.raise_if_cancelled()
                if att.wrote_any:
                    await self._voice_side(self._drain(t, att))
                with t._cancel_lock:
                    t._voice_settled = True  # 與 _claim_voice 同鎖：之後的 cancel_voice 回 False
                return "done"
            except CancelledError as exc:
                if att.regen_utt is not None and not t._root.is_cancelled:
                    return "regen"
                if att.pump_error is not None:
                    raise att.pump_error from None
                if not t._root.is_cancelled and token.is_cancelled and att.pump is not None:
                    # barge-in 只停語音：讓 think 生成到完（EAGER 在此 commit 全文、釋放鎖）
                    await att.pump
                    if att.pump_error is not None:
                        raise att.pump_error from None
                    if t._root.is_cancelled:
                        raise CancelledError(
                            t._root.reason or exc.reason, t._root.detail
                        ) from None
                    return "barged"
                raise
            except _VoiceSideError as vexc:
                return await self._voice_failed(t, att, vexc.cause)
        finally:
            await self._close_attempt(t, att, feed, chunk_stream)

    @staticmethod
    async def _voice_side(awaitable):
        """跑一個語音側呼叫；它的例外包成 :class:`_VoiceSideError`，取消與迭代結束照常傳遞。"""
        try:
            return await awaitable
        except (CancelledError, StopAsyncIteration):
            raise
        except Exception as exc:  # noqa: BLE001
            raise _VoiceSideError(exc) from exc

    async def _voice_failed(self, t: TurnHandle, att: _Attempt, exc: Exception) -> str:
        """語音側出錯：只取消語音 token，讓 think 生成到完（收尾同 barge-in）。

        think 自己的例外（``pump_error``）、沒有語音支線時的例外、整輪已被取消，都不走這條路。
        """
        if att.regen_utt is not None and not t._root.is_cancelled:
            return "regen"
        if att.pump_error is not None or att.pump is None:
            raise exc
        if t._cancel_claimed or t._root.is_cancelled:
            raise CancelledError(
                t._cancel_reason or t._root.reason or CancelReason.SHUTDOWN,
                t._cancel_detail or t._root.detail,
            ) from None
        summary = f"{type(exc).__name__}: {exc}"
        if self._claim_voice(t, summary, KIND_VOICE_ERROR):
            _log.warning(
                "語音側失敗（turn %s），只停語音、文字照常生成: %s", t.turn_id, summary,
                exc_info=exc,
            )
            t._voice_error = summary
            self._cancel_voice_in_loop(t, False)
        else:
            # 已被插話／整輪取消認領：錯誤多半是停掉之後的連帶結果，只留 log
            _log.debug("語音取消後的語音側例外: %s", summary, exc_info=exc)
        await att.pump
        if att.pump_error is not None:
            raise att.pump_error from None
        if t._root.is_cancelled:
            raise CancelledError(
                t._root.reason or CancelReason.SHUTDOWN, t._root.detail
            ) from None
        return "barged"

    async def _close_attempt(self, t: TurnHandle, att: _Attempt, feed, chunk_stream) -> None:
        root_cancelled = t._root.is_cancelled
        for stream in (chunk_stream, feed):
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()
        for task in (att.pump, att.flusher):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        worker = att.text_worker
        if worker is not None:
            if att.regen_utt is not None or root_cancelled:
                # 被丟棄的趟、整輪作廢（取代／讓位／shutdown）：還沒送出的文字不送
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            elif att.text_q is not None:
                # 正常結束與 barge-in 要把文字送完，但不在這裡等：
                # 排空放到釋放播放位置與鎖之後（見 _drain_text）
                att.text_q.put_nowait(None)

    async def _drain_text(self, t: TurnHandle, att: _Attempt) -> None:
        """等文字回呼送完剩餘句子（有逾時；整輪被作廢則放棄）。"""
        worker = att.text_worker
        if worker is None or worker.done():
            return
        canceller = asyncio.ensure_future(t._root.wait())
        try:
            done, _ = await asyncio.wait(
                {worker, canceller}, timeout=self._text_timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if worker in done:
                return
            if canceller in done and t._root.reason is CancelReason.UPSTREAM_ERROR:
                # 生成出錯不是「整輪作廢」：已生成的句子照樣送完（仍受逾時限制）
                done, _ = await asyncio.wait({worker}, timeout=self._text_timeout_s)
                if worker in done:
                    return
            if canceller not in done or t._root.reason is CancelReason.UPSTREAM_ERROR:
                _log.warning("on_sentence 逾時（%.0fs），放棄剩餘句子", self._text_timeout_s)
            worker.cancel()
        finally:
            canceller.cancel()
            await asyncio.gather(worker, canceller, return_exceptions=True)

    # --- 生成端 ---

    @staticmethod
    async def _feed(att: _Attempt, queue: asyncio.Queue):
        """speak 的輸入：從佇列拉過濾後的句子，收到結束／取消／錯誤標記就收尾。"""
        while True:
            item = await queue.get()
            if item is _END:
                return
            if item is _CANCELLED:
                raise CancelledError(
                    att.voice.reason or CancelReason.SHUTDOWN, att.voice.detail
                )
            if isinstance(item, BaseException):
                raise item
            yield item

    async def _pump(self, t: TurnHandle, att: _Attempt, queue: asyncio.Queue) -> None:
        try:
            await self._generate(t, att, queue.put_nowait)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 含管線自己的 CancelledError
            if not isinstance(exc, CancelledError):
                att.pump_error = exc
                msg = f"{type(exc).__name__}: {exc}"
                att.token.cancel(CancelReason.UPSTREAM_ERROR, msg)
                att.voice.cancel(CancelReason.UPSTREAM_ERROR, msg)
            queue.put_nowait(exc)
            return
        queue.put_nowait(_END)

    async def _generate(self, t: TurnHandle, att: _Attempt, emit) -> None:
        """跑 think 到底：打點、記帳、過濾（PASS）、文字回呼；``emit`` 收過濾後要唸的句子。"""
        req = t.request
        tid = t.turn_id
        filt = req.text_filter
        speaking = emit is not None
        t._gen_started = True
        stream = self.think.stream(att.utterance, att.token)
        try:
            async for sentence in stream:
                self.tracer.mark(tid, marks.MARK_THINK_FIRST_SENTENCE)
                att.generated[sentence.index] = sentence
                self.tracer.count_sentence(tid, sentence.split_reason)

                voiced: Sentence | None = sentence
                # PASS 偵測與要不要唸脫鉤：每句都餵 filter（PASS 後不再餵），
                # 語音已取消／不唸只影響是否 emit；PASS 只關語音，think 照常跑完
                if filt is not None and not att.passed:
                    try:
                        text = filt.feed(sentence.text)
                        passed = bool(filt.passed)
                    except Exception:  # noqa: BLE001 - 語音支線不能影響文字路徑
                        _log.warning("語音句子過濾失敗", exc_info=True)
                        text, passed = None, False
                    if passed:
                        att.passed = True
                    elif not text:
                        voiced = None
                    elif text != sentence.text:
                        voiced = dataclasses.replace(sentence, text=text)
                if att.passed or att.voice.is_cancelled:
                    voiced = None  # PASS 後／語音已被截斷：文字照常記帳，但不再餵語音支線

                if voiced is not None and voiced.text:
                    att.voiced[sentence.index] = voiced.text
                # 語音先、文字後：句子先進語音佇列，worker 之後才送文字（不延遲 TTS）
                if speaking and voiced is not None and voiced.text:
                    emit(voiced)
                self._push_text(t, att, sentence)
        finally:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()

        self.tracer.mark(tid, marks.MARK_THINK_DONE, overwrite=True)
        if (
            not att.passed
            and req.regen is not None
            and t.regenerated < req.max_regenerate
            and not t.audio_started
            and not att.token.is_cancelled
        ):
            try:
                fresh = req.regen.refresh(t)
            except Exception:  # noqa: BLE001
                _log.warning("regen 判定失敗", exc_info=True)
                fresh = None
            if fresh is not None:
                # 沿用同一 turn_id；舊趟語音由 token 取消停掉，不 commit
                att.regen_utt = dataclasses.replace(fresh, turn_id=req.utterance.turn_id)
                att.token.cancel(CancelReason.SUPERSEDED, "regenerated")
                att.voice.cancel(CancelReason.SUPERSEDED, "regenerated")
                return
        if att.token.is_cancelled:
            return  # think 被整輪取消而安靜返回：不當成正常完成（不 commit、ok=False）
        await self._finish_generation(t, att)

    async def _finish_generation(self, t: TurnHandle, att: _Attempt) -> None:
        req = t.request
        if req.commit_mode is CommitMode.EAGER and not t._eager_committed:
            generated = self._generated_text(att)
            t._eager_committed = True
            with contextlib.suppress(Exception):
                await self.think.commit(t.turn_id, generated, generated)
        if req.commit_mode is CommitMode.EAGER:
            self._lock_done(t)  # commit 之後才放鎖：下一輪的 prompt 才看得到這一輪
        self._resolve_gen_done(t, True)

    def _push_text(self, t: TurnHandle, att: _Attempt, sentence: Sentence) -> None:
        """句子交給宿主的文字回呼：丟進佇列由 worker 依序處理，不阻塞 TTS。"""
        callback = t.request.on_sentence
        if callback is None:
            return
        if att.text_q is None:
            att.text_q = asyncio.Queue()
            att.text_worker = asyncio.ensure_future(self._text_worker(t, att, callback))
            worker = att.text_worker

            def _stop_text(reason: CancelReason) -> None:
                # 整輪作廢（取代／讓位／shutdown）或 regen 丟棄：立刻停止送出剩餘句子
                if reason is not CancelReason.UPSTREAM_ERROR:
                    worker.cancel()

            att.token.on_cancel(_stop_text)
        att.text_q.put_nowait(sentence)

    @staticmethod
    async def _text_worker(t: TurnHandle, att: _Attempt, callback) -> None:
        assert att.text_q is not None
        while True:
            sentence = await att.text_q.get()
            if sentence is None:
                return
            try:
                await callback(t, sentence)
            except Exception:  # noqa: BLE001 - 文字送出失敗不影響語音
                _log.warning("on_sentence 失敗", exc_info=True)

    # --- 播放端 ---

    async def _flush(self, t: TurnHandle, att: _Attempt) -> None:
        """等前一輪播完，把緩衝的音訊塊依序寫出，然後切成直寫模式。"""
        token = att.voice
        pred = t._predecessor
        started = self._clock()
        if pred is not None and not pred._play_done.is_set():
            await self._wait_event(pred._play_done, token)
        t._queue_wait_s = self._clock() - started
        while not token.is_cancelled:
            if not att.buffer:
                att.direct = True  # 檢查與切換之間沒有 await，不會漏掉新塊
                return
            await self._write(t, att, att.buffer.popleft())

    @staticmethod
    async def _wait_event(event: asyncio.Event, token: CancellationToken) -> None:
        if event.is_set():
            return
        waiter = asyncio.ensure_future(event.wait())
        canceller = asyncio.ensure_future(token.wait())
        try:
            await asyncio.wait({waiter, canceller}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (waiter, canceller):
                task.cancel()
            await asyncio.gather(waiter, canceller, return_exceptions=True)

    async def _write(self, t: TurnHandle, att: _Attempt, chunk: AudioChunk) -> None:
        sink = att.sink
        assert sink is not None
        if not att.idle_done:
            att.idle_done = True
            await self._wait_idle(sink)
        trace = self.tracer.get(t.turn_id)
        if trace is not None:
            trace.phase = TurnPhase.SPEAKING
        duration = chunk.duration_s
        idx = chunk.sentence_index
        att.sent_audio[idx] = att.sent_audio.get(idx, 0.0) + duration  # 寫入前累加（同 bot）
        await sink.write(chunk)
        att.emitted += duration
        att.timeline.append((idx, att.emitted))
        self.tracer.count_chunk(t.turn_id, duration)
        if att.first_write_at is None:
            att.first_write_at = self._clock()
            att.wrote_any = True
            frame_at = sink_first_frame_at(sink)
            self.tracer.mark(
                t.turn_id, marks.MARK_FIRST_FRAME,
                at=frame_at if frame_at is not None else att.first_write_at,
            )

    async def _wait_idle(self, sink: AudioSink) -> None:
        """sink 選用能力：等底層播放器真的釋放。逾時放行並記 log。"""
        wait_idle = getattr(sink, "wait_idle", None)
        if wait_idle is None:
            return
        try:
            await asyncio.wait_for(wait_idle(), self._idle_timeout_s)
        except asyncio.TimeoutError:
            _log.warning("sink.wait_idle 逾時（%.2fs），放行", self._idle_timeout_s)
        except Exception:  # noqa: BLE001
            _log.warning("sink.wait_idle 失敗，放行", exc_info=True)

    async def _drain(self, t: TurnHandle, att: _Attempt) -> None:
        """等 sink 播完，但保持可被取消；逾時視為 TIMEOUT 取消。"""
        sink = att.sink
        assert sink is not None
        token = att.voice
        drain_task = asyncio.ensure_future(sink.drain())
        cancel_task = asyncio.ensure_future(token.wait())
        try:
            done, _ = await asyncio.wait(
                {drain_task, cancel_task},
                timeout=self._play_timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                _log.warning("播放逾時（%.0fs），強制停止", self._play_timeout_s)
                if self._claim(t, CancelReason.TIMEOUT, "play_timeout"):
                    self._cancel_in_loop(t, False)
            # 播放完成與取消可能同時到達，取消優先；播放失敗也要往上拋成 FAILED
            token.raise_if_cancelled()
            drain_task.result()
        finally:
            for task in (drain_task, cancel_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(drain_task, cancel_task, return_exceptions=True)

    # --- 結算 ---

    @staticmethod
    def _generated_text(att: _Attempt) -> str:
        return "".join(
            att.generated[i].text for i in sorted(att.generated) if att.generated[i].text
        )

    @staticmethod
    async def _confirm_stopped(sink: AudioSink | None) -> None:
        confirm = getattr(sink, "confirm_stopped", None)
        if confirm is None:
            return
        with contextlib.suppress(Exception):
            await confirm()

    def _resolve_spoken(
        self, t: TurnHandle, att: _Attempt, cut_short: bool
    ) -> tuple[str, float, bool, list]:
        """依 SpokenRule 推算實際聽到的內容。回傳 ``(spoken, 秒數, 是否確知, spans)``。"""
        sink = att.sink
        evidence = sink_played_evidence(sink) if sink is not None else PlayedEvidence.TRANSPORT_SENT
        rule = resolve_spoken_rule(t.request.spoken_rule, evidence)
        played: float | None = None
        if sink is not None:
            with contextlib.suppress(Exception):
                played = float(sink.played_duration_s)
        spans = build_sentence_spans(att.timeline, att.voiced)
        if rule is SpokenRule.PARTIAL:
            spoken, duration = partial_spoken(att.voiced, att.timeline, att.emitted, played)
            return spoken, duration, True, spans
        text = whole_sentence_spoken(att.voiced, att.sent_audio, played, cut_short)
        duration = 0.0 if played is None else max(0.0, min(att.emitted, played))
        return (text or ""), duration, text is not None, spans

    async def _conclude(
        self, t: TurnHandle, att: _Attempt, phase: TurnPhase, error: str | None
    ) -> None:
        req = t.request
        tid = t.turn_id
        sink = att.sink
        with t._cancel_lock:
            # 取消快照與「停止接受宿主認領」在同一把鎖內：cancel_turn 回 True 的輪一定在這裡被看見
            t._concluding = True
            host = t._cancel_claimed and t._cancel_kind == KIND_HOST
        if host:
            phase = TurnPhase.CANCELLED  # 認領落在 DONE 決定之後：一律視為整輪取消
        cancelled = phase is TurnPhase.CANCELLED
        # 語音取消的狀態在開頭快照一次：skipped 與 metadata 都用快照，避免 await 期間前後不一致
        voice_kind = t._voice_kind if t._voice_claimed else None
        voice_detail = t._voice_detail
        voice_error = t._voice_error
        reason: CancelReason | None = None
        detail = ""
        kind: str | None = None
        if cancelled:
            if t._cancel_claimed:
                reason, detail, kind = t._cancel_reason, t._cancel_detail, t._cancel_kind
            elif t._voice_claimed:
                reason, detail, kind = CancelReason.BARGE_IN, t._voice_detail, t._voice_kind
        barge_in = reason is CancelReason.BARGE_IN
        superseded = cancelled and kind == KIND_OWNER
        aborted = cancelled and not (barge_in and kind != KIND_CASCADE)
        voice_soft = phase is TurnPhase.DONE and voice_kind in _SOFT_VOICE_KINDS
        cut_short = barge_in or superseded or aborted or bool(error) or voice_soft

        # 被停掉的 sink 可能還有「最後實播位置」在回報路上：先等它確認再推算 spoken
        if (phase is not TurnPhase.DONE or voice_soft) and sink is not None:
            await self._confirm_stopped(sink)

        spoken, spoken_s, known, spans = self._resolve_spoken(t, att, cut_short)
        generated = self._generated_text(att)

        # --- commit／settle：嚴格排在確認實播量之後（Pipecat #4111）---
        no_history = reason in (
            CancelReason.SHUTDOWN, CancelReason.SUPERSEDED, CancelReason.HOST
        )
        settle = getattr(self.think, "settle", None)
        history_meta: dict = {}
        if no_history:
            if host:
                if t._host_commit == "none":
                    history_meta = await self._retract_history(t)
                else:
                    history_meta = {"history_retracted": not t._eager_committed}
            elif (
                reason is CancelReason.SUPERSEDED
                and req.retract_on_yield
                and t._eager_committed
                and settle is not None
            ):
                with contextlib.suppress(Exception):
                    await settle(tid, "", generated)
        elif req.commit_mode is CommitMode.SETTLED:
            with contextlib.suppress(Exception):
                await self.think.commit(tid, spoken, generated)
        elif (
            req.history_spoken_only and t._eager_committed and settle is not None
            and known and att.wrote_any  # 未出聲或進度未知時不改寫，免得把整輪 assistant 項刪掉
        ):
            with contextlib.suppress(Exception):
                await settle(tid, spoken, generated)

        # --- 狀態與跳過原因 ---
        skipped: str | None = None
        if phase is TurnPhase.DONE:
            if att.passed:
                skipped = SkipReason.PASS  # 模型的 PASS 優先於 mode_silent 等靜態原因
            elif voice_soft:
                skipped = (
                    SkipReason.VOICE_ERROR if voice_kind == KIND_VOICE_ERROR
                    else SkipReason.VOICE_CANCELLED
                )
            elif t._skip_reason:
                skipped = t._skip_reason
            elif sink is not None:
                play_error = getattr(sink, "play_error", None)
                if not att.voiced:
                    skipped = SkipReason.NO_SPOKEN_TEXT
                elif not att.wrote_any or (play_error and not t.audio_started):
                    skipped = f"play_failed: {play_error}" if play_error else SkipReason.NO_AUDIO
        elif barge_in and att.passed:
            # 語音已被插話截斷但模型仍 PASS：status 維持 CANCELLED／barge_in，PASS 只當資訊旗標
            skipped = SkipReason.PASS
        if phase is TurnPhase.FAILED:
            status = OutcomeStatus.FAILED
        elif cancelled:
            status = OutcomeStatus.CANCELLED
        elif skipped is not None:
            status = OutcomeStatus.SKIPPED
        else:
            status = OutcomeStatus.COMPLETED

        frame_at = sink_first_frame_at(sink) if sink is not None else None
        if frame_at is not None:
            self.tracer.mark(tid, marks.MARK_FIRST_FRAME, at=frame_at, overwrite=True)
        trace = self.tracer.get(tid)
        if trace is not None:
            trace.spoken_duration_s = spoken_s
        cancel_value = reason.value if reason is not None else None
        self.tracer.finish(tid, phase, cancel_value, error)

        meta = self._outcome_meta(t, reason, kind, detail)
        meta.update(history_meta)
        if phase is TurnPhase.FAILED:
            meta.setdefault("reason_code", CancelReason.UPSTREAM_ERROR.value)
        if t._hook_error is not None:
            meta["on_think_start_error"] = t._hook_error
        if voice_error is not None:
            meta["voice_error"] = voice_error
        if voice_kind == KIND_VOICE_CANCEL:
            meta["voice_cancelled"] = voice_detail
        if phase is not TurnPhase.DONE and t._skip_reason:
            meta["sink_skip_reason"] = t._skip_reason  # 取消／失敗時仍保留語音支線的靜態原因
        outcome = TurnOutcome(
            turn_id=tid,
            phase=phase,
            utterance_text=req.utterance.text,
            generated_text=generated,
            spoken_text=spoken,
            spoken_duration_s=spoken_s,
            cancel_reason=cancel_value,
            error=error,
            metadata=meta,
            skipped_reason=skipped,
            cut_short=cut_short,
            audio_started=t.audio_started,
            first_audio_at=frame_at if frame_at is not None else att.first_write_at,
            sentence_spans=spans,
            status=status,
            spoken_known=known,
            barge_in=barge_in,
            superseded=superseded,
            aborted=aborted,
            queue_wait_s=t._queue_wait_s,
            regenerated=t.regenerated,
            attempt_id=t._attempt_no,
            snapshot_version=t._snapshot_version,
        )
        self._lock_done(t)  # SETTLED：commit 完才放鎖（播放位置早已讓出）
        await self._drain_text(t, att)
        self._deliver(t, outcome)

    async def _retract_history(self, t: TurnHandle) -> dict:
        """``commit="none"``：把這一輪整輪撤出歷史，回傳要併進 outcome.metadata 的結果。"""
        tid = t.turn_id
        retract = getattr(self.think, "retract", None)
        settle = getattr(self.think, "settle", None)
        ok = False
        error: str | None = None
        try:
            if retract is not None:
                ok = bool(await retract(tid))
                if not ok:
                    error = "retract 回傳 False"
            elif not t._eager_committed:
                ok = True  # 沒有 retract，但這一輪也沒 commit 過
            elif settle is not None:
                await settle(tid, "")
                error = "think 沒有 retract，只能以 settle 移除 assistant 項；使用者話語仍在歷史"
            else:
                error = "think 不支援 retract／settle"
        except Exception as exc:  # noqa: BLE001 - 撤回失敗不影響 outcome，記錄即可
            error = f"{type(exc).__name__}: {exc}"
        if error is not None:
            _log.warning("撤回歷史失敗（turn %s）：%s", tid, error)
            return {"history_retracted": False, "history_retract_error": error}
        return {"history_retracted": ok}

    # --- outcome 出口 ---

    @staticmethod
    def _outcome_meta(
        t: TurnHandle, reason: CancelReason | None, kind: str | None, detail: str
    ) -> dict:
        meta: dict = {}
        if reason is not None:
            if kind == KIND_HOST and t._host_reason_code:
                code = t._host_reason_code
            elif reason is CancelReason.SUPERSEDED:
                code = REASON_YIELDED if kind == KIND_YIELD else "superseded"
            else:
                code = reason.value
            meta["reason_code"] = code
            meta["cancel_detail"] = detail
            if kind == KIND_CASCADE:
                meta["cascade"] = True
        if t._stale:
            meta["stale_snapshot"] = True
        return meta

    def _deliver(self, t: TurnHandle, outcome: TurnOutcome) -> None:
        """送出這一輪唯一的 outcome（冪等：第二次呼叫無效）。"""
        if t._done:
            return
        t._done = True
        if not t.settled.done():
            t.settled.set_result(outcome)
        callback = self._on_turn_ended
        if callback is not None:
            try:
                callback(outcome)
            except Exception:  # noqa: BLE001
                _log.warning("on_turn_ended 失敗", exc_info=True)

    def _deliver_dropped(self, t: TurnHandle) -> None:
        self._resolve_gen_done(t)
        t._play_done.set()
        self._deliver(t, TurnOutcome(
            turn_id=t.turn_id,
            phase=TurnPhase.CANCELLED,
            utterance_text=t.request.utterance.text,
            skipped_reason=SkipReason.DROPPED_BUSY,
            status=OutcomeStatus.DROPPED_BUSY,
            snapshot_version=t._snapshot_version,
        ))

    def _deliver_closed(self, t: TurnHandle) -> None:
        """aclose 之後的 submit：不建 sink、不生成、不出聲，只給一個 VOIDED outcome。"""
        self._resolve_gen_done(t)
        t._play_done.set()
        self._deliver(t, TurnOutcome(
            turn_id=t.turn_id,
            phase=TurnPhase.CANCELLED,
            utterance_text=t.request.utterance.text,
            cancel_reason=CancelReason.SHUTDOWN.value,
            metadata={"reason_code": CancelReason.SHUTDOWN.value, "cancel_detail": "closed"},
            skipped_reason=SkipReason.SHUTDOWN,
            cut_short=True,
            status=OutcomeStatus.VOIDED,
            aborted=True,
            snapshot_version=t._snapshot_version,
        ))

    def _conclude_voided(self, t: TurnHandle) -> None:
        """等鎖期間被作廢：沒有生成、沒有播放、不 commit。"""
        reason = t._cancel_reason or CancelReason.SHUTDOWN
        detail = t._cancel_detail
        kind = t._cancel_kind
        barge_in = reason is CancelReason.BARGE_IN
        _stop_sink(t._first_sink)
        meta = self._outcome_meta(t, reason, kind, detail)
        if kind == KIND_HOST:
            meta["history_retracted"] = True  # 還沒生成過：這一輪從沒進過歷史
        if t._skip_reason:
            meta["sink_skip_reason"] = t._skip_reason
        self.tracer.finish(t.turn_id, TurnPhase.CANCELLED, reason.value, None)
        self._deliver(t, TurnOutcome(
            turn_id=t.turn_id,
            phase=TurnPhase.CANCELLED,
            utterance_text=t.request.utterance.text,
            cancel_reason=reason.value,
            metadata=meta,
            skipped_reason=SkipReason.VOIDED,
            cut_short=True,
            status=OutcomeStatus.VOIDED,
            barge_in=barge_in,
            superseded=kind == KIND_OWNER,
            aborted=not (barge_in and kind != KIND_CASCADE),
            snapshot_version=t._snapshot_version,
        ))

    async def _fallback_history(self, t: TurnHandle) -> dict:
        """保底路徑的歷史處理：宿主取消且 ``commit="none"`` 時補做撤回（例外吞掉）。"""
        if t._cancel_kind != KIND_HOST:
            return {}
        if t._host_commit != "none":
            return {"history_retracted": not t._eager_committed}
        try:
            return await self._retract_history(t)
        except Exception as exc:  # noqa: BLE001
            return {
                "history_retracted": False,
                "history_retract_error": f"{type(exc).__name__}: {exc}",
            }

    @staticmethod
    def _fallback_meta(t: TurnHandle, cancelled: bool, history_meta: dict | None = None) -> dict:
        if t._cancel_kind == KIND_HOST and t._host_reason_code:
            meta = {"reason_code": t._host_reason_code, "cancel_detail": t._cancel_detail}
            # 沒機會撤回時給保守值：EAGER 已 commit 就不保證歷史乾淨
            meta["history_retracted"] = not t._eager_committed
            meta.update(history_meta or {})
            return meta
        return {
            "reason_code": (
                CancelReason.SHUTDOWN.value if cancelled else CancelReason.UPSTREAM_ERROR.value
            ),
        }

    def _fallback_outcome(
        self, t: TurnHandle, error: str, *, cancelled: bool = False,
        history_meta: dict | None = None,
    ) -> None:
        """內部錯誤或 task 被外部取消時的保底：確保每個 submit 都有 outcome。"""
        if t._done:
            return
        self._resolve_gen_done(t, False)  # 生成若已開始但未結束，同步回呼仍早於 outcome
        self.tracer.finish(
            t.turn_id, TurnPhase.CANCELLED if cancelled else TurnPhase.FAILED, None, error
        )
        self._deliver(t, TurnOutcome(
            turn_id=t.turn_id,
            phase=TurnPhase.CANCELLED if cancelled else TurnPhase.FAILED,
            utterance_text=t.request.utterance.text,
            error=error,
            metadata=self._fallback_meta(t, cancelled, history_meta),
            cut_short=True,
            status=OutcomeStatus.CANCELLED if cancelled else OutcomeStatus.FAILED,
            aborted=cancelled,
            snapshot_version=t._snapshot_version,
        ))

    def _resolve_gen_done(self, t: TurnHandle, ok: bool = False) -> None:
        """生成結束（冪等，先到先贏）：resolve future，並對有生成的輪呼叫一次宿主回呼。"""
        if t.generation_done.done():
            return
        t.generation_done.set_result(None)
        callback = t.request.on_generation_done
        if callback is None or not t._gen_started:
            return
        try:
            result = callback(t, ok)
        except Exception:  # noqa: BLE001 - 宿主回呼壞掉不影響 outcome
            _log.warning("on_generation_done 失敗（turn %s）", t.turn_id, exc_info=True)
            return
        if inspect.isawaitable(result):
            task = asyncio.ensure_future(self._await_generation_cb(t, result))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

    @staticmethod
    async def _await_generation_cb(t: TurnHandle, awaitable) -> None:
        try:
            await awaitable
        except Exception:  # noqa: BLE001
            _log.warning("on_generation_done 失敗（turn %s）", t.turn_id, exc_info=True)

    def _mark_play_done(self, t: TurnHandle) -> None:
        """這一輪的播放位置讓出來。必須等前一輪也結束：被取消的排隊輪
        若搶先結束，會讓更後面的輪在前一輪還在播時開播而疊音。"""
        if t._play_done.is_set():
            return
        pred = t._predecessor
        if pred is None or pred._play_done.is_set():
            t._play_done.set()
        elif not t._chaining:
            t._chaining = True
            asyncio.ensure_future(self._chain(t, pred))

    @staticmethod
    async def _chain(t: TurnHandle, pred: TurnHandle) -> None:
        await pred._play_done.wait()
        t._play_done.set()


__all__ = [
    "IDLE_TIMEOUT_S",
    "PLAY_TIMEOUT_S",
    "TurnCoordinator",
    "TurnHandle",
]
