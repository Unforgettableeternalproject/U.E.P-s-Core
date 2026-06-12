"""
LiDER Full Replay — LLM 驅動的反思重播

對應大腦結構：睡眠 REM 期海馬迴重播（Hippocampal Replay）

職責：
  1. 取高顯著性未重播 Episode
  2. LLM 反思：生成摘要 + 識別與其他 Episode 的關聯
  3. 更新圖譜邊權重（反思後的邊更強）
  4. 標記 is_dreamed=True
  5. 無 LLM 時優雅降級

Dream 完整週期（整合後）：
  衰減更新 → 圖譜修剪 → **重播** → 蒸餾

設計決策：
  - 重播和蒸餾是獨立的：重播強化既有連結，蒸餾提取新概念
  - 重播在蒸餾之前執行（先強化再提取更合理）
  - LLM 輸出 JSON：reflection + related_indices + importance_adjustment
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from echo_memory.schemas.episode import Episode
from echo_memory.storage.episodic import EpisodicMemory
from echo_memory.storage.graph import MemoryGraph

logger = logging.getLogger("echo_memory.dream.replay")

_REPLAY_PROMPT = """\
你是一個記憶反思專家。回顧以下對話記錄，進行反思性分析。

## 對話記錄

{episodes_text}

## 輸出格式

請輸出一個 JSON 物件：

```json
{{
  "reflections": [
    {{
      "index": 0,
      "reflection": "對這段對話的反思摘要（1-2句話）",
      "importance": 0.7,
      "related_indices": [1, 2]
    }}
  ]
}}
```

