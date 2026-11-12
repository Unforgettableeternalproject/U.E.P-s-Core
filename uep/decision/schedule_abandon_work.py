"""``schedule.abandon_work`` 決策點：判斷話語是否為「放棄當前工作階段」的明確表達（規則版 v1）。

- 本決策點只產生話語層事實（``AbandonWorkSignal``）：不含 is_owner、不含行程欄位；owner 判定與投影綁定由消費端（仲裁器）
  自行計算。輸出不是授權。
- 原文只存在於 ``DecisionRequest.native``（``AbandonWorkInput.text``，``repr=False``，僅記憶體）；``facts``、決策日誌、
  訊號都不含原文（日誌只留 utterance 參考與固定原因碼）。
- 規則刻意保守：必須整句（去除空白與標點後）完整符合「明確指向工作」的句型；寧可漏判（使用者再說一次），
  不可誤判（誤放棄會打亂行程）。單獨的「不用做了」（與串鏈尾段取消、Agent 任務取消衝突）、「休息一下」（是暫停不是放棄）、
  否定、疑問、轉述一律不觸發。
- fail-closed：無命中、actor_ref 無效、輸入畸形、hub 未註冊或失敗一律回 None，不拋例外。
- 入口不接任何通道；接線（orchestrator／缺席端）由後續工作負責。
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from uep.contracts.schedule import AbandonWorkSignal
from uep.decision.contracts import (
    DecisionEngineInfo,
    DecisionPlan,
    DecisionRequest,
    DecisionResult,
)
from uep.decision.registry import DecisionPointSpec
from uep.ids import new_id

logger = logging.getLogger("uep.decision.schedule_abandon_work")

KIND_SCHEDULE_ABANDON_WORK = "schedule.abandon_work"
OUTCOME_ABANDON = "abandon"
OUTCOME_NONE = "none"
OUTCOMES = (OUTCOME_ABANDON, OUTCOME_NONE)

REASON_PHRASE = "abandon_phrase"
REASON_NO_MATCH = "abandon_no_match"
ON_ERROR_REASON = "abandon_work_error"
ON_INVALID_REASON = "abandon_work_invalid"

DECIDER_ID = "rules.abandon_work"
DECIDER_VERSION = "1"
RULE_CONFIDENCE = 0.9
_ENGINE_VERSION = "1"
MAX_TEXT_LEN = 40  # 超過視為長文，不是命令式表達

_ACTOR_REF = re.compile(r"^[a-z0-9_.:-]{1,128}$")  # 與 AbandonWorkSignal 的 _ACTOR_REF 一致
_SAFE_REF = re.compile(r"^[A-Za-z0-9_.:\-]{1,120}$")  # utterance id；加上 "abandon:" 前綴後仍在 128 內


def schedule_abandon_work_spec() -> DecisionPointSpec:
    """``schedule.abandon_work`` 規格：sync、接受 native（原文只在記憶體）、純規則不設預算。"""
    return DecisionPointSpec(
        kind=KIND_SCHEDULE_ABANDON_WORK, mode="sync", owner="schedule", outcomes=OUTCOMES, budget_ms=None,
        on_error_reason=ON_ERROR_REASON, on_invalid_reason=ON_INVALID_REASON, allow_native=True, shadow=False,
    )


# ---- 規則 ----

# 允許的句首贅詞與句尾語氣詞（整句 fullmatch，其餘一律不觸發）
_LEAD = r"(?:請|麻煩你?|那|好|嗯|欸|我們|我要|我)?"
# 「先不要工作了」類句型不收「我／我要／我們」開頭：「我先不要工作了」較像在說自己不想工作
_LEAD_NO_SELF = r"(?:請|麻煩你?|那|好|嗯|欸)?"
_TAIL = r"(?:了|吧|啦|囉|喔|哦)?"
# 「放棄」＋指向工作的名詞片語
_ABANDON_OBJ = r"(?:這次|本次|今天的?)(?:的)?工作(?:階段)?"  # 不收「這個／這回／這輪」：易被理解成專案或 agent 任務
# 「不用做了」只在主語明確是工作時才算
_NO_NEED_SUBJ = r"(?:今天|這次|這個|本次)的?工作"

_ZH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"{_LEAD}放棄{_ABANDON_OBJ}{_TAIL}"),  # 放棄這次工作／放棄本次工作／放棄今天的工作
    re.compile(rf"{_LEAD}{_NO_NEED_SUBJ}(?:就|都)?不用(?:再)?做{_TAIL}"),  # 今天的工作不用做了
    re.compile(rf"{_LEAD_NO_SELF}(?:今天)?先不要工作{_TAIL}"),  # 先不要工作了
    re.compile(rf"{_LEAD_NO_SELF}(?:今天)?先不工作{_TAIL}"),  # 先不工作了
)
# 「放棄這次工作了」等句尾「了」已由 _TAIL 涵蓋；「不用做了」的「了」同理
_EN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:please )?(?:give up|abandon) this work session"),
)
# 英文片語清單（供回報與測試）
ENGLISH_PHRASES = ("give up this work session", "abandon this work session")

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


def matches_abandon_work(text: object) -> bool:
    """文字是否為明確的放棄工作表達。保守：任何疑問符號、過長、或未整句符合都回 False。"""
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped or len(stripped) > MAX_TEXT_LEN or _QUESTION_MARK.search(stripped):
        return False
    zh = _strip_noise(stripped)
    if any(p.fullmatch(zh) for p in _ZH_PATTERNS):
        return True
    en = _normalize_en(stripped)
    return any(p.fullmatch(en) for p in _EN_PATTERNS)


# ---- engine ----


@dataclass(frozen=True)
class AbandonWorkInput:
    """engine 的原生輸入；``text`` 僅記憶體（``repr=False``）。"""

    text: str = field(repr=False)


class AbandonWorkEngine:
    """``request.native`` 的 ``AbandonWorkInput`` -> 決策結果；命中 outcome=abandon，其餘 none。

    plan 只帶 ``route="ignore"``：本決策點不派發任何事，訊號由入口函式依結果組出。
    """

    def __init__(self, *, spec: DecisionPointSpec | None = None) -> None:
        self._spec = spec or schedule_abandon_work_spec()

    @property
    def spec(self) -> DecisionPointSpec:
        return self._spec

    def decide(self, request: DecisionRequest) -> DecisionResult:
        inp = request.native
        if not isinstance(inp, AbandonWorkInput):
            return self._fallback(request, self._spec.on_invalid_reason)
        try:
            hit = matches_abandon_work(inp.text)
        except Exception:
            logger.exception("abandon_work 規則失敗")
            return self._fallback(request, self._spec.on_error_reason)
        return DecisionResult(
            decision_id=new_id("dec"), request_id=request.request_id, kind=request.kind,
            outcome=OUTCOME_ABANDON if hit else OUTCOME_NONE, plan=DecisionPlan(route="ignore"),
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


def register_abandon_work(hub: Any, *, replace: bool = False) -> AbandonWorkEngine:
    """在 hub 註冊 ``schedule.abandon_work``（接線時呼叫一次）。"""
    engine = AbandonWorkEngine()
    hub.register(engine.spec, engine, replace=replace)
    return engine


def abandon_signal_id(utterance_id: str) -> str | None:
    """由來源 utterance id 衍生的冪等鍵；同一句重送得到同一個 id。id 不符安全字元集回 None。"""
    if not isinstance(utterance_id, str) or _SAFE_REF.fullmatch(utterance_id) is None:
        return None
    return f"abandon:{utterance_id}"


def judge_abandon_work(
    hub: Any, text: str, *, utterance_id: str, actor_ref: str, actor_authenticated: bool,
    source_platform: str, path: str, now: datetime,
) -> AbandonWorkSignal | None:
    """判斷一句話是否為放棄工作；命中回 ``AbandonWorkSignal``，其餘（無命中、輸入畸形、hub 失敗）回 None，不拋例外。

    每次判斷（輸入合法時）在決策日誌留一列（只有 utterance 參考與固定原因碼，無原文）。
    ``actor_ref`` 須為平台認證 ID 形式；``now`` 須帶時區。訊號不含 is_owner 或任何行程欄位。
    """
    try:
        signal_id = abandon_signal_id(utterance_id)
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
            request_id=signal_id, kind=KIND_SCHEDULE_ABANDON_WORK, trigger="event",
            subject={"candidate_id": utterance_id},
            facts={
                "source_platform": source_platform, "actor_ref": actor_ref,
                "actor_authenticated": actor_authenticated, "path": path,
            },
            allowed_outcomes=OUTCOMES, native=AbandonWorkInput(text=text),
        )
        if hub.engine(KIND_SCHEDULE_ABANDON_WORK) is None:
            return None
        result = hub.decide(KIND_SCHEDULE_ABANDON_WORK, request)
        if result.fallback or result.outcome != OUTCOME_ABANDON:
            return None
        return AbandonWorkSignal(
            signal_id=signal_id, actor_ref=actor_ref, actor_authenticated=actor_authenticated,
            source_platform=source_platform, path=path, decided_at=now,
            decider_id=DECIDER_ID, decider_version=DECIDER_VERSION, reason_ref=None,
            confidence=float(result.confidence),
        )
    except Exception:
        logger.exception("abandon_work 判斷失敗")
        return None
