"""Boundary Translator（決策中樞架構提案 §5.4）：把送進決策模型的中文文字翻成英文。

只服務決策特徵入口（目前是 ``laya.shadow``）。喚醒詞比對（WakeDetector）、給 LLM 的原文與人設回覆
一律維持原文，不經本模組。MT 後端是 Helsinki-NLP/opus-mt-zh-en（MarianMT）；``torch``／``transformers``
只在真後端建立當下延遲 import，本模組 import 時不載入任何推論依賴。

行為：
- 翻譯前先把助理別名（小U、小優、UEP…）換成固定佔位英文名，避免被機翻拼壞。
- 輸入不以 CJK 為主時直接略過（原樣當英文，仍套用別名替換）。
- 退化輸出（單一詞重複佔比過高）與逾時、載入失敗、例外一律回 ``unavailable``，不丟例外；呼叫端自行決定降級。
- 懶載入＋背景 warmup（載入後跑數句短句）；warmup 完成前 ``translate`` 回 ``unavailable``（reason=``warming``）。
- 載入或推論 OOM 時，若允許則改用 CPU 重載（``cpu_fallback``）。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger("uep.echo.translate_wiring")

STATUS_OK = "ok"
STATUS_SKIPPED = "skipped"  # 非 CJK 為主，原樣當英文
STATUS_UNAVAILABLE = "unavailable"

DEFAULT_TRANSLATE_DEVICE = "cuda"
DEFAULT_TRANSLATE_TIMEOUT_MS = 400
DEFAULT_PLACEHOLDER = "Yuki"  # 挑不易被改拼的名字（實測 "Aria" 偶被拼成 "Arya"）
DEFAULT_ALIASES: tuple[str, ...] = ("小U", "小優", "小悠", "U.E.P", "UEP", "uep")
MT_MODEL_SUBDIR = "opus-mt-zh-en"
WARMUP_SENTENCES: tuple[str, ...] = ("你好", "今天天氣怎麼樣？", "幫我查一下明天的行程。")
CJK_RATIO_THRESHOLD = 0.3  # Han 字佔（Han + 拉丁字母）的比例低於此值視為非 CJK 為主
_CACHE_SIZE = 64
_HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_LATIN = re.compile(r"[A-Za-z]")
_LATIN_ALIAS = re.compile(r"[A-Za-z0-9_. \-]+")


def mt_model_dir(configured: str | None = None) -> Path:
    """MT 權重目錄：設定值優先，否則 ``~/.uep/models/mt/opus-mt-zh-en``。"""
    raw = (configured or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".uep" / "models" / "mt" / MT_MODEL_SUBDIR


@dataclass(frozen=True)
class TranslateResult:
    """``text_en`` 為 None 代表 unavailable（呼叫端降級）；``reason`` 只作診斷，不含文字內容。"""

    text_en: str | None
    status: str
    latency_ms: float = 0.0
    reason: str = ""


class MtBackend(Protocol):
    def translate(self, text: str) -> str: ...


class MarianBackend:
    """opus-mt-zh-en 真後端。生成參數：greedy、no_repeat_ngram_size=3、max_new_tokens=min(64, 3×輸入 token+8)。"""

    def __init__(self, model_dir: Path, device: str) -> None:
        import torch  # 延遲 import：推論依賴不進模組頂層
        from transformers import MarianMTModel, MarianTokenizer

        self._torch = torch
        self._device = device
        self._tokenizer = MarianTokenizer.from_pretrained(str(model_dir))
        self._model = MarianMTModel.from_pretrained(str(model_dir)).to(device).eval()

    def translate(self, text: str) -> str:
        enc = self._tokenizer(text, return_tensors="pt", truncation=True, max_length=256).to(self._device)
        n_tokens = int(enc["input_ids"].shape[1])
        with self._torch.inference_mode():
            out = self._model.generate(
                **enc, num_beams=1, no_repeat_ngram_size=3, max_new_tokens=min(64, 3 * n_tokens + 8),
            )
        return self._tokenizer.decode(out[0], skip_special_tokens=True)


def cjk_dominant(text: str) -> bool:
    """文字是否以 CJK（Han）為主：Han 字數 / (Han + 拉丁字母) >= 門檻。沒有 Han 字一律 False。"""
    han = len(_HAN.findall(text))
    if han == 0:
        return False
    latin = len(_LATIN.findall(text))
    return han / (han + latin) >= CJK_RATIO_THRESHOLD


def is_degenerate(text_en: str) -> bool:
    """退化偵測：空輸出，或單一 token 重複佔比過高（>0.5 且至少 6 個 token）視為失敗。"""
    tokens = re.findall(r"\w+", text_en.lower())
    if not tokens:
        return True
    if len(tokens) < 6:
        return False
    return Counter(tokens).most_common(1)[0][1] / len(tokens) > 0.5


def _is_oom(exc: BaseException) -> bool:
    return "out of memory" in str(exc).lower() or type(exc).__name__ == "OutOfMemoryError"


class BoundaryTranslator:
    """同步翻譯介面 ``translate(text) -> TranslateResult``；執行緒安全。"""

    def __init__(
        self,
        *,
        backend_factory: Any,
        device: str = DEFAULT_TRANSLATE_DEVICE,
        timeout_ms: int = DEFAULT_TRANSLATE_TIMEOUT_MS,
        aliases: tuple[str, ...] = DEFAULT_ALIASES,
        placeholder: str = DEFAULT_PLACEHOLDER,
        cpu_fallback: bool = True,
        warmup_sentences: tuple[str, ...] = WARMUP_SENTENCES,
    ) -> None:
        self._factory = backend_factory  # (device) -> MtBackend
        self._device = device
        self._timeout_ms = max(1, int(timeout_ms))
        self.placeholder = placeholder
        self._cpu_fallback = cpu_fallback
        self._warmup_sentences = warmup_sentences
        self._alias_re = self._compile_aliases(aliases)
        self.metrics: Counter[str] = Counter()
        self._backend: MtBackend | None = None
        self._load_error: str | None = None
        self._ready = threading.Event()
        self._start_lock = threading.Lock()
        self._started = False
        self._run_lock = threading.Lock()  # 序列化推論（單一 GPU 模型）
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._cache_lock = threading.Lock()

    # ---- 別名替換 ----

    @staticmethod
    def _compile_aliases(aliases: tuple[str, ...]) -> re.Pattern[str] | None:
        parts = []
        for alias in sorted({a.strip() for a in aliases if a and a.strip()}, key=len, reverse=True):
            esc = re.escape(alias)
            if _LATIN_ALIAS.fullmatch(alias):  # 拉丁別名要有字界，避免吃進別的單字
                esc = rf"(?<![A-Za-z0-9]){esc}(?![A-Za-z0-9])"
            parts.append(esc)
        return re.compile("|".join(parts), re.IGNORECASE) if parts else None

    def replace_aliases(self, text: str) -> str:
        if self._alias_re is None:
            return text
        return self._alias_re.sub(self.placeholder, text)

    # ---- 載入 ----

    def start_warmup(self) -> None:
        """啟動背景載入與 warmup（冪等）。"""
        with self._start_lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._load, name="uep-mt-warmup", daemon=True).start()

    def wait_ready(self, timeout: float | None = None) -> bool:
        """等載入（含 warmup）結束；成功或失敗皆算結束。"""
        return self._ready.wait(timeout)

    @property
    def device(self) -> str:
        return self._device

    @property
    def available(self) -> bool:
        return self._backend is not None

    def _build_and_warm(self, device: str) -> MtBackend:
        backend = self._factory(device)
        for sentence in self._warmup_sentences:  # 假推論讓 CUDA 冷啟動在 warmup 內發生
            backend.translate(sentence)
        return backend

    def _load(self) -> None:
        try:
            try:
                self._backend = self._build_and_warm(self._device)
            except Exception as exc:
                if self._cpu_fallback and self._device != "cpu" and isinstance(exc, (RuntimeError, MemoryError)):
                    logger.warning("MT 在 %s 載入失敗（%s），改用 cpu", self._device, type(exc).__name__)
                    self._device = "cpu"
                    self._backend = self._build_and_warm("cpu")
                else:
                    raise
            logger.info("boundary translator 就緒：device=%s", self._device)
        except BaseException as exc:
            self._load_error = type(exc).__name__
            logger.exception("boundary translator 載入失敗，之後一律 unavailable")
        finally:
            self._ready.set()

    # ---- 翻譯 ----

    def translate(self, text: str, timeout_ms: int | None = None) -> TranslateResult:
        """翻成英文。``timeout_ms`` 覆寫預設逾時（背景工作可放寬）；任何失敗都回 unavailable，不拋例外。"""
        t0 = time.perf_counter()

        def done(text_en: str | None, status: str, reason: str = "") -> TranslateResult:
            self.metrics[f"{status}:{reason}" if reason else status] += 1
            return TranslateResult(text_en, status, (time.perf_counter() - t0) * 1000.0, reason)

        if not text or not text.strip():
            return done(None, STATUS_UNAVAILABLE, "empty")
        prepared = self.replace_aliases(text.strip())
        if not cjk_dominant(prepared):
            return done(prepared, STATUS_SKIPPED)
        with self._cache_lock:
            cached = self._cache.get(prepared)
            if cached is not None:
                self._cache.move_to_end(prepared)
                return done(cached, STATUS_OK, "cache")
        self.start_warmup()  # lazy load；就緒前一律 unavailable
        if self._load_error:
            return done(None, STATUS_UNAVAILABLE, "load_failed")
        backend = self._backend
        if backend is None:
            return done(None, STATUS_UNAVAILABLE, "warming")

        budget = (timeout_ms if timeout_ms is not None else self._timeout_ms) / 1000.0
        box: dict[str, Any] = {}
        finished = threading.Event()
        abandoned = threading.Event()

        def work() -> None:
            with self._run_lock:
                if abandoned.is_set():  # 呼叫端已放棄，不佔用模型
                    return
                try:
                    box["out"] = backend.translate(prepared)
                except BaseException as exc:  # noqa: BLE001 - 後端錯誤不可往外洩
                    box["err"] = exc
                finally:
                    finished.set()

        threading.Thread(target=work, name="uep-mt-run", daemon=True).start()
        if not finished.wait(budget):
            abandoned.set()
            return done(None, STATUS_UNAVAILABLE, "timeout")
        err = box.get("err")
        if err is not None:
            if _is_oom(err):
                self._on_oom()
                return done(None, STATUS_UNAVAILABLE, "oom")
            logger.warning("MT 推論失敗：%s", type(err).__name__)
            return done(None, STATUS_UNAVAILABLE, "error")
        out = str(box.get("out", "")).strip()
        if is_degenerate(out):
            return done(None, STATUS_UNAVAILABLE, "degenerate")
        with self._cache_lock:
            self._cache[prepared] = out
            while len(self._cache) > _CACHE_SIZE:
                self._cache.popitem(last=False)
        return done(out, STATUS_OK)

    def _on_oom(self) -> None:
        """推論 OOM：允許就丟掉 GPU 後端、改用 CPU 重新載入；否則維持 unavailable。"""
        if not self._cpu_fallback or self._device == "cpu":
            return
        logger.warning("MT 推論 OOM，改用 cpu 重載")
        self._backend = None
        self._device = "cpu"
        self._ready.clear()
        self._load_error = None
        with self._start_lock:
            self._started = False
        self.start_warmup()


def create_boundary_translator(
    settings: object | None = None, extra_aliases: tuple[str, ...] = (), backend_factory: Any = None,
) -> BoundaryTranslator:
    """依 ``UEP_TRANSLATE_*`` 組態建立翻譯器（不載入模型；懶載入／warmup 由呼叫端觸發）。

    ``extra_aliases`` 併入助理的名稱與別名（來自決策組態的 bot_identity）。
    """
    from uep.config import get_settings

    st = settings if settings is not None else get_settings()
    model_dir = mt_model_dir(getattr(st, "translate_model_dir", None))
    raw_aliases = (getattr(st, "translate_aliases", None) or "").strip()
    aliases = tuple(a.strip() for a in raw_aliases.split(",") if a.strip()) if raw_aliases else DEFAULT_ALIASES
    device = (getattr(st, "translate_device", None) or "").strip() or DEFAULT_TRANSLATE_DEVICE

    def default_factory(dev: str) -> MtBackend:
        from uep.echo.laya_wiring import resolve_device

        return MarianBackend(model_dir, resolve_device(dev))

    timeout = getattr(st, "translate_timeout_ms", None)
    cpu_fallback = getattr(st, "translate_cpu_fallback", True)
    return BoundaryTranslator(
        backend_factory=backend_factory or default_factory,
        device=device,
        timeout_ms=int(timeout) if timeout else DEFAULT_TRANSLATE_TIMEOUT_MS,
        aliases=(*aliases, *extra_aliases),
        placeholder=(getattr(st, "translate_placeholder", None) or "").strip() or DEFAULT_PLACEHOLDER,
        cpu_fallback=True if cpu_fallback is None else bool(cpu_fallback),
    )
