"""VoxCPM2 串流後端（供 Echo Stream 的 VoxSpeakStage 使用）。

介面契約：
- ``VoxBackend(config)``、``prepare()``（同步）、``stream(text, *, lang=None, speed=None, emotion=None, intensity=None)``、
  ``unload()``、``sample_rate``。
- ``stream`` 是同步 generator，yield 1-D float32 numpy chunk（48 kHz）。消費端在同一執行緒 ``close()``。
- 執行緒契約：optimize=True（torch.compile CUDA graph 綁定執行緒）時，prepare / stream / unload 必須在同一條執行緒；
  prepare 記下執行緒 id，stream 開始生成與 unload 若發現不同執行緒，立即丟 RuntimeError（不等 inductor assert）。
  Echo Stream 端請用單執行緒 executor。optimize=False 不檢查。
- unload 會 ``torch._dynamo.reset()`` 清編譯快取；之後換一條執行緒重新 prepare 必須重編譯，而 torch 2.7 的
  cudagraph tree 容器只存在於「第一個 import cudagraph_trees 的執行緒」，其他執行緒會在 inductor 內
  AssertionError。prepare 開頭會為目前執行緒補上容器（``_ensure_cudagraph_tls``），所以 unload 後可在任何一條執行緒重載，
  之後 stream／unload 仍須與該次 prepare 同一條執行緒。
- 同時只服務一個請求：內部 lock 在 generator 的 ``with`` 內持有，close / GeneratorExit / 例外都會釋放。

各語言定案設定（照實驗結果）：
- zh：ULT（加速參考音 + 逐字稿）、LoRA 開、台灣讀音前處理（normalize=False）
- en：REF（只給參考音 EV25b）、LoRA 關、不前處理
- ja：REF（只給參考音 JV25b）、LoRA 關、先轉假名
情緒（僅 zh）：reference 用 zh 中性參考音鎖音色，prompt 用情緒片段（含逐字稿）帶入情緒，LoRA scale 調為 0.85；
中性（emotion 為 None / 全 0 / "neutral"）走既有路徑，行為不變。en、ja 忽略 emotion。
生成參數 cfg 2.0、steps 10，不載入 denoiser，streaming 不做 badcase 重試。
"""
from __future__ import annotations

import gc
import json
import logging
import re
import sys
import threading
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import numpy as np

from ..config.emotion_presets import EMOTION_LABELS, user_to_strength

logger = logging.getLogger(__name__)

SAMPLE_RATE = 48000
SPEED_TIERS = (1.0, 1.1)
CFG_VALUE = 2.0
INFERENCE_TIMESTEPS = 10
DEFAULT_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets" / "vox"
UNLOAD_TIMEOUT_S = 30.0
# 情緒：強度檔位（實際尺度）、有情緒時的 LoRA scale（中性為 1.0）
EMOTION_TIERS = (0.57, 0.61, 0.65)
EMOTION_LORA_SCALE = 0.85
EMOTION_LANGS = ("zh",)


@dataclass(frozen=True)
class LangSpec:
    lora: bool
    default_speed: float
    preprocess: str | None  # "taiwan" | "kana" | None


