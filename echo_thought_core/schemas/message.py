"""
訊息 Schema — 對話訊息的基本資料結構

從 U.E.P Core 的 schemas.py 遷出並改造：
- 移除 U.E.P 特定的系統狀態耦合
- 增加壓縮相關的元資料欄位
- 支援多後端的統一訊息格式

v2 合併自 llm_module/schemas.py：
- 新增 LLMMode 枚舉
- 新增 LLMInput、LLMOutput 模型
- 新增 LearningSignals、LearningData 模型
- 新增 StatusUpdate 模型
"""

import time
from typing import Literal, Optional, Dict, Any, List
from enum import Enum
from pydantic import BaseModel, Field


# ── 枚舉 ──────────────────────────────────────────────────────────────────────


class LLMMode(str, Enum):
    """LLM 處理模式"""

    CHAT = "chat"         # 對話模式
    WORK = "work"         # 工作模式
    DIRECT = "direct"     # 直接模式（不使用系統提示）
    INTERNAL = "internal" # 內部系統調用


# ── 基礎訊息格式 ──────────────────────────────────────────────────────────────


class Message(BaseModel):
    """統一訊息格式 — 後端無關的訊息表示"""

    role: Literal["system", "user", "assistant", "tool"] = Field(
        ..., description="訊息角色"
    )
    content: str = Field(..., description="訊息內容")
    timestamp: float = Field(default_factory=time.time, description="訊息時間戳")
    token_count: Optional[int] = Field(None, description="此訊息的 token 數量（由 counter 填入）")

    # 壓縮相關
    is_compressed: bool = Field(False, description="此訊息是否為壓縮後的摘要")
    original_turn_range: Optional[str] = Field(
        None, description="若為摘要，記錄原始 turn 範圍（如 '1-15'）"
    )

    # 工具呼叫相關
    tool_call_id: Optional[str] = Field(None, description="工具呼叫 ID")
    tool_name: Optional[str] = Field(None, description="工具名稱")
    is_reproducible: bool = Field(
        True, description="工具輸出是否可重現（影響 Layer 1 清理決策）"
    )

    # 元資料
    metadata: Dict[str, Any] = Field(default_factory=dict, description="額外元資料")


class ConversationEntry(BaseModel):
    """對話條目 — 向後相容的簡化格式"""

    role: Literal["user", "assistant", "system"] = Field(..., description="角色")
    content: str = Field(..., description="內容")
    timestamp: Optional[float] = Field(None, description="時間戳")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="元資料")


class ConversationHistory(BaseModel):
    """對話歷史容器"""

    messages: List[Message] = Field(default_factory=list, description="訊息列表")
    total_tokens: int = Field(0, description="總 token 數")
    compression_count: int = Field(0, description="已執行的壓縮次數")

    def add(self, message: Message) -> None:
        """新增訊息"""
        self.messages.append(message)
        if message.token_count:
            self.total_tokens += message.token_count

    def get_recent(self, n: int) -> List[Message]:
        """取得最近 n 則訊息"""
        return self.messages[-n:] if n < len(self.messages) else list(self.messages)

    @property
    def turn_count(self) -> int:
        """使用者訊息數量"""
        return sum(1 for m in self.messages if m.role == "user")


# ── 學習相關 ──────────────────────────────────────────────────────────────────


class LearningSignals(BaseModel):
    """學習信號 — 累積評分制，用於調整回應風格"""

    formality_signal: Optional[float] = Field(
        None, ge=-1.0, le=1.0,
        description="正式程度信號（-1.0=非正式, 0=中性, 1.0=正式）"
    )
    detail_signal: Optional[float] = Field(
        None, ge=-1.0, le=1.0,
        description="詳細程度信號（-1.0=簡潔, 0=適中, 1.0=詳細）"
    )
    technical_signal: Optional[float] = Field(
        None, ge=-1.0, le=1.0,
        description="技術程度信號（-1.0=通俗, 0=適中, 1.0=專業）"
    )
    interaction_signal: Optional[float] = Field(
        None, ge=-1.0, le=1.0,
        description="互動偏好信號（-1.0=獨立, 0=適中, 1.0=互動）"
    )


class LearningData(BaseModel):
    """學習數據 — 保留向後兼容性"""

    interaction_type: str = Field(..., description="互動類型")
    user_preference: Dict[str, Any] = Field(default_factory=dict, description="使用者偏好")
    conversation_style: Dict[str, Any] = Field(default_factory=dict, description="對話風格")
    system_usage_patterns: Dict[str, Any] = Field(default_factory=dict, description="系統使用模式")


