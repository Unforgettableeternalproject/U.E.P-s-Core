# modules/mem_module/schemas.py
"""
MEM模組內部Schema定義
包含詳細的記憶管理資料結構和操作介面

注意：與其他模組通信時使用 core.schemas.MEMModuleData
這裡的schema主要用於模組內部操作和詳細資料結構
"""

from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any, Literal, Union, TYPE_CHECKING
from enum import Enum
from datetime import datetime
from core.schemas import MEMModuleData as CoreMEMData

if TYPE_CHECKING:
    from modules.nlp_module.schemas import UserProfile


# === 記憶類型與基礎結構 ===

class MemoryType(str, Enum):
    """記憶類型"""
    # === 短期記憶 ===
    SNAPSHOT = "snapshot"              # 對話快照（短期記憶，儲存對話內容）
    
    # === 長期記憶 ===
    PROFILE = "profile"                # 使用者檔案（系統對使用者的認知）
    PREFERENCE = "preference"          # 使用者偏好（系統學到的使用者喜好）
    LONG_TERM = "long_term"           # 通用長期記憶（向後相容）
    SYSTEM_LEARNING = "system_learning"         # 系統學習資料
    
    # 已廢棄（向後相容，映射到 SNAPSHOT）
    # INTERACTION_HISTORY = "interaction_history"  # 已廢棄，請使用 SNAPSHOT


class MemoryImportance(str, Enum):
    """記憶重要性等級"""
    CRITICAL = "critical"      # 關鍵記憶（永久保存）
    HIGH = "high"             # 高重要性（長期保存）
    MEDIUM = "medium"         # 中等重要性（中期保存）
    LOW = "low"               # 低重要性（短期保存）
    TEMPORARY = "temporary"   # 臨時記憶（隨時可刪除）


class MemoryEntry(BaseModel):
    """記憶條目基礎結構"""
    memory_id: str = Field(..., description="記憶條目唯一識別")
    memory_token: str = Field(..., description="記憶令牌（記憶隔離）")
    memory_type: MemoryType = Field(..., description="記憶類型")
    
    # 內容相關
    content: str = Field(..., description="記憶內容")
    summary: Optional[str] = Field(None, description="內容摘要")
    keywords: List[str] = Field(default_factory=list, description="關鍵詞")
    
    # 分類與檢索
    topic: Optional[str] = Field(None, description="主題標籤")
    category: Optional[str] = Field(None, description="分類")
    intent_tags: List[str] = Field(default_factory=list, description="意圖標籤")
    
    # 會話相關
    session_id: Optional[str] = Field(None, description="關聯會話ID")
    
    # 時間與版本
    created_at: datetime = Field(default_factory=datetime.now, description="創建時間")
    updated_at: Optional[datetime] = Field(None, description="更新時間")
    accessed_at: Optional[datetime] = Field(None, description="最後存取時間")
    
    # 重要性與評分
    importance_score: float = Field(0.5, description="重要性評分 0-1")
    access_count: int = Field(0, description="存取次數")
    
    # 向量嵌入
    embedding_vector: Optional[List[float]] = Field(None, description="內容嵌入向量")
    
    # 元資料
    metadata: Dict[str, Any] = Field(default_factory=dict, description="額外元資料")


class ConversationSnapshot(MemoryEntry):
    """對話快照（短期記憶專用）"""
    memory_type: Literal[MemoryType.SNAPSHOT] = MemoryType.SNAPSHOT
    
    # 對話相關
    stage_number: int = Field(..., description="對話階段編號")
    message_count: int = Field(0, description="本階段消息數量")
    participant_count: int = Field(1, description="參與者數量")
    messages: List[Dict[str, Any]] = Field(default_factory=list, description="快照中的訊息")
    participant_info: Dict[str, Dict[str, Any]] = Field(default_factory=dict, description="參與者資訊")
    
    # GSID過期管理
    gsid: int = Field(..., description="General Session ID - 用於短期記憶過期管理")
    
    # 狀態與控制
    is_active: bool = Field(True, description="是否為活躍快照")
    is_archived: bool = Field(False, description="是否已歸檔")
    max_context_length: int = Field(2000, description="最大上下文長度")
    
    # 壓縮與摘要
    compression_level: int = Field(0, description="壓縮等級 0=原始 1-3=不同程度摘要")
    original_length: Optional[int] = Field(None, description="原始內容長度")
    
    # 時間追蹤
    start_time: Optional[datetime] = Field(None, description="快照開始時間")
    key_topics: List[str] = Field(default_factory=list, description="關鍵主題")


