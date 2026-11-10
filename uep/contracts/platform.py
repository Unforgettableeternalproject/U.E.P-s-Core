"""平台宣告注入端的共用型別與詞彙映射（平台宣告注入端契約草案 §2.2；SYS 增補 PD-18）。

只放純資料與映射表，不 import tools／decision／presence（依賴單向，決策層與工具層才能同源引用）。
平台詞彙已統一為 ``PresenceLocation``（DQ-3 已裁決 B）；本檔只保留舊值 ``web`` -> ``website`` 的讀取相容層。
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Literal

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
    # 有監控面在線的平台（平台端點契約 EC-3）：只供 shadow 觀察／展示，不分發給任何消費點，
    # 與 presence_active_platforms 獨立（監控在線不代表她在場）。
    monitor_platforms: frozenset[PlatformId] = frozenset()

    def __post_init__(self) -> None:
        if self.current_platform is not None and not is_safe_ref(self.current_platform):
            raise ValueError(f"current_platform 不合法：{self.current_platform!r}")
        if self.current_platform is None and self.presence_active_platforms:
            raise ValueError("沒有 current_platform 時 presence_active_platforms 必須為空")
        bad = [ref for ref in self.available_executors if not is_safe_ref(ref)]
        if bad:
            raise ValueError(f"available_executors 含不合法的 executor_ref：{bad!r}")
        bad_monitor = [ref for ref in self.monitor_platforms if not is_safe_ref(ref)]
        if bad_monitor:
            raise ValueError(f"monitor_platforms 含不合法的 platform：{bad_monitor!r}")

    def summary(self) -> str:
        """日誌用摘要：只含版本、狀態、平台與 executor 數，不含原文。"""
        return (
            f"v={self.version} status={self.presence_status.value} platform={self.current_platform} "
            f"session={self.session_platform} executors={len(self.available_executors)} "
            f"gen={self.presence_generation} row={self.presence_row_version} monitors={len(self.monitor_platforms)}"
        )


# ---- 平台端點契約（E1；docs/架構重構/平台端點契約.md §2、§4.3）----
# 純資料型別與建構檢查（fail-closed，ValueError）；目前沒有任何 production 消費者，只有契約測試引用。
# monitor（非本人的狀態顯示／缺席端輸入）與 interaction（她本人）是兩種能力面（EC-1）；
# 宣告只表示「技術上能做」，核准設定才表示「允許」（§2.1）。
MonitorFacet = Literal["status_view", "health_view", "activity_view", "instruction_inbox"]
InteractionFacet = Literal["text", "audio_in", "audio_out", "avatar", "desktop_tools", "delivery_receipts"]
MONITOR_FACETS: frozenset[str] = frozenset({"status_view", "health_view", "activity_view", "instruction_inbox"})
INTERACTION_FACETS: frozenset[str] = frozenset(
    {"text", "audio_in", "audio_out", "avatar", "desktop_tools", "delivery_receipts"}
)
AUDIENCE_CAPS: frozenset[str] = frozenset({"public", "group", "private"})
OPERATOR_AUTHS: frozenset[str] = frozenset({"none", "platform_identity", "owner_channel"})
# owner 後台不是平台、不屬於任何 Presence 位置（Q5 A）；作為輸入來源時用這個固定值
SOURCE_OWNER_CONSOLE = "owner_console"
PRESENCE_TARGET_HOST = "host"
# 第一階段只受理 owner／system；member／guest 不建立 PresenceRequest（F10、M-11）
PRESENCE_REQUEST_AUTHORITIES: frozenset[str] = frozenset({"owner", "system"})

# actor／ref 的字元集：小寫英數與 _ . : -。刻意不收空白與中文，顯示名稱（暱稱）因此進不來（F10）；
# blob 引用同理，原文塞不進去（只存 ref 與長度）。
_ACTOR_REF = re.compile(r"^[a-z0-9_.:-]{1,128}$")
_BLOB_REF = re.compile(r"^[a-z0-9_.:-]{1,128}$")
_REQUEST_ID = re.compile(r"^\S{1,256}$")


def _require_int(name: str, value: object, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} 必須是 >= {minimum} 的整數：{value!r}")


def _require_bool(name: str, value: object) -> None:
    if value is not True and value is not False:
        raise ValueError(f"{name} 必須是布林：{value!r}")


@dataclass(frozen=True)
class MonitorSurface:
    """一個監控介面；各自的 audience 上限與指示者辨識方式。``audience_cap_claimed`` 只是平台自稱，
    Core 以核准設定為準且只可收窄（M-4、EC-6）。"""

    surface_ref: str
    audience_cap_claimed: str
    operator_auth: Literal["none", "platform_identity", "owner_channel"]

    def __post_init__(self) -> None:
        if not is_safe_ref(self.surface_ref):
            raise ValueError(f"surface_ref 不合法：{self.surface_ref!r}")
        if self.audience_cap_claimed not in AUDIENCE_CAPS:
            raise ValueError(f"audience_cap_claimed 不合法：{self.audience_cap_claimed!r}")
        if self.operator_auth not in OPERATOR_AUTHS:
            raise ValueError(f"operator_auth 不合法：{self.operator_auth!r}")


@dataclass(frozen=True)
class MonitorCapability:
    """監控能力面。未知 facet、空 facets 一律拒收；``instruction_inbox`` 至少要有一個可辨識指示者的 surface（V-A12）。"""

    facets: frozenset[MonitorFacet]
    surfaces: tuple[MonitorSurface, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.facets, frozenset) or not self.facets:
            raise ValueError("monitor.facets 必須是非空 frozenset")
        unknown = sorted(str(f) for f in self.facets if f not in MONITOR_FACETS)
        if unknown:
            raise ValueError(f"monitor 含未知 facet：{unknown!r}")
        if not isinstance(self.surfaces, tuple) or not all(isinstance(s, MonitorSurface) for s in self.surfaces):
            raise ValueError("monitor.surfaces 必須是 MonitorSurface 的 tuple")
        refs = [s.surface_ref for s in self.surfaces]
        if len(set(refs)) != len(refs):
            raise ValueError("monitor.surfaces 的 surface_ref 不可重複")
        if "instruction_inbox" in self.facets and not any(s.operator_auth != "none" for s in self.surfaces):
            raise ValueError("instruction_inbox 需要至少一個可辨識指示者（operator_auth != none）的 surface")


@dataclass(frozen=True)
class InteractionCapability:
    """互動能力面（她本人的介面）；facets 沿用提案 §13.1 的 capability 清單。宣告不等於可 acquire（EC-4）。"""

    facets: frozenset[InteractionFacet]

    def __post_init__(self) -> None:
        if not isinstance(self.facets, frozenset) or not self.facets:
            raise ValueError("interaction.facets 必須是非空 frozenset")
        unknown = sorted(str(f) for f in self.facets if f not in INTERACTION_FACETS)
        if unknown:
            raise ValueError(f"interaction 含未知 facet：{unknown!r}")


@dataclass(frozen=True)
class PlatformCapabilities:
    """一個平台連線的能力宣告：兩種能力面分開宣告（None＝沒有該能力）；至少要宣告其中一種。"""

    platform: PlatformId
    endpoint_instance_id: str
    protocol_version: int
    monitor: MonitorCapability | None = None
    interaction: InteractionCapability | None = None
    declaration_revision: int = 0

    def __post_init__(self) -> None:
        if not is_safe_ref(self.platform):
            raise ValueError(f"platform 不合法：{self.platform!r}")
        if not is_safe_ref(self.endpoint_instance_id):
            raise ValueError(f"endpoint_instance_id 不合法：{self.endpoint_instance_id!r}")
        _require_int("protocol_version", self.protocol_version, 1)
        _require_int("declaration_revision", self.declaration_revision, 0)
        if self.monitor is not None and not isinstance(self.monitor, MonitorCapability):
            raise ValueError("monitor 必須是 MonitorCapability 或 None")
        if self.interaction is not None and not isinstance(self.interaction, InteractionCapability):
            raise ValueError("interaction 必須是 InteractionCapability 或 None")
        if self.monitor is None and self.interaction is None:
            raise ValueError("monitor 與 interaction 至少要宣告一種能力面")


@dataclass(frozen=True)
class AbsentInboundItem:
    """monitor 入站的唯一型別（不是 UtteranceFact，EC-2）。不存原文，只存可刪 blob 引用與長度（D78）。

    ``source_platform`` 只描述輸入來源，不代表她在場（F6）。``actor_ref`` 只收平台認證 ID 形式
    （不收顯示名稱）；``is_owner`` 由 Core 依 platform_links 計算，未認證者不可為真。"""

    source_platform: PlatformId
    surface_ref: str
    actor_ref: str
    actor_authenticated: bool
    is_owner: bool
    raw_ref: str
    length: int
    received_at: datetime

    def __post_init__(self) -> None:
        if not is_safe_ref(self.source_platform):
            raise ValueError(f"source_platform 不合法：{self.source_platform!r}")
        if not is_safe_ref(self.surface_ref):
            raise ValueError(f"surface_ref 不合法：{self.surface_ref!r}")
        if not isinstance(self.actor_ref, str) or _ACTOR_REF.fullmatch(self.actor_ref) is None:
            raise ValueError("actor_ref 必須是平台認證 ID 形式（小寫英數與 _ . : -），不可是顯示名稱")
        _require_bool("actor_authenticated", self.actor_authenticated)
        _require_bool("is_owner", self.is_owner)
        if self.is_owner and not self.actor_authenticated:
            raise ValueError("未認證的 actor 不可為 owner（is_owner 需 actor_authenticated）")
        # 命名空間綁定（PD-15 b）：已認證 actor 只能是來源平台命名空間的 ID
        if self.actor_authenticated and not self.actor_ref.startswith(f"{self.source_platform}:"):
            raise ValueError("已認證的 actor_ref 必須屬於 source_platform 命名空間")
        if not isinstance(self.raw_ref, str) or _BLOB_REF.fullmatch(self.raw_ref) is None:
            raise ValueError("raw_ref 必須是 blob 引用（不得存放原文）")
        _require_int("length", self.length, 0)
        if not isinstance(self.received_at, datetime) or self.received_at.tzinfo is None:
            raise ValueError("received_at 必須是帶時區的 datetime")


@dataclass(frozen=True)
class PresenceRequest:
    """提交主機裁決的到場／移轉請求（M-7）；本身不授權、不寫 Presence。

    只能由已認證 actor 建立（``actor_authenticated`` 必須為真）；第一階段 authority 只有 owner／system，
    member／guest 不建立請求（F10、M-11）。優先度由 Core 依規則計算，模型輸出不能影響（M-10）。"""

    request_id: str
    source_platform: PlatformId
    requester_ref: str
    actor_authenticated: bool
    authority: Literal["owner", "system"]
    priority: int
    target: PlatformId
    reason_ref: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or _REQUEST_ID.fullmatch(self.request_id) is None:
            raise ValueError(f"request_id 不合法（非空、無空白、256 字元內）：{self.request_id!r}")
        if not is_safe_ref(self.source_platform):
            raise ValueError(f"source_platform 不合法：{self.source_platform!r}")
        if not isinstance(self.requester_ref, str) or _ACTOR_REF.fullmatch(self.requester_ref) is None:
            raise ValueError("requester_ref 必須是平台認證 ID 形式，不可是顯示名稱")
        if self.actor_authenticated is not True:
            raise ValueError("PresenceRequest 只能由已認證 actor 建立（actor_authenticated 必須為 True）")
        if self.authority not in PRESENCE_REQUEST_AUTHORITIES:
            raise ValueError(f"authority 第一階段只收 owner／system：{self.authority!r}")
        if self.authority == "owner" and not self.requester_ref.startswith(f"{self.source_platform}:"):
            raise ValueError("owner 請求的 requester_ref 必須屬於 source_platform 命名空間")
        _require_int("priority", self.priority, 0)
        if not is_safe_ref(self.target):
            raise ValueError(f"target 不合法：{self.target!r}")
        if self.reason_ref is not None and (
            not isinstance(self.reason_ref, str) or _BLOB_REF.fullmatch(self.reason_ref) is None
        ):
            raise ValueError("reason_ref 必須是 blob 引用（不得存放自由文字）")
