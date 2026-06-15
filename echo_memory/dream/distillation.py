"""
KnowledgeDistillation - 知識蒸餾管線

對應大腦分區：睡眠期 SWS（慢波睡眠）海馬迴→新皮質的記憶轉移

職責：
  1. 掃描近期高顯著性 Episode
  2. 呼叫 LLM 識別模式與概念
  3. 抽象為 Concept 寫入 SemanticMemory
  4. 在 MemoryGraph 建立 DERIVED_FROM 溯源邊
  5. 無 LLM 時優雅降級（跳過蒸餾，不報錯）

設計決策：
  - LLM 透過 Callable 注入（不綁死特定 provider）
  - 蒸餾 prompt 輸出 JSON，結構化解析
  - 單次蒸餾上限：MAX_EPISODES_PER_BATCH（避免 token 爆炸）
  - 蒸餾成功時記錄處理過的 Episode ID（由 DreamEngine 統一標記 is_dreamed）
  - 空結果/全失敗時不記錄，允許下一輪重試
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from echo_memory.schemas.episode import Episode
from echo_memory.storage.episodic import EpisodicMemory
from echo_memory.storage.graph import MemoryGraph
from echo_memory.storage.semantic import SemanticMemory

logger = logging.getLogger("echo_memory.dream.distillation")

# 蒸餾 prompt 模板
_DISTILLATION_PROMPT = """\
你是一個知識蒸餾專家。分析以下對話記錄，從中提取可複用的抽象概念或模式。

## 對話記錄

{episodes_text}

## 輸出格式

請輸出一個 JSON 陣列，每個元素代表一個抽象概念：

```json
[
  {{
    "name": "概念名稱（簡短、唯一）",
    "description": "概念描述（1-2句話）",
    "concept_type": "entity | rule | abstract | pattern",
    "source_indices": [0, 2]
  }}
]
```

