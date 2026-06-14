"""
Config Schema — 設定的 Pydantic 模型

v2 合併自 llm_module/schemas.py：
- 新增 LLMModuleData 模型（統一模組數據格式）
- 新增 create_llm_data() 便利函數
"""

from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field


# ── 壓縮設定 ──────────────────────────────────────────────────────────────────


class CompactionConfig(BaseModel):
    """壓縮設定"""

    # 後端設定
    backend: str = Field("gemini", description="LLM 後端: gemini/local")
    model: str = Field("gemini-2.5-flash-lite", description="主要模型")
    compaction_model: Optional[str] = Field(
        None, description="用於摘要的較便宜模型（可選，None 表示使用主模型）"
    )

    # Context window
    context_window_size: int = Field(200000, description="Context window 大小")

    # 壓縮閾值（佔 context window 的百分比）
    thresholds: Dict[str, float] = Field(
        default_factory=lambda: {
            "micro_prune": 0.60,
            "rolling_summary": 0.80,
            "full_compact": 0.92,
        },
        description="各層壓縮閾值",
    )

    # 功能開關
    use_llmlingua: bool = Field(False, description="Layer 1 是否啟用 LLMLingua-2")
    echo_memory_enabled: bool = Field(
        False, description="是否啟用 Echo Memory 整合"
    )

    # 進階設定
    max_emergency_messages: int = Field(
        4, description="Layer 4 緊急模式保留的最後幾則訊息"
    )
    compaction_cooldown_seconds: float = Field(
        60.0, description="兩次壓縮之間的最小間隔"
    )


# ── 人設上下文 ────────────────────────────────────────────────────────────────


class PersonaContext(BaseModel):
    """人設上下文 — 神聖段落的核心"""

    name: str = Field(..., description="角色名稱")
    traits: List[str] = Field(default_factory=list, description="人格特徵")
    speaking_style: Optional[str] = Field(None, description="說話風格描述")
    user_preferences: Dict[str, Any] = Field(
        default_factory=lambda: {"language": "zh-TW"},
        description="使用者偏好",
    )
    custom_instructions: Optional[str] = Field(
        None, description="額外的角色指令"
    )

    def to_sacred_text(self) -> str:
        """轉為神聖段落格式"""
        parts = [f"Character: {self.name}"]
        if self.traits:
            parts.append(f"Personality: {', '.join(self.traits)}")
        if self.speaking_style:
            parts.append(f"Speaking Style: {self.speaking_style}")
        if self.user_preferences:
            prefs = "; ".join(f"{k}={v}" for k, v in self.user_preferences.items())
            parts.append(f"User Preferences: {prefs}")
        if self.custom_instructions:
            parts.append(f"Instructions: {self.custom_instructions}")
        return "\n".join(parts)


# ── LLM 模組數據格式 ──────────────────────────────────────────────────────────


class LLMModuleData(BaseModel):
    """統一模組數據格式 — 模組間傳遞的標準容器"""

    # 核心數據
    text: Optional[str] = Field(None, description="主要文本內容")
    mode: Optional[str] = Field("chat", description="處理模式")

    # 上下文
    context: Optional[Dict[str, Any]] = Field(default_factory=dict, description="上下文信息")

    # 執行控制
    execution_mode: str = Field("sequential", description="執行模式")
    priority: int = Field(5, description="優先級（數字越小優先級越高）")
    timeout: Optional[float] = Field(None, description="超時時間（秒）")

    # 模組間傳遞的附加數據
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="元數據")


# ── 便利函數 ──────────────────────────────────────────────────────────────────


def create_llm_data(text: str, mode: str = "chat", **kwargs) -> LLMModuleData:
    """建立 LLM 模組數據的便利函數

    Args:
        text: 主要文本內容
        mode: 處理模式，預設 "chat"
        **kwargs: 其餘欄位（context、execution_mode、priority、timeout）

    Returns:
        LLMModuleData 實例，kwargs 中未識別的鍵會存入 metadata
    """
    return LLMModuleData(
        text=text,
        mode=mode,
        context=kwargs.get("context", {}),
        execution_mode=kwargs.get("execution_mode", "sequential"),
        priority=kwargs.get("priority", 5),
        timeout=kwargs.get("timeout"),
        metadata=kwargs,
    )
