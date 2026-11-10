"""``nlp.legacy``：現有英文 NLP（bio_tagger＋command_chat_classifier）收編為決策中樞的 shadow proposer（DL-5，L2 特徵層）。

兩個模型都是 ``distilbert-base-uncased`` 微調，僅英文詞彙表，位於 ``models/nlp/``（本機未追蹤資產）：

- ``bio_tagger``：``DistilBertForTokenClassification``，11 個 BIO 標籤（O＋{CALL, CHAT, DIRECT_WORK,
  BACKGROUND_WORK, UNKNOWN} 的 B-／I-），以 ``config.json`` 的 ``id2label`` 為準。
- ``command_chat_classifier``：``DistilBertForSequenceClassification``，``config.json`` 只有 ``LABEL_0..2``；
  語意見 ``CLASSIFIER_LABELS``（以 ``train/nlp/dataset.csv`` 824 筆實測 100% 吻合：0=command、1=chat、2=non-sense）。

只做 shadow：預設不啟用，使用者把 ``nlp.legacy`` 放進 ``UEP_DECISION_SHADOW_PROPOSERS`` 才會組裝。
``transformers``／``torch`` 只在背景載入執行緒內延遲 import；模型目錄走 ``UEP_NLP_MODEL_DIR``
（預設 repo 相對的 ``models/nlp``）。模型檔不存在時，工廠建立當下就拋 ``ValueError``（啟動即報錯，不靜默退回）。

載入策略同 ``laya_wiring``：載入需要數秒，proposer 建構時啟動背景 warmup 執行緒；warmup 期間（或載入失敗後）
``propose`` 立即回 D80 預設並累計 ``metrics``。

語言 gating：模型只懂英文，非英文文字（以 ASCII 字母比例判定）不打分，回 D80 預設
（``reason_code=nlp_lang_unsupported``）。中文要等 MT 或多語編碼器（Q6=C），本次不處理。

隱私：只讀本句文字；不使用 ``speaker_ref`` 與任何平台識別，文字不進日誌。
"""
from __future__ import annotations

import logging
import re
import threading
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from uep.turn.decision_config import DecisionConfig
from uep.turn.models import Proposal, ReplyMode, TurnCandidate
from uep.turn.proposer import fallback_proposal

logger = logging.getLogger("uep.echo.nlp_wiring")

NLP_PROPOSER_ID = "nlp.legacy"
BIO_MODEL_SUBDIR = "bio_tagger"
CLASSIFIER_MODEL_SUBDIR = "command_chat_classifier"
# 預設模型目錄：repo 根目錄下的 models/nlp（可用 UEP_NLP_MODEL_DIR 覆寫）
DEFAULT_NLP_MODEL_DIR = Path(__file__).resolve().parents[2] / "models" / "nlp"
MAX_TOKENS = 256  # 單句截斷長度（模型上限 512，決策句遠小於此）

# 語言 gating：ASCII 字母數 / 全部字母（任何文字系統）數 的下限；低於門檻視為非英文，不打分
ASCII_LETTER_RATIO_THRESHOLD = 0.8
_LABEL_RE = re.compile(r"^[BI]-([A-Z_]+)$")
_UNSAFE = re.compile(r"[^A-Za-z0-9_.:\-]")

# command_chat_classifier 的標籤語意（config.json 只有 LABEL_0..2）。
# 依據：對 train/nlp/dataset.csv（824 筆：command 189／chat 203／non-sense 432）逐筆推理，
# LABEL_0 全部落在 command、LABEL_1 全部落在 chat、LABEL_2 全部落在 non-sense（訓練集內準確率 100%，
# 未做留出驗證；legacy 程式碼只在 config.yaml 列出模型目錄、從未載入它，故無第二來源）。
CLASSIFIER_LABELS: dict[int, str] = {0: "command", 1: "chat", 2: "non-sense"}