class LongTermMemoryEntry(MemoryEntry):
    """長期記憶條目"""
    memory_type: Literal[
        MemoryType.PROFILE, 
        MemoryType.PREFERENCE, 
        MemoryType.LONG_TERM,
        MemoryType.SYSTEM_LEARNING
    ] = Field(..., description="長期記憶類型")
    
    # 持久化相關
    persistence_level: int = Field(5, description="持久化等級 1-10")
    retention_policy: Optional[str] = Field(None, description="保留政策")
    
    # 關聯性
    related_memories: List[str] = Field(default_factory=list, description="關聯記憶ID")
    reference_count: int = Field(0, description="被引用次數")


# === 查詢與操作介面 ===

class MemoryQuery(BaseModel):
    """記憶查詢請求"""
    memory_token: str = Field(..., description="查詢者記憶令牌")
    query_text: str = Field(..., description="查詢文本")
    
    # 過濾條件
    memory_types: Optional[List[MemoryType]] = Field(None, description="記憶類型過濾")
    topic_filter: Optional[str] = Field(None, description="主題過濾")
    time_range: Optional[Dict[str, datetime]] = Field(None, description="時間範圍")
    importance_filter: Optional[List[MemoryImportance]] = Field(None, description="重要性過濾")
    
    # 檢索參數
    max_results: int = Field(10, description="最大結果數")
    similarity_threshold: float = Field(0.7, description="相似度閾值")
    include_archived: bool = Field(False, description="包含已歸檔記憶")
    
    # 上下文相關
    current_intent: Optional[str] = Field(None, description="當前意圖")
    conversation_context: Optional[str] = Field(None, description="對話上下文")


class MemorySearchResult(BaseModel):
    """記憶搜索結果"""
    memory_entry: MemoryEntry = Field(..., description="記憶條目")
    similarity_score: float = Field(..., description="相似度評分")
    relevance_score: float = Field(..., description="相關性評分")
    retrieval_reason: str = Field(..., description="檢索原因")
    
    # SemanticRetriever 需要的額外屬性
    retrieval_method: str = Field(default="semantic", description="檢索方法")
    context_relevance: float = Field(default=0.0, description="上下文相關性")
    context_match: Optional[bool] = Field(default=None, description="上下文匹配")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="附加元資料")


class MemoryOperationResult(BaseModel):
    """記憶操作結果"""
    success: bool = Field(..., description="操作是否成功")
    operation_type: str = Field(..., description="操作類型")
    memory_id: Optional[str] = Field(None, description="涉及的記憶ID")
    message: str = Field(..., description="操作結果訊息")
    affected_count: int = Field(0, description="影響的記憶條目數量")


class LLMMemoryInstruction(BaseModel):
    """LLM記憶操作指令"""
    instruction_type: Literal[
        "create_snapshot", 
        "update_memory", 
        "delete_memory", 
        "archive_snapshot",
        "create_longterm_memory",
        "update_topic"
    ] = Field(..., description="指令類型")
    
    memory_token: str = Field(..., description="記憶令牌")
    target_memory_id: Optional[str] = Field(None, description="目標記憶ID")
    content: Optional[str] = Field(None, description="記憶內容")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="操作元資料")
    reasoning: str = Field(..., description="操作理由")


# === 存儲與索引相關 ===

class VectorIndexConfig(BaseModel):
    """向量索引配置"""
    dimension: int = Field(384, description="向量維度")
    index_type: str = Field("IndexFlatL2", description="索引類型")
    metric_type: str = Field("L2", description="距離計算方式")
    nlist: Optional[int] = Field(None, description="聚類中心數量（IVF索引）")
    nprobe: Optional[int] = Field(None, description="搜索聚類數量（IVF索引）")


class StorageStats(BaseModel):
    """存儲統計資訊"""
    total_memories: int = Field(0, description="總記憶數量")
    memory_by_type: Dict[str, int] = Field(default_factory=dict, description="按類型統計")
    memory_by_user: Dict[str, int] = Field(default_factory=dict, description="按使用者統計")
    total_size_mb: float = Field(0.0, description="總存儲大小（MB）")
    index_size_mb: float = Field(0.0, description="索引大小（MB）")
    last_updated: datetime = Field(default_factory=datetime.now, description="最後更新時間")


