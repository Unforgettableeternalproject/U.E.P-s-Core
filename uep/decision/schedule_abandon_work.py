"""``schedule.abandon_work`` 決策點：把「結束階段」類話語分成四級（規則版 v1；契約 §3A.1）。

分級（歸級不是授權，PERM-2；任何不確定往較嚴的一級放）：

- ``direct``：明確提到「行程」（含工作行程、行程階段）或「回主機」的結束指示。只有此級產生 ``AbandonWorkSignal``。
- ``needs_confirmation``：只提「階段」（工作階段、單獨階段）或既有工作類說法（放棄這次工作、今天的工作不用做了、先不要工作了）。
  本決策點不產生訊號；由呼叫端（orchestrator）走確認流程（``uep/confirm``）。
- ``pause``：無錨點的結束意圖（今天先到這裡／先這樣吧／收工／不用待在那邊了／休息一下），小詞庫。PM-2b 只記決策日誌，
  ``PauseWorkSignal`` 屬 PM-3；訊號型別未就緒前呼叫端視同 none（不放棄、不確認、不暫停）。
- ``none``：其餘（裸「不用做了」「算了」、疑問、否定、轉述、過長）。

- 本決策點只產生話語層事實：不含 is_owner、不含行程欄位、不檢查話語是否「對她說」（addressed 由呼叫端依該輪決策判斷，D6）。
- 原文只存在於 ``DecisionRequest.native``（``AbandonWorkInput.text``，``repr=False``，僅記憶體）；``facts``、決策日誌、
  訊號都不含原文（日誌只留 utterance 參考與固定原因碼）。
- 規則刻意保守：必須整句（去除空白與標點後）完整符合；寧可漏判（使用者再說一次），不可誤判（誤放棄會打亂行程）。
- fail-closed：無命中、actor_ref 無效、輸入畸形、hub 未註冊或失敗一律回 None，不拋例外。
- 入口不接任何通道；接線由 orchestrator 負責。
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

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
OUTCOME_ABANDON_DIRECT = "abandon"  # 沿用現值＝direct，相容既有 hub 註冊與決策日誌
OUTCOME_ABANDON = OUTCOME_ABANDON_DIRECT
OUTCOME_ABANDON_CONFIRM = "abandon_confirm"
OUTCOME_PAUSE = "pause"
OUTCOME_NONE = "none"
OUTCOMES = (OUTCOME_ABANDON_DIRECT, OUTCOME_ABANDON_CONFIRM, OUTCOME_PAUSE, OUTCOME_NONE)

GRADE_DIRECT = "direct"
GRADE_NEEDS_CONFIRMATION = "needs_confirmation"
GRADE_PAUSE = "pause"
GRADE_NONE = "none"
AbandonGrade = Literal["direct", "needs_confirmation", "pause", "none"]
_GRADE_OUTCOME = {
    GRADE_DIRECT: OUTCOME_ABANDON_DIRECT, GRADE_NEEDS_CONFIRMATION: OUTCOME_ABANDON_CONFIRM,
    GRADE_PAUSE: OUTCOME_PAUSE, GRADE_NONE: OUTCOME_NONE,
}
_OUTCOME_GRADE = {v: k for k, v in _GRADE_OUTCOME.items()}

REASON_ANCHORED = "abandon_anchored"
REASON_UNANCHORED = "abandon_unanchored"
REASON_TO_PAUSE = "abandon_to_pause"
REASON_NO_MATCH = "abandon_no_match"
_GRADE_REASON = {
    GRADE_DIRECT: REASON_ANCHORED, GRADE_NEEDS_CONFIRMATION: REASON_UNANCHORED,
    GRADE_PAUSE: REASON_TO_PAUSE, GRADE_NONE: REASON_NO_MATCH,
}
ON_ERROR_REASON = "abandon_work_error"
ON_INVALID_REASON = "abandon_work_invalid"

DECIDER_ID = "rules.abandon_work"
DECIDER_VERSION = "1"
RULE_CONFIDENCE = 0.9
_ENGINE_VERSION = "1"
MAX_TEXT_LEN = 40  # 超過視為長文，不是命令式表達（中文）
MAX_TEXT_LEN_EN = 64  # 英文片語較長（end this schedule session and go back to the host＝49）

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
# 「放棄」＋指向工作的名詞片語（含「工作階段」）
_ABANDON_OBJ = r"(?:這次|本次|今天的?)(?:的)?工作(?:階段)?"  # 不收「這個／這回／這輪」：易被理解成專案或 agent 任務
# 「不用做了」只在主語明確是工作時才算
_NO_NEED_SUBJ = r"(?:今天|這次|本次)的?工作"  # 同 _ABANDON_OBJ，不收「這個」

# 「結束」類：direct 只收明確提到「行程」或「回主機」者（D9）；只提「階段」者歸 needs_confirmation
_ROUTE = r"(?:這次|本次|今天)?(?:的)?(?:工作)?行程(?:階段)?"  # 行程、工作行程、行程階段（含「行程」二字）
_STAGE_ONLY = r"(?:這次|本次|今天)?(?:的)?(?:工作)?階段"  # 工作階段、這次階段（未提「行程」）
_BACK_HOST = r"(?:然後|就)?(?:先)?回(?:到)?主機(?:待命)?"

_ZH_DIRECT: tuple[re.Pattern[str], ...] = (
    re.compile(rf"{_LEAD}(?:先)?結束{_ROUTE}(?:了)?(?:{_BACK_HOST})?{_TAIL}"),  # 結束這次行程／結束今天的行程階段回主機
    re.compile(rf"{_LEAD}(?:先)?結束(?:{_ROUTE}|{_STAGE_ONLY})?(?:了)?{_BACK_HOST}{_TAIL}"),  # 結束了回主機／結束工作階段回主機
)
_ZH_CONFIRM: tuple[re.Pattern[str], ...] = (
    re.compile(rf"{_LEAD}放棄{_ABANDON_OBJ}{_TAIL}"),  # 放棄這次工作／放棄今天的工作／放棄這次工作階段
    re.compile(rf"{_LEAD}{_NO_NEED_SUBJ}(?:就|都)?不用(?:再)?做{_TAIL}"),  # 今天的工作不用做了
    re.compile(rf"{_LEAD_NO_SELF}(?:今天)?先不要工作{_TAIL}"),  # 先不要工作了
    re.compile(rf"{_LEAD_NO_SELF}(?:今天)?先不工作{_TAIL}"),  # 先不工作了
    re.compile(rf"{_LEAD}(?:先)?結束{_STAGE_ONLY}(?:了)?{_TAIL}"),  # 結束這次工作階段（只提「階段」）
)
# 無錨點的結束意圖 → PAUSE（D1）：小而保守的詞庫，整句
_PAUSE_LEAD = r"(?:請|那|好|嗯|欸)?"
_ZH_PAUSE: tuple[re.Pattern[str], ...] = (
    re.compile(rf"{_PAUSE_LEAD}今天先到這裡(?:吧|了|囉|啦)?"),
    re.compile(rf"{_PAUSE_LEAD}先這樣(?:吧|啦|囉)"),
    re.compile(rf"{_PAUSE_LEAD}(?:我們)?收工(?:了|吧|囉|啦)?"),
    re.compile(rf"{_PAUSE_LEAD}不用(?:再)?待在(?:那邊|那裡|那裏)(?:了)?"),
    re.compile(rf"{_PAUSE_LEAD}(?:先)?休息一下(?:吧|囉|喔|啦)?"),
)
_EN_DIRECT: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:please )?end (?:this|today s) schedule(?: session)?(?: and (?:go|return) back to (?:the )?host)?"),
    re.compile(r"(?:please )?end (?:it|this|the session|this session|this work session) and (?:go|return) back to (?:the )?host"),
)
_EN_CONFIRM: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:please )?(?:give up|abandon) this work(?: session)?"),
    re.compile(r"(?:please )?end (?:this|today s) work session"),
)
# 英文片語清單（供回報與測試；direct 與 needs_confirmation 各列）
ENGLISH_PHRASES_DIRECT = (
    "end this schedule", "end this schedule session", "end this schedule session and go back to the host",
    "end this and go back to the host",
)
ENGLISH_PHRASES_CONFIRM = ("give up this work session", "abandon this work session", "end this work session")
ENGLISH_PHRASES = ENGLISH_PHRASES_DIRECT + ENGLISH_PHRASES_CONFIRM

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


def grade_abandon_text(text: object) -> tuple[AbandonGrade, str]:
    """話語分級：回 ``(grade, reason_code)``。保守：疑問符號、過長、未整句符合一律 none。

    歸級優先序 direct → needs_confirmation → pause → none（同一句取較嚴者）。"""
    none: tuple[AbandonGrade, str] = (GRADE_NONE, REASON_NO_MATCH)
    if not isinstance(text, str):
        return none
    stripped = text.strip()
    if not stripped or _QUESTION_MARK.search(stripped):
        return none
    if len(stripped) <= MAX_TEXT_LEN:
        zh = _strip_noise(stripped)
        for grade, patterns in (
            (GRADE_DIRECT, _ZH_DIRECT), (GRADE_NEEDS_CONFIRMATION, _ZH_CONFIRM), (GRADE_PAUSE, _ZH_PAUSE),
        ):
            if any(p.fullmatch(zh) for p in patterns):
                return grade, _GRADE_REASON[grade]  # type: ignore[return-value]
    if len(stripped) <= MAX_TEXT_LEN_EN:
        en = _normalize_en(stripped)
        for grade, patterns in ((GRADE_DIRECT, _EN_DIRECT), (GRADE_NEEDS_CONFIRMATION, _EN_CONFIRM)):
            if any(p.fullmatch(en) for p in patterns):
                return grade, _GRADE_REASON[grade]  # type: ignore[return-value]
    return none


def matches_abandon_work(text: object) -> bool:
    """文字是否為明確結束行程階段（direct）或需確認的放棄工作（needs_confirmation）。pause 與 none 回 False。

    ``commitment.change`` 以它做互斥，因此涵蓋 direct 與 needs_confirmation 兩級。"""
    return grade_abandon_text(text)[0] in (GRADE_DIRECT, GRADE_NEEDS_CONFIRMATION)


# ---- engine ----


@dataclass(frozen=True)
class AbandonWorkInput:
    """engine 的原生輸入；``text`` 僅記憶體（``repr=False``）。"""

    text: str = field(repr=False)


class AbandonWorkEngine:
    """``request.native`` 的 ``AbandonWorkInput`` -> 決策結果；outcome 為四級之一（abandon／abandon_confirm／pause／none）。

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
            grade, reason = grade_abandon_text(inp.text)
        except Exception:
            logger.exception("abandon_work 規則失敗")
            return self._fallback(request, self._spec.on_error_reason)
        return DecisionResult(
            decision_id=new_id("dec"), request_id=request.request_id, kind=request.kind,
            outcome=_GRADE_OUTCOME[grade], plan=DecisionPlan(route="ignore"),
            confidence=1.0 if grade == GRADE_NONE else RULE_CONFIDENCE, reason_code=reason,
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


@dataclass(frozen=True)
class AbandonJudgement:
    """``judge_abandon_work_graded`` 的回傳：分級結果。只有 direct 帶訊號；pause 的 ``PauseWorkSignal`` 屬 PM-3，這裡不產生。"""

    grade: AbandonGrade
    signal: AbandonWorkSignal | None
    reason_code: str
    candidate_ref: str  # utterance id（確認流程冪等鍵 cfm:<utterance_id> 的來源）


def judge_abandon_work_graded(
    hub: Any, text: str, *, utterance_id: str, actor_ref: str, actor_authenticated: bool,
    source_platform: str, path: str, now: datetime,
) -> AbandonJudgement | None:
    """分級判斷一句話；無命中（none）、輸入畸形、hub 失敗回 None，不拋例外。

    每次判斷（輸入合法時）在決策日誌留一列（只有 utterance 參考與固定原因碼，無原文）。
    ``actor_ref`` 須為平台認證 ID 形式；``now`` 須帶時區。訊號不含 is_owner 或任何行程欄位；
    addressed（D6）由呼叫端檢查，本函式不看。
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
        grade: AbandonGrade = _OUTCOME_GRADE.get(result.outcome, GRADE_NONE)  # type: ignore[assignment]
        if result.fallback or grade == GRADE_NONE:
            return None
        signal: AbandonWorkSignal | None = None
        if grade == GRADE_DIRECT:
            signal = AbandonWorkSignal(
                signal_id=signal_id, actor_ref=actor_ref, actor_authenticated=actor_authenticated,
                source_platform=source_platform, path=path, decided_at=now,
                decider_id=DECIDER_ID, decider_version=DECIDER_VERSION, reason_ref=None,
                confidence=float(result.confidence),
            )
        return AbandonJudgement(grade=grade, signal=signal, reason_code=result.reason_code, candidate_ref=utterance_id)
    except Exception:
        logger.exception("abandon_work 判斷失敗")
        return None


def judge_abandon_work(
    hub: Any, text: str, *, utterance_id: str, actor_ref: str, actor_authenticated: bool,
    source_platform: str, path: str, now: datetime,
) -> AbandonWorkSignal | None:
    """只回 direct 的訊號（needs_confirmation／pause／none 一律 None）；輸入畸形或 hub 失敗也回 None，不拋例外。"""
    judgement = judge_abandon_work_graded(
        hub, text, utterance_id=utterance_id, actor_ref=actor_ref, actor_authenticated=actor_authenticated,
        source_platform=source_platform, path=path, now=now,
    )
    return judgement.signal if judgement is not None and judgement.grade == GRADE_DIRECT else None
