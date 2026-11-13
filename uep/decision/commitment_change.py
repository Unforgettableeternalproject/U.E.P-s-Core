"""``commitment.change`` 決策點：判斷話語是否為「取消她答應的事」的明確表達（規則版 v1，只判斷取消）。

- 只產生話語層事實（``CommitmentChangeSignal``）：不含 is_owner、不含承諾資料；owner 判定與承諾綁定由消費端自行處理。
- 原文只存在於 ``DecisionRequest.native``（``CommitmentChangeInput.text``，``repr=False``，僅記憶體）；``facts``、
  決策日誌、訊號都不含原文（日誌只留 utterance 參考與固定原因碼）。
- 規則刻意保守：整句（去除空白與標點後）必須完整符合「明確指向她答應的事」的句型（提醒、剛剛拜託你的事、不用幫我○○了）；
  沒有指涉對象的「算了」「不用了」不觸發。疑問、否定、轉述（「他說不用提醒我了」）、超過長度上限一律不觸發，寧可漏判。
- 與 ``schedule.abandon_work`` 互斥：句型不重疊，且命中放棄工作句型的文字一律不當成取消承諾。
- fail-closed：無命中、actor_ref 無效、輸入畸形、hub 未註冊或失敗一律回 None，不拋例外。
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from uep.contracts.schedule import CommitmentChangeSignal
from uep.decision.contracts import (
    DecisionEngineInfo,
    DecisionPlan,
    DecisionRequest,
    DecisionResult,
)
from uep.decision.registry import DecisionPointSpec
from uep.decision.schedule_abandon_work import matches_abandon_work
from uep.ids import new_id

logger = logging.getLogger("uep.decision.commitment_change")

KIND_COMMITMENT_CHANGE = "commitment.change"
OUTCOME_CANCEL = "cancel"
OUTCOME_NONE = "none"
OUTCOMES = (OUTCOME_CANCEL, OUTCOME_NONE)

REASON_PHRASE = "commitment_cancel_phrase"
REASON_NO_MATCH = "commitment_no_match"
ON_ERROR_REASON = "commitment_change_error"
ON_INVALID_REASON = "commitment_change_invalid"

DECIDER_ID = "rules.commitment_change"
DECIDER_VERSION = "1"
RULE_CONFIDENCE = 0.9
_ENGINE_VERSION = "1"
MAX_TEXT_LEN = 30  # 超過視為長文，不是命令式表達

_ACTOR_REF = re.compile(r"^[a-z0-9_.:-]{1,128}$")  # 與 CommitmentChangeSignal 的 _ACTOR_REF 一致
_SAFE_REF = re.compile(r"^[A-Za-z0-9_.:\-]{1,116}$")  # utterance id；加上 "commitment:" 前綴後仍在 128 內


def commitment_change_spec() -> DecisionPointSpec:
    """``commitment.change`` 規格：sync、接受 native（原文只在記憶體）、純規則不設預算。"""
    return DecisionPointSpec(
        kind=KIND_COMMITMENT_CHANGE, mode="sync", owner="commitment", outcomes=OUTCOMES, budget_ms=None,
        on_error_reason=ON_ERROR_REASON, on_invalid_reason=ON_INVALID_REASON, allow_native=True, shadow=False,
    )


# ---- 規則 ----

# 允許的句首贅詞與句尾語氣詞（整句 fullmatch，其餘一律不觸發）；句首不收「我」「他說」等，避免轉述
_LEAD = r"(?:請|麻煩你?|那|好|嗯|欸)?"
_TAIL = r"(?:了|吧|啦|囉|喔|哦)?"
_DET = r"(?:那個|這個|剛剛的?|剛才的?)"  # 指向性限定詞
_ASKED = r"(?:拜託你|請你|麻煩你|交代你)"  # 「剛剛拜託你的事」
_EARLIER = r"(?:剛剛|剛才)"
# 「不用幫我○○了」的受詞：不得含他／她／說／否定字／疑問字（轉述、雙重否定、疑問都不收）
_OBJ = r"[^他她說不沒別嗎呢吧啊哪誰什怎]{1,12}"

_ZH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"{_LEAD}不用(?:再)?提醒我{_TAIL}"),  # 不用提醒我了／不用再提醒我了
    re.compile(rf"{_LEAD}{_DET}提醒(?:就|都)?(?:取消|不用|不要){_TAIL}"),  # 那個提醒取消／那個提醒不用了
    re.compile(rf"{_LEAD}取消{_DET}提醒{_TAIL}"),  # 取消那個提醒
    re.compile(rf"{_LEAD}{_EARLIER}{_ASKED}的(?:那件)?事(?:情)?(?:就|都)?不用(?:再)?(?:做|辦|幫)?{_TAIL}"),  # 剛剛拜託你的事不用了
    re.compile(rf"{_LEAD}取消{_EARLIER}(?:那件事|那件事情|那個事情|{_ASKED}的事(?:情)?)"),  # 取消剛才那件事
    re.compile(rf"{_LEAD}不用(?:再)?幫我{_OBJ}了(?:吧|啦|囉|喔|哦)?"),  # 不用幫我訂位了
)
_EN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:please )?(?:cancel|forget) (?:that|the|this|my) reminder"),
    re.compile(r"(?:please )?forget about (?:that|the|this|my) reminder"),
    re.compile(r"never ?mind (?:that|the|this|my) reminder"),
)
# 英文片語清單（供回報與測試）
ENGLISH_PHRASES = ("cancel that reminder", "forget that reminder", "forget about that reminder", "never mind the reminder")

_QUESTION_MARK = re.compile(r"[?？]")


def _strip_noise(text: str) -> str:
    """去除空白與標點／符號，用於中文整句比對。"""
    return "".join(
        ch for ch in text if not ch.isspace() and unicodedata.category(ch)[0] not in ("P", "S")
    )


def _normalize_en(text: str) -> str:
    """英文：標點換成空白、收斂空白、轉小寫。"""
    cleaned = "".join(" " if unicodedata.category(ch)[0] in ("P", "S") else ch for ch in text)
    return " ".join(cleaned.lower().split())


def matches_commitment_cancel(text: object) -> bool:
    """文字是否為明確的取消承諾表達。保守：疑問符號、過長、未整句符合、或屬放棄工作句型都回 False。"""
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped or len(stripped) > MAX_TEXT_LEN or _QUESTION_MARK.search(stripped):
        return False
    if matches_abandon_work(stripped):  # 與放棄工作互斥
        return False
    zh = _strip_noise(stripped)
    if any(p.fullmatch(zh) for p in _ZH_PATTERNS):
        return True
    en = _normalize_en(stripped)
    return any(p.fullmatch(en) for p in _EN_PATTERNS)


# ---- engine ----


@dataclass(frozen=True)
class CommitmentChangeInput:
    """engine 的原生輸入；``text`` 僅記憶體（``repr=False``）。"""

    text: str = field(repr=False)


class CommitmentChangeEngine:
    """``request.native`` 的 ``CommitmentChangeInput`` -> 決策結果；命中 outcome=cancel，其餘 none。

    plan 只帶 ``route="ignore"``：本決策點不派發任何事，訊號由入口函式依結果組出。
    """

    def __init__(self, *, spec: DecisionPointSpec | None = None) -> None:
        self._spec = spec or commitment_change_spec()

    @property
    def spec(self) -> DecisionPointSpec:
        return self._spec

    def decide(self, request: DecisionRequest) -> DecisionResult:
        inp = request.native
        if not isinstance(inp, CommitmentChangeInput):
            return self._fallback(request, self._spec.on_invalid_reason)
        try:
            hit = matches_commitment_cancel(inp.text)
        except Exception:
            logger.exception("commitment_change 規則失敗")
            return self._fallback(request, self._spec.on_error_reason)
        return DecisionResult(
            decision_id=new_id("dec"), request_id=request.request_id, kind=request.kind,
            outcome=OUTCOME_CANCEL if hit else OUTCOME_NONE, plan=DecisionPlan(route="ignore"),
            confidence=RULE_CONFIDENCE if hit else 1.0, reason_code=REASON_PHRASE if hit else REASON_NO_MATCH,
            engine=DecisionEngineInfo(tier="rule", id=DECIDER_ID, version=_ENGINE_VERSION),
        )

    @staticmethod
    def _fallback(request: DecisionRequest, reason: str) -> DecisionResult:
        return DecisionResult(
            decision_id=new_id("dec"), request_id=request.request_id, kind=request.kind, outcome=OUTCOME_NONE,
            plan=DecisionPlan(route="ignore"), confidence=0.0, reason_code=reason,
            engine=DecisionEngineInfo(tier="fallback", id="core.fallback", version="d80"), fallback=True,
        )


def register_commitment_change(hub: Any, *, replace: bool = False) -> CommitmentChangeEngine:
    """在 hub 註冊 ``commitment.change``（接線時呼叫一次）。"""
    engine = CommitmentChangeEngine()
    hub.register(engine.spec, engine, replace=replace)
    return engine


def commitment_signal_id(utterance_id: str) -> str | None:
    """由來源 utterance id 衍生的冪等鍵；同一句重送得到同一個 id。id 不符安全字元集回 None。"""
    if not isinstance(utterance_id, str) or _SAFE_REF.fullmatch(utterance_id) is None:
        return None
    return f"commitment:{utterance_id}"


def judge_commitment_change(
    hub: Any, text: str, *, utterance_id: str, actor_ref: str, actor_authenticated: bool,
    source_platform: str, path: str, now: datetime,
) -> CommitmentChangeSignal | None:
    """判斷一句話是否為取消承諾；命中回 ``CommitmentChangeSignal``，其餘（無命中、輸入畸形、hub 失敗）回 None，不拋例外。

    每次判斷（輸入合法時）在決策日誌留一列（只有 utterance 參考與固定原因碼，無原文）。
    ``actor_ref`` 須為平台認證 ID 形式；``now`` 須帶時區。訊號不含 is_owner。
    """
    try:
        signal_id = commitment_signal_id(utterance_id)
        if (
            signal_id is None or not isinstance(text, str)
            or not isinstance(actor_ref, str) or _ACTOR_REF.fullmatch(actor_ref) is None
            or not isinstance(actor_authenticated, bool)
            or not isinstance(source_platform, str) or not source_platform
            or path not in ("turn", "absent")
            or not isinstance(now, datetime) or now.tzinfo is None
        ):
            return None
        request = DecisionRequest(
            request_id=signal_id, kind=KIND_COMMITMENT_CHANGE, trigger="event",
            subject={"candidate_id": utterance_id},
            facts={
                "source_platform": source_platform, "actor_ref": actor_ref,
                "actor_authenticated": actor_authenticated, "path": path,
            },
            allowed_outcomes=OUTCOMES, native=CommitmentChangeInput(text=text),
        )
        if hub.engine(KIND_COMMITMENT_CHANGE) is None:
            return None
        result = hub.decide(KIND_COMMITMENT_CHANGE, request)
        if result.fallback or result.outcome != OUTCOME_CANCEL:
            return None
        return CommitmentChangeSignal(
            signal_id=signal_id, actor_ref=actor_ref, kind="cancel", source_platform=source_platform, path=path,
            decided_at=now, decider_id=DECIDER_ID, decider_version=DECIDER_VERSION, reason_code=result.reason_code,
        )
    except Exception:
        logger.exception("commitment_change 判斷失敗")
        return None
