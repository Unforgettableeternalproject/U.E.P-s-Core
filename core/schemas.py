"""
極簡模組 Schema 定義
用於模組間基礎通信 - 大部分數據由 Working Context 處理

設計原則:
- 只定義真正跨模組傳遞的基礎欄位
- 大部分狀態由 Working Context, State Manager, Session Manager 處理
- 模組內部使用各自的 Input/Output Schema
- 這裡只是「模組間傳遞」的最小公約數
"""

from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from enum import Enum


class ModuleCapability(str, Enum):
    """模組能力定義 (用於框架註冊)"""
    SPEECH_RECOGNITION = "speech_recognition"
    INTENT_CLASSIFICATION = "intent_classification"
    MEMORY_RETRIEVAL = "memory_retrieval"
    MEMORY_STORAGE = "memory_storage"
    TEXT_GENERATION = "text_generation"
    SPEECH_SYNTHESIS = "speech_synthesis"
    SYSTEM_CONTROL = "system_control"


class BaseModuleData(BaseModel):
    """
    模組間傳遞的基礎數據格式
    
    注意: 這只是「模組間傳遞」的最小結構
    - 說話人、情緒等由 Working Context 管理
    - 會話狀態由 Session Manager 管理
    - 系統狀態由 State Manager 管理
    """
    
    # === 核心數據 (幾乎所有模組都需要) ===
    text: Optional[str] = Field(None, description="主要文本內容")
    
    # === 來源追踪 ===
    source_module: Optional[str] = Field(None, description="來源模組")
    
    # === 狀態與錯誤 ===
    status: Optional[str] = Field(None, description="處理狀態: success/error/pending")
    error: Optional[str] = Field(None, description="錯誤訊息")
    
    # === 擴展欄位 (模組特定數據放這裡) ===
    metadata: Dict[str, Any] = Field(default_factory=dict, description="模組特定的額外數據")
    
    class Config:
        extra = "allow"  # 允許模組自由擴展


class STTModuleData(BaseModuleData):
    """
    STT → NLP 傳遞格式
    只包含 NLP 需要的基礎信息 (說話人已在 Working Context)
    """
    confidence: Optional[float] = Field(None, description="識別信心度")
    speaker_info: Optional[Dict[str, Any]] = Field(None, description="說話人基礎信息")
    activation_reason: Optional[str] = Field(None, description="啟動原因")


class NLPModuleData(BaseModuleData):
    """
    NLP → LLM/Router 傳遞格式
    只包含意圖分析結果 (語者已在 Working Context)
    """
    intent: Optional[str] = Field(None, description="主要意圖")
    confidence: Optional[float] = Field(None, description="分類信心度")
    entities: Optional[List[Dict[str, Any]]] = Field(None, description="實體識別結果")


class MEMModuleData(BaseModuleData):
    """
    LLM ↔ MEM 傳遞格式
    MEM 有自己完整的內部操作系統,這只是基礎交互格式
    """
    mode: Optional[str] = Field(None, description="操作模式: query/store/update")
    memory_context: Optional[str] = Field(None, description="記憶上下文（給 LLM）")
    relevant_memories: Optional[List[Dict[str, Any]]] = Field(None, description="相關記憶")


class LLMModuleData(BaseModuleData):
    """
    LLM → TTS/MEM 傳遞格式
    情緒已在 Working Context,這只是回應文本 + 可能的記憶指令
    """
    emotion: Optional[str] = Field(None, description="回應情緒 (給 TTS)")
    memory_instructions: Optional[List[Dict[str, Any]]] = Field(None, description="記憶操作指令 (給 MEM)")


class TTSModuleData(BaseModuleData):
    """
    LLM → TTS 傳遞格式
    語音合成參數 (模型、語速等)
    """
    voice_model: Optional[str] = Field(None, description="語音模型")
    speed: Optional[float] = Field(None, description="語速")
    pitch: Optional[float] = Field(None, description="音調")


class SYSModuleData(BaseModuleData):
    """
    NLP/Router → SYS 傳遞格式
    系統指令執行
    """
    action: Optional[str] = Field(None, description="系統動作")
    workflow_id: Optional[str] = Field(None, description="工作流ID")
    parameters: Optional[Dict[str, Any]] = Field(None, description="動作參數")


class ModuleResponse(BaseModel):
    """模組回應標準格式 (通用)"""
    status: str = Field("success", description="處理狀態: success/error/pending")
    data: Optional[Dict[str, Any]] = Field(None, description="回應數據")
    message: Optional[str] = Field(None, description="回應訊息")
    error: Optional[str] = Field(None, description="錯誤信息")
    execution_time: Optional[float] = Field(None, description="執行時間(秒)")


class ModuleCapabilities(BaseModel):
    """模組能力聲明 (用於 Framework 註冊)"""
    module_id: str = Field(..., description="模組ID")
    capabilities: List[ModuleCapability] = Field(..., description="支援的能力")
    dependencies: List[str] = Field(default_factory=list, description="依賴的模組")
    version: str = Field("1.0.0", description="模組版本")


# ===== 使用說明 =====
# 
# 1. 模組內部使用各自的 Input/Output Schema (如 STTInput/STTOutput)
# 2. 模組間傳遞時使用對應的 ModuleData (如 STTModuleData)
# 3. 大部分狀態已由 Working Context, State Manager, Session Manager 管理
# 4. 這裡的 Schema 只是「模組間傳遞」的最小公約數
#
# 示例:
#   STT 內部: STTInput → process() → STTOutput
#   STT → NLP: STTOutput.to_unified_format() → STTModuleData → NLP
