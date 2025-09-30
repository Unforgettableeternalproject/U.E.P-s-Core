"""
數據轉換器系統 - 模組間數據格式轉換

這個系統負責在不同模組之間轉換數據格式，確保模組間的無縫通信。
結合 Schema 適配器提供更智能的數據轉換能力。
"""

from typing import Dict, Any, Optional, Callable
from utils.debug_helper import debug_log, error_log


class DataTransformer:
    """數據轉換器基類"""
    
    def __init__(self, use_schema_adapter=True):
        self.transformers = {}
        self.use_schema_adapter = use_schema_adapter
        
        # 導入 Schema 適配器（如果啟用）
        if self.use_schema_adapter:
            try:
                from core.schema_adapter import schema_handler
                self.schema_handler = schema_handler
                debug_log(1, f"[DataTransformer] Schema 適配器已啟用")
            except ImportError:
                debug_log(1, f"[DataTransformer] Schema 適配器不可用，使用傳統轉換器")
                self.use_schema_adapter = False
                self.schema_handler = None
        else:
            self.schema_handler = None
    
    def register_transformer(self, 
                           from_module: str, 
                           to_module: str, 
                           transformer_func: Callable[[Dict[str, Any]], Dict[str, Any]]):
        """註冊數據轉換器"""
        key = f"{from_module}->{to_module}"
        self.transformers[key] = transformer_func
        debug_log(1, f"[DataTransformer] 註冊轉換器: {key}")
    
    def transform(self, 
                  from_module: str, 
                  to_module: str, 
                  data: Dict[str, Any]) -> Dict[str, Any]:
        """執行數據轉換"""
        key = f"{from_module}->{to_module}"
        
        # 優先使用 Schema 適配器進行智能轉換
        if self.use_schema_adapter and self.schema_handler:
            try:
                # 使用 Schema 適配器進行標準化輸出
                standardized_output = self.schema_handler.adapter_registry.adapt_output(from_module, data)
                # 再轉換為目標模組的輸入格式
                adapted_input = self.schema_handler.adapter_registry.adapt_input(to_module, standardized_output.get('data', standardized_output))
                
                debug_log(2, f"[DataTransformer] Schema 適配轉換完成 {key}")
                return adapted_input
            except Exception as e:
                debug_log(1, f"[DataTransformer] Schema 適配失敗，回退到傳統轉換器: {e}")
        
        # 回退到傳統轉換器
        if key in self.transformers:
            try:
                transformed_data = self.transformers[key](data)
                debug_log(2, f"[DataTransformer] 轉換完成 {key}: {data} -> {transformed_data}")
                return transformed_data
            except Exception as e:
                error_log(f"[DataTransformer] 轉換失敗 {key}: {e}")
                return data
        else:
            debug_log(2, f"[DataTransformer] 未找到轉換器 {key}，使用原始數據")
            return data
    
    def has_transformer(self, from_module: str, to_module: str) -> bool:
        """檢查是否存在轉換器"""
        key = f"{from_module}->{to_module}"
        return key in self.transformers


# 全局數據轉換器實例
data_transformer = DataTransformer()


# ==================== 具體轉換器實現 ====================

def nlp_to_mem_transformer(data: Dict[str, Any]) -> Dict[str, Any]:
    """NLP 到 MEM 的數據轉換器"""
    
    # 原始 NLP 輸出格式：{'text': '...', 'intent': '...', 'label': '...'}
    text = data.get("text", "")
    intent = data.get("intent", "")
    
    # 根據意圖決定 MEM 操作模式
    if intent in ["chat", "question", "inquiry"]:
        # 聊天類意圖：先獲取相關記憶
        return {
            "mode": "fetch",
            "text": text,
            "top_k": 3  # 獲取最相關的3條記憶
        }
    elif intent in ["command", "instruction"]:
        # 指令類意圖：也獲取相關記憶以提供上下文
        return {
            "mode": "fetch", 
            "text": text,
            "top_k": 2  # 指令相關的記憶較少
        }
    else:
        # 默認：獲取記憶
        return {
            "mode": "fetch",
            "text": text,
            "top_k": 3
        }


def mem_to_llm_transformer(data: Dict[str, Any]) -> Dict[str, Any]:
    """MEM 到 LLM 的數據轉換器"""
    
    # MEM 輸出格式：{'status': '...', 'results': [...], ...}
    status = data.get("status", "")
    results = data.get("results", [])
    original_input = data.get("original_input", "")
    intent = data.get("intent", "chat")
    
    # 構建記憶上下文
    memory_context = ""
    if status == "empty" or not results:
        memory_context = "No relevant memory found."
    else:
        memory_entries = []
        for result in results[:3]:  # 最多使用3條記憶
            user_msg = result.get("user", "")
            response_msg = result.get("response", "")
            if user_msg and response_msg:
                memory_entries.append(f"User: {user_msg}\nAssistant: {response_msg}")
        
        if memory_entries:
            memory_context = "Relevant memory:\n" + "\n\n".join(memory_entries)
        else:
            memory_context = "No relevant memory found."
    
    # 返回 LLM 期望的格式
    return {
        "text": original_input,  # 確保包含原始文字
        "intent": intent,
        "memory": memory_context
    }


