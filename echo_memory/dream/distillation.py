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
  - 支援結構化 LLM 呼叫（structured_llm_fn）以保證合法 JSON 回傳
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from echo_memory.config import get_config
from echo_memory.dream.staleness import (
    Guard,
    find_stale_episode_ids,
    resolve_guard,
    snapshot_episodes,
)
from echo_memory.schemas.episode import Episode
from echo_memory.storage.episodic import EpisodicMemory
from echo_memory.storage.graph import MemoryGraph
from echo_memory.storage.semantic import SemanticMemory
from echo_memory.utils.source_view import (
    SOURCE_LEGEND,
    SUPPORT_AGENT_ONLY,
    load_units,
    render_episode_block,
    support_source,
    units_for,
)

logger = logging.getLogger("echo_memory.dream.distillation")

# 結構化 LLM 呼叫的 JSON Schema，用於 Entry.py 建立 structured_llm_fn 時傳入
# 對應 _DISTILLATION_PROMPT 期望的 JSON 陣列結構（包裝成物件以符合 structured output 規範）
DISTILLATION_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "concepts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "concept_type": {
                        "type": "string",
                        "enum": ["entity", "rule", "abstract", "pattern"],
                    },
                    "subject": {
                        "type": "string",
                        "enum": ["user", "agent", "world"],
                    },
                    "source_indices": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                },
                "required": ["name", "description"],
            },
        },
    },
    "required": ["concepts"],
}

# 蒸餾 prompt 模板
_DISTILLATION_PROMPT = """\
你是一個知識蒸餾專家。分析以下對話記錄，從中提取值得長期記住的知識。

你是一個陪伴型 agent 的長期記憶。**最重要的是認識使用者**：他們的事實
（家人、寵物、工作、計畫）、偏好（喜歡/討厭什麼）、以及對你提出的要求或修正
（例如「不要每次糾正我的英文」）。這些構成使用者的 profile，優先提取。
關於你自己回覆風格的歸納次要，沒有明確規律時不要硬湊。

{source_legend}

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
    "subject": "user | agent | world",
    "source_indices": [0, 2]
  }}
]
```

## 規則
- 只提取跨多個對話出現的模式或可泛化的知識
- 不要重複原始對話內容，要「抽象」和「歸納」
- concept_type 選擇：entity（人/地/物）、rule（規則/偏好）、abstract（抽象概念）、pattern（行為模式）
- subject 選擇：user（關於使用者本人——事實、偏好、對你的要求）、agent（關於你自己的行為規則）、world（其他知識）
- subject=user 的概念只能由「使用者說」（含「來源未確認」的使用者輸入）支撐；只出現在助手回覆的內容，不得標成 subject=user
- 使用者說過一次的具體事實（「我妹妹要去筑波大學」）就值得提取為 subject=user 的 entity，不必等重複出現
- description 用第三人稱陳述使用者（「使用者的妹妹即將就讀筑波大學」），不要寫「我」
- source_indices 是上面對話記錄的索引（從 0 開始）
- 最多輸出 5 個概念
- 如果沒有值得提取的概念，輸出空陣列 []
"""


class _StaleSources(Exception):
    """概念的來源 Episode 在 LLM 期間全數過期（刪除或變動）。"""


