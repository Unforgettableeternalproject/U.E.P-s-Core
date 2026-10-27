"""
Tool Executor — 工具執行器

負責接收 Gemini function call 請求並分派到對應的記憶操作。
每個 handler 回傳 {success: bool, result: str} 格式的結果。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from echo_memory.schemas.evidence import CONTENT_ORIGINS, EvidenceInput, SourceRef

if TYPE_CHECKING:
    from echo_memory.engine import MemoryEngine

logger = logging.getLogger("echo_memory.tools")


class ToolExecutor:
    """
    記憶工具執行器。

    接收來自 LLM 的 function call 並執行對應的記憶操作，
    包含白名單控制（adjust_importance 只能操作當前脈絡中的情節）
    和 Dream 冷卻時間控制。
    """

    def __init__(
        self,
        engine: "MemoryEngine",
        session_id: str,
        dream_cooldown_turns: int = 5,
    ):
        """
        Args:
            engine:               記憶引擎實例
            session_id:           當前對話 session ID
            dream_cooldown_turns: Dream Engine 呼叫之間的最少輪數間隔
        """
        self.engine = engine
        self.session_id = session_id
        self.dream_cooldown_turns = dream_cooldown_turns

        # adjust_importance 只允許操作白名單內的 episode_id（當前脈絡）
        self._context_whitelist: set[str] = set()

        # Dream 冷卻：記錄上次呼叫時的輪數計數
        self._dream_last_turn: int = -999
        self._current_turn: int = 0

    # ──────────────────────────────────────────────────────────────────────
    # 公開 API
    # ──────────────────────────────────────────────────────────────────────

    def update_context_whitelist(self, episode_ids: list[str]) -> None:
        """
        更新 adjust_importance 允許操作的 episode_id 白名單。

        每次 retrieve 後呼叫，確保白名單與當前注入脈絡保持同步。

        Args:
            episode_ids: 目前注入脈絡中的所有 episode ID
        """
        self._context_whitelist = set(episode_ids)
        logger.debug(f"[ToolExecutor] 白名單更新: {len(self._context_whitelist)} 個 episodes")

    def increment_turn(self) -> None:
        """增加輪數計數（用於 Dream 冷卻計算）"""
        self._current_turn += 1

    def execute(self, function_name: str, args: dict) -> dict:
        """
        分派 function call 到對應 handler。

        Args:
            function_name: 工具名稱
            args:          工具參數 dict

        Returns:
            {success: bool, result: str}
        """
        logger.info(f"[ToolExecutor] 執行工具: {function_name}, args={args}")

        handlers = {
            "force_remember": self.handle_force_remember,
            "deep_recall": self.handle_deep_recall,
            "adjust_importance": self.handle_adjust_importance,
            "trigger_dream": self.handle_trigger_dream,
        }

        handler = handlers.get(function_name)
        if handler is None:
            logger.warning(f"[ToolExecutor] 未知工具: {function_name}")
            return {"success": False, "result": f"Unknown tool: {function_name}"}

        try:
            return handler(args)
        except Exception as e:
            logger.exception(f"[ToolExecutor] 工具執行失敗: {function_name}: {e}")
            return {"success": False, "result": f"Tool execution error: {e}"}

    # ──────────────────────────────────────────────────────────────────────
    # 個別 Handler
    # ──────────────────────────────────────────────────────────────────────

    def handle_force_remember(self, args: dict) -> dict:
        """
        強制儲存重要資訊到長期記憶。

        以獨立的 tool_note Episode 存入情節記憶，來源記為 tool 操作（reason 進 attrs），
        salience 預設 0.85（比一般對話更高，反映主動標記的重要性）。
        """
        content = args.get("content", "")
        reason = args.get("reason", "")
        salience = float(args.get("salience", 0.85))

        # 夾緊到合法範圍
        salience = max(0.0, min(1.0, salience))

        if not content:
            return {"success": False, "result": "content is required"}

        # 工具操作與內容出處分離：
        # - 操作本身是 source_type="tool"（由對話 LLM 呼叫，speaker 為 agent），
        #   不再偽裝成使用者發言，也不偽造 agent 回覆。
        # - 內容真正出自誰由 LLM 以工具參數 content_origin 明示；未說明即 "unknown"。
        #   此處不做任何 keyword 推斷。
        content_origin = args.get("content_origin")
        if content_origin not in CONTENT_ORIGINS:
            content_origin = None  # 無效或缺省 -> 引擎記為 unknown
        origin_ids = args.get("origin_evidence_ids") or []
        if not isinstance(origin_ids, (list, tuple)):
            origin_ids = []
        # content_summary 使用乾淨的原文，確保向量搜索的語義準確度
        episode_id = self.engine.store_episode(
            user_input="",
            agent_response="",
            session_id=self.session_id,
            salience=salience,
            content_summary=content[:100],
            episode_kind="tool_note",
            evidence=[EvidenceInput(
                source_type="tool",
                content=content,
                speaker_id="agent",
                source_ref=SourceRef(kind="tool_call", name="force_remember"),
                scope=f"session:{self.session_id}",
                content_origin=content_origin,
                origin_evidence_ids=tuple(str(i) for i in origin_ids),
                attrs={"reason": reason, "requested_salience": salience},
            )],
        )

        logger.info(f"[ToolExecutor] force_remember 儲存成功: {episode_id}")
        return {
            "success": True,
            "result": f"Stored with episode_id={episode_id}, salience={salience:.2f}",
            "episode_id": episode_id,
        }

    def handle_deep_recall(self, args: dict) -> dict:
        """
        執行深度記憶搜索（超出自動注入脈絡的範圍）。

        使用高 dopamine（0.9）來提高探索廣度，
        top_k 上限 20 以防止脈絡過長。
        """
        query = args.get("query", "")
        top_k = int(args.get("top_k", 10))

        if not query:
            return {"success": False, "result": "query is required"}

        # 上限保護
        top_k = min(top_k, 20)

        context = self.engine.retrieve(
            query=query,
            dopamine=0.9,   # 高探索度，召回更多相關記憶
            serotonin=0.5,
            top_k=top_k,
            min_similarity=0.2,  # 較低閾值，deep recall 允許模糊匹配
            exclude_session_id=self.session_id,
        )

        if context.is_empty():
            return {"success": True, "result": "No memories found for this query."}

        result_text = context.to_text()
        logger.info(f"[ToolExecutor] deep_recall 找到 {len(context.episodes)} 個情節")
        return {"success": True, "result": result_text}

    def handle_adjust_importance(self, args: dict) -> dict:
        """
        調整當前脈絡中某個情節的顯著性分數。

        安全機制：只允許操作 _context_whitelist 中的 episode_id，
        防止 LLM 任意修改不在當前對話脈絡的記憶。
        """
        episode_id = args.get("episode_id", "")
        new_salience = args.get("new_salience")
        reason = args.get("reason", "")

        if not episode_id:
            return {"success": False, "result": "episode_id is required"}
        if new_salience is None:
            return {"success": False, "result": "new_salience is required"}

        new_salience = float(new_salience)

        # 白名單驗證：只允許操作當前脈絡中的情節
        if episode_id not in self._context_whitelist:
            logger.warning(
                f"[ToolExecutor] adjust_importance 拒絕：{episode_id} 不在白名單中"
            )
            return {
                "success": False,
                "result": (
                    f"Episode {episode_id!r} is not in the current context. "
                    "You can only adjust importance of memories that were retrieved in this turn."
                ),
            }

        # 夾緊到 [0, 1]
        new_salience = max(0.0, min(1.0, new_salience))

        success = self.engine.episodic.update_salience(episode_id, new_salience)
        if success:
            logger.info(
                f"[ToolExecutor] adjust_importance 成功: {episode_id} → {new_salience:.2f}"
            )
            return {
                "success": True,
                "result": (
                    f"Updated salience for episode {episode_id[:12]}… "
                    f"to {new_salience:.2f}. Reason: {reason}"
                ),
            }
        else:
            return {
                "success": False,
                "result": f"Episode {episode_id!r} not found in storage.",
            }

    def handle_trigger_dream(self, args: dict) -> dict:
        """
        觸發 Dream Engine 記憶鞏固。

        冷卻保護：距上次呼叫必須超過 dream_cooldown_turns 輪，
        防止 LLM 頻繁觸發昂貴的鞏固操作。
        """
        reason = args.get("reason", "")

        # 冷卻檢查
        turns_since_last = self._current_turn - self._dream_last_turn
        if turns_since_last < self.dream_cooldown_turns:
            remaining = self.dream_cooldown_turns - turns_since_last
            logger.info(
                f"[ToolExecutor] trigger_dream 被冷卻限制，還需 {remaining} 輪"
            )
            return {
                "success": False,
                "result": (
                    f"Dream Engine is on cooldown. "
                    f"Please wait {remaining} more turn(s) before triggering again."
                ),
            }

        # 記錄本次觸發輪數
        self._dream_last_turn = self._current_turn

        logger.info(f"[ToolExecutor] trigger_dream 觸發，原因: {reason}")
        report = self.engine.dream(triggered_by="llm_tool")

        # 構建摘要報告
        if report.get("error"):
            return {
                "success": False,
                "result": f"Dream Engine failed: {report['error']}",
            }

        duration = report.get("duration_seconds", 0)
        edges_updated = report.get("edges_updated", 0)
        edges_pruned = report.get("edges_pruned", 0)

        distill = report.get("distillation", {})
        concepts_created = distill.get("concepts_created", 0) if distill else 0
        episodes_processed = distill.get("episodes_processed", 0) if distill else 0

        summary = (
            f"Dream consolidation complete in {duration:.1f}s. "
            f"Edges updated: {edges_updated}, pruned: {edges_pruned}. "
            f"Concepts created: {concepts_created}, episodes processed: {episodes_processed}."
        )

        logger.info(f"[ToolExecutor] trigger_dream 完成: {summary}")
        return {"success": True, "result": summary}
