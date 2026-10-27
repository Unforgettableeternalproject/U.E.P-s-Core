"""
ProcedureExtractor - 從 Episode 蒸餾可複用的程序/SOP

對應大腦結構：基底核（Basal Ganglia）技能學習

職責：
  1. 掃描高顯著性 Episode 群
  2. 呼叫 LLM 辨識可複用的操作步驟或決策流程
  3. 寫入 ProceduralMemory + 建立 APPLIES_TO 邊
  4. 無 LLM 時優雅降級（跳過提取）
  5. 支援結構化 LLM 呼叫（structured_llm_fn）以保證合法 JSON 回傳
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from echo_memory.dream.staleness import (
    Guard,
    find_stale_episode_ids,
    resolve_guard,
    snapshot_episodes,
)
from echo_memory.schemas.episode import Episode
from echo_memory.schemas.procedure import Procedure
from echo_memory.storage.graph import MemoryGraph
from echo_memory.storage.procedural import ProceduralMemory
from echo_memory.storage.semantic import SemanticMemory

logger = logging.getLogger("echo_memory.dream.procedure_extractor")

# 結構化 LLM 呼叫的 JSON Schema，用於 Entry.py 建立 structured_llm_fn 時傳入
PROCEDURE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "name": {"type": "string"},
        "description": {"type": "string"},
        "domain": {
            "type": "string",
            "enum": ["coding", "analysis", "planning", "general"],
        },
        "steps": {"type": "array", "items": {"type": "string"}},
        "trigger_patterns": {"type": "array", "items": {"type": "string"}},
        "source_indices": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["found"],
}

_EXTRACT_PROMPT = """\
你是一個 SOP 提取專家。分析以下對話，判斷是否有可複用的操作步驟或決策流程。

## 對話記錄

{episodes_text}

## 輸出格式

若存在可複用的程序，輸出 JSON:
```json
{{
  "found": true,
  "name": "程序名稱（簡短）",
  "description": "一句話描述這個程序的用途",
  "domain": "coding|analysis|planning|general",
  "steps": ["步驟1", "步驟2", "步驟3"],
  "trigger_patterns": ["觸發關鍵詞1", "觸發關鍵詞2"],
  "source_indices": [0, 2]
}}
```

若無可複用的程序，輸出:
```json
{{"found": false}}
```

