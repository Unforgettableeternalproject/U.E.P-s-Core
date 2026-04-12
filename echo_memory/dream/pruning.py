"""
Graph Pruning - 圖譜修剪模組

負責清理低權重邊緣、優化圖結構、提升檢索性能。

對應大腦分區：預設模式網路（DMN）- 睡眠期間的記憶整理
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from echo_memory.storage.graph import MemoryGraph

logger = logging.getLogger("echo_memory.dream.pruning")


@dataclass
class PruningReport:
    """圖譜修剪報告"""
    
    edges_before: int = 0
    edges_after: int = 0
    edges_pruned: int = 0
    weak_edges_removed: int = 0
    isolated_nodes_removed: int = 0
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
            f"  - 孤立節點清理: {self.isolated_nodes_removed}"
        )


class GraphPruning:
    """
    圖譜修剪器 - 清理低價值連接，優化記憶結構。
    
    策略：
      1. 移除低權重邊（< threshold）
      2. 清理孤立節點（無邊連接）
      3. 更新統計資訊
    """
    
    def __init__(self, graph: MemoryGraph):
        """
        初始化修剪器。
        
        Args:
            graph: MemoryGraph 實例
        """
        self.graph = graph
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
        
        # 2. 清理孤立節點（可選，暫時跳過以保留所有 Episode）
        # isolated = self._find_isolated_nodes()
        # self._remove_nodes(isolated)
        # report.isolated_nodes_removed = len(isolated)
        
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
    
    def update_edge_weights(self) -> int:
        """
        更新所有邊的時間衰減權重（簡化版 LiDER）。
        
        不重新推演，只更新時間衰減分量。
        
        Returns:
            更新的邊數量
        """
        import time
        
        updated_count = 0
        current_time = time.time()
        
        for u, v, data in self.graph.graph.edges(data=True):
            # 獲取邊的創建時間
            created_at = data.get("created_at", current_time)
            time_delta = current_time - created_at
            
            # 重新計算時間衰減
            recency_score = self.graph._compute_recency(time_delta)
            
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
