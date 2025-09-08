"""
UEP 模組 Schema 適配器系統
提供模組間數據格式的標準化和轉換功能，支持漸進式重構
"""

from typing import Dict, Any, Optional, Type, Union
from pydantic import BaseModel, Field
from abc import ABC, abstractmethod
import inspect

class UEPBaseInput(BaseModel):
    """UEP 統一輸入基類"""
    # 通用欄位
    request_id: Optional[str] = Field(None, description="請求標識符")
    context: Optional[Dict[str, Any]] = Field(None, description="上下文信息")
    metadata: Optional[Dict[str, Any]] = Field(None, description="元數據")
    
    class Config:
        extra = "allow"  # 允許額外欄位以支持向後兼容

class UEPBaseOutput(BaseModel):
    """UEP 統一輸出基類"""
    # 通用欄位
    status: str = Field(..., description="處理狀態")
    message: Optional[str] = Field(None, description="狀態訊息")
    error: Optional[str] = Field(None, description="錯誤訊息")
    request_id: Optional[str] = Field(None, description="對應的請求標識符")
    processing_time: Optional[float] = Field(None, description="處理時間(秒)")
    metadata: Optional[Dict[str, Any]] = Field(None, description="元數據")
    
    class Config:
        extra = "allow"  # 允許額外欄位以支持模組特定數據