@dataclass
class DistillationReport:
    """單次蒸餾的報告"""

    episodes_scanned: int = 0
    concepts_created: int = 0
    concepts_updated: int = 0
    concepts_rejected: int = 0  # 被 ConceptInhibitor 擋下的
    concepts_stale: int = 0  # LLM 期間來源 Episode 已過期而丟棄的
    edges_created: int = 0
    relates_to_edges_created: int = 0  # 依來源重疊建立的 Concept↔Concept RELATES_TO
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
            f"新增 {self.concepts_created} / 更新 {self.concepts_updated}"
            f"{f' / 拒絕 {self.concepts_rejected}' if self.concepts_rejected else ''} 個 Concept, "
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
    _MAX_RELATES_PER_CONCEPT = 8  # 單一概念單次蒸餾最多新建的 RELATES_TO 數

    def __init__(
        self,
        episodic: EpisodicMemory,
        semantic: SemanticMemory,
        graph: MemoryGraph,
        llm_fn: Callable[[str], str] | None = None,
        contradiction_detector: Any | None = None,
        patch_coordinator: Any | None = None,
        structured_llm_fn: Callable[[str], dict] | None = None,
        guard: Guard | None = None,
        evidence_store: Any | None = None,
    ):
        """
        Args:
            episodic: EpisodicMemory 實例
            semantic: SemanticMemory 實例
            graph: MemoryGraph 實例（共用的圖）
            llm_fn: 文字 LLM 呼叫函式 (prompt: str) -> str。
                    若為 None 且無 structured_llm_fn，蒸餾會優雅跳過。
            contradiction_detector: ContradictionDetector 實例（可選）。
                    若提供，每次 ingest concept 後自動偵測矛盾。
            patch_coordinator: DreamPatchCoordinator 實例（可選）。
                    若提供，SemanticMemory 寫入走 patch pipeline。
            structured_llm_fn: 結構化 LLM 呼叫函式 (prompt: str) -> dict。
                    由 LLMRouter.get_structured_llm_fn(DISTILLATION_RESPONSE_SCHEMA) 產生，
                    保證回傳合法 JSON dict，回傳結構為 {"concepts": [...]}。
                    若提供，優先使用；若呼叫失敗，不會再 fallback 到 llm_fn。
            guard: 鎖 context manager 工廠（通常為 storage_lock）。
                    LLM 呼叫（蒸餾與矛盾偵測）都在鎖外，只有讀候選與寫入在短臨界區。
                    None 時不加鎖。
            evidence_store: EvidenceStore 實例（可選）。提供時蒸餾 prompt 依來源標示
                    Episode 內容；None 時所有 Episode 視為 legacy_unknown（保守）。
        """
        self._guard = resolve_guard(guard)
        self._evidence_store = evidence_store
        self.episodic = episodic
        self.semantic = semantic
        self.graph = graph
        self.llm_fn = llm_fn
        self._contradiction_detector = contradiction_detector
        self._patch_coordinator = patch_coordinator
        self._structured_llm_fn = structured_llm_fn
        self._relates_to_created = 0  # 單次 run 累計，run() 開頭歸零
        if patch_coordinator is None:
            # 未注入時建立降級 Gate（無 LLM 的 ConceptInhibitor），仍走 Patch Gate 留 audit
            from echo_memory.dream.patch_coordinator import DreamPatchCoordinator
            self._patch_coordinator = DreamPatchCoordinator(semantic=semantic)
            logger.warning(
                "[KnowledgeDistillation] 未注入 patch_coordinator，使用降級 Gate（無 LLM）"
            )
        elif not getattr(patch_coordinator, "has_inhibitor", True):
            # 注入的 coordinator 沒有 inhibitor：補降級 Gate，不退回直寫 SemanticMemory
            patch_coordinator.ensure_gate(semantic)
            logger.warning(
                "[KnowledgeDistillation] patch_coordinator 沒有 inhibitor，補上降級 Gate（無 LLM）"
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
        self._relates_to_created = 0

        # 無 LLM → 優雅降級
        if self.llm_fn is None and self._structured_llm_fn is None:
            report.skipped_reason = "no_llm"
            logger.info("[KnowledgeDistillation] 無 LLM 函式，跳過蒸餾")
            return report

        # 收集候選 Episode
        threshold = min_salience if min_salience is not None else self.MIN_SALIENCE_THRESHOLD
        batch_size = max_episodes if max_episodes is not None else self.MAX_EPISODES_PER_BATCH

        # 臨界區：收集候選並記錄版本快照（LLM 在鎖外）
        with self._guard():
            candidates = self._collect_candidates(threshold, batch_size)
            snapshot = snapshot_episodes(candidates)
        report.episodes_scanned = len(candidates)

        if not candidates:
            report.skipped_reason = "no_episodes"
            logger.info("[KnowledgeDistillation] 無符合條件的 Episode，跳過蒸餾")
            return report

        # 批次取 evidence（鎖外、不呼叫 LLM、只讀一次 evidence.jsonl）
        units_by_episode = load_units(self._evidence_store, candidates)

        # 建構 prompt
        prompt = self._build_prompt(candidates, units_by_episode)

        if self._structured_llm_fn is not None:
            # 結構化路徑：直接回傳 dict，不需要手動 parse
            # 回傳格式為 {"concepts": [...]}
            try:
                data = self._structured_llm_fn(prompt)
                extracted = data.get("concepts", [])
                # 驗證必要欄位，過濾無效項目
                extracted = [
                    item for item in extracted
                    if isinstance(item, dict) and "name" in item and "description" in item
                ][:5]  # 最多 5 個，與文字路徑一致
                report.llm_raw_output = None  # 結構化路徑無 raw 文字
            except Exception as e:
                report.skipped_reason = "llm_error"
                report.errors.append(f"結構化 LLM 呼叫失敗: {e}")
                logger.error(f"[KnowledgeDistillation] 結構化 LLM 呼叫失敗: {e}")
                return report
        else:
            # 文字路徑（fallback）：呼叫 llm_fn 後再解析
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
                ingested = self._ingest_concept(
                    concept_data, candidates, snapshot=snapshot,
                    units_by_episode=units_by_episode,
                )
                if ingested is None:
                    report.concepts_rejected += 1
                    continue
                created, edges, contradicts = ingested
                if created:
                    report.concepts_created += 1
                else:
                    report.concepts_updated += 1
                report.edges_created += edges
                report.contradicts_edges_created += contradicts
            except _StaleSources:
                report.concepts_stale += 1
                logger.info(
                    "[KnowledgeDistillation] 概念 %s 的來源 Episode 已過期，丟棄",
                    concept_data.get("name", "?"),
                )
            except Exception as e:
                report.errors.append(f"概念寫入失敗: {concept_data.get('name', '?')}: {e}")
                logger.error(f"[KnowledgeDistillation] 概念寫入失敗: {e}")

        report.relates_to_edges_created = self._relates_to_created

        # 只在有實際產出時才記錄 Episode ID（供 DreamEngine 統一標記）
        # Major #3 修復：空結果或全部失敗時不標記，允許後續重試
        if report.concepts_created > 0 or report.concepts_updated > 0:
            with self._guard():
                stale_ids = find_stale_episode_ids(
                    self.episodic, snapshot, "KnowledgeDistillation",
                )
            report._distilled_episode_ids = [
                ep.id for ep in candidates if ep.id not in stale_ids
            ]

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

    def _build_prompt(
        self,
        episodes: list[Episode],
        units_by_episode: dict[str, list] | None = None,
    ) -> str:
        """從 Episode 列表建構蒸餾 prompt；每段內容依 evidence 標示來源。"""
        lines = [
            render_episode_block(i, ep, units_for(ep, units_by_episode))
            for i, ep in enumerate(episodes)
        ]
        episodes_text = "\n\n".join(lines)
        return _DISTILLATION_PROMPT.format(
            source_legend=SOURCE_LEGEND, episodes_text=episodes_text,
        )

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
        snapshot: dict[str, tuple[float, bool]] | None = None,
        units_by_episode: dict[str, list] | None = None,
    ) -> tuple[bool, int, int] | None:
        """
        將蒸餾出的概念寫入 SemanticMemory 和 MemoryGraph。
        Inhibitor 拒絕時回傳 None（不算 created/updated、也不算錯誤）。

        鎖語意：寫入在短臨界區內完成；矛盾偵測的 LLM 在鎖外，
        其邊的登記由 detector 自行在鎖內套用。

        Args:
            concept_data: LLM 輸出的概念字典
            source_episodes: 來源 Episode 列表
            snapshot: 候選 Episode 版本快照；提供時鎖內先剔除已過期來源，
                全數過期則拋出 _StaleSources

        Returns:
            (is_new, edges_created, contradicts_created)
        """
        # 語意相近預檢（embedding + 可選 LLM）：必須在持有 storage_lock 之前完成
        similar_hint = None
        precheck = getattr(self._patch_coordinator, "precheck_similar", None)
        if precheck is not None and concept_data.get("name"):
            similar_hint = precheck(
                str(concept_data["name"]), str(concept_data.get("description", "")),
            )
        with self._guard():
            written = self._write_concept_locked(
                concept_data, source_episodes, snapshot, units_by_episode,
                similar_hint=similar_hint,
            )
        if written is None:
            return None
        is_new, concept_id, edges_created = written

        # 矛盾偵測（若有 detector）— audit-before-mutate；LLM 在鎖外
        contradicts_created = 0
        if self._contradiction_detector is not None and concept_id is not None:
            # audit-before-mutate：先取得 audit 許可再偵測
            can_detect = True
            if self._patch_coordinator is not None:
                can_detect = self._patch_coordinator.audit_contradiction_detection(
                    concept_id, -1,  # -1 表示預 audit（數量未知）
                )
            if can_detect:
                with self._guard():
                    concept = self.semantic.get(concept_id)
                if concept is not None:
                    try:
                        edges = self._contradiction_detector.detect_and_register(concept)
                        contradicts_created = len(edges)
                    except Exception as e:
                        logger.warning(
                            f"[KnowledgeDistillation] 矛盾偵測失敗（不影響蒸餾）: {e}"
                        )
            else:
                logger.warning(
                    "[KnowledgeDistillation] audit 失敗，跳過矛盾偵測 (%s)",
                    concept_id[:12],
                )

        return is_new, edges_created, contradicts_created

    def _write_concept_locked(
        self,
        concept_data: dict[str, Any],
        source_episodes: list[Episode],
        snapshot: dict[str, tuple[float, bool]] | None,
        units_by_episode: dict[str, list] | None = None,
        similar_hint: Any = None,
    ) -> tuple[bool, str, int] | None:
        """鎖內寫入 Concept 與圖譜邊。回傳 (is_new, concept_id, edges_created)。"""
        stale_ids: set[str] = set()
        if snapshot:
            stale_ids = find_stale_episode_ids(
                self.episodic, snapshot, "KnowledgeDistillation",
            )
        name = concept_data["name"]
        description = concept_data["description"]
        concept_type = concept_data.get("concept_type", "abstract")
        subject = concept_data.get("subject") or "world"
        if subject not in ("user", "agent", "world"):
            subject = "world"
        source_indices = concept_data.get("source_indices", [])

        # 確定來源 Episode
        source_eps = []
        for idx in source_indices:
            if isinstance(idx, int) and 0 <= idx < len(source_episodes):
                source_eps.append(source_episodes[idx])

        # 如果 LLM 沒指定 source_indices，就用全部候選 Episode
        if not source_eps:
            source_eps = source_episodes

        # 剔除 LLM 期間已刪除 / 已變動的來源；全數過期則丟棄整個概念
        if stale_ids:
            source_eps = [ep for ep in source_eps if ep.id not in stale_ids]
            if not source_eps:
                raise _StaleSources(name)

        # 檢查是否已存在（判斷 is_new）
        existing = self.semantic.get_by_name(name)
        is_new = existing is None

        source_ep_ids = [ep.id for ep in source_eps]

        # 支撐來源：只由 agent／工具內容支撐的概念不得標成使用者的事實或偏好
        support = support_source(source_eps, units_by_episode)
        if subject == "user" and support == SUPPORT_AGENT_ONLY:
            logger.info(
                "[KnowledgeDistillation] 概念 %s 無使用者陳述支撐，subject user -> world",
                name,
            )
            subject = "world"

        # ── SemanticMemory 寫入：一律走 DreamPatchCoordinator → ConceptInhibitor ──
        # （無 inhibitor 的 coordinator 已在 __init__ 補降級 Gate，不再有直寫退路）
        # support_source 與 subject 一樣隨 create／expand patch 攜帶（有 audit、可 rollback）
        submit_kwargs: dict[str, Any] = {
            "name": name,
            "description": description,
            "concept_type": concept_type,
            "source_episode_ids": source_ep_ids,
            "subject": subject,
            "support_source": support,
        }
        if similar_hint is not None:
            submit_kwargs["similar_hint"] = similar_hint
        concept_id, _tx = self._patch_coordinator.submit_distillation_concept(**submit_kwargs)

        if concept_id is None:
            # Inhibitor 拒絕——不是錯誤，也不算 created/updated
            return None

        # subject／support_source 都在 patch pipeline 內隨 create／expand patch 寫入（有 patch trail）

        # 在 MemoryGraph 中註冊概念節點（若不存在）
        # 已合併 / 非 active 的概念不補節點（合併時已摘除圖譜節點）
        stored = self.semantic.get(concept_id)
        if stored is not None and stored.name != name:
            # Gate 判定與既有概念為同一件事，已改以 expand 補入該概念：不算新建
            is_new = False
            name = stored.name
            concept_type = stored.concept_type
        if stored is not None and (stored.status != "active" or stored.metadata.get("merged_into")):
            return is_new, concept_id, 0
        if not self.graph.has_node(concept_id):
            self.graph.add_concept_node(concept_id, name, concept_type)

        # 建立 DERIVED_FROM 邊（Concept → Episode）— 加 audit
        edges_created = 0
        ep_ids_for_edge = [ep.id for ep in source_eps if not self.graph.graph.has_edge(concept_id, ep.id)]
        # Audit-before-mutate
        can_create_edges = True
        if ep_ids_for_edge and self._patch_coordinator is not None:
            can_create_edges = self._patch_coordinator.audit_graph_derived_from(
                concept_id, ep_ids_for_edge,
            )

        # 逐條紀錄（每條 DERIVED_FROM 邊一筆）；audit 失敗同樣阻止建邊
        if can_create_edges and ep_ids_for_edge and self._patch_coordinator is not None:
            can_create_edges = self._patch_coordinator.record_mutations("distillation", [
                {
                    "mutation": "edge_add",
                    "target": {"from": concept_id, "to": ep_id, "edge_type": "DERIVED_FROM"},
                    "source_episode_ids": [ep_id],
                    "source_concept_ids": [concept_id],
                    "prior": None,
                    "new": {"weight": 0.7},
                    "reason": "distillation_derived_from",
                }
                for ep_id in ep_ids_for_edge
            ])

        if can_create_edges:
            for ep in source_eps:
                if not self.graph.has_node(ep.id):
                    self.graph.add_episode_node(ep)
                if not self.graph.graph.has_edge(concept_id, ep.id):
                    self.graph.add_derived_from_edge(concept_id, ep.id)
                    edges_created += 1

        # 來源重疊 → Concept 間 RELATES_TO（M-7）；同一鎖內、audit-before-mutate
        self._relates_to_created += self._link_overlapping_concepts(
            concept_id, set(ep.id for ep in source_eps),
        )

        return is_new, concept_id, edges_created

    def _link_overlapping_concepts(
        self,
        concept_id: str,
        batch_source_ids: set[str],
    ) -> int:
        """
        依來源 Episode 的 Jaccard 重疊，在 Concept 間建立 RELATES_TO。須在鎖內呼叫。

        比對對象是 SemanticMemory 內所有其他 active 概念（含本批較早寫入者與既存者），
        所以「同批」與「既存」使用同一條規則。此為結構性證據（共享來源），
        非關鍵字比對。

        規則：
          - jaccard = |A ∩ B| / |A ∪ B|，A 為本概念完整來源（既有 ∪ 本批），B 為對方來源
          - jaccard >= relates_to_min_jaccard 才建邊
          - weight = base + (1 - base) * jaccard（base = relates_to_weight_base）
          - 單一概念每次最多建 _MAX_RELATES_PER_CONCEPT 條（取 jaccard 最高者）
          - 已有 CONTRADICTS 的配對不建；既有 LATENT_BRIDGE 升級為 RELATES_TO

        Returns:
            新增（含由 bridge 升級）的邊數（無向配對計 1）
        """
        concept = self.semantic.get(concept_id)
        if concept is None or concept.status != "active":
            return 0
        cfg = get_config()
        min_jaccard = cfg.relates_to_min_jaccard
        base = min(1.0, max(0.0, cfg.relates_to_weight_base))

        mine = set(concept.source_episode_ids) | batch_source_ids
        if not mine:
            return 0

        candidates: list[tuple[str, float, float]] = []  # (other_id, jaccard, weight)
        for other in self.semantic.list_concepts(limit=None, status="active"):
            if other.id == concept_id or not other.source_episode_ids:
                continue
            theirs = set(other.source_episode_ids)
            inter = len(mine & theirs)
            if inter == 0:
                continue
            jaccard = inter / len(mine | theirs)
            if jaccard < min_jaccard:
                continue
            if not self.graph.has_node(other.id):
                continue
            candidates.append((other.id, jaccard, base + (1.0 - base) * jaccard))

        if not candidates:
            return 0
        candidates.sort(key=lambda c: c[1], reverse=True)
        candidates = candidates[:self._MAX_RELATES_PER_CONCEPT]

        # 排除 CONTRADICTS 配對與已存在且權重不低於新值的 RELATES_TO（不需要寫入）
        pending: list[tuple[str, float, float]] = []
        for other_id, jaccard, weight in candidates:
            if self.graph.has_contradicts_edge(concept_id, other_id):
                continue
            if self.graph.graph.has_edge(concept_id, other_id):
                data = self.graph.graph[concept_id][other_id]
                if data.get("edge_type") == "RELATES_TO" and data.get("weight", 0.0) >= weight:
                    continue
            pending.append((other_id, jaccard, weight))
        if not pending:
            return 0

        # Audit-before-mutate
        if self._patch_coordinator is not None:
            ok = self._patch_coordinator.audit_graph_relates_to(
                concept_id,
                [{"target": o, "jaccard": j, "weight": w} for o, j, w in pending],
            )
            if not ok:
                logger.warning(
                    "[KnowledgeDistillation] audit 失敗，跳過 RELATES_TO 建立 (%s, %d 條)",
                    concept_id[:12], len(pending),
                )
                return 0

        # 逐條紀錄：RELATES_TO 雙向各一筆，前值為既有邊屬性（bridge 升級時即 LATENT_BRIDGE）
        if self._patch_coordinator is not None:
            entries = []
            for other_id, jaccard, weight in pending:
                for src, dst in ((concept_id, other_id), (other_id, concept_id)):
                    prior = (
                        dict(self.graph.graph[src][dst])
                        if self.graph.graph.has_edge(src, dst) else None
                    )
                    entries.append({
                        "mutation": "edge_add" if prior is None else "edge_update",
                        "target": {"from": src, "to": dst, "edge_type": "RELATES_TO"},
                        "source_episode_ids": sorted(
                            set(concept.source_episode_ids) | batch_source_ids
                        ),
                        "source_concept_ids": [concept_id, other_id],
                        "prior": prior,
                        "new": {"weight": weight, "jaccard": jaccard},
                        "reason": "distillation_overlap",
                    })
            if not self._patch_coordinator.record_mutations("distillation", entries):
                logger.warning(
                    "[KnowledgeDistillation] 逐條 audit 失敗，跳過 RELATES_TO 建立 (%s)",
                    concept_id[:12],
                )
                return 0

        created = 0
        for other_id, jaccard, weight in pending:
            if self.graph.upsert_overlap_relates_to(concept_id, other_id, weight, jaccard):
                created += 1
        return created

    def _mark_episodes_dreamed(self, episodes: list[Episode]) -> None:
        """將已蒸餾的 Episode 標記為 is_dreamed=True。"""
        episode_ids = [ep.id for ep in episodes]
        marked = self.episodic.mark_as_dreamed(episode_ids)

        logger.debug(
            f"[KnowledgeDistillation] 標記 {marked}/{len(episodes)} 個 Episode 為 is_dreamed"
        )
