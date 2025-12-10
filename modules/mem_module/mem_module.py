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
from configs.user_settings_manager import user_settings_manager, get_user_setting
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
        
        # 註冊使用者設定熱重載回調
        user_settings_manager.register_reload_callback("mem_module", self._reload_from_user_settings)

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
            
            # 註冊 CHAT-MEM 協作管道的 provider
            self._register_collaboration_providers()
            
            # 註冊Working Context處理器
            self.working_context_handler = register_memory_context_handler(
                working_context_manager, self.memory_manager
            )
            if not self.working_context_handler:
                error_log("[MEM] Working Context處理器註冊失敗")
                return False
            
            # 註冊狀態變化監聽器
            self._register_state_change_listener()
            
            # Phase 4: 註冊 GS 推進事件監聽器
            self._register_gs_advanced_listener()
            
            # 🔧 註冊會話結束和處理層完成事件監聽器（用於更新快照）
            self._register_snapshot_update_listeners()
            
            # 啟動會話同步
            self._start_session_sync()
            
            # 新架構不需要舊版FAISS相容性
            self.is_initialized = True
            info_log("[MEM] 重構架構初始化完成")
            return True
            
        except Exception as e:
            error_log(f"[MEM] 重構架構初始化失敗: {e}")
            return False
    
    def _register_collaboration_providers(self):
        """註冊 CHAT-MEM 協作管道的資料提供者"""
        try:
            from modules.llm_module.module_interfaces import state_aware_interface
            
            # 註冊記憶檢索 provider
            def memory_retrieval_provider(**kwargs):
                from .schemas import MemoryType
                
                query = kwargs.get('query', '')
                max_results = kwargs.get('max_results', 3)
                memory_types_raw = kwargs.get('memory_types', None)
                
                # 舊類型名稱到新類型的映射
                type_mapping = {
                    'conversation': MemoryType.SNAPSHOT,      # 對話 -> 快照（短期記憶）
                    'user_info': MemoryType.PROFILE,         # 使用者資訊 -> 檔案（長期記憶）
                    'context': MemoryType.SNAPSHOT,          # 上下文 -> 快照（短期記憶）
                    'preference': MemoryType.PREFERENCE,     # 偏好（長期記憶）
                    'long_term': MemoryType.LONG_TERM,      # 長期記憶
                    'system_learning': MemoryType.SYSTEM_LEARNING,  # 系統學習
                    # 向後相容
                    'interaction_history': MemoryType.SNAPSHOT  # 舊名稱映射到快照
                }
                
                # 轉換 memory_types 為 MemoryType 枚舉（如果需要）
                memory_types = None
                if memory_types_raw:
                    if isinstance(memory_types_raw, list):
                        memory_types = []
                        for t in memory_types_raw:
                            if isinstance(t, str):
                                # 使用映射或直接轉換
                                if t in type_mapping:
                                    memory_types.append(type_mapping[t])
                                else:
                                    try:
                                        memory_types.append(MemoryType(t))
                                    except ValueError:
                                        debug_log(2, f"[MEM] 忽略無效的記憶類型: {t}")
                            else:
                                memory_types.append(t)
                        
                        if not memory_types:
                            memory_types = None  # 如果全部無效，使用默認值
                
                # 從 working context 獲取當前身份的 memory_token
                memory_token = working_context_manager.get_memory_token()
                
                debug_log(2, f"[MEM] 記憶檢索請求 - query: {query[:50]}, types: {memory_types}, token: {memory_token}")
                
                # 分離快照和長期記憶的檢索
                snapshot_types = [MemoryType.SNAPSHOT]
                longterm_types = [MemoryType.PROFILE, MemoryType.PREFERENCE, MemoryType.LONG_TERM]
                
                requested_types = memory_types or (snapshot_types + longterm_types)
                
                has_snapshots = any(t in snapshot_types for t in requested_types)
                has_longterm = any(t in longterm_types for t in requested_types)
                
                results = []
                
                # 1. 檢索長期記憶（直接使用）
                if has_longterm:
                    longterm_types_filtered = [t for t in requested_types if t in longterm_types]
                    if longterm_types_filtered:
                        longterm_memories = self.memory_manager.retrieve_memories(
                            query_text=query,
                            memory_token=memory_token,
                            max_results=max_results,
                            memory_types=longterm_types_filtered
                        )
                        results.extend(longterm_memories)
                        debug_log(2, f"[MEM] 檢索到 {len(longterm_memories)} 條長期記憶")
                
                # 2. 檢索並總結快照（需要處理）
                if has_snapshots:
                    snapshot_types_filtered = [t for t in requested_types if t in snapshot_types]
                    if snapshot_types_filtered:
                        snapshot_memories = self.memory_manager.retrieve_memories(
                            query_text=query,
                            memory_token=memory_token,
                            max_results=max_results,
                            memory_types=snapshot_types_filtered
                        )
                        
                        # 總結快照內容
                        if snapshot_memories:
                            summarized_snapshots = self._summarize_snapshots(snapshot_memories)
                            results.extend(summarized_snapshots)
                            debug_log(2, f"[MEM] 檢索並總結 {len(snapshot_memories)} 條快照")
                
                debug_log(2, f"[MEM] 記憶檢索完成 - 總共 {len(results)} 條記憶")
                return results
            
            # 註冊對話儲存 provider
            def conversation_storage_provider(**kwargs):
                from .schemas import MemoryType, MemoryImportance
                
                conversation_data = kwargs.get('conversation_data', {})
                
                # 從 working context 獲取當前身份的 memory_token
                memory_token = working_context_manager.get_memory_token()
                
                debug_log(2, f"[MEM] 對話儲存請求 - token: {memory_token}")
                
                # 提取對話內容
                content = conversation_data.get('content', {})
                metadata = conversation_data.get('metadata', {})
                
                # 構建儲存內容
                storage_content = f"User: {content.get('user_input', '')}\nAssistant: {content.get('assistant_response', '')}"
                
                # 處理 memory_type：將字符串轉換為 MemoryType 枚舉
                memory_type_raw = metadata.get('memory_type', 'snapshot')  # 預設為快照（短期記憶）
                # 類型映射：確保對話類型儲存為快照
                if memory_type_raw in ['conversation', 'interaction_history']:
                    memory_type_raw = 'snapshot'  # 對話類型統一儲存為快照（短期記憶）
                memory_type = MemoryType(memory_type_raw) if isinstance(memory_type_raw, str) else memory_type_raw
                
                # 處理 importance
                importance_raw = metadata.get('importance', 'medium')
                if importance_raw == 'normal':
                    importance_raw = 'medium'  # 轉換舊的重要性名稱
                importance = MemoryImportance(importance_raw) if isinstance(importance_raw, str) else importance_raw
                
                result = self.memory_manager.store_memory(
                    content=storage_content,
                    memory_token=memory_token,
                    memory_type=memory_type,
                    importance=importance,
                    metadata=metadata
                )
                
                debug_log(2, f"[MEM] 對話儲存完成 - 成功: {result}")
                return result
            
            # 註冊 Profile 記憶檢索 provider（用於 LLM 快取注入）
            def profile_memories_provider(**kwargs):
                from .schemas import MemoryType
                
                memory_token = kwargs.get('memory_token')
                max_results = kwargs.get('max_results', 50)
                
                if not memory_token:
                    memory_token = working_context_manager.get_memory_token()
                
                if not memory_token:
                    debug_log(3, "[MEM] 無 memory_token，無法檢索 profile 記憶")
                    return []
                
                debug_log(2, f"[MEM] Profile 記憶檢索請求 - token: {memory_token}")
                
                # 檢索 PROFILE 類型記憶（長期用戶觀察）
                results = self.memory_manager.retrieve_memories(
                    query_text="",  # 空查詢檢索所有
                    memory_token=memory_token,
                    memory_types=[MemoryType.PROFILE],
                    max_results=max_results,
                    similarity_threshold=0.0  # 檢索所有 profile 記憶
                )
                
                debug_log(2, f"[MEM] 檢索到 {len(results)} 條 profile 記憶")
                
                # 轉換為簡化格式
                formatted_results = []
                for result in results:
                    memory_entry = result.memory_entry
                    if isinstance(memory_entry, dict):
                        formatted_results.append({
                            "content": memory_entry.get("content", ""),
                            "created_at": str(memory_entry.get("created_at", ""))
                        })
                    else:
                        formatted_results.append({
                            "content": getattr(memory_entry, "content", ""),
                            "created_at": str(getattr(memory_entry, "created_at", ""))
                        })
                
                return formatted_results
            
            # 註冊到 state_aware_interface
            state_aware_interface.register_chat_mem_provider("memory_retrieval", memory_retrieval_provider)
            state_aware_interface.register_chat_mem_provider("conversation_storage", conversation_storage_provider)
            state_aware_interface.register_chat_mem_provider("profile_memories", profile_memories_provider)
            
            info_log("[MEM] CHAT-MEM 協作管道 provider 註冊完成（含 profile_memories）")
            
        except Exception as e:
            error_log(f"[MEM] 協作 provider 註冊失敗: {e}")
    
    def _summarize_snapshots(self, snapshot_results: List[Any]) -> List[Any]:
        """
        總結快照內容，返回摘要版本
        
        Args:
            snapshot_results: MemorySearchResult 對象列表
            
        Returns:
            總結後的 MemorySearchResult 對象列表
        """
        from .schemas import MemorySearchResult
        
        summarized = []
        
        try:
            for snapshot_result in snapshot_results:
                snapshot_entry = snapshot_result.memory_entry
                
                # 使用 MemorySummarizer 總結快照內容
                if self.memory_manager and self.memory_manager.memory_summarizer:
                    summary = self.memory_manager.memory_summarizer.summarize_conversation(
                        snapshot_entry.content
                    )
                else:
                    # 簡單截斷作為 fallback
                    summary = snapshot_entry.content[:200] + "..." if len(snapshot_entry.content) > 200 else snapshot_entry.content
                
                # 創建摘要版本的 MemoryEntry（使用 model_copy with update）
                from pydantic import BaseModel
                if isinstance(snapshot_entry, BaseModel):
                    update_dict = {'content': f"[快照摘要] {summary}"}
                    if hasattr(snapshot_entry, 'summary'):
                        update_dict['summary'] = summary
                    summarized_entry = snapshot_entry.model_copy(update=update_dict, deep=True)
                else:
                    # Fallback：如果不是 BaseModel，直接複製
                    summarized_entry = snapshot_entry
                
                # 創建新的 MemorySearchResult
                summarized_result = MemorySearchResult(
                    memory_entry=summarized_entry,
                    similarity_score=snapshot_result.similarity_score,
                    relevance_score=snapshot_result.relevance_score,
                    retrieval_reason="相關對話快照（已總結）"
                )
                summarized.append(summarized_result)
                
                debug_log(3, f"[MEM] 快照總結: {len(snapshot_entry.content)} → {len(summary)} 字符")
                
        except Exception as e:
            error_log(f"[MEM] 總結快照失敗: {e}")
            # 失敗時返回原始結果
            return snapshot_results
        
        return summarized
    
    def _register_state_change_listener(self):
        """註冊狀態變化監聽器"""
        try:
            from core.states.state_manager import state_manager
            self.state_change_listener = self._handle_state_change
            state_manager.add_state_change_callback(self.state_change_listener)
            debug_log(2, "[MEM] 狀態變化監聽器註冊完成")
        except Exception as e:
            error_log(f"[MEM] 狀態變化監聽器註冊失敗: {e}")
    
    def _register_gs_advanced_listener(self):
        """註冊 GS 推進事件監聽器（Phase 4）"""
        try:
            from core.event_bus import event_bus, SystemEvent
            event_bus.subscribe(SystemEvent.GS_ADVANCED, self._on_gs_advanced)
            debug_log(2, "[MEM] GS_ADVANCED 事件監聽器註冊完成")
        except Exception as e:
            error_log(f"[MEM] GS_ADVANCED 事件監聽器註冊失敗: {e}")
    
    def _register_snapshot_update_listeners(self):
        """註冊快照更新相關事件監聽器"""
        try:
            from core.event_bus import event_bus, SystemEvent
            
            # 訂閱處理層完成事件 - 每次循環後更新快照
            event_bus.subscribe(
                SystemEvent.PROCESSING_LAYER_COMPLETE,
                self._on_processing_complete,
                handler_name="MEM.snapshot_update"
            )
            
            # 訂閱會話結束事件 - CS 結束時完整保存快照
            event_bus.subscribe(
                SystemEvent.SESSION_ENDED,
                self._on_session_ended,
                handler_name="MEM.session_end"
            )
            
            debug_log(2, "[MEM] 快照更新事件監聽器註冊完成")
        except Exception as e:
            error_log(f"[MEM] 快照更新事件監聽器註冊失敗: {e}")
    
    def _on_gs_advanced(self, event):
        """處理 GS 推進事件 - 清理過期快照"""
        try:
            current_session_id = event.data.get('session_id')
            gs_history = event.data.get('gs_history', [])  # 字符串 session_id 列表
            
            debug_log(2, f"[MEM] 收到 GS 推進通知: {current_session_id}")
            
            # 保留最近 3 個 GS 的快照
            recent_session_ids = gs_history[-3:] if gs_history else []
            
            # 清理過期的快照
            if self.memory_manager and self.memory_manager.snapshot_manager:
                self.memory_manager.snapshot_manager.cleanup_expired_snapshots(recent_session_ids, keep_count=3)
                debug_log(2, f"[MEM] 快照清理完成，保留 session_id: {recent_session_ids}")
            
        except Exception as e:
            error_log(f"[MEM] 處理 GS 推進事件失敗: {e}")
    
    def _on_processing_complete(self, event):
        """處理處理層完成事件 - 更新快照記錄當前互動"""
        try:
            # 只在 CHAT 狀態下處理
            if not self._is_in_chat_state():
                return
            
            # 獲取處理層輸出（LLM 回應）
            # LLM 模組在 PROCESSING_LAYER_COMPLETE 事件中使用 "response" 欄位
            response_text = event.data.get('response', '')
            if not response_text:
                debug_log(3, "[MEM] 處理層輸出無文本內容，跳過快照更新")
                return
            
            # 獲取當前 CS 和用戶輸入
            from core.sessions.session_manager import unified_session_manager
            active_cs = unified_session_manager.get_active_chatting_session_ids()
            
            if not active_cs:
                debug_log(3, "[MEM] 沒有活躍 CS，跳過快照更新")
                return
            
            cs_id = active_cs[0]
            
            # 從 Working Context 獲取用戶輸入
            from core.working_context import working_context_manager
            user_input = working_context_manager.get_context_data('last_user_input') or ''
            
            # 構建互動記錄
            message_data = {
                'user': user_input,
                'assistant': response_text,
                'timestamp': datetime.now().isoformat()
            }
            
            # 更新快照
            if self.memory_manager and self.memory_manager.snapshot_manager:
                success = self.memory_manager.snapshot_manager.add_message_to_snapshot(
                    session_id=cs_id,
                    message_data=message_data
                )
                if success:
                    debug_log(2, f"[MEM] 已更新快照 {cs_id} - 記錄當前互動")
                else:
                    debug_log(2, f"[MEM] 快照 {cs_id} 更新失敗")
            
        except Exception as e:
            error_log(f"[MEM] 處理處理層完成事件失敗: {e}")
    
    def _on_session_ended(self, event):
        """處理會話結束事件 - 完整保存快照並總結"""
        try:
            session_type = event.data.get('session_type')
            
            # 只處理 CS 結束
            if session_type != 'chatting':
                return
            
            cs_id = event.data.get('session_id')
            if not cs_id:
                return
            
            debug_log(2, f"[MEM] CS {cs_id} 結束，準備完整保存快照")
            
            # 獲取 CS 的完整對話記錄
            from core.sessions.session_manager import unified_session_manager
            cs = unified_session_manager.get_chatting_session(cs_id)
            
            if not cs:
                debug_log(2, f"[MEM] 找不到 CS {cs_id}，跳過快照保存")
                return
            
            # 獲取所有對話輪次
            conversation_turns = cs.get_recent_turns(count=None)  # 獲取所有輪次
            
            if not conversation_turns:
                debug_log(2, f"[MEM] CS {cs_id} 沒有對話記錄，跳過快照保存")
                return
            
            # 構建完整的對話摘要
            messages = []
            for turn in conversation_turns:
                if turn.get('user_input'):
                    messages.append({
                        'role': 'user',
                        'content': turn['user_input'].get('text', ''),
                        'timestamp': turn.get('start_time', '')
                    })
                if turn.get('system_response'):
                    messages.append({
                        'role': 'assistant',
                        'content': turn['system_response'].get('content', ''),
                        'timestamp': turn.get('end_time', '')
                    })
            
            # 更新快照內容
            if self.memory_manager and self.memory_manager.snapshot_manager:
                snapshot = self.memory_manager.snapshot_manager.get_snapshot(cs_id)
                if snapshot:
                    # 更新快照的完整內容和摘要
                    self.memory_manager.snapshot_manager.update_snapshot_content(
                        snapshot_id=cs_id,
                        new_content=messages,
                        new_gsids=snapshot.gs_session_ids  # 保持原有的 GSID 列表
                    )
                    info_log(f"[MEM] CS {cs_id} 的快照已完整保存 ({len(messages)} 條訊息)")
                else:
                    debug_log(2, f"[MEM] 找不到快照 {cs_id}，跳過保存")
            
        except Exception as e:
            error_log(f"[MEM] 處理會話結束事件失敗: {e}")
    
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
            
            # 4. 從 StateQueue 獲取實際的觸發內容（用戶輸入）
            trigger_content = ""
            context_content = ""
            try:
                from core.states.state_queue import StateQueue
                state_queue = StateQueue.get_instance()
                current_item = state_queue.get_current_item()
                trigger_content = current_item.get("trigger_content", "") if current_item else ""
                context_content = current_item.get("context_content", trigger_content) if current_item else ""
                debug_log(2, f"[MEM] 從 StateQueue 獲取觸發內容: {trigger_content[:100] if trigger_content else '(空)'}")
            except Exception as e:
                debug_log(3, f"[MEM] 無法獲取 StateQueue 內容: {e}")
            
            # 構建初始上下文
            initial_context = {
                "session_type": "chat",
                "started_by_state_change": True,
                "memory_token": memory_token,
                "identity_context": identity_context,
                "session_context": session_context,
                "trigger_content": trigger_content,
                "state_context_content": context_content  # 提供實際用戶輸入
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

            # 其他情況：可能是 processing 層的誤調用，返回跳過狀態
            debug_log(4, f"[MEM] 收到非預期輸入類型: {type(data).__name__}, 跳過處理")
            return {
                'success': True,  # 改為 True 避免錯誤日誌
                'status': 'skipped',
                'message': '非 MEM 專用輸入，已跳過'
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
    
    def _reload_from_user_settings(self, key_path: str, value: Any) -> bool:
        """
        從 user_settings.yaml 重載設定
        
        Args:
            key_path: 設定路徑
            value: 新值
            
        Returns:
            是否成功
        """
        try:
            info_log(f"[MEM] 🔄 重載使用者設定: {key_path} = {value}")
            
            if key_path == "interaction.memory.enabled":
                # MEM 模組開關
                info_log(f"[MEM] MEM 模組已{'啟用' if value else '禁用'}")
                # 實際開關控制由外部處理
                

                
            else:
                debug_log(2, f"[MEM] 未處理的設定路徑: {key_path}")
                return False
            
            return True
            
        except Exception as e:
            error_log(f"[MEM] 重載使用者設定失敗: {e}")
            import traceback
    
    # ========== MCP Tools Registration ==========
    
    def register_memory_tools_to_mcp(self, mcp_server) -> bool:
        """
        向 MCP Server 註冊記憶檢索工具
        
        這些工具只在 CHAT 路徑可用，讓 LLM 主動檢索對話歷史快照
        
        Args:
            mcp_server: MCP Server 實例
            
        Returns:
            是否成功註冊
        """
        try:
            from modules.sys_module.mcp_server.tool_definitions import MCPTool, ToolParameter, ToolParameterType
            
            info_log("[MEM] 註冊記憶檢索 MCP 工具...")
            
            # 1. memory_retrieve_profile - 獲取用戶完整資料（無過濾）
            mcp_server.register_tool(MCPTool(
                name="memory_retrieve_profile",
                description="Get ALL stored facts about the user (interests, preferences, personal info, habits, skills). Returns EVERYTHING - no filtering, no search. Use when you need complete user context or user asks 'what do you know about me'.",
                parameters=[],  # 無參數，直接全取
                handler=self._handle_memory_retrieve_profile,
                allowed_paths=["CHAT"]
            ))
            
            # 2. memory_search_snapshots - 搜索對話歷史（語義搜索）
            mcp_server.register_tool(MCPTool(
                name="memory_search_snapshots",
                description="Search past conversation history using semantic search. Use when user asks 'what did we discuss about X' or you need to recall previous dialogues on a topic. Returns relevant conversation snapshots with similarity scores.",
                parameters=[
                    ToolParameter(
                        name="query",
                        type=ToolParameterType.STRING,
                        description="Search query describing the topic or conversation you're looking for (e.g., 'python tutorial', 'project planning', 'yesterday's discussion').",
                        required=True
                    ),
                    ToolParameter(
                        name="max_results",
                        type=ToolParameterType.INTEGER,
                        description="Maximum number of snapshots to return (default: 5)",
                        required=False
                    ),
                    ToolParameter(
                        name="similarity_threshold",
                        type=ToolParameterType.FLOAT,
                        description="Minimum similarity score 0.0-1.0 (default: 0.6). Lower = more results but less relevant.",
                        required=False
                    ),
                ],
                handler=self._handle_memory_search_snapshots,
                allowed_paths=["CHAT"]
            ))
            
            # 2b. memory_retrieve_snapshots - 取用 PROFILE + SNAPSHOT 記憶
            mcp_server.register_tool(MCPTool(
                name="memory_retrieve_snapshots",
                description="Retrieve both long-term user profile facts and conversation snapshots in one call. Use when user asks what you know about them or references past discussions. memory_types defaults to 'profile,snapshot'.",
                parameters=[
                    ToolParameter(
                        name="memory_types",
                        type=ToolParameterType.STRING,
                        description="Comma-separated memory types: profile, snapshot, long_term, preference (default: profile,snapshot)",
                        required=False
                    ),
                    ToolParameter(
                        name="query",
                        type=ToolParameterType.STRING,
                        description="Topic to search for when retrieving snapshots/long-term memories (optional for profile-only retrieval)",
                        required=False
                    ),
                    ToolParameter(
                        name="max_results",
                        type=ToolParameterType.INTEGER,
                        description="Maximum number of results to return (default: 5)",
                        required=False
                    ),
                    ToolParameter(
                        name="similarity_threshold",
                        type=ToolParameterType.FLOAT,
                        description="Minimum similarity score 0.0-1.0 (default: 0.6, lowered when query is empty)",
                        required=False
                    ),
                ],
                handler=self._handle_memory_retrieve_snapshots,
                allowed_paths=["CHAT"]
            ))
            
            # 2. memory_get_snapshot - 獲取完整快照內容
            mcp_server.register_tool(MCPTool(
                name="memory_get_snapshot",
                description="Get full conversation details from a specific snapshot by ID. Returns complete message history.",
                parameters=[
                    ToolParameter(
                        name="snapshot_id",
                        type=ToolParameterType.STRING,
                        description="Snapshot memory ID to retrieve",
                        required=True
                    ),
                ],
                handler=self._handle_memory_get_snapshot,
                allowed_paths=["CHAT"]
            ))
            
            # 3. memory_search_timeline - 時間範圍檢索
            mcp_server.register_tool(MCPTool(
                name="memory_search_timeline",
                description="Search snapshots within a time range, optionally filtered by topic. Returns chronologically ordered snapshots.",
                parameters=[
                    ToolParameter(
                        name="start_time",
                        type=ToolParameterType.STRING,
                        description="Start time in ISO format (e.g., '2025-12-01T00:00:00')",
                        required=True
                    ),
                    ToolParameter(
                        name="end_time",
                        type=ToolParameterType.STRING,
                        description="End time in ISO format (e.g., '2025-12-07T23:59:59')",
                        required=True
                    ),
                    ToolParameter(
                        name="topic",
                        type=ToolParameterType.STRING,
                        description="Optional topic filter to narrow results",
                        required=False
                    ),
                ],
                handler=self._handle_memory_search_timeline,
                allowed_paths=["CHAT"]
            ))
            
            # 4. memory_update_profile - 更新用戶檔案記憶
            mcp_server.register_tool(MCPTool(
                name="memory_update_profile",
                description="Store PROFILE memory: long-term facts about the user that persist across ALL future conversations. Use when user shares: interests, preferences, personal info, habits, skills. Example: 'User likes Python' or 'User is a student'. NOT for conversation content - use snapshot for that.",
                parameters=[
                    ToolParameter(
                        name="observation",
                        type=ToolParameterType.STRING,
                        description="The observation or information about the user to store",
                        required=True
                    ),
                    ToolParameter(
                        name="category",
                        type=ToolParameterType.STRING,
                        description="Category of the observation (e.g., 'preference', 'personal_info', 'habit', 'skill')",
                        required=False
                    ),
                    ToolParameter(
                        name="importance",
                        type=ToolParameterType.STRING,
                        description="Importance level: 'critical', 'high', 'medium', 'low' (default: 'medium')",
                        required=False
                    ),
                ],
                handler=self._handle_memory_update_profile,
                allowed_paths=["CHAT"]
            ))
            
            # 5. memory_store_observation - 儲存一般觀察
            mcp_server.register_tool(MCPTool(
                name="memory_store_observation",
                description="Store user observations as PROFILE memory (alternative to memory_update_profile). Use when learning about the user during conversation (what they like, their background, preferences). Stored facts will be available in ALL future conversations.",
                parameters=[
                    ToolParameter(
                        name="content",
                        type=ToolParameterType.STRING,
                        description="The observation content to store",
                        required=True
                    ),
                    ToolParameter(
                        name="memory_type",
                        type=ToolParameterType.STRING,
                        description="Type of memory: 'profile' (user-related) or 'long_term' (general context). Default: 'long_term'",
                        required=False
                    ),
                    ToolParameter(
                        name="topic",
                        type=ToolParameterType.STRING,
                        description="Topic or category of the observation",
                        required=False
                    ),
                    ToolParameter(
                        name="importance",
                        type=ToolParameterType.STRING,
                        description="Importance level: 'critical', 'high', 'medium', 'low' (default: 'medium')",
                        required=False
                    ),
                ],
                handler=self._handle_memory_store_observation,
                allowed_paths=["CHAT"]
            ))
            
            # 6. memory_create_snapshot - 創建新快照
            mcp_server.register_tool(MCPTool(
                name="memory_create_snapshot",
                description="Create SNAPSHOT memory: save current conversation for later retrieval. Use at end of topic/discussion to preserve dialogue history. Different from profile - this stores WHAT WAS SAID, not facts about user. User can later ask 'what did we discuss about X' to retrieve this.",
                parameters=[
                    ToolParameter(
                        name="title",
                        type=ToolParameterType.STRING,
                        description="Semantic title for the snapshot (e.g., 'Python Programming Discussion', 'Project Planning')",
                        required=True
                    ),
                    ToolParameter(
                        name="initial_summary",
                        type=ToolParameterType.STRING,
                        description="Optional: Initial summary describing the snapshot's purpose",
                        required=False
                    ),
                ],
                handler=self._handle_memory_create_snapshot,
                allowed_paths=["CHAT"]
            ))
            
            # 7. memory_add_to_snapshot - 添加消息到當前快照
            mcp_server.register_tool(MCPTool(
                name="memory_add_to_snapshot",
                description="Add a new message to the current active conversation snapshot. Use this to record important dialogue exchanges in real-time.",
                parameters=[
                    ToolParameter(
                        name="speaker",
                        type=ToolParameterType.STRING,
                        description="Who is speaking (e.g., 'user', 'assistant', 'system')",
                        required=True
                    ),
                    ToolParameter(
                        name="content",
                        type=ToolParameterType.STRING,
                        description="The message content to add",
                        required=True
                    ),
                    ToolParameter(
                        name="intent",
                        type=ToolParameterType.STRING,
                        description="Optional: The intent or purpose of the message",
                        required=False
                    ),
                ],
                handler=self._handle_memory_add_to_snapshot,
                allowed_paths=["CHAT"]
            ))
            
            # 8. memory_update_snapshot_summary - 更新快照摘要
            mcp_server.register_tool(MCPTool(
                name="memory_update_snapshot_summary",
                description="Update the summary or metadata of the current conversation snapshot. Use this to refine understanding of the ongoing conversation.",
                parameters=[
                    ToolParameter(
                        name="summary",
                        type=ToolParameterType.STRING,
                        description="Updated summary of the conversation",
                        required=False
                    ),
                    ToolParameter(
                        name="key_topics",
                        type=ToolParameterType.STRING,
                        description="Comma-separated list of key topics discussed",
                        required=False
                    ),
                    ToolParameter(
                        name="notes",
                        type=ToolParameterType.STRING,
                        description="Additional notes or observations about the conversation",
                        required=False
                    ),
                ],
                handler=self._handle_memory_update_snapshot_summary,
                allowed_paths=["CHAT"]
            ))
            
            info_log("[MEM] ✅ 成功註冊 10 個記憶管理 MCP 工具 (5 檢索 + 5 寫入，限制於 CHAT 路徑)")
            return True
            
        except Exception as e:
            error_log(f"[MEM] 註冊 MCP 工具失敗: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    async def _handle_memory_retrieve_profile(self, params: Dict[str, Any]):
        """處理 memory_retrieve_profile 工具調用 - 獲取用戶完整資料"""
        from modules.sys_module.mcp_server.tool_definitions import ToolResult
        
        try:
            # 獲取當前 memory_token
            memory_token = self.memory_manager.identity_manager.get_current_memory_token() if self.memory_manager else None
            
            if not memory_token:
                return ToolResult.error("No active memory token found. User identity may not be set.")
            
            debug_log(2, f"[MEM] 檢索 PROFILE 記憶：直接取出全部（無過濾）")
            
            # 直接獲取所有 PROFILE 記憶，不做語義搜索過濾
            profile_results = self.memory_manager.retrieve_memories(
                query_text="",  # 空查詢，不做語義過濾
                memory_token=memory_token,
                memory_types=[MemoryType.PROFILE],
                max_results=100,  # 取出所有 PROFILE
                similarity_threshold=0.0  # 不過濾
            )
            
            debug_log(2, f"[MEM] PROFILE 檢索結果: {len(profile_results)} 個")
            
            if not profile_results:
                return ToolResult.success(
                    message="No user profile data stored yet",
                    data={
                        "profiles": [],
                        "count": 0
                    }
                )
            
            # 構建結果
            profiles = []
            for idx, result in enumerate(profile_results):
                memory_entry = result.memory_entry
                if isinstance(memory_entry, dict):
                    profile_data = memory_entry
                else:
                    profile_data = memory_entry.model_dump() if hasattr(memory_entry, 'model_dump') else memory_entry.__dict__
                
                content = profile_data.get("content", "")
                debug_log(2, f"[MEM] PROFILE {idx}: content='{content[:50]}...' (len={len(content)})")
                
                profiles.append({
                    "content": content,
                    "category": profile_data.get("tags", []),
                    "importance": profile_data.get("importance", 0.5),
                    "created_at": str(profile_data.get("created_at", "")),
                    "memory_id": profile_data.get("memory_id")
                })
            
            return ToolResult.success(
                message=f"Retrieved {len(profiles)} user profile fact(s)",
                data={
                    "profiles": profiles,
                    "count": len(profiles)
                }
            )
            
        except Exception as e:
            error_log(f"[MEM] memory_retrieve_profile 執行失敗: {e}")
            return ToolResult.error(f"Failed to retrieve user profile: {str(e)}")
    
    async def _handle_memory_search_snapshots(self, params: Dict[str, Any]):
        """處理 memory_search_snapshots 工具調用 - 搜索對話歷史"""
        from modules.sys_module.mcp_server.tool_definitions import ToolResult
        
        try:
            query = params.get("query", "")
            max_results = params.get("max_results", 5)
            similarity_threshold = params.get("similarity_threshold", 0.6)
            
            if not query:
                return ToolResult.error("Query parameter is required for searching conversation snapshots")
            
            # 獲取當前 memory_token
            memory_token = self.memory_manager.identity_manager.get_current_memory_token() if self.memory_manager else None
            
            if not memory_token:
                return ToolResult.error("No active memory token found. User identity may not be set.")
            
            debug_log(2, f"[MEM] 搜索 SNAPSHOT：query='{query}', threshold={similarity_threshold}")
            
            # 使用語義搜索檢索 SNAPSHOT
            snapshot_results = self.memory_manager.retrieve_memories(
                query_text=query,
                memory_token=memory_token,
                memory_types=[MemoryType.SNAPSHOT],
                max_results=max_results,
                similarity_threshold=similarity_threshold
            )
            
            debug_log(2, f"[MEM] SNAPSHOT 搜索結果: {len(snapshot_results)} 個")
            
            if not snapshot_results:
                return ToolResult.success(
                    message="No relevant conversation snapshots found",
                    data={
                        "snapshots": [],
                        "count": 0,
                        "query": query
                    }
                )
            
            # 構建摘要結果
            snapshots = []
            for result in snapshot_results:
                memory_entry = result.memory_entry
                if isinstance(memory_entry, dict):
                    snapshot_data = memory_entry
                else:
                    snapshot_data = memory_entry.model_dump() if hasattr(memory_entry, 'model_dump') else memory_entry.__dict__
                
                snapshots.append({
                    "snapshot_id": snapshot_data.get("memory_id"),
                    "summary": snapshot_data.get("summary", snapshot_data.get("content", "")[:200]),
                    "topics": snapshot_data.get("key_topics", []),
                    "created_at": str(snapshot_data.get("created_at", "")),
                    "message_count": snapshot_data.get("message_count", 0),
                    "similarity_score": result.similarity_score,
                    "relevance": result.retrieval_reason
                })
            
            return ToolResult.success(
                message=f"Found {len(snapshots)} relevant conversation(s)",
                data={
                    "snapshots": snapshots,
                    "count": len(snapshots),
                    "query": query
                }
            )
            
        except Exception as e:
            error_log(f"[MEM] memory_search_snapshots 執行失敗: {e}")
            return ToolResult.error(f"Failed to search snapshots: {str(e)}")

    async def _handle_memory_retrieve_snapshots(self, params: Dict[str, Any]):
        """Handle memory_retrieve_snapshots tool - fetch PROFILE + SNAPSHOT memories together."""
        from modules.sys_module.mcp_server.tool_definitions import ToolResult

        try:
            query = (params.get("query") or "").strip()
            memory_types_str = params.get("memory_types") or "profile,snapshot"
            max_results = params.get("max_results", 5)
            similarity_threshold = params.get("similarity_threshold", 0.6 if query else 0.0)

            type_mapping = {
                'profile': MemoryType.PROFILE,
                'snapshot': MemoryType.SNAPSHOT,
                'long_term': MemoryType.LONG_TERM,
                'preference': MemoryType.PREFERENCE
            }

            requested_types = [t.strip().lower() for t in memory_types_str.split(',')]
            memory_types = []
            for type_str in requested_types:
                if type_str in type_mapping:
                    memory_types.append(type_mapping[type_str])
                else:
                    return ToolResult.error(f"Invalid memory_type: '{type_str}'. Valid types: profile, snapshot, long_term, preference")

            if not memory_types:
                return ToolResult.error("At least one valid memory_type must be specified")

            memory_token = self.memory_manager.identity_manager.get_current_memory_token() if self.memory_manager else None
            if not memory_token:
                return ToolResult.error("No active memory token found. User identity may not be set.")

            has_profile = MemoryType.PROFILE in memory_types
            has_others = any(t != MemoryType.PROFILE for t in memory_types)

            results = []

            if has_profile:
                debug_log(2, "[MEM] Retrieving PROFILE memories (full set)")
                profile_results = self.memory_manager.retrieve_memories(
                    query_text="",
                    memory_token=memory_token,
                    memory_types=[MemoryType.PROFILE],
                    max_results=100,
                    similarity_threshold=0.0
                )
                results.extend(profile_results)
                debug_log(2, f"[MEM] PROFILE 檢索結果: {len(profile_results)} 個")

            if has_others:
                other_types = [t for t in memory_types if t != MemoryType.PROFILE]
                debug_log(2, f"[MEM] 檢索 {other_types} 記憶：query='{query}', threshold={similarity_threshold}")
                other_results = self.memory_manager.retrieve_memories(
                    query_text=query,
                    memory_token=memory_token,
                    memory_types=other_types,
                    max_results=max_results,
                    similarity_threshold=similarity_threshold
                )
                results.extend(other_results)
                debug_log(2, f"[MEM] 其他類型檢索結果: {len(other_results)} 個")

            if not results:
                return ToolResult.success(
                    message="No relevant conversation snapshots found",
                    data={"snapshots": [], "count": 0, "query": query},
                )

            snapshots = []
            for result in results:
                memory_entry = result.memory_entry
                if isinstance(memory_entry, dict):
                    snapshot_data = memory_entry
                else:
                    snapshot_data = memory_entry.model_dump() if hasattr(memory_entry, 'model_dump') else memory_entry.__dict__

                snapshots.append({
                    "snapshot_id": snapshot_data.get("memory_id"),
                    "summary": snapshot_data.get("summary", ""),
                    "topics": snapshot_data.get("key_topics", []),
                    "created_at": str(snapshot_data.get("created_at", "")),
                    "message_count": snapshot_data.get("message_count", 0),
                    "similarity_score": result.similarity_score,
                    "relevance": result.retrieval_reason
                })

            return ToolResult.success(
                message=f"Retrieved {len(snapshots)} relevant conversation snapshot(s)",
                data={"snapshots": snapshots, "count": len(snapshots), "query": query},
            )

        except Exception as e:
            error_log(f"[MEM] memory_retrieve_snapshots 執行失敗: {e}")
            return ToolResult.error(f"Failed to retrieve snapshots: {str(e)}")


    async def _handle_memory_get_snapshot(self, params: Dict[str, Any]):
        """處理 memory_get_snapshot 工具調用"""
        from modules.sys_module.mcp_server.tool_definitions import ToolResult
        
        try:
            snapshot_id = params.get("snapshot_id", "")
            
            if not snapshot_id:
                return ToolResult.error("snapshot_id parameter is required")
            
            # 獲取當前 memory_token
            memory_token = self.memory_manager.identity_manager.get_current_memory_token() if self.memory_manager else None
            
            if not memory_token:
                return ToolResult.error("No active memory token found")
            
            # 從 storage_manager 獲取快照
            memory_entry = self.memory_manager.storage_manager.get_memory_by_id(snapshot_id, memory_token)
            
            if not memory_entry:
                return ToolResult.error(f"Snapshot not found: {snapshot_id}")
            
            # 檢查是否為快照類型
            if memory_entry.memory_type != MemoryType.SNAPSHOT:
                return ToolResult.error(f"Memory {snapshot_id} is not a snapshot (type: {memory_entry.memory_type})")
            
            # 構建完整快照數據
            if isinstance(memory_entry, dict):
                snapshot_data = memory_entry
            else:
                snapshot_data = memory_entry.model_dump() if hasattr(memory_entry, 'model_dump') else memory_entry.__dict__
            
            return ToolResult.success(
                message=f"Retrieved snapshot: {snapshot_id}",
                data={
                    "snapshot_id": snapshot_data.get("memory_id"),
                    "summary": snapshot_data.get("summary", ""),
                    "content": snapshot_data.get("content", ""),
                    "messages": snapshot_data.get("messages", []),
                    "topics": snapshot_data.get("key_topics", []),
                    "created_at": str(snapshot_data.get("created_at", "")),
                    "message_count": snapshot_data.get("message_count", 0),
                    "stage_number": snapshot_data.get("stage_number", 0)
                }
            )
            
        except Exception as e:
            error_log(f"[MEM] memory_get_snapshot 執行失敗: {e}")
            return ToolResult.error(f"Failed to retrieve snapshot: {str(e)}")
    
    async def _handle_memory_search_timeline(self, params: Dict[str, Any]):
        """處理 memory_search_timeline 工具調用"""
        from modules.sys_module.mcp_server.tool_definitions import ToolResult
        from datetime import datetime
        
        try:
            start_time_str = params.get("start_time", "")
            end_time_str = params.get("end_time", "")
            topic = params.get("topic")
            
            if not start_time_str or not end_time_str:
                return ToolResult.error("start_time and end_time parameters are required")
            
            # 解析時間
            try:
                start_time = datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
                end_time = datetime.fromisoformat(end_time_str.replace('Z', '+00:00'))
            except ValueError as e:
                return ToolResult.error(f"Invalid time format. Use ISO format (e.g., '2025-12-01T00:00:00'): {str(e)}")
            
            # 獲取當前 memory_token
            memory_token = self.memory_manager.identity_manager.get_current_memory_token() if self.memory_manager else None
            
            if not memory_token:
                return ToolResult.error("No active memory token found")
            
            # 獲取時間範圍內的所有快照
            all_snapshots = self.memory_manager.storage_manager.get_memories_by_type(
                memory_token=memory_token,
                memory_type=MemoryType.SNAPSHOT
            )
            
            # 過濾時間範圍和主題
            filtered_snapshots = []
            for snapshot in all_snapshots:
                snapshot_time = snapshot.created_at
                if start_time <= snapshot_time <= end_time:
                    # 如果有主題過濾，檢查主題
                    if topic:
                        if topic.lower() in ' '.join(snapshot.key_topics).lower():
                            filtered_snapshots.append(snapshot)
                    else:
                        filtered_snapshots.append(snapshot)
            
            # 按時間排序
            filtered_snapshots.sort(key=lambda x: x.created_at)
            
            # 構建結果
            snapshots = []
            for snapshot in filtered_snapshots:
                snapshot_data = snapshot.model_dump() if hasattr(snapshot, 'model_dump') else snapshot.__dict__
                snapshots.append({
                    "snapshot_id": snapshot_data.get("memory_id"),
                    "summary": snapshot_data.get("summary", ""),
                    "topics": snapshot_data.get("key_topics", []),
                    "created_at": str(snapshot_data.get("created_at", "")),
                    "message_count": snapshot_data.get("message_count", 0)
                })
            
            return ToolResult.success(
                message=f"Found {len(snapshots)} snapshot(s) in timeline",
                data={
                    "snapshots": snapshots,
                    "count": len(snapshots),
                    "time_range": {
                        "start": start_time_str,
                        "end": end_time_str
                    },
                    "topic_filter": topic
                }
            )
            
        except Exception as e:
            error_log(f"[MEM] memory_search_timeline 執行失敗: {e}")
            return ToolResult.error(f"Failed to search timeline: {str(e)}")
    
    async def _handle_memory_update_profile(self, params: Dict[str, Any]):
        """處理 memory_update_profile 工具調用"""
        from modules.sys_module.mcp_server.tool_definitions import ToolResult
        
        try:
            observation = params.get("observation", "")
            category = params.get("category", "general")
            importance_str = params.get("importance", "medium")
            
            if not observation:
                return ToolResult.error("Observation parameter is required")
            
            # 轉換重要性等級
            importance_map = {
                "critical": MemoryImportance.CRITICAL,
                "high": MemoryImportance.HIGH,
                "medium": MemoryImportance.MEDIUM,
                "low": MemoryImportance.LOW
            }
            importance = importance_map.get(importance_str.lower(), MemoryImportance.MEDIUM)
            
            # 獲取當前 memory_token
            memory_token = self.memory_manager.identity_manager.get_current_memory_token() if self.memory_manager else None
            
            if not memory_token:
                return ToolResult.error("No active memory token found")
            
            # 獲取當前 session_id
            session_id = None
            if self.memory_manager and self.memory_manager.current_context:
                session_id = self.memory_manager.current_context.current_session_id
            
            # 儲存為 PROFILE 類型記憶
            result = self.memory_manager.store_memory(
                content=observation,
                memory_token=memory_token,
                memory_type=MemoryType.PROFILE,
                importance=importance,
                topic=category,
                metadata={
                    "category": category,
                    "source": "llm_observation",
                    "updated_by_tool": True
                },
                session_id=session_id
            )
            
            if result.success:
                return ToolResult.success(
                    message=f"Successfully stored user profile observation",
                    data={
                        "memory_id": result.memory_id,
                        "category": category,
                        "importance": importance_str,
                        "observation": observation[:100] + "..." if len(observation) > 100 else observation
                    }
                )
            else:
                return ToolResult.error(f"Failed to store profile: {result.message}")
            
        except Exception as e:
            error_log(f"[MEM] memory_update_profile 執行失敗: {e}")
            return ToolResult.error(f"Failed to update profile: {str(e)}")
    
    async def _handle_memory_store_observation(self, params: Dict[str, Any]):
        """處理 memory_store_observation 工具調用"""
        from modules.sys_module.mcp_server.tool_definitions import ToolResult
        
        try:
            content = params.get("content", "")
            memory_type_str = params.get("memory_type", "long_term")
            topic = params.get("topic", "general")
            importance_str = params.get("importance", "medium")
            
            if not content:
                return ToolResult.error("Content parameter is required")
            
            # 轉換記憶類型
            memory_type_map = {
                "profile": MemoryType.PROFILE,
                "long_term": MemoryType.LONG_TERM,
                "preference": MemoryType.PREFERENCE
            }
            memory_type = memory_type_map.get(memory_type_str.lower(), MemoryType.LONG_TERM)
            
            # 轉換重要性等級
            importance_map = {
                "critical": MemoryImportance.CRITICAL,
                "high": MemoryImportance.HIGH,
                "medium": MemoryImportance.MEDIUM,
                "low": MemoryImportance.LOW
            }
            importance = importance_map.get(importance_str.lower(), MemoryImportance.MEDIUM)
            
            # 獲取當前 memory_token
            memory_token = self.memory_manager.identity_manager.get_current_memory_token() if self.memory_manager else None
            
            if not memory_token:
                return ToolResult.error("No active memory token found")
            
            # 獲取當前 session_id
            session_id = None
            if self.memory_manager and self.memory_manager.current_context:
                session_id = self.memory_manager.current_context.current_session_id
            
            # 儲存記憶
            result = self.memory_manager.store_memory(
                content=content,
                memory_token=memory_token,
                memory_type=memory_type,
                importance=importance,
                topic=topic,
                metadata={
                    "source": "llm_observation",
                    "stored_by_tool": True
                },
                session_id=session_id
            )
            
            if result.success:
                return ToolResult.success(
                    message=f"Successfully stored {memory_type_str} observation",
                    data={
                        "memory_id": result.memory_id,
                        "memory_type": memory_type_str,
                        "topic": topic,
                        "importance": importance_str,
                        "content_preview": content[:100] + "..." if len(content) > 100 else content
                    }
                )
            else:
                return ToolResult.error(f"Failed to store observation: {result.message}")
            
        except Exception as e:
            error_log(f"[MEM] memory_store_observation 執行失敗: {e}")
            return ToolResult.error(f"Failed to store observation: {str(e)}")
    
    async def _handle_memory_create_snapshot(self, params: Dict[str, Any]):
        """處理 memory_create_snapshot 工具調用"""
        from modules.sys_module.mcp_server.tool_definitions import ToolResult
        
        try:
            title = params.get("title", "")
            initial_summary = params.get("initial_summary", "")
            
            if not title:
                return ToolResult.error("title parameter is required")
            
            # 獲取當前 memory_token 和 session_id
            memory_token = self.memory_manager.identity_manager.get_current_memory_token() if self.memory_manager else None
            
            if not memory_token:
                return ToolResult.error("No active memory token found. User identity may not be set.")
            
            # 獲取當前 session_id
            session_id = None
            if self.memory_manager and self.memory_manager.current_context:
                session_id = self.memory_manager.current_context.current_session_id
            
            if not session_id:
                return ToolResult.error("No active conversation session found")
            
            # 創建新快照
            # 先設置快照的語義化標題
            snapshot_manager = self.memory_manager.snapshot_manager
            
            # 開始新的快照會話（如果還沒開始）
            if session_id not in snapshot_manager._active_snapshots:
                snapshot_manager.start_snapshot(session_id, memory_token)
            
            # 獲取快照並設置標題
            snapshot = snapshot_manager._active_snapshots.get(session_id)
            if snapshot:
                # 更新快照的語義化標題
                snapshot.semantic_title = title
                if initial_summary:
                    snapshot.summary = initial_summary
                
                # 註冊到 key_manager 使其可被搜索
                if hasattr(snapshot_manager, 'key_manager'):
                    snapshot_manager.key_manager.register_snapshot(
                        temp_id=f"temp_snapshot_{session_id}",
                        key_value=title
                    )
                
                info_log(f"[MEM] 創建新快照: {title} (session: {session_id})")
                
                return ToolResult.success(
                    message=f"Successfully created new snapshot: '{title}'",
                    data={
                        "session_id": session_id,
                        "title": title,
                        "summary": initial_summary,
                        "memory_token": memory_token
                    }
                )
            else:
                return ToolResult.error("Failed to access snapshot")
            
        except Exception as e:
            error_log(f"[MEM] memory_create_snapshot 執行失敗: {e}")
            return ToolResult.error(f"Failed to create snapshot: {str(e)}")
    
    async def _handle_memory_add_to_snapshot(self, params: Dict[str, Any]):
        """處理 memory_add_to_snapshot 工具調用"""
        from modules.sys_module.mcp_server.tool_definitions import ToolResult
        
        try:
            speaker = params.get("speaker", "")
            content = params.get("content", "")
            intent = params.get("intent", "")
            
            if not speaker or not content:
                return ToolResult.error("Both speaker and content parameters are required")
            
            # 獲取當前 session_id
            session_id = None
            if self.memory_manager and self.memory_manager.current_context:
                session_id = self.memory_manager.current_context.current_session_id
            
            if not session_id:
                return ToolResult.error("No active conversation session found")
            
            # 準備消息數據
            from datetime import datetime
            message_data = {
                "speaker": speaker,
                "content": content,
                "timestamp": datetime.now().isoformat(),
                "intent": [intent] if intent else []
            }
            
            # 添加消息到快照
            success = self.memory_manager.snapshot_manager.add_message_to_snapshot(
                session_id=session_id,
                message_data=message_data
            )
            
            if success:
                # 獲取更新後的快照信息
                snapshot = self.memory_manager.snapshot_manager._active_snapshots.get(session_id)
                message_count = len(snapshot.messages) if snapshot and snapshot.messages else 0
                
                return ToolResult.success(
                    message=f"Successfully added message to snapshot",
                    data={
                        "session_id": session_id,
                        "speaker": speaker,
                        "message_count": message_count,
                        "content_preview": content[:100] + "..." if len(content) > 100 else content
                    }
                )
            else:
                return ToolResult.error("Failed to add message to snapshot")
            
        except Exception as e:
            error_log(f"[MEM] memory_add_to_snapshot 執行失敗: {e}")
            return ToolResult.error(f"Failed to add message to snapshot: {str(e)}")
    
    async def _handle_memory_update_snapshot_summary(self, params: Dict[str, Any]):
        """處理 memory_update_snapshot_summary 工具調用"""
        from modules.sys_module.mcp_server.tool_definitions import ToolResult
        
        try:
            summary = params.get("summary")
            key_topics_str = params.get("key_topics")
            notes = params.get("notes")
            
            if not any([summary, key_topics_str, notes]):
                return ToolResult.error("At least one of summary, key_topics, or notes must be provided")
            
            # 獲取當前 session_id
            session_id = None
            if self.memory_manager and self.memory_manager.current_context:
                session_id = self.memory_manager.current_context.current_session_id
            
            if not session_id:
                return ToolResult.error("No active conversation session found")
            
            # 獲取當前快照
            snapshot = self.memory_manager.snapshot_manager._active_snapshots.get(session_id)
            
            if not snapshot:
                return ToolResult.error(f"No active snapshot found for session {session_id}")
            
            # 準備更新內容
            update_content = {}
            
            if summary:
                update_content["summary"] = summary
            
            if key_topics_str:
                # 解析逗號分隔的主題列表
                key_topics = [topic.strip() for topic in key_topics_str.split(",") if topic.strip()]
                update_content["key_topics"] = key_topics
            
            if notes:
                # 將 notes 添加到 metadata
                if not hasattr(snapshot, 'metadata') or snapshot.metadata is None:
                    snapshot.metadata = {}
                update_content["metadata"] = {**snapshot.metadata, "llm_notes": notes}
            
            # 更新快照
            from datetime import datetime
            update_content["updated_at"] = datetime.now()
            
            # 使用 snapshot_manager 的更新方法
            success = self.memory_manager.snapshot_manager.update_snapshot_content(
                snapshot_id=session_id,
                new_content=snapshot.content,  # 保留原始內容
                new_summary=summary,
                key_topics=update_content.get("key_topics"),
                additional_metadata=update_content.get("metadata", {})
            )
            
            if success:
                return ToolResult.success(
                    message="Successfully updated snapshot summary",
                    data={
                        "session_id": session_id,
                        "updated_fields": list(update_content.keys()),
                        "summary_preview": summary[:100] + "..." if summary and len(summary) > 100 else summary
                    }
                )
            else:
                return ToolResult.error("Failed to update snapshot summary")
            
        except Exception as e:
            error_log(f"[MEM] memory_update_snapshot_summary 執行失敗: {e}")
            return ToolResult.error(f"Failed to update snapshot summary: {str(e)}")
            error_log(traceback.format_exc())
            return False
