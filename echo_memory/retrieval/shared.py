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

指名模式（owner_uid）另回「旁證段」（D87）：其他成員的投影中 subject_uids 含該成員者，
relation="about"。旁證只放行 none／personal（與一般共享規則相同）；當事人撤回／糾正／整體退出
的旁證、以及當事人撤回索引異常時，旁證一律不回（本人段不受影響）。
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
# 指名成員查詢（owner_uid）的相似度門檻。
# 理由：摘要刻意去名化，「他最近在玩什麼」這類問句與摘要的語意相似度天生偏低；
# 指名模式已由 owner_uid 結構化限縮到單一成員且上限 SHARED_MAX_ITEMS 則，
# 門檻不必再承擔「過濾無關內容」的責任（排序仍依相似度），只擋掉明顯反向的結果。
OWNER_RECALL_MIN_SIMILARITY = 0.1


def _degraded(scope_id: str, reason: str) -> SharedContext:
    return SharedContext(scope_id=scope_id, degraded=True, degraded_reason=reason)


def _clean_uids(values: Iterable[Any] | None) -> set[str]:
    return {str(v).strip() for v in (values or ()) if str(v).strip()}


def _owner_tag(owner_uid: Any) -> str:
    """log 用：指名模式只記 uid 末四碼，一般模式記 '-'。"""
    owner = str(owner_uid or "").strip()
    return f"*{owner[-4:]}" if owner else "-"


