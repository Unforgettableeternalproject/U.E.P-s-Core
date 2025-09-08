from core.module_base import BaseModule
from sentence_transformers import SentenceTransformer
import faiss
import numpy as np
import os
import json
from datetime import datetime
from typing import List, Dict, Any, Optional
from .schemas import (
    MEMInput, MEMOutput, MemoryEntry, ConversationSnapshot, 
    LongTermMemoryEntry, MemoryQuery, MemorySearchResult,
    LLMMemoryInstruction, MemoryOperationResult, MemoryType
)
from core.schemas import MEMModuleData, create_mem_data
from core.schema_adapter import MEMSchemaAdapter
from core.working_context import working_context_manager
from configs.config_loader import load_module_config
from utils.debug_helper import debug_log, debug_log_e, info_log, error_log
from core.module_base import BaseModule
import os
import json
from typing import List, Dict, Any, Optional
from .schemas import (
    MEMInput, MEMOutput, MemoryEntry, ConversationSnapshot, 
    LongTermMemoryEntry, MemoryQuery, MemorySearchResult,
    LLMMemoryInstruction, MemoryOperationResult, MemoryType
)
from core.schemas import MEMModuleData, create_mem_data
from core.schema_adapter import MEMSchemaAdapter
from core.working_context import working_context_manager
from configs.config_loader import load_module_config
from utils.debug_helper import debug_log, debug_log_e, info_log, error_log
from .working_context_handler import register_memory_context_handler

# 嘗試導入可選依賴
try:
    from sentence_transformers import SentenceTransformer
    import faiss
    import numpy as np
    OPTIONAL_DEPS_AVAILABLE = True
except ImportError as e:
    debug_log(1, f"[MEM] 可選依賴未安裝: {e}")
    SentenceTransformer = None
    faiss = None
    np = None
    OPTIONAL_DEPS_AVAILABLE = False
