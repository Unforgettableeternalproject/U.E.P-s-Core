"""
Gemini Backend — Google Gemini / Vertex AI 實作

從 U.E.P Core 的 gemini_client.py 遷出並改造。
解耦：移除 U.E.P 的 debug_helper，改用標準 logging。
支援：
- Vertex AI 和 API Key 兩種認證
- Context caching（顯性 + 隱性）
- Function calling / JSON schema 模式
- Layer 2/3 摘要與完整壓縮
"""

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from google import genai
from google.genai import types

from session_control.backends.base import AbstractBackend

if TYPE_CHECKING:
    from session_control.cache.local_cache import CacheManager
from session_control.compression.full_compaction import COMPACTION_PROMPT
from session_control.compression.rolling_summary import SUMMARY_PROMPT
from session_control.schemas.message import Message
from session_control.schemas.session import SacredContent

logger = logging.getLogger(__name__)


class GeminiBackend(AbstractBackend):
    """Google Gemini / Vertex AI 後端

    融合 U.E.P Core GeminiWrapper 的完整實作，
    並符合 AbstractBackend 介面。
    """

    CONTEXT_WINDOWS = {
        "gemini-2.5-flash-lite": 1048576,
        "gemini-2.5-flash": 1048576,
        "gemini-2.5-pro": 1048576,
    }

    def __init__(self, config: Optional[dict] = None, cache_manager: Optional["CacheManager"] = None):
        """初始化 GeminiBackend

        Args:
            config: 設定字典，支援以下鍵值：
                - model: 模型名稱（預設 "gemini-2.5-flash-lite"）
                - temperature: 生成溫度（預設 0.8）
                - top_p: Top-p 採樣（預設 0.95）
                - max_output_tokens: 最大輸出 token 數（預設 8192）
                - safety_settings: 安全設定列表
                - cache_enabled: 是否啟用 Context Caching（預設 True）
                - api_key: API Key 認證（選填，優先使用 Vertex AI）
                - project_id: GCP 專案 ID（選填，覆蓋環境變數）
                - location: GCP 位置（預設 "us-central1"）
            cache_manager: 外部注入的 CacheManager 實例（可選）。
                           提供時，query() 會自動查詢顯性快取。
        """
        cfg = config or {}

        self.model_name = cfg.get("model", "gemini-2.5-flash-lite")
        self.temperature = cfg.get("temperature", 0.8)
        self.top_p = cfg.get("top_p", 0.95)
        self.max_tokens = cfg.get("max_output_tokens", 8192)

        # 安全設定，預設為低風險攔截
        self.safety_settings = [
            types.SafetySetting(
                category=str(item["category"]),
                threshold=str(item["threshold"]),
            )  # type: ignore
            for item in cfg.get(
                "safety_settings",
                [
                    {
                        "category": "HARM_CATEGORY_HATE_SPEECH",
                        "threshold": "BLOCK_LOW_AND_ABOVE",
                    },
                    {
                        "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                        "threshold": "BLOCK_LOW_AND_ABOVE",
                    },
                    {
                        "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                        "threshold": "BLOCK_LOW_AND_ABOVE",
                    },
                    {
                        "category": "HARM_CATEGORY_HARASSMENT",
                        "threshold": "BLOCK_LOW_AND_ABOVE",
                    },
                ],
            )
        ]

        # Vertex AI 或 API Key 認證參數
        self._api_key = cfg.get("api_key")
        self._project_id = cfg.get("project_id") or os.getenv("GCP_PROJECT_ID")
        self._location = cfg.get("location", os.getenv("GCP_LOCATION", "us-central1"))

        # 延遲初始化 client
        self._client = None

        # Context Caching 支援
        self.cache_enabled = cfg.get("cache_enabled", True)

        # CacheManager 整合（可選）
        self._cache_manager = cache_manager

        # 根據處理模式動態生成回應 schema
        self.response_schemas = self._create_response_schemas()

    # -------------------------------------------------------------------------
    # Client 初始化
    # -------------------------------------------------------------------------

    def _get_client(self):
        """延遲初始化 Gemini client（Vertex AI 優先，其次 API Key）"""
        if self._client is None:
            try:
                if self._project_id:
                    self._client = genai.Client(
                        vertexai=True,
                        project=self._project_id,
                        location=self._location,
                    )
                    logger.info(
                        "[Gemini] 使用 Vertex AI 認證，project=%s, location=%s",
                        self._project_id,
                        self._location,
                    )
                else:
                    self._client = genai.Client(api_key=self._api_key)
                    logger.info("[Gemini] 使用 API Key 認證")
            except ImportError:
                raise ImportError(
                    "google-genai package required. Install with: pip install google-genai"
                )
        return self._client

    # -------------------------------------------------------------------------
    # AbstractBackend 介面實作
    # -------------------------------------------------------------------------

    def get_context_window_size(self) -> int:
        """取得此後端的 context window 大小"""
        return self.CONTEXT_WINDOWS.get(self.model_name, 1048576)

    def count_tokens(self, text: str) -> int:
        """Gemini 的 token 計數 — 無本地 tokenizer，使用字元估算"""
        cjk_count = sum(1 for c in text if "一" <= c <= "鿿")
        ascii_count = len(text) - cjk_count
        return (ascii_count // 4) + (cjk_count * 2 // 3) + 1

    async def summarize(
        self,
        messages: List[Message],
        instructions: Optional[str] = None,
    ) -> str:
        """Layer 2 滾動摘要

        將訊息列表摘要為文本，使用 SUMMARY_PROMPT 作為預設指令。
        透過 asyncio.to_thread() 包裝同步 API 呼叫，避免阻塞 event loop。
        """
        prompt_text = instructions or SUMMARY_PROMPT
        conversation_text = self._format_for_summary(messages)
        full_prompt = f"{prompt_text}\n\n---\n\n{conversation_text}"

        result = await asyncio.to_thread(self.query, prompt=full_prompt, mode="direct")
        return result.get("text", "")

    async def compact(
        self,
        messages: List[Message],
        sacred: SacredContent,
        instructions: Optional[str] = None,
    ) -> str:
        """Layer 3 完整壓縮

        將完整對話壓縮為結構化摘要，保留神聖內容。
        透過 asyncio.to_thread() 包裝同步 API 呼叫，避免阻塞 event loop。
        """
        sacred_text = sacred.total_content
        prompt_template = instructions or COMPACTION_PROMPT
        prompt_text = prompt_template.replace("{sacred_content}", sacred_text)
        conversation_text = self._format_for_summary(messages)
        full_prompt = (
            f"{prompt_text}\n\n---\nCONVERSATION TO COMPRESS:\n---\n\n{conversation_text}"
        )

        result = await asyncio.to_thread(self.query, prompt=full_prompt, mode="direct")
        raw = result.get("text", "")
        return self._extract_summary(raw)

    # -------------------------------------------------------------------------
    # Response Schema 生成
    # -------------------------------------------------------------------------

    def _create_response_schemas(self) -> dict:
        """創建不同模式的回應 Schema"""
        return {
            "chat": self._create_chat_schema(),
            "work": self._create_work_schema(),
            "direct": self._create_direct_schema(),
            "internal": self._create_internal_schema(),
        }

    def _create_chat_schema(self) -> dict:
        """創建 CHAT 模式的回應 Schema — 與 MEM 協作"""
        return {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "自然的對話回應文字",
                },
                "confidence": {
                    "type": "number",
                    "description": "回應信心度 (0.0-1.0)",
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
                "status_updates": {
                    "type": "object",
                    "nullable": True,
                    "properties": {
                        "mood_delta": {
                            "type": "number",
                            "description": "情緒變化量 (-1.0 到 +1.0)",
                            "minimum": -1.0,
                            "maximum": 1.0,
                        },
                        "pride_delta": {
                            "type": "number",
                            "description": "自尊變化量 (-1.0 到 +1.0)",
                            "minimum": -1.0,
                            "maximum": 1.0,
                        },
                        "helpfulness_delta": {
                            "type": "number",
                            "description": "助人意願變化量 (-1.0 到 +1.0)",
                            "minimum": -1.0,
                            "maximum": 1.0,
                        },
                        "boredom_delta": {
                            "type": "number",
                            "description": "無聊程度變化量 (-1.0 到 +1.0)",
                            "minimum": -1.0,
                            "maximum": 1.0,
                        },
                    },
                    "description": "根據對話內容建議的系統狀態更新",
                },
                "memory_observation": {
                    "type": "string",
                    "description": "對話觀察摘要，用於記憶處理",
                },
                "learning_signals": {
                    "type": "object",
                    "nullable": True,
                    "properties": {
                        "formality_signal": {
                            "type": "number",
                            "description": "正式程度信號 (-1.0=非正式, 0=中性, 1.0=正式)",
                            "minimum": -1.0,
                            "maximum": 1.0,
                        },
                        "detail_signal": {
                            "type": "number",
                            "description": "詳細程度信號 (-1.0=簡潔, 0=適中, 1.0=詳細)",
                            "minimum": -1.0,
                            "maximum": 1.0,
                        },
                        "technical_signal": {
                            "type": "number",
                            "description": "技術程度信號 (-1.0=通俗, 0=適中, 1.0=專業)",
                            "minimum": -1.0,
                            "maximum": 1.0,
                        },
                        "interaction_signal": {
                            "type": "number",
                            "description": "互動偏好信號 (-1.0=獨立, 0=適中, 1.0=互動)",
                            "minimum": -1.0,
                            "maximum": 1.0,
                        },
                    },
                    "description": "用戶偏好學習信號，累積多次後形成用戶畫像",
                },
                "session_control": {
                    "type": "object",
                    "nullable": True,
                    "properties": {
                        "should_end_session": {
                            "type": "boolean",
                            "description": "是否應該結束當前對話會話",
                        },
                        "end_reason": {
                            "type": "string",
                            "enum": [
                                "natural_conclusion",
                                "user_goodbye",
                                "task_completed",
                                "no_further_input",
                            ],
                            "description": "建議結束會話的原因",
                        },
                        "confidence": {
                            "type": "number",
                            "description": "結束會話建議的信心度",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                    },
                    "description": "會話控制建議，由 LLM 判斷對話是否應該結束",
                },
            },
            "required": ["text", "confidence"],
        }

    def _create_work_schema(self) -> dict:
        """創建 WORK 模式的回應 Schema — 與 SYS 協作"""
        return {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "任務導向的回應文字",
                },
                "confidence": {
                    "type": "number",
                    "description": "任務執行信心度 (0.0-1.0)",
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
                "sys_action": {
                    "type": "object",
                    "nullable": False,
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [
                                "start_workflow",
                                "execute_function",
                                "provide_options",
                            ],
                            "description": "系統動作類型",
                        },
                        "target": {
                            "type": "string",
                            "description": "動作目標（工作流名稱或功能名稱）",
                        },
                        "parameters": {
                            "type": "object",
                            "description": "動作參數",
                        },
                        "confidence": {
                            "type": "number",
                            "description": "動作建議的信心度",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                        "requires_confirmation": {
                            "type": "boolean",
                            "description": "是否需要用戶確認",
                        },
                        "reason": {
                            "type": "string",
                            "description": "選擇此動作的詳細理由",
                        },
                    },
                    "required": ["action", "target", "reason"],
                    "description": "建議的系統動作",
                },
                "status_updates": {
                    "type": "object",
                    "nullable": True,
                    "properties": {
                        "helpfulness_delta": {
                            "type": "number",
                            "description": "完成任務後的助人意願變化",
                            "minimum": -1.0,
                            "maximum": 1.0,
                        },
                        "pride_delta": {
                            "type": "number",
                            "description": "任務成功/失敗對自尊的影響",
                            "minimum": -1.0,
                            "maximum": 1.0,
                        },
                        "mood_delta": {
                            "type": "number",
                            "description": "工作完成狀況對情緒的影響",
                            "minimum": -1.0,
                            "maximum": 1.0,
                        },
                        "boredom_delta": {
                            "type": "number",
                            "description": "任務複雜度對無聊程度的影響",
                            "minimum": -1.0,
                            "maximum": 1.0,
                        },
                        "reason": {
                            "type": "string",
                            "description": "狀態變化原因",
                        },
                    },
                    "description": "基於任務執行狀況的狀態更新",
                },
                "session_control": {
                    "type": "object",
                    "nullable": True,
                    "properties": {
                        "should_end_session": {
                            "type": "boolean",
                            "description": "是否應該結束當前工作會話",
                        },
                        "end_reason": {
                            "type": "string",
                            "enum": [
                                "task_completed",
                                "workflow_finished",
                                "user_satisfied",
                                "cannot_proceed",
                            ],
                            "description": "建議結束會話的原因",
                        },
                        "confidence": {
                            "type": "number",
                            "description": "結束會話建議的信心度",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                    },
                    "description": "會話控制建議，由 LLM 判斷工作是否應該結束",
                },
            },
            "required": ["text", "confidence"],
        }

    def _create_direct_schema(self) -> dict:
        """創建 DIRECT 模式的回應 Schema（純文本，無結構）"""
        return {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "直接回應文字",
                }
            },
            "required": ["text"],
        }

    def _create_internal_schema(self) -> dict:
        """創建 INTERNAL 模式的回應 Schema"""
        return {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "內部系統回應",
                },
                "confidence": {
                    "type": "number",
                    "description": "內部處理信心度",
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
            },
            "required": ["text"],
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
                                "description": "行為 ID（必須來自可用行為列表）",
                            },
                            "params": {
                                "type": "object",
                                "description": "行為參數",
                            },
                        },
                        "required": ["action_id", "params"],
                    },
                }
            },
            "required": ["actions"],
        }

    # -------------------------------------------------------------------------
    # 核心查詢方法
    # -------------------------------------------------------------------------

    def query(
        self,
        prompt: str,
        mode: str = "chat",
        cached_content=None,
        tools=None,
        system_instruction: Optional[str] = None,
        tool_choice: str = "ANY",
    ) -> dict:
        """查詢 Gemini API

        Args:
            prompt: 用戶輸入文字
            mode: 回應模式（chat / work / direct / internal / mischief）
            cached_content: 快取內容 ID，支援單一 id 或 list/tuple
            tools: MCP / Function calling 工具列表
            system_instruction: 自定義系統提示詞（用於 internal / mischief 模式）
            tool_choice: Function calling 模式
                - "ANY"  強制調用工具
                - "AUTO" 自動決定
                - "NONE" 不調用工具

        Returns:
            dict，至少包含 "text" 鍵；若有工具調用則包含 "function_call" 鍵；
            總是包含 "_meta" 鍵（cached/total token 數）。
        """
        # CacheManager 自動查詢：若外部未傳 cached_content，嘗試從 CacheManager 取得
        if cached_content is None and self._cache_manager is not None:
            try:
                cached_content = self._cache_manager.get_cached_content_for_generation(
                    list(self._cache_manager.caches.keys())
                )
            except Exception:
                logger.debug("[Gemini] CacheManager 查詢失敗，繼續無快取模式")

        client = self._get_client()

        contents = [types.Content(role="user", parts=[types.Part(text=prompt)])]

        # 選擇回應 schema
        if mode == "mischief":
            schema = self._create_mischief_schema()
        else:
            schema = self.response_schemas.get(mode, self.response_schemas["chat"])

        config_params: Dict[str, Any] = {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_output_tokens": self.max_tokens,
            "safety_settings": self.safety_settings,
        }

        # 支援自定義系統提示詞（用於 internal 模式或工作流）
        if system_instruction:
            config_params["system_instruction"] = types.Part(text=system_instruction)

        # 提供 tools 時使用 function calling 模式；否則使用 JSON schema 模式
        if tools:
            config_params["tools"] = tools
            config_params["tool_config"] = {
                "function_calling_config": {"mode": tool_choice}
            }
            tool_count = sum(
                len(t.get("function_declarations", [])) for t in tools
            )
            mode_desc = {
                "ANY": "強制調用",
                "AUTO": "自動決定",
                "NONE": "不調用",
            }.get(tool_choice, tool_choice)
            logger.debug(
                "[Gemini] 使用 function calling 模式（%s），工具數量: %d",
                mode_desc,
                tool_count,
            )
        else:
            config_params["response_mime_type"] = "application/json"
            config_params["response_schema"] = schema

        config = types.GenerateContentConfig(**config_params)

        # 支援單一 id 或多個 id 的 Context Caching
        if self.cache_enabled and cached_content:
            if isinstance(cached_content, (list, tuple)):
                config.cached_content = list(cached_content)  # type: ignore
            else:
                config.cached_content = cached_content

        result = client.models.generate_content(
            model=self.model_name,
            contents=contents,  # type: ignore
            config=config,
        )

        # 防禦性檢查：確保 result 和 candidates 不是 None
        if result is None:
            logger.error("[Gemini] API 返回 None")
            return {"text": "Welp...I did not come up with any response, sorry."}

        if (
            not hasattr(result, "candidates")
            or result.candidates is None
            or len(result.candidates) == 0
        ):
            logger.error("[Gemini] API 返回無效的 candidates: %s", result)
            return {"text": "Welp...I did not come up with any valid response, sorry."}

        candidate = result.candidates[0]
        if (
            candidate is None
            or not hasattr(candidate, "content")
            or candidate.content is None
        ):
            logger.error("[Gemini] candidate 或 content 為 None")
            return {"text": "Welp...I did not come up with any content, sorry."}

        # 優先檢查 finish_reason — MALFORMED_FUNCTION_CALL 時 content.parts 通常為空
        # 必須在檢查 parts 之前執行，否則會被提前返回攔截
        if (
            hasattr(candidate, "finish_reason")
            and str(candidate.finish_reason) == "FinishReason.MALFORMED_FUNCTION_CALL"
        ):
            logger.error("[Gemini] 檢測到 MALFORMED_FUNCTION_CALL，Gemini 無法正確調用工具")
            return {
                "text": "",
                "error": "malformed_function_call",
                "finish_reason": "MALFORMED_FUNCTION_CALL",
            }

        # 修復：優先使用 result.text 便利方法（新 SDK 推薦），再嘗試 parts[0]
        if (
            not hasattr(candidate.content, "parts")
            or candidate.content.parts is None
            or len(candidate.content.parts) == 0
        ):
            # content.parts 為空，嘗試使用 result.text 便利方法
            if hasattr(result, "text") and result.text:
                logger.debug("[Gemini] content.parts 為空，但 result.text 可用，使用便利方法")
                return {"text": result.text}
            else:
                logger.error("[Gemini] content.parts 為空且 result.text 不可用")
                if hasattr(result, "prompt_feedback"):
                    logger.debug("[Gemini] prompt_feedback: %s", result.prompt_feedback)
                if hasattr(candidate, "finish_reason"):
                    logger.debug("[Gemini] finish_reason: %s", candidate.finish_reason)
                if hasattr(candidate, "safety_ratings"):
                    logger.debug("[Gemini] safety_ratings: %s", candidate.safety_ratings)
                return {"text": "Sorry, I could not generate any response parts."}

        part = candidate.content.parts[0]  # type: ignore

        payload: Dict[str, Any] = {}

        # 處理 function call 回應
        if hasattr(part, "function_call") and part.function_call:
            # 直接轉換為 dict，避免 dict() 構造函數的類型問題
            args_dict = {}
            if hasattr(part.function_call, "args") and part.function_call.args:
                args_dict = {k: v for k, v in part.function_call.args.items()}

            payload = {
                "function_call": {
                    "name": part.function_call.name,
                    "args": args_dict,
                },
                "text": "",  # function call 時沒有文本回應
            }
        elif hasattr(part, "text") and part.text:
            # 當使用 tools 時，Gemini 可能返回純文本而非 JSON
            if tools:
                # 修復：Gemini 在 function calling 模式下可能返回雙重編碼的 JSON
                try:
                    parsed = json.loads(part.text)
                    if isinstance(parsed, dict) and "text" in parsed:
                        # 解碼內層的 Unicode 轉義序列
                        decoded_text = parsed["text"].encode().decode("unicode_escape")
                        payload = {"text": decoded_text}
                        # 保留其他字段
                        for key, value in parsed.items():
                            if key != "text":
                                payload[key] = value
                    else:
                        payload = {"text": part.text}
                except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
                    # Fallback：當作純文本處理
                    payload = {"text": part.text}
            else:
                try:
                    payload = json.loads(part.text)
                except json.JSONDecodeError:
                    # Fallback：若 JSON 解析失敗，當作純文本處理
                    payload = {"text": part.text}
        elif hasattr(part, "struct") and part.struct:  # type: ignore
            payload = part.struct  # type: ignore
        else:
            payload = {"text": "Gemini did not produce a valid response."}

        # 把快取命中資訊帶回去，方便上層監控顯示
        meta = getattr(result, "usage_metadata", None)
        payload["_meta"] = {
            "cached_input_tokens": (
                getattr(meta, "cached_content_used_input_tokens", 0) if meta else 0
            ),
            "total_input_tokens": (
                getattr(meta, "total_token_count", 0) if meta else 0
            ),
        }
        return payload

    # -------------------------------------------------------------------------
    # 輔助方法
    # -------------------------------------------------------------------------

    def format_messages(self, messages: List[Message]) -> List[Dict[str, Any]]:
        """轉為 Gemini API 格式

        Gemini 只接受 "user" / "model" 兩種 role，
        system / tool 訊息一律對應為 "user"。
        """
        result = []
        for msg in messages:
            role = "user" if msg.role in ("user", "system", "tool") else "model"
            result.append({"role": role, "parts": [{"text": msg.content}]})
        return result

    def _format_for_summary(self, messages: List[Message]) -> str:
        """將訊息列表格式化為純文本，供摘要使用"""
        label_map = {
            "user": "User",
            "assistant": "Assistant",
            "system": "System",
            "tool": "Tool",
        }
        parts = []
        for msg in messages:
            label = label_map.get(msg.role, msg.role)
            parts.append(f"[{label}]: {msg.content}")
        return "\n\n".join(parts)

    @staticmethod
    def _extract_summary(raw: str) -> str:
        """從 LLM 回應中提取 <summary>...</summary> 標籤內的文字"""
        match = re.search(r"<summary>(.*?)</summary>", raw, re.DOTALL)
        if match:
            return match.group(1).strip()
        return raw.strip()
