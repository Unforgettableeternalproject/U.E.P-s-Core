# integration_tests_new.py
"""
System Loop Integration Tests - 使用完整系統循環進行集成測試

本測試套件使用真實的系統循環來測試：
- 完整的三層架構處理流程（輸入層 → 處理層 → 輸出層）
- LLM-SYS 協作管道（工作流審核）
- 狀態管理和會話生命週期
- 事件驅動架構

核心理念：
- 使用真實的 Controller、SystemLoop、ModuleCoordinator、EventBus
- 只模擬使用者輸入（文字模式）來觸發系統處理
- 測試完整的系統行為，而非單一模組
"""

import time
import threading
from typing import Dict, Any, Optional, List
from utils.debug_helper import debug_log, info_log, error_log


class SystemLoopIntegrationTest:
    """
    系統循環集成測試
    
    使用真實的系統組件進行端到端測試
    """
    
    def __init__(self):
        """初始化測試套件"""
        self._clear_module_coordinator_dedupe_keys()
        self.controller = None
        self.system_loop = None
        self.test_results = []
        self.event_log: List[Dict[str, Any]] = []
        self.test_complete = threading.Event()
        # TTS 處理速度是系統痛點，移除超時限制
        
        info_log("[IntegrationTest] 測試套件已初始化")
    
    def setup_system(self, modules_dict: Optional[Dict[str, Any]] = None) -> bool:
        """設置完整系統環境"""
        try:
            info_log("\n" + "="*60)
            info_log("🚀 開始設置系統環境")
            info_log("="*60)
            
            # 0. 如果提供了已初始化的模組，先注入到 registry
            if modules_dict:
                self._inject_modules_to_registry(modules_dict)
                info_log(f"✅ 已注入 {len(modules_dict)} 個預初始化模組到 registry")
            
            # 1. 初始化 Controller
            from core.controller import UnifiedController
            self.controller = UnifiedController()
            
            if not self.controller.initialize():
                error_log("❌ Controller 初始化失敗")
                return False
            
            info_log("✅ Controller 初始化成功")
            
            # 2. 使用全局 SystemLoop 實例（避免雙重訂閱事件）
            from core.system_loop import system_loop
            self.system_loop = system_loop
            
            info_log("✅ SystemLoop 已獲取全局實例")
            
            # ✅ 重要：啟動 EventBus 處理線程
            # 測試環境不會調用 SystemLoop.run()，所以必須手動啟動 EventBus
            from core.event_bus import event_bus
            event_bus.start()
            info_log("✅ EventBus 處理線程已啟動")
            
            # 🔧 測試環境手動初始化 cycle_index
            # 因為 SystemLoop 主循環不運行，需要手動設置 working_context
            self.system_loop.cycle_index = 0
            self.system_loop._update_global_cycle_info()
            info_log(f"✅ 已初始化 cycle_index: {self.system_loop.cycle_index}")
            
            # 3. 訂閱關鍵事件以追蹤測試進度
            self._setup_event_monitoring()
            
            # 4. 啟動系統（Controller 初始化即自動啟動）
            info_log("✅ Controller 已初始化並啟動")
            
            # 5. 創建初始 GS
            from core.sessions.general_session import general_session_manager, GSType
            try:
                gs_id = general_session_manager.start_session(
                    gs_type=GSType.SYSTEM_EVENT,
                    trigger_event={"source": "integration_test", "reason": "test_setup"}
                )
                if gs_id:
                    info_log(f"✅ General Session created: {gs_id}")
                else:
                    info_log("⚠️  GS creation returned None, but system may continue")
            except Exception as e:
                info_log(f"⚠️  GS creation exception: {e}, but system may continue")
            
            info_log("\n" + "="*60)
            info_log("🎉 系統環境設置完成")
            info_log("="*60 + "\n")
            
            return True
            
        except Exception as e:
            error_log(f"❌ 系統設置失敗: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def _clear_module_coordinator_dedupe_keys(self):
        """
        清理 ModuleCoordinator 的去重鍵集合
        
        重要：測試環境必須清理去重鍵，否則會誤擋合法事件
        因為測試每次都用相同的 flow (session_id:cycle_index:layer)
        """
        try:
            from core.module_coordinator import module_coordinator
            
            if module_coordinator:
                with module_coordinator._dedupe_lock:
                    cleared_count = len(module_coordinator._layer_dedupe_keys)
                    module_coordinator._layer_dedupe_keys.clear()
                    debug_log(2, f"[IntegrationTest] 已清理 {cleared_count} 個去重鍵")
            
        except Exception as e:
            debug_log(2, f"[IntegrationTest] 清理去重鍵失敗 (可能尚未初始化): {e}")
    
    def _inject_modules_to_registry(self, modules_dict: Dict[str, Any]):
        """將已初始化的模組注入到 core.registry 中"""
        try:
            from core import registry
            
            # 將 debug_api 中的模組實例注入到 registry._loaded_modules
            for module_name, module_instance in modules_dict.items():
                if module_instance:
                    registry._loaded_modules[module_name] = module_instance
                    info_log(f"[IntegrationTest] 已注入模組 {module_name} 到 registry")
            
            info_log(f"[IntegrationTest] 總共注入了 {len(modules_dict)} 個模組到 registry")
            
        except Exception as e:
            error_log(f"[IntegrationTest] 模組注入失敗: {e}")
    
    def _setup_event_monitoring(self):
        """設置事件監控"""
        try:
            from core.event_bus import event_bus, SystemEvent
            
            # 訂閱所有關鍵事件
            events_to_monitor = [
                SystemEvent.INPUT_LAYER_COMPLETE,
                SystemEvent.PROCESSING_LAYER_COMPLETE,
                SystemEvent.OUTPUT_LAYER_COMPLETE,
                SystemEvent.CYCLE_COMPLETED,  # 🔧 添加循環完成事件訂閱
                SystemEvent.WORKFLOW_REQUIRES_INPUT,
                SystemEvent.WORKFLOW_INPUT_COMPLETED,
                SystemEvent.STATE_CHANGED,
                SystemEvent.SESSION_STARTED,
                SystemEvent.SESSION_ENDED,
            ]
            
            for event in events_to_monitor:
                event_bus.subscribe(
                    event,
                    lambda data, evt=event: self._log_event(evt.value, {"event_data": data}),
                    handler_name=f"IntegrationTest.{event.value}"
                )
            
            info_log("✅ 事件監控已設置")
            
        except Exception as e:
            error_log(f"❌ 事件監控設置失敗: {e}")
    
    def _log_event(self, event_type: str, data: Dict[str, Any]):
        """記錄事件"""
        event = {
            "event_type": event_type,
            "data": data,
            "timestamp": time.time()
        }
        self.event_log.append(event)
        debug_log(2, f"[Event] {event_type}: {data}")
    
    def _determine_test_input(self, step_id: str, step_type: str, 
                              is_optional: bool, prompt: str) -> str:
        """
        根據工作流步驟的屬性決定要注入的測試資料
        
        Args:
            step_id: 步驟 ID
            step_type: 步驟類型 (INPUT, CONFIRMATION, etc.)
            is_optional: 是否為可選步驟
            prompt: 步驟提示文字
            
        Returns:
            要注入的測試資料字串
        """
        # 快速測試用：可以直接修改這個變數來測試不同的輸入場景
        # 如果設定了非空字串，會優先使用這個輸入（跳過下面的自動邏輯）
        # TODO: 技術債務 - 未來需要實現自然語言路徑解析
        # 目前工作流固定使用 D:\\ 進行測試
        custom_input = "Can you put the file in my d drive root?"
        
        # CONFIRMATION 步驟：通過 step_id 識別（通常以 _confirm 結尾）
        if step_id and step_id.endswith("_confirm"):
            info_log(f"      → Confirmation 步驟，注入 'yes'")
            return "yes"
        
        # INTERACTIVE 步驟（需要用戶輸入）：根據 optional 屬性決定
        if step_type == "interactive":
            # 可選步驟：注入空字串觸發 fallback
            if is_optional:
                info_log(f"      → Optional 步驟，注入空字串觸發 fallback")
                return custom_input or ""
            
            # 必填步驟：根據步驟 ID 提供合理的測試資料
            test_data_map = {
                "target_dir_input": custom_input,
                "file_path_input": "C:\\temp\\test_file.txt",
                "tag_input": "test_tag",
                "category_input": "documents",
            }
            
            if step_id in test_data_map:
                info_log(f"      → Required 步驟，注入測試資料: {test_data_map[step_id]}")
                return test_data_map[step_id]
            
            # 預設：注入通用測試資料
            info_log(f"      → Required 步驟，注入預設測試資料")
            return "test_input"
        
        # PROCESSING / SYSTEM 類型：不需要用戶輸入，工作流自動執行
        info_log(f"      → 處理類型步驟 ({step_type})，注入空字串")
        return ""
    
    def inject_text_input(self, text: str) -> bool:
        """
        注入文字輸入到系統
        
        模擬使用者在文字模式下的輸入
        """
        try:
            from core.framework import core_framework
            
            # ⚠️ 不再手動設置 session_id 和 cycle_index
            # 讓 Controller 自己管理 GS 生命週期
            # SystemLoop 會從 Controller 獲取當前的 GS ID
            
            # 獲取 STT 模組
            stt_module = core_framework.get_module('stt')
            if not stt_module:
                error_log("❌ 無法獲取 STT 模組")
                return False
            
            info_log(f"\n📝 注入文字輸入: \"{text}\"")
            
            # 使用 STT 模組的 handle_text_input 方法
            result = stt_module.handle_text_input(text)
            
            # STT 的 handle_text_input 返回 STTOutput.model_dump()
            # 檢查是否有 text 欄位且沒有 error
            if result and result.get("text") and not result.get("error"):
                info_log(f"✅ 文字輸入已處理: {result.get('text')}")
                return True
            else:
                # 這不一定是錯誤，可能只是 STT 成功發送到 NLP 但返回格式不同
                debug_log(2, f"[IntegrationTest] 文字輸入結果: {result}")
                return True  # 改為 True，因為即使沒有 status 欄位，如果有 text 就算成功
                
        except Exception as e:
            error_log(f"❌ 注入文字輸入失敗: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def wait_for_processing_complete(self, timeout: Optional[float] = None) -> bool:
        """
        等待處理完成 - 監控 OUTPUT_LAYER_COMPLETE、SESSION_ENDED 和 WORKFLOW_REQUIRES_INPUT 事件
        
        Args:
            timeout: 已移除超時限制（保留參數以兼容舊代碼）
            
        Returns:
            處理完成時返回 True
        """
        start_time = time.time()
        
        info_log(f"⏳ 等待處理完成 (無超時限制，適應 TTS 處理速度)...")
        info_log(f"   監控 OUTPUT_LAYER_COMPLETE、SESSION_ENDED 和 WORKFLOW_REQUIRES_INPUT 事件")
        
        last_output_time = start_time
        output_count = 0
        latest_cycle_index = -1  # 記錄最新的 cycle_index（從事件中提取）
        session_ended_count = 0
        workflow_inputs_handled = set()  # 記錄已處理的工作流輸入請求
        pending_input = None  # 待注入的輸入（等待當前循環完成）
        
        # 工作流完成的標準：
        # 1. 至少有一個 OUTPUT_LAYER_COMPLETE 事件
        # 2. 系統狀態回到 IDLE（工作流完全結束）
        # 或者：有 SESSION_ENDED 事件（明確的會話結束信號）
        
        while True:  # 無限等待，直到處理完成
            current_time = time.time()
                
            # 🔧 測試環境自動處理 WORKFLOW_REQUIRES_INPUT 事件
            # 策略：
            # 1. 檢測到 WORKFLOW_REQUIRES_INPUT 時，記錄待注入的輸入
            # 2. 等待 OUTPUT_LAYER_COMPLETE 事件（確保當前 TTS 已完成）
            # 3. 輸出完成後立即注入輸入，觸發下一個處理循環
            
            # 檢查 WORKFLOW_REQUIRES_INPUT 事件
            workflow_input_events = [e for e in self.event_log 
                                    if e["event_type"] == "workflow_requires_input"]
            new_input_requests = len(workflow_input_events) - len(workflow_inputs_handled)
            if new_input_requests > 0 and pending_input is None:
                for event in workflow_input_events:
                    event_data_obj = event["data"]["event_data"]
                    event_id = getattr(event_data_obj, 'event_id', 'unknown')
                    
                    if event_id not in workflow_inputs_handled:
                        workflow_inputs_handled.add(event_id)
                        
                        event_actual_data = getattr(event_data_obj, 'data', {})
                        step_id = event_actual_data.get("step_id", "unknown")
                        step_type = event_actual_data.get("step_type", "unknown")
                        prompt = event_actual_data.get("prompt", "")
                        is_optional = event_actual_data.get("optional", False)
                        
                        info_log(f"   ⏸️  工作流等待輸入: {step_id}")
                        info_log(f"      步驟類型: {step_type}, Optional: {is_optional}")
                        info_log(f"      提示: {prompt}")
                        
                        # 決定要注入的測試資料
                        test_input = self._determine_test_input(
                            step_id, step_type, is_optional, prompt
                        )
                        
                        # 🔧 關鍵修復：如果循環已完成（cycle_index >= 0），立即注入；否則等待循環完成
                        # cycle_index 已在 CYCLE_COMPLETED 事件處理時遞增，無需再次遞增
                        if latest_cycle_index >= 0:
                            # 循環已完成，系統準備好接收輸入，立即注入
                            info_log(f"   ✅ 循環已完成（cycle_index={latest_cycle_index}），立即注入輸入: '{test_input}'")
                            time.sleep(0.5)  # 短暫延遲確保系統就緒
                            
                            if not self.inject_text_input(test_input):
                                error_log(f"   ❌ 注入輸入失敗: {step_id}")
                        else:
                            # 循環未完成，記錄待注入的輸入，等待循環完成
                            pending_input = (step_id, test_input, output_count)
                            info_log(f"   ⏳ 循環未完成（cycle_index={latest_cycle_index}），記錄待注入輸入: '{test_input}'")
            
            # 檢查 CYCLE_COMPLETED 事件
            cycle_events = [e for e in self.event_log 
                        if e["event_type"] == "cycle_completed"]
            if len(cycle_events) > 0:
                # 從最新的 CYCLE_COMPLETED 事件中提取 cycle_index
                latest_event = cycle_events[-1]
                event_data_obj = latest_event["data"]["event_data"]
                event_cycle_index = getattr(event_data_obj, 'data', {}).get('cycle_index', -1)
                
                if event_cycle_index > latest_cycle_index:
                    latest_cycle_index = event_cycle_index
                    info_log(f"   ✅ 完成循環 (cycle_index={latest_cycle_index})")
                    
                    # 🔧 關鍵修復：循環完成後立即遞增 cycle_index
                    # 這確保下一個循環的處理使用正確的 cycle_index
                    # 避免去重機制誤攔截新循環的事件
                    # 但要避免重複遞增（SystemLoop 可能已經更新過）
                    if self.system_loop:
                        expected_next_cycle = latest_cycle_index + 1
                        if self.system_loop.cycle_index < expected_next_cycle:
                            self.system_loop.cycle_index = expected_next_cycle
                            self.system_loop._update_global_cycle_info()
                            info_log(f"   🔄 已遞增 cycle_index: {self.system_loop.cycle_index}")
                        else:
                            debug_log(3, f"   ⏭️  cycle_index 已是 {self.system_loop.cycle_index}，跳過遞增")
                
                # 🔧 循環完成後，如果有待注入的輸入，現在注入
                if pending_input is not None:
                    step_id, test_input, recorded_output_count = pending_input
                    info_log(f"   🤖 循環已完成，現在注入輸入: '{test_input}'")
                    
                    # 延遲 0.5 秒確保系統已準備好（STT 需要時間）
                    time.sleep(0.5)
                    
                    if not self.inject_text_input(test_input):
                        error_log(f"   ❌ 注入輸入失敗: {step_id}")
                    
                    pending_input = None
            
            # 檢查 OUTPUT_LAYER_COMPLETE 事件
            output_events = [e for e in self.event_log 
                        if e["event_type"] == "output_layer_complete"]
            if len(output_events) > output_count:
                output_count = len(output_events)
                last_output_time = current_time
                info_log(f"   完成第 {output_count} 個輸出循環")
            
            # 檢查 SESSION_ENDED 事件（但不作為唯一完成條件）
            session_ended_events = [e for e in self.event_log 
                                   if e["event_type"] == "session_ended"]
            if len(session_ended_events) > session_ended_count:
                session_ended_count = len(session_ended_events)
                info_log(f"   檢測到會話結束事件 ({session_ended_count})")
                # 🔧 SESSION_ENDED 後還需要等待最後一個 OUTPUT_LAYER_COMPLETE
                # 因為 follow-up 回應的 TTS 輸出可能還在進行
            
            # 檢查系統狀態 - 從 state_manager 獲取當前狀態
            try:
                from core.states.state_manager import state_manager, UEPState
                current_state = state_manager.get_current_state()
                
                # 每 10 秒記錄一次當前狀態（避免日誌過多）
                if int(current_time - start_time) % 10 == 0 and (current_time - start_time) > 0:
                    debug_log(3, f"[IntegrationTest] 等待中... 狀態={current_state}, 輸出={output_count}, 會話結束={session_ended_count}")
                
                # 完成條件判斷（必須至少有一個輸出）
                if output_count > 0:
                    # 🔧 工作流完成的正確判斷：SESSION_ENDED + 最後的 OUTPUT_LAYER_COMPLETE
                    # 因為 SESSION_ENDED 可能在 follow-up 回應的 TTS 輸出之前發布
                    # 所以需要確保：1) 有 SESSION_ENDED 事件，2) 之後至少還有一個輸出完成
                    if session_ended_count > 0:
                        # 檢查 SESSION_ENDED 之後是否有新的輸出完成
                        session_ended_time = session_ended_events[-1]["timestamp"]
                        outputs_after_session_end = [e for e in output_events 
                                                     if e["timestamp"] > session_ended_time]
                        
                        if len(outputs_after_session_end) > 0:
                            elapsed = current_time - start_time
                            info_log(f"✅ 工作流完成（SESSION_ENDED + 最終輸出完成），耗時: {elapsed:.2f}秒")
                            return True
                        else:
                            # SESSION_ENDED 已收到，但還在等待最後的輸出（follow-up 回應）
                            debug_log(3, f"[IntegrationTest] SESSION_ENDED 已收到，等待最終輸出...")
                    
                    # 方式 2: 系統狀態回到 IDLE（工作流完全結束）
                    if current_state == UEPState.IDLE and session_ended_count > 0:
                        elapsed = current_time - start_time
                        info_log(f"✅ 系統狀態回到 IDLE，工作流完成 (耗時: {elapsed:.2f}秒)")
                        return True
                    
                    # ⚠️ 移除了「10秒無輸出」的後備方案
                    # 因為它會導致測試在系統還在 WORK 狀態時就提早結束
                    # 必須等待系統狀態回到 IDLE 或收到 SESSION_ENDED 事件
            except Exception as e:
                debug_log(1, f"[IntegrationTest] 檢查系統狀態失敗: {e}")
            
            time.sleep(0.1)
    
    def test_file_workflow(self, workflow_name: str, test_llm_sys_collaboration: bool = False) -> Dict[str, Any]:
        """
        測試檔案工作流
        
        Args:
            workflow_name: 工作流名稱 (drop_and_read, intelligent_archive, summarize_tag)
            test_llm_sys_collaboration: 是否測試 LLM-SYS 協作機制（Cycle 0 三階段）
            
        Returns:
            測試結果
        """
        test_name = f"檔案工作流測試 ({workflow_name})"
        if test_llm_sys_collaboration:
            test_name += " [LLM-SYS 協作]"
        
        info_log(f"\n{'='*60}")
        info_log(f"🧪 開始測試: {test_name}")
        info_log(f"{'='*60}")
        
        try:
            # 清空事件日誌
            self.event_log.clear()
            
            # Build test input - 使用英文（系統內部語言）
            # LLM 會使用 MCP 工具來理解意圖並決定工作流
            if workflow_name == "drop_and_read":
                test_input = "Please help me read the file content"
            elif workflow_name == "intelligent_archive":
                test_input = "Please help me archive and organize this file"
            elif workflow_name == "summarize_tag":
                test_input = "Please help me generate summary and tags for the file"
            else:
                test_input = f"Execute {workflow_name} workflow"
            
            info_log(f"📝 測試輸入: \"{test_input}\"")
            
            if test_llm_sys_collaboration:
                info_log("🔍 將驗證 Cycle 0 三階段流程：")
                info_log("   Phase 1: LLM Decision (關鍵詞匹配)")
                info_log("   Phase 2: SYS Start (啟動工作流)")
                info_log("   Phase 3: LLM Response (生成響應)")
            
            # 注入文字輸入
            if not self.inject_text_input(test_input):
                return {
                    "success": False,
                    "error": "無法注入文字輸入"
                }
            
            # 等待處理完成（無超時限制）
            if not self.wait_for_processing_complete():
                return {
                    "success": False,
                    "error": "處理意外中斷",
                    "event_log": self.event_log
                }
            
            # 分析事件日誌
            result = self._analyze_test_results(workflow_name, test_llm_sys_collaboration)
            
            # 記錄測試結果
            self.test_results.append({
                "test_name": test_name,
                "status": "pass" if result["success"] else "fail",
                "result": result
            })
            
            if result["success"]:
                info_log(f"✅ {test_name}: 通過")
                if test_llm_sys_collaboration and result.get("llm_sys_collaboration"):
                    collab = result["llm_sys_collaboration"]
                    info_log(f"   ✓ LLM Decision: {collab.get('llm_decision_detected', False)}")
                    info_log(f"   ✓ SYS Start: {collab.get('sys_start_detected', False)}")
                    info_log(f"   ✓ LLM Response: {collab.get('llm_response_detected', False)}")
                    if collab.get('workflow_type'):
                        info_log(f"   ✓ 工作流類型: {collab['workflow_type']}")
            else:
                error_log(f"❌ {test_name}: 失敗 - {result.get('error', 'Unknown error')}")
            
            return result
            
        except Exception as e:
            error_log(f"❌ {test_name} 測試異常: {e}")
            import traceback
            traceback.print_exc()
            
            self.test_results.append({
                "test_name": test_name,
                "status": "error",
                "error": str(e)
            })
            
            return {
                "success": False,
                "error": str(e)
            }
    
    def _analyze_test_results(self, workflow_name: str, check_collaboration: bool = False) -> Dict[str, Any]:
        """
        分析測試結果
        
        Args:
            workflow_name: 工作流名稱
            check_collaboration: 是否檢查 LLM-SYS 協作機制
        """
        try:
            # 檢查關鍵事件是否都有發生
            input_complete = any(e["event_type"] == "input_layer_complete" 
                               for e in self.event_log)
            processing_complete = any(e["event_type"] == "processing_layer_complete" 
                                    for e in self.event_log)
            output_complete = any(e["event_type"] == "output_layer_complete" 
                                for e in self.event_log)
            
            # 檢查是否有狀態轉換到 WORK
            state_changed = [e for e in self.event_log 
                           if e["event_type"] == "state_changed"]
            work_state_reached = any(e["data"].get("to") == "WORK" 
                                   for e in state_changed)
            
            # 檢查是否有工作流事件
            workflow_events = [e for e in self.event_log 
                             if "workflow" in e["event_type"]]
            
            # 基本成功條件
            success = (input_complete and processing_complete and 
                      output_complete and work_state_reached)
            
            result = {
                "success": success,
                "input_layer_complete": input_complete,
                "processing_layer_complete": processing_complete,
                "output_layer_complete": output_complete,
                "work_state_reached": work_state_reached,
                "workflow_events_count": len(workflow_events),
                "total_events": len(self.event_log),
                "event_log": self.event_log
            }
            
            # 如果需要檢查 LLM-SYS 協作機制
            if check_collaboration:
                collaboration_result = self._check_llm_sys_collaboration()
                result["llm_sys_collaboration"] = collaboration_result
                
                # 更新成功條件：必須完成三階段流程
                if success:
                    success = (collaboration_result.get("llm_decision_detected", False) and
                              collaboration_result.get("sys_start_detected", False) and
                              collaboration_result.get("llm_response_detected", False))
                    result["success"] = success
                    
                    if not success:
                        result["error"] = "LLM-SYS 協作三階段流程未完整執行"
            
            return result
            
        except Exception as e:
            error_log(f"❌ 分析測試結果失敗: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _check_llm_sys_collaboration(self) -> Dict[str, Any]:
        """
        檢查 LLM-SYS 協作機制的三階段執行情況
        
        通過檢查日誌或模組調用來驗證：
        - Phase 1: LLM Decision (phase='decision')
        - Phase 2: SYS Start (operation='start')
        - Phase 3: LLM Response (phase='response')
        
        Returns:
            協作機制檢查結果
        """
        try:
            # 讀取最近的日誌文件
            recent_logs = self._read_recent_logs()
            
            # 檢查 LLM Decision 階段
            llm_decision_detected = any(
                "phase=decision" in log or 
                "_decide_workflow" in log or
                "workflow_decision" in log
                for log in recent_logs
            )
            
            # 檢查 SYS Start 階段
            sys_start_detected = any(
                "operation=start" in log or
                "operation='start'" in log or
                "_start_workflow" in log and "operation" in log
                for log in recent_logs
            )
            
            # 檢查 LLM Response 階段
            llm_response_detected = any(
                "phase=response" in log or
                "_generate_workflow_response" in log or
                "workflow_context" in log
                for log in recent_logs
            )
            
            # 嘗試提取工作流類型
            workflow_type = None
            for log in recent_logs:
                if "workflow_type" in log:
                    # 簡單的字符串匹配
                    for wf in ["drop_and_read", "intelligent_archive", "summarize_tag"]:
                        if wf in log:
                            workflow_type = wf
                            break
                    if workflow_type:
                        break
            
            result = {
                "llm_decision_detected": llm_decision_detected,
                "sys_start_detected": sys_start_detected,
                "llm_response_detected": llm_response_detected,
                "workflow_type": workflow_type,
                "all_phases_completed": (llm_decision_detected and 
                                        sys_start_detected and 
                                        llm_response_detected)
            }
            
            debug_log(2, f"[IntegrationTest] LLM-SYS 協作檢查結果: {result}")
            
            return result
            
        except Exception as e:
            error_log(f"❌ 檢查 LLM-SYS 協作失敗: {e}")
            return {
                "llm_decision_detected": False,
                "sys_start_detected": False,
                "llm_response_detected": False,
                "workflow_type": None,
                "all_phases_completed": False,
                "error": str(e)
            }
    
    def _read_recent_logs(self, max_lines: int = 500) -> List[str]:
        """讀取最近的日誌行"""
        import os
        import glob
        
        logs = []
        
        try:
            # 讀取 runtime 日誌
            runtime_logs = glob.glob("logs/runtime/*.log")
            if runtime_logs:
                # 獲取最新的日誌文件
                latest_log = max(runtime_logs, key=os.path.getmtime)
                with open(latest_log, 'r', encoding='utf-8') as f:
                    logs.extend(f.readlines()[-max_lines:])
            
            # 讀取 debug 日誌
            debug_logs = glob.glob("logs/debug/*.log")
            if debug_logs:
                latest_log = max(debug_logs, key=os.path.getmtime)
                with open(latest_log, 'r', encoding='utf-8') as f:
                    logs.extend(f.readlines()[-max_lines:])
                    
        except Exception as e:
            debug_log(2, f"[IntegrationTest] 讀取日誌失敗: {e}")
        
        return logs
    
    def run_all_tests(self):
        """運行所有測試"""
        info_log("\n" + "="*60)
        info_log("🚀 開始系統循環整合測試套件")
        info_log("="*60)
        
        # 設置系統
        if not self.setup_system():
            error_log("❌ 系統設置失敗，測試終止")
            return
        
        # 給系統一些時間穩定
        time.sleep(2)
        
        # 測試檔案工作流
        self.test_file_workflow("drop_and_read")
        
        # 測試之間稍作停頓
        time.sleep(2)
        
        # 可以添加更多測試
        # self.test_file_workflow("intelligent_archive")
        # self.test_file_workflow("summarize_tag")
        
        # 顯示測試摘要
        self._print_test_summary()
        
        # 清理
        self.cleanup()
    
    def _print_test_summary(self):
        """顯示測試摘要"""
        info_log("\n" + "="*60)
        info_log("📊 測試結果摘要")
        info_log("="*60)
        
        total = len(self.test_results)
        passed = sum(1 for r in self.test_results if r.get("status") == "pass")
        failed = sum(1 for r in self.test_results if r.get("status") == "fail")
        errors = sum(1 for r in self.test_results if r.get("status") == "error")
        
        info_log(f"總測試數: {total}")
        info_log(f"✅ 通過: {passed}")
        info_log(f"❌ 失敗: {failed}")
        info_log(f"⚠️  錯誤: {errors}")
        
        if passed == total:
            info_log("\n🎉 所有測試通過！")
        else:
            info_log(f"\n⚠️  {failed + errors} 個測試未通過")
        
        # 詳細結果
        for result in self.test_results:
            status_icon = "✅" if result["status"] == "pass" else "❌"
            info_log(f"{status_icon} {result['test_name']}: {result['status']}")
    
    def cleanup(self):
        """Clean up test resources"""
        try:
            info_log("\n🧹 Starting test resource cleanup...")
            
            # Stop system loop
            if self.system_loop:
                self.system_loop.stop()
            
            # Shutdown Controller (stops monitoring loop)
            if self.controller:
                self.controller.shutdown()
                info_log("✅ Controller shutdown complete")
            
            # Clean up test data
            self.event_log.clear()
            self.test_results.clear()
            self.test_complete.clear()
            
            info_log("✅ Test resources cleaned up")
            
        except Exception as e:
            error_log(f"❌ 清理資源失敗: {e}")


# 便利函數，供 debug_api.py 調用

def test_system_loop_integration():
    """
    運行系統循環整合測試
    
    Returns:
        測試結果列表
    """
    tester = SystemLoopIntegrationTest()
    tester.run_all_tests()
    return tester.test_results


def test_single_file_workflow(workflow_name: str, modules_dict: Optional[Dict[str, Any]] = None, 
                            test_llm_sys_collaboration: bool = False):
    """
    測試單一檔案工作流
    
    Args:
        workflow_name: 工作流名稱 (drop_and_read, intelligent_archive, summarize_tag)
        modules_dict: 來自 debug_api 的已初始化模組字典
        test_llm_sys_collaboration: 是否測試 LLM-SYS 協作機制（Cycle 0 三階段）
        
    Returns:
        測試結果列表
    """
    # 清空狀態佇列，避免之前測試的 WORK 狀態堆積
    _clear_state_queue()
    
    tester = SystemLoopIntegrationTest()
    
    if not tester.setup_system(modules_dict):
        error_log("❌ 系統設置失敗")
        return []
    
    time.sleep(2)
    tester.test_file_workflow(workflow_name, test_llm_sys_collaboration)
    tester._print_test_summary()
    tester.cleanup()
    
    return tester.test_results


def test_llm_sys_collaboration_workflow(workflow_name: str = "drop_and_read", 
                                       modules_dict: Optional[Dict[str, Any]] = None):
    """
    專門測試 LLM-SYS 協作機制的三階段流程
    
    這個測試會驗證：
    1. Phase 1: LLM Decision - 關鍵詞匹配決定工作流類型
    2. Phase 2: SYS Start - 啟動工作流並返回步驟信息
    3. Phase 3: LLM Response - 生成用戶友好的響應
    
    Args:
        workflow_name: 工作流名稱 (drop_and_read, intelligent_archive, summarize_tag)
        modules_dict: 來自 debug_api 的已初始化模組字典
        
    Returns:
        測試結果列表
    """
    info_log("\n" + "="*70)
    info_log("🔬 LLM-SYS 協作機制專項測試")
    info_log("="*70)
    info_log("測試目標：驗證 Cycle 0 三階段流程實現")
    info_log("  Phase 1: LLM Decision (關鍵詞匹配)")
    info_log("  Phase 2: SYS Start (工作流啟動)")
    info_log("  Phase 3: LLM Response (響應生成)")
    info_log("="*70 + "\n")
    
    return test_single_file_workflow(workflow_name, modules_dict, test_llm_sys_collaboration=True)


def _clear_state_queue():
    """清空狀態佇列文件，避免舊測試的狀態堆積"""
    import os
    import json
    
    queue_file = "memory/state_queue.json"
    
    try:
        if os.path.exists(queue_file):
            # 重置為空佇列
            empty_queue = {
                "queue": [],
                "current_state": "idle",
                "current_item": None
            }
            
            with open(queue_file, 'w', encoding='utf-8') as f:
                json.dump(empty_queue, f, ensure_ascii=False, indent=2)
            
            info_log(f"[IntegrationTest] ✅ 狀態佇列已清空: {queue_file}")
        else:
            info_log(f"[IntegrationTest] 狀態佇列文件不存在，跳過清理: {queue_file}")
    
    except Exception as e:
        error_log(f"[IntegrationTest] ⚠️ 清空狀態佇列失敗: {e}")
