"""``presence.absent_intent`` 決策點（V18 後續）：判斷缺席端輸入是否為「到場／移轉請求」。

裁決（房內 seq 266、267）：
- 缺席端的 lazy 輸入不在各平台處理；原樣轉到核心，由核心判斷意圖。意圖可用決策模型（Laya 路線），模型到位前用
  rules 版 proposer（``rules.absent_intent``），proposer 經 ``AbsentIntentProposerRegistry`` 替換。
- 本決策點**只產生意圖**。授權只來自已認證 actor 與規則（提案 §18.3）：proposal 型別沒有任何授權欄位，engine 也不讀
  ``actor_*``／``is_owner`` 以外的輸入去影響結果，輸出不得被當成授權、仲裁或回條依據。一律 shadow：結果只經決策日誌
  觀察者落盤，不觸發移轉。
- 原文只存在於 ``DecisionRequest.native``（``AbsentIntentInput.text``，``repr=False``，僅記憶體）；``facts``、
  決策日誌、事件 payload 都不含原文。判斷時不開 turn、不載入人格輸出。
- 預算沿用 ``ProposerGuard`` 的規則（decision 層不 import ``uep.turn``，故本模組有同語意的私有 guard）：
  逾時／busy／例外／輸出驗證失敗一律回 fallback（``fallback=True``），呼叫端映射為「未判斷」。
"""
from __future__ import annotations

import logging
import math
import re
import threading
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from uep.decision.contracts import (
    DecisionEngineInfo,
    DecisionPlacement,
    DecisionPlan,
    DecisionRequest,
    DecisionResult,
)
from uep.decision.registry import DecisionPointSpec
from uep.ids import new_id

logger = logging.getLogger("uep.decision.presence_absent_intent")

KIND_PRESENCE_ABSENT_INTENT = "presence.absent_intent"
INTENT_PRESENCE_REQUEST = "presence_request"
INTENT_MESSAGE = "message"
INTENT_OTHER = "other"
INTENTS = (INTENT_PRESENCE_REQUEST, INTENT_MESSAGE, INTENT_OTHER)
TARGET_LOCATIONS = ("desktop", "discord", "website", "host")

ON_TIMEOUT_REASON = "absent_intent_timeout"
ON_BUSY_REASON = "absent_intent_busy"
ON_ERROR_REASON = "absent_intent_error"
ON_INVALID_REASON = "absent_intent_invalid"
NO_INPUT_REASON = "absent_intent_no_input"
DEFAULT_BUDGET_MS = 300

RULES_PROPOSER_ID = "rules.absent_intent"
RULES_PROPOSER_VERSION = "1"
_SAFE_CODE = re.compile(r"^[a-z0-9_.:-]{1,64}$")


def presence_absent_intent_spec(budget_ms: int = DEFAULT_BUDGET_MS) -> DecisionPointSpec:
    """``presence.absent_intent`` 規格：sync、shadow、接受 native（文字只在記憶體）。"""
    return DecisionPointSpec(
        kind=KIND_PRESENCE_ABSENT_INTENT, mode="sync", owner="presence", outcomes=INTENTS, budget_ms=budget_ms,
        on_timeout_reason=ON_TIMEOUT_REASON, on_busy_reason=ON_BUSY_REASON, on_error_reason=ON_ERROR_REASON,
        on_invalid_reason=ON_INVALID_REASON, shadow=True, allow_native=True,
    )


_ACTOR_REF = re.compile(r"^[a-z0-9_.:-]{1,128}$")


