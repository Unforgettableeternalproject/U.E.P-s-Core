"""
Self Model 條目的唯讀判斷（M6）：純函式，不寫入任何東西。

寫入只能經 echo_memory.affect.self_model_gate 的 Gate action；本檔只定義標記格式與讀取端判斷。
"""

from __future__ import annotations

import hashlib
from typing import Any

SELF_MODEL_KEY = "self_model"
VERIFICATION_UNVERIFIED = "unverified"
VERIFICATION_VERIFIED = "verified"
VERIFICATIONS = (VERIFICATION_UNVERIFIED, VERIFICATION_VERIFIED)


def description_digest(description: str) -> str:
    """description 指紋：標記當時的描述；之後描述被改動，條目不再算已驗證。"""
    return hashlib.sha256((description or "").encode("utf-8")).hexdigest()[:16]


def _mark(concept: Any) -> dict[str, Any] | None:
    metadata = getattr(concept, "metadata", None)
    mark = metadata.get(SELF_MODEL_KEY) if isinstance(metadata, dict) else None
    return mark if isinstance(mark, dict) else None


def is_self_model_concept(concept: Any) -> bool:
    """概念是否為合法的 Self Model 條目：subject=agent、標記帶非空 evidence_refs、verification 合法。"""
    mark = _mark(concept)
    if getattr(concept, "subject", "") != "agent" or mark is None:
        return False
    refs = mark.get("evidence_refs")
    return (
        isinstance(refs, list)
        and bool(refs)
        and all(isinstance(r, str) and r for r in refs)
        and mark.get("verification") in VERIFICATIONS
    )


def effective_verification(concept: Any) -> str:
    """實際生效的驗證狀態：標記為 verified 且 description 指紋仍相符才算 verified，其餘 unverified。"""
    mark = _mark(concept)
    if mark is None or mark.get("verification") != VERIFICATION_VERIFIED:
        return VERIFICATION_UNVERIFIED
    if mark.get("description_digest") != description_digest(getattr(concept, "description", "")):
        return VERIFICATION_UNVERIFIED
    return VERIFICATION_VERIFIED
