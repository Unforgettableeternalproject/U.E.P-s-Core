"""
Sharing Schema - 跨使用者分享（M10）的資料模型與純函式

可見度掛在「概念 x 場域」上（Concept.sharing），不掛在 Episode／EvidenceUnit
（證據是 append-only 的事實紀錄，不因可見度而改寫）。證據層的「來源可見度」
由 origin_visibility() 以結構欄位導出，不新增儲存。

設計原則：
  - fail-closed：缺 attrs／dm／audience=1／缺 scope／缺 ledger／壞值，一律 private
  - 只用結構欄位判斷（channel_kind、audience_size、scope、source_type、ledger 成員），
    不從文字內容推測，不使用 keyword／regex 判斷隱私
  - 使用者覆寫只能收緊（private 且 pinned），本模組沒有任何放寬路徑

scope 字串規格：
  - 證據 scope：``guild:<guild_id>``（沿用 EvidenceUnit.scope）
  - 共享 namespace id：``scope-g<guild_id>``（符合 namespace 正規化）
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

# ─── 列舉 ─────────────────────────────────────────────────────────────────────

LEVEL_PRIVATE = "private"
LEVEL_SCOPE_SHARED = "scope_shared"
LEVEL_PUBLIC = "public"  # 保留列舉值，本期不實作（讀入一律降為 private）
SHARING_LEVELS = (LEVEL_PRIVATE, LEVEL_SCOPE_SHARED, LEVEL_PUBLIC)

SENSITIVITIES = ("none", "personal", "sensitive")
# 只有這些敏感度可以投影；sensitive 維持 private
SHAREABLE_SENSITIVITIES = ("none", "personal")

DECIDED_BY = ("default", "llm", "user")

# 可成為 scope_shared 來源的頻道類型（dm／cli／mcp 一律 private）
SHAREABLE_CHANNEL_KINDS = ("voice", "text")

# ─── scope 字串工具 ───────────────────────────────────────────────────────────

SCOPE_GUILD_PREFIX = "guild:"
SCOPE_NAMESPACE_PREFIX = "scope-g"
# "scope-g" 佔 7 字，namespace id 上限 64，故 guild id 最多 57；保守取 48
# （Discord snowflake 最多 20 位數字，綽綽有餘）
_GUILD_ID_MAX_LEN = 48


def is_valid_guild_id(guild_id: Any) -> bool:
    """guild id 是否合法：純 ASCII 數字（Discord snowflake），1～48 位。"""
    if not isinstance(guild_id, str) or not guild_id or len(guild_id) > _GUILD_ID_MAX_LEN:
        return False
    return all(ch in "0123456789" for ch in guild_id)


def guild_scope(guild_id: Any) -> str:
    """guild id -> 正規化的 ``guild:<id>``；id 無效拋 ValueError。"""
    gid = str(guild_id).strip().lower() if guild_id is not None else ""
    if not is_valid_guild_id(gid):
        raise ValueError(f"無效的 guild id: {guild_id!r}")
    return f"{SCOPE_GUILD_PREFIX}{gid}"


def parse_guild_scope(scope: Any) -> str | None:
    """``guild:<id>`` -> guild id；格式不符回傳 None（不拋例外）。前綴大小寫不拘（小寫正規化）。"""
    if not isinstance(scope, str):
        return None
    scope = scope.strip().lower()
    if not scope.startswith(SCOPE_GUILD_PREFIX):
        return None
    gid = scope[len(SCOPE_GUILD_PREFIX):]
    return gid if is_valid_guild_id(gid) else None


def normalize_scope(scope: Any) -> str | None:
    """scope 字串的正規形式（小寫 ``guild:<id>``）；無效回傳 None。"""
    gid = parse_guild_scope(scope)
    return f"{SCOPE_GUILD_PREFIX}{gid}" if gid is not None else None


def is_guild_scope(scope: Any) -> bool:
    return parse_guild_scope(scope) is not None


def scope_to_namespace_id(scope: str) -> str:
    """``guild:<id>`` -> ``scope-g<id>``；scope 無效拋 ValueError。"""
    gid = parse_guild_scope(scope)
    if gid is None:
        raise ValueError(f"無效的 scope: {scope!r}")
    return f"{SCOPE_NAMESPACE_PREFIX}{gid}"


def namespace_to_scope(namespace_id: Any) -> str | None:
    """``scope-g<id>`` -> ``guild:<id>``；不是共享 namespace id 回傳 None。"""
    if not isinstance(namespace_id, str):
        return None
    namespace_id = namespace_id.strip().lower()
    if not namespace_id.startswith(SCOPE_NAMESPACE_PREFIX):
        return None
    gid = namespace_id[len(SCOPE_NAMESPACE_PREFIX):]
    return f"{SCOPE_GUILD_PREFIX}{gid}" if is_valid_guild_id(gid) else None


def is_scope_namespace_id(namespace_id: Any) -> bool:
    return namespace_to_scope(namespace_id) is not None


# ─── 來源可見度（純函式，不入庫）─────────────────────────────────────────────


def _audience_size(attrs: dict[str, Any]) -> int | None:
    raw = attrs.get("audience_size")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 1 else None
    if isinstance(raw, str) and raw.strip().isdigit():
        n = int(raw.strip())
        return n if n >= 1 else None
    return None


def min_audience_size(cfg: Any = None) -> int:
    """可分享的最小在場人數；下限鎖在 2（單人／DM 永遠不可分享）。"""
    if cfg is None:
        from echo_memory.config import get_config
        cfg = get_config()
    try:
        return max(2, int(cfg.sharing_min_audience_size))
    except (AttributeError, TypeError, ValueError):
        return 2


def unit_guild_scope(unit: Any) -> str | None:
    """unit 的 scope 若是合法 ``guild:<id>`` 則回傳，否則 None。"""
    return normalize_scope(getattr(unit, "scope", None))


def origin_visibility(unit: Any, ledger: Any) -> str:
    """EvidenceUnit 的來源可見度："private" | "scope_shared"（只看結構欄位）。

    規則（任一不成立即 private，fail-closed）：
      1. unit 未被遮蔽（tombstone），且 id 不在 privacy ledger（使用者覆寫）
      2. source_type == "user"（agent／tool／observation 不是使用者要分享的）
      3. attrs.channel_kind 是 voice／text（dm／cli／mcp／缺值皆 private）
      4. attrs.audience_size >= 2（缺值／無效／1 皆 private）
      5. scope 是合法 ``guild:<id>``（缺 scope、"namespace"、其他格式皆 private）

    ledger 為 None 或查詢失敗一律視為 private：無法確認使用者覆寫就不放行。
    ledger 只需有 ``is_restricted(unit_id) -> bool``。
    """
    if unit is None or ledger is None:
        return LEVEL_PRIVATE
    try:
        if getattr(unit, "tombstoned_at", None) is not None:
            return LEVEL_PRIVATE
        if ledger.is_restricted(str(getattr(unit, "id", ""))):
            return LEVEL_PRIVATE
        if getattr(unit, "source_type", None) != "user":
            return LEVEL_PRIVATE
        attrs = getattr(unit, "attrs", None)
        if not isinstance(attrs, dict):
            return LEVEL_PRIVATE
        if attrs.get("channel_kind") not in SHAREABLE_CHANNEL_KINDS:
            return LEVEL_PRIVATE
        n = _audience_size(attrs)
        if n is None or n < min_audience_size():
            return LEVEL_PRIVATE
        if unit_guild_scope(unit) is None:
            return LEVEL_PRIVATE
    except Exception:
        return LEVEL_PRIVATE
    return LEVEL_SCOPE_SHARED


def origin_scope(unit: Any, ledger: Any) -> str | None:
    """origin_visibility 為 scope_shared 時回傳該 unit 的 ``guild:<id>``，否則 None。"""
    if origin_visibility(unit, ledger) != LEVEL_SCOPE_SHARED:
        return None
    return unit_guild_scope(unit)


# ─── Concept.sharing ──────────────────────────────────────────────────────────


def _str_list(value: Any) -> list[str]:
    return [str(x) for x in value] if isinstance(value, list) else []


@dataclass
class ScopeSharing:
    """概念在單一場域的分享狀態（Concept.sharing["by_scope"][scope_id]）。

    Attributes:
        level: private / scope_shared（public 保留，讀入降為 private）
        decided_by: default / llm / user
        pinned: True＝使用者收緊鎖定，Dream／LLM 不得再放寬
        cited_unit_ids: 判定當下引用的 scope_shared 來源 unit id（U_S 子集）
        basis_digest: 判定當下 compute_basis_digest(concept)；與現況不符即失效
        summary_digest: 共享摘要的 sha256（摘要本文只存在共享 store，不存這裡）
        projection_id: 共享 namespace 內的投影 id；空字串＝尚無／已清除
        sensitivity: none / personal / sensitive
        judged_at: ISO 時間字串
        patch_id: 寫入這筆狀態的 share patch id
    """

    level: str = LEVEL_PRIVATE
    decided_by: str = "default"
    pinned: bool = False
    cited_unit_ids: list[str] = field(default_factory=list)
    basis_digest: str = ""
    summary_digest: str = ""
    projection_id: str = ""
    sensitivity: str = "none"
    judged_at: str = ""
    patch_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "decided_by": self.decided_by,
            "pinned": bool(self.pinned),
            "cited_unit_ids": list(self.cited_unit_ids),
            "basis_digest": self.basis_digest,
            "summary_digest": self.summary_digest,
            "projection_id": self.projection_id,
            "sensitivity": self.sensitivity,
            "judged_at": self.judged_at,
            "patch_id": self.patch_id,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ScopeSharing:
        """容錯解析：非 dict／未知或壞值一律退回 private（fail-closed）。"""
        if not isinstance(data, dict):
            return cls()
        level = data.get("level")
        if level != LEVEL_SCOPE_SHARED:  # private／public（保留）／未知 -> private
            level = LEVEL_PRIVATE
        decided_by = data.get("decided_by")
        sensitivity = data.get("sensitivity")
        return cls(
            level=level,
            decided_by=decided_by if decided_by in DECIDED_BY else "default",
            pinned=data.get("pinned") is True,
            cited_unit_ids=_str_list(data.get("cited_unit_ids")),
            basis_digest=str(data.get("basis_digest", "") or ""),
            summary_digest=str(data.get("summary_digest", "") or ""),
            projection_id=str(data.get("projection_id", "") or ""),
            sensitivity=sensitivity if sensitivity in SENSITIVITIES else "none",
            judged_at=str(data.get("judged_at", "") or ""),
            patch_id=str(data.get("patch_id", "") or ""),
        )


@dataclass
class SharingState:
    """Concept.sharing 的結構化視圖。預設空＝所有場域 private，舊資料零遷移。"""

    by_scope: dict[str, ScopeSharing] = field(default_factory=dict)

    def get(self, scope_id: str) -> ScopeSharing | None:
        return self.by_scope.get(scope_id)

    def level(self, scope_id: str) -> str:
        entry = self.by_scope.get(scope_id)
        return entry.level if entry is not None else LEVEL_PRIVATE

    def to_dict(self) -> dict[str, Any]:
        if not self.by_scope:
            return {}
        return {"by_scope": {k: v.to_dict() for k, v in self.by_scope.items()}}

    @classmethod
    def from_dict(cls, data: Any) -> SharingState:
        if not isinstance(data, dict):
            return cls()
        raw = data.get("by_scope")
        if not isinstance(raw, dict):
            return cls()
        by_scope: dict[str, ScopeSharing] = {}
        for k, v in raw.items():
            key = normalize_scope(str(k))
            if key is not None:  # 非合法 scope 的項目直接丟棄
                by_scope[key] = ScopeSharing.from_dict(v)
        return cls(by_scope=by_scope)


def copy_sharing(sharing: Any) -> dict[str, Any]:
    """Concept.sharing 的深拷貝（非 dict 視為空）。"""
    return copy.deepcopy(sharing) if isinstance(sharing, dict) else {}
