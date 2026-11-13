"""ModuleHandle Protocol（設計稿 §5.2 的最小子集；PR-2）。

實作放在各 wiring／executor 旁（可 import 子系統），lifecycle 只認這個 Protocol。方法皆可阻塞，coordinator 在受限 worker 上呼叫。
真實 handle 屬 PR-4；PR-2 只用 fake handle 驗收。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

# "warming"（PR-4）：僅觀測型 handle 使用——持有者尚在載入中（要能和失敗區分）；coordinator 對觀測模組轉／維持 warming，
# 不燒重試次數、不判 failed；對可載入的 handle 視同 "ok"。
ProbeResult = Literal["ok", "degraded", "dead", "warming"]


class ObservationOnlyError(RuntimeError):
    """觀測型 handle 被要求 load／warm／unload（契約違反，R3）。"""


@runtime_checkable
class ModuleHandle(Protocol):
    can_load: bool
    can_unload: bool

    def load(self) -> None: ...

    def warm(self) -> None: ...

    def unload(self) -> None: ...

    def probe(self) -> ProbeResult: ...


@dataclass(frozen=True)
class ObservedResource:
    """handle 回報的實際用量（MB）；None = 無法觀測該項。"""

    vram_mb: int | None = None


@runtime_checkable
class ResourceObservable(Protocol):
    """選填介面：handle 載入完成後可回報實際 VRAM 用量，coordinator 用來校正描述子的宣告值（§7.3）。
    不實作也可以（不校正）；方法在 worker 上呼叫，可阻塞。"""

    def observed_resource(self) -> ObservedResource | None: ...


class ObservationOnlyHandle:
    """觀測型 handle 基底：``can_load=can_unload=False``，load／warm／unload 一律拒絕；子類只實作 ``probe``。
    coordinator 對這類 handle 永遠不呼叫 load／unload（狀態只由 probe 推導）。"""

    can_load = False
    can_unload = False

    def load(self) -> None:
        raise ObservationOnlyError("觀測型 handle 禁止 load")

    def warm(self) -> None:
        raise ObservationOnlyError("觀測型 handle 禁止 warm")

    def unload(self) -> None:
        raise ObservationOnlyError("觀測型 handle 禁止 unload")

    def probe(self) -> ProbeResult:
        raise NotImplementedError
