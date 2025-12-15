# modules/llm_module/gemini_client.py

import os
from typing import Any, Optional
from dotenv import load_dotenv
from google import genai
from google.genai import types
from utils.debug_helper import debug_log, info_log, error_log

load_dotenv()

class GeminiWrapper:
    def __init__(self, config: dict):
        self.model_name = config.get("model", "gemini-2.5-flash-lite")
        self.temperature = config.get("temperature", 0.8)
        self.top_p = config.get("top_p", 0.95)
        self.max_tokens = config.get("max_output_tokens", 8192)

        # 安全設定可用 config 控制，這裡先寫死為 OFF
        self.safety_settings = [
            types.SafetySetting(category=str(item["category"]), threshold=str(item["threshold"])) # type: ignore
            for item in config.get("safety_settings", [
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_LOW_AND_ABOVE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_LOW_AND_ABOVE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_LOW_AND_ABOVE"},
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_LOW_AND_ABOVE"},
            ])
        ]

        self.client = genai.Client(
            vertexai=True,
            project=os.getenv("GCP_PROJECT_ID"),
            location=os.getenv("GCP_LOCATION"),
        )

        # Context Caching 支援
        self.cache_enabled = config.get("cache_enabled", True)
        
        # 根據處理模式動態生成回應 schema
        self.response_schemas = self._create_response_schemas()
    
    def _create_response_schemas(self) -> dict:
        """創建不同模式的回應 Schema"""
        return {
            "chat": self._create_chat_schema(),
            "work": self._create_work_schema(),
            "direct": self._create_direct_schema(),
            "internal": self._create_internal_schema()
        }
    
    def _create_chat_schema(self) -> dict:
        """創建 CHAT 模式的回應 Schema - 與 MEM 協作"""
        return {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "自然的對話回應文字"
                },
                "confidence": {
                    "type": "number",
                    "description": "回應信心度 (0.0-1.0)",
                    "minimum": 0.0,
                    "maximum": 1.0
                },
                "status_updates": {
                    "type": "object",
                    "nullable": True,
                    "properties": {
                        "mood_delta": {
                            "type": "number",
                            "description": "情緒變化量 (-1.0 到 +1.0)",
                            "minimum": -1.0,
                            "maximum": 1.0
                        },
                        "pride_delta": {
                            "type": "number", 
                            "description": "自尊變化量 (-1.0 到 +1.0)",
                            "minimum": -1.0,
                            "maximum": 1.0
                        },
                        "helpfulness_delta": {
                            "type": "number",
                            "description": "助人意願變化量 (-1.0 到 +1.0)",
                            "minimum": -1.0,
                            "maximum": 1.0
                        },
                        "boredom_delta": {
                            "type": "number",
                            "description": "無聊程度變化量 (-1.0 到 +1.0)",
                            "minimum": -1.0,
                            "maximum": 1.0
                        }
                    },
                    "description": "根據對話內容建議的系統狀態更新"
                },
                "memory_observation": {
                    "type": "string",
                    "description": "對話觀察摘要，用於記憶處理"
                },
                "learning_signals": {
                    "type": "object",
                    "nullable": True,
                    "properties": {
                        "formality_signal": {
                            "type": "number",
                            "description": "正式程度信號 (-1.0=非正式, 0=中性, 1.0=正式)",
                            "minimum": -1.0,
                            "maximum": 1.0
                        },
                        "detail_signal": {
                            "type": "number",
                            "description": "詳細程度信號 (-1.0=簡潔, 0=適中, 1.0=詳細)",
                            "minimum": -1.0,
                            "maximum": 1.0
                        },
                        "technical_signal": {
                            "type": "number",
                            "description": "技術程度信號 (-1.0=通俗, 0=適中, 1.0=專業)",
                            "minimum": -1.0,
                            "maximum": 1.0
                        },
                        "interaction_signal": {
                            "type": "number",
                            "description": "互動偏好信號 (-1.0=獨立, 0=適中, 1.0=互動)",
                            "minimum": -1.0,
                            "maximum": 1.0
                        }
                    },
                    "description": "用戶偏好學習信號，累積多次後形成用戶畫像"
                },
                "session_control": {
                    "type": "object",
                    "nullable": True,
                    "properties": {
                        "should_end_session": {
                            "type": "boolean",
                            "description": "是否應該結束當前對話會話"
                        },
                        "end_reason": {
                            "type": "string",
                            "enum": ["natural_conclusion", "user_goodbye", "task_completed", "no_further_input"],
                            "description": "建議結束會話的原因"
                        },
                        "confidence": {
                            "type": "number",
                            "description": "結束會話建議的信心度",
                            "minimum": 0.0,
                            "maximum": 1.0
                        }
                    },
                    "description": "會話控制建議，由 LLM 判斷對話是否應該結束"
                }
            },
            "required": ["text", "confidence"]
        }
    
    def _create_work_schema(self) -> dict:
        """創建 WORK 模式的回應 Schema - 與 SYS 協作"""
        return {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "任務導向的回應文字"
                },
                "confidence": {
                    "type": "number",
                    "description": "任務執行信心度 (0.0-1.0)",
                    "minimum": 0.0,
                    "maximum": 1.0
                },
                "sys_action": {
                    "type": "object",
                    "nullable": False,
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["start_workflow", "execute_function", "provide_options"],
                            "description": "系統動作類型"
                        },
                        "target": {
                            "type": "string",
                            "description": "動作目標 (工作流名稱或功能名稱)"
                        },
                        "parameters": {
                            "type": "object",
                            "description": "動作參數"
                        },
                        "confidence": {
                            "type": "number",
                            "description": "動作建議的信心度",
                            "minimum": 0.0,
                            "maximum": 1.0
                        },
                        "requires_confirmation": {
                            "type": "boolean",
                            "description": "是否需要用戶確認"
                        },
                        "reason": {
                            "type": "string",
                            "description": "選擇此動作的詳細理由"
                        }
                    },
                    "required": ["action", "target", "reason"],
                    "description": "建議的系統動作"
                },
                "status_updates": {
                    "type": "object",
                    "nullable": True,
                    "properties": {
                        "helpfulness_delta": {
                            "type": "number",
                            "description": "完成任務後的助人意願變化",
                            "minimum": -1.0,
                            "maximum": 1.0
                        },
                        "pride_delta": {
                            "type": "number",
                            "description": "任務成功/失敗對自尊的影響",
                            "minimum": -1.0,
                            "maximum": 1.0  
                        },
                        "mood_delta": {
                            "type": "number",
                            "description": "工作完成狀況對情緒的影響",
                            "minimum": -1.0,
                            "maximum": 1.0
                        },
                        "boredom_delta": {
                            "type": "number",
                            "description": "任務複雜度對無聊程度的影響",
                            "minimum": -1.0,
                            "maximum": 1.0
                        },
                        "reason": {
                            "type": "string",
                            "description": "狀態變化原因"
                        }
                    },
                    "description": "基於任務執行狀況的狀態更新"
                },
                "session_control": {
                    "type": "object",
                    "nullable": True,
                    "properties": {
                        "should_end_session": {
                            "type": "boolean",
                            "description": "是否應該結束當前工作會話"
                        },
                        "end_reason": {
                            "type": "string",
                            "enum": ["task_completed", "workflow_finished", "user_satisfied", "cannot_proceed"],
                            "description": "建議結束會話的原因"
                        },
                        "confidence": {
                            "type": "number",
                            "description": "結束會話建議的信心度",
                            "minimum": 0.0,
                            "maximum": 1.0
                        }
                    },
                    "description": "會話控制建議，由 LLM 判斷工作是否應該結束"
                }
            },
            "required": ["text", "confidence"]
        }
    
    def _create_direct_schema(self) -> dict:
        """創建 DIRECT 模式的回應 Schema"""
        return {
            "type": "object", 
            "properties": {
                "text": {
                    "type": "string",
                    "description": "直接回應文字"
                }
            },
            "required": ["text"]
        }
        
    def _create_internal_schema(self) -> dict:
        """創建 INTERNAL 模式的回應 Schema"""
        return {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string", 
                    "description": "內部系統回應"
                },
                "confidence": {
                    "type": "number",
                    "description": "內部處理信心度",
                    "minimum": 0.0,
                    "maximum": 1.0
                }
            },
            "required": ["text"]
        }
    
    def _create_mischief_schema(self) -> dict:
        """創建 MISCHIEF 模式的回應 Schema"""
        return {
            "type": "object",
            "properties": {
                "actions": {
                    "type": "array",
                    "description": "MISCHIEF 行為序列",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action_id": {
                                "type": "string",
                                "description": "行為 ID（必須來自可用行為列表）"
                            },
                            "params": {
                                "type": "object",
                                "description": "行為參數"
                            }
                        },
                        "required": ["action_id", "params"]
                    }
                }
            },
            "required": ["actions"]
        }



    # [修改] 允許 str 或 list[str]
    def query(self, prompt: str, mode: str = "chat", cached_content=None, tools=None, system_instruction: Optional[str] = None, tool_choice: str = "ANY") -> dict:
        """
        查詢 Gemini API
        
        Args:
            prompt: 用戶輸入
            mode: 模式（chat/work/internal/mischief）
            cached_content: 快取內容 ID
            tools: MCP 工具列表
            system_instruction: 自定義系統提示詞（用於 internal/mischief 模式）
            tool_choice: Function calling 模式 ("ANY" 強制調用 | "AUTO" 自動決定 | "NONE" 不調用)
        """
        contents = [types.Content(role="user", parts=[types.Part(text=prompt)])]
        # 支持 mischief 模式
        if mode == "mischief":
            schema = self._create_mischief_schema()
        else:
            schema = self.response_schemas.get(mode, self.response_schemas["chat"])

        config_params = {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_output_tokens": self.max_tokens,
            "safety_settings": self.safety_settings
        }
        
        # 🔧 支援自定義系統提示詞（用於 internal 模式或工作流）
        if system_instruction:
            config_params["system_instruction"] = types.Part(text=system_instruction)
        
        # ✅ 如果提供了 tools，使用 function calling 模式；否則使用 JSON schema 模式
        if tools:
            config_params["tools"] = tools
            # ✅ 根據 tool_choice 參數決定 function calling 模式
            config_params["tool_config"] = {"function_calling_config": {"mode": tool_choice}}
            # 🔍 DEBUG: 記錄 tools 數量和模式
            from utils.debug_helper import debug_log
            tool_count = sum(len(t.get('function_declarations', [])) for t in tools)
            mode_desc = {
                "ANY": "強制調用",
                "AUTO": "自動決定",
                "NONE": "不調用"
            }.get(tool_choice, tool_choice)
            debug_log(3, f"[Gemini] 使用 function calling 模式（{mode_desc}），工具數量: {tool_count}")
        else:
            config_params["response_mime_type"] = "application/json"
            config_params["response_schema"] = schema
        
        config = types.GenerateContentConfig(**config_params)

        # [修改] 支援單一 id 或多個 id
        if self.cache_enabled and cached_content:
            if isinstance(cached_content, (list, tuple)):
                config.cached_content = list(cached_content) # type: ignore
            else:
                config.cached_content = cached_content

        result = self.client.models.generate_content(
            model=self.model_name,
            contents=contents, # type: ignore
            config=config
        )

        # 🔧 防禦性檢查：確保 result 和 candidates 不是 None
        if result is None:
            error_log("[Gemini] API 返回 None")
            return {"text": "Welp...I did not come up with any response, sorry."}
        
        if not hasattr(result, 'candidates') or result.candidates is None or len(result.candidates) == 0:
            error_log(f"[Gemini] API 返回無效的 candidates: {result}")
            return {"text": "Welp...I did not come up with any valid response, sorry."}
        
        candidate = result.candidates[0]
        if candidate is None or not hasattr(candidate, 'content') or candidate.content is None:
            error_log(f"[Gemini] candidate 或 content 為 None")
            return {"text": "Welp...I did not come up with any content, sorry."}
        
        # 🔧 **優先檢查 finish_reason** - MALFORMED_FUNCTION_CALL 時 content.parts 通常為空
        # 必須在檢查 parts 之前執行，否則會被提前返回攔截
        if hasattr(candidate, 'finish_reason') and str(candidate.finish_reason) == 'FinishReason.MALFORMED_FUNCTION_CALL':
            error_log(f"[Gemini] 檢測到 MALFORMED_FUNCTION_CALL，Gemini 無法正確調用工具")
            # 返回錯誤標記，讓上層可以降級處理
            return {
                "text": "",
                "error": "malformed_function_call",
                "finish_reason": "MALFORMED_FUNCTION_CALL"
            }
        
        # 🔧 修復：優先使用 result.text 便利方法（新 SDK 推薦），再嘗試 parts[0]
        part = None
        if not hasattr(candidate.content, 'parts') or candidate.content.parts is None or len(candidate.content.parts) == 0:
            # content.parts 為空，嘗試使用 result.text 便利方法
            if hasattr(result, 'text') and result.text:
                debug_log(3, f"[Gemini] content.parts 為空，但 result.text 可用，使用便利方法")
                return {"text": result.text}
            else:
                error_log(f"[Gemini] content.parts 為空且 result.text 不可用")
                # 記錄更多調試信息
                if hasattr(result, 'prompt_feedback'):
                    debug_log(3, f"[Gemini] prompt_feedback: {result.prompt_feedback}")
                if hasattr(candidate, 'finish_reason'):
                    debug_log(3, f"[Gemini] finish_reason: {candidate.finish_reason}")
                if hasattr(candidate, 'safety_ratings'):
                    debug_log(3, f"[Gemini] safety_ratings: {candidate.safety_ratings}")
                return {"text": "Sorry, I could not generate any response parts."}
        
        part = candidate.content.parts[0] # type: ignore

        import json
        payload: dict[str, Any] = {}
        
        # ✅ 處理 function call 回應
        if hasattr(part, 'function_call') and part.function_call:
            # 修復類型錯誤：直接轉換為 dict，避免 dict() 構造函數的類型問題
            args_dict = {}
            if hasattr(part.function_call, 'args') and part.function_call.args:
                args_dict = {k: v for k, v in part.function_call.args.items()}
            
            payload = {
                "function_call": {
                    "name": part.function_call.name,
                    "args": args_dict
                },
                "text": ""  # function call 時沒有文本回應
            }
        elif hasattr(part, 'text') and part.text:
            # 當使用 tools 時，Gemini 可能返回純文本而非 JSON
            if tools:
                # 🔧 修復：Gemini 在 function calling 模式下可能返回雙重編碼的 JSON
                try:
                    # 嘗試解析外層 JSON
                    parsed = json.loads(part.text)
                    if isinstance(parsed, dict) and 'text' in parsed:
                        # 解碼內層的 Unicode 轉義序列
                        decoded_text = parsed['text'].encode().decode('unicode_escape')
                        payload = {"text": decoded_text}
                        # 保留其他字段
                        for key, value in parsed.items():
                            if key != 'text':
                                payload[key] = value
                    else:
                        payload = {"text": part.text}
                except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
                    # Fallback: 當作純文本處理
                    payload = {"text": part.text}
            else:
                try:
                    payload = json.loads(part.text)
                except json.JSONDecodeError:
                    # Fallback: 若 JSON 解析失敗，當作純文本處理
                    payload = {"text": part.text}
        elif hasattr(part, 'struct') and part.struct:  # type: ignore
            payload = part.struct  # type: ignore
        else:
            payload = {"text": "Gemini did not produce a valid response."}

        # [建議] 把快取命中資訊帶回去，方便 Debug GUI 顯示
        meta = getattr(result, "usage_metadata", None)
        payload["_meta"] = {
            "cached_input_tokens": getattr(meta, "cached_content_used_input_tokens", 0) if meta else 0,
            "total_input_tokens": getattr(meta, "total_token_count", 0) if meta else 0,
        }
        return payload
