"""
MISCHIEF 執行引擎

負責解析 LLM 的行為規劃並依序執行。
"""

import json
from typing import Dict, Any, List, Tuple, Optional
import time

from . import MischiefAction
from utils.debug_helper import info_log, debug_log, error_log, SYSTEM_LEVEL, KEY_LEVEL

# 導入全局註冊器實例（在 loader.py 中創建並註冊所有行為）
# 避免循環導入：延遲導入
mischief_registry = None


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
        # 延遲導入 registry 以避免循環導入
        global mischief_registry
        if mischief_registry is None:
            from .loader import mischief_registry as _registry
            mischief_registry = _registry
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
                    
                    # 前端事件（若行為提供定位資訊）
                    frontend_payload = None
                    if hasattr(action, "get_frontend_payload"):
                        try:
                            frontend_payload = action.get_frontend_payload(params)
                        except Exception as e:
                            debug_log(2, f"[MischiefExecutor] 取得前端 payload 失敗: {e}")
                    self._emit_frontend_event(action_id, params, message, frontend_payload)
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
    
    def get_available_actions_for_llm(self, mood: float, intensity: str = "medium") -> str:
        """
        獲取可用行為列表的 JSON 格式（供 LLM 參考）
        
        Args:
            mood: 當前情緒值
            intensity: 搗蛋強度
            
        Returns:
            JSON 字串
        """
        available = mischief_registry.get_available_actions(mood, intensity)
        
        prompt_data = {
            "available_actions": available,
            "instructions": (
                "Plan 1-5 MISCHIEF actions based on the current mood and intensity.\n"
                "Return ONLY JSON in this shape:\n"
                "{\n"
                '  \"actions\": [\n'
                '    {\"action_id\": \"...\", \"params\": {...}},\n'
                "    ...\n"
                "  ]\n"
                "}\n"
                "Rules:\n"
                "- Use only the actions listed in available_actions\n"
                "- Provide all required_params (e.g., text/message content)\n"
                "- Actions execute in order; failures are skipped\n"
                "- Do not include explanations or extra keys"
            )
        }
        
        return json.dumps(prompt_data, ensure_ascii=False, indent=2)

    def _emit_frontend_event(self, action_id: str, params: Dict[str, Any], message: str, frontend_payload: Optional[Dict[str, Any]] = None):
        """將 MISCHIEF 行為通知前端（若 FrontendBridge 存在）"""
        try:
            from core.framework import core_framework
            frontend_bridge = getattr(core_framework, "frontend_bridge", None)
            if frontend_bridge and hasattr(frontend_bridge, "forward_event"):
                payload = frontend_payload or {}
                payload.update({
                    "event": "mischief_action",
                    "action_id": action_id,
                    "params": params or {},
                    "message": message
                })
                frontend_bridge.forward_event(payload)
                debug_log(2, f"[MischiefExecutor] 已通知前端 mischief_action: {action_id}")
        except Exception as e:
            debug_log(2, f"[MischiefExecutor] 通知前端失敗: {e}")


# 全局執行器實例
mischief_executor = MischiefExecutor()
