"""``proactive.initiate`` 離線重跑（DL-6a，Q-A）：把候選 fixture（JSONL）餵給規則 engine，產出日誌形狀的列。

每行一筆記錄：``{"id", "candidate": {...}, "context": {...}, "expect": {...}}``。``candidate``／``context`` 是對預設值的淺層覆寫
（巢狀的 ``grant``／``policy`` 一層合併；``grant`` 設為 ``null`` 代表沒有授權），所以可以描述格式錯誤的候選（走 ``candidate_malformed``）。
預設值固定時間 ``FIXED_NOW``，重跑是同步、純函式、可重現。輸出列可直接交給 ``replay.analyze``。
"""
from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from uep.decision.contracts import DecisionRequest, DecisionResult
from uep.decision.log import JsonlSink, primary_row
from uep.decision.proactive import (
    KIND_PROACTIVE_INITIATE,
    ProactiveEngine,
    proactive_initiate_spec,
)

FIXED_NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)
NAMESPACE = "ns_main"

_BASE_CANDIDATE: dict[str, Any] = {
    "candidate_id": "pcand_base", "trigger_event_ids": ["evt_trigger"], "rule_version": "rv1",
    "goal_ref": "remind_due", "evidence_refs": [], "memory_salience": 0.6, "urgency": "normal", "confidence": 0.8,
    "privacy_scope": "scope_private", "expires_at": "2026-11-10T13:00:00+00:00", "dedupe_key": "remind:task1:owner",
    "source_kind": "schedule", "owner_actor_id": "actor_owner", "created_at": "2026-11-10T11:55:00+00:00",
    "interruptibility_cost": None, "desired_location": None, "decision_reason": None,
    "authorization_ref": "grant_1", "urgency_source": "event",
}
_BASE_CONTEXT: dict[str, Any] = {
    "now": FIXED_NOW.isoformat(), "owner_actor_id": "actor_owner", "audience_scope": "scope_private",
    "presence_status": "ACTIVE", "current_platform": "desktop", "presence_generation": 1, "transfer_pending": False,
    "private_locations": ["desktop"],
    "grant": {
        "valid": True, "revoked": False, "scope_refs": ["scope_private"], "locations": ["desktop", "discord"],
        "may_choose_location": False, "may_move": False,
    },
    "global_stop": False, "muted": False, "in_committed_interaction": False, "activity_interruptible": True,
    "activity_cost": 0.0, "ledger": [], "budget_used": 0,
    "policy": {
        "version": 1, "policy_ref": "proactive_default", "confidence_floor": 0.5, "cooldown_s": 600, "budget_max": 5,
        "short_retry_ms": 5000, "checkpoint_retry_ms": 30000, "budget_retry_ms": 300000, "queued_retry_ms": 60000,
    },
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for key, value in override.items():
        if key in ("grant", "policy") and isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = {**out[key], **value}
        else:
            out[key] = value
    return out


def request_from_record(record: dict[str, Any], *, seq: int = 1) -> DecisionRequest:
    """記錄 -> 請求；``facts`` 不經 dataclass 驗證，格式錯誤的候選由 engine 判 ``candidate_malformed``。"""
    candidate = _merge(_BASE_CANDIDATE, record.get("candidate") or {})
    context = _merge(_BASE_CONTEXT, record.get("context") or {})
    cand_id = str(candidate.get("candidate_id") or "")
    return DecisionRequest(
        request_id=f"{cand_id}:eval:{seq}", kind=KIND_PROACTIVE_INITIATE, trigger="event",
        subject={"candidate_id": cand_id, "namespace": NAMESPACE},
        facts={"candidate": candidate, "context": context},
        allowed_outcomes=proactive_initiate_spec().outcomes,
        snapshot_versions={"policy": context["policy"]["version"], "presence": context["presence_generation"]},
    )


def run_records(
    records: Sequence[dict[str, Any]], engine: ProactiveEngine | None = None,
) -> list[tuple[dict[str, Any], DecisionRequest, DecisionResult]]:
    engine = engine or ProactiveEngine()
    out = []
    for index, record in enumerate(records, 1):
        request = request_from_record(record, seq=index)
        out.append((record, request, engine.decide(request)))
    return out


def rerun_records(records: Sequence[dict[str, Any]], engine: ProactiveEngine | None = None) -> list[dict[str, Any]]:
    """依序重跑；回傳日誌形狀的 primary 列（不含原文）。"""
    spec = proactive_initiate_spec()
    return [
        primary_row(request, result, spec, 0, now=FIXED_NOW) for _, request, result in run_records(records, engine)
    ]


def write_rows(rows: Sequence[dict[str, Any]], path: str | Path) -> None:
    JsonlSink(path).write(rows)
