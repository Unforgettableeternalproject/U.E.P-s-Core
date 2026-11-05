"""``loop.consult`` 決策點（DL-C1，提案 §4.6.6、工具層提案 §4.9 CN-1～9）：Agent 迴圈把局部決定委派給中樞。

決策層側只提供 spec、規則 engine、fail-safe 預設表、6 個 sub_kind 的 schema；工具層 registry 條目、executor 薄層、
迴圈側再驗與計步不在這裡。回傳只是建議（資料，不是授權）：

- ``advice`` 只能落在「凍結集合 ∩ 本次 capability_snapshot」內（CN-1）；超出時只回 ``escalate.needed=True``，不回 advice。
- snapshot／凍結集合缺漏或格式錯 -> fail-closed（escalate）。``placement`` 第一版一律 escalate（Q26 的逐 kind 清單待定）。
- 逾時、busy、invalid、engine 例外一律回 ``advice=None``＋對應 ``reason_code``，不拋例外，由迴圈套用 ``FAILSAFE_DEFAULTS``。
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from uep.decision.contracts import (
    DecisionEngineInfo,
    DecisionFollowup,
    DecisionLoop,
    DecisionOutput,
    DecisionPlan,
    DecisionReply,
    DecisionRequest,
    DecisionResult,
    Escalate,
)
from uep.decision.hub import DecisionHub
from uep.decision.registry import DecisionPointRegistry, DecisionPointSpec
from uep.ids import derived_id

logger = logging.getLogger("uep.decision.loop_consult")

KIND_LOOP_CONSULT = "loop.consult"
ENGINE_ID = "loop_consult_rules"
ENGINE_VERSION = "1"
CONSULT_BUDGET_MS = 300  # 只計 decide()；executor 總時限 0.5s 由工具層訂

ON_TIMEOUT_REASON = "consult_timeout"
ON_BUSY_REASON = "consult_busy"
ON_INVALID_REASON = "consult_invalid"

# escalate 的 reason_code（閉集，安全字元）
ESC_SCOPE_EXCEEDED = "scope_exceeded"
ESC_SNAPSHOT_INVALID = "snapshot_invalid"
ESC_PLACEMENT_REVIEW = "placement_review"

# 暫定值，待 Bernie 調：每 Run 的 consult 上限與連續無進展次數。consult 計入迴圈步數，兩個上限誰先到就照誰
# （迴圈側使用，這裡只提供常數；正式值由 policy_ref 解析）。
MAX_CONSULTS = 8
NO_PROGRESS_N = 3

# sub_kind 閉集（Q25 A：新增須改程式碼＋測試＋審核）
SUB_KINDS: tuple[str, ...] = ("speak", "output_modality", "placement", "ask_user", "loop_end", "followup")

# advice 對應的 plan 區段（ask_user 與 speak 同走 reply）
ADVICE_SECTION: Mapping[str, str] = MappingProxyType({
    "speak": "reply", "output_modality": "output", "placement": "placement",
    "ask_user": "reply", "loop_end": "loop", "followup": "followups",
})

# 各 sub_kind advice 的合法取值（區段內的決定欄位）
SPEAK_MODES: tuple[str, ...] = ("respond", "passive", "pass", "defer")  # 沿用 Proposal 語彙
ASK_USER_MODES: tuple[str, ...] = ("defer", "ask")
LOOP_OUTCOMES: tuple[str, ...] = ("continue", "finalize")
OUTPUT_MODALITIES: tuple[str, ...] = ("text", "audio_stream", "typing", "animation")
FOLLOWUP_KINDS: tuple[str, ...] = ("completion_report",)

# fail-safe 預設表（提案 §4.6.6）：advice=∅ 時由迴圈側套用，全取最保守、不擴權、不產生新副作用的一側
FAILSAFE_DEFAULTS: Mapping[str, str] = MappingProxyType({
    "speak": "hold",
    "output_modality": "keep_current",
    "placement": "keep_current_platform",
    "ask_user": "ask",
    "loop_end": "finalize",
    "followup": "none",
})

_RULE_CONFIDENCE = 0.8
_ESCALATE_CONFIDENCE = 1.0


def loop_consult_spec(budget_ms: int = CONSULT_BUDGET_MS) -> DecisionPointSpec:
    """``loop.consult`` 的規格：sync、可 shadow、不開 native、不用 LLM；fallback reason 由此提供。"""
    outcomes = sorted({*SPEAK_MODES, *ASK_USER_MODES, *LOOP_OUTCOMES, "output_set", "completion_report", "escalate", "none"})
    return DecisionPointSpec(
        kind=KIND_LOOP_CONSULT, mode="sync", owner="loop", outcomes=tuple(outcomes), budget_ms=budget_ms,
        on_timeout_reason=ON_TIMEOUT_REASON, on_busy_reason=ON_BUSY_REASON,
        on_error_reason=ON_INVALID_REASON,  # engine 例外與 invalid 同為 consult_invalid（reason 閉集只有三個）
        on_invalid_reason=ON_INVALID_REASON, shadow=True, allow_native=False,
    )


LOOP_CONSULT_SPEC = loop_consult_spec()


# ---- schema（供工具層 registry 條目使用）----

_STR_LIST = {"type": "array", "items": {"type": "string"}}
_SCOPE_SCHEMA = {
    "type": "object",
    "properties": {"capability_tags": _STR_LIST, "modalities": _STR_LIST, "platforms": _STR_LIST},
}
_COMMON_INPUT_PROPS: dict[str, Any] = {
    "sub_kind": {"enum": list(SUB_KINDS)},
    "run_id": {"type": "string", "minLength": 1},
    "step_id": {"type": "string", "minLength": 1},
    "ordinal": {"type": "integer", "minimum": 0},
    "frozen": _SCOPE_SCHEMA,
    "capability_snapshot": _SCOPE_SCHEMA,
}
_KIND_INPUT_PROPS: dict[str, dict[str, Any]] = {
    "speak": {"user_present": {"type": "boolean"}, "result_ready": {"type": "boolean"}},
    "output_modality": {"proposed_modalities": {"type": "array", "items": {"enum": list(OUTPUT_MODALITIES)}}},
    "placement": {"target_platform": {"type": "string"}, "executor_ref": {"type": "string"}},
    "ask_user": {"confident_default": {"type": "boolean"}},
    "loop_end": {
        "goal_satisfied": {"type": "boolean"}, "made_progress": {"type": "boolean"},
        "consults_used": {"type": "integer", "minimum": 0}, "no_progress_count": {"type": "integer", "minimum": 0},
    },
    "followup": {"report_pending": {"type": "boolean"}},
}
_KIND_ADVICE_SCHEMAS: dict[str, dict[str, Any]] = {
    "speak": {
        "type": "object", "required": ["reply"], "additionalProperties": False,
        "properties": {"reply": {
            "type": "object", "required": ["mode"],
            "properties": {"mode": {"enum": list(SPEAK_MODES)}, "interaction_to": {"type": ["string", "null"]}},
        }},
    },
    "output_modality": {
        "type": "object", "required": ["output"], "additionalProperties": False,
        "properties": {"output": {
            "type": "object", "required": ["modalities", "tts"],
            "properties": {
                "modalities": {"type": "array", "items": {"enum": list(OUTPUT_MODALITIES)}},
                "tts": {"type": "boolean"},
                "emotion": {"type": "object"},
                "history": {"enum": ["commit", "ephemeral"]},
            },
        }},
    },
    # 第一版 placement 一律 escalate，不會有 advice；schema 保留區段形狀供之後放寬
    "placement": {
        "type": "object", "required": ["placement"], "additionalProperties": False,
        "properties": {"placement": {
            "type": "object",
            "properties": {
                "target_platform": {"type": ["string", "null"]}, "executor_ref": {"type": ["string", "null"]},
                "presence_intent": {"enum": ["stay", "request_move", "none"]},
            },
        }},
    },
    "ask_user": {
        "type": "object", "required": ["reply"], "additionalProperties": False,
        "properties": {"reply": {
            "type": "object", "required": ["mode"], "properties": {"mode": {"enum": list(ASK_USER_MODES)}},
        }},
    },
    "loop_end": {
        "type": "object", "required": ["loop"], "additionalProperties": False,
        "properties": {"loop": {
            "type": "object", "required": ["outcome"], "properties": {"outcome": {"enum": list(LOOP_OUTCOMES)}},
        }},
    },
    "followup": {
        "type": "object", "required": ["followups"], "additionalProperties": False,
        "properties": {"followups": {
            "type": "array",
            "items": {
                "type": "object", "required": ["kind"],
                "properties": {
                    "kind": {"enum": list(FOLLOWUP_KINDS)}, "after_ms": {"type": ["integer", "null"]},
                    "on_event": {"type": ["string", "null"]},
                },
            },
        }},
    },
}


def input_schema(sub_kind: str) -> dict[str, Any]:
    """該 sub_kind 的 ``facts`` JSON schema（英文列舉／ID，不含原文）。"""
    _require_sub_kind(sub_kind)
    return {
        "type": "object", "required": ["sub_kind", "run_id", "step_id", "ordinal", "frozen", "capability_snapshot"],
        "properties": {**_COMMON_INPUT_PROPS, **_KIND_INPUT_PROPS[sub_kind], "sub_kind": {"const": sub_kind}},
    }


def output_schema(sub_kind: str) -> dict[str, Any]:
    """回傳 JSON schema：``advice``（該 sub_kind 的 plan 片段或 null）、``reason_code``、``confidence``、``escalate``、``decision_id``。"""
    _require_sub_kind(sub_kind)
    return {
        "type": "object", "required": ["advice", "reason_code", "confidence", "escalate", "decision_id"],
        "properties": {
            "advice": {"anyOf": [{"type": "null"}, _KIND_ADVICE_SCHEMAS[sub_kind]]},
            "reason_code": {"type": "string", "pattern": "^[a-z0-9_.:-]{0,64}$"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "escalate": {
                "type": "object", "required": ["needed", "reason_code"],
                "properties": {"needed": {"type": "boolean"}, "reason_code": {"type": "string"}},
            },
            "decision_id": {"type": "string", "pattern": "^dec_[0-9A-HJKMNP-TV-Z]{26}$"},
        },
    }


def _require_sub_kind(sub_kind: str) -> None:
    if sub_kind not in SUB_KINDS:
        raise ValueError(f"未知的 loop.consult sub_kind：{sub_kind!r}")


# ---- 決定性 decision_id ----

def consult_decision_id(run_id: str, step_id: str, ordinal: int, sub_kind: str) -> str:
    """同一 (run_id, step_id, ordinal, sub_kind) 重放得到同一個 ``dec_`` ID。

    ``new_id("dec")`` 是隨機 ULID，無法支援重放，故改用 ``derived_id``（SHA-256 導出，保留 ``dec`` 前綴與同樣的
    Crockford 格式，``is_valid_id`` 通過）。鍵含 kind 與 sub_kind：同一個 ordinal 換 sub_kind 不會撞號，
    也不會與其他決策點撞號。
    """
    return derived_id("dec", KIND_LOOP_CONSULT, run_id, step_id, str(ordinal), sub_kind)


# ---- 結果 ----

@dataclass(frozen=True)
class ConsultResult:
    """consult 的回傳形狀（工具層以此組 ``result_view``）。``advice`` 為 None 即 ∅：迴圈採 ``FAILSAFE_DEFAULTS``。"""

    advice: DecisionPlan | None
    reason_code: str
    confidence: float
    escalate: Escalate
    decision_id: str
    sub_kind: str | None = None
    fallback: bool = False  # 逾時／busy／invalid／例外（status 仍是 ok）
    outcome: str = "none"  # advice 的決定值；escalate 為 "escalate"；∅ 為 "none"


class _ConsultInvalid(Exception):
    """facts 或規則輸出不合 schema。"""


# ---- 範圍（CN-1）----

def _effective_scope(frozen: Any, snapshot: Any, required: tuple[str, ...]) -> dict[str, frozenset[str]] | None:
    """凍結集合 ∩ 本次 snapshot；任一必要鍵缺漏或格式錯回 None（fail-closed）。只能收窄，不會引入集合外的元素。"""
    if not isinstance(frozen, Mapping) or not isinstance(snapshot, Mapping):
        return None
    scope: dict[str, frozenset[str]] = {}
    for key in required:
        a, b = frozen.get(key), snapshot.get(key)
        for value in (a, b):
            if not isinstance(value, (list, tuple, set, frozenset)) or not all(isinstance(v, str) for v in value):
                return None
        scope[key] = frozenset(a) & frozenset(b)
    return scope


# ---- 規則（每個回傳 advice 區段與決定值；只用 facts 內的英文欄位）----

Rule = Callable[[Mapping[str, Any], Mapping[str, frozenset[str]]], "tuple[DecisionPlan, str, str] | Escalate | None"]


def _flag(facts: Mapping[str, Any], key: str) -> bool:
    value = facts.get(key, False)
    if not isinstance(value, bool):
        raise _ConsultInvalid(f"{key} 必須是 bool")
    return value


def _count(facts: Mapping[str, Any], key: str) -> int:
    value = facts.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _ConsultInvalid(f"{key} 必須是非負整數")
    return value


def _rule_speak(facts: Mapping[str, Any], _scope: Mapping[str, frozenset[str]]):
    # 使用者在場且有結果可說才開口；使用者不在場延後；其餘不插話
    if _flag(facts, "user_present") and _flag(facts, "result_ready"):
        mode, reason = "respond", "speak_result_ready"
    elif not _flag(facts, "user_present"):
        mode, reason = "defer", "speak_user_absent"
    else:
        mode, reason = "pass", "speak_nothing_to_say"
    return DecisionPlan(reply=DecisionReply(mode=mode)), mode, reason


def _rule_output_modality(facts: Mapping[str, Any], scope: Mapping[str, frozenset[str]]):
    proposed = facts.get("proposed_modalities")
    if not isinstance(proposed, (list, tuple)) or not all(isinstance(m, str) for m in proposed):
        raise _ConsultInvalid("proposed_modalities 必須是字串清單")
    if not proposed:
        return None  # 沒有要改的 -> advice=∅，迴圈維持現狀
    if any(m not in OUTPUT_MODALITIES for m in proposed):
        raise _ConsultInvalid("proposed_modalities 含未知模態")
    if not set(proposed) <= scope["modalities"]:
        return Escalate(True, ESC_SCOPE_EXCEEDED)
    modalities = tuple(dict.fromkeys(proposed))
    plan = DecisionPlan(output=DecisionOutput(modalities=modalities, tts="audio_stream" in modalities))
    return plan, "output_set", "output_modality_in_scope"


def _rule_placement(_facts: Mapping[str, Any], _scope: Mapping[str, frozenset[str]]):
    return Escalate(True, ESC_PLACEMENT_REVIEW)  # 第一版最保守：一律交給 escalate（Q26 逐 kind 清單待 Bernie）


def _rule_ask_user(facts: Mapping[str, Any], _scope: Mapping[str, frozenset[str]]):
    # 有把握的預設值可自行決定（defer：不打擾使用者）；否則問
    mode, reason = ("defer", "ask_user_default_ok") if _flag(facts, "confident_default") else ("ask", "ask_user_unsure")
    return DecisionPlan(reply=DecisionReply(mode=mode)), mode, reason


def _rule_loop_end(facts: Mapping[str, Any], _scope: Mapping[str, frozenset[str]]):
    if _flag(facts, "goal_satisfied"):
        outcome, reason = "finalize", "loop_end_goal_met"
    elif _count(facts, "consults_used") >= MAX_CONSULTS:
        outcome, reason = "finalize", "loop_end_consult_cap"
    elif _count(facts, "no_progress_count") >= NO_PROGRESS_N or not _flag(facts, "made_progress"):
        outcome, reason = "finalize", "loop_end_no_progress"
    else:
        outcome, reason = "continue", "loop_end_progressing"
    return DecisionPlan(loop=DecisionLoop(outcome=outcome)), outcome, reason


def _rule_followup(facts: Mapping[str, Any], _scope: Mapping[str, frozenset[str]]):
    if _flag(facts, "report_pending"):
        plan = DecisionPlan(followups=(DecisionFollowup(kind="completion_report"),))
        return plan, "completion_report", "followup_report_pending"
    return DecisionPlan(followups=()), "none", "followup_not_needed"


DEFAULT_RULES: Mapping[str, Rule] = MappingProxyType({
    "speak": _rule_speak, "output_modality": _rule_output_modality, "placement": _rule_placement,
    "ask_user": _rule_ask_user, "loop_end": _rule_loop_end, "followup": _rule_followup,
})

# 每個 sub_kind 需要的 scope 鍵（缺漏即 fail-closed）；consult 本身需要 decision.consult 標籤
_REQUIRED_SCOPE: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "speak": ("capability_tags",), "output_modality": ("capability_tags", "modalities"),
    "placement": ("capability_tags",), "ask_user": ("capability_tags",),
    "loop_end": ("capability_tags",), "followup": ("capability_tags",),
})
CONSULT_TAG = "decision.consult"


def _validate_advice(sub_kind: str, plan: DecisionPlan) -> None:
    """規則輸出的縱深驗證：對應區段存在且取值屬閉集，其他區段必須為空。"""
    sections = {"reply": plan.reply, "output": plan.output, "placement": plan.placement, "loop": plan.loop,
                "followups": plan.followups}
    wanted = ADVICE_SECTION[sub_kind]
    if plan.route is not None or any(v is not None for k, v in sections.items() if k != wanted):
        raise _ConsultInvalid("advice 含非對應區段")
    if sections[wanted] is None:
        raise _ConsultInvalid("advice 缺對應區段")
    if sub_kind == "speak" and (plan.reply is None or plan.reply.mode not in SPEAK_MODES):
        raise _ConsultInvalid("speak.reply.mode 不合法")
    if sub_kind == "ask_user" and (plan.reply is None or plan.reply.mode not in ASK_USER_MODES):
        raise _ConsultInvalid("ask_user 只能是 defer／ask")
    if sub_kind == "loop_end" and (
        plan.loop is None or plan.loop.outcome not in LOOP_OUTCOMES or plan.loop.start
        or plan.loop.trigger_kind or plan.loop.goal_code or plan.loop.policy_ref or plan.loop.reeval_on
    ):
        raise _ConsultInvalid("loop_end 的 loop 只能填 outcome")
    if sub_kind == "output_modality" and (
        plan.output is None or not plan.output.modalities or any(m not in OUTPUT_MODALITIES for m in plan.output.modalities)
    ):
        raise _ConsultInvalid("output.modalities 不合法")
    if sub_kind == "followup" and (
        plan.followups is None or any(f.kind not in FOLLOWUP_KINDS for f in plan.followups)
    ):
        raise _ConsultInvalid("followups.kind 不合法")


class LoopConsultEngine:
    """``loop.consult`` 的規則 engine：同步、無 LLM、不拋例外。

    busy：同一 engine 同時只處理一個 consult，重入或並發的呼叫立即回 ``consult_busy``（不排隊）。
    逾時：規則本身是純記憶體運算；若超出 spec 預算（或 ``request.deadline`` 已過）則捨棄結果回 ``consult_timeout``
    （不另開執行緒搶佔，與 sync 規則 engine 的語意一致）。
    """

    def __init__(
        self,
        *,
        registry: DecisionPointRegistry | None = None,
        rules: Mapping[str, Rule] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registry = registry
        self._rules = dict(rules) if rules is not None else dict(DEFAULT_RULES)
        self._clock = clock
        self._lock = threading.Lock()

    @property
    def spec(self) -> DecisionPointSpec:
        return self._registry.get(KIND_LOOP_CONSULT) if self._registry is not None else LOOP_CONSULT_SPEC

    # -- consult：回傳工具層需要的形狀 --

    def consult(self, facts: Mapping[str, Any], *, deadline: float | None = None, fallback_key: str = "") -> ConsultResult:
        """永不拋例外。``fallback_key`` 只用於 facts 無法取得鍵時導出決定性 ID（如 request_id）。"""
        spec = self.spec
        try:
            key = self._identity(facts)
        except _ConsultInvalid:
            return self._fallback(
                derived_id("dec", KIND_LOOP_CONSULT, "invalid", fallback_key), None, spec.on_invalid_reason
            )
        sub_kind = key[3]
        decision_id = consult_decision_id(*key)
        if not self._lock.acquire(blocking=False):
            return self._fallback(decision_id, sub_kind, spec.on_busy_reason)
        try:
            started = self._clock()
            if deadline is not None and started >= deadline:
                return self._fallback(decision_id, sub_kind, spec.on_timeout_reason)
            try:
                result = self._run(facts, sub_kind, decision_id)
            except _ConsultInvalid as exc:
                logger.warning("loop.consult facts 不合法（%s）", exc)
                return self._fallback(decision_id, sub_kind, spec.on_invalid_reason)
            except Exception:  # engine 例外也不得外洩
                logger.exception("loop.consult engine 例外")
                return self._fallback(decision_id, sub_kind, spec.on_error_reason or spec.on_invalid_reason)
            budget_ms = spec.budget_ms
            elapsed_ms = (self._clock() - started) * 1000
            if (budget_ms is not None and budget_ms > 0 and elapsed_ms > budget_ms) or (
                deadline is not None and self._clock() > deadline
            ):
                return self._fallback(decision_id, sub_kind, spec.on_timeout_reason)
            return result
        finally:
            self._lock.release()

    @staticmethod
    def _identity(facts: Mapping[str, Any]) -> tuple[str, str, int, str]:
        if not isinstance(facts, Mapping):
            raise _ConsultInvalid("facts 必須是 mapping")
        sub_kind, run_id, step_id, ordinal = (facts.get(k) for k in ("sub_kind", "run_id", "step_id", "ordinal"))
        if sub_kind not in SUB_KINDS:
            raise _ConsultInvalid("sub_kind 不在閉集內")
        if not isinstance(run_id, str) or not run_id or not isinstance(step_id, str) or not step_id:
            raise _ConsultInvalid("run_id／step_id 必須是非空字串")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
            raise _ConsultInvalid("ordinal 必須是非負整數")
        return run_id, step_id, ordinal, sub_kind

    def _run(self, facts: Mapping[str, Any], sub_kind: str, decision_id: str) -> ConsultResult:
        if sub_kind == "placement":
            # 第一版一律 escalate，連 snapshot 都不看（最保守，且不因 snapshot 狀況改變 reason）
            return self._escalate(decision_id, sub_kind, ESC_PLACEMENT_REVIEW)
        scope = _effective_scope(facts.get("frozen"), facts.get("capability_snapshot"), _REQUIRED_SCOPE[sub_kind])
        if scope is None:
            return self._escalate(decision_id, sub_kind, ESC_SNAPSHOT_INVALID)
        if CONSULT_TAG not in scope["capability_tags"]:
            return self._escalate(decision_id, sub_kind, ESC_SCOPE_EXCEEDED)  # 連 consult 本身都不在交集內
        outcome = self._rules[sub_kind](facts, scope)
        if isinstance(outcome, Escalate):
            return self._escalate(decision_id, sub_kind, outcome.reason_code)
        if outcome is None:
            return ConsultResult(
                advice=None, reason_code="no_change", confidence=_RULE_CONFIDENCE, escalate=Escalate(),
                decision_id=decision_id, sub_kind=sub_kind, outcome="none",
            )
        plan, value, reason = outcome
        _validate_advice(sub_kind, plan)
        return ConsultResult(
            advice=plan, reason_code=reason, confidence=_RULE_CONFIDENCE, escalate=Escalate(),
            decision_id=decision_id, sub_kind=sub_kind, outcome=value,
        )

    @staticmethod
    def _escalate(decision_id: str, sub_kind: str, reason: str) -> ConsultResult:
        return ConsultResult(
            advice=None, reason_code=reason, confidence=_ESCALATE_CONFIDENCE,
            escalate=Escalate(True, reason), decision_id=decision_id, sub_kind=sub_kind, outcome="escalate",
        )

    @staticmethod
    def _fallback(decision_id: str, sub_kind: str | None, reason: str) -> ConsultResult:
        return ConsultResult(
            advice=None, reason_code=reason, confidence=0.0, escalate=Escalate(), decision_id=decision_id,
            sub_kind=sub_kind, fallback=True, outcome="none",
        )

    # -- DecisionEngine 協定（接 DecisionHub）--

    def decide(self, request: DecisionRequest) -> DecisionResult:
        """hub 入口：把 ``ConsultResult`` 包成 ``DecisionResult``（plan=advice；escalate 以 outcome=\"escalate\" 表達）。"""
        result = self.consult(request.facts, deadline=request.deadline, fallback_key=request.request_id)
        return DecisionResult(
            decision_id=result.decision_id, request_id=request.request_id, kind=request.kind, outcome=result.outcome,
            plan=result.advice, confidence=result.confidence, reason_code=result.reason_code,
            engine=DecisionEngineInfo(
                tier="fallback" if result.fallback else "rule", id=ENGINE_ID, version=ENGINE_VERSION
            ),
            fallback=result.fallback,
        )


def register_loop_consult(hub: DecisionHub, engine: LoopConsultEngine | None = None) -> LoopConsultEngine:
    """在 hub（及其 registry）登記 ``loop.consult``；engine 讀 hub 的 registry，spec 是預算與 reason 的唯一來源。"""
    engine = engine or LoopConsultEngine(registry=hub.registry)
    hub.register(LOOP_CONSULT_SPEC, engine)
    return engine
