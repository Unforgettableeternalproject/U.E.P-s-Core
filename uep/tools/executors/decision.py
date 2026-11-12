"""``decision.consult`` 的 executor 薄層（Agent 架構提案 §4.9）：把工具參數組成 ``loop.consult`` 的 facts，呼叫決策中樞。

- 這裡不含決策邏輯：規則、範圍交集（CN-1）、fail-closed 都在 ``uep.decision.loop_consult``；本層只做 facts 組裝與結果形狀轉換。
- 永不拋例外：中樞逾時／busy／invalid／例外一律變成 ``advice=∅``（附 ``default`` 預設值）的 ok 結果（CN-6），範圍不放寬。
- 結果放在 ``ConsultResults``（回合外、記憶體）：AgentLoop 讀它判斷 escalate（CN-2、CN-3），Gateway 的 ``content_lookup``
  從它取要回餵給模型的文字。receipt 只記 ``result_ref``，結果原文不進 receipt／DB（D33）。
- 重放：同 ``(run_id, step_id, ordinal, sub_kind)`` 不重算（回同一個結果與 ``decision_id``）；``decision_id`` 本身也由
  ``consult_decision_id`` 決定性導出，所以 ``ConsultResults`` 遺失（崩潰後）重算也得到同一個 ID。**只保證 ID 一致，不保證
  advice 一致**（規則或快照變了，重算的 advice 可能不同）。
- 生命週期：Run 終態時由 AgentLoop 呼叫 ``discard_run`` 清除；另有容量上限（``max_entries``，淘汰最舊）。結果被清掉後，若同一個
  action 被重放，Gateway 的 ``content_lookup`` 取不到內容 ⇒ 該項視圖為 ``unknown`` ⇒ 迴圈把 Run 收成 ``FAILED(unresolved_actions)``
  （不編造內容，fail-closed）。
"""
from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from uep.adapters.tool import ReceiptLookup, ToolCall, ToolReceipt
from uep.decision.contracts import DecisionRequest, plan_to_dict
from uep.decision.hub import DecisionHub
from uep.decision.loop_consult import (
    ADVICE_SECTION,
    FAILSAFE_DEFAULTS,
    KIND_LOOP_CONSULT,
    ON_INVALID_REASON,
    consult_decision_id,
)
from uep.decision.loop_digest import LOOP_DIGEST_KEY, sanitize_digest
from uep.ids import new_id
from uep.tools.catalog.decision import INJECTED_CONSULT

logger = logging.getLogger(__name__)
DEFAULT_MAX_ENTRIES = 1024
_SCOPE_KEYS = ("capability_tags", "modalities", "platforms")


