"""
Graph Pruning - 圖譜修剪模組

負責清理低權重邊緣、優化圖結構、提升檢索性能。

對應大腦分區：預設模式網路（DMN）- 睡眠期間的記憶整理
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from echo_memory.storage.graph import MemoryGraph

logger = logging.getLogger("echo_memory.dream.pruning")

# 秒轉小時的換算常數（與 graph.py 保持一致）
_SECONDS_PER_HOUR = 3600.0


@dataclass
class PruningReport:
    """圖譜修剪報告"""

    edges_before: int = 0
    edges_after: int = 0
    edges_pruned: int = 0
    weak_edges_removed: int = 0
    isolated_nodes_removed: int = 0
    nodes_archived: int = 0
    latent_bridges_created: int = 0
    max_frequency_updated: int = 0
    avg_weight_before: float = 0.0
    avg_weight_after: float = 0.0

    def summary(self) -> str:
        """生成摘要"""
        return (
            f"圖譜修剪完成:\n"
            f"  - 邊數: {self.edges_before} → {self.edges_after} "
            f"(移除 {self.edges_pruned})\n"
            f"  - 平均權重: {self.avg_weight_before:.3f} → {self.avg_weight_after:.3f}\n"
            f"  - 弱邊移除: {self.weak_edges_removed}\n"
            f"  - 節點歸檔: {self.nodes_archived}\n"
            f"  - LATENT_BRIDGE 建立: {self.latent_bridges_created}\n"
            f"  - max_frequency: {self.max_frequency_updated}"
        )


class GraphPruning:
    """
    圖譜修剪器 - 清理低價值連接，優化記憶結構。

    策略：
      1. 移除低權重邊（< threshold）
      2. 清理孤立節點（無邊連接）
      3. 更新統計資訊
    """

    # LATENT_BRIDGE 參數（對齊 OpenSoul）
    _BRIDGE_SIMILARITY_THRESHOLD = 0.85
    _MAX_BRIDGES_PER_RUN = 10
    # 節點歸檔參數
    _ARCHIVE_SALIENCE_THRESHOLD = 0.2
    _ARCHIVE_MIN_AGE_DAYS = 30

    def __init__(self, graph: MemoryGraph, semantic=None):
        """
        初始化修剪器。

        Args:
            graph: MemoryGraph 實例
            semantic: SemanticMemory 實例（用於 LATENT_BRIDGE，可選）
        """
        self.graph = graph
        self._semantic = semantic
        self.min_weight_threshold = 0.05  # 最低權重閾值（低於此值的邊將被刪除）
        self.isolated_node_grace_days = 7  # 孤立節點保留天數

    def run(self) -> PruningReport:
        """
        執行完整的圖譜修剪流程。

        Returns:
            PruningReport: 修剪統計報告
        """
        logger.info("[GraphPruning] 開始圖譜修剪")

        report = PruningReport()

        # 記錄修剪前狀態
        stats_before = self.graph.get_stats()
        report.edges_before = stats_before["edges"]
        report.avg_weight_before = stats_before.get("avg_edge_weight", 0.0)

        # 1. 移除低權重邊
        weak_edges = self._find_weak_edges()
        self._remove_edges(weak_edges)
        report.weak_edges_removed = len(weak_edges)

        # 2. 歸檔低價值節點（對齊 OpenSoul _archive_stale_nodes）
        report.nodes_archived = self._archive_stale_nodes()

        # 3. 更新 max_frequency 正規化基準（對齊 OpenSoul _update_max_frequency）
        report.max_frequency_updated = self.graph.recalc_max_frequency()

        # 4. 建立 LATENT_BRIDGE（跨域橋接 / 頓悟機制）
        if self._semantic is not None:
            report.latent_bridges_created = self._create_latent_bridges()

        # 記錄修剪後狀態
        stats_after = self.graph.get_stats()
        report.edges_after = stats_after["edges"]
        report.avg_weight_after = stats_after.get("avg_edge_weight", 0.0)
        report.edges_pruned = report.edges_before - report.edges_after

        # 持久化
        self.graph.save()

        logger.info(
            f"[GraphPruning] 完成 - "
            f"移除 {report.edges_pruned} 條邊, "
            f"平均權重提升至 {report.avg_weight_after:.3f}"
        )

        return report

    def _find_weak_edges(self) -> list[tuple[str, str]]:
        """
        找出權重低於閾值的邊。

        Returns:
            [(from_id, to_id), ...] 弱邊列表
        """
        weak_edges = []

        for u, v, data in self.graph.graph.edges(data=True):
            weight = data.get("weight", 0.0)
            if weight < self.min_weight_threshold:
                weak_edges.append((u, v))

        logger.debug(
            f"[GraphPruning] 發現 {len(weak_edges)} 條弱邊 "
            f"(權重 < {self.min_weight_threshold})"
        )

        return weak_edges

    def _remove_edges(self, edges: list[tuple[str, str]]):
        """
        批量移除邊。

        Args:
            edges: 要移除的邊列表
        """
        if not edges:
            return

        self.graph.graph.remove_edges_from(edges)

        logger.debug(f"[GraphPruning] 已移除 {len(edges)} 條邊")

    def _find_isolated_nodes(self) -> list[str]:
        """
        找出孤立節點（無任何邊連接）。

        Returns:
            [node_id, ...] 孤立節點列表
        """
        import networkx as nx

        isolated = list(nx.isolates(self.graph.graph))

        logger.debug(f"[GraphPruning] 發現 {len(isolated)} 個孤立節點")

        return isolated

    def _remove_nodes(self, nodes: list[str]):
        """
        批量移除節點。

        Args:
            nodes: 要移除的節點列表
        """
        if not nodes:
            return

        self.graph.graph.remove_nodes_from(nodes)

        logger.debug(f"[GraphPruning] 已移除 {len(nodes)} 個節點")

    def _archive_stale_nodes(self) -> int:
        """
        歸檔低價值節點（對齊 OpenSoul _archive_stale_nodes）。

        條件：salience < 0.2 且 is_dreamed=True 且建立超過 30 天。
        標記 archived=True，不物理刪除。
        EcphoryRetrieval 的 expand_neighbors 應跳過 archived 節點。

        Returns:
            歸檔的節點數
        """
        import time

        archived_count = 0
        current_time = time.time()
        min_age_seconds = self._ARCHIVE_MIN_AGE_DAYS * 24 * _SECONDS_PER_HOUR

        for node_id, data in self.graph.graph.nodes(data=True):
            # 只歸檔 Episode 節點
            if data.get("node_type") != "Episode":
                continue
            # 已歸檔的跳過
            if data.get("archived", False):
                continue

            salience = data.get("salience_score", 0.5)
            if salience >= self._ARCHIVE_SALIENCE_THRESHOLD:
                continue

            # 檢查是否已經被 Dream 處理過
            # 注意：graph 節點上的 is_dreamed 需要從 episodic 層同步
            # 這裡用 salience 作為主要判斷依據

            # 檢查年齡
            created_at_str = data.get("timestamp", "")
            if created_at_str:
                try:
                    from datetime import datetime, timezone
                    if isinstance(created_at_str, str):
                        ts = datetime.fromisoformat(created_at_str)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        node_age = (datetime.now(timezone.utc) - ts).total_seconds()
                        if node_age < min_age_seconds:
                            continue
                    else:
                        continue
                except (ValueError, TypeError):
                    continue

            # 標記歸檔
            data["archived"] = True
            archived_count += 1

        if archived_count:
            logger.info(f"[GraphPruning] 歸檔 {archived_count} 個低價值節點")

        return archived_count

    def _create_latent_bridges(self) -> int:
        """
        建立 LATENT_BRIDGE — 跨域橋接（頓悟機制）。

        對齊 OpenSoul soul/dream/pruning.py::_create_latent_bridges()。

        掃描所有 Concept 節點，兩兩計算 cosine similarity，
        若 >= 0.85 且無現有 RELATES_TO 或 LATENT_BRIDGE → 建立橋接。

        Returns:
            建立的橋接數
        """
        import numpy as np

        if self._semantic is None:
            return 0

        # 取得所有有 embedding 的概念
        concepts = self._semantic.list_concepts()
        if len(concepts) < 2:
            return 0

        # 過濾有 embedding 的概念（上限 50 個避免 O(n²) 爆炸）
        concepts_with_emb = [
            (c.id, c.name, c.embedding)
            for c in concepts
            if c.embedding and len(c.embedding) > 0
        ][:50]

        if len(concepts_with_emb) < 2:
            return 0

        bridges_created = 0

        for i in range(len(concepts_with_emb)):
            if bridges_created >= self._MAX_BRIDGES_PER_RUN:
                break

            id_a, name_a, emb_a = concepts_with_emb[i]
            vec_a = np.array(emb_a, dtype=np.float32)
            norm_a = np.linalg.norm(vec_a)
            if norm_a == 0:
                continue

            for j in range(i + 1, len(concepts_with_emb)):
                if bridges_created >= self._MAX_BRIDGES_PER_RUN:
                    break

                id_b, name_b, emb_b = concepts_with_emb[j]
                vec_b = np.array(emb_b, dtype=np.float32)
                norm_b = np.linalg.norm(vec_b)
                if norm_b == 0:
                    continue

                # Cosine similarity
                similarity = float(np.dot(vec_a, vec_b) / (norm_a * norm_b))

                if similarity < self._BRIDGE_SIMILARITY_THRESHOLD:
                    continue

                # 檢查是否已有 RELATES_TO 或 LATENT_BRIDGE
                has_existing = False
                if self.graph.graph.has_edge(id_a, id_b):
                    et = self.graph.graph[id_a][id_b].get("edge_type", "")
                    if et in ("RELATES_TO", "LATENT_BRIDGE"):
                        has_existing = True
                if not has_existing and self.graph.graph.has_edge(id_b, id_a):
                    et = self.graph.graph[id_b][id_a].get("edge_type", "")
                    if et in ("RELATES_TO", "LATENT_BRIDGE"):
                        has_existing = True

                if has_existing:
                    continue

                # 建立 LATENT_BRIDGE（雙向）
                import time
                reason = f"夢境橋接：{name_a} ↔ {name_b}（相似度 {similarity:.3f}）"
                for src, dst in [(id_a, id_b), (id_b, id_a)]:
                    self.graph.graph.add_edge(
                        src, dst,
                        edge_type="LATENT_BRIDGE",
                        weight=similarity,
                        similarity=similarity,
                        reason=reason,
                        created_at=time.time(),
                    )

                bridges_created += 1
                logger.debug(
                    f"[GraphPruning] LATENT_BRIDGE: {name_a} ↔ {name_b} "
                    f"(sim={similarity:.3f})"
                )

        if bridges_created:
            logger.info(
                f"[GraphPruning] 建立 {bridges_created} 條 LATENT_BRIDGE"
            )

        return bridges_created

    def update_edge_weights(self) -> int:
        """
        更新所有邊的時間衰減權重（簡化版 LiDER）。

        不重新推演，只更新時間衰減分量。

        時間語義：
          - created_at：邊建立時的 epoch 秒數（由 graph.add_precedes_edge 寫入）
          - edge_age_hours：邊從建立到現在的存活時間（小時）
            此值與 add_precedes_edge 的 time_delta（兩 episode 間隔）是不同概念，
            前者衡量「邊有多舊」，後者衡量「兩個事件距離多遠」。

        Returns:
            更新的邊數量
        """
        import time

        updated_count = 0
        current_time = time.time()

        for u, v, data in self.graph.graph.edges(data=True):
            if data.get("edge_type") != "PRECEDES":
                continue
            # 計算邊的存活時間（秒 → 小時）
            # 語義：edge_age 衡量「這條邊已存在多久」，用於 Dream 週期性衰減更新
            created_at = data.get("created_at", current_time)
            edge_age_hours = (current_time - created_at) / _SECONDS_PER_HOUR

            # 重新計算時間衰減（以小時為單位）
            recency_score = self.graph._compute_recency(edge_age_hours)

            # 保留原有頻率和顯著性
            frequency = data.get("frequency", 1)
            avg_salience = data.get("avg_salience", 0.5)

            # 重新計算權重
            new_weight = self.graph._compute_edge_weight(
                recency_score,
                frequency,
                avg_salience
            )

            # 更新權重和時間衰減分數
            data["recency_score"] = recency_score
            data["weight"] = new_weight

            updated_count += 1

        logger.info(f"[GraphPruning] 更新 {updated_count} 條邊的權重")

        return updated_count
