"""Console 的本機音訊 I/O：麥克風錄音（``/mic``）與本機播放（``--play``）。

``sounddevice`` 不是 Core 的硬依賴：只在真正使用時才 import，缺少時丟 ``AudioIoError``（繁中訊息，指出安裝方式）。
麥克風以 ``MicRecorder`` 協定抽象（``start``／``stop``），測試用假音源注入；播放以 ``Player`` 協定抽象。
輸入一律是 16 kHz mono int16（與 ``uep/stream`` 的 STT 輸入一致）。
"""
from __future__ import annotations

from typing import Any, Protocol

MIC_SAMPLE_RATE = 16000
INSTALL_HINT = "請在共用環境安裝：uv pip install sounddevice"


class AudioIoError(RuntimeError):
    """本機音訊裝置不可用（缺 sounddevice、找不到裝置）；訊息已是繁體中文。"""


def _import_sounddevice() -> Any:
    try:
        import sounddevice  # type: ignore[import-not-found]
    except ImportError as exc:
        raise AudioIoError(f"缺少 sounddevice，無法使用麥克風／本機播放。{INSTALL_HINT}") from exc
    except OSError as exc:  # PortAudio 動態庫載入失敗
        raise AudioIoError(f"sounddevice 無法載入 PortAudio（{exc}）") from exc
    return sounddevice


def sounddevice_available() -> bool:
    try:
        _import_sounddevice()
    except AudioIoError:
        return False
    return True


class MicRecorder(Protocol):
    def start(self) -> None: ...

    def stop(self) -> bytes:
        """停止錄音，回傳 16 kHz mono int16 PCM。"""
        ...


class SoundDeviceMic:
    """以預設輸入裝置錄音；``start`` 開串流並累積，``stop`` 關閉並回傳 PCM。"""

    def __init__(self, device: int | str | None = None) -> None:
        self._device = device
        self._stream: Any = None
        self._chunks: list[bytes] = []

    def start(self) -> None:
        sd = _import_sounddevice()
        self._chunks = []

        def callback(indata: Any, _frames: int, _time: Any, _status: Any) -> None:
            self._chunks.append(bytes(indata))

        try:
            self._stream = sd.RawInputStream(
                samplerate=MIC_SAMPLE_RATE, channels=1, dtype="int16", device=self._device, callback=callback,
            )
            self._stream.start()
        except Exception as exc:  # noqa: BLE001 - 裝置錯誤轉成可讀訊息
            self._stream = None
            raise AudioIoError(f"無法開啟麥克風（{type(exc).__name__}：{exc}）") from exc

    def stop(self) -> bytes:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()
        return b"".join(self._chunks)


class Player(Protocol):
    def play(self, pcm: bytes, sample_rate: int) -> None: ...

    def stop(self) -> None: ...


class SoundDevicePlayer:
    """串流播放 int16 mono PCM；``stop`` 立即中止（barge-in／取消時靜音）。"""

    def __init__(self, device: int | str | None = None) -> None:
        self._sd = _import_sounddevice()
        self._device = device
        self._stream: Any = None
        self._rate: int | None = None

    def play(self, pcm: bytes, sample_rate: int) -> None:
        if self._stream is None or self._rate != sample_rate:
            self.stop()
            try:
                self._stream = self._sd.RawOutputStream(
                    samplerate=sample_rate, channels=1, dtype="int16", device=self._device,
                )
                self._stream.start()
            except Exception as exc:  # noqa: BLE001
                self._stream = None
                raise AudioIoError(f"無法開啟播放裝置（{type(exc).__name__}：{exc}）") from exc
            self._rate = sample_rate
        self._stream.write(pcm)

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.abort()
            finally:
                stream.close()
