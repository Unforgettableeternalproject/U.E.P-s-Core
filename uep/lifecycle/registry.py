"""模組登記表：描述子與 handle 的對照（設計稿 §5.2）。純記憶體，非執行緒安全（由 coordinator 的鎖保護）。

登記期拒絕：重複 id、同一資源的第二個 canonical id（R9）、型別錯誤。
缺失依賴與依賴循環要等所有模組登記完（登記順序任意）才能判斷，由 ``validate()`` 在 coordinator.start() 前檢查。
"""
from __future__ import annotations

from collections.abc import Iterator

from uep.lifecycle.descriptor import ModuleDescriptor, canonical_name
from uep.lifecycle.handles import ModuleHandle


class ModuleRegistryError(ValueError):
    """登記表斷言失敗；訊息只含 module_id，不含其他內容。"""


class ModuleRegistry:
    def __init__(self) -> None:
        self._items: dict[str, tuple[ModuleDescriptor, ModuleHandle]] = {}
        self._canonical: dict[str, str] = {}

    def register(self, desc: ModuleDescriptor, handle: ModuleHandle) -> None:
        if not isinstance(desc, ModuleDescriptor):
            raise ModuleRegistryError("desc 必須是 ModuleDescriptor")
        for attr in ("can_load", "can_unload"):
            if not isinstance(getattr(handle, attr, None), bool):
                raise ModuleRegistryError(f"{desc.module_id}: handle 缺少布林屬性 {attr}")
        for method in ("load", "warm", "unload", "probe"):
            if not callable(getattr(handle, method, None)):
                raise ModuleRegistryError(f"{desc.module_id}: handle 缺少方法 {method}")
        if desc.module_id in self._items:
            raise ModuleRegistryError(f"{desc.module_id}: id 重複登記")
        canon = canonical_name(desc.module_id)
        if canon in self._canonical:
            raise ModuleRegistryError(f"{desc.module_id}: 與 {self._canonical[canon]} 是同一資源（canonical 名稱重複）")
        self._items[desc.module_id] = (desc, handle)
        self._canonical[canon] = desc.module_id

    def replace_descriptor(self, desc: ModuleDescriptor) -> None:
        """換掉已登記模組的描述子（供啟動前關閉設定閘用）；id 必須已存在。"""
        if desc.module_id not in self._items:
            raise ModuleRegistryError(f"{desc.module_id}: 未登記")
        self._items[desc.module_id] = (desc, self._items[desc.module_id][1])

    def known_ids(self) -> frozenset[str]:
        return frozenset(self._items)

    def __contains__(self, module_id: object) -> bool:
        return module_id in self._items

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def descriptor(self, module_id: str) -> ModuleDescriptor:
        return self._items[module_id][0]

    def handle(self, module_id: str) -> ModuleHandle:
        return self._items[module_id][1]

    def validate(self) -> list[str]:
        """檢查缺失依賴與依賴循環，通過時回傳拓撲序（依賴在前，同層維持登記順序）；失敗拋 ``ModuleRegistryError``。"""
        for module_id, (desc, _) in self._items.items():
            missing = [d for d in desc.depends_on if d not in self._items]
            if missing:
                raise ModuleRegistryError(f"{module_id}: 依賴未登記的模組 {missing}")
        order: list[str] = []
        state: dict[str, int] = {}  # 1 = 走訪中，2 = 完成

        def visit(node: str, path: tuple[str, ...]) -> None:
            if state.get(node) == 2:
                return
            if state.get(node) == 1:
                cycle = " -> ".join((*path[path.index(node):], node))
                raise ModuleRegistryError(f"依賴循環：{cycle}")
            state[node] = 1
            for dep in self._items[node][0].depends_on:
                visit(dep, (*path, node))
            state[node] = 2
            order.append(node)

        for module_id in self._items:
            visit(module_id, ())
        return order
