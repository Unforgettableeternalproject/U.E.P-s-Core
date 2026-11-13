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


class _Job:
    """一次翻譯工作：``done`` 設起代表結束；成功時 ``out`` 有值，失敗時 ``reason`` 有值，被放棄則兩者皆空。"""

    __slots__ = ("abandonable", "abandoned", "done", "out", "prefetch", "reason", "started", "urgent", "waiters")

    def __init__(self, urgent: bool, abandonable: bool, prefetch: bool = False) -> None:
        self.urgent = urgent
        self.abandonable = abandonable  # 由 translate 建立：所有等待者都放棄後才可放棄
        self.prefetch = prefetch  # 預翻譯工作：尚未開始前可被較新的預翻譯取代
        self.started = False  # 已拿到模型開始推論（無法再放棄）
        self.waiters = 0  # 正在等這個工作結果的 translate 呼叫數
        self.abandoned = False
        self.done = threading.Event()
        self.out: str | None = None
        self.reason = ""


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
        self._cpu_fallback_used = False
        self._ready = threading.Event()
        self._start_lock = threading.Lock()
        self._started = False
        self._run_lock = threading.Lock()  # 序列化推論（單一 GPU 模型）
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._inflight: dict[str, _Job] = {}  # 進行中的翻譯工作（同一句只翻一次）；受 _cache_lock 保護
        self._urgent = 0  # 當句翻譯（含預翻譯）進行中的數量；背景脈絡翻譯在它為 0 時才搶模型
        self._urgent_lock = threading.Lock()
        self._urgent_idle = threading.Event()
        self._urgent_idle.set()

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

    @property
    def load_error(self) -> str | None:
        """載入失敗的例外類別名（None = 尚未失敗）；module coordinator 的觀測 handle 用它區分「載入中」與「失敗」。"""
        return self._load_error

    @property
    def cpu_fallback_used(self) -> bool:
        """GPU 載入失敗後已改用 cpu（觀測 handle 以 degraded 表達）。"""
        return self._cpu_fallback_used

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
                    self._cpu_fallback_used = True
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

    def translate(self, text: str, timeout_ms: int | None = None, *, background: bool = False) -> TranslateResult:
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
        # 同一句已有進行中的翻譯（預翻譯或背景脈絡）就等它，不重複翻；等的時間仍受原本的逾時限制
        job, created = self._submit(prepared, urgent=not background, abandonable=True, waiter=True)
        finished = job.done.wait(budget)
        with self._cache_lock:
            job.waiters -= 1
            last_waiter = job.waiters <= 0
        if not finished:
            if job.abandonable and last_waiter and not job.started:
                job.abandoned = True  # 沒有人在等了且還沒開始跑：不佔用模型（已開始的無法中止，結果仍會寫進快取）
            return done(None, STATUS_UNAVAILABLE, "timeout")
        if job.reason:
            return done(None, STATUS_UNAVAILABLE, job.reason)
        if job.out is None:
            return done(None, STATUS_UNAVAILABLE, "timeout")  # 被放棄的工作
        return done(job.out, STATUS_OK, "" if created else "inflight")

    # ---- 翻譯工作（預翻譯、當句、背景脈絡共用）----

    _MAX_PENDING_PREFETCH = 2  # 尚未開始的預翻譯最多幾個；超過就放棄最舊的
    _BACKGROUND_MAX_YIELD_S = 0.5  # 背景脈絡翻譯最多讓路這麼久，之後不論有沒有當句翻譯都會排進去（避免餓死）

    def _submit(
        self, prepared: str, *, urgent: bool, abandonable: bool, prefetch: bool = False, waiter: bool = False,
    ) -> tuple[_Job, bool]:
        """取得（或建立）這句的翻譯工作；回傳 ``(job, 是否由本次呼叫建立)``。同一句同時只會有一個工作。
        ``waiter=True`` 的呼叫端會等結果（登記在 ``job.waiters``，由呼叫端離開時扣掉）。"""
        with self._cache_lock:
            cached = self._cache.get(prepared)
            if cached is not None:  # 與呼叫端先前的快取檢查之間可能已有別的工作完成：直接回結果，不再開工作
                job = _Job(urgent, abandonable)
                job.out = cached
                job.done.set()
                job.waiters = 1 if waiter else 0
                return job, False
            job = self._inflight.get(prepared)
            if job is not None:
                if waiter:
                    job.waiters += 1
                return job, False
            job = _Job(urgent, abandonable, prefetch)
            if waiter:
                job.waiters = 1
            if prefetch:
                self._supersede_prefetch_locked(prepared)
            self._inflight[prepared] = job
        if urgent:
            self._urgent_enter()
        try:
            threading.Thread(target=self._work, args=(prepared, job), name="uep-mt-run", daemon=True).start()
        except BaseException:  # 無法建立執行緒（資源不足）：回滾登記，這次當作不可用，不往外拋
            logger.exception("MT 執行緒建立失敗")
            with self._cache_lock:
                if self._inflight.get(prepared) is job:
                    del self._inflight[prepared]
            if urgent:
                self._urgent_exit()
            job.reason = "error"
            job.done.set()
        return job, True

    def _supersede_prefetch_locked(self, prepared: str) -> None:
        """新預翻譯進來時，放棄還沒開始、沒人在等的舊預翻譯：文字是新文字前綴者（語音合併越來越長）與超過上限的最舊者。
        持有 ``_cache_lock`` 時呼叫。被放棄的工作不會佔用模型，也不再接受新的等待者（從 ``_inflight`` 移除）。"""
        pending = [
            (key, j) for key, j in self._inflight.items()
            if j.prefetch and not j.started and not j.abandoned and j.waiters <= 0
        ]
        drop = [key for key, _ in pending if prepared.startswith(key)]
        rest = [key for key, _ in pending if key not in drop]
        while len(rest) >= self._MAX_PENDING_PREFETCH:
            drop.append(rest.pop(0))
        for key in drop:
            job = self._inflight.pop(key)
            job.abandoned = True
            self.metrics["prefetch_superseded"] += 1

    def _urgent_enter(self) -> None:
        with self._urgent_lock:
            self._urgent += 1
            self._urgent_idle.clear()

    def _urgent_exit(self) -> None:
        with self._urgent_lock:
            self._urgent -= 1
            if self._urgent <= 0:
                self._urgent = 0
                self._urgent_idle.set()

    def _work(self, prepared: str, job: _Job) -> None:
        try:
            if not job.urgent:
                # 背景脈絡翻譯讓路：有當句翻譯在等（含預翻譯）時先不搶模型；已在推論中的無法中止
                give_up_at = time.monotonic() + self._BACKGROUND_MAX_YIELD_S
                while not job.abandoned and time.monotonic() < give_up_at and not self._urgent_idle.wait(0.02):
                    pass
            # 等模型時定期看有沒有被取代／放棄，被放棄的工作不必排到輪到才退出（也讓背景工作不被它擋住）
            while not self._run_lock.acquire(timeout=0.02):
                if job.abandoned:
                    return
            try:
                backend = self._backend
                if job.abandoned or backend is None:
                    return
                job.started = True
                try:
                    out = str(backend.translate(prepared)).strip()
                except BaseException as exc:  # noqa: BLE001 - 後端錯誤不可往外洩
                    if _is_oom(exc):
                        job.reason = "oom"
                    else:
                        logger.warning("MT 推論失敗：%s", type(exc).__name__)
                        job.reason = "error"
                else:
                    if is_degenerate(out):
                        job.reason = "degenerate"
                    else:
                        with self._cache_lock:
                            self._cache[prepared] = out
                            while len(self._cache) > _CACHE_SIZE:
                                self._cache.popitem(last=False)
                        job.out = out
            finally:
                self._run_lock.release()
            if job.reason == "oom":
                self._on_oom()
        finally:
            with self._cache_lock:
                if self._inflight.get(prepared) is job:  # 被取代的舊工作已不在表內；不能誤刪同鍵的新工作
                    del self._inflight[prepared]
            job.done.set()
            if job.urgent:
                self._urgent_exit()

    def prefetch(self, text: str) -> None:
        """不阻塞：在背景翻這句並寫進快取，之後的 ``translate`` 直接命中或等這個進行中的工作。

        已在快取、已在翻譯中、非中文、模型未就緒都直接略過；任何失敗都只計數，不往外拋。
        """
        try:
            if not text or not text.strip():
                return
            prepared = self.replace_aliases(text.strip())
            if not cjk_dominant(prepared):
                return
            with self._cache_lock:
                if prepared in self._cache:
                    self.metrics["prefetch_skip_cached"] += 1
                    return
            self.start_warmup()
            if self._load_error or self._backend is None:
                self.metrics["prefetch_skip_unready"] += 1
                return
            _job, created = self._submit(prepared, urgent=True, abandonable=False, prefetch=True)
            self.metrics["prefetch_started" if created else "prefetch_skip_inflight"] += 1
        except Exception:
            self.metrics["prefetch_error"] += 1
            logger.exception("MT prefetch 失敗")

    def translate_background(self, text: str, timeout_ms: int | None = None) -> TranslateResult:
        """背景工作用（上一輪脈絡）：與 ``translate`` 相同，但讓當句翻譯（含預翻譯）優先使用模型。"""
        return self.translate(text, timeout_ms, background=True)

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
