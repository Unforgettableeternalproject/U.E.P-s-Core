"""
LLM Client - Gemini/Vertex AI 客戶端封裝

延續 U.E.P Core 的 LLM 配置（Vertex AI / Gemini 2.5 Flash）
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("echo_memory.llm")


class LLMClient:
    """
    LLM 客戶端 - Phase 3 實作
    
    當前為骨架，Phase 1/2 不需要。
    """
    
    def __init__(
        self,
        provider: str = "google",
        model: str = "gemini-2.5-flash-lite",
    ):
        self.provider = provider
        self.model = model
        
        # 延遲導入以避免未安裝依賴時報錯
        self._client = None
        
        logger.info(f"[LLMClient] 初始化（骨架） - Provider: {provider}, Model: {model}")
    
    def _initialize_client(self):
        """延遲初始化 LLM 客戶端"""
        if self._client is not None:
            return
        
        if self.provider == "google":
            self._init_google_client()
        else:
            raise ValueError(f"不支援的 LLM Provider: {self.provider}")
    
    def _init_google_client(self):
        """初始化 Google Gemini/Vertex AI 客戶端"""
        try:
            import google.generativeai as genai
            
            # 優先使用 Vertex AI，其次 Gemini API
            gcp_project = os.getenv("GCP_PROJECT_ID")
            api_key = os.getenv("GOOGLE_API_KEY")
            
            if not gcp_project and not api_key:
                raise ValueError(
                    "請設定 GCP_PROJECT_ID（Vertex AI）或 GOOGLE_API_KEY（Gemini API）"
                )
            
            # 目前使用標準 Gemini API（Vertex AI 整合留待 Phase 3）
            if api_key:
                logger.info("[LLMClient] 使用標準 Gemini API")
                genai.configure(api_key=api_key)
            else:
                logger.warning(
                    "[LLMClient] 當前 Phase 僅支援 Gemini API，"
                    "Vertex AI 整合將在 Phase 3 完成"
                )
                raise ValueError("請設定 GOOGLE_API_KEY")
            
            self._client = genai.GenerativeModel(self.model)
            logger.info(f"[LLMClient] Gemini 客戶端初始化成功 - Model: {self.model}")
            
        except ImportError:
            logger.error("[LLMClient] google-generativeai 未安裝，請執行 pip install google-generativeai")
            raise
        except Exception as e:
            logger.error(f"[LLMClient] 初始化失敗: {e}")
            raise
    
    def generate(
        self,
        prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> str:
        """
        生成文本回應。
        
        Args:
            prompt: 輸入提示詞
            temperature: 隨機性 (0-1)
            max_tokens: 最大生成 Token 數
        
        Returns:
            生成的文本
        """
        self._initialize_client()
        
        if self._client is None:
            raise RuntimeError("LLM 客戶端未初始化")
        
        try:
            response = self._client.generate_content(
                prompt,
                generation_config={
                    "temperature": temperature,
                    "max_output_tokens": max_tokens,
                },
            )
            
            return response.text
        
        except Exception as e:
            logger.error(f"[LLMClient] 生成失敗: {e}")
            raise
    
    def build_prompt(
        self,
        user_input: str,
        memory_context: str = "",
        system_instruction: str = "",
    ) -> str:
        """
        構建完整的提示詞。
        
        Args:
            user_input: 用戶輸入
            memory_context: 記憶脈絡（來自 MemoryContext.to_text()）
            system_instruction: 系統指示（可選）
        
        Returns:
            完整的 Prompt
        """
        parts = []
        
        if system_instruction:
            parts.append(f"# 系統指示\n{system_instruction}\n")
        
        if memory_context:
            parts.append(f"# 相關記憶\n{memory_context}\n")
        
        parts.append(f"# 用戶輸入\n{user_input}")
        
        return "\n".join(parts)