@dataclass(frozen=True)
class AbsentIntentInput:
    """與輸入型別無關的中性輸入結構（V18 的 ``UtteranceFact`` 後備路徑與工具層的 ``AbsentInboundItem`` 都轉成它）。

    欄位命名與語意對齊平台端點契約的 ``AbsentInboundItem``：``source_platform``（只描述來源，不代表她在場）、
    ``surface_ref``、``actor_ref``（平台認證 ID 形式，不收顯示名稱）、``actor_authenticated``、``is_owner``
    （未認證者不可為真）。``text`` 僅記憶體（``repr=False``），不是 item 的欄位，由呼叫端依 ``raw_ref`` 取回後帶入。
    這些 actor 欄位只供 proposer 當特徵；它們不是授權，proposer 的輸出也不得改變授權。
    """

    text: str = field(repr=False)
    source_platform: str | None = None
    surface_ref: str | None = None
    actor_ref: str | None = None
    actor_authenticated: bool = False
    is_owner: bool = False

    def __post_init__(self) -> None:
        if self.actor_ref is not None and (not isinstance(self.actor_ref, str) or _ACTOR_REF.fullmatch(self.actor_ref) is None):
            raise ValueError("actor_ref 必須是平台認證 ID 形式（小寫英數與 _ . : -），不可是顯示名稱")
        if self.is_owner and not self.actor_authenticated:
            raise ValueError("未認證的 actor 不可為 owner")


class AbsentInboundLike(Protocol):
    """``AbsentInboundItem`` 的結構型別（鴨子型別，不 import 該型別：工具層鎖住 E5 前沒有消費者）。"""

    source_platform: str
    surface_ref: str
    actor_ref: str
    actor_authenticated: bool
    is_owner: bool


def absent_intent_input_from_item(item: AbsentInboundLike, text: str) -> AbsentIntentInput:
    """``AbsentInboundItem`` -> 中性輸入：只轉換欄位，不接任何通道。

    item 不含原文（只有 ``raw_ref`` blob 引用與長度），``text`` 由呼叫端取回後只在記憶體內帶入。
    """
    return AbsentIntentInput(
        text=text, source_platform=item.source_platform, surface_ref=item.surface_ref, actor_ref=item.actor_ref,
        actor_authenticated=item.actor_authenticated, is_owner=item.is_owner,
    )


@dataclass(frozen=True)
class AbsentIntentProposal:
    """proposer 的輸出：只有意圖、選填目標場域與信心；刻意沒有任何授權相關欄位。"""

    intent: str
    confidence: float
    target_location: str | None = None
    proposer_id: str = "unknown"
    proposer_version: str = "unknown"


@runtime_checkable
class AbsentIntentProposer(Protocol):
    proposer_id: str
    proposer_version: str

    def propose(self, inp: AbsentIntentInput) -> AbsentIntentProposal: ...


def validate_proposal(proposal: object) -> str | None:
    """回傳無效原因（英文碼）；有效回 None。proposer 輸出不受信。"""
    if not isinstance(proposal, AbsentIntentProposal):
        return "not_a_proposal"
    if proposal.intent not in INTENTS:
        return "bad_intent"
    if proposal.target_location is not None and proposal.target_location not in TARGET_LOCATIONS:
        return "bad_target_location"
    conf = proposal.confidence
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not math.isfinite(conf) or not 0.0 <= conf <= 1.0:
        return "bad_confidence"
    if proposal.intent != INTENT_PRESENCE_REQUEST and proposal.target_location is not None:
        return "target_without_request"
    for ident in (proposal.proposer_id, proposal.proposer_version):
        if not isinstance(ident, str) or not _SAFE_CODE.fullmatch(ident):
            return "bad_proposer_id"
    return None


# ---- rules 版 proposer ----

# 目標場域關鍵詞（較長者先比）；只在句子本身已是到場／移轉請求時才取目標
_TARGET_WORDS: tuple[tuple[str, str], ...] = (
    ("discord", "discord"), ("dc", "discord"), ("桌面", "desktop"), ("desktop", "desktop"),
    ("網站", "website"), ("網頁", "website"), ("website", "website"), ("主機", "host"), ("host", "host"),
)
# 到場／移轉的動詞片語。保守：必須是對她的祈使或請求，單純提到「過來」「主機」不算
_MOVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:到|去|來|回|移動到|移到|切換到|換到)\s*(?:discord|dc|桌面|desktop|網站|網頁|website|主機|host)"),
    re.compile(r"(?:過來|來這裡|來這邊|來我這)"),
    re.compile(r"(?:到場|現身)"),
    re.compile(r"(?:移轉|轉移|換)\s*(?:到|去|至)"),
)
# 否定與回顧句型：出現就不判為請求（「不要過來」「昨天她回主機了」）
_NEGATION = re.compile(r"(?:不要|別|不用|不必|不想|無須|無需|才不|不會)")
_PAST_OR_REPORT = re.compile(r"(?:昨天|剛剛|剛才|之前|上次|已經|曾經|是不是|有沒有|為什麼|為何|如何|怎麼|可不可能|嗎[？?]?$)")
_QUESTION_ENDING = re.compile(r"[？?]\s*$")