class SchemaAdapter(ABC):
    """Schema 適配器抽象基類"""
    
    @abstractmethod
    def adapt_input(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """適配輸入數據到模組期望的格式"""
        pass
    
    @abstractmethod
    def adapt_output(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """適配輸出數據到統一格式"""
        pass

class NLPSchemaAdapter(SchemaAdapter):
    """NLP 模組 Schema 適配器"""
    
    def adapt_input(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """將統一格式轉換為 NLP 模組格式"""
        # 提取 NLP 需要的核心數據
        adapted = {
            "text": data.get("text", data.get("content", ""))
        }
        
        # 保留原始數據作為 context
        if data.get("context"):
            adapted.update(data["context"])
            
        return adapted
    
    def adapt_output(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """將 NLP 模組輸出轉換為統一格式"""
        return {
            "status": "success" if data.get("intent") else "error",
            "message": "NLP 處理完成",
            "data": {
                "text": data.get("text", ""),
                "intent": data.get("intent", ""),
                "label": data.get("label", ""),
                "confidence": data.get("confidence", 0.0)
            },
            "module": "nlp"
        }

class MEMSchemaAdapter(SchemaAdapter):
    """MEM 模組 Schema 適配器 - 支援新舊架構"""
    
    def adapt_input(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """將統一格式轉換為 MEM 模組格式"""
        # 檢查是否為新架構格式
        if self._is_new_architecture_input(data):
            return self._adapt_new_architecture_input(data)
        
        # 舊版格式適配
        return self._adapt_legacy_input(data)
    
    def _is_new_architecture_input(self, data: Dict[str, Any]) -> bool:
        """檢查是否為新架構輸入格式"""
        new_arch_indicators = [
            "operation_type", "identity_token", "memory_type", 
            "query_data", "llm_instructions", "conversation_text"
        ]
        return any(key in data for key in new_arch_indicators)
    
    def _adapt_new_architecture_input(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """適配新架構輸入格式"""
        adapted = {
            "operation_type": data.get("operation_type", "query"),
            "identity_token": data.get("identity_token", "anonymous"),
            "content": data.get("content", data.get("text", "")),
            "memory_type": data.get("memory_type", "general"),
            "metadata": data.get("metadata", {}),
            "timestamp": data.get("timestamp"),
            "max_results": data.get("max_results", 10),
            "query_text": data.get("query_text", data.get("text", ""))
        }
        
        # 如果有查詢資料，建立查詢結構
        if data.get("query_data") or adapted["operation_type"] == "query":
            adapted["query_data"] = {
                "identity_token": adapted["identity_token"],
                "query_text": adapted["query_text"],
                "max_results": adapted["max_results"],
                "similarity_threshold": data.get("similarity_threshold", 0.7)
            }
        
        return adapted
    
    def _adapt_legacy_input(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """適配舊版輸入格式"""
        # 根據數據內容推斷操作模式
        if data.get("entry") or (data.get("user") and data.get("response")):
            # 存儲模式
            adapted = {
                "mode": "store",
                "entry": data.get("entry", {
                    "user": data.get("user", data.get("text", "")),
                    "response": data.get("response", "")
                })
            }
        else:
            # 查詢模式
            adapted = {
                "mode": "fetch",
                "text": data.get("text", data.get("query", "")),
                "top_k": data.get("top_k", 3),
                "page": data.get("page", 1)
            }
            
        return adapted
    
    def adapt_output(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """將 MEM 模組輸出轉換為統一格式"""
        # 檢查是否為新架構輸出
        if self._is_new_architecture_output(data):
            return self._adapt_new_architecture_output(data)
        
        # 舊版格式適配
        return self._adapt_legacy_output(data)
    
    def _is_new_architecture_output(self, data: Dict[str, Any]) -> bool:
        """檢查是否為新架構輸出格式"""
        new_arch_indicators = [
            "search_results", "operation_results", "memory_context",
            "active_snapshots", "memory_usage"
        ]
        return any(key in data for key in new_arch_indicators)
    
    def _adapt_new_architecture_output(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """適配新架構輸出格式"""
        status = "success" if data.get("success", False) else "error"
        
        # 提取結果數據
        results_data = {}
        if data.get("search_results"):
            results_data["search_results"] = [
                {
                    "memory_id": result.get("memory_entry", {}).get("memory_id"),
                    "content": result.get("memory_entry", {}).get("content"),
                    "similarity_score": result.get("similarity_score"),
                    "memory_type": result.get("memory_entry", {}).get("memory_type")
                }
                for result in data["search_results"]
            ]
        
        if data.get("operation_results"):
            results_data["operation_results"] = [
                {
                    "operation_type": result.get("operation_type"),
                    "success": result.get("success"),
                    "message": result.get("message"),
                    "affected_count": result.get("affected_count")
                }
                for result in data["operation_results"]
            ]
        
        return {
            "status": status,
            "message": data.get("operation_type", "MEM處理完成"),
            "error": data.get("errors", [None])[0] if data.get("errors") else None,
            "data": {
                **results_data,
                "memory_context": data.get("memory_context"),
                "total_memories": data.get("total_memories", 0),
                "memory_usage": data.get("memory_usage", {}),
                "active_snapshots_count": len(data.get("active_snapshots", []))
            },
            "module": "mem",
            "architecture": "new"
        }
    
    def _adapt_legacy_output(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """適配舊版輸出格式"""
        status = data.get("status", "unknown")
        if status in ["empty", "ok", "stored", "cleared"]:
            status = "success"
        elif status == "failed":
            status = "error"
            
        return {
            "status": status,
            "message": data.get("message", "MEM 處理完成"),
            "error": data.get("error"),
            "data": {
                "results": data.get("results", []),
                "records": data.get("records"),
                "page": data.get("page"),
                "total_pages": data.get("total_pages"),
                "total_records": data.get("total_records"),
                "deleted": data.get("deleted")
            },
            "module": "mem",
            "architecture": "legacy"
        }

class LLMSchemaAdapter(SchemaAdapter):
    """LLM 模組 Schema 適配器"""
    
    def adapt_input(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """將統一格式轉換為 LLM 模組格式"""
        adapted = {
            "text": data.get("text", ""),
            "intent": data.get("intent", "chat"),
            "memory": data.get("memory", data.get("context")),
            "is_internal": data.get("is_internal", False)
        }
        
        return adapted
    
    def adapt_output(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """將 LLM 模組輸出轉換為統一格式"""
        return {
            "status": "success" if data.get("text") else "error",
            "message": "LLM 處理完成",
            "data": {
                "text": data.get("text", ""),
                "mood": data.get("mood", data.get("emotion", "neutral")),
                "sys_action": data.get("sys_action"),
                "confidence": data.get("confidence", 0.0)
            },
            "module": "llm"
        }

class TTSSchemaAdapter(SchemaAdapter):
    """TTS 模組 Schema 適配器"""
    
    def adapt_input(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """將統一格式轉換為 TTS 模組格式"""
        adapted = {
            "text": data.get("text", ""),
            "mood": data.get("mood", data.get("emotion")),
            "save": data.get("save", False),
            "force_chunking": data.get("force_chunking", False)
        }
        
        return adapted
    
    def adapt_output(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """將 TTS 模組輸出轉換為統一格式"""
        return {
            "status": data.get("status", "unknown"),
            "message": data.get("message", "TTS 處理完成"),
            "data": {
                "output_path": data.get("output_path"),
                "is_chunked": data.get("is_chunked", False),
                "chunk_count": data.get("chunk_count"),
                "duration": data.get("duration")
            },
            "module": "tts"
        }

class STTSchemaAdapter(SchemaAdapter):
    """STT 模組 Schema 適配器"""
    
    def adapt_input(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """將統一格式轉換為 STT 模組格式"""
        # 默認使用持續監聽模式
        mode = data.get("mode", "continuous")
        
        adapted = {
            "mode": mode,
            "language": data.get("language", "en-US"),
            "enable_speaker_id": data.get("enable_speaker_id", True),
            "duration": data.get("duration", 30),  # 增加默認持續監聽時間
            "context": data.get("context", "general")
        }
        
        return adapted
    
    def adapt_output(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """將 STT 模組輸出轉換為統一格式"""
        has_text = data.get("text") and data.get("text").strip()
        has_error = data.get("error") is not None
        
        return {
            "status": "success" if has_text and not has_error else "error",
            "message": "STT 處理完成" if has_text else "STT 未識別語音",
            "error": data.get("error"),
            "data": {
                "text": data.get("text", ""),
                "confidence": data.get("confidence", 0.0),
                "speaker_info": data.get("speaker_info"),
                "should_activate": data.get("should_activate", False),
                "activation_reason": data.get("activation_reason")
            },
            "module": "stt"
        }

class SchemaAdapterRegistry:
    """Schema 適配器註冊表"""
    
    def __init__(self):
        self.adapters: Dict[str, SchemaAdapter] = {}
        self._register_default_adapters()
    
    def _register_default_adapters(self):
        """註冊預設適配器"""
        self.adapters["nlp"] = NLPSchemaAdapter()
        self.adapters["mem"] = MEMSchemaAdapter()
        self.adapters["llm"] = LLMSchemaAdapter()
        self.adapters["tts"] = TTSSchemaAdapter()
        self.adapters["stt"] = STTSchemaAdapter()
    
    def register_adapter(self, module_name: str, adapter: SchemaAdapter):
        """註冊自定義適配器"""
        self.adapters[module_name] = adapter
    
    def get_adapter(self, module_name: str) -> Optional[SchemaAdapter]:
        """獲取模組適配器"""
        return self.adapters.get(module_name)
    
    def adapt_input(self, module_name: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """適配輸入數據"""
        adapter = self.get_adapter(module_name)
        if adapter:
            return adapter.adapt_input(data)
        return data
    
    def adapt_output(self, module_name: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """適配輸出數據"""
        adapter = self.get_adapter(module_name)
        if adapter:
            return adapter.adapt_output(data)
        return data

class UnifiedSchemaHandler:
    """統一 Schema 處理器"""
    
    def __init__(self):
        self.adapter_registry = SchemaAdapterRegistry()
    
    def wrap_module_call(self, module_name: str, module_handle, input_data: Dict[str, Any]):
        """包裝模組調用以進行 Schema 適配"""
        try:
            # 輸入適配
            adapted_input = self.adapter_registry.adapt_input(module_name, input_data)
            
            # 調用模組
            if inspect.iscoroutinefunction(module_handle):
                import asyncio
                result = asyncio.run(module_handle(adapted_input))
            else:
                result = module_handle(adapted_input)
            
            # 輸出適配
            if isinstance(result, dict):
                adapted_output = self.adapter_registry.adapt_output(module_name, result)
                return adapted_output
            else:
                return {
                    "status": "success",
                    "message": f"{module_name} 處理完成",
                    "data": result,
                    "module": module_name
                }
                
        except Exception as e:
            return {
                "status": "error",
                "message": f"{module_name} 處理失敗",
                "error": str(e),
                "module": module_name
            }
    
    def register_custom_adapter(self, module_name: str, adapter: SchemaAdapter):
        """註冊自定義適配器"""
        self.adapter_registry.register_adapter(module_name, adapter)

# 全局實例
schema_handler = UnifiedSchemaHandler()

# 便捷函數
def adapt_module_call(module_name: str, module_handle, input_data: Dict[str, Any]):
    """便捷的模組調用適配函數"""
    return schema_handler.wrap_module_call(module_name, module_handle, input_data)
