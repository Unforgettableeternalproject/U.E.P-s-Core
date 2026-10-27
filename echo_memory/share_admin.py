"""
ShareAdmin - 開發者管理介面（M10 包 C）

開發者能看：全場域的共享概念列表與內容（概念摘要）、各場域統計（只有計數）、
share audit（只有 id／動作／原因碼／時間，不含文字）。

開發者看不到：任何 private 概念、episode、evidence、privacy ledger 內容——因為這個類別
**沒有任何擁有者 engine 的 handle**：只收 ``storage_root`` 與 ``AccessContext``，內部自建
唯讀用的 ScopeShareLayer（不帶 embed_fn、semantic_provider、digest resolver、cascade hook），
物理上讀不到擁有者 namespace。

這是 API 層的政策保證，不是密碼學保證；持有磁碟存取權的人仍可讀檔。

授權：``AccessContext(viewer_uid, role)`` 由宿主依自己的設定檔（developer_uids 白名單）建構。
role="developer" 時 viewer_uid 還必須在記憶端 config 的 SHARING_DEVELOPER_UIDS 內
（兩者取交集，MCP 入口的二次核對）；role="system" 是宿主內部流程；"member" 一律拒絕。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from echo_memory.storage.share_layer import AUDIT_FILENAME, ScopeShareLayer, normalize_scope

logger = logging.getLogger("echo_memory.share_admin")

ROLE_DEVELOPER = "developer"
ROLE_MEMBER = "member"
ROLE_SYSTEM = "system"
ROLES = (ROLE_DEVELOPER, ROLE_MEMBER, ROLE_SYSTEM)

# audit 只回傳這些欄位（id／動作／原因碼／時間），其餘一律丟棄
_AUDIT_FIELDS = ("at", "action", "projection_id", "owner_uid", "reason_code")
_AUDIT_DEFAULT_LIMIT = 200


@dataclass(frozen=True)
class AccessContext:
    """宿主判定後的存取身分。"""

    viewer_uid: str
    role: str = ROLE_MEMBER


class ShareAdmin:
    """共享層的唯讀管理介面（無擁有者 handle）。"""

    def __init__(
        self,
        access: AccessContext,
        storage_root: str | Path,
        *,
        developer_uids: Iterable[str] | None = None,
    ):
        """
        Args:
            access: 宿主建構的存取身分。
            storage_root: 與 MemoryEngine 相同的 storage root。
            developer_uids: role=developer 時 viewer_uid 必須在其中；None 讀 config
                SHARING_DEVELOPER_UIDS（空＝沒有任何開發者）。
        """
        self._access = access
        if developer_uids is None:
            from echo_memory.config import get_config

            developer_uids = get_config().sharing_developer_uids
        self._developer_uids = frozenset(str(u) for u in developer_uids)
        # 自建唯讀 layer：不給 embed／provider／resolver／hook，因此讀不到任何擁有者資料
        self._layer = ScopeShareLayer(storage_root)

    # ── 授權 ────────────────────────────────────────────────────

    @property
    def access(self) -> AccessContext:
        return self._access

    def is_authorized(self) -> bool:
        role = self._access.role
        if role == ROLE_SYSTEM:
            return True
        if role == ROLE_DEVELOPER:
            return str(self._access.viewer_uid) in self._developer_uids
        return False  # member／未知角色一律拒絕（fail-closed）

    def _require(self) -> None:
        if not self.is_authorized():
            raise PermissionError("沒有查看共享層管理資料的權限")

    # ── 查詢（全部唯讀）──────────────────────────────────────────

    def list_scopes(self) -> list[str]:
        """磁碟上存在的場域（``guild:<id>``）。"""
        self._require()
        return self._layer.list_scopes()

    def list_shared(self, scope: Any) -> list[dict[str, Any]]:
        """場域目前可見（未撤回）的共享概念：只含摘要與 id，沒有逐字證據或私密概念。"""
        self._require()
        scope_id, _ = normalize_scope(scope)
        return [
            {
                "projection_id": v.projection_id,
                "scope_id": v.scope_id,
                "owner_uid": v.owner_uid,
                "summary": v.summary,
                "claim_kind": v.claim_kind,
                "review_status": v.review_status,
                "support_strength": v.support_strength,
                "sensitivity": v.sensitivity,
                "learned_at": v.learned_at,
            }
            for v in self._layer.list_projections(scope_id)
        ]

    def stats(self, scope: Any | None = None) -> dict[str, dict[str, int]]:
        """各場域統計（投影數、撤回數、被 block 的擁有者數…），只有計數。"""
        self._require()
        scopes = [normalize_scope(scope)[0]] if scope is not None else self._layer.list_scopes()
        out: dict[str, dict[str, int]] = {}
        for scope_id in scopes:
            try:
                out[scope_id] = {k: int(v) for k, v in self._layer.stats(scope_id).items()}
            except Exception as exc:  # noqa: BLE001 - 單一場域壞掉不影響其他
                logger.warning("[ShareAdmin] 統計失敗 %s: %s", scope_id, exc)
                out[scope_id] = {"healthy": 0}
        return out

    def audit(self, scope: Any, *, limit: int = _AUDIT_DEFAULT_LIMIT) -> list[dict[str, str]]:
        """場域的 share audit（最新在後）：只含 id／動作／原因碼／時間。壞行略過。"""
        self._require()
        scope_id, _ = normalize_scope(scope)
        path = self._layer.scope_path(scope_id) / AUDIT_FILENAME
        if not path.exists() or limit <= 0:
            return []
        rows: list[dict[str, str]] = []
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                if not raw.strip():
                    continue
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    rows.append({k: str(rec[k]) for k in _AUDIT_FIELDS if k in rec})
        except OSError as exc:
            logger.warning("[ShareAdmin] 讀取 audit 失敗: %s", exc)
            return []
        return rows[-limit:]
