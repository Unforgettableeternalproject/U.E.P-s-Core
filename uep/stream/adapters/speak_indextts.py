"""SpeakStage adapter：IndexTTS2（Phase 1）。

## 這一層在解什麼問題

後端與契約的方向相反：

* `IndexTTS2Backend.generate()` 是**同步阻塞**呼叫，透過
  ``on_segment_audio(tensor, seg_idx, total)`` **push** 音訊出來
* :class:`~uep.stream.contracts.stages.SpeakStage` 是 **async pull**

所以要做 push→pull 橋接：在 executor 執行緒跑同步推理，callback 用
:meth:`StreamChannel.put_threadsafe` 把音訊塞進**有界**通道，
event loop 這側從通道拉。

## 有界是重點，不是效能調參

無界通道會讓 TTS 一路合成到底。使用者在第 2 句插話時，後面 8 句的 GPU
時間已經花掉了——取消的「上游傳播」變成空話，因為上游早就跑完了。

有界通道滿溢時 ``put_threadsafe`` 會**阻塞推理執行緒**，這正是要的效果：
**背壓要能傳到 GPU**。

## 取消怎麼穿透到 GPU

``generate()` 在執行緒裡跑，asyncio 的取消碰不到它。唯一的協作點是
``on_segment_audio`` callback：

1. token 取消 → 阻塞中的 ``put_threadsafe`` 拿到 ``CancelledError``
2. 例外從 callback 拋出 → 傳播出 ``engine.synthesize`` → ``generate()`` 中止
3. 剩餘的段不會被合成

**已經開始的那一段仍會算完**——段內無法中斷。這是段要切短的另一個理由，
不只是為了 TTFA。

## 為什麼句子要另外緩衝

合成期間我們卡在 ``run_in_executor``，不會去消費上游的句子流。
如果不緩衝，SentenceSplitter 就被 TTS 擋住了，
「LLM 生成第二句的時間被 TTS 合成第一句吃掉」這個重疊也就沒了——
整條管線退化回序列相加。

所以有個 feeder task 持續把句子拉進有界緩衝。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import os
import re
import shutil
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from ..contracts.cancellation import CancellationToken, CancelledError, CancelReason
from ..contracts.style import EMOTION_DIMENSIONS, SpeechStyle
from ..contracts.types import TTS_SAMPLE_RATE, AudioChunk, Sentence
from ..channel import ChannelClosed, StreamChannel

logger = logging.getLogger(__name__)

TEMP_PREFIX = "echo_stream_tts_"
STALE_TEMP_AGE_S = 24 * 3600
EMPTY_TEMP_GRACE_S = 60.0
"""空目錄的寬限期：剛建好、標記檔還沒寫入的目錄不能當殘留清掉。"""
PID_MARKER = ".owner_pid"
"""暫存目錄內的擁有者標記檔，內容是建立該目錄的行程 pid。"""


def _pid_alive(pid: int) -> bool:
    """行程是否存活。Windows 的 ``os.kill(pid, 0)`` 會真的終止行程，所以走 ctypes。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # 存取被拒代表行程存在，只是我們沒權限
            return ctypes.get_last_error() == 5 or kernel32.GetLastError() == 5
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _owner_alive(d: Path) -> bool:
    """目錄裡的 pid 標記指向一個存活的行程（另一個仍在跑的 TTS stage）。"""
    try:
        pid = int((d / PID_MARKER).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return _pid_alive(pid)


def sweep_stale_tempdirs(
    root: str | Path | None = None,
    max_age_s: float = STALE_TEMP_AGE_S,
    empty_grace_s: float = EMPTY_TEMP_GRACE_S,
) -> int:
    """清掉上一次沒正常收工留下的暫存目錄，回傳清了幾個。

    ``TemporaryDirectory`` 只在 ``aclose()`` 時 cleanup；殺 port、關視窗、程式炸掉
    這些結束方式（已知陷阱 #1 那種殘留 server 正是這樣收的）目錄會留著。
    實測 %TEMP% 躺了 23 個空目錄（2026-08-23）。規則：空的一律刪、非空但超過
    ``max_age_s`` 也刪——非空代表某次合成到一半被殺，那個 wav 沒人會再用。
    **不碰仍在使用的**：目錄內的 ``.owner_pid`` 標記指向存活行程就跳過（keep_wav=False
    時兩句之間目錄本來就是空的，不能只靠「空」判斷）；沒有標記的空目錄要超過
    ``empty_grace_s`` 才算殘留，避開剛建好還沒寫標記的瞬間。
    """
    base = Path(root) if root is not None else Path(tempfile.gettempdir())
    removed = 0
    now = time.time()
    try:
        candidates = list(base.glob(f"{TEMP_PREFIX}*"))
    except OSError:
        return 0
    for d in candidates:
        if not d.is_dir():
            continue
        try:
            if _owner_alive(d):
                continue
            age = now - d.stat().st_mtime
            empty = not any(p for p in d.iterdir() if p.name != PID_MARKER)
            if (empty and age > empty_grace_s) or age > max_age_s:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("清掉 %d 個殘留的 TTS 暫存目錄", removed)
    return removed

DEFAULT_SAMPLE_RATE = TTS_SAMPLE_RATE
"""22050Hz，對齊 IndexTTS2 config 的 ``s2mel.preprocess_params.sr``。"""


class _Aborted(Exception):
    """內部訊號：從 callback 拋出以中止 ``generate()``。"""

    is_cancellation = True
    """取消不算失敗：TTS 端 lite_engine 據此把這個例外降為 debug 記錄。"""


CONFIG_STRETCH_RATIO = 1.72
"""引擎 config 的 ``s2mel_stretch_ratio`` 預設值（fallback）。

echo_tts 沒有可 import 的常數（值在 config.yaml 的 ``synthesis.s2mel_stretch_ratio``，
lite_engine 內另有硬編碼預設），所以執行時改向 backend 查
``get_speed_info()["config_stretch_ratio"]``，查不到才用這個值。

引擎裡的關係是 ``stretch_ratio = config_stretch_ratio - speed × 0.5``，
stretch 越大唸得越慢。
"""


def scale_emotion(vector: list[float], intensity: float) -> list[float]:
    """八維向量 × 相對強度 → 送進引擎的向量（總和落在 echo_tts 定案區間）。

    區間常數與縮放函式從 echo_tts import——TTS 那邊調區間，這裡自動跟，
    不在整合層再抄一份數字。import 不到（舊分支）退回「向量 × 強度」。
    """
    try:
        from echo_tts.config.emotion_presets import scale_to_user_strength
    except ImportError:
        alpha = max(0.0, min(1.0, intensity))
        return [v * alpha for v in vector]
    return scale_to_user_strength(list(vector), intensity)


def load_emotion_presets(
    path: str | None = None,
) -> tuple[dict[str, dict[str, float]], dict[str, str]]:
    """讀 echo_tts 的 ``emotion_presets.yaml``，轉成 core 的 preset 表形狀。

    回傳 ``(presets, aliases)``：presets 是 key → 八維分佈（只取 vector，
    ``overrides`` 那些 dynamics / text_markers 是 echo_tts session 層的功能，
    這條管線繞過它，不接）；aliases 目前空——中文別名在 core 的內建表。
    """
    from echo_tts.config.emotion_presets import get_preset_vector, load_presets

    raw = load_presets(path) if path else load_presets()
    presets: dict[str, dict[str, float]] = {}
    for key, entry in raw.items():
        vec = list(get_preset_vector(entry))
        presets[str(key)] = {
            dim: float(v)
            for dim, v in zip(EMOTION_DIMENSIONS, vec, strict=False)
            if float(v) > 0.0
        }
    return presets, {}


def speed_multiplier_to_offset(
    multiplier: float, config_stretch_ratio: float | None = None
) -> float:
    """把 :attr:`SpeechStyle.speed` 的**倍率**換成 IndexTTS 的 speed 偏移。

    契約層用倍率（1.0 = 正常、1.1 = 快 10%），因為那是 backend 無關的說法；
    IndexTTS 用 -1~1 的偏移。換算：

        stretch = CONFIG / r        （要快 r 倍，就把拉伸縮小 r 倍）
        offset  = (CONFIG - stretch) / 0.5 = 2 × CONFIG × (1 - 1/r)

    夾在 [-1, 1]，超出範圍的倍率會被截斷而不是拋錯——
    語速設過頭應該是「頂到底」，不該讓整個 turn 掛掉。
    """
    if multiplier <= 0:
        return 0.0
    ratio = config_stretch_ratio if config_stretch_ratio else CONFIG_STRETCH_RATIO
    offset = 2.0 * ratio * (1.0 - 1.0 / multiplier)
    return max(-1.0, min(1.0, offset))


_TTS_STRIP_CHARS = "「」『』【】《》〈〉“”‘’\"'`*_#~<>|（）()［］[]"
"""合成前剔除的符號。引號會被 IndexTTS 唸出怪聲（實測），
括號、markdown 記號同理——它們是視覺標記，不是語音內容。
逗號句號等韻律標點**保留**，那些影響停頓，是語音的一部分。

例外：成對的「」『』不在這裡剔除，由 :func:`_keep_paired_quotes` 處理——
TTS 端 ``TextNormalizer._emphasis_quotes_to_pauses`` 會把成對引號轉成停頓
（強調語氣），前提是引號還在。"""

_TTS_STRIP_TABLE = {ord(c): None for c in _TTS_STRIP_CHARS if c not in "「」『』"}

_QUOTE_PAIRS = {"「": "」", "『": "』"}
_QUOTE_CHARS = frozenset("「」『』")
_QUOTE_INNER_MAX = 30
"""TTS 端成對引號的正規式是 ``(.{1,30}?)``——內文超過 30 字的配對對方
不認，留著反而會變成孤兒引號，所以超長的一律剔除。"""


def _keep_paired_quotes(text: str) -> str:
    """保留成對、不巢狀、內文 1~30 字的「」『』，其餘引號剔除。

    成對判斷用堆疊（「」配「」、『』配『』）。孤兒、錯配、巢狀外層、
    內文為空或過長的引號都刪掉，行為與舊版（全刪）一致。
    """
    keep: set[int] = set()
    stack: list[int] = []
    for i, ch in enumerate(text):
        if ch in _QUOTE_PAIRS:
            stack.append(i)
        elif ch in _QUOTE_CHARS and stack and _QUOTE_PAIRS[text[stack[-1]]] == ch:
            start = stack.pop()
            inner = text[start + 1 : i]
            if (
                inner.strip()
                and len(inner) <= _QUOTE_INNER_MAX
                and not any(c in _QUOTE_CHARS for c in inner)
            ):
                keep.update((start, i))
    return "".join(ch for i, ch in enumerate(text) if ch not in _QUOTE_CHARS or i in keep)


# --- 顏文字 / 裝飾符號 ---
# 合成前整段剔除：引擎只會濾掉部分字元，「￣▽￣ノ」處理後會殘留「￣ ￣」被唸出怪聲。
# 規則（刻意保守，純 ASCII 與一般中英文不受影響）：
#   1. 括號群組：內含「非中英數／非一般標點」的字元（▽◕ω・ 等）、或整串只有符號
#      （^_^、>_<、T_T），連同前導與緊貼的尾巴（ﾉ*:・ﾟ✧、ノ、單一字母 b/o）一起刪
#   2. 無括號：幾何／方塊／雜項符號、emoji、全形長音符 ￣、半形假名 ﾟﾉ、ヾゞヽ 等「裝飾字元」
#      一律刪；其後緊貼的 ノ 或單一 b/o/p（￣▽￣b）一併刪
#   3. 獨立的 ^^ / >_< / T_T / -_- 類表情（前後不得緊貼英數）
# 不動：OK b 計畫、C++、A/B 測試、(2024年)、(笑)、30°C、一般括號說明。
_KAO_DECOR = (
    r"\u2022"                # 項目符號 •
    r"\u2190-\u21ff"         # 箭頭
    r"\u2300-\u23ff"         # 技術符號
    r"\u2500-\u27bf"         # 方塊／幾何／雜項符號／Dingbats（┻▽◕★✧♪♥）
    r"\u2900-\u29ff\u2b00-\u2bff"
    r"\ufe30-\ufe4f"         # 直排括號（︵︶）
    r"\uffe3"                # ￣
    r"\uff61-\uff9f"         # 半形標點與假名（｡ﾟﾉ）
    r"\u30fd\u30fe\u309d\u309e"   # ヽヾゝゞ
    r"\u0294\u0295\u1d25"    # ʔʕᴥ
    r"\U0001f000-\U0001faff"  # emoji
    r"\ufe0f\u200d"          # emoji 變體／連接符
)
_KAO_TAIL = (  # 括號群組後緊貼的手腳（含泰文）
    r"[\u30fb\uff9f\u30ce\uff89\u30fe\u309e\u30fd"
    r"/\\*:~.\u0e00-\u0e7f]*"
)
_KAO_LETTER = r"(?:[boBOpP](?![A-Za-z0-9]))?"
# 括號內「一般內容」字元：英數、空白、CJK 與假名（不含 ・）、全形標點、一般標點
_KAO_NORMAL = re.compile(
    r"[A-Za-z0-9\s\u4e00-\u9fff\u3000-\u303b\u3041-\u3096\u30a1-\u30fa\u30fc"
    r"\uff00-\uff60\u2010-\u2027\u00b7,.!?;:'\"\-+/%&@#$*=<>_^|\\~`]"
)
_KAO_SYMBOL_ONLY = re.compile(r"^(?=.*[\^_;<>])[\^\-_=~*;:<>.\sA-Za-z]{2,9}$")
_KAO_GROUP = re.compile(
    r"[" + _KAO_DECOR + r"]*[(（\[]([^()（）\[\]\n]{1,24})[)）\]]" + _KAO_TAIL + _KAO_LETTER
)
_KAO_RUN = re.compile(r"[" + _KAO_DECOR + r"]+(?:ノ|[boBOpP](?![A-Za-z0-9]))?")
_KAO_FACE = re.compile(
    r"(?<![A-Za-z0-9_])(?:\^[_.\-w]?\^|>[_.\-w]<|[Tt;=\-][_.][Tt;=\-])(?![A-Za-z0-9_])"
)


def _is_kaomoji_group(inner: str) -> bool:
    """括號內容是否為顏文字（含裝飾字元，或整串只有符號）。"""
    if not inner.strip():
        return False
    if any(not _KAO_NORMAL.match(ch) for ch in inner):
        return True
    if not any(ch.isalnum() for ch in inner):
        return True   # (^_^) (>_<) (;_;)：整串沒有字母數字
    return bool(_KAO_SYMBOL_ONLY.match(inner)) and not re.search(r"[A-Za-z]{3}", inner)


def strip_kaomoji(text: str) -> str:
    """剔除顏文字與裝飾符號（見上方規則）。不處理空白，由呼叫端收攏。"""
    text = _KAO_GROUP.sub(lambda m: "" if _is_kaomoji_group(m.group(1)) else m.group(0), text)
    text = _KAO_RUN.sub("", text)
    return _KAO_FACE.sub("", text)


def sanitize_for_tts(text: str) -> str:
    """清掉不該被唸出來的符號（含顏文字），並收攏多餘空白。"""
    # 顏文字要先於括號剝除（後者會把括號拿掉，群組特徵就沒了）
    text = strip_kaomoji(text)
    # 句中的列表破折號（「小說。 - 《死亡擱淺》」）換成頓點——那是列舉的
    # 停頓，不是連字號。兩側都有空白才算，"well-known" 這種不受影響。
    cleaned = re.sub(r"\s+[-–—•·・]+\s+", "，", text)
    # 先決定成對引號的去留（此時其他符號還在，內文長度以剝除後為準較貼近 TTS，
    # 所以先剝其他符號、引號留到最後處理）
    cleaned = cleaned.translate(_TTS_STRIP_TABLE)
    cleaned = _keep_paired_quotes(cleaned)
    return " ".join(cleaned.split())


def smooth_segment_edges(
    pcm: bytes,
    sample_rate: int,
    fade_ms: float = 4.0,
    tail_silence_ms: float = 15.0,
    *,
    fade_in: bool = True,
    fade_out: bool = True,
) -> bytes:
    """段緣平滑：半 Hann 淡入淡出 + 段尾短靜音墊。

    段與段是獨立合成的，邊界樣本值不連續，接起來就是爆音（click）。
    調查結論（docs/design/tts-improvement-reference.md）：Hann window
    淡化 + 段間短靜音是純訊號處理的消爆音手段，UTMOS 損失僅 -0.08~-0.13。

    淡化只動段緣幾毫秒（預設 4ms ≈ 88 樣本 @22050Hz），聽不出音量變化；
    靜音墊直接附在段尾，前端排程不需要知道這件事。

    ``fade_in`` / ``fade_out`` 可單獨關閉一側：串流式 backend 把一句切成
    多個 chunk 時，只有句首要淡入、句尾要淡出，中間的 chunk 邊界是連續的。
    """
    import numpy as np

    arr = np.frombuffer(pcm, dtype="<i2").astype("float32")
    n_fade = min(int(sample_rate * fade_ms / 1000), len(arr) // 2)
    if n_fade > 0:
        # 半 Hann：0→1 的升沿。比線性淡化少一點高頻假影。
        ramp = 0.5 * (1.0 - np.cos(np.pi * np.arange(n_fade) / n_fade))
        if fade_in:
            arr[:n_fade] *= ramp
        if fade_out:
            arr[-n_fade:] *= ramp[::-1]
    out = arr.astype("<i2").tobytes()
    n_pad = int(sample_rate * tail_silence_ms / 1000)
    if n_pad > 0:
        out += b"\x00\x00" * n_pad
    return out


def tensor_to_pcm16(tensor: Any) -> bytes:
    """把引擎吐出的音訊轉成 int16 little-endian PCM bytes。

    接受 torch tensor 或 numpy array——測試用假 backend 時是後者。
    contracts 層一律用 bytes，torch/numpy 只活在 adapter 邊界。
    """
    if hasattr(tensor, "detach"):
        arr = tensor.detach().cpu().float().numpy()
    else:
        import numpy as np

        arr = np.asarray(tensor, dtype="float32")

    import numpy as np

    arr = np.squeeze(arr)
    if arr.ndim > 1:
        arr = arr[0]
    return (np.clip(arr, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


class IndexTTS2SpeakStage:
    """把 IndexTTS2 包成 SpeakStage。

    ``backend`` 可注入——`IndexTTS2Backend` 本身也接受 ``engine_factory``，
    但注入整個 backend 讓 adapter 的橋接邏輯能在**沒有 GPU、沒有 echo_tts**
    的環境下測試。Phase 1 的多數 bug 會出在橋接而不是模型。
    """

    name = "indextts2"

    def __init__(
        self,
        *,
        backend: Any | None = None,
        device: str | None = None,
        use_fp16: bool | None = None,
        use_accel: bool | None = None,
        queue_size: int | None = None,
        sentence_buffer: int = 2,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        language: str | None = None,
        emotion_vector: list[float] | None = None,
        emotion_overrides: dict | None = None,
        speed: float | None = None,
        lora_scale: float | None = None,
        output_dir: str | Path | None = None,
        keep_wav: bool = False,
        warmup: bool = False,
        warmup_text: str = "嗯。",
        edge_fade_ms: float = 4.0,
        segment_silence_ms: float = 15.0,
    ) -> None:
        self._backend = backend
        self.device = device or "cuda"
        self.use_fp16 = (
            use_fp16 if use_fp16 is not None else True
        )
        # IndexTTS2Backend 的 use_accel 預設為 False；保留該預設，避免整合層
        # 未經實機驗收就改變推理路徑。需要加速時可由參數或環境設定明確開啟。
        self.use_accel = (
            use_accel
            if use_accel is not None
            else False
        )
        self.queue_size = queue_size or 2
        self.sentence_buffer = sentence_buffer
        self.sample_rate = sample_rate
        self.language = language
        self.emotion_vector = emotion_vector
        self.emotion_overrides = emotion_overrides
        self.speed = (
            speed if speed is not None else 0.0
        )
        """語速。引擎裡是 ``stretch_ratio = config_stretch_ratio - speed × 0.5``
        （config 預設 1.72，stretch 越大越慢），範圍 -1.0 ~ 1.0。

        預設 0.0 是**引擎原本的預設值**，與 webapp 一致——adapter 不該偷改
        既有的音色特性。要調快就設這個參數，別讓管線層自己猜。"""
        self.lora_scale = lora_scale
        """LoRA 合併強度。None = 不載（預設）；有值就在 ``prepare()`` 以此 scale **永久合併**
        進 GPT 權重（accel 下也會同步進 accel 複本）。合併後不能再調，要換強度須重新載入。"""
        self.keep_wav = keep_wav
        self.warmup = warmup
        self.warmup_text = warmup_text
        self.edge_fade_ms = edge_fade_ms
        """段緣淡入淡出的長度（毫秒）。0 = 停用，消段間爆音用。"""

        self.segment_silence_ms = segment_silence_ms
        """段尾靜音墊（毫秒）。0 = 停用。與淡化搭配消爆音，見
        :func:`smooth_segment_edges`。"""

        self._default_emotion: list[float] | None = None
        """backend 開機時的情緒向量（角色檔預設）。style 退場時要**還原**——
        不還原的話，一句帶情緒的話會讓之後所有中性句都殘留那個情緒。"""

        self._current_speed_offset: float = 0.0
        self._output_dir = Path(output_dir) if output_dir else None
        self._tempdir: tempfile.TemporaryDirectory | None = None
        self._prepared = False
        self._synthesis_lock = asyncio.Lock()
        """持有模型存取權直到 executor worker 真正結束，不隨呼叫端取消提早釋放。"""

    # --- 生命週期 ---

    async def prepare(self) -> None:
        """載入模型。很慢（數十秒），所以在 executor 跑並且只做一次。"""
        if self._prepared:
            return
        loop = asyncio.get_running_loop()
        # 先掃上次沒收乾淨的暫存目錄，再建自己的
        await loop.run_in_executor(None, sweep_stale_tempdirs)
        if self._backend is None:
            self._backend = await loop.run_in_executor(None, self._build_backend)
        load = getattr(self._backend, "load", None)
        if callable(load):
            await loop.run_in_executor(None, load)

        # LoRA：用合併而非 PEFT 動態掛載——accel 路徑不經 inference_model，掛載會靜默失效
        if self.lora_scale is not None:
            merge = getattr(self._backend, "merge_lora", None)
            if not callable(merge):
                raise RuntimeError("backend 不支援 merge_lora，無法套用 LoRA")
            stats = await loop.run_in_executor(
                None, lambda: merge(scale=self.lora_scale)
            )
            logger.info("[IndexTTS2] LoRA 已合併 scale=%s 同步統計=%s", self.lora_scale, stats)

        # 語速要在載入後才能設（會動到引擎狀態）
        if self.speed:
            set_speed = getattr(self._backend, "set_speed", None)
            if callable(set_speed):
                await loop.run_in_executor(None, set_speed, self.speed)
        self._current_speed_offset = self.speed or 0.0

        # 記住角色檔的預設情緒——per-sentence style 退場時要還原到這裡
        default_emotion = getattr(self._backend, "emotion_vector", None)
        self._default_emotion = list(default_emotion) if default_emotion else None

        if self.warmup:
            await loop.run_in_executor(None, self._run_warmup)
        self._prepared = True

    def _run_warmup(self) -> None:
        """跑一次丟棄的合成。

        **預設關閉，因為 IndexTTS2 引擎自己已經做了。**

        `checkpoints_v25/config.yaml` 裡 ``enable_warmup: true``，
        引擎在 ``_init_models`` 階段就會跑 ``_warmup_models()``
        （預熱文本「你好，這是預熱測試。」），觸發 torch.compile 與
        CUDA kernel 編譯。載入回報的時間本來就含這一段。

        所以 adapter 再暖一次是**重複勞動**：實測多花 15 秒載入
        （27s → 44s），首段時間沒有改善（1788ms → 1842ms，在雜訊範圍內）。

        暖機本身是有效的——只是該由引擎做，不是這一層。保留這個選項是
        為了應付「引擎的內建暖機被關掉」或換用其他後端的情況。
        """
        path = self._next_output_path(
            Sentence(text=self.warmup_text, turn_id="warmup", index=0)
        )
        try:
            self._backend.generate(
                self.warmup_text,
                path,
                on_segment_audio=None,
                language=self.language,
                emotion_overrides=self.emotion_overrides,
            )
        except Exception:  # noqa: BLE001 - 暖機失敗不該擋住正常流程
            return
        finally:
            with contextlib.suppress(OSError):
                Path(path).unlink(missing_ok=True)

    def _build_backend(self) -> Any:
        """建立真的 IndexTTS2Backend。**只有這裡 import echo_tts。**"""
        # echo_tts 已遷入 Core（M5），權重位置見 echo_tts.config.model_paths
        from echo_tts.runtime.indextts_backend import IndexTTS2Backend
        from echo_tts.runtime.indextts_generate import default_generation_paths

        paths = default_generation_paths()
        return IndexTTS2Backend(
            paths=paths,
            device=self.device,
            use_fp16=self.use_fp16,
            use_accel=self.use_accel,
            emotion_vector=self.emotion_vector,
        )

    async def aclose(self) -> None:
        # worker 對模型的同步存取尚未結束前，不卸載 backend。
        async with self._synthesis_lock:
            backend = self._backend
            if backend is not None:
                unload = getattr(backend, "unload", None)
                if callable(unload):
                    with contextlib.suppress(Exception):
                        await asyncio.get_running_loop().run_in_executor(None, unload)
        if self._tempdir is not None:
            with contextlib.suppress(Exception):
                self._tempdir.cleanup()
            self._tempdir = None
        self._prepared = False

    # --- 串流 ---

    async def stream(
        self, sentences: AsyncIterator[Sentence], token: CancellationToken
    ) -> AsyncIterator[AudioChunk]:
        await self.prepare()
        loop = asyncio.get_running_loop()
        stream_token = token.child()

        # 句子緩衝：讓 SentenceSplitter 在 TTS 合成期間能繼續跑，
        # 否則兩層就變成序列相加而不是重疊
        sentence_ch: StreamChannel[Sentence] = StreamChannel(
            maxsize=self.sentence_buffer, token=stream_token
        )
        audio_ch: StreamChannel[AudioChunk] = StreamChannel(
            maxsize=self.queue_size, token=stream_token
        )

        feeder = asyncio.ensure_future(self._feed(sentences, sentence_ch, stream_token))
        producer = asyncio.ensure_future(
            self._produce(sentence_ch, audio_ch, loop, stream_token)
        )

        try:
            async for chunk in audio_ch:
                yield chunk
        finally:
            # aclose/task.cancel 也必須解除 executor callback 的背壓；子 token
            # 不會反向取消其他 stage 共用的 turn token。
            stream_token.cancel(CancelReason.SHUTDOWN, "TTS stream closed")
            for task in (feeder, producer):
                if not task.done():
                    task.cancel()
            # 收掉背景 task 的例外，避免 "Task exception was never retrieved"
            for task in (feeder, producer):
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await task
            stream_token.detach()

    async def _feed(
        self,
        sentences: AsyncIterator[Sentence],
        channel: StreamChannel[Sentence],
        token: CancellationToken,
    ) -> None:
        try:
            async for sentence in sentences:
                if not sentence.text:
                    continue
                await channel.put(sentence)
        except CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - 傳給消費端而不是消失在 task 裡
            channel.fail(exc)
            return
        channel.close()

    async def _produce(
        self,
        sentence_ch: StreamChannel[Sentence],
        audio_ch: StreamChannel[AudioChunk],
        loop: asyncio.AbstractEventLoop,
        token: CancellationToken,
    ) -> None:
        try:
            async for sentence in sentence_ch:
                token.raise_if_cancelled()
                worker = asyncio.create_task(
                    self._synthesize_exclusive(sentence, audio_ch, loop, token)
                )
                # shield 讓 producer 可在取消時立即退出；worker 自己持有鎖，
                # 直到同步 generate 真正結束，因此後續 turn 不會重入模型。
                worker.add_done_callback(self._consume_task_result)
                await asyncio.shield(worker)
        except CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            audio_ch.fail(exc)
            return
        audio_ch.close()

    async def _synthesize_exclusive(
        self,
        sentence: Sentence,
        audio_ch: StreamChannel[AudioChunk],
        loop: asyncio.AbstractEventLoop,
        token: CancellationToken,
    ) -> None:
        async with self._synthesis_lock:
            # 已在等待模型所有權的工作可能已過期；此處檢查可阻止它進 generate。
            token.raise_if_cancelled()
            worker = loop.run_in_executor(
                None, self._synthesize_one, sentence, audio_ch, loop, token
            )
            # shield 避免 task.cancel 把 asyncio 包裝 future 標成取消；鎖只會在
            # executor future 完成後離開，callback 需要的 event loop 仍可正常運作。
            await asyncio.shield(worker)

    @staticmethod
    def _consume_task_result(task: asyncio.Task) -> None:
        """取回呼叫端取消後仍在背景完成的 worker task 例外。"""
        if not task.cancelled():
            with contextlib.suppress(Exception):
                task.exception()

    def _synthesize_one(
        self,
        sentence: Sentence,
        audio_ch: StreamChannel[AudioChunk],
        loop: asyncio.AbstractEventLoop,
        token: CancellationToken,
    ) -> None:
        """在 executor 執行緒裡跑同步推理。**這個函式不在 event loop 上。**"""
        chunk_index = 0
        output_path = self._next_output_path(sentence)
        speak_text = sanitize_for_tts(sentence.text)
        if not any(ch.isalnum() for ch in speak_text):
            return  # 整句都是符號／標點／顏文字——沒東西可唸，不送去合成
        self._apply_style(sentence.style)

        def on_segment(tensor: Any, seg_idx: int, total: int) -> None:
            nonlocal chunk_index
            if token.is_cancelled:
                raise _Aborted
            pcm = tensor_to_pcm16(tensor)
            if self.edge_fade_ms > 0 or self.segment_silence_ms > 0:
                pcm = smooth_segment_edges(
                    pcm,
                    self.sample_rate,
                    fade_ms=self.edge_fade_ms,
                    tail_silence_ms=self.segment_silence_ms,
                )
            chunk = AudioChunk(
                pcm=pcm,
                sample_rate=self.sample_rate,
                turn_id=sentence.turn_id,
                sentence_index=sentence.index,
                chunk_index=chunk_index,
                is_last=sentence.is_last and seg_idx >= total - 1,
            )
            chunk_index += 1
            # 滿了會阻塞這個執行緒 = 背壓傳到 GPU；
            # 取消時 put_threadsafe 會拋 CancelledError／ChannelClosed，
            # 一律轉成 _Aborted：TTS 端 lite_engine 只認 is_cancellation 屬性，
            # 否則 barge-in 的正常中止會被記成 ERROR。
            try:
                audio_ch.put_threadsafe(chunk, loop)
            except (
                CancelledError,
                asyncio.CancelledError,
                concurrent.futures.CancelledError,
                ChannelClosed,
            ) as exc:
                raise _Aborted from exc

        try:
            self._backend.generate(
                speak_text,
                output_path,
                on_segment_audio=on_segment,
                language=self.language,
                emotion_overrides=self.emotion_overrides,
            )
        except (_Aborted, CancelledError):
            return
        finally:
            if not self.keep_wav:
                with contextlib.suppress(OSError):
                    Path(output_path).unlink(missing_ok=True)

    def _apply_style(self, style: SpeechStyle | None) -> None:
        """把句子的 :class:`SpeechStyle` 翻譯成 IndexTTS 的原生控制。

        在 executor 執行緒裡呼叫，且句子是**序列**合成的（一次一句），
        所以直接改 backend 狀態是安全的——不會有兩句同時在改。

        ``style`` 為 None 或中性時完全不動 backend：預設音色是既有資產，
        adapter 不該無故覆寫它。

        ``expressiveness`` 與 ``volume`` 目前沒有對應的 IndexTTS 參數，
        會被忽略——這是刻意的，SpeechStyle 是**各後端取所需**的共同描述，
        不是每個欄位都保證被實作。

        ``intensity`` 的翻譯走 echo_tts 的強度區間（2026-08-23 改）：
        0~1 刻度 → ``user_to_strength`` → 總和落在 echo_tts 定案區間（0.5 = 中點），
        再 ``scale_to_strength`` 等比縮放——preset 向量只剩比例有意義。
        舊做法「向量 × intensity」送進去的總和多半 < 0.5，落在 TTS 那邊定案的
        「情緒表現不足」區，LLM 標記聽起來跟沒標一樣。

        style 為 None 或中性時**還原**角色檔預設，而不是不動——
        上一句的情緒殘留在 backend 狀態裡，是「一句開心之後全部都開心」
        這種 bug 的來源。
        """
        backend = self._backend

        # 情緒向量：有 style 就換算，沒有就還原預設
        if style is not None and not style.is_neutral:
            backend.emotion_vector = scale_emotion(style.emotion_vector(), style.intensity)
        else:
            backend.emotion_vector = (
                list(self._default_emotion) if self._default_emotion else None
            )

        # 語速：style 指定倍率就換算成偏移，否則回到啟動時的基準值。
        # 只在值真的變了才呼叫 set_speed——不必每句都戳引擎。
        target = (
            speed_multiplier_to_offset(style.speed, self._engine_config_stretch_ratio())
            if style is not None and style.speed != 1.0
            else (self.speed or 0.0)
        )
        if target != self._current_speed_offset:
            set_speed = getattr(backend, "set_speed", None)
            if callable(set_speed):
                set_speed(target)
                self._current_speed_offset = target

    def _engine_config_stretch_ratio(self) -> float | None:
        """向已載入的引擎查實際 config 的 stretch 基準；查不到回 None（用常數 fallback）。"""
        backend = self._backend
        if getattr(backend, "engine", None) is None:
            return None  # 未載入時 backend 回的是寫死的 1.7，不可信
        getter = getattr(backend, "get_speed_info", None)
        if not callable(getter):
            return None
        try:
            value = float(getter()["config_stretch_ratio"])
        except Exception:  # noqa: BLE001 - 查不到就用 fallback，不讓語速換算失敗
            return None
        return value if value > 0 else None

    def _next_output_path(self, sentence: Sentence) -> Path:
        """`generate()` 一定要寫檔，即使我們只要 callback 的音訊。

        預設丟暫存目錄並在合成後刪掉；``keep_wav=True`` 時保留，
        調參比對音質時會用到。
        """
        if self._output_dir is not None:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            base = self._output_dir
        else:
            if self._tempdir is None:
                self._tempdir = tempfile.TemporaryDirectory(prefix=TEMP_PREFIX)
            base = Path(self._tempdir.name)
            # 目錄可能被外部清掉（舊版 sweep、使用者手動清 %TEMP%）：自我修復並重寫標記
            if not (base / PID_MARKER).exists():
                base.mkdir(parents=True, exist_ok=True)
                with contextlib.suppress(OSError):
                    (base / PID_MARKER).write_text(str(os.getpid()), encoding="utf-8")
        return base / f"{sentence.turn_id}_{sentence.index:03d}.wav"


__all__ = [
    "IndexTTS2SpeakStage",
    "sanitize_for_tts",
    "strip_kaomoji",
    "smooth_segment_edges",
    "tensor_to_pcm16",
    "speed_multiplier_to_offset",
    "sweep_stale_tempdirs",
    "scale_emotion",
    "load_emotion_presets",
    "DEFAULT_SAMPLE_RATE",
    "CONFIG_STRETCH_RATIO",
]
