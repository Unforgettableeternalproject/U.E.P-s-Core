"""
共享檢索（M10 包 C）：從場域共享 namespace 取回「其他成員提過」的概念摘要。

這一層只做政策與排序，不自己碰儲存：撤回索引、可見度、sensitivity 過濾都在
``ScopeShareLayer.search_projections`` 內（索引遺失／損毀 -> 整個 scope 回空）。
本模組再補三道結構檢查（全部是 id／欄位比對，沒有任何文字內容判斷）：

  1. scope 與引擎綁定：scope_id 必須對應這個 engine 的 namespace（``guild:123`` <->
     ``scope-g123``），否則回空——查詢端沒有「查所有場域」的路徑，跨場域不會外洩。
  2. 排除檢視者自己的投影（本人的事走自己的 retrieve）。
  3. 只輸出 shareable_summary（ProjectionView.summary），不輸出任何其他欄位。

``present_uids`` 內的擁有者排序加成（在場者的事更相關），不在場者仍可出現；
加成只影響排序，不放寬相似度門檻。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from typing import Any

from echo_memory.schemas.memory_context import (
    SHARED_MAX_ITEMS,
    SharedContext,
    SharedItem,
)
from echo_memory.schemas.sharing import (
    SHAREABLE_SENSITIVITIES,
    parse_guild_scope,
    scope_to_namespace_id,
)

logger = logging.getLogger("echo_memory.retrieval.shared")

# 在場者加成（只影響排序）
PRESENT_OWNER_BONUS = 0.1
# 向層要的候選池大小（排序加成後再截斷）
_CANDIDATE_POOL = 20


def _degraded(scope_id: str, reason: str) -> SharedContext:
    return SharedContext(scope_id=scope_id, degraded=True, degraded_reason=reason)


def _clean_uids(values: Iterable[Any] | None) -> set[str]:
    return {str(v).strip() for v in (values or ()) if str(v).strip()}


def retrieve_shared_context(**kwargs: Any) -> SharedContext:
    """檢索共享摘要並記一行觀測 log（items 數、degraded 原因、耗時）。參數同 ``_retrieve_shared_context``。"""
    started = time.monotonic()
    ctx = _retrieve_shared_context(**kwargs)
    elapsed_ms = (time.monotonic() - started) * 1000
    logger.info(
        "[retrieve_shared] scope=%s items=%d degraded=%s reason=%s elapsed_ms=%.0f",
        ctx.scope_id, len(ctx.items), ctx.degraded, ctx.degraded_reason or "-", elapsed_ms,
    )
    return ctx


def _retrieve_shared_context(
    *,
    layer: Any,
    embed_query: Any,
    engine_namespace_id: str,
    query: str,
    scope_id: str,
    viewer_uid: str,
    present_uids: Iterable[Any] = (),
    top_k: int = SHARED_MAX_ITEMS,
    min_similarity: float = 0.45,
    summary_max_chars: int = 120,
) -> SharedContext:
    """檢索共享摘要。任何前提不成立都回傳空的 SharedContext（degraded 標記原因），不拋例外。

    Args:
        layer: ScopeShareLayer（需 is_healthy／search_projections）。
        embed_query: ``(text) -> list[float]``，失敗回空 list。
        engine_namespace_id: 呼叫端 engine 的 namespace id（必須是 scope_id 對應的共享 namespace）。
    """
    if parse_guild_scope(scope_id) is None:
        return _degraded(str(scope_id), "invalid_scope")
    if engine_namespace_id != scope_to_namespace_id(scope_id):
        # 把使用者 engine 或別的場域 engine 當成此場域查詢：拒絕，不回任何資料
        return _degraded(scope_id, "scope_mismatch")
    viewer = str(viewer_uid or "").strip()
    if not viewer:
        return _degraded(scope_id, "missing_viewer")
    if not isinstance(query, str) or not query.strip():
        return SharedContext(scope_id=scope_id)
    try:
        from echo_memory.config import get_config

        if not get_config().sharing_enabled:
            return _degraded(scope_id, "sharing_disabled")
        if not layer.is_healthy(scope_id):
            return _degraded(scope_id, "index_unhealthy")
        vec = embed_query(query)
        if not vec:
            return _degraded(scope_id, "embedding_unavailable")
        limit = max(0, min(int(top_k), SHARED_MAX_ITEMS))
        if limit == 0:
            return SharedContext(scope_id=scope_id)
        views = layer.search_projections(
            scope_id,
            vec,
            exclude_owner=viewer,
            top_k=_CANDIDATE_POOL,
            min_similarity=float(min_similarity),
        )
        if not views:
            mismatch_fn = getattr(layer, "query_dimension_mismatch", None)
            mismatch = mismatch_fn(scope_id, vec) if callable(mismatch_fn) else None
            if mismatch is not None:
                logger.warning("[retrieve_shared] 向量維度不符（資料 %d、查詢 %d），回空並標 degraded", *mismatch)
                return _degraded(scope_id, "embedding_dimension_mismatch")
    except Exception as e:  # noqa: BLE001 - 檢索路徑不可崩潰；一律 fail-closed
        logger.warning("[retrieve_shared] 檢索失敗（回空）: %s", e)
        return _degraded(scope_id, "error")

    present = _clean_uids(present_uids)
    present.discard(viewer)
    seen: set[str] = set()
    ranked: list[tuple[float, SharedItem]] = []
    for v in views:
        pid = str(getattr(v, "projection_id", "") or "")
        owner = str(getattr(v, "owner_uid", "") or "")
        summary = str(getattr(v, "summary", "") or "").strip()
        if not pid or not owner or not summary or pid in seen:
            continue
        if owner == viewer:
            continue
        if getattr(v, "sensitivity", "none") not in SHAREABLE_SENSITIVITIES:
            continue
        seen.add(pid)
        sim = float(getattr(v, "similarity", 0.0) or 0.0)
        in_present = owner in present
        item = SharedItem(
            projection_id=pid,
            owner_uid=owner,
            summary=summary[: max(1, int(summary_max_chars))],
            claim_kind=str(getattr(v, "claim_kind", "fact") or "fact"),
            review_status=str(getattr(v, "review_status", "insufficient") or "insufficient"),
            similarity=sim,
            source_digest=str(getattr(v, "source_digest", "") or ""),
            owner_present=in_present,
        )
        ranked.append((sim + (PRESENT_OWNER_BONUS if in_present else 0.0), item))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return SharedContext(scope_id=scope_id, items=[it for _, it in ranked[:limit]])
