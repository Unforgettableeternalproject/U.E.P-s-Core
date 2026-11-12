"""模組描述子（Module Coordinator 設計稿 §5.1 的最小子集；PR-2）。

只放 coordinator 需要的欄位（PR-3 增加 pinned／uses_gpu／vram_mb；優先序由 ``priority`` 決定，R8）。
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
    - ``pinned``：設定層永久釘住（PR-3，設計稿 §7.2）：永不被驅逐、不接受 ``request_unload``，但仍可因失敗進入 ``failed``。
      v1 的 M 群（kind=model／stage）描述子必須 pinned（§4），由組裝層檢查。
    - ``uses_gpu`` / ``vram_mb``（PR-3，R6）：會由 coordinator 載入、且需要 VRAM 的模組要宣告 ``uses_gpu=True``；``vram_mb`` 是
      載入峰值的宣告成本（MB），未量測請留 None（=> 拒絕載入，``unmeasured_cost``）。不需 GPU 的服務不受預算管理影響。
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
    pinned: bool = False
    uses_gpu: bool = False
    vram_mb: int | None = None

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
        for key in ("pinned", "uses_gpu"):
            if not isinstance(getattr(self, key), bool):
                raise ValueError(f"{key} 必須是布林值")  # noqa: TRY004  與其他欄位一致，統一 ValueError
        if self.vram_mb is not None:
            if isinstance(self.vram_mb, bool) or not isinstance(self.vram_mb, int) or self.vram_mb < 0:
                raise ValueError("vram_mb 必須是非負整數或 None")
            if not self.uses_gpu:
                raise ValueError("宣告 vram_mb 的模組必須 uses_gpu=True")
        if self.blocked_reason is not None and not _REASON_RE.fullmatch(self.blocked_reason):
            raise ValueError("blocked_reason 必須是閉集短碼（小寫英數與底線）")

    @property
    def blocked(self) -> bool:
        return self.blocked_reason is not None
