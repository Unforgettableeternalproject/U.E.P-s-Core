# modules/llm_module/schemas.py
"""
LLM 模組 Schema 定義 - 重構版本

支援 CHAT 和 WORK 狀態的分離處理，整合 StatusManager 和 Context Caching
"""

from pydantic import BaseModel, Field
from typing import Literal, Optional, Dict, Any, List
from enum import Enum


class SystemState(str, Enum):
    """系統狀態枚舉"""
    IDLE = "idle"
    CHAT = "chat"
    WORK = "work"
    SLEEP = "sleep"
    MISCHIEF = "mischief"


class LLMMode(str, Enum):
    """LLM 處理模式"""
    CHAT = "chat"           # 對話模式
    WORK = "work"           # 工作模式  
    DIRECT = "direct"       # 直接模式（不使用系統提示）
    INTERNAL = "internal"   # 內部系統調用


class ContextCacheConfig(BaseModel):
    """上下文快取配置"""
    enabled: bool = True
    max_history_length: int = 10
    cache_system_prompt: bool = True
    cache_identity_info: bool = True
    cache_memory_context: bool = True


class SystemAction(BaseModel):
    """系統動作定義"""
    action: str = Field(..., description="動作類型")
    workflow_type: Optional[str] = Field(None, description="工作流程類型")
    function_name: Optional[str] = Field(None, description="具體功能名稱")
    params: Optional[Dict[str, Any]] = Field(default_factory=dict, description="參數")
    reason: Optional[str] = Field(None, description="選擇此動作的原因")
    confidence: Optional[float] = Field(1.0, description="信心度")


class ConversationEntry(BaseModel):
    """對話條目"""
    role: Literal["user", "assistant", "system"] = Field(..., description="角色")
    content: str = Field(..., description="內容")
    timestamp: Optional[float] = Field(None, description="時間戳")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="元數據")


class LearningSignals(BaseModel):
    """學習信號 - 累積評分制"""
    formality_signal: Optional[float] = Field(None, ge=-1.0, le=1.0, description="正式程度信號 (-1.0=非正式, 0=中性, 1.0=正式)")
    detail_signal: Optional[float] = Field(None, ge=-1.0, le=1.0, description="詳細程度信號 (-1.0=簡潔, 0=適中, 1.0=詳細)")
    technical_signal: Optional[float] = Field(None, ge=-1.0, le=1.0, description="技術程度信號 (-1.0=通俗, 0=適中, 1.0=專業)")
    interaction_signal: Optional[float] = Field(None, ge=-1.0, le=1.0, description="互動偏好信號 (-1.0=獨立, 0=適中, 1.0=互動)")


class LearningData(BaseModel):
    """學習數據 - 保留向後兼容性"""
    interaction_type: str = Field(..., description="互動類型")
    user_preference: Dict[str, Any] = Field(default_factory=dict, description="使用者偏好")
    conversation_style: Dict[str, Any] = Field(default_factory=dict, description="對話風格")
    system_usage_patterns: Dict[str, Any] = Field(default_factory=dict, description="系統使用模式")


