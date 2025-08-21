"""
統一模組 Schema 定義
用於重構後的模組間通信標準化
"""

from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List, Union
from enum import Enum


class ExecutionMode(str, Enum):
    """執行模式"""
    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"
    CONDITIONAL = "conditional"


class Priority(int, Enum):
    """優先級級別"""
    LOW = 1
    NORMAL = 5
    HIGH = 8
    CRITICAL = 10


class ModuleCapability(str, Enum):
    """模組能力定義"""
    SPEECH_RECOGNITION = "speech_recognition"
    INTENT_CLASSIFICATION = "intent_classification"
    MEMORY_RETRIEVAL = "memory_retrieval"
    MEMORY_STORAGE = "memory_storage"
    TEXT_GENERATION = "text_generation"
    SPEECH_SYNTHESIS = "speech_synthesis"
    SYSTEM_CONTROL = "system_control"


class UnifiedModuleData(BaseModel):
    """統一的模組數據格式 - 新 Schema 標準"""
    
    # === 核心數據 ===
    text: Optional[str] = Field(None, description="主要文本內容")
    intent: Optional[str] = Field(None, description="處理意圖")
    context: Optional[Dict[str, Any]] = Field(None, description="上下文信息")
    
    # === 執行控制 ===
    execution_mode: Optional[ExecutionMode] = Field(ExecutionMode.SEQUENTIAL, description="執行模式")
    priority: Optional[Priority] = Field(Priority.NORMAL, description="優先級")
    timeout: Optional[float] = Field(None, description="執行超時時間(秒)")
    
    # === 模組間傳遞的 metadata ===
    metadata: Dict[str, Any] = Field(default_factory=dict, description="模組間傳遞的元數據")
    source_module: Optional[str] = Field(None, description="來源模組")
    target_modules: Optional[List[str]] = Field(None, description="目標模組列表")
    
    # === 模組特定數據 (靈活擴展) ===
    module_data: Dict[str, Any] = Field(default_factory=dict, description="模組特定數據")
    
    # === 狀態信息 ===
    status: Optional[str] = Field("pending", description="處理狀態")
    error: Optional[str] = Field(None, description="錯誤信息")
    
    class Config:
        extra = "allow"  # 允許額外字段以保持靈活性


class STTModuleData(UnifiedModuleData):
    """STT 模組專用數據格式"""
    
    # STT 特定字段
    confidence: Optional[float] = Field(None, description="識別信心度")
    speaker_info: Optional[Dict[str, Any]] = Field(None, description="說話人信息")
    activation_reason: Optional[str] = Field(None, description="啟動原因")
    should_activate: Optional[bool] = Field(None, description="是否應該啟動")
    audio_data: Optional[bytes] = Field(None, description="音頻數據")
    language: Optional[str] = Field("zh-TW", description="語言設定")


class NLPModuleData(UnifiedModuleData):
    """NLP 模組專用數據格式"""
    
    # 語者身份相關
    speaker_id: Optional[str] = Field(None, description="語者ID")
    speaker_confidence: Optional[float] = Field(None, description="語者識別信心度")
    identity_id: Optional[str] = Field(None, description="使用者身份ID")
    identity_status: Optional[str] = Field(None, description="身份狀態")
    
    # 意圖分析相關
    primary_intent: Optional[str] = Field(None, description="主要意圖")
    intent_segments: Optional[List[Dict[str, Any]]] = Field(None, description="意圖片段")
    label: Optional[str] = Field(None, description="分類標籤")
    confidence: Optional[float] = Field(None, description="分類信心度")
    
    # 實體識別與語義分析
    entities: Optional[List[Dict[str, Any]]] = Field(None, description="實體識別結果")
    sentiment: Optional[str] = Field(None, description="情感分析結果")
    
    # 系統控制
    state_transition: Optional[Dict[str, Any]] = Field(None, description="建議的狀態轉換")
    awaiting_input: Optional[bool] = Field(None, description="等待進一步輸入")
    queue_states_added: Optional[List[str]] = Field(None, description="添加到狀態佇列的狀態")
    current_system_state: Optional[str] = Field(None, description="當前系統狀態")
    
    # 處理指引
    next_modules: Optional[List[str]] = Field(None, description="建議的下一步模組")
    processing_notes: Optional[List[str]] = Field(None, description="處理註記")


class MEMModuleData(UnifiedModuleData):
    """MEM 模組專用數據格式"""
    
    # MEM 特定字段
    mode: str = Field("fetch", description="操作模式: fetch/store/delete/list")
    top_k: Optional[int] = Field(3, description="返回結果數量")
    entry: Optional[Dict[str, Any]] = Field(None, description="記憶條目")
    page: Optional[int] = Field(1, description="頁數")
    results: Optional[List[Dict[str, Any]]] = Field(None, description="查詢結果")


class LLMModuleData(UnifiedModuleData):
    """LLM 模組專用數據格式"""
    
    # LLM 特定字段
    memory: Optional[str] = Field(None, description="相關記憶")
    mood: Optional[str] = Field("neutral", description="情感狀態")
    emotion: Optional[str] = Field("neutral", description="情緒")
    sys_action: Optional[str] = Field(None, description="系統動作")
    temperature: Optional[float] = Field(None, description="生成溫度")
    max_tokens: Optional[int] = Field(None, description="最大令牌數")


