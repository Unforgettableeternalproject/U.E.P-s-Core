"""
Memory EventDraft — Echo 對 Core 的最小事件契約（M6，對齊 Core契約草案 §0.6／CC-1／E-20）

純資料：不 import 任何寫入端、Core、網路或子行程模組。
schema 集中在本檔，Core 契約調整只需改這裡。

設計約束：
  - Echo 只送 EventDraft：event_type、payload、idempotency_key、scope_ref 四個欄位。
    event_id、received_at、source、priority、actor_id 等信任欄位由 Core Gateway 填，
    Echo 端 schema 不得出現這些欄位。
  - 事件只描述「已發生」的 Gate 狀態轉移或圖譜結構變化，不含任何指令。
  - evidence_refs 必填；沒有引用就不發。weight 只用於排序，不是門檻（0 與 1 都會發）。
  - payload 只放 ID、狀態、計數、權重；不得帶私人原文。
  - 事件代表「當時發生」：送出後不撤回；之後的 rollback 不會產生補償事件，
    Core 端須自行以 evidence_refs 回查現況。
  - idempotency_key 含轉移識別（假說紀錄版本、工具呼叫 id 等）：每次真實轉移 key 不同，
    重放同一轉移 key 相同。
  - ProactiveCandidate 以 proactive.trigger_observed 送出（trigger_kind=memory_*），
    只有描述性欄位，不含 urgency／interruptibility_cost／desired_location／goal_ref。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

SCHEMA_VERSION = 1

# 事件種類（只增不改；每種各自獨立的 event_type，不併入 memory.consolidated）
KIND_DREAM_COMPLETED = "dream_completed"
KIND_REFLECTION_COMPLETED = "reflection_completed"
KIND_HYPOTHESIS_SUPPORTED = "hypothesis_supported"
KIND_HYPOTHESIS_CONTRADICTED = "hypothesis_contradicted"
KIND_CONTRADICTION_FOUND = "contradiction_found"
KIND_EPISODE_FLAGGED = "episode_flagged"
KIND_SELF_OBSERVATION_PROPOSED = "self_observation_proposed"

EVENT_KINDS = frozenset({
    KIND_DREAM_COMPLETED,
    KIND_REFLECTION_COMPLETED,
    KIND_HYPOTHESIS_SUPPORTED,
    KIND_HYPOTHESIS_CONTRADICTED,
    KIND_CONTRADICTION_FOUND,
    KIND_EPISODE_FLAGGED,
    KIND_SELF_OBSERVATION_PROPOSED,
})

MEMORY_EVENT_PREFIX = "memory."
EVENT_TYPE_TRIGGER_OBSERVED = "proactive.trigger_observed"
TRIGGER_KIND_PREFIX = "memory_"

# 隱私範圍：目前 Echo 端只會從擁有者 namespace 發出 private 事件
PRIVACY_PRIVATE = "private"
PRIVACY_SCOPES = frozenset({PRIVACY_PRIVATE})

# idempotency_key／ScopeRef id 的字元集與長度（E-20）：[A-Za-z0-9_:.-]、≤256
_SAFE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:.-"
)
MAX_IDEMPOTENCY_KEY_LEN = 256

# Echo 端 schema 不得包含的信任欄位（由 Core Gateway 填）
TRUST_FIELDS = frozenset({
    "event_id", "received_at", "source", "priority", "actor_id", "journal_seq", "persona_id",
})


def is_safe_token(value: Any, max_len: int = MAX_IDEMPOTENCY_KEY_LEN) -> bool:
    """字元集 [A-Za-z0-9_:.-]、長度 1..max_len（不用 regex，沒有結尾換行漏洞）。"""
    return isinstance(value, str) and 0 < len(value) <= max_len and set(value) <= _SAFE_CHARS


def clamp_weight(value: Any) -> float:
    """把 weight 夾到 [0, 1]；無法轉換視為 0.0。只供排序，不是門檻。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if v != v:  # NaN
        return 0.0
    return max(0.0, min(1.0, v))


@dataclass(frozen=True)
class ScopeRef:
    """memory.scope／scope_ref 同一型別（§0.6）：level ∈ {private, group, public}。"""

    level: str
    owner_id: str | None = None
    group_id: str | None = None

    def serialize(self) -> str:
        """序列化字串，例 ``scope_private_owner``（level=private, owner_id=owner）。"""
        if self.level == "private" and self.owner_id and is_safe_token(self.owner_id, 128):
            return f"scope_private_{self.owner_id}"
        if self.level == "group" and self.group_id and is_safe_token(self.group_id, 128):
            return f"scope_group_{self.group_id}"
        if self.level == "public":
            return "scope_public"
        raise ValueError(f"無效的 ScopeRef: {self!r}")


