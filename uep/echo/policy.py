"""ES 熱路徑策略的 Core 端實作：不可變快照加原子換入（P3接入方案 §4.4）。

ES 只認 ``PolicySnapshot`` 的機制欄位（version、platform、barge-in 門檻、mode_rules、ttl）；參與者表、
yield_hint 等完整政策留在 Core。讓位判定需要的頻道模式與門檻放在 Core 自己的 ``uep.turn.policy.PolicySnapshot``，
與 ES 快照**同版**推送（同一把鎖、同一個 version）。``current()``、``core_current()`` 同步、不阻塞、
任何執行緒皆可呼叫；熱路徑只讀 frozen 參照，不碰 Core、DB 或模型。

Q-5：快照過期（``ttl_s``）在 coordinator 只讓 barge-in 與讓位退回保守；「不開新輸出」由 Output Gate 與
sink 二次接納負責，不依賴快照時效。S3 預設 ``ttl_s=None``（不過期）。
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from uep.stream.contracts.output import (
    ModeFlags,
    PolicySnapshot,
    TurnView,
    YieldAction,
)
from uep.stream.contracts.turn import VoiceEvent

from uep.turn.decision_config import ListenerConfig
from uep.turn.engagement import ChannelMode
from uep.turn.policy import PolicySnapshot as CorePolicySnapshot
from uep.turn.policy import YieldAccumulator, mode_flags, yieldable

YIELD_REASON = "listener_voice"

logger = logging.getLogger("uep.echo.policy")

CAUSE_PARTICIPANTS = "participants"
CAUSE_CHANNEL_MODE = "channel_mode"
CAUSE_PLATFORM = "platform"
CAUSE_INITIAL = "initial"


@dataclass(frozen=True, slots=True)
class PolicyPublication:
    """一次 ``publish`` 的事實（DL-P1d）：換入當下的版本、前一版與兩份快照的版本／模式，供發布監聽者審計。

    ``cause`` 由 ``publish`` 內部判定：帶 channel_mode 為 channel_mode，平台改變為 platform，其餘為 participants。
    ``cause="initial"`` 是登記監聽者時補送的基準（``add_publish_listener_with_baseline``）：此時 ``prev_version``
    與 ``prev_channel_mode`` 皆為 None，其餘欄位取自當下快照。
    """

    version: int
    prev_version: int | None
    platform: str
    channel_mode: ChannelMode | None
    prev_channel_mode: ChannelMode | None
    cause: str
    ttl_s: float | None
    es_version: int
    core_version: int
    core_channel_mode: ChannelMode | None


PublishListener = Callable[[PolicyPublication], None]


class CoreModeRules:
    """``reply_mode``（不透明字串）-> 機制旗標，依 Core 快照的 ``silent_modes``／``no_barge_in_modes`` 決定。

    預設（與 S3 一致）：passive（D80 逾時預設）不出聲、不可打斷、不取代同 owner 的舊回覆。
    """

    def __init__(self, source: Callable[[], CorePolicySnapshot] | None = None) -> None:
        self._source = source or (lambda: CorePolicySnapshot())

    def flags_for(self, reply_mode: str | None) -> ModeFlags:
        snap = self._source()
        speak, barge_in = mode_flags(snap, reply_mode)
        if not speak or not barge_in:
            return ModeFlags(speak=speak, barge_in=barge_in, supersede_same_owner=False)
        return ModeFlags()


class HostPolicySource:
    def __init__(
        self, platform: str = "web", *, ttl_s: float | None = None, listener: ListenerConfig | None = None,
        barge_in_channel_modes: frozenset[ChannelMode] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._publish_listeners: list[PublishListener] = []
        self._ttl_s = ttl_s
        self._listener = listener or ListenerConfig()
        overrides: dict[str, object] = {}
        if barge_in_channel_modes is not None:
            overrides["barge_in_channel_modes"] = barge_in_channel_modes
        self._core = CorePolicySnapshot.from_listener(
            self._listener, version=1, issued_at=time.perf_counter(), ttl_s=ttl_s, **overrides
        )
        self._snapshot = PolicySnapshot.for_platform(
            platform, version=1, mode_rules=CoreModeRules(self.core_current), ttl_s=ttl_s
        )

    def current(self) -> PolicySnapshot:
        return self._snapshot  # frozen dataclass：換整個參照即原子，讀取免鎖

    def core_current(self) -> CorePolicySnapshot:
        return self._core

    @property
    def version(self) -> int:
        return self._snapshot.version

    def add_publish_listener(self, listener: PublishListener) -> None:
        """登記發布監聽者：快照換入且鎖釋放後呼叫（同步、在發布者的執行緒上），須輕量；例外一律吞掉並記 log。"""
        self._publish_listeners.append(listener)

    def add_publish_listener_with_baseline(self, listener: PublishListener) -> PolicyPublication:
        """在發布鎖內登記監聽者並回傳當下快照的基準 publication（``cause="initial"``）。

        「讀當前快照」與「掛監聽者」同鎖，所以基準之後的每次發布都會通知到 ``listener``、之前的都不會，不漏也不重。
        基準由呼叫者自行送出；listener 在鎖外被呼叫，因此基準與後續發布的先後順序須由呼叫者保證。
        """
        with self._lock:
            self._publish_listeners.append(listener)
            snap, core = self._snapshot, self._core
            return PolicyPublication(
                version=snap.version, prev_version=None, platform=snap.platform,
                channel_mode=core.channel_mode, prev_channel_mode=None, cause=CAUSE_INITIAL, ttl_s=self._ttl_s,
                es_version=snap.version, core_version=core.version, core_channel_mode=core.channel_mode,
            )

    def publish(
        self, *, platform: str | None = None, channel_mode: ChannelMode | None = None
    ) -> PolicySnapshot:
        """推送新版快照（參與者變動、Presence 或頻道模式變化時）；兩份快照同版、issued_at 重新起算。"""
        with self._lock:
            old = self._snapshot
            old_mode = self._core.channel_mode
            version = old.version + 1
            self._core = replace(
                self._core, version=version, issued_at=time.perf_counter(),
                channel_mode=channel_mode if channel_mode is not None else self._core.channel_mode,
            )
            if platform is not None and platform != old.platform:
                fresh = PolicySnapshot.for_platform(
                    platform, version=version, mode_rules=old.mode_rules, ttl_s=self._ttl_s
                )
            else:
                fresh = replace(old, version=version, issued_at=time.perf_counter())
            self._snapshot = fresh
            if channel_mode is not None:
                cause = CAUSE_CHANNEL_MODE
            elif platform is not None and platform != old.platform:
                cause = CAUSE_PLATFORM
            else:
                cause = CAUSE_PARTICIPANTS
            publication = PolicyPublication(
                version=version, prev_version=old.version, platform=fresh.platform,
                channel_mode=self._core.channel_mode, prev_channel_mode=old_mode, cause=cause, ttl_s=self._ttl_s,
                es_version=fresh.version, core_version=self._core.version, core_channel_mode=self._core.channel_mode,
            )
        for listener in tuple(self._publish_listeners):
            try:
                listener(publication)
            except Exception:
                logger.exception("政策發布監聽者失敗")
        return fresh

    def publish_channel_mode(self, mode: ChannelMode) -> PolicySnapshot:
        return self.publish(channel_mode=mode)


class CoreYieldPolicy:
    """ES ``YieldPolicy``：聆聽者讓位（搬自 bot ``BotYield``），只讀 Core 快照。

    主動模式下對方持續出聲達門檻，就讓他「尚未開播」的回覆作廢：有待讓位的回覆（含排隊中）用 0.7s，否則 1.5s；
    觸發後歸零重新累積。回傳 ``YieldAction``（由 coordinator 自己拒絕已開播的輪）。
    快照過期（Q-5）：不累積也不讓位（coordinator 另在快照過期時整個略過本策略）。
    沒有進行中的輪次時 coordinator 不會呼叫策略，由宿主補餵 ``live=[]`` 讓累積狀態不中斷。
    """

    def __init__(
        self, source: HostPolicySource, accumulator: YieldAccumulator | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._source = source
        self._acc = accumulator or YieldAccumulator()
        self._clock = clock

    def on_voiced(self, ev: VoiceEvent, live: Sequence[TurnView]) -> list[YieldAction]:
        speaker = ev.speaker_id
        snap = self._source.core_current()
        if not speaker or not ev.is_voiced or not snap.yield_active or snap.is_stale():
            return []
        seconds = ev.chunk_s if ev.chunk_s > 0 else ev.duration_s
        now = ev.timestamp if ev.timestamp > 0 else self._clock()
        targets = [t for t in live if yieldable(t.owner_id, speaker, t.audio_started, t.behind_playing)]
        if not self._acc.observe(speaker, seconds, now, snap, bool(targets)):
            return []
        return [YieldAction(t.turn_id, YIELD_REASON) for t in targets]