## 規則
- 只提取跨多個對話出現的模式或可泛化的知識
- 不要重複原始對話內容，要「抽象」和「歸納」
- concept_type 選擇：entity（人/地/物）、rule（規則/偏好）、abstract（抽象概念）、pattern（行為模式）
- source_indices 是上面對話記錄的索引（從 0 開始）
- 最多輸出 5 個概念
- 如果沒有值得提取的概念，輸出空陣列 []
"""


@dataclass
class DistillationReport:
    """單次蒸餾的報告"""

    episodes_scanned: int = 0
    concepts_created: int = 0
    concepts_updated: int = 0
    edges_created: int = 0
    contradicts_edges_created: int = 0
    skipped_reason: str | None = None  # "no_llm" | "no_episodes" | "llm_error"
    llm_raw_output: str | None = None
    errors: list[str] = field(default_factory=list)
    # 內部追蹤：被蒸餾的 Episode ID，供 DreamEngine 統一標記 is_dreamed
    _distilled_episode_ids: list[str] = field(default_factory=list, repr=False)

    def summary(self) -> str:
        if self.skipped_reason:
            return f"蒸餾跳過: {self.skipped_reason}"
        return (
            f"蒸餾完成: 掃描 {self.episodes_scanned} 個 Episode, "
            f"新增 {self.concepts_created} / 更新 {self.concepts_updated} 個 Concept, "
            f"建立 {self.edges_created} 條 DERIVED_FROM 邊"
        )


class KnowledgeDistillation:
    """
    知識蒸餾器 — 從 Episode 中提取 Concept。

    Usage:
        distiller = KnowledgeDistillation(
            episodic=episodic,
            semantic=semantic,
            graph=graph,
            llm_fn=my_llm_call,  # (prompt: str) -> str
        )
        report = distiller.run()
    """

    MAX_EPISODES_PER_BATCH = 10
    MIN_SALIENCE_THRESHOLD = 0.5

    def __init__(
        self,
        episodic: EpisodicMemory,
        semantic: SemanticMemory,
        graph: MemoryGraph,
        llm_fn: Callable[[str], str] | None = None,
        contradiction_detector: Any | None = None,
        patch_coordinator: Any | None = None,
    ):
        """
        Args:
            episodic: EpisodicMemory 實例
            semantic: SemanticMemory 實例
            graph: MemoryGraph 實例（共用的圖）
            llm_fn: LLM 呼叫函式 (prompt: str) -> str
                    若為 None，蒸餾會優雅跳過。
            contradiction_detector: ContradictionDetector 實例（可選）。
                    若提供，每次 ingest concept 後自動偵測矛盾。
            patch_coordinator: DreamPatchCoordinator 實例（可選）。
                    若提供，SemanticMemory 寫入走 patch pipeline。
        """
        self.episodic = episodic
        self.semantic = semantic
        self.graph = graph
        self.llm_fn = llm_fn
        self._contradiction_detector = contradiction_detector
        self._patch_coordinator = patch_coordinator
        if patch_coordinator is None:
            logger.warning(
                "[KnowledgeDistillation] 未注入 patch_coordinator，使用舊路徑直接寫入"
            )

    def run(
        self,
        min_salience: float | None = None,
        max_episodes: int | None = None,
    ) -> DistillationReport:
        """
        執行一次知識蒸餾。

        Args:
            min_salience: 最小顯著性閾值（覆寫預設值）
            max_episodes: 最大處理 Episode 數（覆寫預設值）

        Returns:
            DistillationReport
        """
        report = DistillationReport()

        # 無 LLM → 優雅降級
        if self.llm_fn is None:
            report.skipped_reason = "no_llm"
            logger.info("[KnowledgeDistillation] 無 LLM 函式，跳過蒸餾")
            return report

        # 收集候選 Episode
        threshold = min_salience if min_salience is not None else self.MIN_SALIENCE_THRESHOLD
        batch_size = max_episodes if max_episodes is not None else self.MAX_EPISODES_PER_BATCH

        candidates = self._collect_candidates(threshold, batch_size)
        report.episodes_scanned = len(candidates)

        if not candidates:
            report.skipped_reason = "no_episodes"
            logger.info("[KnowledgeDistillation] 無符合條件的 Episode，跳過蒸餾")
            return report

        # 建構 prompt
        prompt = self._build_prompt(candidates)

        # 呼叫 LLM
        try:
            raw_output = self.llm_fn(prompt)
            report.llm_raw_output = raw_output
        except Exception as e:
            report.skipped_reason = "llm_error"
            report.errors.append(f"LLM 呼叫失敗: {e}")
            logger.error(f"[KnowledgeDistillation] LLM 呼叫失敗: {e}")
            return report

        # 解析 LLM 輸出
        try:
            extracted = self._parse_llm_output(raw_output)
        except Exception as e:
            report.skipped_reason = "llm_error"
            report.errors.append(f"LLM 輸出解析失敗: {e}")
            logger.error(f"[KnowledgeDistillation] 解析失敗: {e}")
            return report

        # 寫入 SemanticMemory + 建圖 + 矛盾偵測
        for concept_data in extracted:
            try:
                created, edges, contradicts = self._ingest_concept(concept_data, candidates)
                if created:
                    report.concepts_created += 1
                else:
                    report.concepts_updated += 1
                report.edges_created += edges
                report.contradicts_edges_created += contradicts
            except Exception as e:
                report.errors.append(f"概念寫入失敗: {concept_data.get('name', '?')}: {e}")
                logger.error(f"[KnowledgeDistillation] 概念寫入失敗: {e}")

        # 只在有實際產出時才記錄 Episode ID（供 DreamEngine 統一標記）
        # Major #3 修復：空結果或全部失敗時不標記，允許後續重試
        if report.concepts_created > 0 or report.concepts_updated > 0:
            report._distilled_episode_ids = [ep.id for ep in candidates]

        logger.info(f"[KnowledgeDistillation] {report.summary()}")
        return report

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 內部方法
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _collect_candidates(
        self,
        min_salience: float,
        max_count: int,
    ) -> list[Episode]:
        """
        收集符合蒸餾條件的 Episode。

        條件：
          - salience_score >= min_salience
          - is_dreamed == False（未被蒸餾過）

        排序：salience 降序（最重要的先處理）
        """
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
        """從 Episode 列表建構蒸餾 prompt。"""
        lines = []
        for i, ep in enumerate(episodes):
            lines.append(
                f"[{i}] 使用者: {ep.user_input}\n"
                f"    助手: {ep.agent_response}\n"
                f"    摘要: {ep.content_summary}"
            )

        episodes_text = "\n\n".join(lines)
        return _DISTILLATION_PROMPT.format(episodes_text=episodes_text)

    def _parse_llm_output(self, raw: str) -> list[dict[str, Any]]:
        """
        從 LLM 輸出中解析 JSON 陣列。

        容錯處理：
          - 去除 markdown code fences
          - 嘗試從文本中提取 JSON
        """
        text = raw.strip()

        # 去除 markdown code fences
        if "```" in text:
            # 找到 ``` 之間的內容
            parts = text.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("["):
                    text = part
                    break

        # 嘗試找到 JSON 陣列
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1 or end <= start:
            logger.warning("[KnowledgeDistillation] 未找到 JSON 陣列")
            return []

        json_str = text[start:end + 1]
        result = json.loads(json_str)

        if not isinstance(result, list):
            return []

        # 驗證每個概念的必要欄位
        valid = []
        for item in result:
            if isinstance(item, dict) and "name" in item and "description" in item:
                valid.append(item)

        return valid[:5]  # 最多 5 個

    def _ingest_concept(
        self,
        concept_data: dict[str, Any],
        source_episodes: list[Episode],
    ) -> tuple[bool, int, int]:
        """
        將蒸餾出的概念寫入 SemanticMemory 和 MemoryGraph。

        Args:
            concept_data: LLM 輸出的概念字典
            source_episodes: 來源 Episode 列表

        Returns:
            (is_new, edges_created, contradicts_created)
        """
        name = concept_data["name"]
        description = concept_data["description"]
        concept_type = concept_data.get("concept_type", "abstract")
        source_indices = concept_data.get("source_indices", [])

        # 確定來源 Episode
        source_eps = []
        for idx in source_indices:
            if isinstance(idx, int) and 0 <= idx < len(source_episodes):
                source_eps.append(source_episodes[idx])

        # 如果 LLM 沒指定 source_indices，就用全部候選 Episode
        if not source_eps:
            source_eps = source_episodes

        # 檢查是否已存在（判斷 is_new）
        existing = self.semantic.get_by_name(name)
        is_new = existing is None

        source_ep_ids = [ep.id for ep in source_eps]

        # ── SemanticMemory 寫入：走 patch pipeline 或舊路徑 ──
        concept_id = None
        if self._patch_coordinator is not None:
            # GPT #4: 走 DreamPatchCoordinator → ConceptInhibitor
            concept_id, _tx = self._patch_coordinator.submit_distillation_concept(
                name=name,
                description=description,
                concept_type=concept_type,
                source_episode_ids=source_ep_ids,
            )
            # 若 coordinator 無 inhibitor，退回直接寫入
            if concept_id is None and _tx is None:
                for ep in source_eps:
                    concept_id = self.semantic.upsert_concept(
                        name=name,
                        description=description,
                        concept_type=concept_type,
                        source_episode_id=ep.id,
                    )
        else:
            # 舊路徑：直接 upsert
            for ep in source_eps:
                concept_id = self.semantic.upsert_concept(
                    name=name,
                    description=description,
                    concept_type=concept_type,
                    source_episode_id=ep.id,
                )

        if concept_id is None:
            return is_new, 0

        # 在 MemoryGraph 中註冊概念節點（若不存在）
        if not self.graph.has_node(concept_id):
            self.graph.add_concept_node(concept_id, name, concept_type)

        # 建立 DERIVED_FROM 邊（Concept → Episode）
        edges_created = 0
        for ep in source_eps:
            # 確保 Episode 節點存在
            if not self.graph.has_node(ep.id):
                self.graph.add_episode_node(ep)

            if not self.graph.graph.has_edge(concept_id, ep.id):
                self.graph.add_derived_from_edge(concept_id, ep.id)
                edges_created += 1

        # 矛盾偵測（若有 detector）
        contradicts_created = 0
        if self._contradiction_detector is not None and concept_id is not None:
            concept = self.semantic.get(concept_id)
            if concept is not None:
                try:
                    edges = self._contradiction_detector.detect_and_register(concept)
                    contradicts_created = len(edges)
                except Exception as e:
                    logger.warning(
                        f"[KnowledgeDistillation] 矛盾偵測失敗（不影響蒸餾）: {e}"
                    )

        return is_new, edges_created, contradicts_created

    def _mark_episodes_dreamed(self, episodes: list[Episode]) -> None:
        """將已蒸餾的 Episode 標記為 is_dreamed=True。"""
        episode_ids = [ep.id for ep in episodes]
        marked = self.episodic.mark_as_dreamed(episode_ids)

        logger.debug(
            f"[KnowledgeDistillation] 標記 {marked}/{len(episodes)} 個 Episode 為 is_dreamed"
        )
