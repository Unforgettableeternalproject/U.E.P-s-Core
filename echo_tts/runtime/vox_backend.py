"""VoxCPM2 串流後端（供 Echo Stream 的 VoxSpeakStage 使用）。

介面契約：
- ``VoxBackend(config)``、``prepare()``（同步）、``stream(text, *, lang=None, speed=None)``、``unload()``、``sample_rate``。
- ``stream`` 是同步 generator，yield 1-D float32 numpy chunk（48 kHz）。消費端在同一執行緒 ``close()``。
- 同時只服務一個請求：內部 lock 在 generator 的 ``with`` 內持有，close / GeneratorExit / 例外都會釋放。

各語言定案設定（照實驗結果）：
- zh：ULT（加速參考音 + 逐字稿）、LoRA 開、台灣讀音前處理（normalize=False）
- en：REF（只給參考音 EV25b）、LoRA 關、不前處理
- ja：REF（只給參考音 JV25b）、LoRA 關、先轉假名
生成參數 cfg 2.0、steps 10，不載入 denoiser，streaming 不做 badcase 重試。
"""
from __future__ import annotations

import gc
import json
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import numpy as np

SAMPLE_RATE = 48000
SPEED_TIERS = (1.0, 1.1, 1.2)
CFG_VALUE = 2.0
INFERENCE_TIMESTEPS = 10
DEFAULT_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets" / "vox"
UNLOAD_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class LangSpec:
    lora: bool
    default_speed: float
    preprocess: str | None  # "taiwan" | "kana" | None


LANGS: dict[str, LangSpec] = {
    "zh": LangSpec(lora=True, default_speed=1.2, preprocess="taiwan"),
    "en": LangSpec(lora=False, default_speed=1.0, preprocess=None),
    "ja": LangSpec(lora=False, default_speed=1.0, preprocess="kana"),
}

# 暖機句：每種語言短、中、長各一句
WARMUP_TEXTS: dict[str, tuple[str, str, str]] = {
    "zh": (
        "你好。",
        "週末我想去市場買點菜，順便繞去書店逛逛。",
        "今天早上我先把信箱整理了一遍，接著確認了下午會議要用的資料，等一下還要去超商領包裹，應該可以在天黑之前把事情都處理完。",
    ),
    "en": (
        "Hello there.",
        "The weather is nice today. I finished organizing everything on my desk.",
        "The report is almost finished. I only need to check the numbers once more, and then I will send it to the team before the meeting starts this afternoon.",
    ),
    "ja": (
        "こんにちは。",
        "今日はいい天気ですね。机の上の仕事を全部片付けました。",
        "今朝の会議では、来月の予定と新しい計画について話し合い、最後に資料を全員に送りました。そのあと、静かな店で温かいお茶を飲みました。",
    ),
}

# 假名：平假名、片假名（不含中點 U+30FB 與長音 U+30FC，中文名字常用到）、片假名擴充、半形片假名
_KANA = re.compile(r"[ぁ-ゖゝ-ゟァ-ヺヽ-ヿㇰ-ㇿｦ-ﾟ]")
_HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿々〆]")


def detect_lang(text: str) -> str:
    """含假名是 ja（含漢字的日文也先看假名），含漢字是 zh，其餘 en。"""
    if _KANA.search(text):
        return "ja"
    if _HAN.search(text):
        return "zh"
    return "en"


def normalize_lang(lang: str | None, text: str) -> str:
    if lang is None:
        return detect_lang(text)
    key = str(lang).lower().replace("_", "-").split("-")[0]
    if key not in LANGS:
        raise ValueError(f"不支援的語言：{lang!r}（支援 {sorted(LANGS)}）")
    return key


def snap_speed(speed: float | None, lang: str, available: tuple[float, ...] = SPEED_TIERS) -> float:
    """取最接近的固定檔位；None 時用語言預設。available 為該語言實際有參考音的檔位。"""
    if speed is None:
        return LANGS[lang].default_speed
    try:
        value = float(speed)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"speed 必須是數字：{speed!r}") from exc
    return min(available, key=lambda t: (abs(t - value), t))