class RulesAbsentIntentProposer:
    """確定性的中文到場／移轉請求偵測（模型到位前的後備）。

    保守：沒有明確句型就輸出 ``other``；命中且無否定／回顧／疑問的才輸出 ``presence_request``。
    ``message`` 需要語意判斷，規則版不輸出。
    """

    proposer_id = RULES_PROPOSER_ID
    proposer_version = RULES_PROPOSER_VERSION

    def propose(self, inp: AbsentIntentInput) -> AbsentIntentProposal:
        text = (inp.text or "").strip().lower()
        if not text or len(text) > 80:  # 長文不是命令式請求
            return self._other()
        if _NEGATION.search(text) or _PAST_OR_REPORT.search(text) or _QUESTION_ENDING.search(text):
            return self._other()
        if not any(p.search(text) for p in _MOVE_PATTERNS):
            return self._other()
        target = next((loc for word, loc in _TARGET_WORDS if word in text), None)
        return AbsentIntentProposal(
            INTENT_PRESENCE_REQUEST, 0.8 if target is not None else 0.6, target,
            self.proposer_id, self.proposer_version,
        )

    def _other(self) -> AbsentIntentProposal:
        return AbsentIntentProposal(INTENT_OTHER, 0.5, None, self.proposer_id, self.proposer_version)


class AbsentIntentProposerRegistry:
    """以 ``proposer_id`` 註冊 proposer 工廠；日後換成 Laya 只需註冊並指派，engine 不依賴具體實作。"""

    def __init__(self) -> None:
        self._factories: dict[str, Callable[..., AbsentIntentProposer]] = {}

    def register(self, proposer_id: str, factory: Callable[..., AbsentIntentProposer], *, replace: bool = False) -> None:
        if not _SAFE_CODE.fullmatch(proposer_id):
            raise ValueError(f"proposer_id 含不安全字元：{proposer_id!r}")
        if proposer_id in self._factories and not replace:
            raise ValueError(f"proposer_id 已註冊：{proposer_id}")
        self._factories[proposer_id] = factory

    def create(self, proposer_id: str, **kwargs: Any) -> AbsentIntentProposer:
        try:
            factory = self._factories[proposer_id]
        except KeyError:
            raise KeyError(f"未註冊的 absent intent proposer_id：{proposer_id}") from None
        return factory(**kwargs)

    def ids(self) -> list[str]:
        return sorted(self._factories)


def default_proposer_registry() -> AbsentIntentProposerRegistry:
    registry = AbsentIntentProposerRegistry()
    registry.register(RULES_PROPOSER_ID, RulesAbsentIntentProposer)
    return registry


# ---- engine ----