LANGS: dict[str, LangSpec] = {
    "zh": LangSpec(lora=True, default_speed=1.0, preprocess="taiwan"),
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


def load_emotion_clips(assets_dir: str | Path) -> dict[str, dict[float, dict[str, str]]]:
    """讀取 manifest 的 emotion 區段；回傳 {情緒: {強度檔位: {wav(絕對路徑), transcript}}}。

    沒有 emotion 區段回傳空 dict（全部走中性）；列出但檔案不存在的片段直接報錯。
    """
    root = Path(assets_dir)
    data = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    out: dict[str, dict[float, dict[str, str]]] = {}
    for clip in (data.get("emotion") or {}).get("clips", []):
        label = str(clip["emotion"]).lower()
        if label not in EMOTION_LABELS:
            raise ValueError(f"manifest 情緒標籤無效：{label!r}")
        wav = root / clip["wav"]
        if not wav.is_file():
            raise FileNotFoundError(f"manifest 指向的情緒片段不存在：{wav}")
        if not clip.get("transcript"):
            raise ValueError(f"情緒片段 {label} {clip['strength']} 必須有 transcript")
        out.setdefault(label, {})[float(clip["strength"])] = {"wav": str(wav), "transcript": clip["transcript"]}
    return out


def resolve_emotion(emotion: Any) -> str | None:
    """把 8 維向量 / 標籤字串 / None 解析成情緒標籤；中性、未知或格式不符回傳 None（不拋例外）。

    向量順序同 EMOTION_LABELS；取最大值，同值取靠前者；全部 <= 0 視為中性。
    """
    if emotion is None:
        return None
    if isinstance(emotion, str):
        label = emotion.strip().lower()
        if label in EMOTION_LABELS:
            return label
        if label not in ("", "neutral"):
            logger.warning("未知的情緒標籤 %r，改走中性", emotion)
        return None
    try:
        values = [float(v) for v in emotion]
    except (TypeError, ValueError):
        logger.warning("無法解析的 emotion：%r，改走中性", emotion)
        return None
    if len(values) != len(EMOTION_LABELS):
        logger.warning("emotion 向量長度 %d 不是 %d，改走中性", len(values), len(EMOTION_LABELS))
        return None
    top = max(values)
    if not top > 0:  # 全 0 或含 NaN
        return None
    return EMOTION_LABELS[values.index(top)]


def pick_emotion_tier(intensity: float | None, available: Any) -> float | None:
    """intensity（0~1 user 刻度）→ 實際強度 → 最接近的可用檔位；平手取較強者。無可用檔位回傳 None。"""
    tiers = sorted(float(t) for t in available)
    if not tiers:
        return None
    try:
        strength = round(user_to_strength(intensity), 4)
    except (TypeError, ValueError):
        strength = round(user_to_strength(None), 4)
    return min(tiers, key=lambda t: (round(abs(t - strength), 4), -t))


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
        self._owner_thread: int | None = None  # prepare 所在執行緒（optimize=True 時才記錄）
        self._lora_scale: float = 1.0
        self._emotion_clips: dict[str, dict[float, dict[str, str]]] = {}
        self._zh_refs: dict[float, dict[str, str]] = {}
        self._emotion_caches: dict[tuple[str, float, float], dict] = {}
        self._ignored_emotion_logged: set[str] = set()

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
        self._owner_thread = threading.get_ident() if self.optimize else None
        if self.optimize:
            _ensure_cudagraph_tls()
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
        self._zh_refs = dict(manifest["zh"]["speeds"])
        self._emotion_clips = load_emotion_clips(self.assets_dir)
        # 暖機：每種語言短、中、長；zh 走 ULT（預設檔位 1.0）
        for lang in LANGS:
            for text in WARMUP_TEXTS[lang]:
                for _ in self._generate(text, lang, None):
                    pass
        # zh 情緒路徑暖機一次（含 LoRA 0.85）；沒有情緒片段就略過
        if self._emotion_clips:
            label = "calm" if "calm" in self._emotion_clips else next(iter(self._emotion_clips))
            for _ in self._generate(WARMUP_TEXTS["zh"][1], "zh", None, label, None):
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
            self._check_thread("unload")
            self._release_locked()
        finally:
            self._lock.release()

    def _check_thread(self, op: str) -> None:
        if self._owner_thread is not None and self._owner_thread != threading.get_ident():
            raise RuntimeError(
                f"VoxBackend 必須在 prepare 的同一條執行緒呼叫 stream／unload（torch.compile CUDA graph 綁定執行緒）；"
                f"目前在執行緒 {threading.get_ident()} 呼叫 {op}，prepare 在執行緒 {self._owner_thread}"
            )

    def _release_locked(self) -> None:
        self._owner_thread = None
        self._model = None
        self._caches.clear()
        self._tiers.clear()
        self._tw = None
        self._kana = None
        self._lora_on = None
        self._lora_scale = 1.0
        self._emotion_clips.clear()
        self._zh_refs.clear()
        self._emotion_caches.clear()
        self._ignored_emotion_logged.clear()
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
    def stream(
        self,
        text: str,
        *,
        lang: str | None = None,
        speed: float | None = None,
        emotion: Any = None,
        intensity: float | None = None,
    ) -> Iterator[np.ndarray]:
        """yield 1-D float32 chunk。lock 在 with 內，close / GeneratorExit 時一定釋放。

        emotion：8 維向量（順序同 EMOTION_LABELS）、標籤字串或 None；intensity：0~1 user 刻度（None = 0.5）。
        只有 zh 套用情緒，其他語言忽略。
        """
        with self._lock:
            if self._model is None:
                raise RuntimeError("VoxBackend 尚未 prepare 或已 unload")
            self._check_thread("stream")
            yield from self._generate(text, lang, speed, emotion, intensity)

    def _preprocess(self, text: str, lang: str) -> str:
        kind = LANGS[lang].preprocess
        if kind == "taiwan":
            text = self._tw.normalize(text)
        elif kind == "kana":
            text = self._kana.normalize(text)
        return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()

    def _generate(
        self, text: str, lang: str | None, speed: float | None, emotion: Any = None, intensity: float | None = None
    ) -> Iterator[np.ndarray]:
        """呼叫端須已持有 lock。"""
        lang = normalize_lang(lang, text)
        tier = snap_speed(speed, lang, self._tiers[lang])
        body = self._preprocess(text, lang)
        if not body:
            return
        cache = self._caches[(lang, tier)]
        label = resolve_emotion(emotion)
        scale = 1.0
        emo_tier = None
        if label is not None and lang not in EMOTION_LANGS:
            if lang not in self._ignored_emotion_logged:
                self._ignored_emotion_logged.add(lang)
                logger.debug("VOX %s 尚未支援情緒，忽略 emotion=%r", lang, emotion)
            label = None
        if label is not None:
            found = self._emotion_cache(label, intensity, tier)
            if found is not None:
                (cache, emo_tier), scale = found, EMOTION_LORA_SCALE
        self._set_lora(LANGS[lang].lora)
        try:
            if scale != 1.0:
                self._set_lora_scale(scale)
            mode = "COMBO" if emo_tier is not None else ("ULT" if lang == "zh" else "REF")
            logger.info(
                "VOX 生成 lang=%s mode=%s lora=%s scale=%s speed=%s emotion=%s intensity_tier=%s text=%r",
                lang, mode, "on" if LANGS[lang].lora else "off", scale if LANGS[lang].lora else 0.0, tier,
                label if emo_tier is not None else None, emo_tier, body,
            )
            gen = self._open_stream(body, cache)
            try:
                for wav in gen:
                    chunk = np.asarray(wav, dtype=np.float32).reshape(-1)
                    if chunk.size:
                        yield chunk
            finally:
                close = getattr(gen, "close", None)
                if close is not None:
                    close()
        finally:
            if self._lora_scale != 1.0:
                self._set_lora_scale(1.0)  # 例外 / close 也要還原，不殘留

    def _emotion_cache(self, label: str, intensity: float | None, speed_tier: float) -> tuple[dict, float] | None:
        """找 (情緒, 檔位) 的 prompt cache；缺檔位退到最接近者，完全沒有片段回傳 None（走中性）。"""
        clips = self._emotion_clips.get(label)
        ref = self._zh_refs.get(speed_tier)
        if not clips or ref is None:
            return None
        strength = pick_emotion_tier(intensity, clips)
        key = (label, strength, speed_tier)
        if key not in self._emotion_caches:
            self._emotion_caches[key] = self._build_emotion_cache(clips[strength], ref)
        return self._emotion_caches[key], strength

    def _build_emotion_cache(self, clip: dict[str, str], ref: dict[str, str]) -> dict:
        """reference = zh 中性參考音（鎖音色），prompt = 情緒片段 + 逐字稿（帶入情緒）。"""
        return self._model.tts_model.build_prompt_cache(
            prompt_text=clip["transcript"], prompt_wav_path=clip["wav"], reference_wav_path=ref["wav"]
        )

    def _set_lora(self, on: bool) -> None:
        if self._lora_on is not on:
            self._model.set_lora_enabled(on)
            self._lora_on = on
            self._lora_scale = 1.0  # set_lora_enabled 會把 scaling 重設為 alpha/r

    def _set_lora_scale(self, scale: float) -> None:
        """直接改 LoRALinear 的 scaling buffer（fill_ 不觸發 torch.compile 重編譯）；1.0 = alpha/r。"""
        from voxcpm.modules.layers.lora import LoRALinear

        for m in self._model.tts_model.modules():
            if isinstance(m, LoRALinear):
                m.scaling.fill_(m._base_scaling * scale)
        self._lora_scale = scale

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


def _ensure_cudagraph_tls() -> None:
    """讓目前執行緒擁有 inductor cudagraph tree 的容器（等同這條執行緒是第一個 import 的）。

    容器是 ``threading.local`` 屬性，只在 import cudagraph_trees 的那條執行緒上存在；其他執行緒一旦需要編譯
    （例如 unload 的 dynamo reset 清掉快取後換執行緒重載）就會在 ``get_obj`` 的 assert 失敗。已存在則不動。
    """
    try:
        from torch._inductor import cudagraph_trees as trees
    except Exception:  # noqa: BLE001 - 沒有 inductor／cuda 時不影響 prepare
        return
    local = trees.local
    if not hasattr(local, "tree_manager_containers"):
        local.tree_manager_containers = {}
    if not hasattr(local, "tree_manager_locks"):
        local.tree_manager_locks = defaultdict(threading.Lock)


def _to_numpy(raw: Any) -> Iterator[np.ndarray]:
    try:
        for wav, _, _ in raw:
            yield wav.squeeze(0).cpu().numpy()
    finally:
        raw.close()


__all__ = ["VoxBackend", "detect_lang", "normalize_lang", "snap_speed", "load_manifest", "load_emotion_clips", "resolve_emotion", "pick_emotion_tier", "SAMPLE_RATE"]