## 規則
- 為每個對話記錄寫一段反思（reflection）
- importance 是 0-1 的浮點數，代表這段記憶的重要程度
- related_indices 列出與此記錄語義相關的其他記錄索引
- 反思要超越原始對話，提煉出潛在意義或模式
- 如果記錄太瑣碎不值得反思，importance 設為 < 0.3
"""


@dataclass
class ReplayReport:
    """重播報告"""

    episodes_replayed: int = 0
    edges_strengthened: int = 0
    edges_created: int = 0
    avg_importance: float = 0.0
    skipped_reason: str | None = None
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.skipped_reason:
            return f"重播跳過: {self.skipped_reason}"
        return (
            f"重播完成: {self.episodes_replayed} 個 Episode, "
            f"強化 {self.edges_strengthened} / 新建 {self.edges_created} 條邊, "
            f"平均重要性 {self.avg_importance:.2f}"
        )


class LiDERReplay:
    """
    LiDER 反思重播器。

    Usage:
        replayer = LiDERReplay(episodic, graph, llm_fn)
        report = replayer.run()
    """

    MAX_EPISODES_PER_BATCH = 8
    MIN_SALIENCE_THRESHOLD = 0.4

    def __init__(
        self,
        episodic: EpisodicMemory,
        graph: MemoryGraph,
        llm_fn: Callable[[str], str] | None = None,
    ):
        self.episodic = episodic
        self.graph = graph
        self.llm_fn = llm_fn

    def run(
        self,
        min_salience: float | None = None,
        max_episodes: int | None = None,
    ) -> ReplayReport:
        """執行一次反思重播。"""
        report = ReplayReport()

        if self.llm_fn is None:
            report.skipped_reason = "no_llm"
            logger.info("[LiDERReplay] 無 LLM 函式，跳過重播")
            return report

        threshold = min_salience if min_salience is not None else self.MIN_SALIENCE_THRESHOLD
        batch_size = max_episodes if max_episodes is not None else self.MAX_EPISODES_PER_BATCH

        candidates = self._collect_candidates(threshold, batch_size)
        report.episodes_replayed = len(candidates)

        if not candidates:
            report.skipped_reason = "no_episodes"
            return report

        # 建構 prompt + 呼叫 LLM
        prompt = self._build_prompt(candidates)
        try:
            raw_output = self.llm_fn(prompt)
        except Exception as e:
            report.skipped_reason = "llm_error"
            report.errors.append(f"LLM 呼叫失敗: {e}")
            return report

        # 解析反思結果
        try:
            reflections = self._parse_llm_output(raw_output)
        except Exception as e:
            report.skipped_reason = "llm_error"
            report.errors.append(f"解析失敗: {e}")
            return report

        # 應用反思：更新圖譜
        importances = []
        for ref in reflections:
            idx = ref.get("index", -1)
            if not isinstance(idx, int) or idx < 0 or idx >= len(candidates):
                continue

            importance = min(1.0, max(0.0, ref.get("importance", 0.5)))
            importances.append(importance)
            related_indices = ref.get("related_indices", [])

            ep = candidates[idx]

            # 更新 Episode 的顯著性（反思後可能調高或調低）
            new_salience = (ep.salience_score + importance) / 2
            self.episodic.update_salience(ep.id, new_salience)

            # 建立/強化相關 Episode 之間的邊
            for rel_idx in related_indices:
                if not isinstance(rel_idx, int) or rel_idx < 0 or rel_idx >= len(candidates):
                    continue
                if rel_idx == idx:
                    continue

                rel_ep = candidates[rel_idx]

                # 確保兩個節點都在圖中
                if not self.graph.has_node(ep.id):
                    self.graph.add_episode_node(ep)
                if not self.graph.has_node(rel_ep.id):
                    self.graph.add_episode_node(rel_ep)

                # 已有邊 → 強化
                if self.graph.graph.has_edge(ep.id, rel_ep.id):
                    edge_data = self.graph.graph[ep.id][rel_ep.id]
                    edge_data["frequency"] = edge_data.get("frequency", 1) + 1
                    edge_data["weight"] = min(
                        1.0,
                        edge_data.get("weight", 0.5) + importance * 0.1,
                    )
                    report.edges_strengthened += 1
                elif self.graph.graph.has_edge(rel_ep.id, ep.id):
                    edge_data = self.graph.graph[rel_ep.id][ep.id]
                    edge_data["frequency"] = edge_data.get("frequency", 1) + 1
                    edge_data["weight"] = min(
                        1.0,
                        edge_data.get("weight", 0.5) + importance * 0.1,
                    )
                    report.edges_strengthened += 1
                else:
                    # 新建反思邊（語義關聯，非時序關係）
                    self.graph.graph.add_edge(
                        ep.id,
                        rel_ep.id,
                        edge_type="REPLAY_RELATED",
                        weight=importance * 0.6,
                        frequency=1,
                        created_at=time.time(),
                        replay_created=True,
                    )
                    report.edges_created += 1

        report.avg_importance = (
            sum(importances) / len(importances) if importances else 0.0
        )

        # 標記已重播
        self._mark_episodes_dreamed(candidates)

        # 持久化圖
        self.graph.save()

        logger.info(f"[LiDERReplay] {report.summary()}")
        return report

    def _collect_candidates(
        self,
        min_salience: float,
        max_count: int,
    ) -> list[Episode]:
        """收集未重播的高顯著性 Episode。"""
        all_episodes = self.episodic.list_episodes(
            limit=200,
            sort_by="salience",
            ascending=False,
        )

        candidates = [
            ep for ep in all_episodes
            if ep.salience_score >= min_salience and not ep.is_dreamed
        ]

        return candidates[:max_count]

    def _build_prompt(self, episodes: list[Episode]) -> str:
        lines = []
        for i, ep in enumerate(episodes):
            lines.append(
                f"[{i}] 使用者: {ep.user_input}\n"
                f"    助手: {ep.agent_response}\n"
                f"    摘要: {ep.content_summary}"
            )
        return _REPLAY_PROMPT.format(episodes_text="\n\n".join(lines))

    def _parse_llm_output(self, raw: str) -> list[dict[str, Any]]:
        """解析 LLM 反思輸出。"""
        text = raw.strip()

        # 去除 markdown fences
        if "```" in text:
            parts = text.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    text = part
                    break

        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            return []

        data = json.loads(text[start:end + 1])
        return data.get("reflections", [])

    def _mark_episodes_dreamed(self, episodes: list[Episode]) -> None:
        episode_ids = [ep.id for ep in episodes]
        self.episodic.mark_as_dreamed(episode_ids)
