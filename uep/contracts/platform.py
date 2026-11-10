"""平台宣告注入端的共用型別與詞彙映射（平台宣告注入端契約草案 §2.2；SYS 增補 PD-18）。

只放純資料與映射表，不 import tools／decision／presence（依賴單向，決策層與工具層才能同源引用）。
三套平台詞彙並存是已知債（DQ-3 暫採 A：契約層映射表）：映射表集中在本檔，日後改成統一改名（B）時只動這裡。
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from uep.contracts.presence import PresenceLocation, PresenceStatus

# 平台與 executor 識別字的字元集：與 ``uep.decision.loop_consult._SAFE_REF`` 相同（小寫英數與 _ . : -）。
# 刻意不允許 ``@``：registry 內部的 variant 鍵是 ``tool@platform``，對外一律用 ``tool:platform``（PD-18）。
SAFE_REF = re.compile(r"^[a-z0-9_.:-]{1,64}$")

PlatformId = str

# ExecutorDescriptor.origin 的合法值
ORIGIN_REGISTRY_STATIC = "registry_static"  # 階段 A：由 registry 靜態資料推導
ORIGIN_PLATFORM_DECLARED = "platform_declared"  # 階段 B：平台已宣告、在線且已核准
ORIGIN_COARSE_FALLBACK = "coarse_fallback"  # F-1 退路：只回 current_platform 的粗粒度結果
EXECUTOR_ORIGINS: frozenset[str] = frozenset({ORIGIN_REGISTRY_STATIC, ORIGIN_PLATFORM_DECLARED, ORIGIN_COARSE_FALLBACK})

# ---- 詞彙映射表（DQ-3-A）：三套詞彙的唯一對照點 ----
# 1. Presence location：閉集 PresenceLocation，恆等映射（PlatformId 為開放字串，但目前的值都來自此集合）
PRESENCE_LOCATION_TO_PLATFORM: Mapping[str, PlatformId] = MappingProxyType({loc.value: loc.value for loc in PresenceLocation})
# 2. session.platform：輸入端平台；預設值 ``web`` 對應 Presence 的 ``website``
SESSION_PLATFORM_TO_PLATFORM: Mapping[str, PlatformId] = MappingProxyType({
    "web": PresenceLocation.WEBSITE.value,
    "website": PresenceLocation.WEBSITE.value,
    "discord": PresenceLocation.DISCORD.value,
    "desktop": PresenceLocation.DESKTOP.value,
})
# 3. host 端點註冊用的 location（預設 ``desktop``）：沿用 Presence location 詞彙
ENDPOINT_LOCATION_TO_PLATFORM: Mapping[str, PlatformId] = MappingProxyType({
    "desktop": PresenceLocation.DESKTOP.value,
    "discord": PresenceLocation.DISCORD.value,
    "website": PresenceLocation.WEBSITE.value,
})


def _lookup(table: Mapping[str, PlatformId], value: str | None) -> PlatformId | None:
    if not isinstance(value, str):
        return None
    return table.get(value.strip().lower())


def platform_from_presence_location(location: str | None) -> PlatformId | None:
    """Presence location -> PlatformId；不在閉集（或 None）回 None。"""
    return _lookup(PRESENCE_LOCATION_TO_PLATFORM, location)


def platform_from_session(session_platform: str | None) -> PlatformId | None:
    """``SessionContext.platform`` -> PlatformId；無法映射回 None（不影響 current_platform）。"""
    return _lookup(SESSION_PLATFORM_TO_PLATFORM, session_platform)


def platform_from_endpoint_location(location: str | None) -> PlatformId | None:
    """host 端點 location -> PlatformId；無法映射回 None。"""
    return _lookup(ENDPOINT_LOCATION_TO_PLATFORM, location)


def is_safe_ref(value: object) -> bool:
    return isinstance(value, str) and SAFE_REF.fullmatch(value) is not None


@dataclass(frozen=True)
class ExecutorDescriptor:
    """一個可承接工具執行的 executor。``executor_ref`` 格式 ``tool:platform``（PD-18），由工具層
    ``ToolRegistry.executors_for`` 單點產生；本型別只驗證字元集，不負責組字串。"""

    executor_ref: str
    tool: str
    platform: PlatformId
    origin: str

    def __post_init__(self) -> None:
        if not is_safe_ref(self.executor_ref):
            raise ValueError(f"executor_ref 不合法（小寫英數與 _ . : -，1～64 字元，不含 @）：{self.executor_ref!r}")
        if not isinstance(self.tool, str) or not self.tool:
            raise ValueError("tool 不可為空")
        if not is_safe_ref(self.platform):
            raise ValueError(f"platform 不合法：{self.platform!r}")
        if self.origin not in EXECUTOR_ORIGINS:
            raise ValueError(f"origin 不合法：{self.origin!r}")


@dataclass(frozen=True)
class PlatformDeclaration:
    """平台宣告（階段 B 才有真實資料；階段 A 可為空）。"""

    platform: PlatformId
    online: bool
    declared_tools: frozenset[str] = frozenset()
    declaration_revision: int = 0

    def __post_init__(self) -> None:
        if not is_safe_ref(self.platform):
            raise ValueError(f"platform 不合法：{self.platform!r}")
        if self.declaration_revision < 0:
            raise ValueError("declaration_revision 不可為負")


@dataclass(frozen=True)
class PlatformContext:
    """單一 turn 的平台脈絡快照；三個消費點只讀它（第 5 步起才分發，目前只做 shadow）。

    ``current_platform`` = Presence ACTIVE 時的 location，否則 None；``session_platform`` 是輸入端平台，
    兩者並存、不混用（DQ-1 暫採 C）。"""

    version: int
    presence_generation: int
    presence_row_version: int
    presence_status: PresenceStatus
    current_platform: PlatformId | None
    presence_active_platforms: frozenset[PlatformId]
    session_platform: PlatformId | None
    available_executors: tuple[str, ...]
    registry_revision: int = 0
    declaration_revision: int = 0

    def __post_init__(self) -> None:
        if self.current_platform is not None and not is_safe_ref(self.current_platform):
            raise ValueError(f"current_platform 不合法：{self.current_platform!r}")
        if self.current_platform is None and self.presence_active_platforms:
            raise ValueError("沒有 current_platform 時 presence_active_platforms 必須為空")
        bad = [ref for ref in self.available_executors if not is_safe_ref(ref)]
        if bad:
            raise ValueError(f"available_executors 含不合法的 executor_ref：{bad!r}")

    def summary(self) -> str:
        """日誌用摘要：只含版本、狀態、平台與 executor 數，不含原文。"""
        return (
            f"v={self.version} status={self.presence_status.value} platform={self.current_platform} "
            f"session={self.session_platform} executors={len(self.available_executors)} "
            f"gen={self.presence_generation} row={self.presence_row_version}"
        )
