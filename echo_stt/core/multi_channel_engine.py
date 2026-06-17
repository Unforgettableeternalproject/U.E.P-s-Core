"""
MultiChannelSTTEngine — 多通道 STT 系統統一入口

將 ChannelManager / STTWorkerPool / TranscriptAggregator 串聯為
完整的多通道語音辨識管線。

架構：
    [音訊來源] → [ChannelManager] → [TranscriptionQueue]
                                        ↓
                                  [STTWorkerPool]  ← 共用 WhisperModel
                                        ↓
                                [TranscriptAggregator] → on_entry callback

生命週期：
    CREATED → initialize() → INITIALIZED → start() → RUNNING
    RUNNING → stop() → STOPPED → start() (重啟) 或 shutdown() → SHUTDOWN
"""

from __future__ import annotations

import logging
import time
from enum import Enum
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple, Union

import numpy as np

from echo_stt.core.config import load_config, get
from echo_stt.core.multi_channel_schemas import (
    AudioSegment,
    ChannelStatus,
    ChannelType,
    EngineMetrics,
    QueueMetrics,
    TranscriptEntry,
    TranscriptSession,
)
from echo_stt.channel.manager import ChannelManager
from echo_stt.output.aggregator import TranscriptAggregator
from echo_stt.pipeline.worker import STTWorkerPool
from echo_stt.vad.noise_filter import NoiseFilter

logger = logging.getLogger(__name__)


class EngineState(str, Enum):
    """引擎狀態"""
    CREATED = "created"
    INITIALIZED = "initialized"
    RUNNING = "running"
    STOPPED = "stopped"
    SHUTDOWN = "shutdown"