# ---- BIO／分類器 -> Proposal 的映射（暫定映射，待標註集驗證）----
# 原則：L2 是「特徵」不是「決策」（提案 §5.2）；這裡的映射只為了讓 shadow 能與主引擎比對差異，
# 不代表模型真的學過 reply_mode。訓練資料沒有 reply_mode／Risky 標註。
# route：BIO 的工作類標籤決定路徑；DIRECT_WORK（立即處理）優先於 BACKGROUND_WORK（背景任務）優先於 CHAT。
BIO_ROUTE: dict[str, str] = {
    "DIRECT_WORK": "task_request",
    "BACKGROUND_WORK": "agent_loop",
    "CHAT": "direct_chat",
}
BIO_ROUTE_PRIORITY: tuple[str, ...] = ("DIRECT_WORK", "BACKGROUND_WORK", "CHAT")
# addressed：任一 BIO 區段為 CALL 即視為對助理說話
BIO_ADDRESSED_LABELS: frozenset[str] = frozenset({"CALL"})
# 分類器：non-sense -> reply_mode=pass；command 在 BIO 沒有工作類標籤時補成 task_request（chat 不影響 route）
CLASSIFIER_PASS_LABEL = "non-sense"
CLASSIFIER_COMMAND_LABEL = "command"
CLASSIFIER_COMMAND_ROUTE = "task_request"
DEFAULT_ROUTE = "direct_chat"


def _safe_code(value: str) -> str:
    return _UNSAFE.sub("_", value)[:64] or "unknown"


def ascii_letter_ratio(text: str) -> float:
    """ASCII 字母佔全部字母的比例；沒有任何字母（純數字、標點、空白）回 0.0。"""
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for ch in letters if ch.isascii()) / len(letters)


def is_english(text: str) -> bool:
    return ascii_letter_ratio(text) >= ASCII_LETTER_RATIO_THRESHOLD


@dataclass(frozen=True)
class BioSegment:
    """BIO 標籤合併後的區段（只保留標籤與信心，不保留文字）。"""

    label: str  # CALL／CHAT／DIRECT_WORK／BACKGROUND_WORK／UNKNOWN
    confidence: float  # 區段內詞首 token 的平均 softmax 機率
    words: int


@dataclass(frozen=True)
class NlpFeatures:
    """兩個模型對一句話的原始輸出（L2 特徵）。"""

    segments: tuple[BioSegment, ...]
    intent: str  # command／chat／non-sense
    intent_confidence: float


class NlpBackend(Protocol):
    def analyze(self, text: str) -> NlpFeatures: ...


def segments_from_labels(labels: list[str], confidences: list[float]) -> tuple[BioSegment, ...]:
    """逐詞 BIO 標籤 -> 區段。``I-X`` 接在同類 ``B-X``／``I-X`` 之後延續，否則（孤立的 I-）視為新區段；``O`` 結束區段。"""
    segments: list[BioSegment] = []
    cur_label: str | None = None
    cur_conf: list[float] = []

    def flush() -> None:
        if cur_label is not None and cur_conf:
            segments.append(BioSegment(cur_label, sum(cur_conf) / len(cur_conf), len(cur_conf)))

    for tag, conf in zip(labels, confidences, strict=True):
        m = _LABEL_RE.match(tag)
        if m is None:  # "O" 或未知標籤
            flush()
            cur_label, cur_conf = None, []
            continue
        name = m.group(1)
        if tag.startswith("I-") and cur_label == name:
            cur_conf.append(conf)
            continue
        flush()
        cur_label, cur_conf = name, [conf]
    flush()
    return tuple(segments)


