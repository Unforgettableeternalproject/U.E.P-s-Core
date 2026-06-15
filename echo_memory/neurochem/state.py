"""
NeurochemState — 神經化學狀態機

對應大腦結構：VTA（腹側被蓋區，多巴胺）+ 中縫核（血清素）

設計原則：
  - 5 種模式（BALANCED/HIGH_DA/HIGH_5HT/EXCITED/CAUTIOUS）
  - 漸進式遞增（朝目標值平滑移動，不是直接跳變）
  - 自然衰減（沒有刺激時慢慢回到基線）
  - 衍生參數驅動檢索行為（seed_k / max_hops / min_weight / concept_top_k）

使用方式：
  state = NeurochemState()
  state.stimulate(dopamine_delta=0.2)  # 新奇事件 → DA 上升
  params = state.get_retrieval_params()  # 取得當前檢索參數
  state.tick()  # 每次互動後自然衰減
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("echo_memory.neurochem")


class NeurochemMode(Enum):
    """
    神經化學模式 — 根據 DA/5-HT 水位自動判定。

    語義對齊 OpenSoul：
      EXCITED = DA 高 + 5-HT 低 → 高度興奮但低穩定性（衝動探索）
      CAUTIOUS = DA 低 + 5-HT 高 → 低動力但高謹慎（保守深思）
    """

    BALANCED = "balanced"           # DA ≈ 5-HT ≈ 中間值（預設）
    HIGH_DA = "high_dopamine"       # DA ≥ 0.65 → 探索模式（廣搜索、低閾值）
    HIGH_5HT = "high_serotonin"     # 5-HT ≥ 0.65 → 深度模式（多跳、高精度）
    EXCITED = "excited"             # DA ≥ 0.75 且 5-HT < 0.4 → 衝動探索模式
    CAUTIOUS = "cautious"           # DA < 0.35 且 5-HT ≥ 0.65 → 保守深思模式


@dataclass
class RetrievalParams:
    """由神經化學狀態衍生的檢索參數。"""

    seed_k: int = 5              # Episode 向量搜索的種子數
    max_hops: int = 2            # 圖譜展開跳數
    min_weight: float = 0.15     # 展開最小邊權重
    concept_top_k: int = 5       # Concept 向量搜索數
    concept_min_similarity: float = 0.3  # Concept 最低相似度
    exploration_bonus: float = 0.0  # 展開時的探索加成（降低 min_weight）


@dataclass
class NeurochemState:
    """
    神經化學狀態管理器。

    Attributes:
        dopamine: 多巴胺水位 [0, 1]（影響探索廣度）
        serotonin: 血清素水位 [0, 1]（影響搜索深度/精度）
        baseline_da: 多巴胺基線值
        baseline_5ht: 血清素基線值
        approach_rate: 刺激時朝目標值的接近速率（0-1）
        decay_rate: 每次 tick 朝基線衰減的速率（0-1）
    """

    dopamine: float = 0.5
    serotonin: float = 0.5

    baseline_da: float = 0.5
    baseline_5ht: float = 0.5

    approach_rate: float = 0.3   # 每次刺激接近目標值的 30%
    decay_rate: float = 0.05     # 每次 tick 回落基線的 5%

    # OpenSoul 對齊：刺激速率與上限
    da_update_rate: float = 0.08
    ht_update_rate: float = 0.06
    da_max_per_cycle: float = 0.15
    ht_max_per_cycle: float = 0.12
    decay_per_hour: float = 0.05

    def stimulate(
        self,
        dopamine_delta: float = 0.0,
        serotonin_delta: float = 0.0,
    ) -> None:
        """
        漸進式刺激。

        不直接加減，而是朝「當前值 + delta」的目標平滑接近。
        公式：new = current + approach_rate × (target - current)

        Args:
            dopamine_delta: DA 刺激量（正值增加，負值減少）
            serotonin_delta: 5-HT 刺激量
        """
        if dopamine_delta != 0.0:
            target = self._clamp(self.dopamine + dopamine_delta)
            self.dopamine += self.approach_rate * (target - self.dopamine)
            self.dopamine = self._clamp(self.dopamine)

        if serotonin_delta != 0.0:
            target = self._clamp(self.serotonin + serotonin_delta)
            self.serotonin += self.approach_rate * (target - self.serotonin)
            self.serotonin = self._clamp(self.serotonin)

        logger.debug(
            f"[NeurochemState] 刺激後: DA={self.dopamine:.3f}, "
            f"5-HT={self.serotonin:.3f}, mode={self.mode.value}"
        )

    # ── OpenSoul 事件驅動方法 ────────────────────────────────────────

    def on_success(self, reward: float = 0.3) -> None:
        """
        正向回饋 / 任務成功。

        DA 朝向高獎勵目標漸進，5-HT 輕微下降（可承擔更多風險）。
        對應 OpenSoul: neurochem.on_success(reward)
        """
        da_target = self._clamp(0.5 + reward * 0.4)
        self.dopamine += self.da_update_rate * (da_target - self.dopamine)
        # 輕微降低血清素（成功後更願意冒險）
        ht_target = self._clamp(self.serotonin - reward * 0.05)
        self.serotonin += self.ht_update_rate * (ht_target - self.serotonin)
        self.dopamine = self._clamp(self.dopamine)
        self.serotonin = self._clamp(self.serotonin)
        logger.debug(f"[NeurochemState] on_success(reward={reward:.2f}): "
                     f"DA={self.dopamine:.3f}, 5-HT={self.serotonin:.3f}")

    def on_failure(self, penalty: float = 0.2) -> None:
        """
        驗證失敗 / 負向回饋。

        DA 下降（動力降低），5-HT 上升（進入謹慎模式）。
        對應 OpenSoul: neurochem.on_failure(penalty)
        """
        da_target = self._clamp(max(0.2, 0.5 - penalty * 0.3))
        self.dopamine += self.da_update_rate * (da_target - self.dopamine)
        ht_target = self._clamp(self.serotonin + penalty * 0.15)
        self.serotonin += self.ht_update_rate * (ht_target - self.serotonin)
        self.dopamine = self._clamp(self.dopamine)
        self.serotonin = self._clamp(self.serotonin)
        logger.debug(f"[NeurochemState] on_failure(penalty={penalty:.2f}): "
                     f"DA={self.dopamine:.3f}, 5-HT={self.serotonin:.3f}")

    def on_uncertainty(self, level: float = 0.2) -> None:
        """
        高不確定性。

        DA 輕微下降，5-HT 朝向謹慎端漸進。
        對應 OpenSoul: neurochem.on_uncertainty(level)
        """
        self.dopamine += self.da_update_rate * (-level * 0.1)
        ht_target = self._clamp(0.5 + level * 0.3)
        self.serotonin += self.ht_update_rate * (ht_target - self.serotonin)
        self.dopamine = self._clamp(self.dopamine)
        self.serotonin = self._clamp(self.serotonin)
        logger.debug(f"[NeurochemState] on_uncertainty(level={level:.2f}): "
                     f"DA={self.dopamine:.3f}, 5-HT={self.serotonin:.3f}")

    def on_discovery(self, novelty: float = 0.4) -> None:
        """
        新知識連結 / 夢境共鳴。

        DA 朝向高點漸進，5-HT 不變。
        對應 OpenSoul: neurochem.on_discovery(novelty)
        """
        da_target = self._clamp(min(0.85, 0.5 + novelty * 0.35))
        self.dopamine += self.da_update_rate * (da_target - self.dopamine)
        self.dopamine = self._clamp(self.dopamine)
        logger.debug(f"[NeurochemState] on_discovery(novelty={novelty:.2f}): "
                     f"DA={self.dopamine:.3f}, 5-HT={self.serotonin:.3f}")

    def natural_decay(self, hours: float = 1.0) -> None:
        """
        時間流逝引起的自然衰減。

        DA 和 5-HT 都趨向 baseline，衰減量與時間成正比。
        對應 OpenSoul: neurochem.natural_decay(hours)
        """
        decay_factor = max(0.8, 1.0 - self.decay_per_hour * hours)
        self.dopamine = self.baseline_da + decay_factor * (self.dopamine - self.baseline_da)
        self.serotonin = self.baseline_5ht + decay_factor * (self.serotonin - self.baseline_5ht)
        self.dopamine = self._clamp(self.dopamine)
        self.serotonin = self._clamp(self.serotonin)

    def tick(self) -> None:
        """
        自然衰減（每次互動後呼叫）。

        朝基線值回落：new = current + decay_rate × (baseline - current)
        """
        self.dopamine += self.decay_rate * (self.baseline_da - self.dopamine)
        self.serotonin += self.decay_rate * (self.baseline_5ht - self.serotonin)

        self.dopamine = self._clamp(self.dopamine)
        self.serotonin = self._clamp(self.serotonin)

    # ── 衍生屬性（OpenSoul 對齊）────────────────────────────────────

    @property
    def verification_threshold(self) -> float:
        """品質閘門動態門檻：5-HT 越高，驗證越嚴。"""
        return 0.5 + self.serotonin * 0.4

    @property
    def search_breadth(self) -> int:
        """EcphoryRAG BFS 搜尋廣度：5-HT 越高，搜索越廣。"""
        return int(5 + self.serotonin * 15)

    @property
    def learning_rate(self) -> float:
        """語意記憶邊緣更新速率：DA 越高，學習越快。"""
        return 0.1 + self.dopamine * 0.9

    @property
    def salience_boost(self) -> float:
        """新記憶顯著性加乘：DA 越高，新記憶越突出。"""
        return 1.0 + self.dopamine * 0.5

    @property
    def mode(self) -> NeurochemMode:
        """
        根據當前 DA/5-HT 水位判定模式。

        語義對齊 OpenSoul：
          EXCITED = DA ≥ 0.75 且 5-HT < 0.4（衝動探索）
          CAUTIOUS = DA < 0.35 且 5-HT ≥ 0.65（保守深思）
        """
        da = self.dopamine
        ht = self.serotonin

        if da >= 0.75 and ht < 0.4:
            return NeurochemMode.EXCITED
        elif da < 0.35 and ht >= 0.65:
            return NeurochemMode.CAUTIOUS
        elif da >= 0.65:
            return NeurochemMode.HIGH_DA
        elif ht >= 0.65:
            return NeurochemMode.HIGH_5HT
        else:
            return NeurochemMode.BALANCED

    def get_retrieval_params(self) -> RetrievalParams:
        """
        根據當前神經化學狀態計算檢索參數。

        DA 高 → seed_k 增加、min_weight 降低（更多探索）
        5-HT 高 → max_hops 增加、concept_min_similarity 降低（更深搜索）
        EXCITED → 全部放大
        CAUTIOUS → 全部收縮
        """
        da = self.dopamine
        ht = self.serotonin

        # seed_k: 3 ~ 8，DA 越高越多
        seed_k = int(3 + 5 * da)

        # max_hops: 1 ~ 3，5-HT 越高越深
        if ht > 0.7:
            max_hops = 3
        elif ht > 0.4:
            max_hops = 2
        else:
            max_hops = 1

        # min_weight: 0.05 ~ 0.35，DA 越高閾值越低（更願意探索弱連結）
        min_weight = max(0.05, 0.35 - da * 0.3)

        # concept_top_k: 3 ~ 8
        concept_top_k = int(3 + 5 * da)

        # concept_min_similarity: 0.15 ~ 0.45，5-HT 低時更寬鬆
        concept_min_sim = max(0.15, 0.45 - ht * 0.3)

        # exploration_bonus: DA 高時額外降低 min_weight
        exploration_bonus = max(0.0, (da - 0.5) * 0.2)

        return RetrievalParams(
            seed_k=seed_k,
            max_hops=max_hops,
            min_weight=max(0.0, min_weight - exploration_bonus),
            concept_top_k=concept_top_k,
            concept_min_similarity=concept_min_sim,
            exploration_bonus=exploration_bonus,
        )

    def to_dict(self) -> dict:
        """序列化（含衍生屬性供診斷）。"""
        return {
            "dopamine": round(self.dopamine, 4),
            "serotonin": round(self.serotonin, 4),
            "mode": self.mode.value,
            "baseline_da": self.baseline_da,
            "baseline_5ht": self.baseline_5ht,
            "verification_threshold": round(self.verification_threshold, 4),
            "search_breadth": self.search_breadth,
            "learning_rate": round(self.learning_rate, 4),
            "salience_boost": round(self.salience_boost, 4),
        }

    @classmethod
    def from_dict(cls, data: dict) -> NeurochemState:
        """反序列化。"""
        return cls(
            dopamine=data.get("dopamine", 0.5),
            serotonin=data.get("serotonin", 0.5),
            baseline_da=data.get("baseline_da", 0.5),
            baseline_5ht=data.get("baseline_5ht", 0.5),
        )

    @staticmethod
    def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
        return max(low, min(high, value))
