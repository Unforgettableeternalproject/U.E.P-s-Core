"""
MemoryGraph - NetworkX 記憶圖管理器

混合架構的圖結構層，提供記憶節點間的關係管理和多跳檢索。

設計原則：
  - 輕量級（相比 FalkorDB/Neo4j）
  - 無 Docker 依賴
  - 支援動態邊緣權重
  - 持久化（pickle/GraphML）
"""

from __future__ import annotations

import logging
import math
import pickle
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import networkx as nx

from echo_memory.schemas.episode import Episode
from echo_memory.config import get_config

logger = logging.getLogger("echo_memory.graph")

# 秒轉小時的換算常數
_SECONDS_PER_HOUR = 3600.0


class MemoryGraph:
    """
    記憶圖管理器 - 基於 NetworkX。

    節點類型：
      - Episode: 對話情節
      - Concept: 抽象概念（Phase 2）
      - Entity: 實體（Phase 3）

    邊類型：
      - PRECEDES: 時序關係（A 發生在 B 之前）
      - CO_OCCURS: 共現關係（Phase 2）
      - RELATES_TO: 語義關聯（Phase 2）

    時間單位約定：
      - time_delta_hours：兩個 episode 之間的事件間隔（小時），用於建邊時的初始 recency 計算
      - edge_age_hours：邊從 created_at 到當下的存活時間（小時），用於 Dream 更新時的衰減計算
      - 衰減公式：Recency = exp(-λ·hours)，λ = EDGE_RECENCY_LAMBDA（預設 0.01 /小時）
    """

    def __init__(self, storage_path: str | Path):
        """
        初始化記憶圖。

        Args:
            storage_path: 圖數據存儲路徑
        """
        self.storage_path = Path(storage_path)
        self.graph_file = self.storage_path / "memory_graph.gpickle"

        # 創建有向圖
        self.graph = nx.DiGraph()

        # 從配置讀取邊緣權重參數
        _cfg = get_config()
        self.alpha = _cfg.edge_weight_alpha   # Recency 權重
        self.beta = _cfg.edge_weight_beta     # Frequency 權重
        self.gamma = _cfg.edge_weight_gamma   # Salience 權重
        self.decay_lambda = _cfg.edge_recency_lambda  # 時間衰減率（每小時）

        # 載入已存在的圖
        self._load()

        logger.info(
            f"[MemoryGraph] 初始化完成 - "
            f"節點數: {self.graph.number_of_nodes()}, "
            f"邊數: {self.graph.number_of_edges()}, "
            f"decay_lambda: {self.decay_lambda}/hr"
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 節點管理
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def add_episode_node(self, episode: Episode) -> None:
        """
        添加情節節點。

        Args:
            episode: Episode 對象
        """
        self.graph.add_node(
            episode.id,
            node_type="Episode",
            timestamp=episode.timestamp.isoformat(),
            salience_score=episode.salience_score,
            session_id=episode.session_id,
        )
        logger.debug(f"[MemoryGraph] 添加節點: {episode.id}")

    def has_node(self, node_id: str) -> bool:
        """檢查節點是否存在"""
        return self.graph.has_node(node_id)

    def get_node_count(self) -> int:
        """獲取節點總數"""
        return self.graph.number_of_nodes()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 邊管理 - PRECEDES（時序關係）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def add_precedes_edge(
        self,
        from_id: str,
        to_id: str,
        time_delta: float,
    ) -> None:
        """
        添加 PRECEDES 邊（時序關係）。

        Args:
            from_id: 前序節點 ID
            to_id: 後序節點 ID
            time_delta: 兩個 episode 之間的事件間隔（秒）。
                        此值僅用於記錄原始間隔，recency 計算會先轉換為小時。
        """
        if not self.graph.has_node(from_id) or not self.graph.has_node(to_id):
            logger.warning(f"[MemoryGraph] 節點不存在，無法添加邊: {from_id} -> {to_id}")
            return

        # 將事件間隔從秒轉換為小時，再計算 recency
        # time_delta 語義：兩個 episode 事件本身的時間差（非邊的存活時間）
        time_delta_hours = time_delta / _SECONDS_PER_HOUR
        recency_score = self._compute_recency(time_delta_hours)

        # 獲取節點的顯著性
        from_sal = self.graph.nodes[from_id].get("salience_score", 0.5)
        to_sal = self.graph.nodes[to_id].get("salience_score", 0.5)
        avg_salience = (from_sal + to_sal) / 2

        # 初始頻率為 1
        frequency = 1

        # 計算動態權重
        weight = self._compute_edge_weight(recency_score, frequency, avg_salience)

        self.graph.add_edge(
            from_id,
            to_id,
            edge_type="PRECEDES",
            time_delta=time_delta,          # 原始秒數，保留供外部參考
            time_delta_hours=time_delta_hours,  # 小時數，便於 debug
            recency_score=recency_score,
            frequency=frequency,
            avg_salience=avg_salience,
            weight=weight,
            created_at=time.time(),         # epoch 秒，邊的建立時間點
        )

        logger.debug(
            f"[MemoryGraph] 添加 PRECEDES 邊: {from_id[:8]}... -> {to_id[:8]}... "
            f"(time_delta={time_delta_hours:.2f}hr, recency={recency_score:.3f}, weight={weight:.3f})"
        )

    def update_precedes_edge(self, from_id: str, to_id: str) -> None:
        """
        更新 PRECEDES 邊（增加頻率）。

        當兩個節點再次按順序出現時，增加邊的頻率。
        """
        if not self.graph.has_edge(from_id, to_id):
            logger.warning(f"[MemoryGraph] 邊不存在: {from_id} -> {to_id}")
            return

        edge_data = self.graph[from_id][to_id]

        # 增加頻率
        edge_data["frequency"] += 1

        # 重新計算權重
        edge_data["weight"] = self._compute_edge_weight(
            edge_data["recency_score"],
            edge_data["frequency"],
            edge_data["avg_salience"],
        )

        logger.debug(
            f"[MemoryGraph] 更新邊頻率: {from_id[:8]}... -> {to_id[:8]}... "
            f"(freq={edge_data['frequency']}, weight={edge_data['weight']:.3f})"
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 動態權重計算
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _compute_recency(self, hours: float) -> float:
        """
        計算時間衰減分數。

        使用指數衰減：R = exp(-λ·hours)
        λ = EDGE_RECENCY_LAMBDA（預設 0.01 /小時）

        行為範例（λ=0.01）：
          1 小時後 ≈ 0.990
          24 小時後 ≈ 0.787
          7 天後 ≈ 0.186

        Args:
            hours: 時間間隔（小時）

        Returns:
            衰減分數 [0, 1]
        """
        return math.exp(-self.decay_lambda * hours)

    def _compute_edge_weight(
        self,
        recency: float,
        frequency: int,
        salience: float,
    ) -> float:
        """
        計算動態邊緣權重。

        公式：W = α·Recency + β·log(1+Frequency) + γ·Salience

        Args:
            recency: 時間衰減分數 [0, 1]
            frequency: 共現頻率（整數）
            salience: 平均顯著性 [0, 1]

        Returns:
            權重分數 [0, 1]
        """
        # 頻率對數化（避免過度增長）
        freq_score = math.log(1 + frequency) / math.log(11)  # 歸一化到 [0, 1]
        freq_score = min(1.0, freq_score)

        # 加權求和
        weight = (
            self.alpha * recency +
            self.beta * freq_score +
            self.gamma * salience
        )

        return min(1.0, max(0.0, weight))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 多跳檢索
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def expand_neighbors(
        self,
        seed_ids: list[str],
        max_hops: int = 2,
        min_weight: float = 0.15,
        max_results: int = 20,
    ) -> list[tuple[str, float, int]]:
        """
        從種子節點多跳展開，獲取相關節點。

        Args:
            seed_ids: 種子節點 ID 列表
            max_hops: 最大跳數
            min_weight: 最小邊權重閾值
            max_results: 最大返回數量

        Returns:
            [(node_id, avg_weight, hop_distance), ...] 按權重降序
        """
        if not seed_ids:
            return []

        visited = set(seed_ids)
        current_layer = seed_ids
        node_weights: dict[str, tuple[float, int]] = {}  # node_id -> (weight, hops)

        for hop in range(1, max_hops + 1):
            next_layer = []

            for node_id in current_layer:
                if not self.graph.has_node(node_id):
                    continue

                # 前向和後向鄰居（PRECEDES 是有向邊）
                neighbors = list(self.graph.successors(node_id)) + \
                           list(self.graph.predecessors(node_id))

                for neighbor in neighbors:
                    if neighbor in visited:
                        continue

                    # 獲取邊權重
                    if self.graph.has_edge(node_id, neighbor):
                        weight = self.graph[node_id][neighbor].get("weight", 0.0)
                    elif self.graph.has_edge(neighbor, node_id):
                        weight = self.graph[neighbor][node_id].get("weight", 0.0)
                    else:
                        weight = 0.0

                    # 過濾低權重邊
                    if weight < min_weight:
                        continue

                    # 記錄節點權重和跳數
                    if neighbor not in node_weights or node_weights[neighbor][0] < weight:
                        node_weights[neighbor] = (weight, hop)

                    visited.add(neighbor)
                    next_layer.append(neighbor)

            current_layer = next_layer

            if not current_layer:
                break

        # 按權重排序並限制數量
        results = [
            (node_id, weight, hops)
            for node_id, (weight, hops) in node_weights.items()
        ]
        results.sort(key=lambda x: x[1], reverse=True)

        logger.debug(
            f"[MemoryGraph] 多跳展開完成 - "
            f"種子: {len(seed_ids)}, 發現: {len(results)}, "
            f"最大跳數: {max_hops}"
        )

        return results[:max_results]

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 持久化
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _load(self) -> None:
        """從磁盤載入圖"""
        if not self.graph_file.exists():
            logger.info("[MemoryGraph] 未找到已存在的圖文件，創建新圖")
            return

        try:
            with open(self.graph_file, 'rb') as f:
                self.graph = pickle.load(f)
            logger.info(
                f"[MemoryGraph] 載入圖完成 - "
                f"{self.graph.number_of_nodes()} 節點, "
                f"{self.graph.number_of_edges()} 邊"
            )
        except Exception as e:
            logger.error(f"[MemoryGraph] 載入圖失敗: {e}")
            self.graph = nx.DiGraph()

    def save(self) -> None:
        """保存圖到磁盤"""
        try:
            self.storage_path.mkdir(parents=True, exist_ok=True)
            with open(self.graph_file, 'wb') as f:
                pickle.dump(self.graph, f)
            logger.debug(
                f"[MemoryGraph] 保存圖完成 - "
                f"{self.graph.number_of_nodes()} 節點, "
                f"{self.graph.number_of_edges()} 邊"
            )
        except Exception as e:
            logger.error(f"[MemoryGraph] 保存圖失敗: {e}")

    def clear(self) -> None:
        """清空圖（測試用）"""
        self.graph.clear()
        if self.graph_file.exists():
            self.graph_file.unlink()
        logger.info("[MemoryGraph] 已清空圖")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 統計與診斷
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def get_stats(self) -> dict[str, Any]:
        """獲取圖統計資訊"""
        edge_weights = [
            data.get("weight", 0.0)
            for _, _, data in self.graph.edges(data=True)
        ]

        return {
            "nodes": self.graph.number_of_nodes(),
            "edges": self.graph.number_of_edges(),
            "avg_degree": (
                sum(dict(self.graph.degree()).values()) / self.graph.number_of_nodes()
                if self.graph.number_of_nodes() > 0
                else 0
            ),
            "avg_edge_weight": (
                sum(edge_weights) / len(edge_weights)
                if edge_weights
                else 0
            ),
        }
