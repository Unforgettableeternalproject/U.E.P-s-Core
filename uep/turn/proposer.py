"""決策插槽（P3接入方案 §4）：TurnProposer Port、選用的回饋 Port、驗證與逾時防護。

``TurnProposer.propose`` 是唯一必要的方法；其餘（``OutcomeObserver``、``TickingProposer``、
``ListenerAware``）都是選用 Port，Orchestrator 以 ``isinstance``（runtime_checkable）探測，
所以換一個只實作 ``propose`` 的 proposer 不需要改 Core 其他程式（Orchestrator 以 ``getattr``／``isinstance`` 探測）。預設規則實作見 ``rule_proposer.py``，
以 ``proposer_id`` 註冊（``registry.py``）。

Proposal 一律視為不受信資料：``validate_proposal`` 只接受 schema 內的列舉與安全字元集；逾時、例外、
驗證失敗或被 CC-7 拒絕時，Orchestrator 以 ``fallback_proposal`` 回到 D80 的預設（被點名或單人 -> respond，
多人未被點名 -> passive）。
"""
from __future__ import annotations

import logging
import queue
import re
import threading
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from uep.state.coordinator import InteractionState
from uep.turn.models import Proposal, ReplyMode, TurnCandidate, TurnOutcomeFacts

logger = logging.getLogger("uep.turn.proposer")

DECISION_ACTOR = "core.decision"  # CC-7 的提議者身分（DEFAULT_PROPOSERS[Interaction]）
ROUTES = frozenset({"direct_chat", "agent_loop", "task_request", "ignore"})
_SAFE_CODE = re.compile(r"[A-Za-z0-9_.:\-]{1,64}")
# 決策插槽目前只允許請求 LISTENING（進入旁聽觀察）；其餘 Interaction 轉換歸 Orchestrator（core.turn）
_INTERACTION_VALUES = frozenset({InteractionState.LISTENING.value})


class TurnProposer(Protocol):
    def propose(self, candidate: TurnCandidate) -> Proposal: ...


@runtime_checkable
class OutcomeObserver(Protocol):
    """選用：接收回合的開始與結果（bot 的 ``on_bot_response_started／responded／passed／failed``）。

    Q-1：插話後的冷卻放在 proposer，讀 ``facts.barge_in`` 判斷，不放在 ES。
    """

    def on_turn_started(self, turn_id: str, candidate_id: str, speakers: tuple[str, ...]) -> None: ...

    def on_turn_outcome(self, facts: TurnOutcomeFacts) -> None: ...


@dataclass(frozen=True)
class Interjection:
    """沒有新 utterance 觸發的決策（被動模式插話）：以 buffer 最後一句為代表。"""

    speaker_ref: str
    text: str = field(repr=False)
    utterance_refs: tuple[str, ...]
    proposal: Proposal
    speakers: tuple[str, ...] = ()  # buffer 內的所有說話者；插話後每個人都開 context window（bot 行為）


@runtime_checkable
class TickingProposer(Protocol):
    """選用：宿主定期呼叫 ``Orchestrator.tick``，轉給 proposer 的 ``tick``（插話與沉默觸發）。"""

    def tick(self) -> list[Interjection]: ...


@runtime_checkable
class AudienceAware(Protocol):
    """選用：audience 切換時清掉所有進行中的狀態（buffer、context window、生成中標記）。"""

    def on_audience_changed(self) -> None: ...


@runtime_checkable
class ParticipantAware(Protocol):
    """選用：成員離場時清掉他留在 buffer 的文字與窗口。"""

    def on_participant_left(self, speaker_ref: str) -> None: ...


@runtime_checkable
class ListenerAware(Protocol):
    """選用：這句新文字是否算「對方還在講」（主動模式下有意義的內容），讓位用。"""

    def is_listener_continuation(self, text: str) -> bool: ...


class RespondProposer:
    """S3 最小 proposer：被送進來的每一句都回應。"""

    proposer_id = "respond.minimal"
    version = "s3-respond"

    def propose(self, candidate: TurnCandidate) -> Proposal:
        return Proposal(
            reply_mode=ReplyMode.RESPOND, reason_code="default_respond",
            proposer_id=self.proposer_id, proposer_version=self.version,
        )


def fallback_proposal(candidate: TurnCandidate, reason_code: str) -> Proposal:
    """D80 預設：被點名或單人頻道 -> respond；多人頻道且未被點名 -> passive。人數未知視為單人。"""
    solo = candidate.participant_count is None or candidate.participant_count <= 1
    mode = ReplyMode.RESPOND if (candidate.addressed_hint or solo) else ReplyMode.PASSIVE
    return Proposal(
        reply_mode=mode, addressed=candidate.addressed_hint, confidence=0.0, reason_code=reason_code,
        proposer_id="core.fallback", proposer_version="d80",
    )


