# demo_controller.py - 展示 LLM 到 SYS 的完整流程

import sys
sys.path.append('.')

from modules.llm_module.llm_module import LLMModule
from modules.sys_module.sys_module import SYSModule
from utils.debug_helper import debug_log, info_log, error_log

class SimplifiedController:
    """簡化的 Controller 展示 LLM → SYS 整合"""
    
    def __init__(self):
        self.llm = LLMModule()
        self.sys = SYSModule()
        
        # 初始化模組
        self.llm.initialize()
        self.sys.initialize()
        
        print("✅ Controller 初始化完成")
    
    def process_user_command(self, user_input: str) -> dict:
        """處理用戶指令的完整流程"""
        
        print(f"\n📝 用戶輸入: {user_input}")
        
        # 步驟1: LLM 分析用戶指令
        llm_result = self.llm.handle({
            "text": user_input,
            "intent": "command",
            "memory": ""
        })
        
        print(f"🧠 LLM 分析結果: {llm_result.get('text', '')}")
        
        # 步驟2: 檢查是否有系統動作建議
        sys_action = llm_result.get("sys_action")
        if not sys_action:
            print("ℹ️ LLM 未建議執行系統動作")
            return {
                "status": "completed",
                "response": llm_result.get("text", ""),
                "action_taken": None
            }
        
        print(f"⚙️ LLM 建議的系統動作: {sys_action}")
        
        # 步驟3: 根據系統動作執行相應操作
        if sys_action["action"] == "start_workflow":
            return self._handle_workflow_start(sys_action, user_input, llm_result)
        elif sys_action["action"] == "execute_function":
            return self._handle_direct_function(sys_action, llm_result)
        else:
            print(f"⚠️ 未知的系統動作類型: {sys_action['action']}")
            return {
                "status": "error",
                "response": "系統無法處理此動作類型",
                "action_taken": None
            }
    
    def _handle_workflow_start(self, sys_action: dict, user_input: str, llm_result: dict) -> dict:
        """處理工作流程啟動"""
        workflow_type = sys_action.get("workflow_type")
        params = sys_action.get("params", {})
        
        print(f"🚀 啟動工作流程: {workflow_type}")
        
        try:
            # 啟動 SYS 工作流程
            workflow_result = self.sys.handle({
                "mode": "start_workflow",
                "params": {
                    "workflow_type": workflow_type,
                    "command": user_input,
                    **params
                }
            })
            
            if workflow_result.get("status") == "success":
                session_id = workflow_result.get("session_id")
                print(f"✅ 工作流程啟動成功，Session ID: {session_id}")
                
                return {
                    "status": "workflow_started",
                    "response": llm_result.get("text", ""),
                    "session_id": session_id,
                    "workflow_type": workflow_type,
                    "action_taken": "start_workflow",
                    "next_prompt": workflow_result.get("prompt", "")
                }
            else:
                print(f"❌ 工作流程啟動失敗: {workflow_result}")
                return {
                    "status": "error",
                    "response": "工作流程啟動失敗",
                    "action_taken": None
                }
                
        except Exception as e:
            error_log(f"工作流程啟動異常: {e}")
            return {
                "status": "error",
                "response": f"系統錯誤: {str(e)}",
                "action_taken": None
            }
    
    def _handle_direct_function(self, sys_action: dict, llm_result: dict) -> dict:
        """處理直接功能執行"""
        function_name = sys_action.get("function_name")
        params = sys_action.get("params", {})
        
        print(f"🔧 執行功能: {function_name}")
        
        try:
            # 這裡可以根據 function_name 調用對應的 SYS 功能
            # 暫時返回模擬結果
            print(f"📋 功能參數: {params}")
            
            return {
                "status": "function_executed",
                "response": llm_result.get("text", ""),
                "function_name": function_name,
                "action_taken": "execute_function",
                "params": params
            }
            
        except Exception as e:
            error_log(f"功能執行異常: {e}")
            return {
                "status": "error", 
                "response": f"功能執行失敗: {str(e)}",
                "action_taken": None
            }
    
    def continue_workflow(self, session_id: str, user_input: str) -> dict:
        """繼續工作流程"""
        print(f"\n🔄 繼續工作流程 {session_id}: {user_input}")
        
        try:
            result = self.sys.handle({
                "mode": "continue_workflow",
                "params": {
                    "session_id": session_id,
                    "user_input": user_input
                }
            })
            
            print(f"✅ 工作流程繼續結果: {result}")
            return result
            
        except Exception as e:
            error_log(f"工作流程繼續異常: {e}")
            return {
                "status": "error",
                "response": f"工作流程繼續失敗: {str(e)}"
            }

def demo_llm_sys_integration():
    """演示 LLM-SYS 整合的完整流程"""
    
    print("🎯 開始 LLM-SYS 整合演示")
    
    controller = SimplifiedController()
    
    # 測試指令列表
    test_commands = [
        "幫我測試一個簡單的回顯功能",
        "查詢台北的天氣資訊", 
        "幫我整理桌面上的檔案",
        "設定一個提醒",
        "截個圖並加上標註"
    ]
    
    for i, command in enumerate(test_commands, 1):
        print(f"\n{'='*50}")
        print(f"📋 測試 {i}: {command}")
        print('='*50)
        
        try:
            result = controller.process_user_command(command)
            
            print(f"\n📊 處理結果:")
            print(f"   狀態: {result.get('status')}")
            print(f"   回應: {result.get('response', '')[:100]}...")
            print(f"   動作: {result.get('action_taken')}")
            
            # 如果啟動了工作流程，嘗試繼續
            if result.get("status") == "workflow_started":
                session_id = result.get("session_id")
                
                if result.get("workflow_type") == "echo":
                    # 對 echo 工作流程提供測試輸入
                    print(f"\n🔄 為 echo 工作流程提供測試輸入...")
                    continue_result = controller.continue_workflow(session_id, "Hello from demo!")
                    print(f"   繼續結果: {continue_result.get('status')}")
                
        except Exception as e:
            print(f"❌ 測試失敗: {e}")
            import traceback
            traceback.print_exc()
    
    print("\n🎉 演示完成")

if __name__ == "__main__":
    demo_llm_sys_integration()
