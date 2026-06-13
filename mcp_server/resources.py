"""
Echo Memory MCP Resource Handlers

實作所有 MCP 資源（resources）的處理邏輯。
每個資源對應一個 URI，返回 JSON 格式的結構化資料。

資源列表：
    1. memory://stats              — 記憶統計 JSON
    2. memory://episodes/recent    — 最近 10 個情節
    3. memory://concepts           — 所有概念列表
    4. memory://graph/summary      — 圖結構摘要
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone

from mcp.server import Server
from mcp.types import Resource

logger = logging.getLogger("echo_memory.mcp.resources")

# ─── 資源 URI 定義 ─────────────────────────────────────────────────────────────
_RESOURCE_STATS = "memory://stats"
_RESOURCE_EPISODES_RECENT = "memory://episodes/recent"
_RESOURCE_CONCEPTS = "memory://concepts"
_RESOURCE_GRAPH_SUMMARY = "memory://graph/summary"


def register_resources(server: Server, get_engine: Callable) -> None:
    """
    向 MCP Server 註冊所有資源。

    Args:
        server: MCP Server 實例
        get_engine: 取得 MemoryEngine 實例的工廠函式（Callable[[], MemoryEngine]）
    """

    @server.list_resources()
    async def handle_list_resources() -> list[Resource]:
        """回傳所有可用資源的定義清單。"""
        return [
            Resource(
                uri=_RESOURCE_STATS,
                name="記憶系統統計",
                description=(
                    "Echo Memory 系統的統計資訊，"
                    "包含各層記憶的數量和圖結構資訊。"
                ),
                mimeType="application/json",
            ),
            Resource(
                uri=_RESOURCE_EPISODES_RECENT,
                name="最近情節列表",
                description=(
                    "最近 10 個存儲的情節記憶，"
                    "按時間戳降序排列。"
                ),
                mimeType="application/json",
            ),
            Resource(
                uri=_RESOURCE_CONCEPTS,
                name="概念列表",
                description=(
                    "所有語義概念的列表，"
                    "包含名稱和描述。"
                ),
                mimeType="application/json",
            ),
            Resource(
                uri=_RESOURCE_GRAPH_SUMMARY,
                name="記憶圖摘要",
                description=(
                    "MemoryGraph 的結構摘要，"
                    "包含節點數、邊數和統計資訊。"
                ),
                mimeType="application/json",
            ),
        ]

    @server.read_resource()
    async def handle_read_resource(uri: str) -> str:
        """
        讀取指定 URI 的資源內容。

        Args:
            uri: 資源 URI（memory://xxx 格式）

        Returns:
            JSON 格式的字串
        """
        try:
            uri_str = str(uri)
            if uri_str == _RESOURCE_STATS:
                return await _read_stats(get_engine)
            elif uri_str == _RESOURCE_EPISODES_RECENT:
                return await _read_recent_episodes(get_engine)
            elif uri_str == _RESOURCE_CONCEPTS:
                return await _read_concepts(get_engine)
            elif uri_str == _RESOURCE_GRAPH_SUMMARY:
                return await _read_graph_summary(get_engine)
            else:
                error_data = {"error": f"未知的資源 URI: {uri}"}
                return json.dumps(error_data, ensure_ascii=False)

        except Exception as exc:
            logger.exception(f"[MCP Resource] 讀取資源 '{uri}' 失敗: {exc}")
            error_data = {
                "error": f"讀取資源失敗: {type(exc).__name__}: {exc}"
            }
            return json.dumps(error_data, ensure_ascii=False)


# ─── 各資源的具體實作 ──────────────────────────────────────────────────────────


async def _read_stats(get_engine: Callable) -> str:
    """
    讀取記憶系統統計資訊。

    Returns:
        JSON 字串，包含各層記憶的數量和圖結構資訊
    """
    def _sync_stats() -> dict:
        engine = get_engine()
        stats = engine.get_stats()

        # 加入圖結構統計
        graph_stats = engine.episodic.graph.get_stats()

        return {
            "memory_layers": {
                "episodes": stats.get("total_episodes", 0),
                "concepts": stats.get("total_concepts", 0),
                "working_cache": stats.get("working_cache_size", 0),
            },
            "graph": {
                "nodes": graph_stats["nodes"],
                "edges": graph_stats["edges"],
                "avg_degree": round(graph_stats.get("avg_degree", 0.0), 4),
                "avg_edge_weight": round(graph_stats.get("avg_edge_weight", 0.0), 4),
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    data = await asyncio.to_thread(_sync_stats)
    return json.dumps(data, ensure_ascii=False, indent=2)


async def _read_recent_episodes(get_engine: Callable) -> str:
    """
    讀取最近 10 個情節。

    Returns:
        JSON 字串，包含最近情節的摘要列表
    """
    def _sync_list() -> list[dict]:
        engine = get_engine()
        episodic = engine.episodic

        # 使用 list_episodes 方法（若存在），否則手動從 _episodes 讀取
        if hasattr(episodic, "list_episodes"):
            episodes_objs = episodic.list_episodes(
                session_id=None,
                limit=10,
                sort_by="timestamp",
                ascending=False,
            )
        else:
            episodes_objs = sorted(
                episodic._episodes.values(),
                key=lambda ep: ep.timestamp,
                reverse=True,
            )[:10]

        result = []
        for ep in episodes_objs:
            result.append({
                "id": ep.id,
                "session_id": ep.session_id,
                "timestamp": ep.timestamp.isoformat(),
                "salience_score": round(ep.salience_score, 4),
                "content_summary": ep.content_summary[:200],
                "is_dreamed": ep.is_dreamed,
                "da_weight": round(ep.da_weight, 4),
                "ht_weight": round(ep.ht_weight, 4),
            })
        return result

    episodes = await asyncio.to_thread(_sync_list)
    data = {
        "count": len(episodes),
        "episodes": episodes,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


async def _read_concepts(get_engine: Callable) -> str:
    """
    讀取所有概念列表。

    Returns:
        JSON 字串，包含所有語義概念
    """
    def _sync_list() -> list[dict]:
        engine = get_engine()

        # 使用 list_concepts 方法（若存在），否則直接讀取 _concepts dict
        if hasattr(engine.semantic, "list_concepts"):
            concepts_objs = engine.semantic.list_concepts(
                limit=200,
                sort_by="updated_at",
            )
        else:
            concepts_objs = list(engine.semantic._concepts.values())

        result = []
        for c in concepts_objs:
            entry: dict = {
                "id": c.id,
                "name": c.name,
                "description": c.description[:300],
                "concept_type": c.concept_type,
            }
            # 選用欄位（新版 Concept 才有）
            if hasattr(c, "confidence"):
                entry["confidence"] = round(c.confidence, 4)
            if hasattr(c, "access_count"):
                entry["access_count"] = c.access_count
            if hasattr(c, "source_episode_ids"):
                entry["source_episodes"] = len(c.source_episode_ids)
            if hasattr(c, "updated_at") and c.updated_at is not None:
                entry["updated_at"] = c.updated_at.isoformat()
            result.append(entry)

        return result

    concepts = await asyncio.to_thread(_sync_list)
    data = {
        "count": len(concepts),
        "concepts": concepts,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


async def _read_graph_summary(get_engine: Callable) -> str:
    """
    讀取記憶圖結構摘要。

    Returns:
        JSON 字串，包含圖統計和節點類型分布
    """
    def _sync_graph() -> dict:
        engine = get_engine()
        graph = engine.episodic.graph
        nx_graph = graph.graph

        # 統計各類型節點數
        node_type_counts: dict[str, int] = {}
        for node_id, data in nx_graph.nodes(data=True):
            ntype = data.get("node_type", "Unknown")
            node_type_counts[ntype] = node_type_counts.get(ntype, 0) + 1

        # 統計各類型邊數
        edge_type_counts: dict[str, int] = {}
        for _, _, data in nx_graph.edges(data=True):
            etype = data.get("edge_type", "Unknown")
            edge_type_counts[etype] = edge_type_counts.get(etype, 0) + 1

        stats = graph.get_stats()

        return {
            "nodes": {
                "total": stats["nodes"],
                "by_type": node_type_counts,
            },
            "edges": {
                "total": stats["edges"],
                "by_type": edge_type_counts,
                "avg_weight": round(stats.get("avg_edge_weight", 0.0), 4),
            },
            "avg_degree": round(stats.get("avg_degree", 0.0), 4),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    data = await asyncio.to_thread(_sync_graph)
    return json.dumps(data, ensure_ascii=False, indent=2)
