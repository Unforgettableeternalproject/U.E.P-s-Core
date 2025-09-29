# modules/llm_module/gemini_client.py

import os
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

class GeminiWrapper:
    def __init__(self, config: dict):
        self.model_name = config.get("model", "gemini-2.5-flash-lite")
        self.temperature = config.get("temperature", 0.8)
        self.top_p = config.get("top_p", 0.95)
        self.max_tokens = config.get("max_output_tokens", 8192)

        # 安全設定可用 config 控制，這裡先寫死為 OFF
        self.safety_settings = [
            types.SafetySetting(category=str(item["category"]), threshold=str(item["threshold"]))
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
                    "nullable": True,
                    "properties": {
                        "action_type": {
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
                    "required": ["action_type", "target", "reason"],
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



    # [修改] 允許 str 或 list[str]
    def query(self, prompt: str, mode: str = "chat", cached_content=None) -> dict:
        contents = [types.Content(role="user", parts=[types.Part(text=prompt)])]
        schema = self.response_schemas.get(mode, self.response_schemas["chat"])

        config = types.GenerateContentConfig(
            temperature=self.temperature,
            top_p=self.top_p,
            max_output_tokens=self.max_tokens,
            response_mime_type="application/json",
            response_schema=schema,
            safety_settings=self.safety_settings
        )

        # [修改] 支援單一 id 或多個 id
        if self.cache_enabled and cached_content:
            if isinstance(cached_content, (list, tuple)):
                config.cached_content = list(cached_content)
            else:
                config.cached_content = cached_content

        result = self.client.models.generate_content(
            model=self.model_name,
            contents=contents,
            config=config
        )

        part = result.candidates[0].content.parts[0]

        import json
        payload = {}
        if hasattr(part, 'text') and part.text:
            payload = json.loads(part.text)
        elif hasattr(part, 'struct') and part.struct:
            payload = part.struct
        else:
            payload = {"text": "❌ Gemini 未產出有效回應"}

        # [建議] 把快取命中資訊帶回去，方便 Debug GUI 顯示
        meta = getattr(result, "usage_metadata", None)
        payload["_meta"] = {
            "cached_input_tokens": getattr(meta, "cached_content_used_input_tokens", 0) if meta else 0,
            "total_input_tokens": getattr(meta, "total_token_count", 0) if meta else 0,
        }
        return payload
