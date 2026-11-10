"""平台宣告注入端的共用型別與詞彙映射（平台宣告注入端契約草案 §2.2；SYS 增補 PD-18）。

只放純資料與映射表，不 import tools／decision／presence（依賴單向，決策層與工具層才能同源引用）。
平台詞彙已統一為 ``PresenceLocation``（DQ-3 已裁決 B）；本檔只保留舊值 ``web`` -> ``website`` 的讀取相容層。
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from uep.contracts.presence import PresenceLocation, PresenceStatus

# 平台與 executor 識別字的字元集：與 ``uep.decision.loop_consult._SAFE_REF`` 相同（小寫英數與 _ . : -）。
# 刻意不允許 ``@``：registry 內部的 variant 鍵是 ``tool@platform``，對外一律用 ``tool:platform``（PD-18）。
SAFE_REF = re.compile(r"^[a-z0-9_.:-]{1,64}$")

PlatformId = str
_PRESENCE_LOCATIONS: frozenset[str] = frozenset(loc.value for loc in PresenceLocation)

# ExecutorDescriptor.origin 的合法值
ORIGIN_REGISTRY_STATIC = "registry_static"  # 階段 A：由 registry 靜態資料推導
ORIGIN_PLATFORM_DECLARED = "platform_declared"  # 階段 B：平台已宣告、在線且已核准
ORIGIN_COARSE_FALLBACK = "coarse_fallback"  # F-1 退路：只回 current_platform 的粗粒度結果
EXECUTOR_ORIGINS: frozenset[str] = frozenset({ORIGIN_REGISTRY_STATIC, ORIGIN_PLATFORM_DECLARED, ORIGIN_COARSE_FALLBACK})

# ---- 舊值相容層（DQ-3-B）----
# 平台詞彙已統一為 ``PresenceLocation``（desktop／discord／website）；唯一保留的舊值是 ``web``（舊
# ``SessionContext.platform`` 預設值），只在邊界（``SessionContext`` 建構、``replay_turn``）讀取時正規化成
# ``website``。寫入一律用新詞彙。Presence 與 endpoint 已是同一套詞彙，不需要映射。
LEGACY_PLATFORM_ALIASES: Mapping[str, PlatformId] = MappingProxyType({"web": PresenceLocation.WEBSITE.value})

_logger = logging.getLogger("uep.contracts.platform")
_legacy_warned: set[str] = set()


def normalize_platform(value: str | None) -> str | None:
    """舊值 ``web`` -> ``website``，其餘原樣回傳（不做開放字串的合法性判斷）。

    正規化時每個舊值只記一次 debug log（行程內），供追蹤舊資料是否仍在流入。"""
    if not isinstance(value, str):
        return value
    alias = LEGACY_PLATFORM_ALIASES.get(value.strip().lower())
    if alias is None:
        return value
    if value not in _legacy_warned:
        _legacy_warned.add(value)
        _logger.debug("platform 舊值 %r 已正規化為 %r（相容層）", value, alias)
    return alias


def platform_from_presence_location(location: str | None) -> PlatformId | None:
    """Presence location -> PlatformId；不在閉集（或 None）回 None。"""
    if not isinstance(location, str):
        return None
    value = location.strip().lower()
    return value if value in _PRESENCE_LOCATIONS else None


def platform_from_session(session_platform: str | None) -> PlatformId | None:
    """``SessionContext.platform`` -> PlatformId（舊值 ``web`` 先正規化）；不在閉集回 None（不影響 current_platform）。"""
    return platform_from_presence_location(normalize_platform(session_platform))


def platform_from_endpoint_location(location: str | None) -> PlatformId | None:
    """host 端點 location -> PlatformId；endpoint 已使用 Presence 詞彙，不收舊值 ``web``。"""
    return platform_from_presence_location(location)


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
