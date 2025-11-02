"""
模組調用協調器 - 負責系統層的模組間調用邏輯
實現完整的三層架構：輸入層 → 處理層 → 輸出層
根據 docs/完整系統流程文檔.md 中定義的分層架構

=== Flow-Based 去重機制 ===
為防止事件重複處理,採用 flow-based 去重策略:

1. Flow ID = session_id + cycle_index
   - session_id: GS (General Session) ID,不是模組層級的 CS/WS
   - cycle_index: 系統循環計數器 (每次對話往返 +1)

2. Dedupe Key = flow_id + layer
   格式: "{session_id}:{cycle_index}:{layer}"
   示例: "gs_20231013_001:7:PROCESSING"

3. 生命週期管理:
   - CYCLE_COMPLETED: 清理當前 cycle 的所有 layer 鍵
   - SESSION_ENDED: 清理整個 session 的所有鍵
   - 保護性清理: 超過 2000 個鍵時自動清理一半

4. 會話層級說明:
   - GS (General Session): 系統級會話,跨越整個對話生命週期
   - CS (Chatting Session): 對話會話,MEM 模組使用
   - WS (Workflow Session): 工作流會話,LLM 和 SYS 模組使用
   - 本協調器使用 GS 作為去重的 session_id
   
註: LLM 是兩個邏輯系統 (對話/工作流) 的中樞
"""

import time
import threading
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from enum import Enum

from utils.debug_helper import debug_log, info_log, error_log


class ProcessingLayer(Enum):
    """處理層級定義"""
    INPUT = "input"          # 輸入層：STT, NLP
    PROCESSING = "processing"  # 處理層：MEM, LLM, SYS
    OUTPUT = "output"        # 輸出層：TTS


class InvocationResult(Enum):
    """調用結果狀態"""
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    NO_TARGET = "no_target"


@dataclass
class LayerTransition:
    """層級轉換請求"""
    from_layer: ProcessingLayer
    to_layer: ProcessingLayer
    data: Dict[str, Any]
    source_module: str
    reasoning: str


@dataclass
class ModuleInvocationRequest:
    """模組調用請求"""
    target_module: str
    input_data: Dict[str, Any]
    source_module: str
    reasoning: str
    layer: ProcessingLayer
    priority: int = 1
    timeout: float = 30.0


@dataclass
class ModuleInvocationResponse:
    """模組調用回應"""
    target_module: str
    result: InvocationResult
    layer: ProcessingLayer
    output_data: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    execution_time: float = 0.0


