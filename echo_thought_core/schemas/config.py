"""Config Schema — 設定的 Pydantic 模型"""

from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field, field_validator, model_validator

from echo_thought_core.defaults import (
    DEFAULT_MODEL,
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_EMERGENCY_KEEP,
)


# ── 壓縮設定 ──────────────────────────────────────────────────────────────────


class CompactionConfig(BaseModel):
    """壓縮設定"""

    # 後端設定
    backend: str = Field("gemini", description="LLM 後端: gemini/local")
    model: str = Field(DEFAULT_MODEL, description="主要模型")
    compaction_model: Optional[str] = Field(
        None, description="用於摘要的較便宜模型（可選，None 表示使用主模型）"
    )

    # Context window
    context_window_size: int = Field(DEFAULT_CONTEXT_WINDOW, description="Context window 大小")

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
        DEFAULT_EMERGENCY_KEEP, description="Layer 4 緊急模式保留的最後幾則訊息"
    )
    compaction_cooldown_seconds: float = Field(
        60.0, description="兩次壓縮之間的最小間隔"
    )

    @field_validator("context_window_size")
    @classmethod
    def validate_context_window(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("context_window_size 必須為正整數")
        return v

    @field_validator("max_emergency_messages")
    @classmethod
    def validate_max_emergency(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("max_emergency_messages 必須為正整數")
        return v

    @field_validator("compaction_cooldown_seconds")
    @classmethod
    def validate_cooldown(cls, v: float) -> float:
        if v < 0:
            raise ValueError("compaction_cooldown_seconds 不可為負數")
        return v

    @model_validator(mode="after")
    def validate_thresholds(self) -> "CompactionConfig":
        t = self.thresholds
        for key, val in t.items():
            if not (0 < val < 1):
                raise ValueError(
                    f"threshold '{key}' 必須介於 0 到 1 之間（不含），但得到 {val}"
                )

        # 確保閾值嚴格遞增
        keys_order = ["micro_prune", "rolling_summary", "full_compact"]
        present = [k for k in keys_order if k in t]
        for i in range(1, len(present)):
            if t[present[i]] <= t[present[i - 1]]:
                raise ValueError(
                    f"threshold 必須嚴格遞增: "
                    f"{present[i-1]}={t[present[i-1]]} 不應 >= {present[i]}={t[present[i]]}"
                )

        return self


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