def nlp_to_llm_transformer(data: Dict[str, Any]) -> Dict[str, Any]:
    """NLP 到 LLM 的數據轉換器（當跳過 MEM 時使用）"""
    
    # NLP 輸出：{'text': '...', 'intent': '...', 'label': '...'}
    text = data.get("text", "")
    intent = data.get("intent", "")
    
    return {
        "text": text,
        "intent": intent,
        "memory": "No relevant memory found."  # 沒有記憶時的默認值
    }


def llm_to_tts_transformer(data: Dict[str, Any]) -> Dict[str, Any]:
    """LLM 到 TTS 的數據轉換器"""
    
    # LLM 輸出格式可能包含：{'text': '...', 'mood': '...', ...}
    text = data.get("text", "")
    mood = data.get("mood", "neutral")
    
    return {
        "text": text,
        "mood": mood,
        "save": False  # 測試時不保存音檔
    }


def llm_to_sys_transformer(data: Dict[str, Any]) -> Dict[str, Any]:
    """LLM 到 SYS 的數據轉換器（用於指令處理）"""
    
    # LLM 輸出可能包含系統動作
    sys_action = data.get("sys_action", {})
    text = data.get("text", "")
    
    if isinstance(sys_action, dict) and sys_action:
        # 如果有系統動作，轉換為 SYS 模組格式
        action = sys_action.get("action", "")
        params = sys_action.get("params", {})
        
        if action:
            return {
                "mode": action,
                "params": params
            }
    
    # 如果沒有明確的系統動作，使用文字作為指令
    return {
        "mode": "quick_phrases",  # 或其他合適的默認模式
        "params": {"text": text}
    }


def chat_session_to_mem_store_transformer(data: Dict[str, Any]) -> Dict[str, Any]:
    """聊天會話結束時儲存到 MEM 的轉換器"""
    
    user_text = data.get("user_input", "")
    assistant_response = data.get("assistant_response", "")
    
    return {
        "mode": "store",
        "entry": {
            "user": user_text,
            "response": assistant_response
        }
    }


# ==================== 註冊所有轉換器 ====================

def register_all_transformers():
    """註冊所有預定義的數據轉換器"""
    
    # NLP 相關轉換器
    data_transformer.register_transformer("nlp", "mem", nlp_to_mem_transformer)
    data_transformer.register_transformer("nlp", "llm", nlp_to_llm_transformer)
    
    # MEM 相關轉換器  
    data_transformer.register_transformer("mem", "llm", mem_to_llm_transformer)
    
    # LLM 相關轉換器
    data_transformer.register_transformer("llm", "tts", llm_to_tts_transformer)
    data_transformer.register_transformer("llm", "sys", llm_to_sys_transformer)
    
    # 會話儲存轉換器
    data_transformer.register_transformer("chat_session", "mem", chat_session_to_mem_store_transformer)
    
    debug_log(1, "[DataTransformer] 所有預定義轉換器已註冊")


# ==================== 智能轉換器 ====================

def smart_transform(from_module: str, to_module: str, data: Dict[str, Any], 
                   context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """智能數據轉換 - 根據上下文選擇最佳轉換策略"""
    
    # 首先嘗試使用註冊的轉換器
    if data_transformer.has_transformer(from_module, to_module):
        return data_transformer.transform(from_module, to_module, data)
    
    # 如果沒有專用轉換器，嘗試智能推斷
    debug_log(2, f"[DataTransformer] 使用智能轉換: {from_module} -> {to_module}")
    
    # 智能轉換規則
    if to_module == "mem" and "text" in data:
        # 任何包含 text 的數據都可以轉換為 MEM 查詢
        return {
            "mode": "fetch",
            "text": data["text"], 
            "top_k": 3
        }
    elif to_module == "llm" and "text" in data:
        # 任何包含 text 的數據都可以轉換為 LLM 輸入
        return {
            "text": data["text"],
            "intent": data.get("intent", "chat"),
            "memory": data.get("memory", "No relevant memory found.")
        }
    elif to_module == "tts" and "text" in data:
        # 任何包含 text 的數據都可以轉換為 TTS 輸入
        return {
            "text": data["text"],
            "mood": data.get("mood", "neutral"),
            "save": False
        }
    else:
        # 無法轉換時返回原始數據
        debug_log(2, f"[DataTransformer] 無法智能轉換 {from_module} -> {to_module}，返回原始數據")
        return data


# 在模組載入時自動註冊轉換器
register_all_transformers()