class _BudgetGuard:
    """與 ``ProposerGuard`` 同語意：daemon 執行緒執行、呼叫端以預算等待、上一次還在跑立即回 busy。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = False

    def call(self, fn: Callable[[], Any], budget_s: float | None) -> tuple[Any, str]:
        with self._lock:
            if self._busy:
                return None, "busy"
            self._busy = True
        future: Future[Any] = Future()

        def run() -> None:
            try:
                result = fn()
            except BaseException as exc:  # noqa: BLE001 - 任何例外交回呼叫端
                with self._lock:
                    self._busy = False
                future.set_exception(exc)
            else:
                with self._lock:
                    self._busy = False
                future.set_result(result)

        threading.Thread(target=run, name="uep-absent-intent", daemon=True).start()
        try:
            return future.result(timeout=budget_s), "ok"
        except FutureTimeout:
            return None, "timeout"
        except Exception:
            logger.exception("absent intent proposer 失敗")
            return None, "error"


class AbsentIntentEngine:
    """``presence.absent_intent`` 的 engine：``request.native`` 的 ``AbsentIntentInput`` -> 意圖 ``DecisionResult``。

    plan 只帶 ``route="ignore"``（不觸發任何事）與 ``placement.presence_intent``／``target_platform`` 這類建議；
    不帶 reply、policy、capability，也沒有授權欄位。
    """

    def __init__(self, proposer: AbsentIntentProposer | None = None, *, spec: DecisionPointSpec | None = None) -> None:
        self.proposer: AbsentIntentProposer = proposer or RulesAbsentIntentProposer()
        self._spec = spec or presence_absent_intent_spec()
        self._guard = _BudgetGuard()

    @property
    def spec(self) -> DecisionPointSpec:
        return self._spec

    @property
    def budget_s(self) -> float | None:
        ms = self._spec.budget_ms
        return None if ms is None or ms <= 0 else ms / 1000

    def decide(self, request: DecisionRequest) -> DecisionResult:
        spec = self._spec
        inp = request.native
        if not isinstance(inp, AbsentIntentInput):
            return self._fallback(request, spec.on_invalid_reason or NO_INPUT_REASON)
        proposer = self.proposer
        proposal, status = self._guard.call(lambda: proposer.propose(inp), self.budget_s)
        if status != "ok":
            reason = {"timeout": spec.on_timeout_reason, "busy": spec.on_busy_reason}.get(status, spec.on_error_reason)
            return self._fallback(request, reason)
        if validate_proposal(proposal) is not None:
            return self._fallback(request, spec.on_invalid_reason)
        is_request = proposal.intent == INTENT_PRESENCE_REQUEST
        return DecisionResult(
            decision_id=new_id("dec"), request_id=request.request_id, kind=request.kind, outcome=proposal.intent,
            plan=DecisionPlan(
                route="ignore",
                placement=DecisionPlacement(
                    target_platform=proposal.target_location, presence_intent="request_move" if is_request else "none",
                ),
            ),
            confidence=float(proposal.confidence), reason_code=proposal.intent,  # 目標位置在 plan.placement.target_platform，日誌另存 target 欄
            engine=DecisionEngineInfo(tier="rule" if proposal.proposer_id.startswith("rules.") else "model",
                                      id=proposal.proposer_id, version=proposal.proposer_version),
        )

    @staticmethod
    def _fallback(request: DecisionRequest, reason: str) -> DecisionResult:
        return DecisionResult(
            decision_id=new_id("dec"), request_id=request.request_id, kind=request.kind, outcome=INTENT_OTHER,
            plan=DecisionPlan(route="ignore"), confidence=0.0, reason_code=reason,
            engine=DecisionEngineInfo(tier="fallback", id="core.fallback", version="d80"), fallback=True,
        )


def presence_request_from_result(result: DecisionResult) -> bool | None:
    """決策結果 -> V18 ``presence_request`` 欄位：fallback／無法判斷（other）為 None，message 為 False。"""
    if result.fallback:
        return None
    if result.outcome == INTENT_PRESENCE_REQUEST:
        return True
    if result.outcome == INTENT_MESSAGE:
        return False
    return None


def judge_absent_intent(
    hub: Any, inp: AbsentIntentInput, *, request_id: str, ref: str | None = None,
) -> DecisionResult:
    """中性入口：任何輸入型別轉成 ``AbsentIntentInput`` 後呼叫。只回意圖結果（shadow），不觸發移轉、仲裁或回條。

    ``ref`` 是不含原文的安全參考（如 ``es:...`` 或 item 的 ``raw_ref``），進決策日誌的 candidate_id。
    ``facts`` 只放 ID／列舉，原文只經 ``native``。engine 以 fallback 結果表示逾時／busy／例外；hub 本身例外由呼叫端接。
    """
    request = DecisionRequest(
        request_id=request_id, kind=KIND_PRESENCE_ABSENT_INTENT, trigger="event",
        subject={"candidate_id": ref} if ref else {},
        facts={
            "source_platform": inp.source_platform or "missing", "surface_ref": inp.surface_ref,
            "actor_ref": inp.actor_ref, "actor_authenticated": inp.actor_authenticated, "is_owner": inp.is_owner,
        },
        allowed_outcomes=INTENTS, native=inp,
    )
    return hub.decide(KIND_PRESENCE_ABSENT_INTENT, request)