# === 與核心Schema的轉換工具 ===

class MEMSchemaAdapter:
    """MEM模組Schema適配器 - 處理內部Schema與核心Schema的轉換"""
    
    @staticmethod
    def to_core_data(
        operation_type: str,
        memory_query: Optional[MemoryQuery] = None,
        search_results: Optional[List[MemorySearchResult]] = None,
        operation_results: Optional[List[MemoryOperationResult]] = None,
        **kwargs
    ) -> CoreMEMData:
        """轉換為核心Schema格式"""
        
        core_data = CoreMEMData(
            mode=operation_type,
            **kwargs
        )
        
        # 處理查詢相關
        if memory_query:
            core_data.query_text = memory_query.query_text
            core_data.identity_token = memory_query.memory_token  # 核心Schema使用identity_token欄位存儲memory_token
            core_data.memory_types = [mt.value for mt in memory_query.memory_types] if memory_query.memory_types else None
            core_data.topic_filter = memory_query.topic_filter
            core_data.similarity_threshold = memory_query.similarity_threshold
            core_data.include_archived = memory_query.include_archived
            core_data.top_k = memory_query.max_results
        
        # 處理搜索結果
        if search_results:
            core_data.search_results = [
                {
                    "memory_entry": result.memory_entry.model_dump(),
                    "similarity_score": result.similarity_score,
                    "relevance_score": result.relevance_score,
                    "retrieval_reason": result.retrieval_reason
                }
                for result in search_results
            ]
            core_data.results = core_data.search_results  # 向後兼容
        
        # 處理操作結果
        if operation_results:
            core_data.operation_results = [
                result.model_dump() for result in operation_results
            ]
        
        return core_data
    
    @staticmethod
    def from_core_data(core_data: CoreMEMData) -> Dict[str, Any]:
        """從核心Schema格式轉換"""
        
        result = {
            "operation_type": core_data.mode,
            "memory_token": core_data.identity_token,  # 核心Schema的identity_token對應到記憶令牌
            "text": core_data.text,
            "context": core_data.context
        }
        
        # 處理查詢
        if core_data.query_text:
            result["memory_query"] = MemoryQuery(
                memory_token=core_data.identity_token or "",  # 核心Schema的identity_token對應到記憶令牌
                query_text=core_data.query_text,
                memory_types=[MemoryType(mt) for mt in core_data.memory_types] if core_data.memory_types else None,
                topic_filter=core_data.topic_filter,
                max_results=core_data.top_k or 10,
                similarity_threshold=core_data.similarity_threshold or 0.7,
                include_archived=core_data.include_archived or False,
                current_intent=core_data.current_intent,
                conversation_context=core_data.conversation_context
            )
        
        # 處理LLM指令
        if core_data.llm_instructions:
            result["llm_instructions"] = [
                LLMMemoryInstruction(**instr) for instr in core_data.llm_instructions
            ]
        
        # 處理記憶條目
        if core_data.memory_entry:
            result["memory_entry"] = MemoryEntry(**core_data.memory_entry)
        
        return result


# === 輸入輸出Schema (模組內部使用) ===

