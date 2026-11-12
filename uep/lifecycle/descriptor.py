"""模組描述子（Module Coordinator 設計稿 §5.1 的最小子集；PR-2）。

只放 coordinator 狀態機需要的欄位；資源向量、預算、優先序驅逐、lease／pin 屬 PR-3，不在此。
純資料，不得 import ``uep.tools``／``uep.decision``／``uep.echo``／``uep.stream`` 或子系統。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, get_args

ModuleKind = Literal["service", "model", "stage", "platform"]
PriorityClass = Literal["resident", "on_demand", "speculative", "background"]

MODULE_KINDS: frozenset[str] = frozenset(get_args(ModuleKind))
PRIORITY_CLASSES: frozenset[str] = frozenset(get_args(PriorityClass))

# R9：扁平舊 id（``lore``、``web_search``）與 ``kind:name`` 並存
_ID_RE = re.compile(r"[a-z][a-z0-9_]*(?::[a-z][a-z0-9_]*)?")
_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def canonical_name(module_id: str) -> str:
    """同一資源只能有一個 canonical id（R9）：取 ``kind:name`` 的 name 部分比對，扁平 id 即自身。"""
    return module_id.rsplit(":", 1)[-1]


@dataclass(frozen=True)
class ModuleDescriptor:
    """最小描述子。

    - ``depends_on``：其他 module_id；依賴未 ready 則不啟動本模組。
    - ``autoload``：``start()`` 後自動載入（resident 語意）；否則等 ``request_load``。
    - ``needs_warm``：load 之後還要 ``warm``（loading -> warming -> ready）。
    - ``blocked_reason``：設定層閘（未設定／禁止）。非 None 時模組恆為 unavailable，任何 probe 成功都不得覆蓋，
      也不自動重試（R12）。閉集短碼，會原樣放進事件的 ``reason_code``。
    """

    module_id: str
    kind: ModuleKind = "service"
    depends_on: tuple[str, ...] = ()
    load_timeout_s: float = 30.0
    unload_timeout_s: float = 30.0
    priority: PriorityClass = "on_demand"
    autoload: bool = False
    needs_warm: bool = False
    blocked_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.module_id, str) or not _ID_RE.fullmatch(self.module_id):
            raise ValueError("module_id 必須是扁平 id 或 kind:name（小寫英數與底線）")
        if self.kind not in MODULE_KINDS:
            raise ValueError(f"不支援的 kind：{self.kind!r}")
        if self.priority not in PRIORITY_CLASSES:
            raise ValueError(f"不支援的 priority：{self.priority!r}")
        for key in ("load_timeout_s", "unload_timeout_s"):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{key} 必須是正數")
        if isinstance(self.depends_on, str) or any(not isinstance(d, str) or not d for d in self.depends_on):
            raise ValueError("depends_on 必須是 module_id 的 tuple")
        if self.module_id in self.depends_on:
            raise ValueError("模組不得依賴自己")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("depends_on 含重複 id")
        if self.blocked_reason is not None and not _REASON_RE.fullmatch(self.blocked_reason):
            raise ValueError("blocked_reason 必須是閉集短碼（小寫英數與底線）")

    @property
    def blocked(self) -> bool:
        return self.blocked_reason is not None
