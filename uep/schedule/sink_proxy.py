"""決策層 sink 的延遲綁定代理（AR-3b）。

``TurnOrchestrator`` 只在建構時收 ``on_abandon_signal``／``on_presence_request``，而仲裁器是 core 啟動後才組裝。
Host 先建一個 ``PresenceSinkProxy`` 傳給 orchestrator；仲裁器建好之後 ``bind(target)``，關閉時 ``unbind()``。

- 兩個方法與 sink 同簽名：``on_abandon_signal(signal, *, is_owner=None)``、
  ``on_presence_request(proposal, *, request_id, actor_ref, actor_authenticated, is_owner, source_platform)``。
- target 尚未綁定（或已解除）時呼叫會丟棄並計數（``presence_sink.dropped.*``）；轉發失敗只記錄並計數，不外洩給
  orchestrator（它也會吞例外，但這裡的失敗不該被算成回合失敗）。
- thread-safe：bind／unbind 與呼叫可以在不同執行緒；呼叫端只持鎖取 target 參考，轉發在鎖外。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class PresenceSinkProxy:
    def __init__(self, metrics_inc: Callable[[str], None] | None = None) -> None:
        self._metrics_inc = metrics_inc
        self._lock = threading.Lock()
        self._target: Any | None = None
        self.dropped = 0

    def bind(self, target: Any) -> None:
        with self._lock:
            self._target = target

    def unbind(self) -> None:
        with self._lock:
            self._target = None

    @property
    def bound(self) -> bool:
        with self._lock:
            return self._target is not None

    def _count(self, name: str) -> None:
        if self._metrics_inc is not None:
            try:
                self._metrics_inc(name)
            except Exception:
                logger.debug("指標寫入失敗：%s", name, exc_info=True)

    def _target_or_drop(self, kind: str) -> Any | None:
        with self._lock:
            target = self._target
            if target is None:
                self.dropped += 1
        if target is None:
            self._count(f"presence_sink.dropped.{kind}")
        return target

    def on_abandon_signal(self, signal: Any, *, is_owner: bool | None = None) -> None:
        target = self._target_or_drop("abandon_signal")
        if target is None:
            return
        try:
            target.on_abandon_signal(signal, is_owner=is_owner)
        except Exception:
            logger.exception("轉發放棄訊號失敗")
            self._count("presence_sink.error.abandon_signal")

    def on_presence_request(self, proposal: Any, *, request_id: str, actor_ref: str, actor_authenticated: bool,
                            is_owner: bool, source_platform: str) -> None:
        target = self._target_or_drop("presence_request")
        if target is None:
            return
        try:
            target.on_presence_request(
                proposal, request_id=request_id, actor_ref=actor_ref, actor_authenticated=actor_authenticated,
                is_owner=is_owner, source_platform=source_platform)
        except Exception:
            logger.exception("轉發在場請求失敗")
            self._count("presence_sink.error.presence_request")
