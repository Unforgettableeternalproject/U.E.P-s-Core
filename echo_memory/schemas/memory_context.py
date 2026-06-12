"""
MemoryContext - 檢索結果容器

EcphoryRAG 檢索引擎的輸出格式，整合三層記憶的相關節點。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from echo_memory.schemas.concept import Concept
from echo_memory.schemas.entity import Entity
from echo_memory.schemas.episode import Episode
from echo_memory.schemas.procedure import Procedure


@dataclass
class MemoryContext:
    """
    記憶脈絡容器。

    包含檢索到的所有相關記憶節點，可序列化為 LLM 可讀的文本格式。
    """

    episodes: list[Episode] = field(default_factory=list)
    concepts: list[Concept] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    procedures: list[Procedure] = field(default_factory=list)
    similarities: dict[str, float] = field(default_factory=dict)  # episode_id -> 相似度分數
    concept_similarities: dict[str, float] = field(default_factory=dict)  # concept_id -> 相似度分數

    def is_empty(self) -> bool:
        """檢查是否為空"""
        return not (self.episodes or self.concepts or self.entities or self.procedures)

    def to_text(self) -> str:
        """
        序列化為 LLM 可讀的文本格式。
        
        包含：
        - 相對時間標記（幾分鐘前/幾小時前/N天前）
        - 重要程度標記（核心/重要/一般/淡薄）
        - 結構化分區（對話記憶/語義概念/實體）
        """
        parts: list[str] = []

        # ─── 情節記憶 ────────────────────────────────────────────────────────
        if self.episodes:
            parts.append("【相關對話記憶（以下為過去對話，不是當前訊息）】")
            for ep in self.episodes[:5]:  # 最多顯示 5 個
                rel_time = self._relative_time(ep.timestamp)
                time_tag = f"（{rel_time}）" if rel_time else ""

                # 重要程度標記
                sal = ep.salience_score
                if sal >= 0.75:
                    sal_tag = "核心記憶"
                elif sal >= 0.5:
                    sal_tag = "重要記憶"
                elif sal >= 0.25:
                    sal_tag = "一般記憶"
                else:
                    sal_tag = "淡薄記憶"

                # 相似度分數
                similarity = self.similarities.get(ep.id, 0.0)
                sim_tag = f"[匹配度:{similarity:.0%}]" if similarity > 0 else ""

                parts.append(
                    f"  - {time_tag}{sim_tag}[重要程度:《{sal_tag}》] {ep.content_summary}"
                )

        # ─── 語義概念 ────────────────────────────────────────────────────────
        if self.concepts:
            parts.append("\n【相關語義概念】")
            for c in self.concepts[:5]:
                sim = self.concept_similarities.get(c.id, 0.0)
                sim_tag = f"[匹配度:{sim:.0%}]" if sim > 0 else ""
                parts.append(f"  - {sim_tag}{c.name}: {c.description}")

        # ─── 實體 ────────────────────────────────────────────────────────────
        if self.entities:
            parts.append("\n【提及的實體】")
            for e in self.entities[:5]:
                etype = e.entity_type or "未知"
                name = e.name
                desc = e.description
                parts.append(f"  - [{etype}] {name}: {desc}")

        # ─── 程序記憶 ────────────────────────────────────────────────────────
        if self.procedures:
            parts.append("\n【相關程序/SOP】")
            for p in self.procedures[:3]:
                steps_preview = " → ".join(p.steps[:3])
                if len(p.steps) > 3:
                    steps_preview += " → ..."
                rate_tag = f"[成功率:{p.success_rate:.0%}]" if p.total_uses > 0 else ""
                parts.append(
                    f"  - {rate_tag}{p.name}: {p.description}\n"
                    f"    步驟: {steps_preview}"
                )

        return "\n".join(parts) if parts else "（無相關記憶）"

    @staticmethod
    def _relative_time(ts: datetime) -> str:
        """將時間戳轉換為相對時間（幾分鐘前/幾小時前/N天前）"""
        try:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            diff_sec = (now - ts).total_seconds()

            if diff_sec < 120:
                return "剛才"
            elif diff_sec < 3600:
                return f"{int(diff_sec // 60)} 分鐘前"
            elif diff_sec < 86400:
                return f"{int(diff_sec // 3600)} 小時前"
            else:
                return f"{int(diff_sec // 86400)} 天前"
        except Exception:
            return ""

    def summary(self) -> str:
        """簡短摘要（用於日誌）"""
        return (
            f"MemoryContext("
            f"episodes={len(self.episodes)}, "
            f"concepts={len(self.concepts)}, "
            f"entities={len(self.entities)})"
        )
