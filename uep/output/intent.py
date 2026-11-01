"""OutputIntent 與 ControlSignal（契約 CC-5 §5.1、§5.3）。"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from uep.events.envelope import scope_rank


def require_aware(value: datetime | None) -> datetime | None:
    """時間欄必須帶時區（naive 與 aware 比較會在 Gate 內才拋 TypeError，改在建構時拒絕）。"""
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("時間欄必須帶時區（tz-aware）")
    return value


class OutputKind(StrEnum):
    TEXT = "text"
    AUDIO_STREAM = "audio_stream"
    TYPING = "typing"
    AVATAR_ENTER_LEAVE = "avatar_enter_leave"
    ANIMATION = "animation"
    DESKTOP_UI_ACTION = "desktop_ui_action"
    SYSTEM_NOTICE = "system_notice"


class OutputOrigin(StrEnum):
    PERSONA = "persona"
    SYSTEM = "system"


class OutputIntent(BaseModel):
    """計畫輸出（不可變）。實際交付結果在 DeliveryRecord，兩者分離（O-1）。"""

    model_config = ConfigDict(frozen=True)

    output_id: str
    sequence: int = Field(ge=0)
    kind: OutputKind
    origin: OutputOrigin
    # CC-4 授權六元組
    persona_id: str
    endpoint_id: str
    lease_id: str
    fencing_epoch: int
    core_epoch: int
    presence_generation: int
    activity_id: str | None = None
    turn_id: str | None = None
    turn_generation: int | None = None
    attempt_id: str | None = None
    audience_scope: str
    content_ref: str  # 不內嵌原文（D33）
    content_format: dict[str, Any] | None = None  # 音訊 {sample_rate, channels, codec}
    valid_until: datetime
    generated_text_ref: str | None = None

    @field_validator("valid_until")
    @classmethod
    def _aware_valid_until(cls, value: datetime) -> datetime:
        return require_aware(value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _check(self) -> OutputIntent:
        scope_rank(self.audience_scope)
        interactive = (self.turn_id, self.turn_generation, self.attempt_id)
        if any(v is not None for v in interactive) and any(v is None for v in interactive):
            raise ValueError("互動輸出須同時帶 turn_id／turn_generation／attempt_id")
        if all(v is None for v in interactive) and self.activity_id is None:
            raise ValueError("輸出須屬於互動回合（turn_*）或主動活動（activity_id）")
        if self.kind is OutputKind.AUDIO_STREAM and not self.content_format:
            raise ValueError("audio_stream 必須帶 content_format")
        if self.origin is OutputOrigin.SYSTEM and self.generated_text_ref is not None:
            raise ValueError("origin=system 不是 persona 生成稿，不得帶 generated_text_ref（O-8）")
        return self


class ControlKind(StrEnum):
    STOP = "stop"
    ABORT = "abort"
    REVOKE = "revoke"


class ControlScope(StrEnum):
    TURN = "turn"
    SEGMENT = "segment"
    TRANSFER = "transfer"
    PROCESS = "process"


class ControlSignal(BaseModel):
    """獨立控制通道訊號；``control_id`` 為冪等鍵。"""

    model_config = ConfigDict(frozen=True)

    control_id: str
    kind: ControlKind
    scope: ControlScope
    target_fence: dict[str, int]
    issued_at: datetime

    @field_validator("issued_at")
    @classmethod
    def _aware_issued_at(cls, value: datetime) -> datetime:
        return require_aware(value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _check(self) -> ControlSignal:
        if "core_epoch" not in self.target_fence:
            raise ValueError("target_fence 必須含 core_epoch")
        if self.scope is ControlScope.TURN and "turn_generation" not in self.target_fence:
            raise ValueError("scope=turn 的 target_fence 必須含 turn_generation")
        return self