from sentence_transformers import SentenceTransformer
import faiss
import numpy as np
import os
import json
from typing import List, Dict, Any, Optional
from .schemas import (
    MEMInput, MEMOutput, MemoryEntry, ConversationSnapshot, 
    LongTermMemoryEntry, MemoryQuery, MemorySearchResult,
    LLMMemoryInstruction, MemoryOperationResult, MemoryType
)
from core.schemas import MEMModuleData, create_mem_data
from core.schema_adapter import MEMSchemaAdapter
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
        
        # 舊版相容性
        self.model = None
        self.index = None
        self.metadata: List[dict] = []
        self.dimension = 384
        
        # 模組狀態
        self.is_initialized = False
        self._legacy_mode = False  # 是否使用舊版模式
        
        info_log("[MEM] Phase 2 記憶管理模組初始化完成")

    def debug(self):
        # Debug level = 1
        debug_log(1, "[MEM] Debug 模式啟用")
        debug_log(1, f"[MEM] 重構模式: {'新架構' if not self._legacy_mode else '舊版相容'}")
        
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
            # 檢查是否啟用新架構
            use_new_architecture = self.config.get("mem_module", {}).get("enable_new_architecture", True)
            
            if use_new_architecture:
                try:
                    return self._initialize_new_architecture()
                except Exception as e:
                    error_log(f"[MEM] 新架構初始化失敗: {e}")
                    info_log("[MEM] 回退到舊版架構模式")
                    self._legacy_mode = True
                    return self._initialize_legacy_mode()
            else:
                info_log("[MEM] 使用舊版架構模式")
                self._legacy_mode = True
                return self._initialize_legacy_mode()
                
        except Exception as e:
            error_log(f"[MEM] 初始化失敗: {e}")
            # 回退到舊版模式
            error_log("[MEM] 強制回退到舊版架構模式")
            self._legacy_mode = True
            return self._initialize_legacy_mode()
    
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
            
            # 嘗試載入嵌入模型以保持向後相容性（可選）
            try:
                info_log("[MEM] 嘗試載入嵌入模型以保持向後相容...")
                from sentence_transformers import SentenceTransformer
                self.model = SentenceTransformer(self.embedding_model)
                
                if self._faiss_index_exists():
                    info_log("[MEM] 載入現有FAISS索引")
                    self._load_index()
                else:
                    info_log("[MEM] 創建新FAISS索引")
                    self._create_index()
                    
                info_log("[MEM] 向後相容性設定完成")
            except ImportError as e:
                info_log(f"[MEM] 跳過向後相容性設定（缺少依賴）: {e}")
                # 沒有舊版依賴，但新架構仍可正常工作
                self.model = None
                self.index = None
                self.metadata = []
            
            self.is_initialized = True
            info_log("[MEM] 重構架構初始化完成")
            return True
            
        except Exception as e:
            error_log(f"[MEM] 重構架構初始化失敗: {e}")
            return False
    
    def _initialize_legacy_mode(self) -> bool:
        """初始化舊版模式（向後相容）"""
        try:
            info_log(f"[MEM] 舊版模式初始化中...")
            
            # 嘗試載入嵌入模型（可選）
            try:
                info_log(f"[MEM] 嘗試載入嵌入模型: {self.embedding_model}")
                self.model = SentenceTransformer(self.embedding_model)
                info_log("[MEM] 嵌入模型載入成功")
                
                # 嘗試載入FAISS索引
                if self._faiss_index_exists():
                    info_log("[MEM] 正在載入FAISS索引")
                    self._load_index()
                else:
                    info_log("[MEM] FAISS 索引不存在，正在創建索引")
                    self._create_index()
                    
            except ImportError as e:
                info_log(f"[MEM] 嵌入模型依賴缺失: {e}")
                info_log("[MEM] 將在基本模式下運行（無向量搜索功能）")
                self.model = None
                self.index = None
                self.metadata = []
            except Exception as e:
                info_log(f"[MEM] 嵌入模型載入失敗: {e}")
                info_log("[MEM] 將在基本模式下運行")
                self.model = None
                self.index = None
                self.metadata = []

            self.is_initialized = True
            info_log(f"[MEM] 舊版模式初始化完成")
            return True
            
        except Exception as e:
            error_log(f"[MEM] 舊版模式初始化失敗: {e}")
            # 即使失敗也設為已初始化，以基本模式運行
            self.is_initialized = True
            self.model = None
            self.index = None
            self.metadata = []
            info_log("[MEM] 以最基本模式運行")
            return True


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
                if not self._legacy_mode and self.memory_manager:
                    return self._handle_new_schema(data)
                else:
                    # 新格式但舊模式，轉換處理
                    return self._handle_legacy_from_new_schema(data)
            
            # 處理舊版格式（向後相容）
            if hasattr(data, 'mode'):
                return self._handle_legacy_schema(data)
            
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
    
    def _handle_legacy_from_new_schema(self, data: MEMInput) -> Dict[str, Any]:
        """從新Schema格式轉換到舊版處理"""
        try:
            if data.operation_type == "query" and data.query_data:
                results = self._retrieve_memory(data.query_data.query_text, data.query_data.max_results)
                return {
                    "success": True,
                    "operation_type": "query",
                    "results": results
                }
            else:
                return self._create_error_response("舊版模式下的新Schema轉換暫不支援此操作")
                
        except Exception as e:
            return self._create_error_response(f"Schema轉換失敗: {str(e)}")
    
    def _create_error_response(self, message: str) -> Dict[str, Any]:
        """創建錯誤回應"""
        return {
            "success": False,
            "error": message,
            "status": "failed"
        }
    
    def _handle_legacy_schema(self, payload):
        """處理舊版Schema格式（向後相容）"""
        debug_log(1, f"[MEM] 接收到的舊版資料: {payload}")

        if not hasattr(payload, 'mode'):
            return self._create_error_response("舊版格式缺少mode字段")

        match (payload.mode):
            case "fetch":
                info_log("[MEM] 查詢模式啟用")

                if payload.text is None:
                    info_log("[MEM] 查詢文本為空，請提供有效的文本", "WARNING")
                    return {"error": "請提供查詢文本"}
                results = self._retrieve_memory(payload.text, payload.top_k)

                if not results:
                    info_log("[MEM] 查詢結果為空", "WARNING")
                    return {
                        "results": [],
                        "message": "查無相關記憶",
                        "status": "empty"
                    }

                debug_log_e(1, f"[MEM] 查詢結果: {results['results']}")
                debug_log(2, f"[MEM] 完整查詢結果: {results}")
                return results  # 直接返回字典格式以保持相容性
            case "store":
                info_log("[MEM] 儲存模式啟用")
                if payload.entry is None:
                    info_log("[MEM] 儲存文本為空，請提供有效的文本", "WARNING")
                    return {"error": "請提供儲存文本"}
                try:
                    entry = payload.entry
                    self._add_memory(entry["user"], entry)
                    info_log(f"[MEM] 儲存成功: {entry}")
                except Exception as e:
                    error_log(f"[MEM] 儲存失敗: {e}")
                    return {"error": f"儲存失敗: {e}"}
                return {"status": "stored"}
            case "list_all":
                info_log("[MEM] 列出所有記憶")
                if not self.metadata:
                    info_log("[MEM] 沒有任何記憶可供列出", "WARNING")
                    return {"results": [], "status": "empty"}
                page = payload.page or 1
                page_size = self.config.get("page_size", 10)
                return self._list_all(page, page_size)
            case "clear_all":
                info_log("[MEM] 清除所有記憶")
                return self._clear_all()
            case "clear_by_text":
                info_log("[MEM] 根據文本清除記憶")
                if payload.text is None:
                    info_log("[MEM] 清除文本為空，請提供有效的文本", "WARNING")
                    return {"status": "failed", "error": "請提供清除文本"}
                try:
                    # 限制 top_k 不超過 metadata 的長度
                    top_k = min(payload.top_k or 5, len(self.metadata))
                    result = self._clear_by_text(payload.text, top_k)

                    debug_log(2, f"[MEM] 清除結果: {result}")
                    return result
                except Exception as e:
                    error_log(f"[MEM] 清除失敗，可能是查詢結果並不存在")
                    return {"status": "failed", "error": f"清除失敗: {e}"}
            case _:
                info_log(f"[MEM] 不支援的模式: {payload.mode}", "WARNING")
                return {"error": f"不支援的模式: {payload.mode}"}
    
    # === 新架構支援方法 ===
    
    def process_nlp_output(self, nlp_output) -> Optional[MEMOutput]:
        """處理來自NLP模組的輸出（新架構）"""
        if self._legacy_mode or not self.nlp_integration:
            debug_log(2, "[MEM] 新架構未啟用，跳過NLP輸出處理")
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
            if not self._legacy_mode and self.memory_manager:
                from .schemas import MemoryQuery
                query = MemoryQuery(
                    identity_token=identity_token,
                    query_text=query_text,
                    max_results=5
                )
                results = self.memory_manager.process_memory_query(query)
                if self.nlp_integration:
                    return self.nlp_integration.extract_memory_context_for_llm(results)
            
            # 舊版模式回退
            results = self._retrieve_memory(query_text, 5)
            if results and 'results' in results:
                context_parts = ["=== 相關記憶 ==="]
                for i, item in enumerate(results['results'][:3], 1):
                    context_parts.append(f"{i}. {item.get('user', '無內容')}")
                context_parts.append("=== 記憶結束 ===")
                return "\n".join(context_parts)
            
            return ""
            
        except Exception as e:
            error_log(f"[MEM] 獲取LLM記憶上下文失敗: {e}")
            return ""
    
    # === 舊版方法保持不變（向後相容） ===

    def _embed_text(self, text):
        embedding = self.model.encode(text)
        return embedding.astype(np.float32)

    def _add_memory(self, text, metadata):
        if self.index is None:
            self._create_index()

        embedding = self._embed_text(text)
        self.index.add(np.array([embedding]))
        self.metadata.append(metadata)

        debug_log_e(1, f"[MEM] 新增記憶: {text}")
        debug_log_e(2, f"[MEM] 新增記憶的Metadata: {metadata}")
        debug_log_e(2, f"[MEM] 當前記憶數量: {len(self.metadata)}")
        debug_log_e(2, f"[MEM] 當前索引維度: {self.index.ntotal}")

        faiss.write_index(self.index, self.index_file)
        with open(self.metadata_file, "w") as f:
            json.dump(self.metadata, f)
        debug_log(1, "[MEM] 記憶已儲存到索引和Metadata中")

    def _retrieve_memory(self, query, top_k=5):
        if self.index is None:
            if self._faiss_index_exists():
                self._load_index()
            else:
                error_log("[MEM] FAISS 索引不存在，無法檢索記憶")
                return []

        query_embedding = self._embed_text(query)

        debug_log(1, f"[MEM] 查詢文本: {query}")
        debug_log(2, f"[MEM] 查詢嵌入: {query_embedding}")

        distances, indices = self.index.search(np.array([query_embedding]), top_k)
    
        debug_log(3, f"[MEM] 查詢距離: {distances}")

        results = []

        for i, dist in zip(indices[0], distances[0]):
            if i < len(self.metadata) and dist <= self.max_distance:
                results.append(self.metadata[i])


        debug_log(3, f"[MEM] 檢索到的索引: {indices}")

        if not results:
            return {
                "results": [],
                "message": "查無相關記憶（超出相似度閾值）",
                "status": "empty"
            }

        return {
            "results": results,
            "status": "ok"
        }

    def _list_all(self, page: int = 1, page_size: int = 10) -> dict:
        if not self.metadata:
            info_log("[MEM] 列出所有記憶時發現沒有任何記憶", "WARNING")
            return {"status": "empty", "message": "目前沒有任何記憶"}

        total_records = len(self.metadata)
        total_pages = (total_records + page_size - 1) // page_size  # 計算總頁數

        if page < 1 or page > total_pages:
            info_log(f"[MEM] 無效的頁碼: {page}，總頁數: {total_pages}", "WARNING")
            return {"status": "failed", "message": f"頁碼無效，請輸入 1 到 {total_pages} 之間的頁碼"}

        start_index = (page - 1) * page_size
        end_index = min(start_index + page_size, total_records)

        records = self.metadata[start_index:end_index]

        return {
            "status": "ok",
            "page": page,
            "total_pages": total_pages,
            "records": records,
            "message": f"顯示第 {page} 頁，共 {total_pages} 頁"
        }

    def _clear_all(self):
        self._create_index()  # 重建空的 index
        return {"status": "cleared", "message": "記憶已完全清空"}

    def _clear_by_text(self, text, top_k=5):
        query_embedding = self._embed_text(text)
        _, indices = self.index.search(np.array([query_embedding]), top_k)

        # 刪除 metadata 中對應的資料
        valid_indices = sorted(set(i for i in indices[0] if i < len(self.metadata)), reverse=True)

        if not valid_indices:
            return {"status": "failed", "deleted": 0, "message": "未找到可刪除的記憶"}

        for i in valid_indices:
            del self.metadata[i]

        self._rebuild_index()

        info_log(f"[MEM] 成功清除 {len(valid_indices)} 條記憶")

        return {"status": "cleared", "deleted": len(valid_indices), "message": f"成功刪除 {len(valid_indices)} 筆記憶"}

    def _rebuild_index(self):
        self.index = faiss.IndexFlatL2(self.dimension)
        embeddings = [self._embed_text(entry["user"]) for entry in self.metadata]
        if embeddings:
            self.index.add(np.array(embeddings))
        faiss.write_index(self.index, self.index_file)
        with open(self.metadata_file, "w") as f:
            json.dump(self.metadata, f)

    def _faiss_index_exists(self):
        return os.path.exists(self.index_file)

    def _create_index(self):
        os.makedirs(os.path.dirname(self.index_file), exist_ok=True)
        self.index = faiss.IndexFlatL2(self.dimension)
        self.metadata = []
        faiss.write_index(self.index, self.index_file)
        with open(self.metadata_file, "w") as f:
            json.dump(self.metadata, f)

        debug_log(1, "[MEM] FAISS 索引和Metadata已創建")

    def _load_index(self):
        self.index = faiss.read_index(self.index_file)
        with open(self.metadata_file, "r") as f:
            self.metadata = json.load(f)

        debug_log(1, "[MEM] FAISS 索引和Metadata已載入")

    def shutdown(self):
        info_log("[MEM] 模組關閉")
