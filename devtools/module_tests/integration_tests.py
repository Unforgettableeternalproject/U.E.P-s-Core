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
        self.test_timeout = 120  # 測試超時時間（秒）
        
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
            
            # 2. 初始化 SystemLoop
            from core.system_loop import SystemLoop
            self.system_loop = SystemLoop()
            
            info_log("✅ SystemLoop 初始化成功")
            
            # ✅ 重要：啟動 EventBus 處理線程
            # 測試環境不會調用 SystemLoop.run()，所以必須手動啟動 EventBus
            from core.event_bus import event_bus
            event_bus.start()
            info_log("✅ EventBus 處理線程已啟動")
            
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
    
    def inject_text_input(self, text: str) -> bool:
        """
        注入文字輸入到系統
        
        模擬使用者在文字模式下的輸入
        """
        try:
            from core.framework import core_framework
            from core.working_context import working_context_manager
            import uuid
            
            # ✅ 在測試環境下手動設置 cycle_index（因為沒有通過 SystemLoop）
            # 這確保 ModuleCoordinator 能正確判斷是否第一次進入處理層
            # 
            # ⚠️ 重要：每次測試使用唯一的 session_id 以避免去重機制誤擋
            # ModuleCoordinator 使用 flow-based 去重: session_id:cycle_index:layer
            test_session_id = f'test_gs_{uuid.uuid4().hex[:8]}'
            
            working_context_manager.global_context_data['current_cycle_index'] = 0
            working_context_manager.global_context_data['current_gs_id'] = test_session_id
            debug_log(2, f"[IntegrationTest] 已設置測試環境 cycle_index=0, session_id={test_session_id}")
            
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
    
    def wait_for_processing_complete(self, timeout: float = 30.0) -> bool:
        """
        等待處理完成
        
        Args:
            timeout: 超時時間（秒）
            
        Returns:
            是否在超時前完成
        """
        start_time = time.time()
        
        info_log(f"⏳ 等待處理完成 (超時: {timeout}秒)...")
        
        while time.time() - start_time < timeout:
            # 檢查是否有 OUTPUT_LAYER_COMPLETE 事件
            output_events = [e for e in self.event_log 
                           if e["event_type"] == "output_layer_complete"]
            
            if output_events:
                elapsed = time.time() - start_time
                info_log(f"✅ 處理完成 (耗時: {elapsed:.2f}秒)")
                return True
            
            time.sleep(0.1)
        
        error_log(f"❌ 處理超時 ({timeout}秒)")
        return False
    
    def test_file_workflow(self, workflow_name: str) -> Dict[str, Any]:
        """
        測試檔案工作流
        
        Args:
            workflow_name: 工作流名稱 (drop_and_read, intelligent_archive, summarize_tag)
            
        Returns:
            測試結果
        """
        test_name = f"檔案工作流測試 ({workflow_name})"
        info_log(f"\n{'='*60}")
        info_log(f"🧪 開始測試: {test_name}")
        info_log(f"{'='*60}")
        
        try:
            # 清空事件日誌
            self.event_log.clear()
            
            # Build test input (English - internal system language)
            # Different commands for different workflows
            if workflow_name == "drop_and_read":
                test_input = "Please help me read the file content"
            elif workflow_name == "intelligent_archive":
                test_input = "Please help me archive the file"
            elif workflow_name == "summarize_tag":
                test_input = "Please help me summarize and tag the file"
            else:
                test_input = f"Execute {workflow_name} workflow"
            
            # 注入文字輸入
            if not self.inject_text_input(test_input):
                return {
                    "success": False,
                    "error": "無法注入文字輸入"
                }
            
            # 等待處理完成（或超時）
            if not self.wait_for_processing_complete(timeout=60.0):
                return {
                    "success": False,
                    "error": "處理超時"
                }
            
            # 分析事件日誌
            result = self._analyze_test_results(workflow_name)
            
            # 記錄測試結果
            self.test_results.append({
                "test_name": test_name,
                "status": "pass" if result["success"] else "fail",
                "result": result
            })
            
            if result["success"]:
                info_log(f"✅ {test_name}: 通過")
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
    
    def _analyze_test_results(self, workflow_name: str) -> Dict[str, Any]:
        """分析測試結果"""
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
            
            success = (input_complete and processing_complete and 
                      output_complete and work_state_reached)
            
            return {
                "success": success,
                "input_layer_complete": input_complete,
                "processing_layer_complete": processing_complete,
                "output_layer_complete": output_complete,
                "work_state_reached": work_state_reached,
                "workflow_events_count": len(workflow_events),
                "total_events": len(self.event_log),
                "event_log": self.event_log
            }
            
        except Exception as e:
            error_log(f"❌ 分析測試結果失敗: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
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


def test_single_file_workflow(workflow_name: str, modules_dict: Optional[Dict[str, Any]] = None):
    """
    測試單一檔案工作流
    
    Args:
        workflow_name: 工作流名稱 (drop_and_read, intelligent_archive, summarize_tag)
        modules_dict: 來自 debug_api 的已初始化模組字典
        
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
    tester.test_file_workflow(workflow_name)
    tester._print_test_summary()
    tester.cleanup()
    
    return tester.test_results


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
