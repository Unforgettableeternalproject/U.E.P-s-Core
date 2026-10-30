"""echo_memory 範例草稿的測試端輔助：正式轉譯已移入 ``uep.memory.sink``（S2），這裡只負責載入 fixture。

fixture 的草稿都是 ``scope_private_owner``，所以固定以 owner_id=owner 轉譯。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from uep.events.envelope import EventDraft, TrustedContext
from uep.memory.sink import translate_draft

FIXTURE = Path(__file__).parent / "fixtures" / "echo_memory_drafts.json"


def load_samples() -> dict[str, list[dict[str, Any]]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def translate(raw: dict[str, Any]) -> tuple[EventDraft, TrustedContext]:
    return translate_draft(raw, owner_id="owner")