class MultiChannelSTTEngine:
    """多通道 STT 系統統一入口。

    Usage:
        # 手動生命週期
        engine = MultiChannelSTTEngine(config=cfg, on_entry=print_entry)
        engine.initialize()
        engine.start()
        engine.add_local_mic("mic_0")
        ...
        engine.shutdown()

        # Context manager
        with MultiChannelSTTEngine(config=cfg) as engine:
            engine.add_local_mic("mic_0")
            time.sleep(10)
    """

    def __init__(
        self,
        config: Optional[dict] = None,
        on_entry: Optional[Callable[[TranscriptEntry], None]] = None,
        on_session_end: Optional[Callable[[TranscriptSession], None]] = None,
        speaker_module: Optional[Any] = None,
    ):
        """
        Args:
            config: 完整設定 dict（None 時自動 load_config()）
            on_entry: 每次辨識結果的即時 callback
            on_session_end: session 結束時的 callback
            speaker_module: SpeakerIdentification 實例（可選）
        """
        self.config = config if config is not None else load_config()
        self.on_entry = on_entry
        self.on_session_end = on_session_end
        self.speaker_module = speaker_module

        # 讀取 multi_channel 配置
        mc = self.config.get("multi_channel", {})
        self._enabled: bool = mc.get("enabled", False)
        self._num_workers: int = mc.get("num_workers", 2)
        self._queue_max_size: int = mc.get("queue_max_size", 50)
        self._max_channels: int = mc.get("max_channels", 8)
        self._drain_timeout: float = mc.get("drain_timeout_s", 5.0)
        self._direct_timeout: float = mc.get("direct_transcribe_timeout_s", 10.0)
        self._default_language: Optional[str] = mc.get("default_language", None)
        self._noise_filter_enabled: bool = mc.get("noise_filter_enabled", False)
        self._vad_config: dict = mc.get("vad_config", {})

        # 子元件（initialize() 時建立）
        self._stt_engine: Optional[Any] = None  # STTEngine (lazy import)
        self._worker_pool: Optional[STTWorkerPool] = None
        self._channel_manager: Optional[ChannelManager] = None
        self._aggregator: Optional[TranscriptAggregator] = None

        # 狀態
        self._state = EngineState.CREATED
        self._started_at: Optional[float] = None
        self._total_channels_ever: int = 0

        logger.info(
            "[MCEngine] 建立完成 (enabled=%s, workers=%d, queue=%d)",
            self._enabled, self._num_workers, self._queue_max_size,
        )

    # ──────────────────────────────────────────────────────────
    #  生命週期
    # ──────────────────────────────────────────────────────────

    def initialize(self) -> bool:
        """載入模型並建立所有子元件。

        Returns:
            True 若初始化成功，False 若 multi_channel.enabled=false 或失敗。
        """
        # Idempotent: 已初始化過就跳過
        if self._state in (
            EngineState.INITIALIZED,
            EngineState.RUNNING,
            EngineState.STOPPED,
        ):
            logger.debug("[MCEngine] 已初始化，跳過")
            return True

        if self._state == EngineState.SHUTDOWN:
            logger.error("[MCEngine] 引擎已 shutdown，無法重新初始化")
            return False

        if not self._enabled:
            logger.warning(
                "[MCEngine] multi_channel.enabled=false，跳過初始化。"
                "若要使用多通道功能，請在 stt_config.yaml 設定 multi_channel.enabled: true"
            )
            return False

        try:
            # 1. 建立並初始化 STTEngine（載入 WhisperModel）
            #    若 _stt_engine 已注入（測試用），跳過建立
            if self._stt_engine is None:
                from echo_stt.core.engine import STTEngine
                self._stt_engine = STTEngine(config=self.config)

            init_ok = self._stt_engine.initialize()
            if not init_ok:
                logger.error("[MCEngine] STTEngine 初始化失敗")
                return False

            # 2. 偵測後端——transformers 非執行緒安全，強制 1 worker
            backend = get("whisper.backend", "faster-whisper", config=self.config)
            effective_workers = self._num_workers
            if backend != "faster-whisper" and effective_workers > 1:
                logger.warning(
                    "[MCEngine] %s 後端不支援多 worker，自動降為 1",
                    backend,
                )
                effective_workers = 1

            # 3. 建立降噪濾波器（可選）
            noise_filter: Optional[NoiseFilter] = None
            if self._noise_filter_enabled:
                noise_filter = NoiseFilter()

            # 4. 建立 TranscriptAggregator
            self._aggregator = TranscriptAggregator(
                on_entry=self._on_entry_internal,
                speaker_module=self.speaker_module,
            )

            # 5. 建立 STTWorkerPool
            #    result_callback 直接接 aggregator.add_entry
            #    簽名匹配: (entry, segment) -> None
            self._worker_pool = STTWorkerPool(
                transcribe_fn=self._stt_engine._transcribe,
                result_callback=self._aggregator.add_entry,
                num_workers=effective_workers,
                max_queue_size=self._queue_max_size,
                noise_filter=noise_filter,
                default_language=self._default_language,
            )

            # 6. 建立 ChannelManager
            #    segment_callback 直接接 worker_pool.submit
            self._channel_manager = ChannelManager(
                segment_callback=self._worker_pool.submit,
                vad_config=self._vad_config,
                max_channels=self._max_channels,
            )

            self._state = EngineState.INITIALIZED
            logger.info(
                "[MCEngine] 初始化完成 (backend=%s, workers=%d, device=%s)",
                backend, effective_workers, self._stt_engine.device,
            )
            return True

        except Exception as e:
            logger.error("[MCEngine] 初始化失敗: %s", e, exc_info=True)
            return False

    def start(self) -> None:
        """啟動引擎（開始處理音訊）。"""
        if self._state == EngineState.SHUTDOWN:
            raise RuntimeError("Engine has been shut down and cannot be restarted")
        if self._state not in (EngineState.INITIALIZED, EngineState.STOPPED):
            raise RuntimeError(
                f"Engine not initialized (state={self._state.value}). "
                "Call initialize() first."
            )

        self._started_at = time.time()

        # 啟動 worker pool
        self._worker_pool.start()

        # 啟動所有已註冊的 channel
        self._channel_manager.start_all()

        # 建立 session（使用啟動時間作為 base）
        self._aggregator.start_session(started_at=self._started_at)

        self._state = EngineState.RUNNING
        logger.info("[MCEngine] 引擎已啟動")

    def stop(self) -> None:
        """停止音訊採集和辨識（session 保留可讀取）。"""
        if self._state != EngineState.RUNNING:
            logger.debug("[MCEngine] 非 RUNNING 狀態，跳過 stop")
            return

        # 停止音訊輸入
        self._channel_manager.stop_all()

        # 等待佇列排空
        self._worker_pool.drain(timeout=self._drain_timeout)

        # 停止 worker
        self._worker_pool.stop()

        self._state = EngineState.STOPPED
        logger.info("[MCEngine] 引擎已停止")

    def shutdown(self) -> None:
        """完全關閉並釋放所有資源。"""
        if self._state == EngineState.SHUTDOWN:
            return

        # 若仍在 running，先 stop
        if self._state == EngineState.RUNNING:
            self.stop()

        # 關閉 channel manager
        if self._channel_manager is not None:
            self._channel_manager.shutdown()

        # 結束 session
        if self._aggregator is not None:
            session = self._aggregator.end_session()
            if session is not None and self.on_session_end is not None:
                try:
                    self.on_session_end(session)
                except Exception as e:
                    logger.error("[MCEngine] on_session_end callback 失敗: %s", e)

        # 釋放 STTEngine 資源
        if self._stt_engine is not None and hasattr(self._stt_engine, "shutdown"):
            try:
                self._stt_engine.shutdown()
            except Exception as e:
                logger.debug("[MCEngine] STTEngine shutdown: %s", e)

        self._state = EngineState.SHUTDOWN
        logger.info("[MCEngine] 引擎已關閉")

    # ──────────────────────────────────────────────────────────
    #  Context Manager
    # ──────────────────────────────────────────────────────────

    def __enter__(self) -> "MultiChannelSTTEngine":
        if not self.initialize():
            raise RuntimeError(
                "MultiChannelSTTEngine initialization failed. "
                "Check multi_channel.enabled and model availability."
            )
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.shutdown()
        return False  # 不抑制例外

    # ──────────────────────────────────────────────────────────
    #  通道管理
    # ──────────────────────────────────────────────────────────

    def add_local_mic(
        self,
        channel_id: str,
        device_index: Optional[int] = None,
        speaker_label: Optional[str] = None,
        speaker_name: Optional[str] = None,
        chunk_duration_ms: int = 50,
        auto_start: bool = True,
    ) -> str:
        """新增本地麥克風通道。

        Args:
            channel_id: 通道 ID
            device_index: 麥克風裝置索引（None = 系統預設）
            speaker_label: 預設說話人標籤
            speaker_name: 預設說話人名稱
            chunk_duration_ms: 音訊 chunk 大小（毫秒）
            auto_start: 是否立即啟動

        Returns:
            channel_id
        """
        self._require_initialized()
        cid = self._channel_manager.add_local_mic(
            channel_id=channel_id,
            device_index=device_index,
            speaker_label=speaker_label,
            speaker_name=speaker_name,
            chunk_duration_ms=chunk_duration_ms,
        )
        self._total_channels_ever += 1

        if auto_start and self._state == EngineState.RUNNING:
            self._channel_manager.start_channel(channel_id)

        return cid

    def add_file_channel(
        self,
        channel_id: str,
        audio_data: np.ndarray,
        sample_rate: int = 16000,
        speaker_label: Optional[str] = None,
        speaker_name: Optional[str] = None,
        chunk_duration_ms: int = 50,
        realtime: bool = False,
        auto_start: bool = True,
    ) -> str:
        """新增預錄音檔通道（測試用）。

        Args:
            channel_id: 通道 ID
            audio_data: int16 numpy array
            sample_rate: 取樣率
            speaker_label: 說話人標籤
            speaker_name: 說話人名稱
            chunk_duration_ms: chunk 大小（毫秒）
            realtime: 是否模擬即時播放速度
            auto_start: 是否立即啟動

        Returns:
            channel_id
        """
        self._require_initialized()
        cid = self._channel_manager.add_file_channel(
            channel_id=channel_id,
            audio_data=audio_data,
            sample_rate=sample_rate,
            speaker_label=speaker_label,
            speaker_name=speaker_name,
            chunk_duration_ms=chunk_duration_ms,
            realtime=realtime,
        )
        self._total_channels_ever += 1

        if auto_start and self._state == EngineState.RUNNING:
            self._channel_manager.start_channel(channel_id)

        return cid

    def add_discord_channel(
        self,
        channel_id: str,
        user_id: str,
        username: Optional[str] = None,
        auto_start: bool = True,
    ) -> str:
        """新增 Discord 語音通道（預留介面）。

        Raises:
            NotImplementedError: 此功能將在獨立的 Discord 整合專案中實作。
        """
        raise NotImplementedError(
            "Discord channel support is reserved for future implementation. "
            "See: feature/discord-sink (separate project)"
        )

    def remove_channel(
        self,
        channel_id: str,
        drain_timeout: float = 3.0,
    ) -> bool:
        """移除通道。

        Args:
            channel_id: 通道 ID
            drain_timeout: 等待殘留 segment 完成的 timeout（秒）

        Returns:
            True 若成功移除
        """
        self._require_initialized()

        # 先停止該通道
        self._channel_manager.stop_channel(channel_id)

        # 等待殘留 segment 完成辨識
        if self._worker_pool is not None:
            self._worker_pool.drain(timeout=drain_timeout)

        return self._channel_manager.remove_channel(channel_id)

    # ──────────────────────────────────────────────────────────
    #  直接辨識（無通道）
    # ──────────────────────────────────────────────────────────

    def transcribe_segment(
        self,
        audio_float: np.ndarray,
        channel_id: str = "direct",
        language: Optional[str] = None,
        speaker_label: str = "unknown",
        speaker_name: str = "Unknown",
    ) -> Optional[TranscriptEntry]:
        """直接辨識一段音訊（跳過 VAD，同步阻塞）。

        適用於測試腳本或一次性辨識，不走通道管線。

        Args:
            audio_float: float32 音訊 [-1, 1]
            channel_id: 虛擬通道 ID
            language: 語言覆寫
            speaker_label: 說話人標籤
            speaker_name: 說話人名稱

        Returns:
            TranscriptEntry，或 None（佇列滿 / 辨識失敗）
        """
        self._require_initialized()

        if self._stt_engine is None:
            return None

        # 直接呼叫底層辨識（同步，不走佇列）
        effective_lang = language or self._default_language
        try:
            result = self._stt_engine._transcribe(audio_float, effective_lang)
        except Exception as e:
            logger.error("[MCEngine] transcribe_segment 失敗: %s", e)
            return None

        text = result.get("text", "").strip()
        if not text:
            return None

        now = time.time()
        duration = len(audio_float) / 16000  # 估算

        entry = TranscriptEntry(
            segment_id=f"direct_{int(now * 1000)}",
            text=text,
            language=result.get("language"),
            confidence=result.get("confidence", 0.0),
            channel_id=channel_id,
            channel_type=ChannelType.FILE,
            speaker_label=speaker_label,
            speaker_name=speaker_name,
            start_ts=now,
            end_ts=now + duration,
        )

        # 加入 aggregator（如果有 active session）
        if self._aggregator is not None:
            self._aggregator.add_entry(entry)

        return entry

    # ──────────────────────────────────────────────────────────
    #  Session 管理
    # ──────────────────────────────────────────────────────────

    def start_session(
        self,
        session_id: Optional[str] = None,
    ) -> TranscriptSession:
        """開始新的 transcript session。

        若有 active session 會先結束並觸發 on_session_end。
        """
        self._require_initialized()

        # 結束舊 session
        old = self._aggregator.end_session()
        if old is not None and self.on_session_end is not None:
            try:
                self.on_session_end(old)
            except Exception as e:
                logger.error("[MCEngine] on_session_end callback 失敗: %s", e)

        return self._aggregator.start_session(
            session_id=session_id,
            started_at=time.time(),
        )

    def end_session(self) -> Optional[TranscriptSession]:
        """結束當前 session。"""
        self._require_initialized()

        session = self._aggregator.end_session()
        if session is not None and self.on_session_end is not None:
            try:
                self.on_session_end(session)
            except Exception as e:
                logger.error("[MCEngine] on_session_end callback 失敗: %s", e)
        return session

    def get_transcript(
        self,
        format: Literal["readable", "srt", "json"] = "readable",
        since_ts: Optional[float] = None,
    ) -> Union[str, list]:
        """取得 transcript。

        Args:
            format: "readable" / "srt" / "json"
            since_ts: 只取此時間戳之後的 entries（僅 readable 格式支援）

        Returns:
            str（readable/srt）或 list（json）
        """
        self._require_initialized()

        if format == "readable":
            return self._aggregator.format_readable(since_ts=since_ts)
        elif format == "srt":
            return self._aggregator.format_srt()
        elif format == "json":
            return self._aggregator.export_json()
        else:
            raise ValueError(f"Unknown format: {format!r}")

    # ──────────────────────────────────────────────────────────
    #  Metrics / 狀態查詢
    # ──────────────────────────────────────────────────────────

    def get_metrics(self) -> EngineMetrics:
        """取得引擎整體統計。"""
        uptime = 0.0
        if self._started_at is not None and self._state in (
            EngineState.RUNNING, EngineState.STOPPED,
        ):
            uptime = time.time() - self._started_at

        # 佇列 metrics
        queue_metrics = QueueMetrics()
        if self._worker_pool is not None:
            raw = self._worker_pool.get_metrics()
            queue_data = raw.get("queue", {})
            queue_metrics = QueueMetrics(**queue_data)

        # Session 統計
        total_entries = 0
        total_audio = 0.0
        session = self._aggregator.get_session() if self._aggregator else None
        if session is not None:
            total_entries = session.total_segments
            total_audio = session.total_audio_duration

        return EngineMetrics(
            active_channels=(
                self._channel_manager.active_count
                if self._channel_manager else 0
            ),
            total_channels_ever=self._total_channels_ever,
            queue=queue_metrics,
            total_entries=total_entries,
            total_audio_duration=total_audio,
            uptime=round(uptime, 2),
            model_name=(
                getattr(self._stt_engine, "_fw_model_size", None)
                if self._stt_engine else None
            ),
            device=(
                getattr(self._stt_engine, "device", None)
                if self._stt_engine else None
            ),
        )

    def get_channel_status(
        self,
        channel_id: Optional[str] = None,
    ) -> Union[ChannelStatus, Dict[str, ChannelStatus], None]:
        """取得通道狀態。

        Args:
            channel_id: 特定通道 ID（None = 全部）

        Returns:
            單一 ChannelStatus / 全部的 dict / None（通道不存在）
        """
        self._require_initialized()

        if channel_id is not None:
            return self._channel_manager.get_channel_status(channel_id)
        return self._channel_manager.get_all_status()

    def detect_overlapping_speech(self) -> List[Tuple[str, str]]:
        """偵測目前同時有語音的通道對。"""
        self._require_initialized()
        return self._channel_manager.detect_overlapping_speech()

    # ── Properties ──

    @property
    def is_initialized(self) -> bool:
        return self._state in (
            EngineState.INITIALIZED,
            EngineState.RUNNING,
            EngineState.STOPPED,
        )

    @property
    def is_running(self) -> bool:
        return self._state == EngineState.RUNNING

    @property
    def state(self) -> EngineState:
        return self._state

    @property
    def channel_count(self) -> int:
        if self._channel_manager is None:
            return 0
        return self._channel_manager.channel_count

    # ──────────────────────────────────────────────────────────
    #  Internal
    # ──────────────────────────────────────────────────────────

    def _require_initialized(self) -> None:
        """確保引擎已初始化。"""
        if not self.is_initialized:
            raise RuntimeError(
                f"Engine not initialized (state={self._state.value}). "
                "Call initialize() first."
            )

    def _on_entry_internal(self, entry: TranscriptEntry) -> None:
        """內部 entry callback — 轉發給外部 on_entry。"""
        if self.on_entry is not None:
            try:
                self.on_entry(entry)
            except Exception as e:
                logger.error("[MCEngine] on_entry callback 失敗: %s", e)
