"""echo_memory EventDraft -> uep 轉譯（P3接入方案 §2.2 scope_ref 轉譯；S2 MemoryHost 會有正式實作）。

S1 只需要一份契約基準：把記憶端範例 JSON 轉成 uep 的 (EventDraft, TrustedContext)，
確認註冊表與 Gateway 接受。這裡不 import echo_memory，範例以 fixture JSON 提供。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from uep.events.envelope import EventDraft, TrustedContext

FIXTURE = Path(__file__).parent / "fixtures" / "echo_memory_drafts.json"


def load_samples() -> dict[str, list[dict[str, Any]]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def translate(raw: dict[str, Any]) -> tuple[EventDraft, TrustedContext]:
    """scope_ref -> TrustedContext；expires_at 搬到信封；summary 轉入可刪 blob（D78）；其餘原樣。"""
    payload = dict(raw["payload"])
    expires_at = payload.pop("expires_at", None)
    summary = payload.pop("summary", None)
    draft = EventDraft(
        event_type=raw["event_type"],
        idempotency_key=raw["idempotency_key"],
        payload=payload,
        expires_at=datetime.fromisoformat(expires_at) if expires_at else None,
        payload_blob=summary.encode("utf-8") if summary else None,
    )
    trusted = TrustedContext(
        source="core.memory", actor_id="uep:memory", scope_ref=raw["scope_ref"], external=True
    )
    return draft, trusted
