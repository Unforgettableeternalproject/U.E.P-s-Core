"""ES 熱路徑策略的 Core 端實作：不可變快照加原子換入（P3接入方案 §4.4）。

ES 只認 ``PolicySnapshot`` 的機制欄位（version、platform、barge-in 門檻、mode_rules、ttl）；參與者表、
yield_hint、讓位門檻等完整政策留在 Core（S5）。``current()`` 同步、不阻塞、任何執行緒皆可呼叫。

Q-5：快照過期（``ttl_s``）在 coordinator 只讓 barge-in 與讓位退回保守；「不開新輸出」由 Output Gate 與
sink 二次接納負責，不依賴快照時效。S3 預設 ``ttl_s=None``（不過期）。
"""
from __future__ import annotations

import threading
import time
from dataclasses import replace

from echo_stream.contracts.output import ModeFlags, PolicySnapshot

from uep.turn.models import ReplyMode


class CoreModeRules:
    """``reply_mode``（不透明字串）-> 機制旗標。S3 只有 respond 與 passive 兩種會進到 ES。"""

    def flags_for(self, reply_mode: str | None) -> ModeFlags:
        if reply_mode == ReplyMode.PASSIVE.value:
            return ModeFlags(speak=False, barge_in=False, supersede_same_owner=False)
        return ModeFlags()


class HostPolicySource:
    def __init__(self, platform: str = "web", *, ttl_s: float | None = None) -> None:
        self._lock = threading.Lock()
        self._ttl_s = ttl_s
        self._snapshot = PolicySnapshot.for_platform(
            platform, version=1, mode_rules=CoreModeRules(), ttl_s=ttl_s
        )

    def current(self) -> PolicySnapshot:
        return self._snapshot  # frozen dataclass：換整個參照即原子，讀取免鎖

    @property
    def version(self) -> int:
        return self._snapshot.version

    def publish(self, *, platform: str | None = None) -> PolicySnapshot:
        """推送新版快照（參與者變動、Presence 變化時）；版本遞增、issued_at 重新起算。"""
        with self._lock:
            old = self._snapshot
            if platform is not None and platform != old.platform:
                fresh = PolicySnapshot.for_platform(
                    platform, version=old.version + 1, mode_rules=old.mode_rules, ttl_s=self._ttl_s
                )
            else:
                fresh = replace(old, version=old.version + 1, issued_at=time.perf_counter())
            self._snapshot = fresh
            return fresh