def validate_proposal(proposal: object) -> str | None:
    """回傳違規原因碼；None = 通過。只接受 schema 內的列舉與安全字元集（模型輸出視為不受信資料）。"""
    if not isinstance(proposal, Proposal):
        return "not_a_proposal"
    if not isinstance(proposal.reply_mode, ReplyMode):
        return "reply_mode"
    if proposal.route not in ROUTES:
        return "route"
    if proposal.interaction_to is not None and proposal.interaction_to not in _INTERACTION_VALUES:
        # Behavior 軸的值（WORK、MISCHIEF…）也在這裡被擋：決策插槽不開 Behavior 軸
        return "interaction_to"
    if not isinstance(proposal.addressed, bool):
        return "addressed"
    confidence = proposal.confidence
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0:
        return "confidence"
    for value in (proposal.reason_code, proposal.proposer_id, proposal.proposer_version):
        if not isinstance(value, str) or not _SAFE_CODE.fullmatch(value):
            return "identifier"
    return None


class ProposerGuard:
    """在自管的 daemon 執行緒上執行 proposer，呼叫端以預算等待；逾時就放棄結果。

    proposer 在 Core 端執行，不在 ES 回呼內阻塞；單一執行緒同時序列化有狀態 proposer 的呼叫。
    卡住的執行緒無法終止，因此：上一個呼叫還在跑（``busy``）時新呼叫**不入佇列**，立即回 busy
    （佇列因此有界、卡住期間每句不必等滿預算）；執行緒是 daemon，行程結束與 ``close`` 都不會被它拖住。
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._lock = threading.Lock()
        self._busy = False
        self._closed = False
        self._queue: queue.SimpleQueue[tuple[Callable[[Any], Any], Any, Future[Any]] | None] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        return self._busy

    def submit(self, fn: Callable[[Any], Any], arg: Any) -> Future[Any] | None:
        """入佇列並回 Future；guard 已關閉或上一個呼叫還在跑時回 None（不入佇列）。"""
        with self._lock:
            if self._closed or self._busy:
                return None
            self._busy = True
            future: Future[Any] = Future()
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name=f"uep-proposer-{self._name}", daemon=True)
                self._thread.start()
            self._queue.put((fn, arg, future))
            return future

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            fn, arg, future = item
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(fn(arg))
                    except BaseException as exc:  # noqa: BLE001 - 任何例外都交回呼叫端
                        future.set_exception(exc)
            finally:
                with self._lock:
                    self._busy = False

    def call(self, fn: Callable[[Any], Any], arg: Any, budget_s: float | None) -> tuple[Any, str]:
        """回傳 ``(結果, 狀態)``；狀態為 ok／timeout／busy／error，非 ok 時結果為 None。"""
        future = self.submit(fn, arg)
        if future is None:
            return None, "error" if self._closed else "busy"
        try:
            return future.result(timeout=budget_s), "ok"
        except FutureTimeout:
            future.cancel()  # 還在排隊就能取消；已在執行的無法終止，結果被丟棄
            return None, "timeout"
        except Exception:
            logger.exception("TurnProposer 失敗")
            return None, "error"

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._queue.put(None)  # 不 join：卡住的執行緒是 daemon，不拖住關閉


class ProposerRegistry:
    """以 ``proposer_id`` 註冊 proposer 工廠，設定選用；Orchestrator 不依賴任何具體實作。"""

    def __init__(self) -> None:
        self._factories: dict[str, Callable[..., TurnProposer]] = {}

    def register(self, proposer_id: str, factory: Callable[..., TurnProposer], *, replace: bool = False) -> None:
        if not _SAFE_CODE.fullmatch(proposer_id):
            raise ValueError(f"proposer_id 含不安全字元：{proposer_id!r}")
        if proposer_id in self._factories and not replace:
            raise ValueError(f"proposer_id 已註冊：{proposer_id}")
        self._factories[proposer_id] = factory

    def create(self, proposer_id: str, **kwargs: Any) -> TurnProposer:
        try:
            factory = self._factories[proposer_id]
        except KeyError:
            raise KeyError(f"未註冊的 proposer_id：{proposer_id}") from None
        return factory(**kwargs)

    def ids(self) -> list[str]:
        return sorted(self._factories)
