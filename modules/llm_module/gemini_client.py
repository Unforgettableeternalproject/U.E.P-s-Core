# modules/llm_module/gemini_client.py

import os
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

class GeminiWrapper:
    def __init__(self, config: dict):
        self.model_name = config.get("model", "gemini-2.0-flash-001")
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

        self.response_schema = {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "LLM 生成的回應文字"
                },
                "emotion": {
                    "type": "string", 
                    "description": "情緒標記",
                    "enum": ["neutral", "happy", "sad", "excited", "confused", "helpful", "concerned"]
                },
                "sys_action": {
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["start_workflow", "execute_function"],
                                    "description": "系統動作類型"
                                },
                                "workflow_type": {
                                    "type": "string",
                                    "description": "工作流程類型 (當 action 為 start_workflow 時)"
                                },
                                "function_name": {
                                    "type": "string", 
                                    "description": "具體功能名稱 (當 action 為 execute_function 時)"
                                },
                                "params": {
                                    "type": "object",
                                    "description": "動作參數"
                                },
                                "reason": {
                                    "type": "string",
                                    "description": "選擇此動作的原因說明"
                                }
                            },
                            "required": ["action", "reason"],
                            "description": "系統動作指令 (僅在 command intent 且能找到合適功能時提供)"
                        },
                        {
                            "type": "null"
                        }
                    ]
                }
            },
            "required": ["text", "emotion", "sys_action"]
        }



    def query(self, prompt: str) -> str:
        contents = [
            types.Content(role="user", parts=[types.Part(text=prompt)])
        ]

        config = types.GenerateContentConfig(
            temperature=self.temperature,
            top_p=self.top_p,
            max_output_tokens=self.max_tokens,
            response_mime_type="application/json",
            response_schema=self.response_schema,
            safety_settings=self.safety_settings
        )

        result = self.client.models.generate_content(
            model=self.model_name,
            contents=contents,
            config=config
        )

        part = result.candidates[0].content.parts[0]

        if hasattr(part, 'text') and part.text:
            import json
            return json.loads(part.text)
        elif hasattr(part, 'struct') and part.struct:
            return part.struct
        else:
            return {"text": "❌ Gemini 未產出有效回應"}