class TTSModuleData(UnifiedModuleData):
    """TTS 模組專用數據格式"""
    
    # TTS 特定字段
    mood: Optional[str] = Field("neutral", description="語音情感")
    save: Optional[bool] = Field(False, description="是否保存音頻")
    voice_model: Optional[str] = Field(None, description="語音模型")
    speed: Optional[float] = Field(1.0, description="語速")
    pitch: Optional[float] = Field(1.0, description="音調")


class SYSModuleData(UnifiedModuleData):
    """SYS 模組專用數據格式"""
    
    # SYS 特定字段
    action: Optional[str] = Field(None, description="系統動作")
    workflow_id: Optional[str] = Field(None, description="工作流ID")
    parameters: Optional[Dict[str, Any]] = Field(None, description="動作參數")


class ModuleResponse(BaseModel):
    """模組回應標準格式"""
    
    status: str = Field("success", description="處理狀態: success/error/pending")
    data: Optional[UnifiedModuleData] = Field(None, description="回應數據")
    message: Optional[str] = Field(None, description="回應訊息")
    error: Optional[str] = Field(None, description="錯誤信息")
    execution_time: Optional[float] = Field(None, description="執行時間(秒)")
    
    # 向後兼容的字段
    text: Optional[str] = Field(None, description="文本回應(向後兼容)")
    result: Optional[Any] = Field(None, description="結果數據(向後兼容)")


class ModuleCapabilities(BaseModel):
    """模組能力聲明"""
    
    module_id: str = Field(..., description="模組ID")
    capabilities: List[ModuleCapability] = Field(..., description="支援的能力")
    input_formats: List[str] = Field(..., description="支援的輸入格式")
    output_formats: List[str] = Field(..., description="支援的輸出格式")
    dependencies: List[str] = Field(default_factory=list, description="依賴的模組")
    version: str = Field("1.0.0", description="模組版本")


# === 工廠函數 ===

def create_stt_data(**kwargs) -> STTModuleData:
    """創建 STT 模組數據"""
    return STTModuleData(**kwargs)


def create_nlp_data(**kwargs) -> NLPModuleData:
    """創建 NLP 模組數據"""
    return NLPModuleData(**kwargs)


def create_mem_data(**kwargs) -> MEMModuleData:
    """創建 MEM 模組數據"""
    return MEMModuleData(**kwargs)


def create_llm_data(**kwargs) -> LLMModuleData:
    """創建 LLM 模組數據"""
    return LLMModuleData(**kwargs)


def create_tts_data(**kwargs) -> TTSModuleData:
    """創建 TTS 模組數據"""
    return TTSModuleData(**kwargs)


def create_sys_data(**kwargs) -> SYSModuleData:
    """創建 SYS 模組數據"""
    return SYSModuleData(**kwargs)


# === 向後兼容工具 ===

class LegacyDataAdapter:
    """舊格式數據適配器"""
    
    @staticmethod
    def adapt_to_unified(legacy_data: Dict[str, Any], module_type: str) -> UnifiedModuleData:
        """將舊格式數據轉換為統一格式"""
        
        if module_type == "nlp":
            return NLPModuleData(
                text=legacy_data.get("text"),
                intent=legacy_data.get("intent"),
                label=legacy_data.get("label"),
                confidence=legacy_data.get("confidence"),
                metadata=legacy_data
            )
        
        elif module_type == "mem":
            return MEMModuleData(
                text=legacy_data.get("text"),
                mode=legacy_data.get("mode", "fetch"),
                top_k=legacy_data.get("top_k", 3),
                entry=legacy_data.get("entry"),
                metadata=legacy_data
            )
        
        elif module_type == "llm":
            return LLMModuleData(
                text=legacy_data.get("text"),
                intent=legacy_data.get("intent"),
                memory=legacy_data.get("memory"),
                mood=legacy_data.get("mood", "neutral"),
                metadata=legacy_data
            )
        
        elif module_type == "tts":
            return TTSModuleData(
                text=legacy_data.get("text"),
                mood=legacy_data.get("mood", "neutral"),
                save=legacy_data.get("save", False),
                metadata=legacy_data
            )
        
        else:
            # 通用適配
            return UnifiedModuleData(
                text=legacy_data.get("text"),
                intent=legacy_data.get("intent"),
                metadata=legacy_data
            )
    
    @staticmethod
    def adapt_from_unified(unified_data: UnifiedModuleData, target_format: str) -> Dict[str, Any]:
        """將統一格式數據轉換為目標格式"""
        
        base_dict = unified_data.model_dump(exclude_none=True)
        
        if target_format == "legacy":
            # 返回平坦化的字典格式
            result = {
                "text": unified_data.text,
                "intent": unified_data.intent,
                "status": unified_data.status
            }
            
            # 合併 module_data
            if unified_data.module_data:
                result.update(unified_data.module_data)
            
            return result
        
        return base_dict


# === 示例用法 ===

if __name__ == "__main__":
    # 創建統一格式數據
    nlp_data = create_nlp_data(
        text="你好，我是用戶",
        intent="chat",
        label="chat",
        confidence=0.95,
        metadata={"source": "user_input"}
    )
    
    print("NLP 數據:", nlp_data.model_dump_json(indent=2))
    
    # 舊格式適配示例
    legacy_data = {"text": "測試", "intent": "chat", "old_field": "value"}
    adapted_data = LegacyDataAdapter.adapt_to_unified(legacy_data, "nlp")
    print("適配後的數據:", adapted_data.model_dump_json(indent=2))