def retrieve_shared_context(**kwargs: Any) -> SharedContext:
    """檢索共享摘要並記一行觀測 log（items 數、degraded 原因、耗時）。參數同 ``_retrieve_shared_context``。"""
    started = time.monotonic()
    ctx = _retrieve_shared_context(**kwargs)
    elapsed_ms = (time.monotonic() - started) * 1000
    logger.info(
        "[retrieve_shared] scope=%s owner=%s items=%d degraded=%s reason=%s elapsed_ms=%.0f",
        ctx.scope_id, _owner_tag(kwargs.get("owner_uid")), len(ctx.items),
        ctx.degraded, ctx.degraded_reason or "-", elapsed_ms,
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
    owner_uid: str | None = None,
    activity_fn: Any = None,
) -> SharedContext:
    """檢索共享摘要。任何前提不成立都回傳空的 SharedContext（degraded 標記原因），不拋例外。

    Args:
        layer: ScopeShareLayer（需 is_healthy／search_projections）。
        embed_query: ``(text) -> list[float]``，失敗回空 list。
        engine_namespace_id: 呼叫端 engine 的 namespace id（必須是 scope_id 對應的共享 namespace）。
        owner_uid: 指名成員查詢（宿主把名字解析成 uid 後傳入，結構化篩選）。給定時只回該擁有者的
            投影、門檻改用 ``OWNER_RECALL_MIN_SIMILARITY``（忽略 min_similarity）；viewer 等於
            owner_uid 回空；query 可為空字串，改依投影 learned_at 由新到舊取前幾則。
            另附旁證段（其他成員提到該成員的投影，最多同樣的上限）。
    """
    if parse_guild_scope(scope_id) is None:
        return _degraded(str(scope_id), "invalid_scope")
    if engine_namespace_id != scope_to_namespace_id(scope_id):
        # 把使用者 engine 或別的場域 engine 當成此場域查詢：拒絕，不回任何資料
        return _degraded(scope_id, "scope_mismatch")
    viewer = str(viewer_uid or "").strip()
    if not viewer:
        return _degraded(scope_id, "missing_viewer")
    owner_filter = str(owner_uid).strip() if owner_uid is not None else None
    if owner_filter is not None and (not owner_filter or owner_filter == viewer):
        # 指名但 uid 為空（解析失敗）不退化成一般檢索；本人的事走自己的 retrieve
        return SharedContext(scope_id=scope_id)
    query_empty = not isinstance(query, str) or not query.strip()
    if query_empty and owner_filter is None:
        return SharedContext(scope_id=scope_id)
    try:
        from echo_memory.config import get_config

        if not get_config().sharing_enabled:
            return _degraded(scope_id, "sharing_disabled")
        if not layer.is_healthy(scope_id):
            return _degraded(scope_id, "index_unhealthy")
        limit = max(0, min(int(top_k), SHARED_MAX_ITEMS))
        if limit == 0:
            return SharedContext(scope_id=scope_id)
        if query_empty:
            # 指名 + 空 query：不做向量比對，依 learned_at 由新到舊
            views = sorted(
                layer.list_projections(scope_id, owner_uid=owner_filter),
                key=lambda v: str(getattr(v, "learned_at", "") or ""),
                reverse=True,
            )
        else:
            vec = embed_query(query)
            if not vec:
                return _degraded(scope_id, "embedding_unavailable")
            if owner_filter is not None:
                views = layer.search_projections(
                    scope_id, vec, exclude_owner=viewer, owner=owner_filter,
                    top_k=_CANDIDATE_POOL, min_similarity=OWNER_RECALL_MIN_SIMILARITY,
                )
            else:
                views = layer.search_projections(
                    scope_id, vec, exclude_owner=viewer,
                    top_k=_CANDIDATE_POOL, min_similarity=float(min_similarity),
                )
        if not views and not query_empty:
            mismatch_fn = getattr(layer, "query_dimension_mismatch", None)
            mismatch = mismatch_fn(scope_id, vec) if callable(mismatch_fn) else None
            if mismatch is not None:
                logger.warning("[retrieve_shared] 向量維度不符（資料 %d、查詢 %d），回空並標 degraded", *mismatch)
                return _degraded(scope_id, "embedding_dimension_mismatch")
    except Exception as e:  # noqa: BLE001 - 檢索路徑不可崩潰；一律 fail-closed
        logger.warning("[retrieve_shared] 檢索失敗（回空）: %s", e)
        return _degraded(scope_id, "error")

    about_views: list[Any] = []
    if owner_filter is not None:
        try:
            if query_empty:
                about_views = sorted(
                    layer.list_about(scope_id, owner_filter, exclude_owner=viewer),
                    key=lambda v: str(getattr(v, "learned_at", "") or ""),
                    reverse=True,
                )
            else:
                about_views = layer.search_about(
                    scope_id, vec, owner_filter, exclude_owner=viewer,
                    top_k=_CANDIDATE_POOL, min_similarity=OWNER_RECALL_MIN_SIMILARITY,
                )
        except Exception as e:  # noqa: BLE001 - 旁證失敗只丟旁證（fail-closed），不影響本人段
            logger.warning("[retrieve_shared] 旁證檢索失敗（旁證回空）: %s", e)
            about_views = []

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
        if owner_filter is not None and owner != owner_filter:
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
    if not query_empty:  # 空 query 保持 learned_at 新到舊，不以相似度重排
        ranked.sort(key=lambda pair: pair[0], reverse=True)
    items = [it for _, it in ranked[:limit]]

    for v in about_views:
        if len([it for it in items if it.relation == "about"]) >= limit:
            break
        pid = str(getattr(v, "projection_id", "") or "")
        speaker = str(getattr(v, "owner_uid", "") or "")
        summary = str(getattr(v, "summary", "") or "").strip()
        if not pid or not speaker or not summary or pid in seen:
            continue
        if speaker == viewer or speaker == owner_filter:
            continue
        if getattr(v, "sensitivity", "none") not in SHAREABLE_SENSITIVITIES:
            continue
        if owner_filter not in tuple(getattr(v, "subject_uids", ()) or ()):
            continue
        seen.add(pid)
        items.append(SharedItem(
            projection_id=pid,
            owner_uid=speaker,
            summary=summary[: max(1, int(summary_max_chars))],
            claim_kind=str(getattr(v, "claim_kind", "fact") or "fact"),
            review_status=str(getattr(v, "review_status", "insufficient") or "insufficient"),
            similarity=float(getattr(v, "similarity", 0.0) or 0.0),
            source_digest=str(getattr(v, "source_digest", "") or ""),
            owner_present=speaker in present,
            relation="about",
            about_uid=owner_filter,
        ))
    activity_items: list[dict] = []
    if owner_filter is not None and callable(activity_fn):
        # 活動只在指名查詢時讀取；當事人整體退出（含索引異常 fail-closed）即不附
        try:
            if not layer.is_about_opted_out(scope_id, owner_filter):
                activity_items = [dict(a) for a in (activity_fn(owner_filter) or [])]
        except Exception as e:  # noqa: BLE001 - 活動失敗不影響本人段與旁證段
            logger.warning("[retrieve_shared] 遊玩摘要讀取失敗（不附活動）: %s", e)
            activity_items = []
    return SharedContext(
        scope_id=scope_id, items=items,
        activity_items=activity_items,
        activity_owner_uid=(owner_filter or "") if activity_items else "",
    )