class _TorchBackend:
    """真實後端：兩個 DistilBERT 在 CPU 上推理。僅由 ``_load_backend`` 建立。"""

    def __init__(self, torch_mod: Any, bio_tok: Any, bio_model: Any, cls_tok: Any, cls_model: Any) -> None:
        self._torch = torch_mod
        self._bio_tok, self._bio_model = bio_tok, bio_model
        self._cls_tok, self._cls_model = cls_tok, cls_model
        self._bio_id2label: dict[int, str] = {int(k): v for k, v in bio_model.config.id2label.items()}

    def analyze(self, text: str) -> NlpFeatures:
        torch = self._torch
        with torch.inference_mode():
            enc = self._bio_tok(text, return_tensors="pt", truncation=True, max_length=MAX_TOKENS)
            probs = torch.softmax(self._bio_model(**enc).logits[0], dim=-1)
            top_p, top_i = probs.max(dim=-1)
            # 訓練時只對每個詞的第一個 subtoken 標註（其餘 -100），推論也只取詞首 token
            labels: list[str] = []
            confs: list[float] = []
            prev = None
            for idx, word_id in enumerate(enc.word_ids(0)):
                if word_id is not None and word_id != prev:
                    labels.append(self._bio_id2label[int(top_i[idx])])
                    confs.append(float(top_p[idx]))
                prev = word_id

            cenc = self._cls_tok(text, return_tensors="pt", truncation=True, max_length=MAX_TOKENS)
            cprobs = torch.softmax(self._cls_model(**cenc).logits[0], dim=-1)
            cp, ci = cprobs.max(dim=-1)
        return NlpFeatures(
            segments=segments_from_labels(labels, confs),
            intent=CLASSIFIER_LABELS.get(int(ci), "unknown"),
            intent_confidence=float(cp),
        )


def resolve_model_dir(configured: str | None) -> Path:
    """``UEP_NLP_MODEL_DIR`` 優先；未設定用 repo 相對的 ``models/nlp``。"""
    return Path(configured).expanduser() if configured and configured.strip() else DEFAULT_NLP_MODEL_DIR


def missing_model_files(model_dir: Path) -> list[str]:
    """回傳缺少的必要檔案（相對 ``model_dir``）；空 list 代表齊全。"""
    need = [
        f"{BIO_MODEL_SUBDIR}/config.json", f"{BIO_MODEL_SUBDIR}/model.safetensors",
        f"{CLASSIFIER_MODEL_SUBDIR}/config.json", f"{CLASSIFIER_MODEL_SUBDIR}/pytorch_model.bin",
    ]
    return [rel for rel in need if not (model_dir / rel).is_file()]


def load_backend(model_dir: Path) -> tuple[NlpBackend, str]:
    """載入兩個模型（CPU）並回傳 (後端, 版本字串)。``transformers``／``torch`` 在此才 import。"""
    import torch
    import transformers
    from transformers import (
        AutoModelForSequenceClassification,
        AutoModelForTokenClassification,
        AutoTokenizer,
    )

    bio_dir, cls_dir = model_dir / BIO_MODEL_SUBDIR, model_dir / CLASSIFIER_MODEL_SUBDIR
    bio_model = AutoModelForTokenClassification.from_pretrained(str(bio_dir)).eval()
    cls_model = AutoModelForSequenceClassification.from_pretrained(str(cls_dir)).eval()
    backend = _TorchBackend(
        torch, AutoTokenizer.from_pretrained(str(bio_dir)), bio_model,
        AutoTokenizer.from_pretrained(str(cls_dir)), cls_model,
    )
    return backend, _safe_code(f"nlp-legacy-tf{transformers.__version__}")


def map_features(candidate: TurnCandidate, features: NlpFeatures, *, version: str) -> Proposal:
    """NlpFeatures -> Proposal（暫定映射，待標註集驗證；映射表見模組常數）。"""
    labels = {seg.label for seg in features.segments}
    bio_route = next((BIO_ROUTE[lab] for lab in BIO_ROUTE_PRIORITY if lab in labels), None)
    if bio_route is not None and bio_route != DEFAULT_ROUTE:
        route = bio_route  # DIRECT_WORK／BACKGROUND_WORK
    elif features.intent == CLASSIFIER_COMMAND_LABEL:
        route = CLASSIFIER_COMMAND_ROUTE  # BIO 沒有工作類標籤，分類器判 command 時補成 task_request
    else:
        route = DEFAULT_ROUTE  # CHAT 或無區段
    addressed = bool(labels & BIO_ADDRESSED_LABELS)
    if features.intent == CLASSIFIER_PASS_LABEL:
        mode = ReplyMode.PASS
    else:
        # 與 D80 預設一致：被點名或單人 -> respond；多人未被點名 -> passive
        solo = candidate.participant_count is None or candidate.participant_count <= 1
        mode = ReplyMode.RESPOND if (addressed or candidate.addressed_hint or solo) else ReplyMode.PASSIVE
    bio_conf = sum(seg.confidence for seg in features.segments) / len(features.segments) if features.segments else 0.0
    confidence = (bio_conf + features.intent_confidence) / 2 if features.segments else features.intent_confidence
    return Proposal(
        reply_mode=mode, addressed=addressed, confidence=min(1.0, max(0.0, confidence)), route=route,
        reason_code="nlp_model", proposer_id=NLP_PROPOSER_ID, proposer_version=version,
    )


