"""
MISCHIEF 執行引擎

負責解析 LLM 的行為規劃並依序執行。
"""

import json
from typing import Dict, Any, List, Tuple, Optional
import time

from . import MischiefAction, MischiefActionRegistry
from utils.debug_helper import info_log, debug_log, error_log, SYSTEM_LEVEL, KEY_LEVEL

# 創建全局註冊器實例（會被 loader.py 重新指定）
mischief_registry = MischiefActionRegistry()


class MischiefExecutor:
    """
    MISCHIEF 行為執行器
    
    負責：
    1. 解析 LLM 返回的行為序列
    2. 依序執行每個行為
    3. 處理執行失敗
    4. 統計執行結果
    """
    
    def __init__(self):
        self.execution_history: List[Dict[str, Any]] = []
        info_log("[MischiefExecutor] 執行引擎初始化")
    
    def parse_llm_response(self, response: str) -> Tuple[bool, List[Dict[str, Any]]]:
        """
        解析 LLM 返回的行為規劃
        
        預期格式 (JSON):
        {
            "actions": [
                {
                    "action_id": "MoveWindowAction",
                    "params": {}
                },
                {
                    "action_id": "CreateTextFileAction",
                    "params": {"message": "..."}
                }
            ]
        }
        
        Returns:
            (success, actions_list)
        """
        try:
            # 嘗試解析 JSON
            data = json.loads(response)
            
            if "actions" not in data:
                error_log("[MischiefExecutor] LLM 回應缺少 'actions' 欄位")
                return False, []
            
            actions = data["actions"]
            
            if not isinstance(actions, list):
                error_log("[MischiefExecutor] 'actions' 欄位必須是列表")
                return False, []
            
            # 驗證每個行為
            validated_actions = []
            for i, action_data in enumerate(actions):
                if not isinstance(action_data, dict):
                    debug_log(2, f"[MischiefExecutor] 行為 {i} 格式錯誤，跳過")
                    continue
                
                action_id = action_data.get("action_id")
                if not action_id:
                    debug_log(2, f"[MischiefExecutor] 行為 {i} 缺少 action_id，跳過")
                    continue
                
                # 檢查行為是否已註冊
                if not mischief_registry.get_action(action_id):
                    debug_log(2, f"[MischiefExecutor] 行為 {action_id} 未註冊，跳過")
                    continue
                
                params = action_data.get("params", {})
                validated_actions.append({
                    "action_id": action_id,
                    "params": params
                })
            
            info_log(f"[MischiefExecutor] 解析出 {len(validated_actions)} 個有效行為")
            return True, validated_actions
            
        except json.JSONDecodeError as e:
            error_log(f"[MischiefExecutor] JSON 解析失敗: {e}")
            return False, []
        except Exception as e:
            error_log(f"[MischiefExecutor] 解析行為規劃失敗: {e}")
            return False, []
    
    def execute_actions(self, actions: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        執行行為序列
        
        Args:
            actions: 行為列表（來自 parse_llm_response）
            
        Returns:
            執行結果統計，包含：
            - total, success, failed, skipped, details
            - speech_texts: 需要 TTS 處理的文字列表（如果有 Speak action）
        """
        results = {
            "total": len(actions),
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "details": [],
            "speech_texts": []  # 收集所有需要說的話
        }
        
        info_log(f"[MischiefExecutor] 開始執行 {len(actions)} 個行為")
        
        for i, action_data in enumerate(actions):
            action_id = action_data["action_id"]
            params = action_data["params"]
            
            debug_log(2, f"[MischiefExecutor] [{i+1}/{len(actions)}] 執行: {action_id}")
            
            # 獲取行為實例
            action = mischief_registry.get_action(action_id)
            
            if not action:
                results["skipped"] += 1
                results["details"].append({
                    "action_id": action_id,
                    "status": "skipped",
                    "message": "行為未找到"
                })
                continue
            
            # 執行行為
            try:
                success, message = action.execute(params)
                
                if success:
                    results["success"] += 1
                    results["details"].append({
                        "action_id": action_id,
                        "status": "success",
                        "message": message
                    })
                    info_log(f"[MischiefExecutor] ✓ {action_id}: {message}")
                    
                    # 特殊處理：如果是 Speak action，收集文字
                    if action_id == "SpeakAction":
                        results["speech_texts"].append(message)
                        debug_log(SYSTEM_LEVEL, f"[MischiefExecutor] 收集語音文字: {message[:30]}...")
                else:
                    results["failed"] += 1
                    results["details"].append({
                        "action_id": action_id,
                        "status": "failed",
                        "message": message
                    })
                    debug_log(SYSTEM_LEVEL, f"[MischiefExecutor] ✗ {action_id}: {message}")
                
                # 執行間隔（避免太快）
                time.sleep(0.5)
                
            except Exception as e:
                results["failed"] += 1
                error_msg = f"執行異常: {str(e)}"
                results["details"].append({
                    "action_id": action_id,
                    "status": "failed",
                    "message": error_msg
                })
                error_log(f"[MischiefExecutor] ✗ {action_id} 異常: {e}")
        
        # 記錄到歷史
        self.execution_history.append({
            "timestamp": time.time(),
            "results": results
        })
        
        info_log(f"[MischiefExecutor] 執行完成: "
                 f"成功 {results['success']}, "
                 f"失敗 {results['failed']}, "
                 f"跳過 {results['skipped']}")
        
        return results
    
    def get_available_actions_for_llm(self, mood: float) -> str:
        """
        獲取可用行為列表的 JSON 格式（供 LLM 參考）
        
        Args:
            mood: 當前情緒值
            
        Returns:
            JSON 字串
        """
        available = mischief_registry.get_available_actions(mood)
        
        prompt_data = {
            "available_actions": available,
            "instructions": (
                "請根據當前情緒和系統狀態，選擇 1-5 個行為組成搗蛋序列。\n"
                "回應格式必須是 JSON:\n"
                "{\n"
                '  "actions": [\n'
                '    {"action_id": "...", "params": {...}},\n'
                "    ...\n"
                "  ]\n"
                "}\n"
                "注意：\n"
                "- 只能使用上述 available_actions 中的行為\n"
                "- required_params 必須提供\n"
                "- 行為會依序執行，失敗則跳過"
            )
        }
        
        return json.dumps(prompt_data, ensure_ascii=False, indent=2)


# 全局執行器實例
mischief_executor = MischiefExecutor()
