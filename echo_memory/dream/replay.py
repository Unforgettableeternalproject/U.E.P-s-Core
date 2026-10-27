"""
LiDER Full Replay — LLM 驅動的反思重播

對應大腦結構：睡眠 REM 期海馬迴重播（Hippocampal Replay）

職責：
  1. 取高顯著性未重播 Episode
  2. LLM 反思：生成摘要 + 識別與其他 Episode 的關聯
  3. 更新圖譜邊權重（反思後的邊更強）
  4. 記錄處理過的 Episode ID（由 DreamEngine 統一標記 is_dreamed）
  5. 無 LLM 時優雅降級

Dream 完整週期（整合後）：
  衰減更新 → 圖譜修剪 → **重播** → 蒸餾

設計決策：
  - 重播和蒸餾是獨立的：重播強化既有連結，蒸餾提取新概念
  - 重播在蒸餾之前執行（先強化再提取更合理）
  - LLM 輸出 JSON：reflection + related_indices + importance_adjustment
  - 支援結構化 LLM 呼叫（structured_llm_fn）以保證合法 JSON 回傳
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from echo_memory.schemas.episode import Episode
from echo_memory.storage.episodic import EpisodicMemory
from echo_memory.dream.staleness import (
    Guard,
    find_stale_episode_ids,
    resolve_guard,
    snapshot_episodes,
)
from echo_memory.storage.graph import MemoryGraph

logger = logging.getLogger("echo_memory.dream.replay")

# 結構化 LLM 呼叫的 JSON Schema，用於 Entry.py 建立 structured_llm_fn 時傳入
REPLAY_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "reflections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "reflection": {"type": "string"},
                    "importance": {"type": "number"},
                    "related_indices": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                },
                "required": ["index", "reflection", "importance"],
            },
        },
    },
    "required": ["reflections"],
}

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
    # 內部追蹤：被重播的 Episode ID，供 DreamEngine 統一標記 is_dreamed
    _replayed_episode_ids: list[str] = field(default_factory=list, repr=False)

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
        patch_coordinator=None,
        structured_llm_fn: Callable[[str], dict] | None = None,
        guard: Guard | None = None,
    ):
        """
        Args:
            episodic: EpisodicMemory 實例
            graph: MemoryGraph 實例
            llm_fn: 文字 LLM 呼叫函式 (prompt: str) -> str。
                    無 structured_llm_fn 時作為主路徑，有時作為 fallback。
            patch_coordinator: DreamPatchCoordinator 實例（可選）。
            structured_llm_fn: 結構化 LLM 呼叫函式 (prompt: str) -> dict。
                    由 LLMRouter.get_structured_llm_fn(REPLAY_RESPONSE_SCHEMA) 產生，
                    保證回傳合法 JSON dict，不需要手動 parse。
                    若提供，優先使用；若呼叫失敗，不會再 fallback 到 llm_fn。
            guard: 鎖 context manager 工廠（通常為 storage_lock）。
                    只在「收集候選」與「套用結果」兩個短臨界區持有，LLM 呼叫在鎖外。
                    None 時不加鎖。
        """
        self.episodic = episodic
        self.graph = graph
        self.llm_fn = llm_fn
        self._patch_coordinator = patch_coordinator
        self._structured_llm_fn = structured_llm_fn
        self._guard = resolve_guard(guard)

    def run(
        self,
        min_salience: float | None = None,
        max_episodes: int | None = None,
    ) -> ReplayReport:
        """執行一次反思重播。"""
        report = ReplayReport()

        if self.llm_fn is None and self._structured_llm_fn is None:
            report.skipped_reason = "no_llm"
            logger.info("[LiDERReplay] 無 LLM 函式，跳過重播")
            return report

        threshold = min_salience if min_salience is not None else self.MIN_SALIENCE_THRESHOLD
        batch_size = max_episodes if max_episodes is not None else self.MAX_EPISODES_PER_BATCH

        # 臨界區 1（短）：收集候選並記錄版本快照
        with self._guard():
            candidates = self._collect_candidates(threshold, batch_size)
            snapshot = snapshot_episodes(candidates)
        report.episodes_replayed = len(candidates)

        if not candidates:
            report.skipped_reason = "no_episodes"
            return report

        # 建構 prompt + 呼叫 LLM
        prompt = self._build_prompt(candidates)

        if self._structured_llm_fn is not None:
            # 結構化路徑：直接回傳 dict，不需要手動 parse
            try:
                data = self._structured_llm_fn(prompt)
                reflections = data.get("reflections", [])
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

            # 解析反思結果（內建多層容錯，不應再拋例外）
            try:
                reflections = self._parse_llm_output(raw_output)
            except Exception as e:
                report.skipped_reason = "parse_error"
                report.errors.append(f"解析失敗: {e}")
                return report

        if not reflections:
            report.skipped_reason = "parse_empty"
            report.errors.append("LLM 回傳的 JSON 無法解析出任何 reflection")
            return report

        # 臨界區 2（短）：LLM 已在鎖外完成，鎖內比對快照後套用
        with self._guard():
            return self._apply_reflections(candidates, snapshot, reflections, report)

    def _apply_reflections(
        self,
        candidates: list[Episode],
        snapshot: dict[str, tuple[float, bool]],
        reflections: list[dict[str, Any]],
        report: ReplayReport,
    ) -> ReplayReport:
        """鎖內套用反思結果；已過期（刪除 / 變動）的 Episode 一律略過。"""
        stale_ids = find_stale_episode_ids(self.episodic, snapshot, "LiDERReplay")
        if stale_ids:
            report.errors.append(f"{len(stale_ids)} 個 Episode 在 LLM 期間已過期，已丟棄")
        if len(stale_ids) >= len(candidates):
            report.skipped_reason = "stale"
            return report

        def _valid(i: Any) -> bool:
            return (
                isinstance(i, int)
                and 0 <= i < len(candidates)
                and candidates[i].id not in stale_ids
            )

        # ── 預掃描：計算預期 mutation 規模（供 audit-before-mutate）──
        expected_salience_updates = 0
        expected_edges = 0
        importances = []
        for ref in reflections:
            idx = ref.get("index", -1)
            if not _valid(idx):
                continue
            importance = min(1.0, max(0.0, ref.get("importance", 0.5)))
            importances.append(importance)
            expected_salience_updates += 1
            for rel_idx in ref.get("related_indices", []):
                if _valid(rel_idx) and rel_idx != idx:
                    expected_edges += 1

        # ── audit-before-mutate ──
        if self._patch_coordinator is not None and (expected_salience_updates or expected_edges):
            if not self._patch_coordinator.audit_replay_batch(
                edges_reinforced=0,  # 預估值，強化 vs 新建比例未知
                edges_created=expected_edges,
                salience_updates=expected_salience_updates,
            ):
                logger.warning(
                    "[LiDERReplay] audit 失敗，跳過 replay mutation"
                )
                report.skipped_reason = "audit_failure"
                report._replayed_episode_ids = [ep.id for ep in candidates if ep.id not in stale_ids]
                return report

        # ── 應用反思：更新圖譜 ──
        importances.clear()
        for ref in reflections:
            idx = ref.get("index", -1)
            if not _valid(idx):
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
                if not _valid(rel_idx):
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
                    with self.graph.lock:
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

        # 注意：不在此處標記 is_dreamed。
        report._replayed_episode_ids = [ep.id for ep in candidates if ep.id not in stale_ids]

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
        """
        解析 LLM 反思輸出。

        容錯策略：
        1. 去除 markdown code fences
        2. 嘗試完整 JSON parse
        3. 若失敗（常見於 max_tokens 截斷），嘗試修復截斷的 JSON
        4. 若仍失敗，嘗試逐行提取完整的 reflection 物件
        """
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

        json_text = text[start:end + 1]

        # 嘗試 1：完整 parse
        try:
            data = json.loads(json_text)
            return data.get("reflections", [])
        except json.JSONDecodeError:
            pass

        # 嘗試 2：截斷修復 — 在最後一個完整的 } 後補上 ]}
        # LLM 常因 max_tokens 在 reflections 陣列中間截斷
        logger.debug("[LiDERReplay] JSON 完整 parse 失敗，嘗試截斷修復")
        for suffix in ["]}", "]}}", "]\n}}"]:
            # 找到最後一個完整的 reflection 物件結尾
            last_obj_end = json_text.rfind("}")
            if last_obj_end == -1:
                continue
            # 往前找倒數第二個 }（最後一個完整 reflection 的結尾）
            second_last = json_text.rfind("}", 0, last_obj_end)
            if second_last == -1:
                continue
            truncated = json_text[:second_last + 1] + suffix
            try:
                data = json.loads(truncated)
                reflections = data.get("reflections", [])
                if reflections:
                    logger.info(
                        f"[LiDERReplay] 截斷修復成功，恢復 {len(reflections)} 個 reflection"
                    )
                    return reflections
            except json.JSONDecodeError:
                continue

        # 嘗試 3：逐個提取 reflection 物件
        logger.debug("[LiDERReplay] 截斷修復失敗，嘗試逐個提取")
        reflections = []
        search_start = 0
        while True:
            # 找到 "index" 關鍵字的位置
            idx_pos = json_text.find('"index"', search_start)
            if idx_pos == -1:
                break
            # 往前找 {
            obj_start = json_text.rfind("{", search_start, idx_pos)
            if obj_start == -1:
                search_start = idx_pos + 1
                continue
            # 往後找配對的 }
            depth = 0
            obj_end = -1
            for i in range(obj_start, len(json_text)):
                if json_text[i] == "{":
                    depth += 1
                elif json_text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        obj_end = i
                        break
            if obj_end == -1:
                break
            try:
                obj = json.loads(json_text[obj_start:obj_end + 1])
                if "index" in obj:
                    reflections.append(obj)
            except json.JSONDecodeError:
                pass
            search_start = obj_end + 1

        if reflections:
            logger.info(
                f"[LiDERReplay] 逐個提取成功，恢復 {len(reflections)} 個 reflection"
            )
        else:
            logger.warning("[LiDERReplay] 所有 JSON 解析策略均失敗")

        return reflections

    def _mark_episodes_dreamed(self, episodes: list[Episode]) -> None:
        episode_ids = [ep.id for ep in episodes]
        self.episodic.mark_as_dreamed(episode_ids)
