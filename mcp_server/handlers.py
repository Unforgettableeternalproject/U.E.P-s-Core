"""
Echo Memory MCP Tool Handlers

實作所有 MCP 工具（tools）的處理邏輯。
每個工具對應一個 async handler，使用 asyncio.to_thread() 包裝 sync 的 MemoryEngine API。

工具列表：
    1. store_episode      — 存儲對話情節
    2. retrieve_memories  — 檢索記憶
    3. search_concepts    — 搜尋語義概念
    4. get_memory_stats   — 取得記憶統計
    5. trigger_dream      — 觸發 Dream Engine
    6. list_episodes      — 列出情節
    7. store_observation  — 存儲重要觀察（高顯著性）
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from mcp.server import Server
from mcp.types import TextContent, Tool

from echo_memory.schemas.evidence import EvidenceInput, SourceRef
from mcp_server.access_policy import check_namespace_access, policy_from_config

logger = logging.getLogger("echo_memory.mcp.handlers")

# Dream Engine 冷卻時間（秒）
_DREAM_COOLDOWN_SECONDS = 60
_last_dream_time: float = 0.0

# Dream Engine 執行超時（秒）
_DREAM_TIMEOUT_SECONDS = 30.0


def _namespace_schema() -> dict[str, Any]:
    return {
        "type": "string",
        "description": "隔離記憶資料的 namespace（可選，預設伺服器設定）",
        "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
    }


def _get_engine(get_engine: Callable, args: dict[str, Any], *, write: bool = False):
    """取得 engine；先過 namespace 存取策略（拒絕他人的使用者 namespace、場域共享 namespace 寫入）。"""
    namespace_id = check_namespace_access(
        args.get("namespace_id"), policy_from_config(), write=write,
    )
    return get_engine(namespace_id)


def register_tools(server: Server, get_engine: Callable) -> None:
    """
    向 MCP Server 註冊所有工具。

    Args:
        server: MCP Server 實例
        get_engine: 取得 MemoryEngine 實例的工廠函式（Callable[[], MemoryEngine]）
    """

    @server.list_tools()
    async def handle_list_tools() -> list[Tool]:
        """回傳所有可用工具的定義清單。"""
        return [
            Tool(
                name="store_episode",
                description=(
                    "存儲一段對話情節到 Echo Memory。"
                    "記錄用戶輸入與 agent 回應，可設定顯著性分數。"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "namespace_id": _namespace_schema(),
                        "user_input": {
                            "type": "string",
                            "description": "用戶的輸入文字",
                        },
                        "agent_response": {
                            "type": "string",
                            "description": "Agent 的回應文字",
                        },
                        "session_id": {
                            "type": "string",
                            "description": "會話 ID（可選，預設為 'mcp-default'）",
                        },
                        "salience": {
                            "type": "number",
                            "description": "顯著性分數 0.0-1.0（可選，預設 0.5）",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                    },
                    "required": ["user_input", "agent_response"],
                },
            ),
            Tool(
                name="retrieve_memories",
                description=(
                    "根據查詢文字從 Echo Memory 檢索相關記憶。"
                    "返回格式化的記憶脈絡文字，可直接作為 LLM 的 context 使用。"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "namespace_id": _namespace_schema(),
                        "query": {
                            "type": "string",
                            "description": "查詢文字",
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "返回記憶數量（可選，預設 5）",
                            "minimum": 1,
                            "maximum": 50,
                        },
                        "min_similarity": {
                            "type": "number",
                            "description": "最小相似度閾值 0.0-1.0（可選，預設 0.3）",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                        "dopamine": {
                            "type": "number",
                            "description": "多巴胺濃度 0.0-1.0，影響探索廣度（可選，預設 0.5）",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                        "serotonin": {
                            "type": "number",
                            "description": "血清素濃度 0.0-1.0，影響謹慎程度（可選，預設 0.5）",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                        "exclude_session_id": {
                            "type": "string",
                            "description": "排除指定 session 的記憶（避免與當前對話重複，可選）",
                        },
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="search_concepts",
                description=(
                    "在 Echo Memory 的語義記憶中搜尋相關概念。"
                    "使用向量相似度搜尋，返回概念名稱與信心分數列表。"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "namespace_id": _namespace_schema(),
                        "query": {
                            "type": "string",
                            "description": "搜尋關鍵字或描述",
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "返回概念數量（可選，預設 5）",
                            "minimum": 1,
                            "maximum": 50,
                        },
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="get_memory_stats",
                description=(
                    "取得 Echo Memory 系統的統計資訊，"
                    "包含情節數、概念數和圖結構資訊。"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"namespace_id": _namespace_schema()},
                    "required": [],
                },
            ),
            Tool(
                name="get_profile",
                description=(
                    "取得使用者 profile：Dream 蒸餾出的、關於使用者本人的長期記憶"
                    "（事實、偏好、對 agent 的要求）。不靠相似度、全部列出，"
                    "適合「你記得我什麼」這類開放式回顧，或在對話開頭建立脈絡。"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "namespace_id": _namespace_schema(),
                        "max_chars": {
                            "type": "integer",
                            "description": "輸出字元上限（預設 800）",
                        },
                    },
                    "required": [],
                },
            ),
            Tool(
                name="trigger_dream",
                description=(
                    "觸發 Dream Engine 執行離線記憶鞏固。"
                    "Dream Engine 會進行記憶圖的邊權重更新和修剪。"
                    "此操作可能需要數秒，並有 60 秒冷卻時間。"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "namespace_id": _namespace_schema(),
                        "triggered_by": {
                            "type": "string",
                            "description": "觸發來源標記（可選，預設 'mcp'）",
                        },
                    },
                    "required": [],
                },
            ),
            Tool(
                name="list_episodes",
                description=(
                    "列出 Echo Memory 中的情節記憶。"
                    "可依 session ID 篩選，支援排序和數量限制。"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "namespace_id": _namespace_schema(),
                        "session_id": {
                            "type": "string",
                            "description": "過濾特定 session 的情節（可選）",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "最多返回數量（可選，預設 10，最大 50）",
                            "minimum": 1,
                            "maximum": 50,
                        },
                        "sort_by": {
                            "type": "string",
                            "description": "排序欄位：'timestamp'（預設）或 'salience'",
                            "enum": ["timestamp", "salience"],
                        },
                    },
                    "required": [],
                },
            ),
            Tool(
                name="store_observation",
                description=(
                    "存儲一個重要觀察或事實到 Echo Memory，使用高顯著性分數。"
                    "適合存儲需要長期記憶的重要資訊。"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "namespace_id": _namespace_schema(),
                        "content": {
                            "type": "string",
                            "description": "觀察或事實的內容",
                        },
                        "importance": {
                            "type": "number",
                            "description": "重要程度 0.0-1.0（可選，預設 0.85）",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                    },
                    "required": ["content"],
                },
            ),
        ]

    @server.call_tool()
    async def handle_call_tool(
        name: str,
        arguments: dict[str, Any] | None,
    ) -> list[TextContent]:
        """
        處理工具呼叫請求的統一入口。

        根據 name 路由到對應的處理函式。
        MemoryEngine 的 sync API 透過 asyncio.to_thread() 執行。
        """
        args = arguments or {}

        try:
            if name == "store_episode":
                result = await _handle_store_episode(get_engine, args)
            elif name == "retrieve_memories":
                result = await _handle_retrieve_memories(get_engine, args)
            elif name == "search_concepts":
                result = await _handle_search_concepts(get_engine, args)
            elif name == "get_memory_stats":
                result = await _handle_get_memory_stats(get_engine, args)
            elif name == "get_profile":
                result = await _handle_get_profile(get_engine, args)
            elif name == "trigger_dream":
                result = await _handle_trigger_dream(get_engine, args)
            elif name == "list_episodes":
                result = await _handle_list_episodes(get_engine, args)
            elif name == "store_observation":
                result = await _handle_store_observation(get_engine, args)
            else:
                result = f"未知的工具名稱: {name}"

        except Exception as exc:
            logger.exception(f"[MCP Handler] 工具 '{name}' 執行失敗: {exc}")
            result = f"工具執行失敗: {type(exc).__name__}: {exc}"

        return [TextContent(type="text", text=str(result))]


# ─── 各工具的具體實作 ──────────────────────────────────────────────────────────


async def _handle_store_episode(
    get_engine: Callable,
    args: dict[str, Any],
) -> str:
    """
    存儲對話情節。

    Returns:
        episode_id 字串
    """
    user_input: str = args["user_input"]
    agent_response: str = args["agent_response"]
    session_id: str = args.get("session_id", "mcp-default")
    salience: float = float(args.get("salience", 0.5))

    def _sync_store() -> str:
        engine = _get_engine(get_engine, args, write=True)
        return engine.store_episode(
            user_input=user_input,
            agent_response=agent_response,
            session_id=session_id,
            salience=salience,
        )

    episode_id = await asyncio.to_thread(_sync_store)
    logger.info(f"[MCP Handler] store_episode 完成: {episode_id}")
    return f"已存儲情節，episode_id: {episode_id}"


async def _handle_retrieve_memories(
    get_engine: Callable,
    args: dict[str, Any],
) -> str:
    """
    檢索相關記憶，返回格式化的記憶脈絡文字。
    """
    query: str = args["query"]
    top_k: int = int(args.get("top_k", 5))
    # 預設閾值不硬編碼，由 engine.retrieve() 讀取 config（0.35）
    min_similarity: float = float(args.get("min_similarity", 0.35))
    dopamine: float = float(args.get("dopamine", 0.5))
    serotonin: float = float(args.get("serotonin", 0.5))

    exclude_session_id: str | None = args.get("exclude_session_id")

    def _sync_retrieve() -> str:
        engine = _get_engine(get_engine, args)
        context = engine.retrieve(
            query=query,
            dopamine=dopamine,
            serotonin=serotonin,
            top_k=top_k,
            min_similarity=min_similarity,
            exclude_session_id=exclude_session_id,
        )
        return context.to_text()

    memory_text = await asyncio.to_thread(_sync_retrieve)
    logger.info(f"[MCP Handler] retrieve_memories 完成，查詢: {query[:30]}...")
    return memory_text


async def _handle_search_concepts(
    get_engine: Callable,
    args: dict[str, Any],
) -> str:
    """搜尋語義概念（向量搜索）。"""
    query: str = args["query"]
    top_k: int = int(args.get("top_k", 5))

    def _sync_search() -> list[tuple[str, str]]:
        engine = _get_engine(get_engine, args)

        if engine.semantic.count() == 0:
            return []

        query_embedding = engine.episodic.generate_query_embedding(query)
        results = engine.semantic.search_by_vector(
            query_embedding=query_embedding,
            top_k=top_k,
            min_similarity=0.35,
        )
        # 已被取代（deprecated + replaced_by）與已合併的概念不回給外部
        return [
            (c.name, f"{sim:.1%}") for c, sim in results
            if not (c.status == "deprecated" and c.metadata.get("replaced_by"))
            and not c.metadata.get("merged_into")
        ]

    concept_list = await asyncio.to_thread(_sync_search)
    logger.info(f"[MCP Handler] search_concepts 完成，找到 {len(concept_list)} 個概念")

    if not concept_list:
        return f"未找到與 '{query}' 相關的概念。"

    lines = [f"找到 {len(concept_list)} 個相關概念："]
    for i, (name, confidence) in enumerate(concept_list, 1):
        lines.append(f"  {i}. {name}（信心度: {confidence}）")
    return "\n".join(lines)


async def _handle_get_profile(get_engine: Callable, args: dict[str, Any]) -> str:
    """使用者 profile（subject=user 的 concept 全列）。"""
    max_chars = int(args.get("max_chars") or 800)

    def _sync() -> str:
        return _get_engine(get_engine, args).get_profile(max_chars=max_chars)

    text = await asyncio.to_thread(_sync)
    logger.info("[MCP Handler] get_profile 完成（%d 字元）", len(text))
    return text or "（尚無關於使用者的長期記憶——Dream 蒸餾後才會出現）"


async def _handle_get_memory_stats(get_engine: Callable, args: dict[str, Any]) -> str:
    """
    取得記憶系統統計資訊。
    """
    def _sync_stats() -> dict[str, Any]:
        engine = _get_engine(get_engine, args)
        stats = engine.get_stats()

        # 加入圖結構統計
        graph_stats = engine.episodic.graph.get_stats()
        stats["graph_nodes"] = graph_stats["nodes"]
        stats["graph_edges"] = graph_stats["edges"]
        stats["graph_avg_edge_weight"] = round(graph_stats.get("avg_edge_weight", 0.0), 4)

        return stats

    stats = await asyncio.to_thread(_sync_stats)
    logger.info("[MCP Handler] get_memory_stats 完成")

    lines = [
        "Echo Memory 系統統計：",
        f"  情節記憶 (Episodes):  {stats.get('total_episodes', 0):,} 筆",
        f"  語義概念 (Concepts):  {stats.get('total_concepts', 0):,} 個",
        f"  工作記憶快取:          {stats.get('working_cache_size', 0):,} 筆",
        f"  記憶圖節點:            {stats.get('graph_nodes', 0):,}",
        f"  記憶圖邊:              {stats.get('graph_edges', 0):,}",
        f"  平均邊權重:            {stats.get('graph_avg_edge_weight', 0.0):.4f}",
    ]
    return "\n".join(lines)


async def _handle_trigger_dream(
    get_engine: Callable,
    args: dict[str, Any],
) -> str:
    """
    觸發 Dream Engine。

    具有冷卻時間保護（60 秒），避免頻繁觸發。
    使用 asyncio.wait_for 加 timeout 避免無限等待。
    """
    global _last_dream_time

    triggered_by: str = args.get("triggered_by", "mcp")

    # 冷卻時間檢查
    elapsed = time.time() - _last_dream_time
    if elapsed < _DREAM_COOLDOWN_SECONDS:
        remaining = int(_DREAM_COOLDOWN_SECONDS - elapsed)
        return f"Dream Engine 冷卻中，請等待 {remaining} 秒後再試。"

    def _sync_dream() -> dict[str, Any]:
        engine = _get_engine(get_engine, args, write=True)
        return engine.dream(triggered_by=triggered_by)

    try:
        _last_dream_time = time.time()
        report = await asyncio.wait_for(
            asyncio.to_thread(_sync_dream),
            timeout=_DREAM_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return f"Dream Engine 執行超時（>{_DREAM_TIMEOUT_SECONDS:.0f}s），請稍後再試。"

    logger.info(f"[MCP Handler] trigger_dream 完成: {report}")

    if report.get("skipped_reason"):
        return f"Dream Engine 本次未執行：{report['skipped_reason']}"

    # 格式化報告
    duration = report.get("duration_seconds", 0.0)
    lines = [
        f"Dream Engine 執行完成（耗時 {duration:.1f} 秒）：",
        f"  觸發來源:   {report.get('triggered_by', triggered_by)}",
        f"  更新邊數:   {report.get('edges_updated', 0)}",
        f"  修剪邊數:   {report.get('edges_pruned', 0)}",
    ]

    # 相容新版本的詳細報告（若有）
    if "replay" in report and report["replay"]:
        r = report["replay"]
        skipped = r.get("skipped_reason")
        if skipped:
            lines.append(f"  重播階段:   已跳過（{skipped}）")
        else:
            lines.append(
                f"  重播階段:   已重播 {r.get('episodes_replayed', 0)} 個情節，"
                f"強化 {r.get('edges_strengthened', 0)} 邊"
            )

    if "distillation" in report and report["distillation"]:
        d = report["distillation"]
        skipped = d.get("skipped_reason")
        if skipped:
            lines.append(f"  蒸餾階段:   已跳過（{skipped}）")
        else:
            lines.append(
                f"  蒸餾階段:   掃描 {d.get('episodes_scanned', 0)} 個情節，"
                f"新建 {d.get('concepts_created', 0)} 個概念"
            )

    if report.get("error"):
        lines.append(f"  錯誤:       {report['error']}")

    return "\n".join(lines)


async def _handle_list_episodes(
    get_engine: Callable,
    args: dict[str, Any],
) -> str:
    """列出情節記憶，返回格式化的摘要列表。"""
    session_id: str | None = args.get("session_id")
    limit: int = min(int(args.get("limit", 10)), 50)
    sort_by: str = args.get("sort_by", "timestamp")

    def _sync_list() -> list[dict[str, Any]]:
        engine = _get_engine(get_engine, args)
        episodes_objs = engine.episodic.list_episodes(
            session_id=session_id,
            limit=limit,
            sort_by=sort_by,
            ascending=False,
        )

        result = []
        for ep in episodes_objs:
            result.append({
                "id": ep.id,
                "session_id": ep.session_id,
                "timestamp": ep.timestamp.isoformat(),
                "salience": round(ep.salience_score, 3),
                "summary": ep.content_summary[:100],
                "is_dreamed": ep.is_dreamed,
            })
        return result

    episodes = await asyncio.to_thread(_sync_list)
    logger.info(f"[MCP Handler] list_episodes 完成，返回 {len(episodes)} 筆")

    if not episodes:
        filter_info = f"（session: {session_id}）" if session_id else ""
        return f"沒有找到情節記憶{filter_info}。"

    filter_info = f"（session: {session_id}）" if session_id else ""
    lines = [f"最近 {len(episodes)} 筆情節記憶{filter_info}（排序: {sort_by}）："]
    for i, ep in enumerate(episodes, 1):
        dreamed_tag = " [已鞏固]" if ep["is_dreamed"] else ""
        lines.append(
            f"  {i}. [{ep['timestamp'][:19]}] "
            f"顯著性:{ep['salience']:.2f}{dreamed_tag} "
            f"- {ep['summary']}"
        )
    return "\n".join(lines)


async def _handle_store_observation(
    get_engine: Callable,
    args: dict[str, Any],
) -> str:
    """
    存儲重要觀察，使用高顯著性分數。

    將觀察存儲為 tool_note 情節：來源是 MCP 工具操作（source_type=tool），
    內容出處未說明（content_origin=unknown），不再以 "[觀察記錄]" 佔位 user_input。
    """
    content: str = args["content"]
    importance: float = float(args.get("importance", 0.85))

    def _sync_store() -> str:
        engine = _get_engine(get_engine, args, write=True)
        return engine.store_episode(
            user_input="",
            agent_response="",
            session_id="mcp-observations",
            content_summary=content[:200],
            salience=importance,
            episode_kind="tool_note",
            evidence=[EvidenceInput(
                source_type="tool",
                content=content,
                source_ref=SourceRef(kind="mcp", name="store_observation"),
                content_origin="unknown",
            )],
            source="mcp_observation",
        )

    episode_id = await asyncio.to_thread(_sync_store)
    logger.info(f"[MCP Handler] store_observation 完成: {episode_id}")
    return (
        f"已存儲觀察記錄，episode_id: {episode_id}，"
        f"重要程度: {importance:.0%}"
    )
