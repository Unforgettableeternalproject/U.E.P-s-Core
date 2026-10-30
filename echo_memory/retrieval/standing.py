"""
檢索期的 support 面向：standing 與側向抑制（純函式，不碰 IO）。

四維拆分（relevance／support／confidence／independence）在檢索期的分工：
  - 排序分數 = relevance(sim) × retrieval_weight × status_factor
    relevance 與 support 分開相乘，confidence 不計入排序（避免重複計入）。
  - standing = confidence × status_factor × source_rank，
    只用於 tie-break、側向抑制的分攤與 profile 排序。
  - 側向抑制：CONTRADICTS 邊上只壓 standing 較低的一方。

CONTRADICTS 邊的 weight 是「矛盾強度」(0-1)，越強越該壓；舊式把 weight 當乘數
（強度越小壓得越狠）語意相反，已移除。
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

# 審核狀態 → 排序因子（not_checked 只是微幅偏低，不得變相成「未審核＝可疑」）
DEFAULT_STATUS_FACTOR: dict[str, float] = {
    "supported": 1.00,
    "not_required": 1.00,  # 免佐證（identity／general）：不因不需驗證而低於 supported
    "not_checked": 0.95,
    "insufficient": 0.80,
    "contradicted": 0.45,
}

# support_source → 來源權重
DEFAULT_SOURCE_RANK: dict[str, float] = {
    "user": 1.0,
    "legacy_unknown": 0.8,
    "agent_only": 0.6,
}

# 側向抑制預設參數（config 提供時以 config 為準）
DEFAULT_GAMMA = 0.6
DEFAULT_FLOOR = 0.4
# 橋接展開遇到有矛盾邊的鄰居時的折扣
DEFAULT_BRIDGE_CONFLICT_FACTOR = 0.6

_UNKNOWN_SOURCE_RANK = DEFAULT_SOURCE_RANK["legacy_unknown"]
_UNKNOWN_STATUS_FACTOR = DEFAULT_STATUS_FACTOR["not_checked"]


def review_status(concept: Any) -> str:
    """概念目前有效的審核狀態；審核綁定失效或無此介面時視為 not_checked。"""
    fn = getattr(concept, "effective_review_status", None)
    if not callable(fn):
        return "not_checked"
    try:
        status = fn()
    except Exception:
        return "not_checked"
    return status if isinstance(status, str) and status else "not_checked"


def support_source_of(concept: Any) -> str:
    """概念的 support_source；舊資料沒有此欄位時視為 legacy_unknown。"""
    meta = getattr(concept, "metadata", None) or {}
    value = meta.get("support_source")
    return value if isinstance(value, str) and value else "legacy_unknown"


def is_superseded(concept: Any) -> bool:
    """已被取代的歷史概念：status=deprecated 且帶 replaced_by（沿用既有欄位）。"""
    if getattr(concept, "status", "") != "deprecated":
        return False
    meta = getattr(concept, "metadata", None) or {}
    return bool(meta.get("replaced_by"))


def status_factor(
    concept: Any,
    table: Mapping[str, float] | None = None,
) -> float:
    """審核狀態對應的排序因子。"""
    table = table if table is not None else DEFAULT_STATUS_FACTOR
    return float(table.get(review_status(concept), _UNKNOWN_STATUS_FACTOR))


def source_rank(
    concept: Any,
    table: Mapping[str, float] | None = None,
) -> float:
    """support_source 對應的來源權重。"""
    table = table if table is not None else DEFAULT_SOURCE_RANK
    return float(table.get(support_source_of(concept), _UNKNOWN_SOURCE_RANK))


def standing(
    concept: Any,
    table: Mapping[str, float] | None = None,
    rank_table: Mapping[str, float] | None = None,
) -> float:
    """standing = confidence × status_factor × source_rank。"""
    confidence = float(getattr(concept, "confidence", 0.0) or 0.0)
    return confidence * status_factor(concept, table) * source_rank(concept, rank_table)


def lateral_multiplier(
    own_standing: float,
    other_standing: float,
    edge_weight: float,
    gamma: float = DEFAULT_GAMMA,
    floor: float = DEFAULT_FLOOR,
) -> float:
    """單條 CONTRADICTS 邊上，自己被對方壓制的乘數。

    share = own / (own + other)；m = max(floor, 1 - gamma × w × (1 - share))。
    standing 較高者乘數接近 1；兩者 standing 皆為 0 時各取 share=0.5。
    """
    w = min(1.0, max(0.0, float(edge_weight)))
    total = own_standing + other_standing
    share = 0.5 if total <= 0 else own_standing / total
    return max(floor, 1.0 - gamma * w * (1.0 - share))


def inhibition_multipliers(
    concepts_by_id: Mapping[str, Any],
    edges_of: Callable[[str], list[tuple[str, float]]],
    gamma: float = DEFAULT_GAMMA,
    floor: float = DEFAULT_FLOOR,
    table: Mapping[str, float] | None = None,
    rank_table: Mapping[str, float] | None = None,
) -> dict[str, float]:
    """計算結果集合內各概念受到的側向抑制乘數（多條邊取最小）。

    Args:
        concepts_by_id: 結果集合（只有雙方都在集合內的矛盾邊才會互相抑制）
        edges_of: concept_id → [(neighbor_id, edge_weight)]（CONTRADICTS 邊）
    """
    standings = {
        cid: standing(c, table, rank_table) for cid, c in concepts_by_id.items()
    }
    result: dict[str, float] = {}
    for cid in concepts_by_id:
        for nid, weight in edges_of(cid):
            if nid not in concepts_by_id or nid == cid:
                continue
            m = lateral_multiplier(standings[cid], standings[nid], weight, gamma, floor)
            if m < result.get(cid, 1.0):
                result[cid] = m
    return result


def params_from_config(cfg: Any) -> dict[str, Any]:
    """從 config 讀側向抑制與狀態／來源因子；欄位缺少時用預設值。"""
    status_table = {
        st: float(getattr(cfg, f"review_status_factor_{st}", dflt))
        for st, dflt in DEFAULT_STATUS_FACTOR.items()
    }
    rank_attr = {"user": "user", "legacy_unknown": "legacy", "agent_only": "agent"}
    rank_table = {
        src: float(
            getattr(cfg, f"standing_source_rank_{attr}", DEFAULT_SOURCE_RANK[src])
        )
        for src, attr in rank_attr.items()
    }
    return {
        "gamma": float(getattr(cfg, "contradicts_gamma", DEFAULT_GAMMA)),
        "floor": float(getattr(cfg, "contradicts_floor", DEFAULT_FLOOR)),
        "bridge_conflict_factor": float(
            getattr(cfg, "bridge_conflict_factor", DEFAULT_BRIDGE_CONFLICT_FACTOR)
        ),
        "status_table": status_table,
        "rank_table": rank_table,
    }