# ── LLM 輸入輸出 ──────────────────────────────────────────────────────────────


class LLMInput(BaseModel):
    """LLM 模組輸入

    從 U.E.P Core llm_module 遷入，U.E.P 特定欄位（source_layer、
    processing_context、collaboration_context）保留但設為可選，
    以維持向後相容性。
    """

    # 基本輸入
    text: str = Field(..., description="要處理的文字內容")
    mode: LLMMode = Field(LLMMode.CHAT, description="處理模式")

    # 系統狀態上下文
    system_state: Optional[str] = Field(None, description="當前系統狀態（字串形式，避免耦合枚舉）")
    session_id: Optional[str] = Field(None, description="會話 ID")
    cycle_index: Optional[int] = Field(None, description="當前循環索引（由 MC 從事件傳遞）")

    # 身份和記憶上下文
    identity_context: Optional[Dict[str, Any]] = Field(None, description="身份上下文")
    memory_context: Optional[str] = Field(None, description="記憶上下文")
    system_context: Optional[Dict[str, Any]] = Field(None, description="系統上下文")

    # 系統功能上下文（WORK 模式用）
    available_functions: Optional[str] = Field(None, description="可用系統功能")
    workflow_context: Optional[Dict[str, Any]] = Field(None, description="工作流上下文")
    workflow_session_id: Optional[str] = Field(None, description="工作流會話 ID（指定處理哪個工作流）")

    # 對話歷史與快取
    ignore_cache: Optional[bool] = Field(False, description="是否忽略回應快取")
    conversation_history: Optional[List[ConversationEntry]] = Field(
        default_factory=list, description="對話歷史"
    )
    # cache_config 型別為 Any，避免直接 import session.py 中的 ContextCacheConfig 造成循環依賴
    cache_config: Optional[Any] = Field(None, description="快取配置（ContextCacheConfig 實例）")

    # 控制選項
    is_internal: bool = Field(False, description="是否為內部系統調用")
    enable_learning: bool = Field(True, description="是否啟用學習功能")
    system_report: bool = Field(False, description="是否為系統報告模式（系統主動通知）")

    # U.E.P Router 整合欄位（可選，保留向後兼容）
    source_layer: Optional[str] = Field(None, description="來源層級（input/processing/output）")
    processing_context: Optional[Dict[str, Any]] = Field(None, description="處理層上下文")
    collaboration_context: Optional[Dict[str, Any]] = Field(None, description="協作模組上下文")
    entities: Optional[Dict[str, Any]] = Field(None, description="NLP 解析的實體")
    confidence: Optional[float] = Field(None, description="輸入層信心度")
    session_context: Optional[Dict[str, Any]] = Field(None, description="會話上下文")
    enable_memory_retrieval: Optional[bool] = Field(None, description="是否啟用記憶檢索")
    enable_system_actions: Optional[bool] = Field(None, description="是否啟用系統動作")

    # 向後兼容（舊版本支援）
    intent: Optional[str] = Field(None, description="舊版意圖（向後兼容）")
    memory: Optional[str] = Field(None, description="舊版記憶（向後兼容）")


class LLMOutput(BaseModel):
    """LLM 模組輸出 — 僅保留實際使用的欄位"""

    # 基本輸出
    text: str = Field(..., description="LLM 生成的回應文字")

    # 執行狀態
    success: bool = Field(True, description="處理是否成功")
    error: Optional[str] = Field(None, description="錯誤訊息")

    # 效能指標
    processing_time: Optional[float] = Field(None, description="處理時間（秒）")
    tokens_used: Optional[int] = Field(None, description="使用的 token 數量")

    # 信心度（用於某些判斷場景）
    confidence: Optional[float] = Field(1.0, description="回應信心度（0.0-1.0）")

    # 擴展資訊（如 function_call_made、workflow_decision 等）
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="額外元資料")


# ── 狀態更新 ──────────────────────────────────────────────────────────────────


class StatusUpdate(BaseModel):
    """系統狀態數值更新 — 對情緒、自尊等指標施加增量"""

    mood_delta: Optional[float] = Field(None, description="情緒變化（建議範圍: ±0.05 到 ±0.2）")
    pride_delta: Optional[float] = Field(None, description="自尊變化（建議範圍: ±0.05 到 ±0.2）")
    helpfulness_delta: Optional[float] = Field(None, description="助人意願變化（建議範圍: ±0.05 到 ±0.2）")
    boredom_delta: Optional[float] = Field(None, description="無聊程度變化（建議範圍: ±0.05 到 ±0.2）")
    reason: Optional[str] = Field(None, description="變化原因")