class ModuleInvocationCoordinator:
    """模組調用協調器 - 實現完整三層架構的模組間調用管理"""
    
    # 定義模組所屬層級
    MODULE_LAYERS = {
        'stt': ProcessingLayer.INPUT,
        'nlp': ProcessingLayer.INPUT,
        'mem': ProcessingLayer.PROCESSING,
        'llm': ProcessingLayer.PROCESSING,
        'sys': ProcessingLayer.PROCESSING,
        'tts': ProcessingLayer.OUTPUT
    }
    
    def __init__(self):
        """初始化協調器"""
        self._invocation_lock = threading.Lock()
        self._active_invocations = {}
        self._invocation_history = []
        self._layer_transitions = []
        
        # 新的去重機制: flow_id + layer
        # dedupe_key = f"{session_id}:{cycle_index}:{layer}"
        self._layer_dedupe_keys = set()  # 層級去重集合
        self._dedupe_lock = threading.Lock()
        self._max_dedupe_keys = 2000  # 最多保留 2000 個去重鍵
        
        # 統計信息
        self._dedupe_hit_count = 0
        self._cleanup_count = 0
        
        # 會話結束管理 - 雙條件終止機制
        self._pending_session_end = None  # 標記會話結束請求，等待 CYCLE_COMPLETED
        
        info_log("[ModuleCoordinator] 三層架構模組調用協調器初始化")
        info_log("[ModuleCoordinator] 使用 flow-based 去重策略 (session_id:cycle_index:layer)")
        
        # ✅ 訂閱事件總線
        self._setup_event_subscriptions()
    
    def _setup_event_subscriptions(self):
        """設置事件訂閱 - 事件驅動架構的核心"""
        try:
            from core.event_bus import event_bus, SystemEvent
            
            info_log("[ModuleCoordinator] 開始訂閱事件總線...")
            
            # 訂閱輸入層完成事件
            event_bus.subscribe(
                SystemEvent.INPUT_LAYER_COMPLETE,
                self._on_input_layer_complete,
                handler_name="ModuleCoordinator.input_complete"
            )
            info_log(f"[ModuleCoordinator] ✓ 已訂閱 INPUT_LAYER_COMPLETE")
            
            # 訂閱處理層完成事件
            event_bus.subscribe(
                SystemEvent.PROCESSING_LAYER_COMPLETE,
                self._on_processing_layer_complete,
                handler_name="ModuleCoordinator.processing_complete"
            )
            info_log(f"[ModuleCoordinator] ✓ 已訂閱 PROCESSING_LAYER_COMPLETE")
            
            # 訂閱循環完成事件 (用於清理去重鍵)
            event_bus.subscribe(
                SystemEvent.CYCLE_COMPLETED,
                self._on_cycle_completed,
                handler_name="ModuleCoordinator.cycle_complete"
            )
            info_log(f"[ModuleCoordinator] ✓ 已訂閱 CYCLE_COMPLETED")
            
            # 訂閱會話結束事件 (用於清理去重鍵)
            event_bus.subscribe(
                SystemEvent.SESSION_ENDED,
                self._on_session_ended,
                handler_name="ModuleCoordinator.session_end"
            )
            info_log(f"[ModuleCoordinator] ✓ 已訂閱 SESSION_ENDED")
            
            info_log("[ModuleCoordinator] ✅ 事件訂閱完成 (4 個事件)")
            
        except Exception as e:
            error_log(f"[ModuleCoordinator] ❌ 事件訂閱失敗: {e}")
            import traceback
            traceback.print_exc()
    
    def _on_input_layer_complete(self, event):
        """
        輸入層完成事件處理器
        當 NLP 發布 INPUT_LAYER_COMPLETE 事件時觸發
        使用 flow-based 去重: session_id + cycle_index + layer
        
        注意: 這裡的 session_id 是 GS (General Session),
              不是模組層級的 CS (MEM) 或 WS (LLM/SYS)
        """
        try:
            # 提取 flow 識別資訊 (來自 General Session)
            session_id = event.data.get('session_id', 'unknown')  # GS ID
            cycle_index = event.data.get('cycle_index', -1)
            
            # 構建去重鍵: flow_id + layer
            dedupe_key = f"{session_id}:{cycle_index}:INPUT"
            
            # 檢查是否已處理過此 flow 的此 layer
            with self._dedupe_lock:
                if dedupe_key in self._layer_dedupe_keys:
                    self._dedupe_hit_count += 1
                    debug_log(2, f"[ModuleCoordinator] ⚠️ 跳過重複處理 (dedupe_key={dedupe_key}, 命中次數={self._dedupe_hit_count})")
                    return
                
                # 標記為已處理
                self._layer_dedupe_keys.add(dedupe_key)
                debug_log(3, f"[ModuleCoordinator] ✓ 已記錄去重鍵: {dedupe_key} (共 {len(self._layer_dedupe_keys)} 個)")
                
                # 保護性清理: 避免集合無限增長
                if len(self._layer_dedupe_keys) > self._max_dedupe_keys:
                    removed_count = len(self._layer_dedupe_keys) - (self._max_dedupe_keys // 2)
                    keys_to_remove = list(self._layer_dedupe_keys)[:(self._max_dedupe_keys // 2)]
                    for old_key in keys_to_remove:
                        self._layer_dedupe_keys.discard(old_key)
                    self._cleanup_count += removed_count
                    debug_log(3, f"[ModuleCoordinator] 保護性清理: 移除 {removed_count} 個舊鍵")
            
            info_log(f"[ModuleCoordinator] 🎯 收到輸入層完成事件 (flow={session_id}:{cycle_index}, event_id={event.event_id})")
            debug_log(2, f"[ModuleCoordinator] 事件數據: {list(event.data.keys())}")
            self.handle_layer_completion(ProcessingLayer.INPUT, event.data)
                    
        except Exception as e:
            error_log(f"[ModuleCoordinator] ❌ 處理輸入層完成事件失敗: {e}")
            import traceback
            traceback.print_exc()
    
    def _on_processing_layer_complete(self, event):
        """
        處理層完成事件處理器
        當 LLM 發布 PROCESSING_LAYER_COMPLETE 事件時觸發
        使用 flow-based 去重: session_id + cycle_index + layer
        
        注意: 這裡的 session_id 是 GS (General Session),
              不是模組層級的 CS (MEM) 或 WS (LLM/SYS)
        """
        try:
            # 提取 flow 識別資訊 (來自 General Session)
            session_id = event.data.get('session_id', 'unknown')  # GS ID
            cycle_index = event.data.get('cycle_index', -1)
            
            # 構建去重鍵: flow_id + layer
            dedupe_key = f"{session_id}:{cycle_index}:PROCESSING"
            
            # 檢查是否已處理過此 flow 的此 layer
            with self._dedupe_lock:
                if dedupe_key in self._layer_dedupe_keys:
                    self._dedupe_hit_count += 1
                    debug_log(2, f"[ModuleCoordinator] ⚠️ 跳過重複處理 (dedupe_key={dedupe_key}, 命中次數={self._dedupe_hit_count})")
                    return
                
                # 標記為已處理
                self._layer_dedupe_keys.add(dedupe_key)
                debug_log(3, f"[ModuleCoordinator] ✓ 已記錄去重鍵: {dedupe_key} (共 {len(self._layer_dedupe_keys)} 個)")
                
                # 保護性清理
                if len(self._layer_dedupe_keys) > self._max_dedupe_keys:
                    removed_count = len(self._layer_dedupe_keys) - (self._max_dedupe_keys // 2)
                    keys_to_remove = list(self._layer_dedupe_keys)[:(self._max_dedupe_keys // 2)]
                    for old_key in keys_to_remove:
                        self._layer_dedupe_keys.discard(old_key)
                    self._cleanup_count += removed_count
                    debug_log(3, f"[ModuleCoordinator] 保護性清理: 移除 {removed_count} 個舊鍵")
            
            info_log(f"[ModuleCoordinator] 🎯 收到處理層完成事件 (flow={session_id}:{cycle_index}, event_id={event.event_id})")
            debug_log(2, f"[ModuleCoordinator] 事件數據: {list(event.data.keys())}")
            self.handle_layer_completion(ProcessingLayer.PROCESSING, event.data)
                    
        except Exception as e:
            error_log(f"[ModuleCoordinator] ❌ 處理處理層完成事件失敗: {e}")
            import traceback
            traceback.print_exc()
    
    def _on_cycle_completed(self, event):
        """
        循環完成事件處理器
        
        處理兩個任務：
        1. 清理去重鍵 - 移除已完成 cycle 的所有 layer 鍵
        2. 檢查會話結束 - 雙條件終止機制的第二個條件
        
        注意: 這裡的 session_id 是 GS (General Session)
        """
        try:
            session_id = event.data.get('session_id', 'unknown')  # GS ID
            cycle_index = event.data.get('cycle_index', -1)
            flow_prefix = f"{session_id}:{cycle_index}:"
            
            # 任務 1: 清理去重鍵
            with self._dedupe_lock:
                # 找出並移除此 flow 的所有 layer 鍵
                keys_to_remove = [k for k in self._layer_dedupe_keys if k.startswith(flow_prefix)]
                for key in keys_to_remove:
                    self._layer_dedupe_keys.discard(key)
                
                self._cleanup_count += len(keys_to_remove)
                info_log(f"[ModuleCoordinator] 🧹 CYCLE_COMPLETED 清理: 移除 {len(keys_to_remove)} 個去重鍵 (flow={session_id}:{cycle_index})")
                debug_log(3, f"[ModuleCoordinator] 剩餘去重鍵數量: {len(self._layer_dedupe_keys)}")
            
            # 任務 2: 檢查會話結束請求（雙條件終止機制）
            if self._pending_session_end:
                pending = self._pending_session_end
                pending_gs_id = pending.get('gs_id')
                
                # 檢查是否是同一個 GS
                if pending_gs_id == session_id:
                    reason = pending.get('reason', 'LLM requested')
                    info_log(f"[ModuleCoordinator] ✅ 雙條件終止機制滿足：")
                    info_log(f"  └─ 條件 1: 外部中斷點 (LLM session_control) ✅")
                    info_log(f"  └─ 條件 2: 循環結束 (CYCLE_COMPLETED) ✅")
                    info_log(f"[ModuleCoordinator] 現在執行會話結束 (gs_id={session_id}, reason={reason})")
                    
                    # 執行會話結束
                    self._handle_session_end(pending.get('session_control'))
                    
                    # 清除標記
                    self._pending_session_end = None
                else:
                    debug_log(2, f"[ModuleCoordinator] 會話結束標記的 gs_id ({pending_gs_id}) 與當前循環的 gs_id ({session_id}) 不匹配，繼續等待")
                
        except Exception as e:
            error_log(f"[ModuleCoordinator] ❌ 處理循環完成事件失敗: {e}")
            import traceback
            traceback.print_exc()
    
    def _on_session_ended(self, event):
        """
        會話結束事件處理器
        清理整個 session 的去重鍵
        
        注意: 這裡的 session_id 是 GS (General Session)
              當 GS 結束時,其下的所有 CS (MEM) 和 WS (LLM/SYS) 也都結束了
        """
        try:
            session_id = event.data.get('session_id', 'unknown')  # GS ID
            session_prefix = f"{session_id}:"
            
            with self._dedupe_lock:
                # 找出並移除此 session 的所有鍵
                keys_to_remove = [k for k in self._layer_dedupe_keys if k.startswith(session_prefix)]
                for key in keys_to_remove:
                    self._layer_dedupe_keys.discard(key)
                
                self._cleanup_count += len(keys_to_remove)
                info_log(f"[ModuleCoordinator] 🧹 SESSION_ENDED 清理: 移除 {len(keys_to_remove)} 個去重鍵 (session={session_id})")
                debug_log(3, f"[ModuleCoordinator] 剩餘去重鍵數量: {len(self._layer_dedupe_keys)}")
                debug_log(3, f"[ModuleCoordinator] 統計 - 命中次數: {self._dedupe_hit_count}, 清理次數: {self._cleanup_count}")
                
        except Exception as e:
            error_log(f"[ModuleCoordinator] ❌ 處理會話結束事件失敗: {e}")
            import traceback
            traceback.print_exc()
    
    def handle_layer_completion(self, layer: ProcessingLayer, completion_data: Dict[str, Any]) -> bool:
        """
        處理層級完成通知，協調下一層處理
        
        Args:
            layer: 完成的層級
            completion_data: 完成數據
            
        Returns:
            bool: 是否成功觸發下一層處理
        """
        try:
            info_log(f"[ModuleCoordinator] {layer.value}層完成，協調下一層處理")
            debug_log(2, f"[ModuleCoordinator] 完成數據: {list(completion_data.keys())}")
            
            # 根據當前層決定下一層處理
            if layer == ProcessingLayer.INPUT:
                return self._transition_to_processing_layer(completion_data)
            elif layer == ProcessingLayer.PROCESSING:
                return self._transition_to_output_layer(completion_data)
            else:
                debug_log(2, f"[ModuleCoordinator] {layer.value}層是最終層，無需進一步處理")
                return True
                
        except Exception as e:
            error_log(f"[ModuleCoordinator] 處理{layer.value}層完成失敗: {e}")
            return False
    
    def _transition_to_processing_layer(self, input_data: Dict[str, Any]) -> bool:
        """輸入層 → 處理層轉換
        
        重要架構設計：
        - 不使用 Router（Router 依賴系統狀態，而狀態尚未轉換）
        - 直接從 NLP 結果的 primary_intent 決定處理層路徑
        - WORK → LLM + SYS, CHAT → MEM + LLM
        """
        try:
            info_log("[ModuleCoordinator] 輸入層 → 處理層轉換")
            
            # 從 NLP 結果獲取主要意圖
            nlp_result = input_data.get('nlp_result', {})
            primary_intent = nlp_result.get('primary_intent')
            
            if not primary_intent:
                debug_log(2, "[ModuleCoordinator] NLP 結果無主要意圖，跳過處理層")
                return False
            
            # 導入 IntentType 以進行判斷
            from modules.nlp_module.intent_types import IntentType
            
            # 處理枚舉或字符串值
            intent_value = primary_intent.value if hasattr(primary_intent, 'value') else primary_intent
            intent_name = primary_intent.name if hasattr(primary_intent, 'name') else str(primary_intent)
            
            info_log(f"[ModuleCoordinator] 主要意圖: {intent_name} (value={intent_value})")
            
            # ✨ 檢查是否為 WORK 路徑的 Cycle 0（需要特殊處理）
            cycle_index = input_data.get('cycle_index', 0)
            
            if (primary_intent == IntentType.WORK or intent_value == "work") and cycle_index == 0:
                # WORK Cycle 0: 三階段處理（LLM 決策 → SYS 啟動 → LLM 回應）
                info_log("[ModuleCoordinator] 🎯 WORK Cycle 0 - 開始三階段處理")
                return self._handle_work_cycle_0(input_data)
            else:
                # CHAT 路徑或 WORK Cycle 1+: 標準處理
                info_log(f"[ModuleCoordinator] 標準路徑處理 (intent={intent_name}, cycle={cycle_index})")
                
                # 準備處理層調用請求（根據 primary_intent 決定 WORK/CHAT 路徑）
                requests = self._prepare_processing_requests(intent_name, input_data)
                
                # 執行處理層調用
                responses = self.invoke_multiple_modules(requests)
                
                # 檢查是否有成功的調用
                success_count = sum(1 for r in responses if r.result == InvocationResult.SUCCESS)
                info_log(f"[ModuleCoordinator] 處理層完成: {success_count}/{len(responses)} 成功")
                
                return success_count > 0
            
        except Exception as e:
            error_log(f"[ModuleCoordinator] 輸入層 → 處理層轉換失敗: {e}")
            return False
    
    def _transition_to_output_layer(self, processing_data: Dict[str, Any]) -> bool:
        """處理層 → 輸出層轉換"""
        try:
            info_log("[ModuleCoordinator] 處理層 → 輸出層轉換")
            
            # 從處理層結果中提取文字內容
            response_text = self._extract_response_text(processing_data)
            
            if not response_text:
                debug_log(2, "[ModuleCoordinator] 處理層無文字輸出，跳過輸出層")
                return False
            
            info_log(f"[ModuleCoordinator] 處理層文字輸出: {response_text[:50]}...")
            
            # 通過 Router 獲取輸出層路由決策
            from core.router import router
            routing_decision = router.route_system_output(
                text=response_text, 
                source_module="processing_layer"
            )
            
            if routing_decision.target_module != "tts":
                debug_log(1, f"[ModuleCoordinator] Router 未指向 TTS: {routing_decision.target_module}")
                return False
            
            info_log(f"[ModuleCoordinator] Router 決策: {routing_decision.reasoning}")
            
            # 準備輸出層調用（通常是TTS）
            output_request = ModuleInvocationRequest(
                target_module="tts",
                input_data=self._prepare_output_input(processing_data),
                source_module="processing_layer",
                reasoning="處理層完成，轉送輸出層",
                layer=ProcessingLayer.OUTPUT,
                priority=2
            )
            
            # 執行輸出層調用
            response = self.invoke_module(output_request)
            
            success = response.result == InvocationResult.SUCCESS
            if success:
                info_log("[ModuleCoordinator] 輸出層完成，三層流程結束")
                # ✅ TTS 模組已經通過事件總線發布 OUTPUT_LAYER_COMPLETE 事件
                # ✅ SystemLoop 會自動接收並處理，不需要重複通知
                debug_log(2, "[ModuleCoordinator] 等待 TTS 發布的 OUTPUT_LAYER_COMPLETE 事件完成循環")
                
                # 🆕 Task 5: 檢查會話結束請求（雙條件終止機制）
                llm_output = processing_data.get('llm_output', {})
                session_control = llm_output.get('metadata', {}).get('session_control')
                
                # Support both formats: {'action': 'end_session'} and {'session_ended': True}
                should_end = (session_control and 
                             (session_control.get('action') == 'end_session' or 
                              session_control.get('session_ended') is True))
                
                if should_end:
                    reason = session_control.get('reason', 'LLM requested')
                    info_log(f"[ModuleCoordinator] 🔚 LLM 請求結束會話 (原因: {reason})")
                    # ⚠️ 雙條件終止機制：
                    # 條件 1: 外部中斷點被調用 ✅ (此處標記)
                    # 條件 2: 所屬循環結束 ⌛ (等待 CYCLE_COMPLETED)
                    # → 不立即結束，等待循環完成後再檢查並執行
                    
                    # 從 processing_data 頂層獲取 session_id (GS ID)
                    gs_id = processing_data.get('session_id', 'unknown')
                    self._pending_session_end = {
                        'reason': reason,
                        'session_control': session_control,
                        'gs_id': gs_id
                    }
                    info_log(f"[ModuleCoordinator] ✅ 已標記會話結束請求，等待循環完成 (gs_id={gs_id})")
            else:
                error_log(f"[ModuleCoordinator] 輸出層調用失敗: {response.error_message}")
            
            return success
            
        except Exception as e:
            error_log(f"[ModuleCoordinator] 處理層 → 輸出層轉換失敗: {e}")
            return False
    
    def _handle_work_cycle_0(self, input_data: Dict[str, Any]) -> bool:
        """處理 WORK 路徑的 Cycle 0（啟動工作流）
        
        ✅ MCP 架構：LLM 通過 MCP function calling 啟動工作流
        
        Cycle 0 流程：
        1. LLM 接收用戶請求和可用的 MCP tools
        2. LLM 決策並調用 start_workflow
        3. MCP Client → SYS 模組啟動工作流
        4. LLM 生成回應：「工作流已啟動，第一步是...」
        
        Cycle 1+ 流程（工作流步驟互動）：
        1. SYS 通過 review_step 返回當前步驟信息
        2. LLM 將步驟轉換為用戶友好的描述
        3. 用戶回應 → LLM 決定 approve_step/modify_step/cancel_workflow
        4. 通過 MCP 調用對應工具
        5. 重複直到工作流完成
        
        Args:
            input_data: 輸入數據
            
        Returns:
            bool: 是否成功
        """
        try:
            info_log("[ModuleCoordinator] 🎯 WORK Cycle 0 - LLM 通過 MCP 處理工作流")
            
            # 調用 LLM（LLM 會通過 MCP function calling 啟動工作流）
            llm_data = self._prepare_llm_input(input_data)
            llm_data['phase'] = 'response'  # 直接生成回應（包含 MCP 調用）
            
            llm_request = ModuleInvocationRequest(
                target_module="llm",
                input_data=llm_data,
                source_module="input_layer",
                reasoning="WORK Cycle 0 - LLM 處理工作流（含 MCP 調用）",
                layer=ProcessingLayer.PROCESSING,
                priority=5
            )
            
            llm_response = self.invoke_module(llm_request)
            
            if llm_response.result != InvocationResult.SUCCESS:
                error_log("[ModuleCoordinator] LLM 處理階段失敗")
                return False
            
            llm_output = llm_response.output_data
            debug_log(2, f"[ModuleCoordinator] llm 完整返回結果: {llm_output}")
            
            # ✅ 檢查是否成功調用了 MCP function
            function_call_made = llm_output.get('metadata', {}).get('function_call_made', False)
            function_call_result = llm_output.get('metadata', {}).get('function_call_result', {})
            
            if function_call_made:
                info_log("[ModuleCoordinator] ✅ LLM 已通過 MCP 啟動工作流")
                
                # ✅ 檢查工作流是否成功啟動
                if function_call_result.get('status') == 'success':
                    debug_log(2, "[ModuleCoordinator] 工作流啟動成功，LLM 已生成初始回應")
                    # TODO: 在未來的 Cycle 1 中，需要：
                    # 1. 調用 review_step 獲取第一個步驟
                    # 2. LLM 將步驟轉換為用戶描述
                    # 3. 等待用戶回應後再繼續
                else:
                    debug_log(2, f"[ModuleCoordinator] 工作流啟動失敗: {function_call_result.get('error')}")
                    # LLM 已經在 follow-up response 中解釋了錯誤
            else:
                debug_log(2, "[ModuleCoordinator] LLM 未調用 MCP function（可能在詢問更多信息）")
            
            # 舊架構的兼容處理（如果 LLM 返回了舊格式的 workflow_decision）
            workflow_decision = llm_output.get('workflow_decision')
            if workflow_decision:
                debug_log(1, "[ModuleCoordinator] ⚠️ LLM 返回了舊格式的 workflow_decision，應該使用 MCP function calling")
            
            # ✅ Cycle 0 完成，LLM 已經返回初始回應
            # ⚠️ 注意：完整的 Cycle 0 應該包括：
            #    1. 啟動工作流 ✓
            #    2. 獲取第一步信息 (TODO)
            #    3. LLM 描述第一步給用戶 (TODO)
            #    4. 等待用戶回應才進入 Cycle 1
            
            info_log("[ModuleCoordinator] ✓ WORK Cycle 0 完成（MCP 架構）")
            return True
            
        except Exception as e:
            error_log(f"[ModuleCoordinator] WORK Cycle 0 處理失敗: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def _prepare_processing_requests(self, primary_target: str, input_data: Dict[str, Any]) -> List[ModuleInvocationRequest]:
        """準備處理層調用請求"""
        requests = []
        nlp_result = input_data.get('nlp_result', {})
        primary_intent = nlp_result.get('primary_intent')
        
        # 導入 IntentType 以進行正確的枚舉比較
        from modules.nlp_module.intent_types import IntentType
        
        # 處理枚舉或字符串值
        intent_value = primary_intent.value if hasattr(primary_intent, 'value') else primary_intent
        intent_name = primary_intent.name if hasattr(primary_intent, 'name') else str(primary_intent)
        
        debug_log(2, f"[ModuleCoordinator] 準備處理層請求 - Intent: {intent_name} (value={intent_value})")
        
        # 根據意圖決定處理層模組組合
        if primary_intent == IntentType.CHAT or intent_value == "chat":
            # CHAT路徑：MEM + LLM
            info_log("[ModuleCoordinator] CHAT 路徑: MEM + LLM")
            requests.extend([
                ModuleInvocationRequest(
                    target_module="mem",
                    input_data=self._prepare_mem_input(input_data),
                    source_module="input_layer",
                    reasoning="聊天模式記憶查詢",
                    layer=ProcessingLayer.PROCESSING,
                    priority=4
                ),
                ModuleInvocationRequest(
                    target_module="llm",
                    input_data=self._prepare_llm_input(input_data),
                    source_module="input_layer", 
                    reasoning="聊天對話生成",
                    layer=ProcessingLayer.PROCESSING,
                    priority=3
                )
            ])
        elif primary_intent == IntentType.WORK or intent_value == "work":
            # WORK路徑：LLM + SYS (不需要 MEM)
            info_log("[ModuleCoordinator] WORK 路徑: LLM + SYS")
            requests.extend([
                ModuleInvocationRequest(
                    target_module="llm",
                    input_data=self._prepare_llm_input(input_data),
                    source_module="input_layer",
                    reasoning="工作模式任務分析",
                    layer=ProcessingLayer.PROCESSING,
                    priority=4
                ),
                ModuleInvocationRequest(
                    target_module="sys",
                    input_data=self._prepare_sys_input(input_data),
                    source_module="input_layer",
                    reasoning="系統工作流執行",
                    layer=ProcessingLayer.PROCESSING,
                    priority=3
                )
            ])
        else:
            # 默認：使用主要目標
            info_log(f"[ModuleCoordinator] 默認路徑: {primary_target}")
            requests.append(ModuleInvocationRequest(
                target_module=primary_target,
                input_data=self._prepare_module_input(primary_target, input_data),
                source_module="input_layer",
                reasoning=f"默認處理：{intent_name}",
                layer=ProcessingLayer.PROCESSING,
                priority=3
            ))
        
        return requests
    
    def _prepare_mem_input(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """準備MEM模組輸入"""
        nlp_result = input_data.get('nlp_result', {})
        return {
            "text": input_data.get('input_data', {}).get('text', ''),
            "source": "three_layer_coordinator",
            "operation": "store_and_retrieve",
            "memory_context": {
                "identity_id": nlp_result.get('identity', {}).get('identity_id'),
                "conversation_type": nlp_result.get('primary_intent'),
                "entities": nlp_result.get('entities', [])
            },
            "timestamp": input_data.get('timestamp', time.time()),
            "nlp_result": nlp_result
        }
    
    def _prepare_llm_input(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """準備LLM模組輸入
        
        重要架構設計：
        - 第一次進入處理層 (cycle_index = 0)：從狀態上下文 (WorkflowSession) 獲取命令文本
        - 第二次以後 (cycle_index > 0)：從 Router 獲取用戶回應文本
        """
        nlp_result = input_data.get('nlp_result', {})
        cycle_index = input_data.get('cycle_index', 0)
        primary_intent = nlp_result.get('primary_intent')
        
        # 導入 IntentType 以進行判斷
        from modules.nlp_module.intent_types import IntentType
        intent_value = primary_intent.value if hasattr(primary_intent, 'value') else primary_intent
        
        # 判斷文本來源
        if (primary_intent == IntentType.WORK or intent_value == "work") and cycle_index == 0:
            # ✅ Cycle 0: 使用完整的原始輸入文本進行決策
            # NLP 已經分析過意圖，但 LLM 需要完整上下文來理解和決策
            input_text = input_data.get('input_data', {}).get('text', '')
            debug_log(2, f"[ModuleCoordinator] WORK Cycle 0 - 使用完整原始輸入: {input_text[:50]}...")
        else:
            # 其他情況：從 Router 獲取文本（CHAT 路徑或 WORK 第二次以後）
            input_text = input_data.get('input_data', {}).get('text', '')
            if cycle_index > 0:
                debug_log(2, f"[ModuleCoordinator] WORK cycle {cycle_index} - 從 Router 獲取用戶回應")
        
        # ✅ 根據 primary_intent 決定 LLM 模式
        if primary_intent == IntentType.WORK or intent_value == "work":
            llm_mode = "work"
        else:
            llm_mode = "chat"
        
        # ✅ WORK Cycle 0: 提取 NLP 找到的工作流匹配信息
        suggested_workflow = None
        if (primary_intent == IntentType.WORK or intent_value == "work") and cycle_index == 0:
            # 從 NLP 的 processing_notes 或 intent_segments 中提取工作流提示
            for note in nlp_result.get('processing_notes', []):
                if 'matching function' in note.lower() or 'workflow' in note.lower():
                    suggested_workflow = note
                    break
        
        base_data = {
            "text": input_text,
            "source": "three_layer_coordinator",
            "mode": llm_mode,  # ✅ 添加 mode 參數，讓 LLM 知道是 WORK 還是 CHAT
            "conversation_type": nlp_result.get('primary_intent'),
            "user_input": input_text,
            "context": {
                "intent_segments": nlp_result.get('intent_segments', []),
                "entities": nlp_result.get('entities', []),
                "processing_notes": nlp_result.get('processing_notes', [])
            },
            "timestamp": input_data.get('timestamp', time.time()),
            "nlp_result": nlp_result,
            "identity": nlp_result.get('identity'),
            "intent": nlp_result.get('primary_intent'),
            "confidence": nlp_result.get('overall_confidence', 0.0),
            "cycle_index": cycle_index  # ✅ 傳遞 cycle_index 供 LLM 模組使用
        }
        
        # ✅ 添加工作流提示（如果有）
        if suggested_workflow:
            base_data['workflow_hint'] = suggested_workflow
        
        return base_data
    
    def _prepare_sys_input(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """準備SYS模組輸入
        
        重要架構設計：
        - 第一次進入處理層 (cycle_index = 0)：從狀態上下文 (WorkflowSession) 獲取命令文本
        - 第二次以後 (cycle_index > 0)：從 Router 獲取用戶回應文本
        """
        nlp_result = input_data.get('nlp_result', {})
        cycle_index = input_data.get('cycle_index', 0)
        
        # 判斷文本來源（與 LLM 使用相同邏輯）
        if cycle_index == 0:
            # ✅ Cycle 0: 使用完整的原始輸入文本
            input_text = input_data.get('input_data', {}).get('text', '')
            debug_log(2, f"[ModuleCoordinator] WORK Cycle 0 - SYS 使用完整原始輸入: {input_text[:50]}...")
        else:
            # 第二次以後：從 Router 獲取用戶回應
            input_text = input_data.get('input_data', {}).get('text', '')
            debug_log(2, f"[ModuleCoordinator] WORK cycle {cycle_index} - SYS 從 Router 獲取文本")
        
        return {
            "text": input_text,
            "source": "three_layer_coordinator",
            "mode": "workflow",  # ✅ 添加 mode 參數，SYS 模組必需
            "operation": "workflow_execution",
            "system_context": {
                "intent": nlp_result.get('primary_intent'),
                "entities": nlp_result.get('entities', []),
                "command_type": "work_task"
            },
            "timestamp": input_data.get('timestamp', time.time()),
            "nlp_result": nlp_result,
            "cycle_index": cycle_index  # ✅ 傳遞 cycle_index 供 SYS 模組使用
        }
    
    def _prepare_output_input(self, processing_data: Dict[str, Any]) -> Dict[str, Any]:
        """準備輸出層（TTS）輸入"""
        return {
            "text": processing_data.get('response', processing_data.get('text', '')),
            "source": "three_layer_coordinator",
            "output_mode": "voice",
            "timestamp": time.time(),
            "processing_result": processing_data
        }
    
    def _prepare_module_input(self, target_module: str, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """為特定模組準備輸入數據（通用方法）"""
        if target_module == "mem":
            return self._prepare_mem_input(input_data)
        elif target_module == "llm":
            return self._prepare_llm_input(input_data)
        elif target_module == "sys":
            return self._prepare_sys_input(input_data)
        elif target_module == "tts":
            return self._prepare_output_input(input_data)
        else:
            # 通用輸入格式
            nlp_result = input_data.get('nlp_result', {})
            return {
                "text": input_data.get('input_data', {}).get('text', ''),
                "source": "three_layer_coordinator",
                "timestamp": input_data.get('timestamp', time.time()),
                "nlp_result": nlp_result,
                "intent": nlp_result.get('primary_intent'),
                "confidence": nlp_result.get('overall_confidence', 0.0)
            }
    
    def invoke_module(self, request: ModuleInvocationRequest) -> ModuleInvocationResponse:
        """
        調用目標模組
        
        Args:
            request: 模組調用請求
            
        Returns:
            ModuleInvocationResponse: 調用回應
        """
        start_time = time.time()
        
        with self._invocation_lock:
            try:
                info_log(f"[ModuleCoordinator] 調用{request.layer.value}層模組: {request.target_module}")
                debug_log(2, f"[ModuleCoordinator] 調用原因: {request.reasoning}")
                debug_log(3, f"[ModuleCoordinator] 輸入數據: {list(request.input_data.keys())}")
                
                # 獲取目標模組
                from core.framework import core_framework
                target_module = core_framework.get_module(request.target_module)
                
                if not target_module:
                    error_msg = f"無法找到目標模組: {request.target_module}"
                    error_log(f"[ModuleCoordinator] {error_msg}")
                    return ModuleInvocationResponse(
                        target_module=request.target_module,
                        result=InvocationResult.NO_TARGET,
                        layer=request.layer,
                        error_message=error_msg,
                        execution_time=time.time() - start_time
                    )
                
                # 記錄活躍調用
                invocation_id = f"{request.target_module}_{int(time.time() * 1000)}"
                self._active_invocations[invocation_id] = {
                    "target": request.target_module,
                    "layer": request.layer.value,
                    "start_time": start_time,
                    "source": request.source_module
                }
                
                # 實際調用模組
                result_data = target_module.handle(request.input_data)
                
                # 移除活躍調用記錄
                if invocation_id in self._active_invocations:
                    del self._active_invocations[invocation_id]
                
                execution_time = time.time() - start_time
                
                # ✅ 正確判斷成功: 檢查 result_data['success'] 字段
                is_success = result_data and isinstance(result_data, dict) and result_data.get('success', False)
                
                if result_data:
                    # 記錄模組返回結果
                    self._log_module_result(request.target_module, result_data)
                    
                    # 根據 success 字段決定結果狀態
                    if is_success:
                        info_log(f"[ModuleCoordinator] {request.layer.value}層模組 {request.target_module} 處理完成 ({execution_time:.3f}s)")
                        invocation_result = InvocationResult.SUCCESS
                    else:
                        # success=False 視為失敗,不是成功
                        error_msg = result_data.get('error', '未知錯誤')
                        debug_log(2, f"[ModuleCoordinator] {request.layer.value}層模組 {request.target_module} 處理失敗: {error_msg}")
                        invocation_result = InvocationResult.FAILED
                    
                    response = ModuleInvocationResponse(
                        target_module=request.target_module,
                        result=invocation_result,
                        layer=request.layer,
                        output_data=result_data,
                        execution_time=execution_time
                    )
                    
                    # 只有真正成功時才檢查層級完成
                    if is_success:
                        self._check_layer_completion(request.layer, result_data)
                    
                else:
                    debug_log(2, f"[ModuleCoordinator] {request.layer.value}層模組 {request.target_module} 無返回結果")
                    response = ModuleInvocationResponse(
                        target_module=request.target_module,
                        result=InvocationResult.FAILED,  # 無返回結果視為失敗
                        layer=request.layer,
                        output_data=None,
                        execution_time=execution_time
                    )
                
                # 記錄調用歷史
                self._invocation_history.append({
                    "timestamp": time.time(),
                    "target_module": request.target_module,
                    "layer": request.layer.value,
                    "source_module": request.source_module,
                    "result": response.result.value,
                    "execution_time": execution_time
                })
                
                # 保持歷史記錄在合理範圍內
                if len(self._invocation_history) > 100:
                    self._invocation_history = self._invocation_history[-50:]
                
                return response
                
            except Exception as e:
                execution_time = time.time() - start_time
                error_msg = f"調用模組失敗: {e}"
                error_log(f"[ModuleCoordinator] {error_msg}")
                
                # 清理活躍調用記錄
                invocation_id = f"{request.target_module}_{int(start_time * 1000)}"
                if invocation_id in self._active_invocations:
                    del self._active_invocations[invocation_id]
                
                return ModuleInvocationResponse(
                    target_module=request.target_module,
                    result=InvocationResult.FAILED,
                    layer=request.layer,
                    error_message=error_msg,
                    execution_time=execution_time
                )
    
    def _check_layer_completion(self, current_layer: ProcessingLayer, result_data: Dict[str, Any]):
        """檢查當前層是否完成，決定是否觸發下一層"""
        try:
            # 簡化版本：直接檢查結果是否包含需要傳遞的數據
            if result_data and 'response' in result_data:
                debug_log(2, f"[ModuleCoordinator] {current_layer.value}層處理完成，準備觸發下一層")
                # 在實際實現中，這裡會根據具體邏輯決定是否觸發下一層
                # 目前簡化為日誌記錄
            
        except Exception as e:
            debug_log(3, f"[ModuleCoordinator] 檢查層級完成狀態時發生錯誤: {e}")
    
    def invoke_multiple_modules(self, requests: List[ModuleInvocationRequest]) -> List[ModuleInvocationResponse]:
        """
        批量調用多個模組
        
        Args:
            requests: 模組調用請求列表
            
        Returns:
            List[ModuleInvocationResponse]: 調用回應列表
        """
        info_log(f"[ModuleCoordinator] 批量調用 {len(requests)} 個模組")
        
        responses = []
        for request in requests:
            response = self.invoke_module(request)
            responses.append(response)
            
            # 如果有任何關鍵模組調用失敗，考慮終止後續調用
            if response.result == InvocationResult.FAILED and request.priority >= 5:
                error_log(f"[ModuleCoordinator] 關鍵模組 {request.target_module} 調用失敗，終止後續調用")
                break
        
        return responses
    
    def _log_module_result(self, module_name: str, result_data: Any):
        """記錄模組返回結果的詳細信息"""
        try:
            # 簡化日誌：直接輸出整個結果字典
            debug_log(3, f"[ModuleCoordinator] {module_name} 完整返回結果: {result_data}")
                
        except Exception as e:
            debug_log(3, f"[ModuleCoordinator] 記錄 {module_name} 結果時發生錯誤: {e}")
    
    def _extract_response_text(self, processing_data: Dict[str, Any]) -> str:
        """從處理層數據中提取文字回應"""
        try:
            # 優先順序：response > text > content
            if "response" in processing_data:
                return processing_data["response"]
            elif "text" in processing_data:
                return processing_data["text"]
            elif "content" in processing_data:
                return processing_data["content"]
            else:
                # 如果是嵌套結構，嘗試深度提取
                if isinstance(processing_data, dict):
                    for key in ["llm_output", "result", "data"]:
                        if key in processing_data:
                            nested_data = processing_data[key]
                            if isinstance(nested_data, dict):
                                if "text" in nested_data:
                                    return nested_data["text"]
                                elif "response" in nested_data:
                                    return nested_data["response"]
                
                debug_log(2, f"[ModuleCoordinator] 無法從處理層數據中提取文字: {list(processing_data.keys())}")
                return ""
                
        except Exception as e:
            error_log(f"[ModuleCoordinator] 提取回應文字失敗: {e}")
            return ""
    
    def get_active_invocations(self) -> Dict[str, Any]:
        """獲取當前活躍的調用狀態"""
        return dict(self._active_invocations)
    
    def get_invocation_stats(self) -> Dict[str, Any]:
        """獲取調用統計信息"""
        if not self._invocation_history:
            return {
                "total_invocations": 0,
                "avg_execution_time": 0.0,
                "success_rate": 0.0,
                "module_stats": {}
            }
        
        total = len(self._invocation_history)
        successful = sum(1 for h in self._invocation_history if h["result"] == "success")
        avg_time = sum(h["execution_time"] for h in self._invocation_history) / total
        
        # 模組統計
        module_stats = {}
        for history in self._invocation_history:
            module = history["target_module"]
            if module not in module_stats:
                module_stats[module] = {"count": 0, "success": 0, "avg_time": 0.0}
            
            module_stats[module]["count"] += 1
            if history["result"] == "success":
                module_stats[module]["success"] += 1
            module_stats[module]["avg_time"] = (
                module_stats[module]["avg_time"] * (module_stats[module]["count"] - 1) + 
                history["execution_time"]
            ) / module_stats[module]["count"]
        
        return {
            "total_invocations": total,
            "avg_execution_time": avg_time,
            "success_rate": successful / total,
            "active_invocations": len(self._active_invocations),
            "module_stats": module_stats
        }
    
    def _handle_session_end(self, session_control: Dict[str, Any]):
        """
        🆕 處理會話結束請求（雙條件終止機制）
        
        當 LLM 決定結束會話時（通過 session_control），並且循環已完成（CYCLE_COMPLETED）：
        1. 結束所有活躍的 WS（workflow session）
        2. 結束所有活躍的 CS（chatting session）
        
        注意：
        - ❌ 不結束 GS！GS 是系統層級會話，由 Controller 管理
        - ✅ GS 結束條件：狀態佇列清空 + 系統回到 IDLE（由 Controller.check_gs_end_conditions 檢查）
        - ✅ CS/WS 結束會發布 SESSION_ENDED 事件，StateManager 監聽並通知 StateQueue
        - ✅ StateQueue 自動處理下一個狀態（或回到 IDLE）
        
        Args:
            session_control: 會話控制指令
        """
        try:
            info_log("[ModuleCoordinator] 處理 CS/WS 結束請求")
            
            # 獲取所有活躍的子會話
            from core.sessions.session_manager import unified_session_manager
            
            # 結束所有工作流會話 (WS)
            active_ws = unified_session_manager.get_active_workflow_session_ids()
            for ws_id in active_ws:
                debug_log(2, f"[ModuleCoordinator] 結束工作流會話: {ws_id}")
                unified_session_manager.end_workflow_session(ws_id)
            
            # 結束所有聊天會話 (CS)
            active_cs = unified_session_manager.get_active_chatting_session_ids()
            for cs_id in active_cs:
                debug_log(2, f"[ModuleCoordinator] 結束聊天會話: {cs_id}")
                unified_session_manager.end_chatting_session(cs_id)
            
            # ⚠️ 重要：不結束 GS！
            # GS 生命週期：
            #   - 創建：進入非 IDLE 狀態時（由 Controller._monitor_gs_lifecycle）
            #   - 結束：狀態佇列清空 + IDLE 狀態（由 Controller.check_gs_end_conditions）
            # 
            # CS/WS 結束後：
            #   → 發布 SESSION_ENDED 事件
            #   → StateManager 監聽並通知 StateQueue.complete_current_state()
            #   → StateQueue 處理下一個狀態（若有）或轉換到 IDLE（若佇列為空）
            #   → 當 StateQueue 為空且 IDLE 時，Controller 才結束 GS
            
            info_log("[ModuleCoordinator] ✅ CS/WS 結束完成，等待狀態轉換")
            
        except Exception as e:
            error_log(f"[ModuleCoordinator] 處理會話結束失敗: {e}")
    
    def get_deduplication_stats(self) -> Dict[str, Any]:
        """
        獲取去重統計信息 (G. 監控與除錯)
        
        Returns:
            包含去重命中次數、清理次數、活躍鍵數量等診斷信息
        """
        with self._dedupe_lock:
            # 分析活躍的 dedupe keys
            active_flows = set()
            layers_count = {"INPUT": 0, "PROCESSING": 0, "OUTPUT": 0}
            
            for key in self._layer_dedupe_keys:
                try:
                    parts = key.split(":")
                    if len(parts) >= 3:
                        session_id = parts[0]
                        cycle_index = parts[1]
                        layer = parts[2]
                        
                        flow_id = f"{session_id}:{cycle_index}"
                        active_flows.add(flow_id)
                        
                        if layer in layers_count:
                            layers_count[layer] += 1
                except:
                    pass
            
            return {
                "dedupe_hit_count": self._dedupe_hit_count,
                "cleanup_count": self._cleanup_count,
                "active_dedupe_keys": len(self._layer_dedupe_keys),
                "max_dedupe_keys": self._max_dedupe_keys,
                "active_flows": len(active_flows),
                "layers_distribution": layers_count,
                "memory_pressure": len(self._layer_dedupe_keys) / self._max_dedupe_keys if self._max_dedupe_keys > 0 else 0.0
            }


# 全局協調器實例
module_coordinator = ModuleInvocationCoordinator()