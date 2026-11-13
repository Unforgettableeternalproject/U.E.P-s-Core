"""M 群模組的唯讀觀測 handle 與描述子預設表（Module Coordinator 設計稿 §4；PR-4）。

M 群 = 本地模型／stage（stt、tts、think、laya、nlp、translate）。v1 只觀測：持有者（wiring）自己載入，coordinator 只讀狀態。
- 本檔只 import ``uep.lifecycle``，不 import 任何子系統或 wiring（遵守 ``tests/uep/test_import_boundary.py``）；
  持有者以 duck typing 傳入（``readiness``／``loaded``／``load_error``／``available``／``cpu_fallback_used``）。
- probe 對應：載入中（尚無結果）-> ``warming``；失敗 -> ``dead``；CPU fallback -> ``degraded``；其餘 ``ok``。
- load／warm／unload 一律 ``ObservationOnlyError``；描述子 ``pinned=True``、``vram_mb=None``（不受 VRAM 預算管理）。
- embedding 本版不登記（尚無可觀測的持有者）。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from uep.lifecycle.descriptor import ModuleDescriptor
from uep.lifecycle.handles import ModuleHandle, ObservationOnlyHandle, ProbeResult

MODULE_STT = "stage:stt"
MODULE_TTS = "stage:tts"
MODULE_THINK = "stage:think"
MODULE_LAYA = "model:laya"
MODULE_NLP = "model:nlp"
MODULE_TRANSLATE = "model:translate"


@dataclass(frozen=True)
class ObservedState:
    """持有者的當下狀態：``ready`` 為 None = 載入中（尚無結果）、False = 失敗、True = 可用；``degraded`` = 可用但降級（例如 CPU fallback）。"""

    ready: bool | None
    degraded: bool = False


class ObservedHolderHandle(ObservationOnlyHandle):
    """只讀 ``read()`` 的觀測 handle。``read`` 在 worker 上呼叫，必須不阻塞（只讀旗標）。"""

    def __init__(self, name: str, read: Callable[[], ObservedState]) -> None:
        self._name = name
        self._read = read

    def __repr__(self) -> str:
        return f"ObservedHolderHandle({self._name})"

    def probe(self) -> ProbeResult:
        state = self._read()
        if state.ready is None:
            return "warming"
        if state.ready is False:
            return "dead"
        return "degraded" if state.degraded else "ok"


# 已知限制：stage prepare 失敗即使 degrade_on_failure 已降級繼續，readiness.ready 仍是 False，所以這裡一律顯示 dead。
def _stage_reader(wiring: Any, key: str) -> Callable[[], ObservedState]:
    def read() -> ObservedState:
        readiness = wiring.readiness.get(key)
        return ObservedState(None if readiness is None else readiness.ready)

    return read


def _loader_reader(holder: Any) -> Callable[[], ObservedState]:
    """Laya／NLP：``loaded`` 為真 = 可用；載入失敗以 ``load_error`` 區分，其餘視為載入中。"""

    def read() -> ObservedState:
        if holder.loaded:
            return ObservedState(True)
        return ObservedState(False if getattr(holder, "load_error", None) else None)

    return read


def _translator_reader(translator: Any) -> Callable[[], ObservedState]:
    def read() -> ObservedState:
        if translator.available:
            return ObservedState(True, degraded=bool(getattr(translator, "cpu_fallback_used", False)))
        return ObservedState(False if getattr(translator, "load_error", None) else None)

    return read


def _descriptor(module_id: str, kind: str) -> ModuleDescriptor:
    return ModuleDescriptor(
        module_id, kind=kind, priority="resident", autoload=True, needs_warm=False, pinned=True, uses_gpu=False, vram_mb=None,
    )  # type: ignore[arg-type]


def build_m_group_registrations(
    *, inference: Any = None, laya: Any = None, nlp: Any = None, translator: Any = None,
) -> list[tuple[ModuleDescriptor, ModuleHandle]]:
    """M 群 ``(描述子, handle)``；只登記有給持有者的項目。``inference`` 是 ``InferenceWiring``（取其 ``readiness`` 的
    input／speak／think），其餘為各自的 proposer／translator。"""
    out: list[tuple[ModuleDescriptor, ModuleHandle]] = []
    if inference is not None:
        for module_id, key in ((MODULE_STT, "input"), (MODULE_TTS, "speak"), (MODULE_THINK, "think")):
            out.append((_descriptor(module_id, "stage"), ObservedHolderHandle(module_id, _stage_reader(inference, key))))
    if laya is not None:
        out.append((_descriptor(MODULE_LAYA, "model"), ObservedHolderHandle(MODULE_LAYA, _loader_reader(laya))))
    if nlp is not None:
        out.append((_descriptor(MODULE_NLP, "model"), ObservedHolderHandle(MODULE_NLP, _loader_reader(nlp))))
    if translator is not None:
        out.append((_descriptor(MODULE_TRANSLATE, "model"), ObservedHolderHandle(MODULE_TRANSLATE, _translator_reader(translator))))
    return out
