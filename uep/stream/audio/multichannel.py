"""多聲道：轉錄池與聲道引擎（平台無關，不 import 子系統或第三方套件）。

單一聲道的狀態機在 :mod:`uep.stream.audio.channel_engine`；這裡負責
「多個說話者 → 共用一個 backend → 整個 session 一條輸出流」的排程與生命週期。
平台特化（取樣率轉換、來源執行緒、backend 載入）由 adapters 處理。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import Any

from ..contracts.cancellation import CancellationToken, CancelledError
from ..contracts.turn import TurnDetector
from ..contracts.types import STT_SAMPLE_RATE, Utterance, new_turn_id
from ..channel import ChannelClosed, StreamChannel
from .channel_engine import Segment, VoiceChannel, decode_stats, merge_parts
from ..turn_detector import SilenceTurnDetector
from .vad import EnergyVad

logger = logging.getLogger(__name__)

_MAX_FILL_S = 2.0
"""單次補入的靜音上限。更長的缺口分多次 tick 補完。"""

_CANCELLED = (asyncio.CancelledError, CancelledError)


class PoolResult:
    """一次轉錄任務的產物與耗時。``queue_ms`` 是等 pool 排程的時間，與 ``stt_ms`` 分開。"""

    __slots__ = ("result", "queue_ms", "stt_ms", "started_ts", "done_ts", "timed_out")

    def __init__(
        self,
        result: Any,
        queue_ms: float = 0.0,
        stt_ms: float = 0.0,
        started_ts: float = 0.0,
        done_ts: float = 0.0,
        timed_out: bool = False,
    ) -> None:
        self.timed_out = timed_out
        self.result = result
        self.queue_ms = queue_ms
        self.stt_ms = stt_ms
        self.started_ts = started_ts
        self.done_ts = done_ts


_TIMED_OUT = object()
"""``TranscriberPool._await_with_timeout`` 的哨兵：該任務已逾時放棄。"""


class _ThreadState:
    """執行緒端與 loop 端共享的完成／孤兒旗標；由 ``TranscriberPool._orphan_lock`` 保護。"""

    __slots__ = ("finished", "orphaned")

    def __init__(self) -> None:
        self.finished = False
        self.orphaned = False


class _Job:
    __slots__ = ("pcm", "sample_rate", "submitted", "future")

    def __init__(
        self, pcm: bytes, sample_rate: int, submitted: float, future: asyncio.Future
    ) -> None:
        self.pcm = pcm
        self.sample_rate = sample_rate
        self.submitted = submitted
        self.future = future


class TranscriberPool:
    """共享單一 backend 的轉錄池：per-channel FIFO + 跨聲道 round-robin。

    * 同一聲道同時最多一個任務在跑，後續任務排在該聲道的佇列——
      同說話者的結果順序不會亂（即使 ``max_concurrency > 1``）。
    * 有待辦任務的聲道排成輪轉環；任務完成後，該聲道若還有待辦就排到環尾，
      所以人多時每人輪流，話多的人不會餓死別人。
    * 同時執行數以 ``max_concurrency`` 限制（預設 1，GPU 不並行）。
    * ``job_timeout_s``（預設 None＝停用）：單一任務的牆鐘逾時。逾時就立刻釋放
      槽位給下一個任務，該任務以空結果（``timed_out=True``）完成、結果作廢。
      Whisper 偶發的解碼重試／重複迴圈可能卡 4–13 秒，不設就會拖住後面所有聲道。
      實際上限 = ``job_timeout_s + job_timeout_per_audio_s × 音訊秒數``，避免把
      真的長段講話（實測有 30 秒、455 字）當成卡死丟掉；建議
      ``job_timeout_s=4.0``、``job_timeout_per_audio_s=0.5``。per_audio 預設
      0.0＝固定上限。轉錄跑在執行緒，Python 無法中止，逾時後那條執行緒仍會
      算完（GPU 仍在跑），此時最多同時 ``max_orphans``（預設 1）個「孤兒」；
      孤兒額滿時，再逾時的任務不放棄，照常等到完成，避免 GPU 上無限堆疊。
    * backend 若有 ``transcribe_for(speaker_id, pcm, sr)`` 就呼叫它
      （讓 backend 做逐聲道詞表等），否則用 ``transcribe(pcm, sr)``。
    """

    def __init__(
        self,
        backend: Any,
        *,
        max_concurrency: int = 1,
        job_timeout_s: float | None = None,
        job_timeout_per_audio_s: float = 0.0,
        max_orphans: int = 1,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency 必須 >= 1")
        if job_timeout_s is not None and job_timeout_s <= 0:
            raise ValueError("job_timeout_s 必須 > 0 或為 None")
        if job_timeout_per_audio_s < 0:
            raise ValueError("job_timeout_per_audio_s 必須 >= 0")
        if max_orphans < 0:
            raise ValueError("max_orphans 必須 >= 0")
        self.backend = backend
        self.max_concurrency = max_concurrency
        self.job_timeout_s = job_timeout_s
        self.job_timeout_per_audio_s = job_timeout_per_audio_s
        self.max_orphans = max_orphans
        self.timeouts = 0
        self._orphans = 0
        self._orphan_lock = threading.Lock()
        self._clock = clock
        self._queues: dict[str, deque[_Job]] = {}
        self._ring: deque[str] = deque()
        self._running: set[str] = set()
        self._active = 0
        self.peak_active = 0
        self._tasks: set[asyncio.Task] = set()
        self._closed = False

    async def run(self, speaker_id: str, pcm: bytes, sample_rate: int) -> PoolResult:
        """送出一個轉錄任務並等待結果。"""
        if self._closed:
            raise asyncio.CancelledError
        loop = asyncio.get_running_loop()
        job = _Job(pcm, sample_rate, self._clock(), loop.create_future())
        self._queues.setdefault(speaker_id, deque()).append(job)
        if speaker_id not in self._running and speaker_id not in self._ring:
            self._ring.append(speaker_id)
        self._dispatch()
        return await job.future

    def discard(self, speaker_id: str) -> None:
        """丟掉該聲道尚未開始的任務（以空結果完成，呼叫端的世代檢查會作廢）。"""
        queue = self._queues.get(speaker_id)
        if not queue:
            return
        while queue:
            job = queue.popleft()
            if not job.future.done():
                job.future.set_result(PoolResult(None))
        if speaker_id in self._ring:
            self._ring.remove(speaker_id)
        if speaker_id not in self._running:
            self._queues.pop(speaker_id, None)

    def close(self) -> None:
        """取消執行中與排隊中的任務。"""
        self._closed = True
        for queue in self._queues.values():
            for job in queue:
                if not job.future.done():
                    job.future.cancel()
        self._queues.clear()
        self._ring.clear()
        for task in list(self._tasks):
            task.cancel()

    def _dispatch(self) -> None:
        while not self._closed and self._active < self.max_concurrency and self._ring:
            speaker_id = self._ring.popleft()
            queue = self._queues.get(speaker_id)
            if not queue:
                self._queues.pop(speaker_id, None)
                continue
            job = queue.popleft()
            if job.future.done():  # 等待端已被取消
                if queue:
                    self._ring.append(speaker_id)
                else:
                    del self._queues[speaker_id]
                continue
            self._running.add(speaker_id)
            self._active += 1
            self.peak_active = max(self.peak_active, self._active)
            task = asyncio.ensure_future(self._execute(speaker_id, job))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def _invoke(self, speaker_id: str, pcm: bytes, sample_rate: int) -> Any:
        per_channel = getattr(self.backend, "transcribe_for", None)
        if callable(per_channel):
            return per_channel(speaker_id, pcm, sample_rate)
        return self.backend.transcribe(pcm, sample_rate)

    def _invoke_tracked(
        self, state: _ThreadState, speaker_id: str, pcm: bytes, sample_rate: int
    ) -> Any:
        """在執行緒內呼叫 backend，結束時（無論 loop 是否還活著）歸還孤兒額度。"""
        try:
            return self._invoke(speaker_id, pcm, sample_rate)
        finally:
            with self._orphan_lock:
                state.finished = True
                if state.orphaned:
                    self._orphans -= 1

    async def _await_with_timeout(
        self,
        speaker_id: str,
        job: _Job,
        fut: asyncio.Future,
        state: _ThreadState,
        started: float,
    ) -> Any:
        """等執行緒結果；逾時且孤兒額度有空就放棄它（回傳 ``_TIMED_OUT``）。"""
        audio_s = len(job.pcm) / 2 / max(1, job.sample_rate)
        limit = (self.job_timeout_s or 0.0) + self.job_timeout_per_audio_s * audio_s
        done, _ = await asyncio.wait({fut}, timeout=limit)
        if done:
            return fut.result()
        with self._orphan_lock:
            # 剛好在此刻完成、或孤兒額滿：不放棄，照常等
            if state.finished or self._orphans >= self.max_orphans:
                abandon = False
            else:
                abandon = True
                state.orphaned = True
                self._orphans += 1
        if not abandon:
            return await fut
        self.timeouts += 1
        fut.add_done_callback(self._swallow_orphan)
        elapsed = self._clock() - started
        logger.warning(
            "轉錄逾時：speaker=%s 音訊=%.2fs 已耗時=%.2fs（上限 %.2fs），結果作廢並釋放槽位",
            speaker_id,
            audio_s,
            elapsed,
            limit,
        )
        if not job.future.done():
            job.future.set_result(
                PoolResult(
                    None,
                    queue_ms=max(0.0, (started - job.submitted) * 1000.0),
                    stt_ms=max(0.0, elapsed * 1000.0),
                    started_ts=started,
                    done_ts=started + elapsed,
                    timed_out=True,
                )
            )
        return _TIMED_OUT

    @staticmethod
    def _swallow_orphan(fut: asyncio.Future) -> None:
        """吞掉孤兒的結果／例外（不外洩、不產生未取回警告）。額度由執行緒端歸還，
        所以 loop 先關閉、callback 永遠不會被呼叫也不影響計數。"""
        if not fut.cancelled():
            fut.exception()

    async def _execute(self, speaker_id: str, job: _Job) -> None:
        loop = asyncio.get_running_loop()
        started = self._clock()
        try:
            if self.job_timeout_s is None:
                result = await loop.run_in_executor(
                    None, self._invoke, speaker_id, job.pcm, job.sample_rate
                )
            else:
                state = _ThreadState()
                fut = loop.run_in_executor(
                    None, self._invoke_tracked, state, speaker_id, job.pcm, job.sample_rate
                )
                result = await self._await_with_timeout(speaker_id, job, fut, state, started)
                if result is _TIMED_OUT:
                    return
        except asyncio.CancelledError:
            if not job.future.done():
                job.future.cancel()
            raise
        except Exception as exc:  # noqa: BLE001 - 交給等待端決定如何處理
            if not job.future.done():
                job.future.set_exception(exc)
        else:
            done = self._clock()
            if not job.future.done():
                job.future.set_result(
                    PoolResult(
                        result,
                        queue_ms=max(0.0, (started - job.submitted) * 1000.0),
                        stt_ms=max(0.0, (done - started) * 1000.0),
                        started_ts=started,
                        done_ts=done,
                    )
                )
        finally:
            self._active -= 1
            self._running.discard(speaker_id)
            queue = self._queues.get(speaker_id)
            if queue:
                self._ring.append(speaker_id)
            else:
                self._queues.pop(speaker_id, None)
            self._dispatch()


_CHUNK, _SILENCE, _FLUSH, _CLOSE = range(4)
"""聲道輸入佇列的項目種類。控制項（flush / close）與音訊走同一條佇列以保證順序。"""


class _ChannelCfg:
    """單一聲道的設定視圖：policy 可個別覆寫，其餘即時委派給引擎的 cfg。"""

    __slots__ = ("_parent", "_policy")

    def __init__(self, parent: Any, policy: Any = None) -> None:
        self._parent = parent
        self._policy = policy

    @property
    def policy(self) -> Any:
        return self._policy if self._policy is not None else self._parent.policy

    @property
    def preroll_s(self) -> float:
        return self._parent.preroll_s

    @property
    def partial_silence_s(self) -> float:
        return self._parent.partial_silence_s

    @property
    def partial_min_speech_s(self) -> float:
        return self._parent.partial_min_speech_s

    @property
    def on_partial(self) -> Any:
        return self._parent.on_partial

    @property
    def on_voice_event(self) -> Any:
        return self._parent.on_voice_event


class _ChState:
    """引擎內一個聲道的執行期狀態。"""

    __slots__ = (
        "speaker_id",
        "core",
        "items",
        "wake",
        "segments",
        "fed_until",
        "last_feed",
        "busy",
        "parts",
        "parts_gen",
        "tasks",
    )

    def __init__(self, speaker_id: str, core: VoiceChannel, now: float, max_pending: int):
        self.speaker_id = speaker_id
        self.core = core
        self.items: deque[tuple[int, bytes, float]] = deque()
        self.wake = asyncio.Event()
        self.segments: asyncio.Queue[Segment | None] = asyncio.Queue(maxsize=max_pending)
        self.fed_until: float | None = None
        """音訊時間軸已涵蓋到的牆鐘時刻（真實 chunk + 補入的靜音）。"""
        self.last_feed = now
        self.busy = False
        self.parts: list[Any] = []
        self.parts_gen = -1
        self.tasks: list[asyncio.Task] = []


class ChannelEngine:
    """多聲道引擎：每個說話者一條聲道，共用轉錄池，整個 session 一條輸出流。

    ``cfg`` 是設定來源（duck typing，每次現讀）：``policy`` / ``preroll_s`` /
    ``partial_silence_s`` / ``partial_min_speech_s`` /
    ``on_partial(speaker_id, text, index)`` / ``on_voice_event`` / ``min_confidence``。

    ## 執行緒模型

    ``feed`` / ``join`` / ``leave`` / ``discard`` 可從任何執行緒呼叫：
    非 loop 執行緒經 ``call_soon_threadsafe`` 進 loop，在 loop 執行緒呼叫則直接執行。
    所有聲道狀態只在 loop 執行緒被改動，不需要鎖。

    ## 無封包即靜音

    Discord 在使用者不說話時不送封包——turn detector 因此收不到靜音、
    turn 永遠結束不了。每個聲道追蹤「音訊時間軸涵蓋到的牆鐘時刻」
    （``fed_until``），背景 tick 發現 ``now - fed_until`` 超過補靜音門檻
    （``silence_fill_s``，約一個 chunk）時，就把等長的靜音 chunk 塞進該聲道的
    輸入佇列（與真實 chunk 同序），VAD 與 turn detector 照常前進。真實 chunk
    抵達時若發現與時間軸之間有缺口也會先補靜音。沒有進行中語音的聲道不補。
    """

    def __init__(
        self,
        cfg: Any,
        *,
        backend: Any = None,
        sample_rate: int = STT_SAMPLE_RATE,
        max_pending_segments: int = 4,
        queue_size: int = 8,
        chunk_queue_max: int = 256,
        idle_evict_s: float = 60.0,
        max_channels: int = 32,
        max_concurrency: int = 1,
        job_timeout_s: float | None = None,
        job_timeout_per_audio_s: float = 0.0,
        tick_interval_s: float = 0.1,
        silence_fill_s: float = 0.1,
        clock: Callable[[], float] = time.perf_counter,
        vad_factory: Callable[[], EnergyVad] | None = None,
        detector_factory: Callable[[Any], TurnDetector] | None = None,
    ) -> None:
        self.cfg = cfg
        self.backend = backend
        self.sample_rate = sample_rate
        self.max_pending_segments = max_pending_segments
        self.queue_size = queue_size
        self.chunk_queue_max = chunk_queue_max
        self.idle_evict_s = idle_evict_s
        self.max_channels = max_channels
        self.max_concurrency = max_concurrency
        self.job_timeout_s = job_timeout_s
        self.job_timeout_per_audio_s = job_timeout_per_audio_s
        self.tick_interval_s = tick_interval_s
        self.silence_fill_s = silence_fill_s
        self._clock = clock
        self._vad_factory = vad_factory or (lambda: EnergyVad(sample_rate))
        self._detector_factory = detector_factory or (lambda policy: SilenceTurnDetector(policy))

        self.stats: dict[str, int] = {
            "chunks_dropped": 0,
            "feed_not_open": 0,
            "channels_rejected": 0,
            "channels_evicted": 0,
            "stt_errors": 0,
            "stt_timeouts": 0,
        }
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = True
        self._channels: dict[str, _ChState] = {}
        self._draining: list[_ChState] = []
        self._pending_joins: dict[str, dict[str, Any]] = {}
        self._pool: TranscriberPool | None = None
        self._out: StreamChannel[Utterance] | None = None
        self._ticker: asyncio.Task | None = None
        self._shutdown_task: asyncio.Task | None = None
        self._rejected_logged: set[str] = set()

    @property
    def channel_ids(self) -> list[str]:
        return list(self._channels)

    @property
    def pool(self) -> TranscriberPool | None:
        return self._pool

    # --- 生命週期 ---

    async def open(self, token: CancellationToken) -> AsyncIterator[Utterance]:
        """開啟 session，產出整個 session 唯一的 Utterance 流。"""
        if self._loop is not None:
            raise RuntimeError("ChannelEngine 已開啟")
        if self.backend is None:
            raise RuntimeError("ChannelEngine 需要 backend（宿主預載後注入）")
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._closed = False
        self._shutdown_task = None
        out: StreamChannel[Utterance] = StreamChannel(maxsize=self.queue_size, token=token)
        self._out = out
        self._pool = TranscriberPool(
            self.backend,
            max_concurrency=self.max_concurrency,
            job_timeout_s=self.job_timeout_s,
            job_timeout_per_audio_s=self.job_timeout_per_audio_s,
            clock=self._clock,
        )
        self._ticker = asyncio.ensure_future(self._tick_loop())

        def _on_cancel(_reason: Any) -> None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._request_shutdown)

        token.on_cancel(_on_cancel)
        for speaker_id, opts in list(self._pending_joins.items()):
            self._join_now(speaker_id, opts)
        self._pending_joins.clear()

        try:
            async for utterance in out:
                yield utterance
        except CancelledError:
            return
        finally:
            await self.shutdown()

    def _request_shutdown(self) -> None:
        if self._loop is not None and self._shutdown_task is None:
            self._shutdown_task = asyncio.ensure_future(self._do_shutdown())

    async def shutdown(self) -> None:
        """收尾：取消所有背景 task、關閉輸出流。冪等。"""
        if self._loop is None:
            return
        self._request_shutdown()
        task = self._shutdown_task
        if task is not None:
            await asyncio.shield(task)

    async def _do_shutdown(self) -> None:
        self._closed = True
        tasks: list[asyncio.Task] = []
        if self._ticker is not None:
            tasks.append(self._ticker)
        for st in [*self._channels.values(), *self._draining]:
            tasks.extend(st.tasks)
        if self._pool is not None:
            self._pool.close()
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task
        if self._out is not None:
            self._out.close()
        self._channels.clear()
        self._draining.clear()
        self._ticker = None
        self._pool = None
        self._loop = None

    # --- 外部入口（任何執行緒） ---

    def _call_loop(self, fn: Callable[..., None], *args: Any) -> bool:
        loop = self._loop
        if loop is None or self._closed:
            return False
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            fn(*args)
            return True
        try:
            loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:  # loop 已關閉
            return False
        return True

    def feed(self, speaker_id: str, pcm: bytes, ts: float | None = None) -> None:
        """餵入 16kHz mono int16 PCM。thread-safe。"""
        if not pcm:
            return
        if len(pcm) % 2:
            pcm = pcm[:-1]
        if ts is None:
            ts = self._clock()
        if not self._call_loop(self._enqueue, speaker_id, pcm, ts):
            self.stats["feed_not_open"] += 1

    def join(self, speaker_id: str, **opts: Any) -> None:
        """預先建立聲道。opts 可帶 ``policy`` / ``vad`` / ``turn_detector``。"""
        if self._loop is None:
            self._pending_joins[speaker_id] = opts
            return
        self._call_loop(self._join_now, speaker_id, opts)

    def leave(self, speaker_id: str, flush: bool = True) -> None:
        """移除聲道。``flush=True`` 先把已收的音訊處理完並送出殘餘段。"""
        if self._loop is None:
            self._pending_joins.pop(speaker_id, None)
            return
        self._call_loop(self._leave_now, speaker_id, flush)

    def discard(self, speaker_id: str | None = None) -> None:
        """丟棄進行中的輸入（累積音訊、排隊與轉錄中的段）；None 表示全部聲道。"""
        self._call_loop(self._discard_now, speaker_id)

    # --- loop 執行緒內部 ---

    def _join_now(self, speaker_id: str, opts: dict[str, Any]) -> _ChState | None:
        st = self._channels.get(speaker_id)
        if st is not None:
            return st
        if len(self._channels) >= self.max_channels:
            self.stats["channels_rejected"] += 1
            if speaker_id not in self._rejected_logged:
                self._rejected_logged.add(speaker_id)
                logger.warning("聲道數已達上限 %d，拒絕 %s", self.max_channels, speaker_id)
            return None
        cfg = _ChannelCfg(self.cfg, opts.get("policy"))
        vad = opts.get("vad") or self._vad_factory()
        if vad.sample_rate != self.sample_rate:
            raise ValueError(f"vad 取樣率 {vad.sample_rate} 與引擎 {self.sample_rate} 不符")
        detector = opts.get("turn_detector") or self._detector_factory(cfg.policy)
        core = VoiceChannel(vad, detector, cfg, speaker_id=speaker_id)
        st = _ChState(speaker_id, core, self._clock(), self.max_pending_segments)
        st.tasks = [
            asyncio.ensure_future(self._pump(st)),
            asyncio.ensure_future(self._worker(st)),
        ]
        self._channels[speaker_id] = st
        return st

    def _push_item(self, st: _ChState, kind: int, pcm: bytes, ts: float) -> None:
        if len(st.items) >= self.chunk_queue_max:
            # 滿了丟最舊的音訊（控制項不丟）
            for i, item in enumerate(st.items):
                if item[0] in (_CHUNK, _SILENCE):
                    del st.items[i]
                    self.stats["chunks_dropped"] += 1
                    break
        st.items.append((kind, pcm, ts))
        st.wake.set()

    def _enqueue(self, speaker_id: str, pcm: bytes, ts: float) -> None:
        st = self._channels.get(speaker_id)
        if st is None:
            st = self._join_now(speaker_id, {})
            if st is None:
                return
        st.last_feed = self._clock()
        if st.fed_until is None:
            st.fed_until = ts
        elif ts - st.fed_until >= self.silence_fill_s and (st.core.active or st.items):
            # 封包之間有缺口（對方沒說話時不送封包）：先補等長靜音
            self._fill_silence(st, ts)
        elif ts > st.fed_until:
            st.fed_until = ts
        self._push_item(st, _CHUNK, pcm, ts)
        st.fed_until += (len(pcm) // 2) / self.sample_rate

    def _fill_silence(self, st: _ChState, until: float) -> None:
        assert st.fed_until is not None
        gap = until - st.fed_until
        step = min(gap, _MAX_FILL_S)
        n = int(step * self.sample_rate)
        if n > 0:
            self._push_item(st, _SILENCE, b"\x00\x00" * n, st.fed_until + step)
        st.fed_until = until if gap <= _MAX_FILL_S else st.fed_until + step

    def _tick(self, now: float) -> None:
        """補靜音與閒置回收。獨立成同步方法，測試可直接以假時鐘驅動。"""
        for st in list(self._channels.values()):
            if st.items or st.fed_until is None:
                continue  # 還有未處理的輸入，時間軸以它們為準
            if st.core.active:
                if now - st.fed_until >= self.silence_fill_s:
                    self._fill_silence(st, now)
                continue
            st.fed_until = max(st.fed_until, now)
            if (
                now - st.last_feed >= self.idle_evict_s
                and st.segments.empty()
                and not st.busy
                and not st.parts
            ):
                self.stats["channels_evicted"] += 1
                self._close_channel(st, flush=False)

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(self.tick_interval_s)
            try:
                self._tick(self._clock())
            except Exception:  # noqa: BLE001 - tick 壞掉不能讓整個引擎停擺
                logger.exception("聲道 tick 失敗")

    def _close_channel(self, st: _ChState, flush: bool) -> None:
        if self._channels.get(st.speaker_id) is st:
            del self._channels[st.speaker_id]
        if flush:
            self._push_item(st, _FLUSH, b"", self._clock())
        else:
            self._discard_state(st)
        self._push_item(st, _CLOSE, b"", self._clock())
        self._draining.append(st)

    def _leave_now(self, speaker_id: str, flush: bool) -> None:
        st = self._channels.get(speaker_id)
        if st is not None:
            self._close_channel(st, flush)

    def _discard_state(self, st: _ChState) -> None:
        st.items = deque(item for item in st.items if item[0] == _CLOSE)
        st.core.discard()
        st.parts = []
        while True:
            try:
                item = st.segments.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is None:  # 結束哨兵不能吞——放回去
                with contextlib.suppress(asyncio.QueueFull):
                    st.segments.put_nowait(None)
                break
        if self._pool is not None:
            self._pool.discard(st.speaker_id)

    def _discard_now(self, speaker_id: str | None) -> None:
        for st in [*self._channels.values(), *self._draining]:
            if speaker_id is None or st.speaker_id == speaker_id:
                self._discard_state(st)

    # --- 背景 task ---

    async def _pump(self, st: _ChState) -> None:
        """依序處理輸入項：音訊 → 狀態機 → 待轉錄段（背壓回到輸入佇列，滿了丟最舊）。"""
        try:
            while True:
                while not st.items:
                    st.wake.clear()
                    await st.wake.wait()
                kind, pcm, ts = st.items.popleft()
                if kind == _CLOSE:
                    await st.segments.put(None)
                    return
                if kind == _FLUSH:
                    tail = st.core.flush(ts)
                    segments = [tail] if tail is not None else []
                else:
                    segments = st.core.push(pcm, ts)
                for segment in segments:
                    await st.segments.put(segment)
        except _CANCELLED:
            return
        except Exception:  # noqa: BLE001
            logger.exception("聲道 %s pump 失敗", st.speaker_id)

    async def _worker(self, st: _ChState) -> None:
        """依序轉錄該聲道的段，turn 結束時拼成一則 Utterance 進輸出流。"""
        cfg = self.cfg
        out = self._out
        pool = self._pool
        assert out is not None and pool is not None
        sid = st.speaker_id

        def _passes(result: Any) -> bool:
            return (
                result is not None
                and bool(result.text.strip())
                and result.confidence >= cfg.min_confidence
            )

        try:
            while True:
                segment = await st.segments.get()
                if segment is None:
                    break
                st.busy = True
                try:
                    if segment.gen != st.core.gen:
                        continue
                    if segment.gen != st.parts_gen:
                        st.parts = []
                        st.parts_gen = segment.gen
                    job: PoolResult | None = None
                    if segment.pcm:
                        try:
                            job = await pool.run(sid, segment.pcm, self.sample_rate)
                        except _CANCELLED:
                            raise
                        except Exception:  # noqa: BLE001 - 單一聲道轉錄失敗不拖垮 session
                            self.stats["stt_errors"] += 1
                            logger.exception("聲道 %s 轉錄失敗", sid)
                    if job is not None and job.timed_out:
                        self.stats["stt_timeouts"] += 1
                    if segment.gen != st.core.gen:
                        continue
                    result = job.result if job is not None else None
                    if _passes(result):
                        st.parts.append(result)
                        callback = cfg.on_partial
                        if callback is not None:
                            try:
                                callback(sid, result.text.strip(), len(st.parts) - 1)
                            except Exception:  # noqa: BLE001 - 訂閱者壞掉不能斷轉錄
                                pass
                    if not segment.final or not st.parts:
                        continue
                    text, confidence, language = merge_parts(st.parts)
                    stats = decode_stats(st.parts)
                    n_parts = len(st.parts)
                    st.parts = []
                    done_ts = job.done_ts if job is not None else self._clock()
                    await out.put(
                        Utterance(
                            text=text,
                            turn_id=new_turn_id(),
                            speaker_id=sid,
                            is_final=True,
                            confidence=confidence,
                            language=language,
                            started_at=segment.started_at,
                            ended_at=segment.ended_at,
                            metadata={
                                "speech_start_ts": segment.started_at,
                                "speech_end_ts": segment.ended_at,
                                "turn_end_ts": segment.turn_end_ts,
                                "stt_done_ts": done_ts,
                                "stt_ms": job.stt_ms if job is not None else 0.0,
                                "queue_ms": job.queue_ms if job is not None else 0.0,
                                "channel_segments": n_parts,
                                **stats,
                            },
                        )
                    )
                finally:
                    st.busy = False
        except (*_CANCELLED, ChannelClosed):
            pass
        except Exception as exc:  # noqa: BLE001 - 傳給消費端而不是消失在 task 裡
            out.fail(exc)
        finally:
            if st in self._draining:
                self._draining.remove(st)
