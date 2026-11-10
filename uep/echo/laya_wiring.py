"""``laya.shadow``：Laya 決策模型的 shadow proposer 殼（決策中樞 P4）。

只做 shadow：預設不啟用，使用者把 ``laya.shadow`` 放進 ``UEP_DECISION_SHADOW_PROPOSERS`` 才會組裝。
``laya`` 套件只在這個模組內延遲 import；沒有安裝時，工廠建立 proposer 當下就拋 ``ValueError``
（啟動即報錯，不靜默退回）。本模組不綁定 laya 的特定版本：只用 ``laya.load(model_id, device=...)`` 與
``agent.predict(state, questions)``，回傳以 ``answers.<題>.choice／probabilities／noul`` 讀取。

載入策略：模型載入需要 10 秒以上，不能在熱路徑同步發生。proposer 建構時就啟動背景 warmup 執行緒；
warmup 期間（或載入失敗後）``propose`` 立即回 D80 預設（``proposer_id=laya.shadow``、``reason_code`` 標明原因）
並累計 ``metrics``，不會阻塞 ``ProposerGuard`` 的預算。

權重位置：比照 TTS 權重慣例放在使用者目錄，不進 git、不走 HF 快取。``UEP_LAYA_MODEL`` 未設時載入
``<UEP_LAYA_MODELS_DIR>/laya-multilingual``（``UEP_LAYA_MODELS_DIR`` 預設 ``~/.uep/models/laya``），
目錄內是解參照後的 HF snapshot 實體檔（含 ``REVISION`` 檔記錄來源 snapshot 雜湊，作為 ``version`` 的 revision）；
該目錄不存在時工廠建立當下就拋 ``ValueError``，不自動退回 hub 下載。``UEP_LAYA_MODEL`` 明確設成 HF id
或其他路徑則照原樣傳給 ``laya.load``。

隱私：只送出 state 所需欄位（助理名稱、泛稱的說話者、在場人數、本句文字）。``speaker_ref`` 等平台識別、
Risky／秘密欄位一律不進模型；文字只在 shadow 內使用，不進日誌。
"""
from __future__ import annotations

import importlib.util
import logging
import re
import sys
import threading
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

from uep.turn.decision_config import DecisionConfig
from uep.turn.models import Proposal, ReplyMode, TurnCandidate
from uep.turn.proposer import ROUTES, fallback_proposal

logger = logging.getLogger("uep.echo.laya_wiring")

LAYA_PROPOSER_ID = "laya.shadow"
DEFAULT_LAYA_MODEL = "convaiinnovations/laya-multilingual"  # HF hub id；只在使用者明確設定時使用
LAYA_MODEL_SUBDIR = "laya-multilingual"  # <UEP_LAYA_MODELS_DIR> 下的預設模型目錄名
LAYA_REVISION_FILE = "REVISION"  # 模型目錄內記錄來源 snapshot 雜湊的檔案
DEFAULT_LAYA_DEVICE = "cuda"
_UNSAFE = re.compile(r"[^A-Za-z0-9_.:\-]")

# 問題集（laya 的 choice／noul 題型）；選項說明是給零樣本模型看的
QUESTIONS: dict[str, dict[str, Any]] = {
    "reply_mode": {
        "type": "choice",
        "instructions": "根據這段對話，助理此刻應該採取哪種回應模式？",
        "criteria": {
            "respond": "助理被叫到或被詢問，應該立即回答",
            "passive": "助理沒被叫到，但可在背景聆聽、僅在必要時簡短插話",
            "pass": "與助理無關的對話，保持沉默不回應",
            "defer": "資訊不足或有風險，需要先向使用者確認或延後處理",
        },
    },
    "route": {
        "type": "choice",
        "instructions": "這則訊息該交給哪條處理路徑？",
        "criteria": {
            "direct_chat": "一般聊天或簡單問答，直接用對話回覆即可",
            "agent_loop": "需要多步推理、規劃或多次工具呼叫的複雜任務",
            "task_request": "單一明確的指令或工具請求（開關裝置、查行事曆、寄信等）",
            "ignore": "不是對助理說的話，應忽略",
        },
    },
    "addressed": {
        "type": "noul",
        "instructions": "這則訊息是對助理說的（直接對助理說話或延續與助理的對話）嗎？",
    },
}