## 規則
- 只提取有明確步驟順序、可被未來重複執行的操作流程
- 不要把一般性對話當成程序
- steps 至少要有 2 個步驟
- domain 選擇：coding（程式開發）、analysis（分析研究）、planning（規劃決策）、general（其他）
- trigger_patterns 是會觸發這個程序的關鍵詞或情境描述
"""


@dataclass
class ProcedureExtractionReport:
    """程序提取報告"""

    episodes_scanned: int = 0
    procedures_created: int = 0
    procedures_updated: int = 0
    applies_to_edges: int = 0
    skipped_reason: str | None = None
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.skipped_reason:
            return f"程序提取跳過: {self.skipped_reason}"
        return (
            f"程序提取完成: 掃描 {self.episodes_scanned} 個 Episode, "
            f"新增 {self.procedures_created} / 更新 {self.procedures_updated} 個 Procedure"
        )


class ProcedureExtractor:
    """
    從 Episode 中提取可複用的程序/SOP。

    Usage:
        extractor = ProcedureExtractor(
            procedural=procedural,
            semantic=semantic,
            graph=graph,
            llm_fn=my_llm_call,
        )
        report = extractor.run(episodes)
    """

    def __init__(
        self,
        procedural: ProceduralMemory,
        semantic: SemanticMemory | None = None,
        graph: MemoryGraph | None = None,
        llm_fn: Callable[[str], str] | None = None,
        structured_llm_fn: Callable[[str], dict] | None = None,
        guard: Guard | None = None,
        episodic: Any | None = None,
    ):
        """
        Args:
            procedural: ProceduralMemory 實例
            semantic: SemanticMemory 實例（可選，用於建立 APPLIES_TO 邊）
            graph: MemoryGraph 實例（可選，用於建立 APPLIES_TO 邊）
            llm_fn: 文字 LLM 呼叫函式 (prompt: str) -> str。
                    若為 None 且無 structured_llm_fn，程序提取會優雅跳過。
            structured_llm_fn: 結構化 LLM 呼叫函式 (prompt: str) -> dict。
                    由 LLMRouter.get_structured_llm_fn(PROCEDURE_RESPONSE_SCHEMA) 產生，
                    保證回傳合法 JSON dict，不需要手動 parse。
                    若提供，優先使用；若呼叫失敗，不會再 fallback 到 llm_fn。
        """
        self.procedural = procedural
        # guard：鎖 context manager 工廠，LLM 在鎖外，寫入在短臨界區；None 時不加鎖
        # episodic：提供時，寫回前比對來源 Episode 是否已過期
        self._guard = resolve_guard(guard)
        self._episodic = episodic
        self.semantic = semantic
        self.graph = graph
        self.llm_fn = llm_fn
        self._structured_llm_fn = structured_llm_fn

    def run(self, episodes: list[Episode]) -> ProcedureExtractionReport:
        """
        對給定的 Episode 列表執行程序提取。

        Args:
            episodes: 要分析的 Episode 列表

        Returns:
            ProcedureExtractionReport
        """
        report = ProcedureExtractionReport()

        if self.llm_fn is None and self._structured_llm_fn is None:
            report.skipped_reason = "no_llm"
            return report

        if not episodes:
            report.skipped_reason = "no_episodes"
            return report

        report.episodes_scanned = len(episodes)

        # 臨界區（短）：記錄來源 Episode 版本快照，LLM 在鎖外
        snapshot = None
        if self._episodic is not None:
            with self._guard():
                snapshot = snapshot_episodes(episodes)

        # 建構 prompt
        prompt = self._build_prompt(episodes)

        if self._structured_llm_fn is not None:
            # 結構化路徑：直接回傳 dict，不需要手動 parse
            try:
                data = self._structured_llm_fn(prompt)
                # 驗證 found 欄位：若無程序則 data 中 found=False 或缺少必要欄位
                if not data.get("found", False):
                    result = None
                elif not data.get("name") or not data.get("steps"):
                    result = None
                elif len(data.get("steps", [])) < 2:
                    result = None
                else:
                    result = data
            except Exception as e:
                report.skipped_reason = "llm_error"
                report.errors.append(f"結構化 LLM 呼叫失敗: {e}")
                return report
        else:
            # 文字路徑（fallback）：呼叫 llm_fn 後再解析
            try:
                raw_output = self.llm_fn(prompt)
            except Exception as e:
                report.skipped_reason = "llm_error"
                report.errors.append(f"LLM 呼叫失敗: {e}")
                return report

            # 解析（保留原有 _parse_llm_output 三層容錯邏輯）
            try:
                result = self._parse_llm_output(raw_output)
            except Exception as e:
                report.skipped_reason = "llm_error"
                report.errors.append(f"解析失敗: {e}")
                return report

        if result is None:
            # LLM 判斷無可複用程序
            return report

        # 寫入 ProceduralMemory（鎖內短臨界區；來源過期則丟棄）
        try:
            with self._guard():
                stale_ids: set[str] = set()
                if snapshot is not None:
                    stale_ids = find_stale_episode_ids(
                        self._episodic, snapshot, "ProcedureExtractor",
                    )
                if len(stale_ids) >= len(episodes):
                    report.skipped_reason = "stale"
                    logger.info("[ProcedureExtractor] 來源 Episode 全數過期，丟棄結果")
                    return report
                self._ingest_procedure(result, episodes, report, stale_ids)
        except Exception as e:
            report.errors.append(f"程序寫入失敗: {e}")
            logger.error(f"[ProcedureExtractor] 寫入失敗: {e}")

        logger.info(f"[ProcedureExtractor] {report.summary()}")
        return report

    def _build_prompt(self, episodes: list[Episode]) -> str:
        lines = []
        for i, ep in enumerate(episodes):
            lines.append(
                f"[{i}] 使用者: {ep.user_input}\n"
                f"    助手: {ep.agent_response}\n"
                f"    摘要: {ep.content_summary}"
            )
        return _EXTRACT_PROMPT.format(episodes_text="\n\n".join(lines))

    def _parse_llm_output(self, raw: str) -> dict[str, Any] | None:
        """解析 LLM 輸出，容錯 markdown fences。"""
        text = raw.strip()

        # 去 markdown fences
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
            return None

        data = json.loads(text[start:end + 1])

        if not data.get("found", False):
            return None

        # 驗證必要欄位
        if not data.get("name") or not data.get("steps"):
            return None

        if len(data.get("steps", [])) < 2:
            return None

        return data

    def _ingest_procedure(
        self,
        data: dict[str, Any],
        source_episodes: list[Episode],
        report: ProcedureExtractionReport,
        stale_ids: set[str] | None = None,
    ) -> None:
        """寫入 Procedure + 建立 APPLIES_TO 邊。"""
        name = data["name"]
        description = data.get("description", "")
        domain = data.get("domain", "general")
        steps = data["steps"]
        trigger_patterns = data.get("trigger_patterns", [])
        source_indices = data.get("source_indices", [])

        # 確定來源 Episode
        source_ids = []
        for idx in source_indices:
            if isinstance(idx, int) and 0 <= idx < len(source_episodes):
                source_ids.append(source_episodes[idx].id)
        if not source_ids:
            source_ids = [ep.id for ep in source_episodes]
        if stale_ids:
            source_ids = [i for i in source_ids if i not in stale_ids]
            if not source_ids:
                logger.info("[ProcedureExtractor] 指定來源皆已過期，丟棄結果")
                report.skipped_reason = "stale"
                return

        # 檢查是否已存在
        existing = self.procedural.get_by_name(name)
        is_new = existing is None

        procedure = Procedure(
            name=name,
            description=description,
            steps=steps,
            domain=domain,
            trigger_patterns=trigger_patterns,
            source_episode_ids=source_ids,
        )

        proc_id = self.procedural.write(procedure)

        if is_new:
            report.procedures_created += 1
        else:
            report.procedures_updated += 1

        # 建立 APPLIES_TO 邊（Procedure → 相關 Concept）
        if self.graph is not None and self.semantic is not None:
            self._link_to_concepts(proc_id, name, description, domain, report)

    def _link_to_concepts(
        self,
        proc_id: str,
        name: str,
        description: str,
        domain: str,
        report: ProcedureExtractionReport,
    ) -> None:
        """嘗試將 Procedure 與相關 Concept 建立 APPLIES_TO 邊。"""
        if self.semantic.count() == 0:
            return

        # 確保 Procedure 節點存在
        if not self.graph.has_node(proc_id):
            self.graph.add_procedure_node(proc_id, name, domain)

        # 用 embed_fn 搜索相關 Concept
        if self.semantic._embed_fn is not None:
            text = f"{name}: {description}"
            query_vec = self.semantic._embed_fn(text)
            related = self.semantic.search_by_vector(query_vec, top_k=3, min_similarity=0.4)

            for concept, sim in related:
                if not self.graph.has_node(concept.id):
                    self.graph.add_concept_node(concept.id, concept.name, concept.concept_type)
                self.graph.add_applies_to_edge(proc_id, concept.id, weight=sim)
                report.applies_to_edges += 1