def _shadow_fallback(candidate: TurnCandidate, reason_code: str) -> Proposal:
    """不打分時的預設：D80 判斷，但標成 nlp.legacy、confidence 0，方便在 shadow 記錄中辨識。"""
    return replace(fallback_proposal(candidate, reason_code), proposer_id=NLP_PROPOSER_ID, proposer_version="fallback")


class NlpLegacyProposer:
    """現有英文 NLP 的 shadow proposer。"""

    proposer_id = NLP_PROPOSER_ID

    def __init__(self, *, model_dir: Path | str | None = None, autoload: bool = True) -> None:
        self._model_dir = Path(model_dir) if model_dir else DEFAULT_NLP_MODEL_DIR
        missing = missing_model_files(self._model_dir)
        if missing:
            raise ValueError(
                f"nlp.legacy 找不到模型檔（{self._model_dir}）：{', '.join(missing)}；"
                "請放入 models/nlp、以 UEP_NLP_MODEL_DIR 指向，或從 UEP_DECISION_SHADOW_PROPOSERS 移除 nlp.legacy"
            )
        self.version = "warming"
        self.metrics: Counter[str] = Counter()
        self._backend: NlpBackend | None = None
        self._load_error: str | None = None
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        if autoload:
            self.start_warmup()

    # ---- 載入 ----

    def start_warmup(self) -> None:
        """啟動背景載入（冪等）；載入完成前 ``propose`` 回預設。"""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._load, name="uep-nlp-warmup", daemon=True)
        self._thread.start()

    def wait_ready(self, timeout: float | None = None) -> bool:
        """等載入結束（成功或失敗皆算結束）；測試與診斷用。"""
        return self._ready.wait(timeout)

    @property
    def loaded(self) -> bool:
        return self._backend is not None

    def _load(self) -> None:
        try:
            backend, version = load_backend(self._model_dir)
            self.version = version
            self._backend = backend
            logger.info("nlp.legacy 載入完成：version=%s", version)
        except BaseException as exc:
            self._load_error = type(exc).__name__
            logger.exception("nlp.legacy 載入失敗，之後一律回預設")
        finally:
            self._ready.set()

    # ---- 提議 ----

    def propose(self, candidate: TurnCandidate) -> Proposal:
        if not is_english(candidate.text):
            self.metrics["nlp_lang_unsupported"] += 1
            return _shadow_fallback(candidate, "nlp_lang_unsupported")
        backend = self._backend
        if backend is None:
            reason = "nlp_load_failed" if self._load_error else "nlp_warming_up"
            self.metrics[reason] += 1
            return _shadow_fallback(candidate, reason)
        proposal = map_features(candidate, backend.analyze(candidate.text), version=self.version)
        self.metrics["nlp_proposed"] += 1
        return proposal


def create_nlp_proposer(config: DecisionConfig | None = None, settings: object | None = None, **_: object) -> NlpLegacyProposer:
    """registry 工廠：模型目錄來自 ``UEP_NLP_MODEL_DIR``（未設＝repo 相對的 ``models/nlp``）。"""
    from uep.config import get_settings

    st = settings if settings is not None else get_settings()
    return NlpLegacyProposer(model_dir=resolve_model_dir(getattr(st, "nlp_model_dir", None)))
