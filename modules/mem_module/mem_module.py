from core.module_base import BaseModule
from datetime import datetime
from typing import List, Dict, Any, Optional
from .working_context_handler import register_memory_context_handler
from .schemas import (
    MEMInput, MEMOutput
)
from core.schemas import MEMModuleData
from core.schema_adapter import MEMSchemaAdapter
from core.working_context import working_context_manager
from configs.config_loader import load_module_config
from utils.debug_helper import debug_log, debug_log_e, info_log, error_log

class MEMModule(BaseModule):
    """記憶管理模組 - Phase 2 重構版本
    
    新功能：
    1. 身份隔離記憶系統 (Memory Token機制)
    2. 短期/長期記憶分層管理
    3. 對話快照系統
    4. LLM記憶操作指令支援
    5. 與NLP模組深度整合
    6. Working Context決策處理
    """
    
    def __init__(self, config=None):
        """初始化MEM模組"""
        super().__init__()
        
        # 載入配置
        self.config = config or load_module_config("mem_module")
        
        # 基礎設定（向後兼容）
        self.embedding_model = self.config.get("embedding_model", "all-MiniLM-L6-v2")
        self.index_file = self.config.get("index_file", "memory/faiss_index")
        self.metadata_file = self.config.get("metadata_file", "memory/metadata.json")
        self.max_distance = self.config.get("max_distance", 0.85)
        
        # 新架構組件（延遲初始化）
        self.memory_manager = None
        self.storage_manager = None
        self.nlp_integration = None
        self.working_context_handler = None
        self.schema_adapter = MEMSchemaAdapter()
        
        # 模組狀態
        self.is_initialized = False

        info_log("[MEM] Phase 2 記憶管理模組初始化完成")

    def debug(self):
        # Debug level = 1
        debug_log(1, "[MEM] Debug 模式啟用")
        debug_log(1, f"[MEM] 新架構模式啟用")
        
        # Debug level = 2
        debug_log(2, f"[MEM] 嵌入模型: {self.embedding_model}")
        debug_log(2, f"[MEM] FAISS 索引檔案: {self.index_file}")
        debug_log(2, f"[MEM] 元資料檔案: {self.metadata_file}")
        debug_log(2, f"[MEM] 記憶管理器狀態: {'已載入' if self.memory_manager else '未載入'}")
        
        # Debug level = 3
        debug_log(3, f"[MEM] 完整模組設定: {self.config}")

    def initialize(self):
        """初始化MEM模組"""
        debug_log(1, "[MEM] 初始化中...")
        self.debug()

        try:
            # 使用新架構
            return self._initialize_new_architecture()
                
        except Exception as e:
            error_log(f"[MEM] 初始化失敗: {e}")
            return False
    
    def _initialize_new_architecture(self) -> bool:
        """初始化新架構"""
        try:
            info_log("[MEM] 初始化新重構記憶管理系統...")
            
            # 動態導入新架構組件（避免循環導入）
            from .memory_manager import MemoryManager
            
            # 初始化重構的記憶管理器
            self.memory_manager = MemoryManager(self.config.get("mem_module", {}))
            if not self.memory_manager.initialize():
                error_log("[MEM] 重構記憶管理器初始化失敗")
                return False
            
            # 註冊Working Context處理器
            self.working_context_handler = register_memory_context_handler(
                working_context_manager, self.memory_manager
            )
            if not self.working_context_handler:
                error_log("[MEM] Working Context處理器註冊失敗")
                return False
            
            # 新架構不需要舊版FAISS相容性
            self.is_initialized = True
            info_log("[MEM] 重構架構初始化完成")
            return True
            
        except Exception as e:
            error_log(f"[MEM] 重構架構初始化失敗: {e}")
            return False


    def register(self):
        """註冊方法 - 返回模組實例"""
        return self

    def handle(self, data=None):
        """處理輸入數據 - 支援新舊兩種模式"""
        try:
            if not self.is_initialized:
                error_log("[MEM] 模組未初始化")
                return self._create_error_response("模組未初始化")
            
            # 處理核心Schema格式
            if isinstance(data, MEMModuleData):
                return self._handle_core_schema(data)
            
            # 處理新架構Schema格式
            if isinstance(data, MEMInput):
                if self.memory_manager:
                    return self._handle_new_schema(data)
                else:
                    return self._create_error_response("記憶管理器未初始化")
            
            # 預設處理
            debug_log(2, "[MEM] 使用預設記憶檢索處理")
            query_text = str(data) if data else ""
            return self._retrieve_memory(query_text)
            
        except Exception as e:
            error_log(f"[MEM] 處理請求失敗: {e}")
            return self._create_error_response(f"處理失敗: {str(e)}")
    
    def _handle_core_schema(self, data: MEMModuleData) -> Dict[str, Any]:
        """處理核心Schema格式"""
        try:
            debug_log(2, f"[MEM] 處理核心Schema: {data.operation_type}")
            
            if data.operation_type == "query":
                # 記憶查詢
                results = self._retrieve_memory(data.query_text, data.max_results or 5)
                return {
                    "success": True,
                    "operation_type": "query",
                    "results": results,
                    "total_results": len(results)
                }
            elif data.operation_type == "store":
                # 存儲記憶
                if data.content:
                    metadata = {
                        "identity_token": data.identity_token or "anonymous",
                        "memory_type": data.memory_type or "general",
                        "timestamp": datetime.now().isoformat(),
                        "metadata": data.metadata or {}
                    }
                    self._add_memory(data.content, metadata)
                    return {"success": True, "operation_type": "store", "message": "記憶已存儲"}
                else:
                    return self._create_error_response("存儲內容不能為空")
            else:
                return self._create_error_response(f"不支援的操作類型: {data.operation_type}")
                
        except Exception as e:
            error_log(f"[MEM] 核心Schema處理失敗: {e}")
            return self._create_error_response(f"處理失敗: {str(e)}")
    
    def _handle_new_schema(self, data: MEMInput) -> MEMOutput:
        """處理新架構Schema格式"""
        try:
            debug_log(2, f"[MEM] 使用新架構處理: {data.operation_type}")
            
            if data.operation_type == "query":
                # 使用新記憶管理器查詢
                if data.query_data:
                    results = self.memory_manager.process_memory_query(data.query_data)
                    memory_context = ""
                    if self.nlp_integration:
                        memory_context = self.nlp_integration.extract_memory_context_for_llm(results)
                    
                    return MEMOutput(
                        success=True,
                        operation_type="query",
                        search_results=results,
                        memory_context=memory_context,
                        total_memories=len(results)
                    )
                else:
                    return MEMOutput(
                        success=False,
                        operation_type="query",
                        errors=["查詢資料不能為空"]
                    )
            
            elif data.operation_type == "create_snapshot":
                # 創建對話快照
                if data.conversation_text and data.identity_token:
                    snapshot = self.memory_manager.create_conversation_snapshot(
                        identity_token=data.identity_token,
                        conversation_text=data.conversation_text,
                        topic=data.intent_info.get("primary_intent") if data.intent_info else None
                    )
                    
                    return MEMOutput(
                        success=bool(snapshot),
                        operation_type="create_snapshot",
                        active_snapshots=[snapshot] if snapshot else [],
                        message="快照創建成功" if snapshot else "快照創建失敗"
                    )
                else:
                    return MEMOutput(
                        success=False,
                        operation_type="create_snapshot",
                        errors=["身份令牌和對話文本不能為空"]
                    )
            
            elif data.operation_type == "process_llm_instruction":
                # 處理LLM記憶指令
                if data.llm_instructions:
                    results = self.memory_manager.process_llm_instructions(data.llm_instructions)
                    return MEMOutput(
                        success=all(r.success for r in results),
                        operation_type="process_llm_instruction",
                        operation_results=results
                    )
                else:
                    return MEMOutput(
                        success=False,
                        operation_type="process_llm_instruction",
                        errors=["LLM指令不能為空"]
                    )
            
            else:
                return MEMOutput(
                    success=False,
                    operation_type=data.operation_type,
                    errors=[f"不支援的操作類型: {data.operation_type}"]
                )
                
        except Exception as e:
            error_log(f"[MEM] 新架構處理失敗: {e}")
            return MEMOutput(
                success=False,
                operation_type=data.operation_type,
                errors=[f"處理失敗: {str(e)}"]
            )
    
    def _create_error_response(self, message: str) -> Dict[str, Any]:
        """創建錯誤回應"""
        return {
            "success": False,
            "error": message,
            "status": "failed"
        }

    # === 新架構支援方法 ===
    
    def process_nlp_output(self, nlp_output) -> Optional[MEMOutput]:
        """處理來自NLP模組的輸出（新架構）"""
        if not self.nlp_integration:
            debug_log(2, "[MEM] NLP整合未啟用，跳過NLP輸出處理")
            return None
        
        try:
            mem_input = self.nlp_integration.process_nlp_output(nlp_output)
            return self._handle_new_schema(mem_input)
        except Exception as e:
            error_log(f"[MEM] 處理NLP輸出失敗: {e}")
            return None
    
    def get_memory_context_for_llm(self, identity_token: str, query_text: str) -> str:
        """為LLM獲取記憶上下文"""
        try:
            if self.memory_manager:
                from .schemas import MemoryQuery
                query = MemoryQuery(
                    identity_token=identity_token,
                    query_text=query_text,
                    max_results=5
                )
                results = self.memory_manager.process_memory_query(query)
                if self.nlp_integration:
                    return self.nlp_integration.extract_memory_context_for_llm(results)
            
            return ""
            
        except Exception as e:
            error_log(f"[MEM] 獲取LLM記憶上下文失敗: {e}")
            return ""

    def shutdown(self):
        """模組關閉"""
        info_log("[MEM] 模組關閉")
        if self.memory_manager:
            # 如果需要，可以在這裡添加記憶管理器的清理邏輯
            pass
