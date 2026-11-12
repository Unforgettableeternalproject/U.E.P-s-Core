"""ModuleHandle Protocol（設計稿 §5.2 的最小子集；PR-2）。

實作放在各 wiring／executor 旁（可 import 子系統），lifecycle 只認這個 Protocol。方法皆可阻塞，coordinator 在受限 worker 上呼叫。
真實 handle 屬 PR-4；PR-2 只用 fake handle 驗收。
"""
from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

ProbeResult = Literal["ok", "degraded", "dead"]


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
