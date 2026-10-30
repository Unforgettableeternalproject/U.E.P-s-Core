"""echo_memory EventDraft -> uep 轉譯與 sink（P3接入方案 §2.2 scope_ref 轉譯、§3.1 事件）。

記憶端 emitter 在 daemon 執行緒呼叫 ``sink.emit(draft)``、不讀回傳值：本 sink thread-safe、永不拋例外，
發布失敗只記 log 與計數。

轉譯規則（記憶端不改）：
- ``scope_ref`` 不信任 echo 的字串（它以 namespace id 組成，可含 ``-``，與 uep 的 scope 字元集不相容），
  改由 Host 的 ``namespace -> owner_id`` 映射重建 ``scope_private_<owner_id>``（D77）；映射缺失、
  ``privacy_scope`` 不是 private 一律丟棄並計數。
- ``source=core.memory``、``actor_id=uep:memory``、``external=True``；``idempotency_key`` 原樣沿用（內容雜湊）。
- ``expires_at`` 從 payload 搬到信封欄位；``summary`` 轉入可刪的 payload_blob（D78），不進 journal 的 payload。
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from uep.events.envelope import EventDraft, TrustedContext
from uep.events.publisher import EventPublisher
from uep.events.registry import MEMORY_EVENT_TYPES
from uep.memory.metrics import MemoryMetrics
from uep.memory.namespaces import owner_scope_ref

logger = logging.getLogger("uep.memory.sink")

MEMORY_SOURCE = "core.memory"
MEMORY_ACTOR = "uep:memory"
TRIGGER_OBSERVED = "proactive.trigger_observed"
_ACCEPTED_TYPES = frozenset(MEMORY_EVENT_TYPES) | {TRIGGER_OBSERVED}


class TranslateError(ValueError):
    """草稿無法轉譯（原因碼在 ``code``）；sink 會丟棄並以此計數。"""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def _field(draft: Any, name: str) -> Any:
    return draft.get(name) if isinstance(draft, Mapping) else getattr(draft, name, None)


def translate_draft(draft: Any, owner_id: str | None) -> tuple[EventDraft, TrustedContext]:
    """echo ``EventDraft``（物件或 dict）-> (uep EventDraft, TrustedContext)；不合規拋 ``TranslateError``。"""
    event_type = _field(draft, "event_type")
    if event_type not in _ACCEPTED_TYPES:
        raise TranslateError("unknown_type", f"不接受的事件類型：{event_type!r}")
    if owner_id is None:
        raise TranslateError("no_owner", "此 namespace 沒有 owner 映射")
    key = _field(draft, "idempotency_key")
    if not isinstance(key, str) or not key:
        raise TranslateError("no_idempotency_key")
    payload = dict(_field(draft, "payload") or {})
    if payload.get("privacy_scope", "private") != "private":
        raise TranslateError("privacy_scope", "只接受 private 範圍的記憶事件")
    expires_at = payload.pop("expires_at", None)
    summary = payload.pop("summary", None)
    try:
        expires = datetime.fromisoformat(expires_at) if expires_at else None
    except (TypeError, ValueError) as exc:
        raise TranslateError("bad_expires_at") from exc
    translated = EventDraft(
        event_type=event_type,
        idempotency_key=key,
        payload=payload,
        expires_at=expires,
        payload_blob=summary.encode("utf-8") if isinstance(summary, str) and summary else None,
    )
    trusted = TrustedContext(
        source=MEMORY_SOURCE, actor_id=MEMORY_ACTOR, scope_ref=owner_scope_ref(owner_id), external=True
    )
    return translated, trusted


class MemorySink:
    """單一 namespace 的 ``CandidateSink``。``on_dream_completed(namespace_id, draft)`` 在 dream_completed
    首次落地後呼叫（Host 用它替排程觸發的 dream 補 Activity）。"""

    def __init__(
        self,
        publisher: EventPublisher,
        namespace_id: str,
        owner_id: str | None,
        *,
        metrics: MemoryMetrics | None = None,
        on_dream_completed: Callable[[str, Any], None] | None = None,
    ) -> None:
        self._publisher = publisher
        self.namespace_id = namespace_id
        self.owner_id = owner_id
        self._metrics = metrics or MemoryMetrics()
        self._on_dream_completed = on_dream_completed

    def emit(self, draft: Any) -> None:
        try:
            translated, trusted = translate_draft(draft, self.owner_id)
        except TranslateError as exc:
            self._metrics.inc(f"memory.sink.dropped.{exc.code}")
            logger.warning("memory sink 丟棄草稿（%s）：namespace=%s", exc.code, self.namespace_id)
            return
        except Exception:
            self._metrics.inc("memory.sink.dropped.invalid")
            logger.warning("memory sink 草稿不合規：namespace=%s", self.namespace_id, exc_info=True)
            return
        try:
            recorded = self._publisher.publish(translated, trusted)
        except Exception:
            self._metrics.inc("memory.sink.publish_failed")
            logger.exception("memory sink 發布失敗（已忽略，記憶端不受影響）：%s", translated.event_type)
            return
        if recorded.duplicate:
            self._metrics.inc("memory.sink.duplicate")
            return
        self._metrics.inc("memory.sink.published")
        if translated.event_type == "memory.dream_completed" and self._on_dream_completed is not None:
            try:
                self._on_dream_completed(self.namespace_id, draft)
            except Exception:
                logger.exception("on_dream_completed 失敗（已忽略）")