def scope_ref_for(namespace_id: str, privacy_scope: str = PRIVACY_PRIVATE) -> str:
    """由 namespace_id／privacy_scope 映射 scope_ref。Echo 只發 private：owner 為 namespace。"""
    if privacy_scope != PRIVACY_PRIVATE:
        raise ValueError(f"不支援的 privacy_scope: {privacy_scope!r}")
    return ScopeRef("private", owner_id=namespace_id).serialize()


def compute_idempotency_key(
    kind: str,
    namespace_id: str,
    evidence_refs: tuple[str, ...] | list[str],
    derived_from: tuple[str, ...] | list[str] = (),
    schema_version: int = SCHEMA_VERSION,
    prefix: str = "mem",
) -> str:
    """內容雜湊 idempotency_key：同 kind、namespace、引用、來源必得同一把（冪等）。"""
    payload = json.dumps(
        {
            "k": kind,
            "ns": namespace_id,
            "e": sorted(str(x) for x in evidence_refs),
            "d": sorted(str(x) for x in derived_from),
            "v": schema_version,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"{prefix}:{kind}:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class EventDraft:
    """Echo 送給 Core Gateway 的事件草稿；只有這四個欄位，信任欄位由 Gateway 填。"""

    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""
    scope_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "payload": dict(self.payload),
            "idempotency_key": self.idempotency_key,
            "scope_ref": self.scope_ref,
        }


def build_memory_draft(
    kind: str,
    namespace_id: str,
    evidence_refs: tuple[str, ...],
    derived_from: tuple[str, ...] = (),
    weight: float = 0.0,
    privacy_scope: str = PRIVACY_PRIVATE,
) -> EventDraft:
    """組 memory.<kind> 草稿。kind 不合法、引用為空、key 不合規一律拋 ValueError。"""
    if kind not in EVENT_KINDS:
        raise ValueError(f"未知事件種類: {kind!r}")
    if not evidence_refs:
        raise ValueError("evidence_refs 必填")
    key = compute_idempotency_key(kind, namespace_id, evidence_refs, derived_from)
    if not is_safe_token(key):
        raise ValueError("idempotency_key 不合規")
    return EventDraft(
        event_type=f"{MEMORY_EVENT_PREFIX}{kind}",
        payload={
            "evidence_refs": list(evidence_refs),
            "derived_from": list(derived_from),
            "weight": clamp_weight(weight),
            "privacy_scope": privacy_scope,
            "schema_version": SCHEMA_VERSION,
        },
        idempotency_key=key,
        scope_ref=scope_ref_for(namespace_id, privacy_scope),
    )


def build_candidate_draft(
    kind: str,
    namespace_id: str,
    source_keys: tuple[str, ...],
    dedupe_key: str,
    evidence_refs: tuple[str, ...],
    memory_weight: float,
    confidence: float,
    expires_at: datetime,
    summary: str,
    privacy_scope: str = PRIVACY_PRIVATE,
    transition_ref: str = "",
) -> EventDraft:
    """組 proactive.trigger_observed 候選草稿（trigger_kind=memory_<kind>）。summary 純描述。"""
    if kind not in EVENT_KINDS:
        raise ValueError(f"未知事件種類: {kind!r}")
    if not evidence_refs:
        raise ValueError("evidence_refs 必填")
    key = compute_idempotency_key(
        kind, namespace_id, evidence_refs, (dedupe_key, transition_ref), prefix="memc",
    )
    if not is_safe_token(key):
        raise ValueError("idempotency_key 不合規")
    return EventDraft(
        event_type=EVENT_TYPE_TRIGGER_OBSERVED,
        payload={
            "trigger_kind": f"{TRIGGER_KIND_PREFIX}{kind}",
            "dedupe_key": dedupe_key,
            "source_idempotency_keys": list(source_keys),
            "evidence_refs": list(evidence_refs),
            "memory_weight": clamp_weight(memory_weight),
            "confidence": clamp_weight(confidence),
            "privacy_scope": privacy_scope,
            "expires_at": expires_at.isoformat(),
            "summary": summary,
            "schema_version": SCHEMA_VERSION,
        },
        idempotency_key=key,
        scope_ref=scope_ref_for(namespace_id, privacy_scope),
    )


@runtime_checkable
class CandidateSink(Protocol):
    """宿主（Core）注入的接收端：單一 emit(draft)。Echo 不讀回傳值，也不依賴它成功。

    事件與候選共用同一個方法，由 draft.event_type 區分（memory.* 或 proactive.trigger_observed）。
    """

    def emit(self, draft: EventDraft) -> Any: ...
