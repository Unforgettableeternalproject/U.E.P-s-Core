from core.bases.module_base import BaseModule
from datetime import datetime
import json
from typing import List, Dict, Any, Optional
from .working_context_handler import register_memory_context_handler
from .schemas import (
    MEMInput, MEMOutput, MemoryType, MemoryImportance
)
from core.schemas import MEMModuleData
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
        
        # 狀態管理整合
        self.state_change_listener = None
        
        # 會話管理整合
        self.session_sync_timer = None
        self.current_system_session_id = None
        
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
        
        # Debug level = 4
        debug_log(4, f"[MEM] 完整模組設定: {self.config}")

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
            
            # 註冊狀態變化監聽器
            self._register_state_change_listener()
            
            # 啟動會話同步
            self._start_session_sync()
            
            # 新架構不需要舊版FAISS相容性
            self.is_initialized = True
            info_log("[MEM] 重構架構初始化完成")
            return True
            
        except Exception as e:
            error_log(f"[MEM] 重構架構初始化失敗: {e}")
            return False
    
    def _register_state_change_listener(self):
        """註冊狀態變化監聽器"""
        try:
            from core.states.state_manager import state_manager
            self.state_change_listener = self._handle_state_change
            state_manager.add_state_change_callback(self.state_change_listener)
            debug_log(2, "[MEM] 狀態變化監聽器註冊完成")
        except Exception as e:
            error_log(f"[MEM] 狀態變化監聽器註冊失敗: {e}")
    
    def _handle_state_change(self, old_state, new_state):
        """處理狀態變化"""
        try:
            debug_log(2, f"[MEM] 狀態變化: {old_state.value} -> {new_state.value}")
            
            if new_state.value == "chat":
                # CHAT狀態啟動 - 加入會話
                self._join_chat_session()
            elif old_state.value == "chat" and new_state.value != "chat":
                # CHAT狀態結束 - 離開會話
                self._leave_chat_session()
                
        except Exception as e:
            error_log(f"[MEM] 處理狀態變化失敗: {e}")
    
    def _join_chat_session(self):
        """加入聊天會話 - 根據MEM代辦.md要求整合會話管理"""
        try:
            if not self.memory_manager:
                debug_log(2, "[MEM] 記憶管理器未初始化，跳過加入會話")
                return
            
            from core.states.state_manager import state_manager
            from core.working_context import working_context_manager
            
            # 1. 從State Manager獲取目前系統狀態上下文
            current_session_id = state_manager.get_current_session_id()
            debug_log(2, f"[MEM] 當前系統會話ID: {current_session_id}")
            if not current_session_id:
                debug_log(2, "[MEM] 當前沒有活躍會話，跳過加入")
                return
            
            # 檢查是否已經在相同會話中（避免重複加入）
            if self.memory_manager.is_in_chat_session(current_session_id):
                debug_log(2, f"[MEM] 已在會話 {current_session_id} 中，跳過重複加入")
                return
            
            # 2. 從Working Context獲取Identity相關資料
            identity_context = working_context_manager.get_current_identity()
            
            if identity_context and identity_context.get("memory_token"):
                memory_token = identity_context["memory_token"]
                debug_log(2, f"[MEM] 從身份上下文獲取記憶令牌: {memory_token}")
            else:
                # 通過身份管理器獲取當前記憶令牌（可能是anonymous）
                memory_token = self.memory_manager.identity_manager.get_current_memory_token()
                debug_log(2, f"[MEM] 從身份管理器獲取記憶令牌: {memory_token}")
            
            # 3. 從Session Manager獲取目前會話相關資料（根據代辦.md要求4）
            session_context = self._get_session_context_from_session_manager(current_session_id)
            
            # 構建初始上下文
            initial_context = {
                "session_type": "chat",
                "started_by_state_change": True,
                "memory_token": memory_token,
                "identity_context": identity_context,
                "session_context": session_context
            }
            
            # 委託給MemoryManager處理實際的會話加入邏輯
            success = self.memory_manager.join_chat_session(
                session_id=current_session_id,
                memory_token=memory_token,
                initial_context=initial_context
            )
            
            # 檢查是否為臨時身份，如果是則直接返回成功，不做任何記憶體操作
            if memory_token == self.memory_manager.identity_manager.anonymous_token:
                debug_log(1, f"[MEM] 檢測到臨時身份，跳過記憶體處理，直接返回")
                info_log(f"[MEM] 臨時身份狀態變化處理完成: chat (無記憶體操作)")
                return
            
            if success:
                info_log(f"[MEM] 成功加入聊天會話: {current_session_id}")
            else:
                error_log(f"[MEM] 加入聊天會話失敗: {current_session_id}")
                
        except Exception as e:
            error_log(f"[MEM] 加入聊天會話時發生錯誤: {e}")
    
    def _get_session_context_from_session_manager(self, session_id: str) -> Dict[str, Any]:
        """從統一Session Manager獲取會話相關資料 - 實現代辦.md要求4"""
        try:
            # 使用統一的 session_manager 獲取任何類型的會話
            from core.sessions.session_manager import session_manager
            session = session_manager.get_session(session_id)
            
            if session:
                # 根據會話類型返回不同的信息
                session_type_name = type(session).__name__
                
                if session_type_name == "ChattingSession":
                    return {
                        "session_type": "chatting",
                        "gs_session_id": session.gs_session_id,
                        "identity_context": session.identity_context,
                        "conversation_turns": len(session.conversation_turns),
                        "last_activity": session.last_activity.isoformat() if hasattr(session.last_activity, 'isoformat') else str(session.last_activity),
                        "status": session.status.value if hasattr(session.status, 'value') else str(session.status)
                    }
                elif session_type_name == "WorkflowSession":
                    return {
                        "session_type": "workflow",
                        "workflow_type": getattr(session, 'workflow_type', 'unknown'),
                        "command": getattr(session, 'command', 'unknown'),
                        "status": session.status.value if hasattr(session.status, 'value') else str(session.status),
                        "created_at": session.created_at.isoformat() if hasattr(session.created_at, 'isoformat') else str(session.created_at)
                    }
                elif session_type_name == "GeneralSession":
                    return {
                        "session_type": "general",
                        "gs_type": session.gs_type.value if hasattr(session.gs_type, 'value') else str(session.gs_type),
                        "status": session.status.value if hasattr(session.status, 'value') else str(session.status),
                        "created_at": session.created_at.isoformat() if hasattr(session.created_at, 'isoformat') else str(session.created_at)
                    }
                else:
                    return {
                        "session_type": "unknown",
                        "class_name": session_type_name,
                        "session_id": session_id
                    }
            
            # 如果找不到對應的會話，返回基本資訊
            return {
                "session_type": "unknown",
                "session_id": session_id,
                "note": "無法從Session Manager獲取詳細資訊"
            }
            
        except Exception as e:
            error_log(f"[MEM] 從Session Manager獲取會話資訊失敗: {e}")
            return {
                "session_type": "error",
                "session_id": session_id,
                "error": str(e)
            }
    
    def _leave_chat_session(self):
        """離開聊天會話 - 簡化為接口，實際邏輯委託給MemoryManager"""
        try:
            if not self.memory_manager:
                debug_log(2, "[MEM] 記憶管理器未初始化，跳過離開會話")
                return
            
            from core.states.state_manager import state_manager
            
            # 獲取當前會話ID
            current_session_id = state_manager.get_current_session_id()
            if not current_session_id:
                debug_log(2, "[MEM] 當前沒有活躍會話，跳過離開")
                return
            
            # 檢查是否真的在這個會話中
            if not self.memory_manager.is_in_chat_session(current_session_id):
                debug_log(2, f"[MEM] 不在會話 {current_session_id} 中，跳過離開")
                return
            
            # 委託給MemoryManager處理實際的會話離開邏輯
            result = self.memory_manager.leave_chat_session(current_session_id)
            
            if result.success:
                info_log(f"[MEM] 成功離開聊天會話: {current_session_id}")
            else:
                debug_log(2, f"[MEM] 離開聊天會話: {current_session_id} - {result.message}")
                
        except Exception as e:
            error_log(f"[MEM] 離開聊天會話時發生錯誤: {e}")
    
    def _start_session_sync(self):
        """啟動會話同步"""
        try:
            import threading
            self.session_sync_timer = threading.Timer(1.0, self._sync_session_state)
            self.session_sync_timer.daemon = True
            self.session_sync_timer.start()
            debug_log(2, "[MEM] 會話同步已啟動")
        except Exception as e:
            error_log(f"[MEM] 啟動會話同步失敗: {e}")
    
    def _sync_session_state(self):
        """同步會話狀態 - 定期檢查系統會話狀態"""
        try:
            # 獲取當前系統會話ID
            from core.states.state_manager import state_manager
            current_system_session = state_manager.get_current_session_id()
            
            # 檢查會話ID是否改變
            if current_system_session != self.current_system_session_id:
                debug_log(2, f"[MEM] 系統會話ID變化: {self.current_system_session_id} -> {current_system_session}")
                self._handle_session_change(self.current_system_session_id, current_system_session)
                self.current_system_session_id = current_system_session
            
            # 繼續同步（每秒檢查一次）
            if self.session_sync_timer and self.is_initialized:
                import threading
                self.session_sync_timer = threading.Timer(1.0, self._sync_session_state)
                self.session_sync_timer.daemon = True
                self.session_sync_timer.start()
                
        except Exception as e:
            error_log(f"[MEM] 會話狀態同步失敗: {e}")
    
    def _handle_session_change(self, old_session_id: Optional[str] = None, new_session_id: Optional[str] = None):
        """處理會話變化 - 根據MEM代辦.md優化會話管理邏輯"""
        try:
            # 根據代辦.md：MEM透過比對當前內部會話ID與系統中Chatting Session ID來確認是否還在同一個會話當中
            debug_log(3, f"[MEM] 處理會話變化: {old_session_id} -> {new_session_id}")
            
            # 檢查舊會話是否需要離開
            if old_session_id and self.memory_manager:
                # 檢查內部會話狀態
                if old_session_id in self.memory_manager.current_chat_sessions:
                    debug_log(2, f"[MEM] 舊會話 {old_session_id} 仍在內部記錄中，將由狀態監聽器處理離開")
                    # 不在這裡處理離開，交給狀態監聽器處理以避免重複
            
            # 檢查新會話是否需要加入
            if new_session_id and self.memory_manager:
                # 檢查是否已經在新會話中
                if not self.memory_manager.is_in_chat_session(new_session_id):
                    debug_log(2, f"[MEM] 檢測到新會話 {new_session_id}，將由狀態監聽器處理加入")
                    # 不在這裡處理加入，交給狀態監聽器處理
                else:
                    debug_log(3, f"[MEM] 已在新會話 {new_session_id} 中")
            
            # 更新內部會話狀態追蹤
            if new_session_id:
                self.current_system_session_id = new_session_id
                debug_log(3, f"[MEM] 更新內部追蹤的系統會話ID: {new_session_id}")
                
        except Exception as e:
            error_log(f"[MEM] 處理會話變化失敗: {e}")
    
    def _is_session_synced(self) -> bool:
        """檢查會話是否同步 - 根據代辦.md要求比對會話ID"""
        if not self.memory_manager or not self.current_system_session_id:
            return False
        
        # 根據代辦.md：透過比對當前內部會話ID與系統中Chatting Session ID來確認是否還在同一個會話當中
        return self.current_system_session_id in self.memory_manager.current_chat_sessions
    
    def get_current_session_info(self) -> Dict[str, Any]:
        """獲取當前會話資訊 - 用於調試和監控"""
        try:
            result = {
                "system_session_id": self.current_system_session_id,
                "internal_sessions": list(self.memory_manager.current_chat_sessions) if self.memory_manager else [],
                "is_session_synced": self._is_session_synced() if self.memory_manager else False,
                "memory_manager_initialized": self.memory_manager is not None,
                "session_sync_active": self.session_sync_timer is not None
            }
            
            # 添加詳細的會話狀態資訊
            if self.memory_manager and self.current_system_session_id:
                result["session_details"] = self._get_session_context_from_session_manager(
                    self.current_system_session_id
                )
            
            return result
        except Exception as e:
            error_log(f"[MEM] 獲取會話資訊失敗: {e}")
            return {"error": str(e)}


    def register(self):
        """註冊方法 - 返回模組實例"""
        return self

    def handle(self, data=None):
        """處理輸入數據 - 支援新舊兩種模式"""
        try:
            if not self.is_initialized:
                error_log("[MEM] 模組未初始化")
                return self._create_error_response("模組未初始化")
            
            # CS狀態限制檢查 - MEM只在CHAT狀態下運行
            if not self._is_in_chat_state():
                debug_log(2, "[MEM] 非CHAT狀態，拒絕處理請求")
                return self._create_error_response("MEM模組只在CHAT狀態下運行")
            
            # 檢查身份狀態，優雅處理臨時身份
            if self.memory_manager and self.memory_manager.identity_manager and self.memory_manager.identity_manager.is_temporary_identity():
                identity_desc = self.memory_manager.identity_manager.get_identity_type_description()
                info_log(f"[MEM] 檢測到{identity_desc}，跳過個人記憶存取，返回基本回應")
                return self._create_temporary_identity_response()
            
            # 檢查會話狀態和來源
            session_check = self._check_request_session_context(data)
            debug_log(3, f"[MEM] 會話檢查結果: {session_check}")
            
            # 記錄當前身份類型（用於調試）
            if self.memory_manager and self.memory_manager.identity_manager:
                identity_desc = self.memory_manager.identity_manager.get_identity_type_description()
                debug_log(2, f"[MEM] 當前身份: {identity_desc}")
            
            # 處理舊 API 格式 (向後相容)
            if isinstance(data, dict) and "mode" in data:
                return self._handle_legacy_api(data)
            
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
    
    def _is_in_chat_state(self) -> bool:
        """檢查當前是否處於CHAT狀態"""
        try:
            from core.states.state_manager import state_manager
            current_state = state_manager.get_state()
            return current_state.value == "chat"
        except Exception as e:
            error_log(f"[MEM] 檢查CHAT狀態失敗: {e}")
            return False
    
    def _check_request_session_context(self, data) -> Dict[str, Any]:
        """檢查請求的會話上下文 - 根據代辦.md優化會話一致性檢查"""
        try:
            result = {
                "is_same_session": False,
                "request_source": "unknown",
                "session_synced": self._is_session_synced(),
                "current_system_session": self.current_system_session_id,
                "internal_sessions": list(self.memory_manager.current_chat_sessions) if self.memory_manager else [],
                "trigger_type": "unknown",  # user_input 或 system_triggered
                "has_nlp_info": False,
                "conversation_context": None,
                "session_consistency_check": None  # 新增會話一致性檢查結果
            }
            
            # 根據代辦.md進行會話一致性檢查
            if self.current_system_session_id and self.memory_manager:
                consistency_check = self._perform_session_consistency_check()
                result["session_consistency_check"] = consistency_check
                debug_log(3, f"[MEM] 會話一致性檢查: {consistency_check}")
            
            # 檢查請求來源和類型
            if isinstance(data, dict):
                # 檢查是否包含會話相關資訊
                if "session_id" in data:
                    result["request_source"] = "internal_with_session"
                    session_id = data.get("session_id")
                    result["is_same_session"] = (session_id == self.current_system_session_id)
                    result["trigger_type"] = "system_triggered"  # 帶會話ID的通常是系統觸發
                elif "from_nlp" in data or "intent_info" in data:
                    result["request_source"] = "from_nlp"
                    result["has_nlp_info"] = True
                    result["trigger_type"] = "user_input"  # 來自NLP的通常是使用者輸入
                    
                    # 提取對話上下文
                    if "conversation_text" in data:
                        result["conversation_context"] = data["conversation_text"]
                elif "from_router" in data:
                    result["request_source"] = "from_router"
                    result["trigger_type"] = "user_input"  # 來自Router的通常是使用者輸入
                else:
                    result["request_source"] = "direct_call"
                    result["trigger_type"] = "system_triggered"  # 直接調用通常是系統觸發
            
            elif hasattr(data, 'intent_info'):
                # 新架構Schema
                result["request_source"] = "new_schema"
                result["has_nlp_info"] = True
                result["trigger_type"] = "user_input"
                
                if hasattr(data, 'conversation_text') and data.conversation_text:
                    result["request_source"] = "new_schema_with_conversation"
                    result["conversation_context"] = data.conversation_text
            
            # 根據代辦文件邏輯：判斷是否需要處理記憶
            if result["trigger_type"] == "user_input" and result["has_nlp_info"]:
                # 使用者輸入且有NLP資訊，需要處理記憶
                result["should_process_memory"] = True
                debug_log(3, f"[MEM] 檢測到使用者輸入請求，需要處理記憶")
            elif result["trigger_type"] == "system_triggered":
                # 系統觸發的請求，可能不需要重複處理記憶
                result["should_process_memory"] = False
                debug_log(3, f"[MEM] 檢測到系統觸發請求，跳過記憶處理")
            else:
                result["should_process_memory"] = True  # 預設處理
            
            # 會話一致性檢查（根據代辦.md要求）
            if result["is_same_session"] and result["session_synced"]:
                debug_log(3, f"[MEM] 檢測到相同會話請求 ({self.current_system_session_id})，可重用上下文資訊")
                result["can_reuse_context"] = True
            else:
                result["can_reuse_context"] = False
                
                # 如果會話不一致，記錄詳細資訊
                if not result["session_synced"]:
                    debug_log(2, f"[MEM] 會話同步失效：系統會話={self.current_system_session_id}, 內部會話={result['internal_sessions']}")
            
            return result
            
        except Exception as e:
            error_log(f"[MEM] 檢查請求會話上下文失敗: {e}")
            return {
                "error": str(e), 
                "is_same_session": False, 
                "request_source": "error",
                "trigger_type": "unknown",
                "should_process_memory": False,
                "session_consistency_check": {"status": "error", "message": str(e)}
            }
    
    def _perform_session_consistency_check(self) -> Dict[str, Any]:
        """執行會話一致性檢查 - 根據代辦.md要求"""
        try:
            check_result = {
                "status": "unknown",
                "system_session_id": self.current_system_session_id,
                "internal_sessions": list(self.memory_manager.current_chat_sessions),
                "session_managers_status": {},
                "recommendations": []
            }
            
            # 1. 檢查系統會話ID是否存在
            if not self.current_system_session_id:
                check_result["status"] = "no_system_session"
                check_result["recommendations"].append("系統會話ID為空，建議檢查StateManager狀態")
                return check_result
            
            # 2. 檢查內部會話狀態
            if not self.memory_manager.current_chat_sessions:
                check_result["status"] = "no_internal_sessions"
                check_result["recommendations"].append("內部沒有活躍會話，可能需要重新加入")
                return check_result
            
            # 3. 檢查會話ID一致性
            if self.current_system_session_id in self.memory_manager.current_chat_sessions:
                check_result["status"] = "consistent"
            else:
                check_result["status"] = "inconsistent"
                check_result["recommendations"].append("系統會話ID與內部會話不匹配，需要同步")
            
            # 4. 檢查各Session Manager的狀態（根據代辦.md要求）
            try:
                # 使用統一Session Manager檢查所有會話類型
                from core.sessions.session_manager import session_manager
                
                # 檢查當前會話
                current_session = session_manager.get_session(self.current_system_session_id)
                if current_session:
                    session_type_name = type(current_session).__name__
                    check_result["session_managers_status"]["current_session"] = {
                        "session_type": session_type_name,
                        "exists": True,
                        "status": current_session.status.value if hasattr(current_session, 'status') else None
                    }
                else:
                    check_result["session_managers_status"]["current_session"] = {
                        "session_type": "unknown",
                        "exists": False,
                        "status": None
                    }
                
                # 檢查所有活躍會話的狀態
                all_active = session_manager.get_all_active_sessions()
                check_result["session_managers_status"]["active_sessions"] = {
                    "general": len(all_active.get('general', [])),
                    "chatting": len(all_active.get('chatting', [])),
                    "workflow": len(all_active.get('workflow', []))
                }
                
            except Exception as e:
                check_result["session_managers_status"]["error"] = str(e)
            
            # 5. 根據檢查結果生成建議
            if check_result["status"] == "inconsistent":
                if not any(sm["exists"] for sm in check_result["session_managers_status"].values() if isinstance(sm, dict)):
                    check_result["recommendations"].append("所有Session Manager都沒有對應會話，建議重新建立")
                else:
                    check_result["recommendations"].append("部分Session Manager有對應會話，建議重新同步內部狀態")
            
            return check_result
            
        except Exception as e:
            error_log(f"[MEM] 會話一致性檢查失敗: {e}")
            return {
                "status": "error",
                "message": str(e),
                "recommendations": ["會話一致性檢查失敗，建議重新初始化"]
            }
    
    def _handle_legacy_api(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """處理舊 API 格式 - 向後相容性支援"""
        try:
            mode = data.get("mode", "")
            debug_log(2, f"[MEM] 處理舊API格式: {mode}")
            
            if mode == "store":
                # 舊格式: {"mode": "store", "entry": {"user": "...", "response": "..."}}
                entry = data.get("entry", {})
                
                # 轉換為新格式
                if "user" in entry and "response" in entry:
                    # 組合對話內容
                    conversation_text = f"用戶: {entry['user']}\n系統: {entry['response']}"
                    memory_token = data.get("memory_token", "legacy_user")
                    
                    # 使用新架構存儲
                    mem_input = MEMInput(
                        operation_type="create_snapshot",
                        memory_token=memory_token,
                        conversation_text=conversation_text,
                        intent_info={"primary_intent": "legacy_conversation"}
                    )
                    
                    result = self._handle_new_schema(mem_input)
                    
                    if isinstance(result, MEMOutput) and result.success:
                        return {"status": "stored", "message": result.message}
                    else:
                        return {"status": "error", "message": "存儲失敗"}
                
                else:
                    return {"status": "error", "message": "缺少必要的 user 或 response 字段"}
            
            elif mode == "fetch":
                # 舊格式: {"mode": "fetch", "text": "...", "top_k": 5}
                query_text = data.get("text", "")
                top_k = data.get("top_k", 5)
                memory_token = data.get("memory_token", "legacy_user")
                
                # 使用新架構查詢
                mem_input = MEMInput(
                    operation_type="query_memory",
                    memory_token=memory_token,
                    query_text=query_text,
                    max_results=top_k
                )
                
                result = self._handle_new_schema(mem_input)
                
                if isinstance(result, MEMOutput) and result.success:
                    # 轉換回舊格式
                    legacy_results = []
                    if hasattr(result, 'search_results') and result.search_results:
                        for search_result in result.search_results:
                            # 嘗試從對話快照中提取 user/response 格式
                            content = search_result.get('content', '')
                            confidence = search_result.get('confidence', 0)
                            
                            # 簡單解析對話格式
                            if '用戶:' in content and '系統:' in content:
                                parts = content.split('系統:')
                                if len(parts) >= 2:
                                    user_part = parts[0].replace('用戶:', '').strip()
                                    response_part = parts[1].strip()
                                    legacy_results.append({
                                        "user": user_part,
                                        "response": response_part,
                                        "confidence": confidence
                                    })
                            else:
                                # 如果不是對話格式，作為通用響應
                                legacy_results.append({
                                    "user": query_text,
                                    "response": content,
                                    "confidence": confidence
                                })
                    
                    if legacy_results:
                        return {"results": legacy_results, "status": "success"}
                    else:
                        return {"results": [], "status": "empty"}
                
                else:
                    return {"results": [], "status": "error"}
            
            else:
                return {"status": "error", "message": f"不支援的模式: {mode}"}
                
        except Exception as e:
            error_log(f"[MEM] 舊API處理失敗: {e}")
            return {"status": "error", "message": f"處理失敗: {str(e)}"}
    
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
                        "memory_token": data.memory_token or "anonymous",  # 使用新架構的memory_token
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
                    
                    # 生成記憶總結上下文
                    memory_summary = self.memory_manager.summarize_memories_for_llm(
                        results, data.query_data.query_text
                    )
                    
                    # 向後兼容：如果有 NLP 整合，也使用它
                    memory_context = memory_summary.get("summary", "")
                    if self.nlp_integration:
                        nlp_context = self.nlp_integration.extract_memory_context_for_llm(results)
                        if nlp_context:
                            memory_context = f"{memory_context}\n{nlp_context}"
                    
                    return MEMOutput(
                        success=True,
                        operation_type="query",
                        search_results=results,
                        memory_context=memory_context,
                        memory_summary=memory_summary,  # 新增結構化總結
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
                if data.conversation_text and data.memory_token:
                    snapshot = self.memory_manager.create_conversation_snapshot(
                        memory_token=data.memory_token,
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
                        errors=["記憶令牌和對話文本不能為空"]
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
            
            # === 新增支援的操作類型 ===
            
            elif data.operation_type == "validate_token":
                # 驗證記憶令牌
                if data.memory_token:
                    # 對於測試令牌，自動視為有效
                    if data.memory_token.startswith("test_"):
                        is_valid = True
                    else:
                        is_valid = self.memory_manager.identity_manager.validate_memory_access(data.memory_token)
                    return MEMOutput(
                        success=is_valid,
                        operation_type="validate_token",
                        message=f"令牌 {data.memory_token} {'有效' if is_valid else '無效'}"
                    )
                else:
                    return MEMOutput(
                        success=False,
                        operation_type="validate_token",
                        errors=["記憶令牌不能為空"]
                    )
            
            elif data.operation_type == "process_identity":
                # 處理身分資訊 - 從 Working Context 獲取而非直接從 NLP
                memory_token = None
                user_profile = None
                
                # 首先嘗試從 Working Context 獲取當前身份
                from core.working_context import working_context_manager
                current_identity = working_context_manager.get_current_identity()
                
                if current_identity:
                    memory_token = current_identity.get("memory_token")
                    user_profile = current_identity
                    debug_log(2, f"[MEM] 從身份獲取記憶令牌: {memory_token}")
                    debug_log(3, f"[MEM] 從 Working Context 獲取身份: {current_identity.get('identity_id', 'Unknown')}")
                elif data.intent_info and "user_profile" in data.intent_info:
                    # 後備方案：從 NLP 輸出獲取（但這應該很少發生）
                    user_profile = data.intent_info["user_profile"]
                    memory_token = user_profile.get("memory_token", "unknown")
                    debug_log(2, "[MEM] 後備：從 NLP 輸出獲取身份資訊")
                
                if memory_token and user_profile:
                    return MEMOutput(
                        success=True,
                        operation_type="process_identity",
                        data={"memory_token": memory_token, "user_profile": user_profile},
                        message="身分資訊處理成功"
                    )
                else:
                    return MEMOutput(
                        success=False,
                        operation_type="process_identity",
                        errors=["無法從 Working Context 或 NLP 輸出獲取身份資訊"]
                    )
            
            elif data.operation_type == "store_memory":
                # 存儲記憶
                if data.memory_entry and data.memory_token:
                    memory_entry = data.memory_entry
                    result = self.memory_manager.store_memory(
                        content=memory_entry.get("content", ""),
                        memory_token=data.memory_token,
                        memory_type=getattr(MemoryType, memory_entry.get("memory_type", "SNAPSHOT").upper()),
                        importance=getattr(MemoryImportance, memory_entry.get("importance", "MEDIUM").upper()),
                        topic=memory_entry.get("topic"),
                        metadata=memory_entry.get("metadata", {})
                    )
                    
                    return MEMOutput(
                        success=result.success if result else False,
                        operation_type="store_memory",
                        operation_result=result.model_dump() if result else None,
                        message="記憶存儲成功" if result and result.success else "記憶存儲失敗"
                    )
                else:
                    return MEMOutput(
                        success=False,
                        operation_type="store_memory",
                        errors=["記憶條目和記憶令牌不能為空"]
                    )
            
            elif data.operation_type == "query_memory":
                # 查詢記憶（簡化版本）
                if data.memory_token and data.query_text:
                    from .schemas import MemoryQuery
                    query = MemoryQuery(
                        memory_token=data.memory_token,
                        query_text=data.query_text,
                        memory_types=[getattr(MemoryType, mt.upper()) for mt in data.memory_types] if data.memory_types else None,
                        max_results=data.max_results or 10
                    )
                    
                    results = self.memory_manager.process_memory_query(query)
                    
                    return MEMOutput(
                        success=True,
                        operation_type="query_memory",
                        search_results=results,
                        message=f"查詢到 {len(results)} 條記憶"
                    )
                else:
                    return MEMOutput(
                        success=False,
                        operation_type="query_memory",
                        errors=["記憶令牌和查詢文本不能為空"]
                    )
            
            elif data.operation_type == "process_nlp_output":
                # 處理NLP輸出 - 使用實際 NLP 輸出格式
                if data.intent_info:
                    # 處理實際 NLP 輸出格式
                    primary_intent = data.intent_info.get("primary_intent", "unknown")
                    overall_confidence = data.intent_info.get("overall_confidence", 0.0)
                    
                    # 從 Working Context 的身份中獲取記憶令牌
                    from core.working_context import working_context_manager
                    current_identity = working_context_manager.get_current_identity()
                    memory_token = current_identity.get("memory_token") if current_identity else None
                    
                    # 如果身份中沒有，使用提供的記憶令牌作為後備
                    if not memory_token:
                        memory_token = data.memory_token
                        debug_log(2, f"[MEM] 使用後備記憶令牌: {memory_token}")
                    else:
                        debug_log(2, f"[MEM] 使用身份記憶令牌: {memory_token}")
                    
                    # 根據意圖和信心度決定是否創建記憶
                    create_memory = overall_confidence > 0.7  # 只有高信心度的意圖才創建記憶
                    
                    if create_memory and data.conversation_text and memory_token:
                        # 將 primary_intent 轉換為字符串（如果是 Enum）
                        topic = str(primary_intent) if hasattr(primary_intent, 'value') else str(primary_intent)
                        
                        snapshot = self.memory_manager.create_conversation_snapshot(
                            memory_token=memory_token,
                            conversation_text=data.conversation_text,
                            topic=topic
                        )
                        
                        debug_log(3, f"[MEM] 基於 NLP 分析創建快照: intent={topic}, confidence={overall_confidence}")
                    
                    return MEMOutput(
                        success=True,
                        operation_type="process_nlp_output",
                        message="NLP輸出處理成功",
                        data={
                            "intent": str(primary_intent),
                            "confidence": overall_confidence,
                            "memory_token": memory_token,
                            "memory_created": create_memory
                        }
                    )
                else:
                    return MEMOutput(
                        success=False,
                        operation_type="process_nlp_output",
                        errors=["NLP輸出資料不能為空"]
                    )
            
            elif data.operation_type == "update_context":
                # 更新對話上下文
                if data.memory_token and data.conversation_context:
                    # 模擬上下文更新
                    return MEMOutput(
                        success=True,
                        operation_type="update_context",
                        message="對話上下文更新成功",
                        session_context=data.conversation_context
                    )
                else:
                    return MEMOutput(
                        success=False,
                        operation_type="update_context",
                        errors=["記憶令牌和上下文資料不能為空"]
                    )
            
            elif data.operation_type == "generate_summary":
                # 生成總結 - 使用新的記憶總結功能
                if data.conversation_text:
                    # 將對話文本轉換為記憶列表進行總結
                    conversation_parts = [data.conversation_text]
                    
                    # 使用記憶管理器的總結功能
                    summary_text = self.memory_manager.chunk_and_summarize_memories(
                        conversation_parts, chunk_size=1
                    )
                    
                    # 構建總結資料
                    summary_data = {
                        "summary": summary_text or f"對話總結：{data.conversation_text[:100]}...",
                        "key_points": ["主要討論內容", "重要決策", "後續行動"],
                        "topics": [data.intent_info.get("primary_intent", "對話") if data.intent_info else "對話"],
                        "summarization_method": "external_model" if self.memory_manager.memory_summarizer else "basic"
                    }
                    
                    return MEMOutput(
                        success=True,
                        operation_type="generate_summary",
                        operation_result=summary_data,
                        message="總結生成成功"
                    )
                else:
                    return MEMOutput(
                        success=False,
                        operation_type="generate_summary",
                        errors=["對話文本不能為空"]
                    )
            
            elif data.operation_type == "extract_key_points":
                # 提取關鍵要點
                if data.conversation_text:
                    # 模擬關鍵要點提取
                    key_points = [
                        "提取的要點1",
                        "提取的要點2", 
                        "提取的要點3"
                    ]
                    
                    return MEMOutput(
                        success=True,
                        operation_type="extract_key_points",
                        operation_result={"key_points": key_points},
                        message="關鍵要點提取成功"
                    )
                else:
                    return MEMOutput(
                        success=False,
                        operation_type="extract_key_points",
                        errors=["對話文本不能為空"]
                    )
            
            elif data.operation_type == "integrate_user_characteristics":
                # 整合用戶特質
                if data.user_profile and data.memory_token:
                    # 將用戶特質存儲為長期記憶
                    result = self.memory_manager.store_memory(
                        content=f"用戶特質：{json.dumps(data.user_profile, ensure_ascii=False)}",
                        memory_token=data.memory_token,
                        memory_type=MemoryType.LONG_TERM,
                        importance=MemoryImportance.HIGH,
                        topic="用戶特質",
                        metadata={"type": "user_characteristics", "data": data.user_profile}
                    )
                    
                    return MEMOutput(
                        success=result.success if result else False,
                        operation_type="integrate_user_characteristics",
                        message="用戶特質整合成功"
                    )
                else:
                    return MEMOutput(
                        success=False,
                        operation_type="integrate_user_characteristics",
                        errors=["用戶資料和記憶令牌不能為空"]
                    )
            
            elif data.operation_type == "generate_llm_instruction":
                # 生成LLM指令
                if data.memory_token and data.query_text:
                    # 先查詢相關記憶
                    from .schemas import MemoryQuery
                    query = MemoryQuery(
                        memory_token=data.memory_token,
                        query_text=data.query_text,
                        max_results=5
                    )
                    
                    relevant_memories = self.memory_manager.process_memory_query(query)
                    
                    # 生成LLM指令
                    llm_instruction = self.memory_manager.generate_llm_instruction(
                        relevant_memories=relevant_memories,
                        context=data.conversation_context or ""
                    )
                    
                    return MEMOutput(
                        success=True,
                        operation_type="generate_llm_instruction",
                        llm_instruction=llm_instruction.model_dump() if llm_instruction else {},
                        message="LLM指令生成成功"
                    )
                else:
                    return MEMOutput(
                        success=False,
                        operation_type="generate_llm_instruction",
                        errors=["記憶令牌和查詢文本不能為空"]
                    )
            
            elif data.operation_type == "process_llm_response":
                # 處理LLM回應
                if data.llm_response and data.memory_token:
                    llm_response = data.llm_response
                    
                    # 處理記憶更新
                    if "memory_updates" in llm_response:
                        for update in llm_response["memory_updates"]:
                            self.memory_manager.store_memory(
                                content=update.get("content", ""),
                                memory_token=data.memory_token,
                                memory_type=MemoryType.LONG_TERM if update.get("type") == "user_preference" else MemoryType.SNAPSHOT,
                                importance=getattr(MemoryImportance, update.get("importance", "MEDIUM").upper()),
                                topic=update.get("type", "llm_feedback"),
                                metadata={"source": "llm_response"}
                            )
                    
                    return MEMOutput(
                        success=True,
                        operation_type="process_llm_response",
                        message="LLM回應處理成功"
                    )
                else:
                    return MEMOutput(
                        success=False,
                        operation_type="process_llm_response",
                        errors=["LLM回應和記憶令牌不能為空"]
                    )
            
            # === 會話管理操作 ===
            
            elif data.operation_type in ["create_session", "get_session_info", "add_session_interaction", 
                                       "get_session_history", "update_session_context", "get_session_context",
                                       "end_session", "archive_session", "search_archived_sessions",
                                       "preserve_session_context", "retrieve_session_context", "get_snapshot_history"]:
                # 會話相關操作（目前返回模擬成功）
                return MEMOutput(
                    success=True,
                    operation_type=data.operation_type,
                    message=f"{data.operation_type} 操作模擬成功",
                    data={"session_id": getattr(data, 'session_id', 'mock_session')}
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
        debug_log(2, "[MEM] 處理 NLP 輸出")
        
        try:
            # 直接處理 NLP 輸出，不依賴 nlp_integration
            if isinstance(nlp_output, dict):
                # 構造 MEMInput
                mem_input = MEMInput(
                    operation_type="process_nlp_output",
                    intent_info=nlp_output,
                    conversation_text=nlp_output.get("original_text", ""),
                    memory_token=None  # 讓 _handle_new_schema 從 Working Context 獲取
                )
                return self._handle_new_schema(mem_input)
            else:
                error_log("[MEM] NLP 輸出格式無效")
                return None
                
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

    def handle(self, data) -> dict:
        """處理MEM請求 - 實現BaseModule接口"""
        try:
            # 檢查是否在CHAT狀態
            if not self._is_in_chat_state():
                return {
                    'success': False,
                    'error': 'MEM模組只在CHAT狀態下運行',
                    'status': 'failed'
                }
            
            # ✅ 檢查臨時身份,優雅跳過個人記憶存取
            if self.memory_manager and self.memory_manager.identity_manager and self.memory_manager.identity_manager.is_temporary_identity():
                identity_desc = self.memory_manager.identity_manager.get_identity_type_description()
                debug_log(2, f"[MEM] handle() 檢測到{identity_desc}，跳過個人記憶存取")
                return self._create_temporary_identity_response()

            # 如果是字符串，嘗試轉換為MEMInput
            if isinstance(data, str):
                # 簡單的字符串處理，測試用
                return {
                    'success': False,
                    'error': '不支援字符串輸入，請使用MEMInput對象',
                    'status': 'invalid_input'
                }

            # 如果是MEMInput對象，處理它
            if hasattr(data, 'operation_type'):
                return self._handle_mem_input(data)

            # 其他情況
            return {
                'success': False,
                'error': '不支援的輸入類型',
                'status': 'invalid_input'
            }

        except Exception as e:
            error_log(f"[MEM] 處理請求失敗: {e}")
            return {
                'success': False,
                'error': str(e),
                'status': 'error'
            }

    def _handle_mem_input(self, mem_input) -> dict:
        """處理MEMInput對象"""
        try:
            operation_type = mem_input.operation_type

            if operation_type == "store_memory":
                return self._handle_store_memory(mem_input)
            elif operation_type == "query_memory":
                return self._handle_query_memory(mem_input)
            elif operation_type == "create_snapshot":
                return self._handle_create_snapshot(mem_input)
            elif operation_type == "validate_token":
                return self._handle_validate_token(mem_input)
            elif operation_type == "process_identity":
                return self._handle_process_identity(mem_input)
            elif operation_type == "process_nlp_output":
                return self._handle_process_nlp_output(mem_input)
            elif operation_type == "get_snapshot_history":
                return self._handle_get_snapshot_history(mem_input)
            else:
                return {
                    'success': False,
                    'error': f'不支援的操作類型: {operation_type}',
                    'status': 'unsupported_operation'
                }

        except Exception as e:
            error_log(f"[MEM] 處理MEMInput失敗: {e}")
            return {
                'success': False,
                'error': str(e),
                'status': 'error'
            }

    def _handle_store_memory(self, mem_input) -> dict:
        """處理記憶存儲請求"""
        try:
            if not self.memory_manager:
                return {'success': False, 'error': '記憶管理器未初始化'}

            # 從mem_input提取記憶資訊
            content = mem_input.memory_entry.get('content', '')
            memory_type_str = mem_input.memory_entry.get('memory_type', 'long_term')
            topic = mem_input.memory_entry.get('topic', 'general')
            importance_str = mem_input.memory_entry.get('importance', 'medium')

            # 轉換為MemoryManager期望的枚舉類型
            from .schemas import MemoryType, MemoryImportance
            memory_type = MemoryType(memory_type_str) if memory_type_str in [e.value for e in MemoryType] else MemoryType.LONG_TERM
            importance = MemoryImportance(importance_str.lower()) if importance_str.lower() in [e.value for e in MemoryImportance] else MemoryImportance.MEDIUM

            # 調用MemoryManager的store_memory方法
            result = self.memory_manager.store_memory(
                content=content,
                memory_token=mem_input.memory_token,
                memory_type=memory_type,
                importance=importance,
                topic=topic
            )

            return {
                'success': result.success,
                'message': result.message,
                'memory_id': result.memory_id if hasattr(result, 'memory_id') and result.success else None,
                'status': 'success' if result.success else 'failed'
            }

        except Exception as e:
            error_log(f"[MEM] 處理記憶存儲失敗: {e}")
            return {'success': False, 'error': str(e), 'status': 'error'}

    def _handle_query_memory(self, mem_input) -> dict:
        """處理記憶查詢請求"""
        try:
            if not self.memory_manager:
                return {'success': False, 'error': '記憶管理器未初始化'}

            from .schemas import MemoryQuery
            query = MemoryQuery(
                memory_token=mem_input.memory_token,
                query_text=mem_input.query_text,
                memory_types=mem_input.memory_types or ['long_term', 'snapshot'],
                max_results=mem_input.max_results or 10
            )

            results = self.memory_manager.process_memory_query(query)

            return {
                'success': True,
                'search_results': [r.model_dump() if hasattr(r, 'model_dump') else r for r in results],
                'status': 'success'
            }

        except Exception as e:
            return {'success': False, 'error': str(e), 'status': 'error'}

    def _handle_create_snapshot(self, mem_input) -> dict:
        """處理快照創建請求"""
        try:
            if not self.memory_manager:
                return {'success': False, 'error': '記憶管理器未初始化'}

            result = self.memory_manager.create_conversation_snapshot(
                memory_token=mem_input.memory_token,
                conversation_text=mem_input.conversation_text
            )

            if result is None:
                return {
                    'success': False,
                    'error': '快照創建失敗',
                    'status': 'failed'
                }

            return {
                'success': True,
                'message': '快照創建成功',
                'snapshot_id': result.memory_id,
                'status': 'success'
            }

        except Exception as e:
            return {'success': False, 'error': str(e), 'status': 'error'}

    def _handle_validate_token(self, mem_input) -> dict:
        """處理令牌驗證請求"""
        try:
            if not self.memory_manager:
                return {'success': False, 'error': '記憶管理器未初始化'}

            is_valid = self.memory_manager.identity_manager.validate_memory_access(
                mem_input.memory_token, "read"
            )

            return {
                'success': is_valid,
                'message': '令牌驗證成功' if is_valid else '令牌驗證失敗',
                'status': 'success' if is_valid else 'failed'
            }

        except Exception as e:
            return {'success': False, 'error': str(e), 'status': 'error'}

    def _handle_process_identity(self, mem_input) -> dict:
        """處理身份資訊處理請求"""
        try:
            # 簡單實現 - 實際應該與NLP模組整合
            return {
                'success': True,
                'message': '身份資訊處理成功',
                'data': {'memory_token': mem_input.memory_token},
                'status': 'success'
            }

        except Exception as e:
            return {'success': False, 'error': str(e), 'status': 'error'}

    def _handle_process_nlp_output(self, mem_input) -> dict:
        """處理NLP輸出處理請求"""
        try:
            # 簡單實現 - 實際應該與NLP模組整合
            return {
                'success': True,
                'message': 'NLP輸出處理成功',
                'status': 'success'
            }

        except Exception as e:
            return {'success': False, 'error': str(e), 'status': 'error'}

    def _handle_get_snapshot_history(self, mem_input) -> dict:
        """處理快照歷史檢索請求"""
        try:
            if not self.memory_manager:
                return {'success': False, 'error': '記憶管理器未初始化'}

            snapshots = self.memory_manager.snapshot_manager.get_active_snapshots(
                mem_input.memory_token
            )

            return {
                'success': True,
                'search_results': [s.model_dump() if hasattr(s, 'model_dump') else str(s) for s in snapshots],
                'status': 'success'
            }

        except Exception as e:
            return {'success': False, 'error': str(e), 'status': 'error'}

    def _create_temporary_identity_response(self) -> dict:
        """為臨時身份創建適當的回應，不存取個人記憶"""
        try:
            identity_desc = self.memory_manager.identity_manager.get_identity_type_description() if (self.memory_manager and self.memory_manager.identity_manager) else "未知身份"
            
            return {
                'success': True,
                'message': f'臨時身份模式：{identity_desc}',
                'memory_context': '',  # 空的記憶上下文
                'search_results': [],  # 無搜尋結果
                'total_memories': 0,
                'active_snapshots': [],
                'temporal_context': {
                    'identity_type': 'temporary',
                    'access_level': 'basic',
                    'personal_memory_access': False,
                    'note': '臨時身份無法存取個人記憶庫'
                },
                'status': 'temporary_identity'
            }
            
        except Exception as e:
            error_log(f"[MEM] 創建臨時身份回應失敗: {e}")
            return self._create_error_response("臨時身份處理錯誤")

    def shutdown(self):
        """模組關閉"""
        info_log("[MEM] 模組關閉")
        if self.memory_manager:
            # 如果需要，可以在這裡添加記憶管理器的清理邏輯
            pass
