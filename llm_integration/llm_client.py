"""
LLM Client — Gemini/Vertex AI 客戶端封裝

移植自 U.E.P Core 的 GeminiWrapper，精簡為純文字聊天用途。
使用 google-genai SDK（新版統一 SDK）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import yaml

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

logger = logging.getLogger("echo_memory.llm")


def _load_llm_config() -> dict:
    """載入 LLM 配置檔"""
    config_path = Path(__file__).parent / "config.yaml"
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


class LLMClient:
    """
    Gemini LLM 客戶端 — 純文字多輪聊天。

    支援兩種認證方式（自動偵測）：
    1. Vertex AI: GCP_PROJECT_ID + GCP_LOCATION（優先）
    2. API Key:  GOOGLE_API_KEY
    """

    def __init__(
        self,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        character: str | None = None,
    ):
        config = _load_llm_config()

        self.model_name = model or config.get("model", "gemini-2.5-flash-lite")
        self.temperature = temperature if temperature is not None else config.get("temperature", 0.8)
        self.top_p: float = config.get("top_p", 1.0)
        self.max_tokens = max_tokens or config.get("max_output_tokens", 4096)
        self.character = character or config.get("character", "")
        self.memory_preamble: str = config.get("memory_preamble", "")

        # 對話歷史（google-genai 的 Content 格式）
        self._history: list[dict] = []

        # 延遲初始化
        self._client = None
        self._initialized = False

    # ──────────────────────────────────────────────
    # 初始化
    # ──────────────────────────────────────────────

    def _ensure_client(self):
        """延遲初始化 genai.Client"""
        if self._initialized:
            return
        self._initialized = True

        try:
            from google import genai  # noqa: F811
        except ImportError:
            raise ImportError(
                "google-genai 未安裝。請執行: pip install google-genai\n"
                "或使用主專案的 Python 環境。"
            )

        gcp_project = os.getenv("GCP_PROJECT_ID")
        gcp_location = os.getenv("GCP_LOCATION", "us-central1")
        api_key = os.getenv("GOOGLE_API_KEY")

        if gcp_project:
            # Vertex AI 模式
            self._client = genai.Client(
                vertexai=True,
                project=gcp_project,
                location=gcp_location,
            )
            logger.info(f"[LLM] Vertex AI 模式 — project={gcp_project}, location={gcp_location}")
        elif api_key:
            # API Key 模式
            self._client = genai.Client(api_key=api_key)
            logger.info("[LLM] API Key 模式")
        else:
            raise ValueError(
                "需要設定 GCP_PROJECT_ID（Vertex AI）或 GOOGLE_API_KEY（Gemini API）。"
                "請在 .env 檔案中設定。"
            )

        logger.info(f"[LLM] Model: {self.model_name}, Temperature: {self.temperature}")

    # ──────────────────────────────────────────────
    # 核心 API
    # ──────────────────────────────────────────────

    def chat(
        self,
        message: str,
        memory_context: str = "",
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """
        發送訊息並取得回應（多輪對話）。

        Args:
            message: 使用者訊息
            memory_context: 記憶脈絡（來自 MemoryContext.to_text()）
            temperature: 覆蓋預設溫度
            max_tokens: 覆蓋預設 max tokens

        Returns:
            模型回應文字
        """
        self._ensure_client()
        from google.genai import types

        # 構建系統指示
        system_parts = [self.character]
        if memory_context:
            system_parts.append(self.memory_preamble)
            system_parts.append(f"## Relevant Memories\n{memory_context}")
        system_instruction = "\n\n".join(p for p in system_parts if p)

        # 加入當前使用者訊息
        self._history.append({"role": "user", "text": message})

        # 轉換為 API 格式
        contents = [
            types.Content(
                role=turn["role"],
                parts=[types.Part(text=turn["text"])],
            )
            for turn in self._history
        ]

        # 呼叫 API
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=temperature or self.temperature,
            top_p=self.top_p,
            max_output_tokens=max_tokens or self.max_tokens,
        )

        try:
            result = self._client.models.generate_content(
                model=self.model_name,
                contents=contents,
                config=config,
            )

            response_text = self._extract_text(result)

            # 加入模型回應到歷史
            self._history.append({"role": "model", "text": response_text})

            return response_text

        except Exception as e:
            # 移除失敗的使用者訊息
            self._history.pop()
            logger.error(f"[LLM] 生成失敗: {e}")
            raise

    def generate(
        self,
        prompt: str,
        *,
        system_instruction: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """
        單次生成（不記歷史）。用於 Dream Engine 蒸餾等。

        Args:
            prompt: 輸入提示詞
            system_instruction: 系統指示（可選）
            temperature: 覆蓋預設溫度
            max_tokens: 覆蓋預設 max tokens

        Returns:
            生成的文字
        """
        self._ensure_client()
        from google.genai import types

        contents = [types.Content(role="user", parts=[types.Part(text=prompt)])]

        config_params: dict = {
            "temperature": temperature or self.temperature,
            "top_p": self.top_p,
            "max_output_tokens": max_tokens or self.max_tokens,
        }
        if system_instruction:
            config_params["system_instruction"] = system_instruction

        config = types.GenerateContentConfig(**config_params)

        result = self._client.models.generate_content(
            model=self.model_name,
            contents=contents,
            config=config,
        )

        return self._extract_text(result)

    # ──────────────────────────────────────────────
    # 歷史管理
    # ──────────────────────────────────────────────

    def clear_history(self):
        """清空對話歷史"""
        self._history.clear()

    def get_history(self) -> list[dict]:
        """取得對話歷史副本"""
        return list(self._history)

    @property
    def turn_count(self) -> int:
        """對話輪數（一問一答算一輪）"""
        return len(self._history) // 2

    # ──────────────────────────────────────────────
    # 工具方法
    # ──────────────────────────────────────────────

    def as_llm_fn(self) -> callable:
        """
        返回 (str) -> str 的 callable，供 MemoryEngine 的 llm_fn 使用。
        用於 Dream Engine 的蒸餾和重播。
        """
        def _fn(prompt: str) -> str:
            return self.generate(prompt, temperature=0.3, max_tokens=2048)
        return _fn

    @staticmethod
    def _extract_text(result) -> str:
        """從 API 回應中提取文字"""
        # 優先用 result.text 便利方法
        if hasattr(result, "text") and result.text:
            return result.text

        # Fallback: 手動從 candidates 取
        if (
            result
            and hasattr(result, "candidates")
            and result.candidates
            and result.candidates[0].content
            and result.candidates[0].content.parts
        ):
            parts_text = []
            for part in result.candidates[0].content.parts:
                if hasattr(part, "text") and part.text:
                    parts_text.append(part.text)
            if parts_text:
                return "\n".join(parts_text)

        logger.warning("[LLM] 無法從回應中提取文字")
        return "(No response generated)"

    # ──────────────────────────────────────────────
    # 向後相容
    # ──────────────────────────────────────────────

    def build_prompt(
        self,
        user_input: str,
        memory_context: str = "",
        system_instruction: str = "",
    ) -> str:
        """向後相容 — 舊版 Entry.py 呼叫用。建議改用 chat()。"""
        parts = []
        if system_instruction:
            parts.append(f"# System\n{system_instruction}\n")
        if memory_context:
            parts.append(f"# Memories\n{memory_context}\n")
        parts.append(f"# User\n{user_input}")
        return "\n".join(parts)
