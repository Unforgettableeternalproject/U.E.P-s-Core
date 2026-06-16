"""
LLM Router — 模組級無狀態任務路由器

與 LLMClient 的職責劃分：
- LLMClient：CHAT 角色的多輪對話管理者。管歷史、Function Calling、tool-use loop。
- LLMRouter：模組級無狀態任務路由器。根據角色選模型和參數，
             供 Verifier/Judge/ConceptExtractor/Reflection 等模組使用。

詳見 docs/design/llm-client-vs-router.md
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import yaml

logger = logging.getLogger("echo_memory.llm.router")

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 資料結構
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class LLMRole(Enum):
    """
    LLM 角色定義。

    每個角色對應一組模型+參數配置，用途各異：
    - CHAT: 主對話生成（由 LLMClient 管理，Router 僅保存配置參考）
    - WORK: 記憶操作任務（概念提取、反思、蒸餾、Dream 重播）
    - EVAL: 評估與驗證（Verifier 概念驗證、Judge 存儲決策）
    """
    CHAT = "chat"
    WORK = "work"
    EVAL = "eval"


@dataclass
class RoleConfig:
    """角色配置"""
    model: str
    temperature: float
    max_tokens: int
    description: str = ""


@dataclass
class RouterStats:
    """路由統計（可用於儀表板/除錯）"""
    total_calls: int = 0
    calls_by_role: dict[str, int] = field(default_factory=dict)
    failures: int = 0
    fallback_used: int = 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LLMRouter
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class LLMRouter:
    """
    模組級無狀態任務路由器。

    根據 LLMRole 選擇對應的模型和參數，提供 ``(str) -> str`` 的統一介面。
    所有呼叫都是無狀態的（不管理對話歷史）。

    與 LLMClient 的關鍵差異
    -----------------------
    +------------------+-------------------------------+-------------------------------+
    |                  | LLMClient                     | LLMRouter                     |
    +------------------+-------------------------------+-------------------------------+
    | 對話歷史         | ✅ 管理 _history               | ❌ 無狀態                     |
    | Function Calling | ✅ tool-use loop               | ❌ 不支援                     |
    | 角色/人格        | ✅ character 注入              | ❌ 無人格                     |
    | 模型切換         | ❌ 固定一個模型               | ✅ 按角色路由                 |
    | 消費者           | Entry.py 的 chat() 呼叫       | Verifier/Judge/ConceptExtractor |
    +------------------+-------------------------------+-------------------------------+

    使用範例::

        router = LLMRouter()

        # 取得特定角色的 llm_fn（符合既有 Callable[[str], str] 契約）
        eval_fn = router.get_llm_fn(LLMRole.EVAL)
        result = eval_fn("判斷這個回覆是否與記憶矛盾...")

        # 直接呼叫
        result = router.generate("提取概念...", role=LLMRole.WORK)
    """

    # 角色預設配置（會被 config.yaml 的 router.roles 區塊覆蓋）
    #
    # 模型分配策略（艾斯維爾 2026-06-15 決策）：
    #   CHAT / WORK → gemini-3.1-flash-lite（較新、較強）
    #   EVAL        → gemini-2.5-flash-lite（評估性質，舊模型即可，成本低）
    #   WORK 高難度 → gemini-3.5-flash（複雜推理/蒸餾，未來可選升級）
    _DEFAULT_CONFIGS: dict[LLMRole, RoleConfig] = {
        LLMRole.CHAT: RoleConfig(
            model="gemini-3.1-flash-lite",
            temperature=0.8,
            max_tokens=4096,
            description="主對話生成（參考用，實際由 LLMClient 管理）",
        ),
        LLMRole.WORK: RoleConfig(
            model="gemini-3.1-flash-lite",
            temperature=0.3,
            max_tokens=1024,
            description="記憶操作（概念提取、反思、蒸餾）",
        ),
        LLMRole.EVAL: RoleConfig(
            model="gemini-2.5-flash-lite",
            temperature=0.0,
            max_tokens=768,
            description="評估（Verifier、Judge）",
        ),
    }

    def __init__(self, config_path: str | Path | None = None):
        """
        初始化 LLMRouter。

        Args:
            config_path: config.yaml 路徑。None 時使用預設路徑
                         (llm_integration/config.yaml)。
        """
        self._configs: dict[LLMRole, RoleConfig] = {}
        self._load_config(config_path)

        self._stats = RouterStats()

        # google-genai Client（延遲初始化）
        self._client: Any = None
        self._initialized = False

    # ──────────────────────────────────────────────
    # 配置載入
    # ──────────────────────────────────────────────

    def _load_config(self, config_path: str | Path | None = None) -> None:
        """載入配置，合併預設值與 config.yaml 的 router 區塊。"""
        # 先複製預設值
        for role, default in self._DEFAULT_CONFIGS.items():
            self._configs[role] = RoleConfig(
                model=default.model,
                temperature=default.temperature,
                max_tokens=default.max_tokens,
                description=default.description,
            )

        # 嘗試從 config.yaml 載入
        if config_path is None:
            config_path = Path(__file__).parent / "config.yaml"
        else:
            config_path = Path(config_path)

        if not config_path.exists():
            logger.info("[Router] config.yaml 不存在，使用預設配置")
            return

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning(f"[Router] config.yaml 載入失敗: {e}，使用預設配置")
            return

        router_cfg = raw.get("router", {})
        if not router_cfg:
            return

        roles_cfg = router_cfg.get("roles", {})
        for role_name, role_params in roles_cfg.items():
            try:
                role = LLMRole(role_name)
            except ValueError:
                logger.warning(f"[Router] 未知角色 '{role_name}'，跳過")
                continue

            if not isinstance(role_params, dict):
                continue

            cfg = self._configs[role]
            if "model" in role_params:
                cfg.model = str(role_params["model"])
            if "temperature" in role_params:
                cfg.temperature = float(role_params["temperature"])
            if "max_tokens" in role_params:
                cfg.max_tokens = int(role_params["max_tokens"])
            if "description" in role_params:
                cfg.description = str(role_params["description"])

        logger.info(
            "[Router] 配置已載入 — "
            + ", ".join(f"{r.value}={c.model}" for r, c in self._configs.items())
        )

    # ──────────────────────────────────────────────
    # Client 初始化
    # ──────────────────────────────────────────────

    def _ensure_client(self) -> None:
        """
        延遲初始化 genai.Client。

        認證優先順序（與 LLMClient **相反**）：
        1. API Key 優先 — Router 使用的 3.x 模型在 Gemini API (AI Studio) 上
           可用，但 Vertex AI 可能尚未開通。
        2. Vertex AI 備援 — 沒有 API Key 時回退到 Vertex AI。

        對比 LLMClient 是 Vertex AI 優先、API Key 備援。
        """
        if self._initialized:
            return
        self._initialized = True

        try:
            from google import genai  # noqa: F811
        except ImportError:
            raise ImportError(
                "google-genai 未安裝。請使用主專案 U.E.P-s-Core 的 Python 環境。"
            )

        api_key = os.getenv("GOOGLE_API_KEY")
        gcp_project = os.getenv("GCP_PROJECT_ID")
        gcp_location = os.getenv("GCP_LOCATION", "us-central1")

        if api_key:
            # API Key 優先：3.x 模型在 Gemini API (AI Studio) 可用
            self._client = genai.Client(api_key=api_key)
            logger.info("[Router] API Key 模式（Gemini API / AI Studio）")
        elif gcp_project:
            # Vertex AI 備援
            self._client = genai.Client(
                vertexai=True,
                project=gcp_project,
                location=gcp_location,
            )
            logger.info(f"[Router] Vertex AI 備援 — project={gcp_project}")
        else:
            raise ValueError(
                "需要設定 GOOGLE_API_KEY（Gemini API）或 GCP_PROJECT_ID（Vertex AI）。"
                "請在 .env 檔案中設定。"
            )

    # ──────────────────────────────────────────────
    # 核心 API
    # ──────────────────────────────────────────────

    def generate(
        self,
        prompt: str,
        role: LLMRole = LLMRole.WORK,
        *,
        system_instruction: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """
        無狀態單次生成。

        Args:
            prompt: 輸入提示詞
            role: LLM 角色（決定使用哪個模型和參數）
            system_instruction: 系統指示（可選）
            temperature: 覆蓋角色預設溫度（None 時使用角色配置）
            max_tokens: 覆蓋角色預設 max tokens（None 時使用角色配置）

        Returns:
            生成的文字

        Raises:
            ValueError: 未知角色或未設定 API 金鑰
            ImportError: google-genai 未安裝
        """
        self._ensure_client()
        from google.genai import types

        cfg = self._configs.get(role)
        if cfg is None:
            raise ValueError(f"未知角色: {role}")

        # 統計
        self._stats.total_calls += 1
        self._stats.calls_by_role[role.value] = (
            self._stats.calls_by_role.get(role.value, 0) + 1
        )

        contents = [types.Content(role="user", parts=[types.Part(text=prompt)])]

        config_params: dict = {
            "temperature": temperature if temperature is not None else cfg.temperature,
            "max_output_tokens": max_tokens or cfg.max_tokens,
        }
        if system_instruction:
            config_params["system_instruction"] = system_instruction

        config = types.GenerateContentConfig(**config_params)

        try:
            result = self._client.models.generate_content(
                model=cfg.model,
                contents=contents,
                config=config,
            )
            return self._extract_text(result)
        except Exception as primary_error:
            logger.warning(
                f"[Router] {role.value} 主模型失敗 ({cfg.model}): {primary_error}"
            )

            # ── Ollama fallback：僅重試失敗的這一次 ──
            fallback_result = self._try_ollama_fallback(prompt, role, cfg)
            if fallback_result is not None:
                self._stats.fallback_used += 1
                logger.info(
                    f"[Router] {role.value} Ollama fallback 成功"
                )
                return fallback_result

            # fallback 也失敗（或不可用），計為最終失敗
            self._stats.failures += 1
            logger.error(
                f"[Router] {role.value} 主模型 + fallback 均失敗: {primary_error}"
            )
            raise primary_error

    def get_llm_fn(
        self,
        role: LLMRole = LLMRole.WORK,
        *,
        system_instruction: str = "",
    ) -> Callable[[str], str]:
        """
        取得特定角色的 ``(str) -> str`` callable。

        這是各模組的主要介面。回傳的 callable 符合既有的 ``llm_fn`` 契約，
        可直接傳入 SubconsciousAssessor、StorageJudge、ConceptExtractor、
        ReflectionModule、DreamEngine 等模組。

        Args:
            role: LLM 角色
            system_instruction: 固定的系統指示（嵌入 closure 中）

        Returns:
            Callable[[str], str]
        """
        def _fn(prompt: str) -> str:
            return self.generate(
                prompt,
                role=role,
                system_instruction=system_instruction,
            )
        return _fn

    # ──────────────────────────────────────────────
    # 配置查詢 / 診斷
    # ──────────────────────────────────────────────

    def get_role_config(self, role: LLMRole) -> RoleConfig:
        """取得角色配置（唯讀副本）。"""
        cfg = self._configs.get(role)
        if cfg is None:
            raise ValueError(f"未知角色: {role}")
        return RoleConfig(
            model=cfg.model,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            description=cfg.description,
        )

    @property
    def available_roles(self) -> list[LLMRole]:
        """已配置的角色列表。"""
        return list(self._configs.keys())

    @property
    def stats(self) -> RouterStats:
        """路由統計。"""
        return self._stats

    def summary(self) -> str:
        """配置摘要（用於儀表板/除錯）。"""
        lines = ["LLM Router 配置："]
        for role, cfg in self._configs.items():
            lines.append(
                f"  {role.value:>5}: {cfg.model} "
                f"(temp={cfg.temperature}, max={cfg.max_tokens})"
                f" — {cfg.description}"
            )
        # Ollama fallback 狀態
        fb_status = "enabled" if self._OLLAMA_FALLBACK_ENABLED else "disabled"
        lines.append(f"  fallback: Ollama {self._OLLAMA_FALLBACK_MODEL} ({fb_status})")

        if self._stats.total_calls > 0:
            lines.append(
                f"\n統計：{self._stats.total_calls} calls, "
                f"{self._stats.failures} failures, "
                f"{self._stats.fallback_used} fallback"
            )
            for role_name, count in sorted(self._stats.calls_by_role.items()):
                lines.append(f"  {role_name}: {count}")
        return "\n".join(lines)

    # ──────────────────────────────────────────────
    # Ollama Fallback
    # ──────────────────────────────────────────────

    # Ollama fallback 配置（從 .env 或環境變數讀取）
    _OLLAMA_FALLBACK_MODEL = os.getenv("OLLAMA_FALLBACK_MODEL", "gemma4:e4b-it-q4_K_M")
    _OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    _OLLAMA_FALLBACK_ENABLED = os.getenv("OLLAMA_FALLBACK_ENABLED", "true").lower() == "true"

    def _try_ollama_fallback(
        self,
        prompt: str,
        role: LLMRole,
        cfg: RoleConfig,
    ) -> str | None:
        """
        嘗試用 Ollama 本地模型重試失敗的 LLM 呼叫。

        僅在主模型失敗時呼叫，只重試這一次。
        成功回傳生成文字，失敗回傳 None。

        Args:
            prompt: 原始提示詞
            role: 角色（用於日誌）
            cfg: 原角色配置（用於 temperature 等參數參考）
        """
        if not self._OLLAMA_FALLBACK_ENABLED:
            return None

        try:
            import httpx
        except ImportError:
            logger.debug("[Router] httpx 未安裝，無法使用 Ollama fallback")
            return None

        url = f"{self._OLLAMA_BASE_URL}/api/generate"

        try:
            response = httpx.post(
                url,
                json={
                    "model": self._OLLAMA_FALLBACK_MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "temperature": cfg.temperature,
                        "num_predict": cfg.max_tokens,
                    },
                },
                timeout=120.0,  # 本地模型推理可能較慢
            )
            response.raise_for_status()
            data = response.json()
            text = data.get("response", "").strip()
            if text:
                return text
            logger.warning("[Router] Ollama fallback 回傳空文字")
            return None
        except Exception as e:
            logger.warning(f"[Router] Ollama fallback 失敗: {e}")
            return None

    # ──────────────────────────────────────────────
    # 內部方法
    # ──────────────────────────────────────────────

    @staticmethod
    def _extract_text(result: Any) -> str:
        """從 API 回應中提取純文字（與 LLMClient 邏輯相同）。"""
        if hasattr(result, "text") and result.text:
            return result.text

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

        logger.warning("[Router] 無法從回應中提取文字")
        return "(No response generated)"