class MEMInput(BaseModel):
    """MEM模組內部輸入格式"""
    operation_type: Literal[
        # 基本操作
        "query", "store", "update", "delete", 
        "create_snapshot", "process_llm_instruction",
        
        # 記憶令牌管理
        "validate_token", "process_identity",
        
        # 記憶管理
        "store_memory", "query_memory", 
        
        # NLP整合
        "process_nlp_output", "update_context",
        
        # 分析與總結
        "generate_summary", "extract_key_points",
        
        # 用戶特質管理
        "integrate_user_characteristics",
        
        # LLM交互
        "generate_llm_instruction", "process_llm_response",
        
        # 會話管理
        "create_session", "get_session_info", "add_session_interaction",
        "get_session_history", "update_session_context", "get_session_context",
        "end_session", "archive_session", "search_archived_sessions",
        "process_conversation_turn", "handle_topic_transition",
        "convert_session_to_memory", "load_user_context",
        "provide_contextual_guidance", "preserve_session_context",
        "retrieve_session_context", "record_session_content",
        "generate_session_summary", "get_snapshot_history"
    ] = Field(..., description="操作類型")
    
    # 記憶令牌相關
    memory_token: Optional[str] = Field(None, description="記憶存取令牌")
    user_profile: Optional[Dict[str, Any]] = Field(None, description="使用者檔案（來自NLP）")
    
    # 查詢相關
    query_data: Optional[MemoryQuery] = Field(None, description="查詢資料")
    query_text: Optional[str] = Field(None, description="查詢文本")
    max_results: Optional[int] = Field(None, description="最大結果數量")
    memory_types: Optional[List[str]] = Field(None, description="記憶類型過濾")
    
    # 存儲相關
    memory_entry: Optional[Dict[str, Any]] = Field(None, description="要存儲的記憶條目")
    
    # LLM指令相關
    llm_instructions: Optional[List[LLMMemoryInstruction]] = Field(None, description="LLM記憶指令")
    llm_response: Optional[Dict[str, Any]] = Field(None, description="LLM回應資料")
    
    # 上下文相關
    conversation_text: Optional[str] = Field(None, description="當前對話文本")
    conversation_context: Optional[Dict[str, Any]] = Field(None, description="對話上下文")
    intent_info: Optional[Dict[str, Any]] = Field(None, description="意圖資訊（來自NLP）")
    
    # 會話管理相關
    session_id: Optional[str] = Field(None, description="會話ID")
    session_metadata: Optional[Dict[str, Any]] = Field(None, description="會話元數據")
    session_context: Optional[Dict[str, Any]] = Field(None, description="會話上下文")
    session_content: Optional[Dict[str, Any]] = Field(None, description="會話內容")
    interaction_data: Optional[Dict[str, Any]] = Field(None, description="互動資料")
    interaction_content: Optional[str] = Field(None, description="互動內容")
    turn_number: Optional[int] = Field(None, description="對話輪次")
    previous_context: Optional[Dict[str, Any]] = Field(None, description="之前的上下文")
    current_topic: Optional[str] = Field(None, description="當前話題")
    transition_phase: Optional[str] = Field(None, description="轉換階段")
    memory_extraction_options: Optional[Dict[str, Any]] = Field(None, description="記憶提取選項")
    context_scope: Optional[str] = Field(None, description="上下文範圍")
    use_previous_context: Optional[bool] = Field(None, description="使用之前的上下文")
    load_previous_context: Optional[bool] = Field(None, description="載入之前的上下文")
    create_summary: Optional[bool] = Field(None, description="創建總結")
    summary_options: Optional[Dict[str, Any]] = Field(None, description="總結選項")
    archive_options: Optional[Dict[str, Any]] = Field(None, description="歸檔選項")
    search_scope: Optional[str] = Field(None, description="搜尋範圍")


class MEMOutput(BaseModel):
    """MEM模組內部輸出格式"""
    success: bool = Field(..., description="操作是否成功")
    operation_type: str = Field(..., description="執行的操作類型")
    
    # 通用消息
    message: Optional[str] = Field(None, description="操作結果消息")
    
    # 查詢結果
    search_results: List[MemorySearchResult] = Field(default_factory=list, description="搜索結果")
    
    # 操作結果
    operation_results: List[MemoryOperationResult] = Field(default_factory=list, description="操作結果")
    operation_result: Optional[Dict[str, Any]] = Field(None, description="單個操作結果")
    
    # LLM提示相關
    memory_context: Optional[str] = Field(None, description="記憶上下文（供LLM使用）")
    memory_summary: Optional[Dict[str, Any]] = Field(None, description="結構化記憶總結")
    relevant_memories: List[MemoryEntry] = Field(default_factory=list, description="相關記憶條目")
    
    # 快照管理
    active_snapshots: List[ConversationSnapshot] = Field(default_factory=list, description="活躍快照")
    snapshot_summary: Optional[str] = Field(None, description="快照摘要")
    
    # 會話管理相關
    session_id: Optional[str] = Field(None, description="會話ID")
    session_context: Optional[Dict[str, Any]] = Field(None, description="會話上下文")
    session_info: Optional[Dict[str, Any]] = Field(None, description="會話資訊")
    interaction_count: Optional[int] = Field(None, description="互動數量")
    
    # 統計與狀態
    total_memories: int = Field(0, description="總記憶數量")
    memory_usage: Dict[str, int] = Field(default_factory=dict, description="記憶使用統計")
    
    # 通用數據字段
    data: Optional[Dict[str, Any]] = Field(None, description="操作相關數據")
    
    # 錯誤與警告
    errors: List[str] = Field(default_factory=list, description="錯誤訊息")
    warnings: List[str] = Field(default_factory=list, description="警告訊息")