class ConsultResults:
    """consult 結果暫存：以 action_id 取用，並以決定性身分去重（重放不重算）。"""

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self._lock = threading.Lock()
        self._by_action: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._by_identity: dict[tuple[str, str, int, str], dict[str, Any]] = {}
        self._receipts: dict[str, ToolReceipt] = {}
        self._run_of: dict[str, str] = {}
        self._max_entries = max_entries

    def get(self, action_id: str) -> dict[str, Any] | None:
        with self._lock:
            found = self._by_action.get(action_id)
            return dict(found) if found is not None else None

    def content(self, action_id: str) -> str | None:
        """回餵給模型的文字（緊湊 JSON，只含 advice／原因碼／ID，沒有使用者原文）；沒有紀錄回 None。"""
        found = self.get(action_id)
        return None if found is None else json.dumps(found, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def identity(self, key: tuple[str, str, int, str]) -> dict[str, Any] | None:
        with self._lock:
            return self._by_identity.get(key)

    def put(self, action_id: str, key: tuple[str, str, int, str] | None, payload: dict[str, Any]) -> None:
        with self._lock:
            self._by_action[action_id] = dict(payload)
            if key is not None:
                self._by_identity[key] = dict(payload)
                self._run_of[action_id] = key[0]
            while len(self._by_action) > self._max_entries:  # 容量上限：淘汰最舊的（重放走 unresolved_actions，見模組說明）
                oldest, _ = self._by_action.popitem(last=False)
                self._receipts.pop(oldest, None)
                self._run_of.pop(oldest, None)
            while len(self._by_identity) > self._max_entries:
                self._by_identity.pop(next(iter(self._by_identity)))

    def discard_run(self, run_id: str) -> None:
        """Run 進入終態：清掉該 Run 的所有 consult 結果、receipt 查核表與去重鍵。"""
        with self._lock:
            for action_id in [a for a, r in self._run_of.items() if r == run_id]:
                self._by_action.pop(action_id, None)
                self._receipts.pop(action_id, None)
                self._run_of.pop(action_id, None)
            for key in [k for k in self._by_identity if k[0] == run_id]:
                self._by_identity.pop(key, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_action)

    def receipt(self, action_id: str) -> ToolReceipt | None:
        with self._lock:
            return self._receipts.get(action_id)

    def put_receipt(self, action_id: str, receipt: ToolReceipt) -> None:
        with self._lock:
            self._receipts[action_id] = receipt


class ConsultToolExecutor:
    """滿足 ``uep.adapters.tool.ToolExecutor``。"""

    def __init__(self, hub: DecisionHub, results: ConsultResults | None = None) -> None:
        self._hub = hub
        self.results = results if results is not None else ConsultResults()

    @property
    def lookup_authoritative(self) -> bool:
        return False

    def lookup_receipt(self, action_id: str) -> ReceiptLookup:
        receipt = self.results.receipt(action_id)
        return ReceiptLookup("found", receipt) if receipt is not None else ReceiptLookup("undetermined")

    def execute(self, call: ToolCall) -> ToolReceipt:
        existing = self.results.receipt(call.action_id)
        if existing is not None and call.idempotency in ("native", "via_action_id"):
            return existing
        params = dict(call.params) if isinstance(call.params, Mapping) else {}
        key = self._identity(params)
        payload = self.results.identity(key) if key is not None else None
        if payload is None:
            payload = self._decide(call.action_id, params, key)
        self.results.put(call.action_id, key, payload)
        receipt = ToolReceipt(
            receipt_id=new_id("rcpt"), action_id=call.action_id, succeeded=True, result_ref=f"blob_{call.action_id}",
            recorded_at=datetime.now(UTC), source_verified=True,
            meta={"status": "executed", "decision_id": str(payload.get("decision_id", ""))[:64] or "none"},  # CN-9
        )
        self.results.put_receipt(call.action_id, receipt)
        return receipt

    # ---- 內部 ----

    @staticmethod
    def _identity(params: Mapping[str, Any]) -> tuple[str, str, int, str] | None:
        run_id, step_id, ordinal, sub_kind = (params.get(k) for k in ("run_id", "step_id", "ordinal", "sub_kind"))
        if (
            isinstance(run_id, str) and run_id and isinstance(step_id, str) and step_id and isinstance(sub_kind, str)
            and isinstance(ordinal, int) and not isinstance(ordinal, bool) and ordinal >= 0
        ):
            return run_id, step_id, ordinal, sub_kind
        return None

    @staticmethod
    def _facts(params: Mapping[str, Any]) -> dict[str, Any]:
        facts: dict[str, Any] = {k: v for k, v in params.items() if k not in INJECTED_CONSULT}
        digest = sanitize_digest(params.get(LOOP_DIGEST_KEY))  # 選填；一律經 sanitize，非法／缺漏視為沒有
        if digest is not None:
            facts[LOOP_DIGEST_KEY] = digest
        for name in ("sub_kind", "run_id", "step_id", "ordinal", "consults_used", "no_progress_count"):
            if name in params:
                facts[name] = params[name]
        facts["frozen"] = {k: list(params.get(f"frozen_{k}") or ()) for k in _SCOPE_KEYS}
        facts["capability_snapshot"] = {k: list(params.get(f"snapshot_{k}") or ()) for k in _SCOPE_KEYS}
        return facts

    def _decide(self, action_id: str, params: Mapping[str, Any], key: tuple[str, str, int, str] | None) -> dict[str, Any]:
        sub_kind = str(params.get("sub_kind", ""))
        request = DecisionRequest(request_id=action_id, kind=KIND_LOOP_CONSULT, facts=self._facts(params))
        try:
            result = self._hub.decide(KIND_LOOP_CONSULT, request)
        except Exception:  # 中樞失敗不得外洩，也不得放寬範圍（CN-6）
            logger.exception("loop.consult 呼叫中樞失敗")
            decision_id = consult_decision_id(*key) if key is not None else new_id("dec")
            return self._payload(sub_kind, None, ON_INVALID_REASON, 0.0, False, "", decision_id, True)
        escalate = result.outcome == "escalate"
        advice = None
        if result.plan is not None and sub_kind in ADVICE_SECTION:
            section = ADVICE_SECTION[sub_kind]
            body = plan_to_dict(result.plan).get(section)
            advice = {section: body} if body is not None else None
        return self._payload(
            sub_kind, advice, result.reason_code, result.confidence, escalate, result.reason_code if escalate else "",
            result.decision_id, bool(result.fallback),
        )

    @staticmethod
    def _payload(
        sub_kind: str, advice: dict[str, Any] | None, reason: str, confidence: float, escalate: bool, esc_reason: str,
        decision_id: str, fallback: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sub_kind": sub_kind, "advice": advice, "reason_code": reason, "confidence": confidence,
            "escalate": {"needed": escalate, "reason_code": esc_reason}, "decision_id": decision_id, "fallback": fallback,
        }
        if advice is None and not escalate and sub_kind in FAILSAFE_DEFAULTS:
            payload["default"] = FAILSAFE_DEFAULTS[sub_kind]  # advice=∅：迴圈與模型看到的保守預設
        return json.loads(json.dumps(payload))  # tuple -> list，確保可 JSON 序列化
