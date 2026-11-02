"""
Prompt Constants — 佔位符與標記常數

定義 PromptBuilder 在模板中使用的佔位符字串。
未來整合 U.E.P Core 時，這些佔位符對應
PromptManager._replace_system_values_placeholder() 的替換目標。
"""

# ── 模板佔位符 ────────────────────────────────────────────────────────────────
# 格式：{snake_case}，與 U.E.P Core config.yaml 中的佔位符對齊

PLACEHOLDER_PERSONA = "{persona}"
PLACEHOLDER_SYSTEM_VALUES = "{system_values}"
PLACEHOLDER_MEMORY = "{memory_context}"
PLACEHOLDER_WORKFLOW = "{workflow_context}"
PLACEHOLDER_SACRED = "{sacred_content}"
PLACEHOLDER_INSTRUCTIONS = "{custom_instructions}"

# ── 模式名稱常數 ──────────────────────────────────────────────────────────────

MODE_CHAT = "chat"
MODE_WORK = "work"
MODE_DIRECT = "direct"

ALL_MODES = [MODE_CHAT, MODE_WORK, MODE_DIRECT]

# ── 注入段落標記 ──────────────────────────────────────────────────────────────
# 用於在 system prompt 中標記各段的開始/結束，方便除錯和解析

SECTION_MARKER_PERSONA = "## Identity & Persona"
SECTION_MARKER_STATUS = "## System Status"
SECTION_MARKER_MEMORY = "## Relevant Memories"
SECTION_MARKER_WORKFLOW = "## Workflow Context"
SECTION_MARKER_INSTRUCTIONS = "## Additional Instructions"
