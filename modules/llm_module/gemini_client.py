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
                "text": {"type": "string"},
                "mood": {"type": "string"},
                "sys_action": {
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string"},
                                "target": {"type": "string"}
                            },
                            "required": ["action", "target"]
                        },
                        {
                            "type": "null"
                        }
                    ]
                }
            },
            "required": ["text", "mood", "sys_action"]
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