def laya_models_dir(configured: str | None = None) -> Path:
    """Laya 權重根目錄：``UEP_LAYA_MODELS_DIR`` 優先，否則 ``~/.uep/models/laya``。"""
    raw = (configured or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".uep" / "models" / "laya"


def default_laya_model_dir(configured: str | None = None) -> Path:
    """預設的本地模型目錄（``<根>/laya-multilingual``）。"""
    return laya_models_dir(configured) / LAYA_MODEL_SUBDIR


def read_source_revision(model_dir: Path) -> str | None:
    """讀模型目錄內記錄的來源 revision；沒有該檔或為空回 None。"""
    try:
        text = (model_dir / LAYA_REVISION_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def laya_available() -> bool:
    """laya 套件是否可 import（不實際載入；sys.modules 內已有者視為可用）。"""
    if "laya" in sys.modules:
        return True
    try:
        return importlib.util.find_spec("laya") is not None
    except (ImportError, ValueError):
        return False


def _safe_code(value: str) -> str:
    return _UNSAFE.sub("_", value)[:64] or "unknown"


def resolve_device(requested: str, cuda_available: bool | None = None) -> str:
    """``cuda`` 不可用時退回 ``cpu``；其他值原樣使用。``cuda_available`` 僅供測試注入。"""
    if requested.lower() != "cuda":
        return requested
    if cuda_available is None:
        try:
            import torch

            cuda_available = bool(torch.cuda.is_available())
        except Exception:  # noqa: BLE001 - 沒有 torch 或 CUDA 初始化失敗都退回 cpu
            cuda_available = False
    return "cuda" if cuda_available else "cpu"


def build_state(candidate: TurnCandidate, assistant_name: str) -> dict[str, str]:
    """TurnCandidate -> laya 的 state dict。不送 speaker_ref、事件／會話 ID 與任何 Risky／秘密欄位。

    候選沒有對話歷史，``recent_context`` 固定為無；人數未知視為單人（與 D80 一致）。
    """
    count = candidate.participant_count
    others = f"另有 {count - 1} 人在場" if count is not None and count > 1 else "無"
    return {
        "assistant_name": assistant_name,
        "speaker": "使用者",
        "other_people_present": others,
        "recent_context": "（無）",
        "message": candidate.text,
    }


def _shadow_fallback(candidate: TurnCandidate, reason_code: str) -> Proposal:
    """warmup／載入失敗期間的預設：D80 判斷，但標成 laya.shadow、confidence 0，方便在 shadow 記錄中辨識。"""
    base = fallback_proposal(candidate, reason_code)
    return replace(base, proposer_id=LAYA_PROPOSER_ID, proposer_version="warming")


class LayaProposer:
    """Laya 零樣本決策模型的 shadow proposer（reply_mode／route／addressed 三題）。"""

    proposer_id = LAYA_PROPOSER_ID

    def __init__(
        self,
        *,
        model_id: str = DEFAULT_LAYA_MODEL,
        revision: str | None = None,
        device: str = DEFAULT_LAYA_DEVICE,
        assistant_name: str = "U.E.P",
        source_revision: str | None = None,
        autoload: bool = True,
    ) -> None:
        if not laya_available():
            raise ValueError(
                "laya.shadow 需要 laya 套件，但目前環境未安裝；請安裝後再啟用，"
                "或從 UEP_DECISION_SHADOW_PROPOSERS 移除 laya.shadow"
            )
        self._model_id = model_id
        self._revision = revision or None
        self._source_revision = source_revision or None  # 本地載入時的來源 revision，只用於 version 標示
        self._device = device
        self._assistant_name = assistant_name
        self.version = "warming"
        self.metrics: Counter[str] = Counter()
        self._agent: Any = None
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
        self._thread = threading.Thread(target=self._load, name="uep-laya-warmup", daemon=True)
        self._thread.start()

    def wait_ready(self, timeout: float | None = None) -> bool:
        """等載入結束（成功或失敗皆算結束）；測試與診斷用。"""
        return self._ready.wait(timeout)

    @property
    def loaded(self) -> bool:
        return self._agent is not None

    def _load(self) -> None:
        try:
            import laya

            device = resolve_device(self._device)
            kwargs: dict[str, Any] = {"device": device}
            if self._revision:
                kwargs["revision"] = self._revision
            agent = laya.load(self._model_id, **kwargs)
            revision = getattr(agent, "revision", None) or self._revision or self._source_revision or "unpinned"
            self.version = _safe_code(f"laya-{getattr(laya, '__version__', 'unknown')}:{revision}")
            self._agent = agent
            logger.info("laya.shadow 載入完成：model=%s device=%s version=%s", self._model_id, device, self.version)
        except BaseException as exc:
            self._load_error = type(exc).__name__
            logger.exception("laya.shadow 載入失敗，之後一律回預設")
        finally:
            self._ready.set()

    # ---- 提議 ----

    def propose(self, candidate: TurnCandidate) -> Proposal:
        agent = self._agent
        if agent is None:
            reason = "laya_load_failed" if self._load_error else "laya_warming_up"
            self.metrics[reason] += 1
            return _shadow_fallback(candidate, reason)
        result = agent.predict(build_state(candidate, self._assistant_name), QUESTIONS)
        answers = result["answers"]
        reply = answers["reply_mode"]
        route = answers["route"]["choice"]
        if route not in ROUTES:
            raise ValueError(f"laya 回傳未知的 route：{route!r}")
        mode = ReplyMode(reply["choice"])  # 未知值拋 ValueError，由 shadow 記為失敗
        probs = reply.get("probabilities")
        confidence = float(probs.get(reply["choice"], 0.0)) if isinstance(probs, dict) else 0.0
        self.metrics["laya_proposed"] += 1
        return Proposal(
            reply_mode=mode, addressed=float(answers["addressed"]["noul"]) >= 0.5,
            confidence=min(1.0, max(0.0, confidence)), route=route, reason_code="laya_model",
            proposer_id=self.proposer_id, proposer_version=self.version,
        )


def create_laya_proposer(config: DecisionConfig | None = None, settings: object | None = None, **_: object) -> LayaProposer:
    """registry 工廠：組態來自 ``UEP_LAYA_MODELS_DIR``／``UEP_LAYA_MODEL``／``UEP_LAYA_REVISION``／``UEP_LAYA_DEVICE``。

    ``UEP_LAYA_MODEL`` 未設時用預設本地目錄；該目錄不存在就在這裡拋 ``ValueError``（不退回 hub）。
    """
    from uep.config import get_settings

    st = settings if settings is not None else get_settings()
    cfg = config or DecisionConfig()
    explicit = (getattr(st, "laya_model", None) or "").strip()
    revision = (getattr(st, "laya_revision", None) or "").strip() or None
    source_revision: str | None = None
    if explicit:
        model_id = explicit
    else:
        local = default_laya_model_dir(getattr(st, "laya_models_dir", None))
        if not local.is_dir():
            raise ValueError(
                f"laya.shadow 找不到本地模型目錄：{local}；請把 laya-multilingual 權重放在該處，"
                "或設定 UEP_LAYA_MODELS_DIR／UEP_LAYA_MODEL，或從 UEP_DECISION_SHADOW_PROPOSERS 移除 laya.shadow"
            )
        model_id = str(local)
        source_revision = read_source_revision(local)
        revision = None  # 本地目錄沒有 hub revision 可固定
    return LayaProposer(
        model_id=model_id,
        revision=revision,
        device=(getattr(st, "laya_device", None) or "").strip() or DEFAULT_LAYA_DEVICE,
        assistant_name=cfg.bot_identity.primary_name,
        source_revision=source_revision,
    )
