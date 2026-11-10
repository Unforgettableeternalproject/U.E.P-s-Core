"""``turn.reply`` 離線重跑（DL-3）：把 TurnCandidate 序列（JSONL）餵給多個 proposer 並產出日誌形狀的列。

最小可行版：JSONL 每行一個 candidate 的 dict（欄位同 ``TurnCandidate``，``ctx`` 為巢狀 dict，``text`` 必須自帶，
因為 decision_log 不存原文）；``deadline`` 一律忽略。第一個 proposer 當 baseline（role=primary），其餘為 shadow，
輸出列可直接交給 ``replay.analyze``。重跑是同步、無預算、無狀態隔離：有內部狀態的 proposer（如 rules.default 的
coupling／context）依序餵入，結果等同依序回放，但不模擬時間（clock 取實際時間）。
"""
from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from uep.decision.log import COLUMNS, JsonlSink
from uep.ids import new_id
from uep.turn.models import Proposal, SessionContext, TurnCandidate
from uep.turn.proposer import validate_proposal
from uep.turn.registry import create_proposer

_CTX_FIELDS = {
    "conversation_id", "segment_id", "audience_scope", "session_ref", "scope_ref", "guild_id", "platform",
    "profile_actor_id",
}


def candidate_from_dict(data: dict[str, Any]) -> TurnCandidate:
    ctx = data.get("ctx") or {}
    # ctx.platform 若是舊 fixture 的 "web"，由 SessionContext 建構時的相容層正規化成 "website"（DQ-3-B）
    session = SessionContext(**{k: v for k, v in ctx.items() if k in _CTX_FIELDS})
    return TurnCandidate(
        candidate_id=data["candidate_id"],
        utterance_refs=tuple(data.get("utterance_refs") or ()),
        speakers=tuple(data.get("speakers") or ()),
        ctx=session,
        interaction_state=data.get("interaction_state"),
        interaction_version=data.get("interaction_version"),
        presence_active=bool(data.get("presence_active", True)),
        text=data.get("text", ""),
        segments=int(data.get("segments", 1)),
        participant_count=data.get("participant_count"),
        addressed_hint=bool(data.get("addressed_hint", False)),
    )


def candidate_to_dict(candidate: TurnCandidate, *, include_text: bool = True) -> dict[str, Any]:
    """TurnCandidate -> JSONL 用 dict（不含 deadline）；供匯出重放輸入。"""
    ctx = {k: getattr(candidate.ctx, k) for k in _CTX_FIELDS}
    out: dict[str, Any] = {
        "candidate_id": candidate.candidate_id, "utterance_refs": list(candidate.utterance_refs),
        "speakers": list(candidate.speakers), "ctx": ctx, "interaction_state": candidate.interaction_state,
        "interaction_version": candidate.interaction_version, "presence_active": candidate.presence_active,
        "segments": candidate.segments, "participant_count": candidate.participant_count,
        "addressed_hint": candidate.addressed_hint,
    }
    if include_text:
        out["text"] = candidate.text
    return out


def _row(candidate: TurnCandidate, proposer_id: str, proposal: Proposal | None, status: str, elapsed_ms: int,
         *, request_id: str, primary: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    row: dict[str, Any] = dict.fromkeys(COLUMNS)
    outcome = proposal.reply_mode.value if proposal is not None else None
    row.update(
        decision_id=new_id("dec"), request_id=request_id, kind="turn.reply", created_at=now,
        role="shadow" if primary is not None else "primary",
        primary_decision_id=primary["decision_id"] if primary is not None else None,
        proposer_id=proposal.proposer_id if proposal is not None and proposal.proposer_id != "unknown" else proposer_id,
        version=proposal.proposer_version if proposal is not None else None,
        outcome=outcome, reason_code=proposal.reason_code if proposal is not None else None,
        confidence=proposal.confidence if proposal is not None else None,
        elapsed_ms=elapsed_ms, status=status,
        candidate_id=candidate.candidate_id, participant_count=candidate.participant_count,
        addressed_hint=candidate.addressed_hint, utterance_refs=list(candidate.utterance_refs),
    )
    if primary is not None:
        row["agree"] = outcome is not None and outcome == primary["outcome"]
    else:
        row["engine"], row["fallback"] = "rule", status != "ok"
    return row


def rerun_candidates(candidates: Sequence[dict[str, Any]], proposer_ids: Sequence[str]) -> list[dict[str, Any]]:
    """依序把每個 candidate 餵給所有 proposer；回傳日誌形狀的列。baseline 失敗時 shadow 仍照常記錄（無對照則 agree=False）。"""
    if not proposer_ids:
        raise ValueError("至少需要一個 proposer（第一個為 baseline）")
    proposers = [(pid, create_proposer(pid)) for pid in proposer_ids]
    rows: list[dict[str, Any]] = []
    for data in candidates:
        candidate = candidate_from_dict(data)
        request_id = new_id("req")
        now = datetime.now(UTC)
        primary: dict[str, Any] | None = None
        for index, (pid, proposer) in enumerate(proposers):
            started = time.monotonic()
            proposal: Proposal | None = None
            status = "ok"
            try:
                proposal = proposer.propose(candidate)
                if validate_proposal(proposal) is not None:
                    proposal, status = None, "invalid"
            except Exception:  # noqa: BLE001 - 單一 proposer 失敗只記錄
                status = "error"
            row = _row(candidate, pid, proposal, status, int((time.monotonic() - started) * 1000),
                       request_id=request_id, primary=primary, now=now)
            if index == 0:
                primary = row
            rows.append(row)
    return rows


def write_rows(rows: Sequence[dict[str, Any]], path: str | Path) -> None:
    JsonlSink(path).write(rows)