class LLMInput(BaseModel):
    """LLM 模組輸入 - 重構版本"""
    # 基本輸入
    text: str = Field(..., description="要處理的文字內容")
    mode: LLMMode = Field(LLMMode.CHAT, description="處理模式")
    
    # 系統狀態上下文
    system_state: Optional[SystemState] = Field(None, description="當前系統狀態")
    session_id: Optional[str] = Field(None, description="會話ID")
    cycle_index: Optional[int] = Field(None, description="當前循環索引（由 MC 從事件傳遞）")
    
    # 身份和記憶上下文
    identity_context: Optional[Dict[str, Any]] = Field(None, description="身份上下文")
    memory_context: Optional[str] = Field(None, description="記憶上下文")
    system_context: Optional[Dict[str, Any]] = Field(None, description="系統上下文")
    
    # 系統功能上下文（WORK 模式用）
    available_functions: Optional[str] = Field(None, description="可用系統功能")
    workflow_context: Optional[Dict[str, Any]] = Field(None, description="工作流上下文")
    workflow_session_id: Optional[str] = Field(None, description="工作流會話ID（用於指定處理哪個工作流）")
    
    # 對話歷史和快取
    ignore_cache: Optional[bool] = Field(False, description="是否忽略回應快取")
    conversation_history: Optional[List[ConversationEntry]] = Field(default_factory=list, description="對話歷史")
    cache_config: Optional[ContextCacheConfig] = Field(default_factory=ContextCacheConfig, description="快取配置")
    
    # 控制選項
    is_internal: bool = Field(False, description="是否為內部系統調用")
    enable_learning: bool = Field(True, description="是否啟用學習功能")
    system_report: bool = Field(False, description="是否為系統報告模式（系統主動通知）")
    
    # 新 Router 整合支援
    source_layer: Optional[str] = Field(None, description="來源層級（input/processing/output）")
    processing_context: Optional[Dict[str, Any]] = Field(None, description="處理層上下文")
    collaboration_context: Optional[Dict[str, Any]] = Field(None, description="協作模組上下文")
    entities: Optional[Dict[str, Any]] = Field(None, description="NLP 解析的實體")
    confidence: Optional[float] = Field(None, description="輸入層信心度")
    session_context: Optional[Dict[str, Any]] = Field(None, description="會話上下文")
    enable_memory_retrieval: Optional[bool] = Field(None, description="是否啟用記憶檢索")
    enable_system_actions: Optional[bool] = Field(None, description="是否啟用系統動作")
    
    # 向後兼容 (舊版本支援)
    intent: Optional[str] = Field(None, description="舊版意圖（向後兼容）")
    memory: Optional[str] = Field(None, description="舊版記憶（向後兼容）")


class StatusUpdate(BaseModel):
    """系統狀態更新"""
    mood_delta: Optional[float] = Field(None, description="情緒變化")
    pride_delta: Optional[float] = Field(None, description="自尊變化") 
    helpfulness_delta: Optional[float] = Field(None, description="助人意願變化")
    boredom_delta: Optional[float] = Field(None, description="無聊程度變化")
    reason: Optional[str] = Field(None, description="變化原因")


class LLMOutput(BaseModel):
    """LLM 模組輸出 - 重構版本"""
    # 基本輸出
    text: str = Field(..., description="LLM 生成的回應文字")
    
    # 系統動作（WORK 模式）
    sys_action: Optional[SystemAction] = Field(None, description="系統動作建議")
    
    # 狀態更新
    status_updates: Optional[StatusUpdate] = Field(None, description="系統狀態更新")
    
    # 學習數據
    learning_data: Optional[LearningData] = Field(None, description="學習到的資訊")
    
    # 會話管理
    conversation_entry: Optional[ConversationEntry] = Field(None, description="對話條目")
    session_state: Optional[str] = Field(None, description="會話狀態")
    
    # 記憶處理（CHAT 模式與 MEM 協作）
    memory_observation: Optional[str] = Field(None, description="對話觀察摘要")
    memory_summary: Optional[str] = Field(None, description="記憶摘要")
    
    # 其他輸出
    emotion: Optional[str] = Field("neutral", description="情緒標記")
    confidence: Optional[float] = Field(1.0, description="回應信心度")
    processing_time: Optional[float] = Field(None, description="處理時間")
    
    # 執行狀態
    success: bool = Field(True, description="處理是否成功")
    error: Optional[str] = Field(None, description="錯誤訊息")
    tokens_used: Optional[int] = Field(None, description="使用的 token 數量")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="額外元數據")
    
    # 向後兼容
    mood: Optional[str] = Field("neutral", description="舊版情緒標記（向後兼容）")


class LLMModuleData(BaseModel):
    """統一模組數據格式"""
    # 核心數據  
    text: Optional[str] = Field(None, description="主要文本內容")
    mode: Optional[str] = Field("chat", description="處理模式")
    
    # 上下文
    context: Optional[Dict[str, Any]] = Field(default_factory=dict, description="上下文信息")
    
    # 執行控制
    execution_mode: str = Field("sequential", description="執行模式")
    priority: int = Field(5, description="優先級")
    timeout: Optional[float] = Field(None, description="超時時間")
    
    # 模組間傳遞的數據
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="元數據")


# 創建統一數據格式的便利函數
def create_llm_data(text: str, mode: str = "chat", **kwargs) -> LLMModuleData:
    """創建 LLM 模組數據"""
    return LLMModuleData(
        text=text,
        mode=mode,
        context=kwargs.get("context", {}),
        execution_mode=kwargs.get("execution_mode", "sequential"),
        priority=kwargs.get("priority", 5),
        timeout=kwargs.get("timeout"),
        metadata=kwargs
    )