def load_manifest(assets_dir: str | Path) -> dict[str, Any]:
    """讀取 assets manifest 並檢查檔案存在；回傳 {lang: {"mode", "speeds": {tier: {wav(絕對路徑), transcript}}}}。"""
    root = Path(assets_dir)
    path = root / "manifest.json"
    if not path.is_file():
        raise FileNotFoundError(f"找不到 VOX assets manifest：{path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, Any] = {}
    for lang, spec in data["languages"].items():
        mode = spec["mode"]
        if mode not in ("ult", "ref"):
            raise ValueError(f"manifest {lang} 的 mode 無效：{mode!r}")
        speeds: dict[float, dict[str, Any]] = {}
        for tier, item in spec["speeds"].items():
            wav = root / item["wav"]
            if not wav.is_file():
                raise FileNotFoundError(f"manifest 指向的參考音不存在：{wav}")
            if mode == "ult" and not item.get("transcript"):
                raise ValueError(f"{lang} {tier} 為 ULT 模式，必須有 transcript")
            speeds[float(tier)] = {"wav": str(wav), "transcript": item.get("transcript")}
        out[lang] = {"mode": mode, "speeds": speeds}
    return out


class VoxBackend:
    sample_rate = SAMPLE_RATE

    def __init__(self, config: dict):
        config = dict(config or {})
        for key in ("model_dir", "lora_dir"):
            if not config.get(key):
                raise ValueError(f"VoxBackend config 必須提供 {key}")
        self.model_dir = Path(config["model_dir"])
        self.lora_dir = Path(config["lora_dir"])
        self.assets_dir = Path(config.get("assets_dir") or DEFAULT_ASSETS_DIR)
        self.device = config.get("device", "cuda")
        self.optimize = bool(config.get("optimize", True))
        self._lock = threading.Lock()
        self._model: Any = None
        self._caches: dict[tuple[str, float], dict] = {}
        self._tiers: dict[str, tuple[float, ...]] = {}
        self._tw: Any = None
        self._kana: Any = None
        self._lora_on: bool | None = None

    # ------------------------------------------------------------------ 生命週期
    @property
    def prepared(self) -> bool:
        return self._model is not None

    def prepare(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            try:
                self._prepare_locked()
            except BaseException:
                self._release_locked()
                raise

    def _prepare_locked(self) -> None:
        manifest = load_manifest(self.assets_dir)
        for lang in LANGS:
            if lang not in manifest:
                raise ValueError(f"manifest 缺少語言 {lang}")
        for p in (self.model_dir, self.lora_dir):
            if not p.exists():
                raise FileNotFoundError(f"路徑不存在：{p}")
        from ..text.japanese_kana import JapaneseKanaNormalizer
        from ..text.taiwan_pronunciation import TaiwanPronunciationNormalizer

        self._tw = TaiwanPronunciationNormalizer()
        self._kana = JapaneseKanaNormalizer()
        self._model = self._load_model()
        sr = getattr(getattr(self._model, "tts_model", None), "sample_rate", SAMPLE_RATE)
        if sr != SAMPLE_RATE:
            raise RuntimeError(f"模型輸出取樣率 {sr} 與契約 {SAMPLE_RATE} 不符")
        # prompt cache：每個 (語言, 檔位) 一份，ULT 同時給 prompt 與 reference
        for lang, spec in manifest.items():
            self._tiers[lang] = tuple(sorted(spec["speeds"]))
            for tier, item in spec["speeds"].items():
                self._caches[(lang, tier)] = self._build_cache(spec["mode"], item)
        # 暖機：每種語言短、中、長；zh 走 ULT（預設檔位 1.2）
        for lang in LANGS:
            for text in WARMUP_TEXTS[lang]:
                for _ in self._generate(text, lang, None):
                    pass

    def _load_model(self) -> Any:
        from voxcpm import VoxCPM
        from voxcpm.model.voxcpm2 import LoRAConfig

        cfg = LoRAConfig(enable_lm=True, enable_dit=True, enable_proj=False, r=32, alpha=32, dropout=0.0)
        model = VoxCPM.from_pretrained(
            str(self.model_dir),
            load_denoiser=False,
            optimize=self.optimize,
            local_files_only=True,
            device=self.device,
            lora_config=cfg,
        )
        model.load_lora(str(self.lora_dir))
        return model

    def _build_cache(self, mode: str, item: dict[str, Any]) -> dict:
        tts = self._model.tts_model
        if mode == "ult":
            return tts.build_prompt_cache(
                prompt_text=item["transcript"], prompt_wav_path=item["wav"], reference_wav_path=item["wav"]
            )
        return tts.build_prompt_cache(reference_wav_path=item["wav"])

    def unload(self) -> None:
        """釋放模型與 GPU 記憶體（對應 IndexTTS2Backend.unload 的語意）。"""
        if not self._lock.acquire(timeout=UNLOAD_TIMEOUT_S):
            raise TimeoutError("VoxBackend.unload 等不到 lock：有 stream 尚未結束或 generator 未 close")
        try:
            self._release_locked()
        finally:
            self._lock.release()

    def _release_locked(self) -> None:
        self._model = None
        self._caches.clear()
        self._tiers.clear()
        self._tw = None
        self._kana = None
        self._lora_on = None
        gc.collect()
        torch = sys.modules.get("torch")
        if torch is not None and torch.cuda.is_available():
            try:
                torch._dynamo.reset()
            except Exception:  # noqa: BLE001
                pass
            gc.collect()
            torch.cuda.empty_cache()

    # ------------------------------------------------------------------ 串流
    def stream(self, text: str, *, lang: str | None = None, speed: float | None = None) -> Iterator[np.ndarray]:
        """yield 1-D float32 chunk。lock 在 with 內，close / GeneratorExit 時一定釋放。"""
        with self._lock:
            if self._model is None:
                raise RuntimeError("VoxBackend 尚未 prepare 或已 unload")
            yield from self._generate(text, lang, speed)

    def _preprocess(self, text: str, lang: str) -> str:
        kind = LANGS[lang].preprocess
        if kind == "taiwan":
            text = self._tw.normalize(text)
        elif kind == "kana":
            text = self._kana.normalize(text)
        return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()

    def _generate(self, text: str, lang: str | None, speed: float | None) -> Iterator[np.ndarray]:
        """呼叫端須已持有 lock。"""
        lang = normalize_lang(lang, text)
        tier = snap_speed(speed, lang, self._tiers[lang])
        body = self._preprocess(text, lang)
        if not body:
            return
        self._set_lora(LANGS[lang].lora)
        gen = self._open_stream(body, self._caches[(lang, tier)])
        try:
            for wav in gen:
                chunk = np.asarray(wav, dtype=np.float32).reshape(-1)
                if chunk.size:
                    yield chunk
        finally:
            close = getattr(gen, "close", None)
            if close is not None:
                close()

    def _set_lora(self, on: bool) -> None:
        if self._lora_on is not on:
            self._model.set_lora_enabled(on)
            self._lora_on = on

    def _open_stream(self, text: str, cache: dict) -> Iterator[np.ndarray]:
        raw = self._model.tts_model._generate_with_prompt_cache(
            target_text=text,
            prompt_cache=cache,
            min_len=2,
            max_len=4096,
            inference_timesteps=INFERENCE_TIMESTEPS,
            cfg_value=CFG_VALUE,
            retry_badcase=False,
            streaming=True,
        )
        return _to_numpy(raw)


def _to_numpy(raw: Any) -> Iterator[np.ndarray]:
    try:
        for wav, _, _ in raw:
            yield wav.squeeze(0).cpu().numpy()
    finally:
        raw.close()


__all__ = ["VoxBackend", "detect_lang", "normalize_lang", "snap_speed", "load_manifest", "SAMPLE_RATE"]
