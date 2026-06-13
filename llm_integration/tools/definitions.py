"""
工具定義模組 — Gemini Function Calling Schema

定義 Echo Memory 系統的四個記憶工具：
- force_remember:    強制儲存重要資訊到長期記憶
- deep_recall:       執行更深度的記憶搜索
- adjust_importance: 調整當前脈絡中某記憶的顯著性
- trigger_dream:     觸發記憶鞏固（Dream Engine）
"""

from __future__ import annotations

# ──────────────────────────────────────────────────────────────────────────────
# 原始 Schema Dict（供偵錯/序列化使用）
# ──────────────────────────────────────────────────────────────────────────────

TOOL_DECLARATIONS: list[dict] = [
    {
        "name": "force_remember",
        "description": (
            "Explicitly store an important piece of information into long-term memory. "
            "Use when something is significant enough to remember beyond this conversation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The information to store in long-term memory.",
                },
                "reason": {
                    "type": "string",
                    "description": "Why this information is important enough to remember.",
                },
                "salience": {
                    "type": "number",
                    "description": (
                        "Importance score between 0.0 and 1.0 (default 0.85). "
                        "Higher means more important."
                    ),
                },
            },
            "required": ["content", "reason"],
        },
    },
    {
        "name": "deep_recall",
        "description": (
            "Perform a deeper memory search beyond the auto-injected context. "
            "Use when standard context is insufficient or you need more specific memories."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query to find relevant memories.",
                },
                "top_k": {
                    "type": "integer",
                    "description": (
                        "Number of memories to retrieve (default 10, maximum 20)."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "adjust_importance",
        "description": (
            "Adjust the salience of a memory from the current context. "
            "Only usable for episode_ids visible in the injected memories."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "episode_id": {
                    "type": "string",
                    "description": "The ID of the episode to adjust (must be in current context).",
                },
                "new_salience": {
                    "type": "number",
                    "description": "New salience score between 0.0 and 1.0.",
                },
                "reason": {
                    "type": "string",
                    "description": "Why you are adjusting the importance of this memory.",
                },
            },
            "required": ["episode_id", "new_salience", "reason"],
        },
    },
    {
        "name": "trigger_dream",
        "description": (
            "Trigger memory consolidation (Dream Engine). "
            "Use sparingly — this is a heavy operation and has a cooldown between uses."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why memory consolidation is needed right now.",
                },
            },
            "required": ["reason"],
        },
    },
]


# ──────────────────────────────────────────────────────────────────────────────
# 轉換為 Gemini SDK 格式
# ──────────────────────────────────────────────────────────────────────────────

def to_gemini_tools():
    """
    將 TOOL_DECLARATIONS 轉換為 google.genai.types.Tool 物件。

    Returns:
        list[google.genai.types.Tool]：可直接傳入 Gemini API 的工具列表
    """
    from google.genai import types

    declarations = []
    for decl in TOOL_DECLARATIONS:
        # 將 parameters dict 轉為 Schema 物件
        params_dict = decl["parameters"]
        properties = {}
        for prop_name, prop_info in params_dict.get("properties", {}).items():
            properties[prop_name] = types.Schema(
                type=prop_info["type"].upper(),
                description=prop_info.get("description", ""),
            )

        schema = types.Schema(
            type="OBJECT",
            properties=properties,
            required=params_dict.get("required", []),
        )

        func_decl = types.FunctionDeclaration(
            name=decl["name"],
            description=decl["description"],
            parameters=schema,
        )
        declarations.append(func_decl)

    return [types.Tool(function_declarations=declarations)]
