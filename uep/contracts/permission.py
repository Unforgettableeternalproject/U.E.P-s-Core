"""權限與指示分級契約型別（PM-1；零依賴，只含純資料與純函式）。

依據：hidden_docs/架構重構/權限與指示分級契約草案.md §0（PERM-1～12）、§1、§2。
- 授權 = 請求者階級（Tier）× 指示等級（Instruction）× 工作分級（Grade）× 工具屬性，任何一軸都不能放寬另一軸。
- 本模組只定義型別、預設對照表與載入驗證；仲裁器消費對照表屬 PM-2，身分解析屬 PM-3。
- 對照表可設定，但有硬下限（``HARD_FLOORS``），且只能收緊（PERM-9）；驗證失敗一律回退內建預設表（fail-closed）。
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from types import MappingProxyType

logger = logging.getLogger(__name__)


class RequesterTier(IntEnum):
    """請求者階級；值 = 優先序帶（band），也是階級比較用的 rank。數值帶是程式常數，不進設定檔。"""

    GUEST = 10  # 訪客
    UNVERIFIED = 30  # 未驗證成員
    MEMBER = 50  # 已驗證成員（= 現有 PRIORITY_REQUESTER）
    ADMIN = 75  # 管理員
    SYSTEM = 100  # 系統訊息（受信任的系統指令）
    OWNER = 200
    EMERGENCY = 300  # 系統緊急


# 帶數值對照（priority_of 走 tier 後的來源）
TIER_BAND: dict[RequesterTier, int] = {tier: int(tier) for tier in RequesterTier}


class InstructionLevel(IntEnum):
    """指示等級；值越大 = 所需權限越高，只用於排序與單調性檢查。"""

    CHAT = 10  # 一般聊天／動作
    MEMORY = 20  # 記憶
    PUBLIC_INFO = 30  # 公開資訊分享
    ACTION = 40  # 行為（非工作的一次性動作）
    TASK_MINOR = 50  # 次級工作
    TASK_NORMAL = 60  # 工作
    TASK_SUPERIOR = 70  # 上級工作
    PAUSE = 80  # 休息（工作暫停；不是 Behavior 的 REST）
    TEMP_SESSION = 90  # 臨時階段（臨時指示 override）
    SYSTEM_INFO = 100  # 提供系統資訊
    SESSION_INTERRUPT = 110  # 中斷工作階段
    FORCED_SLEEP = 120  # 強制睡眠


class WorkGrade(StrEnum):
    """工作分級。"""

    SUPERIOR = "superior"
    NORMAL = "normal"
    MINOR = "minor"


GRADE_LEVEL: dict[WorkGrade, InstructionLevel] = {
    WorkGrade.SUPERIOR: InstructionLevel.TASK_SUPERIOR,
    WorkGrade.NORMAL: InstructionLevel.TASK_NORMAL,
    WorkGrade.MINOR: InstructionLevel.TASK_MINOR,
}

# ---- authority 與 tier 的一致性（§1.3）----
# authority 是「請求怎麼來的」（授權路徑），tier 是階級；ADMIN 沿用 member 授權路徑
AUTHORITY_TIERS: dict[str, frozenset[RequesterTier]] = {
    "owner": frozenset({RequesterTier.OWNER}),
    "system": frozenset({RequesterTier.SYSTEM}),
    "member": frozenset({RequesterTier.MEMBER, RequesterTier.ADMIN}),
}
AUTHORITY_DEFAULT_TIER: dict[str, RequesterTier] = {
    "owner": RequesterTier.OWNER, "system": RequesterTier.SYSTEM, "member": RequesterTier.MEMBER}


def derive_tier(authority: str, emergency: bool = False) -> RequesterTier:
    """由 authority／emergency 推導預設階級；未知 authority 拋 ValueError（fail-closed）。"""
    if emergency:
        return RequesterTier.EMERGENCY
    try:
        return AUTHORITY_DEFAULT_TIER[authority]
    except KeyError:
        raise ValueError(f"authority 只收 owner／system／member：{authority!r}") from None


def check_authority_tier(authority: str, tier: RequesterTier, emergency: bool = False) -> None:
    """authority↔tier 一致性檢查（§1.3），不一致拋 ValueError。

    ``emergency=True`` 時 tier 必為 EMERGENCY。非 system 的 authority 帶 emergency 仍可建構：
    仲裁器以 ``emergency_requires_system`` 拒絕（既有行為，PM-1 不改判定）。
    """
    if not isinstance(tier, RequesterTier):
        raise TypeError(f"tier 必須是 RequesterTier：{tier!r}")
    if authority not in AUTHORITY_TIERS:
        raise ValueError(f"authority 只收 owner／system／member：{authority!r}")
    if emergency:
        if tier != RequesterTier.EMERGENCY:
            raise ValueError("emergency=True 時 tier 必須是 EMERGENCY")
        return
    if tier not in AUTHORITY_TIERS[authority]:
        raise ValueError(f"authority={authority!r} 與 tier={tier.name} 不一致")


# ---- 系統元件身分閉集（§1.2a；PERM-10）----

@dataclass(frozen=True)
class SystemComponent:
    """登記過的 Core 元件身分；只能從內部通道發起。其餘能力皆預設 False（只能收緊，PERM-9）。"""

    component_id: str  # 例：core.schedule_planner、core.global_stop
    max_tier: RequesterTier = RequesterTier.SYSTEM  # SYSTEM；緊急來源可為 EMERGENCY
    may_propose_transfer: bool = False  # 是否可提出 PresenceRequest（Q-PM-6）

    def __post_init__(self) -> None:
        if not isinstance(self.component_id, str) or not self.component_id.startswith("core.") \
                or len(self.component_id) <= len("core."):
            raise ValueError(f"component_id 必須是 core.<name> 形式：{self.component_id!r}")
        if self.max_tier not in (RequesterTier.SYSTEM, RequesterTier.EMERGENCY):
            raise ValueError("系統元件的 max_tier 只能是 SYSTEM 或 EMERGENCY")


# 登記表是程式常數＋測試鎖住；新增＝改程式碼＋測試＋審核（與 CAPABILITY_CEILING 同一治理）
SYSTEM_COMPONENTS: frozenset[SystemComponent] = frozenset({
    # Chatroom 指派不是系統元件：其內容視為資料，不進仲裁（11/13 裁決）
    SystemComponent("core.schedule_planner", may_propose_transfer=True),
    SystemComponent("core.global_stop", RequesterTier.EMERGENCY, may_propose_transfer=True),
    SystemComponent("core.health_watchdog", RequesterTier.EMERGENCY, may_propose_transfer=True),
})
_SYSTEM_COMPONENT_BY_ID: dict[str, SystemComponent] = {c.component_id: c for c in SYSTEM_COMPONENTS}


def system_component(component_id: str) -> SystemComponent | None:
    """依身分查登記表；不在閉集內回 None。"""
    return _SYSTEM_COMPONENT_BY_ID.get(component_id)


def is_system_component(component_id: str) -> bool:
    return component_id in _SYSTEM_COMPONENT_BY_ID


# ---- 指示對照表（§2.2、§2.3）----

@dataclass(frozen=True)
class InstructionRule:
    min_tier: RequesterTier
    requires_human_origin: bool = False  # 僅 FORCED_SLEEP 預設為 True（§5）
    requires_confirmation: bool = False  # 兩步確認；FORCED_SLEEP 預設為 True


L = InstructionLevel
T = RequesterTier

# 內建預設表（§2.2）
DEFAULT_RULES: Mapping[InstructionLevel, InstructionRule] = MappingProxyType({
    L.FORCED_SLEEP: InstructionRule(T.OWNER, requires_human_origin=True, requires_confirmation=True),
    L.SESSION_INTERRUPT: InstructionRule(T.OWNER),
    L.SYSTEM_INFO: InstructionRule(T.SYSTEM),
    L.TEMP_SESSION: InstructionRule(T.ADMIN),
    L.PAUSE: InstructionRule(T.ADMIN),
    L.TASK_SUPERIOR: InstructionRule(T.MEMBER),
    L.TASK_NORMAL: InstructionRule(T.MEMBER),
    L.TASK_MINOR: InstructionRule(T.UNVERIFIED),
    L.ACTION: InstructionRule(T.UNVERIFIED),
    L.PUBLIC_INFO: InstructionRule(T.UNVERIFIED),
    L.MEMORY: InstructionRule(T.UNVERIFIED),
    L.CHAT: InstructionRule(T.GUEST),
})

# 硬下限（PERM-3）：寫死在程式，設定不可降低；MEMORY 訪客永遠不能用
HARD_FLOORS: Mapping[InstructionLevel, RequesterTier] = MappingProxyType({
    L.FORCED_SLEEP: T.OWNER,
    L.SESSION_INTERRUPT: T.OWNER,
    L.MEMORY: T.UNVERIFIED,
})

del L, T  # 縮寫只供上方兩張表使用

DEFAULT_POLICY_REVISION = 1


@dataclass(frozen=True)
class PermissionPolicy:
    """指示等級 → 最低階級的對照表（純函式授權）。"""

    revision: int
    rules: Mapping[InstructionLevel, InstructionRule]

    def __post_init__(self) -> None:
        object.__setattr__(self, "rules", MappingProxyType(dict(self.rules)))  # 唯讀副本

    def rule_of(self, level: InstructionLevel) -> InstructionRule:
        return self.rules[level]

    def required_tier(self, level: InstructionLevel) -> RequesterTier:
        return self.rules[level].min_tier

    def authorize(self, tier: RequesterTier, level: InstructionLevel, *, human_origin: bool = False) -> bool:
        """階級是否足以下此指示。

        規則要求 human-origin（強制睡眠）時，呼叫端必須明示 ``human_origin=True``（預設 False，fail-closed）；
        因此緊急階級（系統元件，非人類）也不會取得強制睡眠權（Q-PM-11）。兩步確認不在此判定。
        """
        rule = self.rules[level]
        if tier < rule.min_tier:
            return False
        return human_origin or not rule.requires_human_origin


def validate_rules(rules: object, *, baseline: Mapping[InstructionLevel, InstructionRule] = DEFAULT_RULES) -> list[str]:
    """驗證對照表；回違規描述清單（空＝通過）。

    檢查：(1) 12 級齊全且型別正確；(2) 單調性 PERM-4；(3) 硬下限不可降；(4) 只能收緊 PERM-9。
    """
    if not isinstance(rules, Mapping):
        return ["rules 必須是對照表"]
    errors: list[str] = []
    for key, rule in rules.items():
        if not isinstance(key, InstructionLevel):
            errors.append(f"未知的指示等級：{key!r}")
        elif not isinstance(rule, InstructionRule) or not isinstance(rule.min_tier, RequesterTier) \
                or not isinstance(rule.requires_human_origin, bool) \
                or not isinstance(rule.requires_confirmation, bool):
            errors.append(f"{key.name} 的規則型別不合法")
    missing = [level.name for level in InstructionLevel if level not in rules]
    if missing:
        errors.append(f"缺少指示等級：{', '.join(missing)}")
    if errors:
        return errors  # 結構已壞，後續檢查無意義
    previous: tuple[InstructionLevel, RequesterTier] | None = None
    for level in sorted(InstructionLevel):
        tier = rules[level].min_tier
        if previous is not None and tier < previous[1]:
            errors.append(f"單調性違反：{level.name} 的最低階級 {tier.name} 低於 {previous[0].name} 的 {previous[1].name}")
        previous = (level, tier)
    for level, floor in HARD_FLOORS.items():
        if rules[level].min_tier < floor:
            errors.append(f"硬下限違反：{level.name} 不得低於 {floor.name}")
    for level in InstructionLevel:
        base, rule = baseline[level], rules[level]
        if rule.min_tier < base.min_tier:
            errors.append(f"只能收緊：{level.name} 的最低階級不得低於預設 {base.min_tier.name}")
        if base.requires_human_origin and not rule.requires_human_origin:
            errors.append(f"只能收緊：{level.name} 不得取消 human-origin 要求")
        if base.requires_confirmation and not rule.requires_confirmation:
            errors.append(f"只能收緊：{level.name} 不得取消確認要求")
    return errors


DEFAULT_POLICY = PermissionPolicy(DEFAULT_POLICY_REVISION, DEFAULT_RULES)


def load_policy(rules: object, revision: int) -> PermissionPolicy:
    """載入設定的對照表；驗證失敗回退內建預設表並記錄警告（fail-closed 到預設，不是全開）。"""
    errors = validate_rules(rules)
    if errors or not isinstance(revision, int) or isinstance(revision, bool):
        if not errors:
            errors = [f"revision 必須是整數：{revision!r}"]
        logger.warning("權限對照表驗證失敗，回退內建預設表：%s", "；".join(errors))
        return DEFAULT_POLICY
    return PermissionPolicy(revision, rules)  # type: ignore[arg-type]  # validate_rules 已確認為 Mapping
