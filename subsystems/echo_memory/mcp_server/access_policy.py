"""
MCP namespace 存取策略（M10 包 C）

補洞：過去 handlers 的 ``get_engine(args.get("namespace_id"))`` 允許呼叫者指定任意
namespace（含他人）。「連開發者也看不到使用者私密」不能只靠宿主慣例，記憶端要有保證。

規則（全部只看 namespace id 的結構，不看內容）：
  - 未指定或等於伺服器預設 namespace -> 允許（那是這個 MCP 伺服器自己的資料）。
  - 純數字 id（使用者 namespace，例如 Discord uid）-> 預設拒絕；
    config MCP_ALLOW_USER_NAMESPACES=true 才放行（僅供開發者讀自己的測試 namespace）。
  - ``scope-g<guild>``（場域共享 namespace）-> 寫入一律拒絕（只有 ScopeShareLayer 能寫）；
    讀取僅限開發者：MCP_DEVELOPER_UID 必須在 SHARING_DEVELOPER_UIDS 白名單內。
  - 其餘 id（測試／工具用 namespace）-> 允許。

這是 API 層的政策保證，不是密碼學保證；持有磁碟存取權的人仍可讀檔。

本模組只依賴標準函式庫與 echo_memory.namespace／schemas.sharing，不 import mcp，
可在缺少 MCP SDK 的環境獨立匯入與測試。
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field

from echo_memory.namespace import normalize_namespace_id

_SCOPE_PREFIX = "scope-"
_SCOPE_GUILD_PREFIX = "scope-g"


class NamespaceAccessDenied(PermissionError):
    """MCP 呼叫者無權存取指定的 namespace。"""


@dataclass(frozen=True)
class NamespacePolicy:
    """存取策略設定。預設是最嚴格的（fail-closed）。"""

    default_namespace: str
    allow_user_namespaces: bool = False
    developer_uid: str = ""
    developer_uids: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_developer(self) -> bool:
        return bool(self.developer_uid) and self.developer_uid in self.developer_uids


def policy_from_config(cfg=None, env=None) -> NamespacePolicy:
    """由 echo_memory config 與環境變數建構策略。讀取失敗一律退回最嚴格設定。"""
    env = os.environ if env is None else env
    if cfg is None:
        from echo_memory.config import get_config

        cfg = get_config()
    uids: Iterable[str] = getattr(cfg, "sharing_developer_uids", ()) or ()
    return NamespacePolicy(
        default_namespace=normalize_namespace_id(getattr(cfg, "memory_namespace_id", None)),
        allow_user_namespaces=bool(getattr(cfg, "mcp_allow_user_namespaces", False)),
        developer_uid=str(env.get("MCP_DEVELOPER_UID", "") or "").strip(),
        developer_uids=frozenset(str(u) for u in uids),
    )


def classify_scope_namespace(namespace_id: str) -> str | None:
    """回傳正規化（小寫）的共享 namespace id；不是共享 namespace 回傳 None；形式可疑拋 ValueError。

    Windows 檔案系統不分大小寫，``Scope-G42`` 會落在同一個目錄，所以前綴以 casefold 判斷。
    保留前綴 ``scope-`` 之後必須是 ``g`` 加純數字 guild id，其餘一律視為可疑（拒絕），
    不給任何人用變體名稱繞過。
    """
    folded = namespace_id.casefold()
    if not folded.startswith(_SCOPE_PREFIX):
        return None
    gid = folded[len(_SCOPE_GUILD_PREFIX):] if folded.startswith(_SCOPE_GUILD_PREFIX) else ""
    if not (gid and gid.isascii() and gid.isdigit()):
        raise ValueError(f"保留的 namespace 前綴 {_SCOPE_PREFIX!r} 格式不符: {namespace_id!r}")
    return f"{_SCOPE_GUILD_PREFIX}{gid}"


def is_user_namespace_id(namespace_id: str) -> bool:
    """使用者 namespace：純數字 id（Discord uid 等）。"""
    return namespace_id.isascii() and namespace_id.isdigit()


def check_namespace_access(
    namespace_id: str | None,
    policy: NamespacePolicy,
    *,
    write: bool = False,
) -> str:
    """檢查並回傳正規化後的 namespace id；不允許時拋 NamespaceAccessDenied。

    Args:
        namespace_id: 呼叫者要求的 namespace（None＝伺服器預設）。
        policy: 存取策略。
        write: 該工具是否會寫入。
    """
    try:
        ns = normalize_namespace_id(namespace_id or policy.default_namespace)
    except ValueError as exc:
        raise NamespaceAccessDenied(f"無效的 namespace_id: {exc}") from exc
    if ns == policy.default_namespace:
        return ns
    try:
        scope_ns = classify_scope_namespace(ns)
    except ValueError as exc:
        raise NamespaceAccessDenied(str(exc)) from exc
    if scope_ns is not None:
        ns = scope_ns  # 正規化為小寫，之後的 engine 查找也用這個
        if write:
            raise NamespaceAccessDenied("場域共享 namespace 不可經 MCP 寫入")
        if not policy.is_developer:
            raise NamespaceAccessDenied("場域共享 namespace 僅開發者可讀")
        return ns
    if is_user_namespace_id(ns) and not policy.allow_user_namespaces:
        raise NamespaceAccessDenied("不可經 MCP 存取使用者 namespace")
    return ns
