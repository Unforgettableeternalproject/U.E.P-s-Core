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

輸入英文化：題目與 state 字面值全英文，定義在 ``uep.decision.laya_schema``（訓練工具共用）。
``UEP_DECISION_TRANSLATE`` 開啟時，本句文字與上一輪脈絡先經 Boundary Translator（``translate_wiring``）翻成英文
才送進模型；翻譯 unavailable 時預設回 ``reason_code=translate_unavailable`` 的 shadow 預設（訓練只用英文），
可設 ``UEP_TRANSLATE_ON_UNAVAILABLE=original`` 改送原文。翻譯在 propose 內同步進行（shadow 不在主路徑預算內）。
關閉（預設）時 Laya 收原文。

已知競態（接受）：上一輪脈絡（Q2）在回合完成後由背景執行緒翻譯；若下一句 propose 搶在翻譯完成前到，會用舊的脈絡。

隱私：只送出 state 所需欄位（助理名稱、泛稱的說話者、在場人數、近期脈絡、本句文字）。``speaker_ref`` 等平台識別、
Risky／秘密欄位一律不進模型；文字只在 shadow 內使用，不進日誌。
"""
from __future__ import annotations

import importlib.util
import logging
import re
import sys
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from uep.decision.laya_schema import (
    CONTEXT_MAX_CHARS,
    NO_CONTEXT,
    QUESTIONS,
    WARMUP_MESSAGE,
    build_state_dict,
    format_recent_context,
)
from uep.echo.translate_wiring import BoundaryTranslator, create_boundary_translator
from uep.turn.decision_config import DecisionConfig
from uep.turn.models import Proposal, ReplyMode, TurnCandidate, TurnOutcomeFacts
from uep.turn.proposer import (
    ROUTES,
    Interjection,
    ProposerGuard,
    ShadowDiff,
    fallback_proposal,
    validate_proposal,
)
from uep.turn.rule_proposer import RuleProposer

logger = logging.getLogger("uep.echo.laya_wiring")

LAYA_PROPOSER_ID = "laya.shadow"
DEFAULT_LAYA_MODEL = "convaiinnovations/laya-multilingual"  # HF hub id；只在使用者明確設定時使用
LAYA_MODEL_SUBDIR = "laya-multilingual"  # <UEP_LAYA_MODELS_DIR> 下的預設模型目錄名
LAYA_REVISION_FILE = "REVISION"  # 模型目錄內記錄來源 snapshot 雜湊的檔案
DEFAULT_LAYA_DEVICE = "cuda"
_UNSAFE = re.compile(r"[^A-Za-z0-9_.:\-]")

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


def build_state(
    candidate: TurnCandidate, assistant_name: str, *, message: str | None = None, recent_context: str = NO_CONTEXT,
    capabilities: Sequence[str] | None = None,
) -> dict[str, str]:
    """TurnCandidate -> laya 的 state dict。不送 speaker_ref、事件／會話 ID 與任何 Risky／秘密欄位。

    ``message`` 未給時用候選原文；翻譯開啟時由 proposer 傳入英文。人數未知視為單人（與 D80 一致）。
    """
    return build_state_dict(
        assistant_name=assistant_name, message=candidate.text if message is None else message,
        participant_count=candidate.participant_count, recent_context=recent_context, capabilities=capabilities,
    )


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
        translator: BoundaryTranslator | None = None,
        on_translate_unavailable: str = "fallback",
        autoload: bool = True,
        capabilities: Sequence[str] | None = None,
    ) -> None:
        if not laya_available():
            raise ValueError(
                "laya.shadow 需要 laya 套件，但目前環境未安裝；請安裝後再啟用，"
                "或從 UEP_DECISION_SHADOW_PROPOSERS 移除 laya.shadow"
            )
        self._model_id = model_id
        # 可用能力清單：None＝laya_schema.DEFAULT_CAPABILITIES；工具層清單接上後由組裝層傳入（接點：這個參數）
        self._capabilities = tuple(capabilities) if capabilities is not None else None
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
        # Boundary Translator（None＝關閉，Laya 收原文）與 Q2 的上一輪脈絡（英文，行程內，不進日誌）
        self._translator = translator
        self._send_original_on_unavailable = on_translate_unavailable == "original"
        self._recent_text: OrderedDict[str, str] = OrderedDict()  # candidate_id -> 使用者原文，等 outcome 配對
        # 上一輪脈絡：英文（使用者句、她的回覆）與上一輪使用者的 speaker_ref 集合；只在行程內比對，不送出、不入 state
        self._context_parts: tuple[str | None, str | None] | None = None
        self._context_speakers: frozenset[str] = frozenset()
        self._context_lock = threading.Lock()
        self._context_threads: list[threading.Thread] = []
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
            if self._translator is not None:
                # 必須等 laya.load 返回才啟動翻譯器：兩邊同時第一次 import transformers 的延遲載入不是執行緒安全的
                #（實測並行時 laya 會 ImportError: AutoTokenizer）；之後翻譯器與 Laya 的假推論並行
                self._translator.start_warmup()
            # 假推論：CUDA 冷啟動（實測第一次約 16 秒）要在 warmup 內發生，ready 之後才算可用
            agent.predict(
                build_state_dict(assistant_name=self._state_assistant_name(), message=WARMUP_MESSAGE, capabilities=self._capabilities), QUESTIONS,
            )
            if self._translator is not None:
                self._translator.wait_ready()  # 翻譯器失敗不擋 Laya，propose 會走 translate_unavailable
            revision = getattr(agent, "revision", None) or self._revision or self._source_revision or "unpinned"
            self.version = _safe_code(f"laya-{getattr(laya, '__version__', 'unknown')}:{revision}")
            self._agent = agent
            logger.info("laya.shadow 載入完成：model=%s device=%s version=%s", self._model_id, device, self.version)
        except BaseException as exc:
            self._load_error = type(exc).__name__
            logger.exception("laya.shadow 載入失敗，之後一律回預設")
        finally:
            self._ready.set()

    def _state_assistant_name(self) -> str:
        """翻譯開啟時助理名稱用佔位英文名（與譯文內被替換的別名一致）。"""
        return self._translator.placeholder if self._translator is not None else self._assistant_name

    # ---- Q2：上一輪脈絡 ----

    def on_turn_outcome(self, facts: TurnOutcomeFacts) -> None:
        """OutcomeObserver：回合完成後記下「她的上一句回覆＋使用者上一句」，背景翻成英文給下一次 propose 用。

        同時記下上一輪的 speaker_ref 集合（僅行程內比對）；propose 時與 ``candidate.speakers`` 比對：
        只要有任一位相同即視為同一說話者（標 ``User``），否則標 ``Another user``；任一邊沒有 speaker 資料則視為相同。
        """
        with self._context_lock:
            user_text = self._recent_text.pop(facts.candidate_id, "")
        if self._translator is None or facts.status != "completed" or not facts.reply_text.strip():
            return
        thread = threading.Thread(
            target=self._refresh_context, args=(user_text, facts.reply_text, frozenset(facts.speakers)), name="uep-laya-context", daemon=True,
        )
        self._context_threads = [t for t in self._context_threads if t.is_alive()] + [thread]
        thread.start()

    def _refresh_context(self, user_text: str, reply_text: str, speakers: frozenset[str] = frozenset()) -> None:
        translator = self._translator
        if translator is None:
            return

        def to_en(text: str) -> str | None:
            text = text.strip()[:CONTEXT_MAX_CHARS]
            if not text:
                return None
            return translator.translate(text, timeout_ms=2000).text_en  # 背景工作，逾時放寬

        try:
            parts = (to_en(user_text), to_en(reply_text))
        except Exception:
            logger.exception("laya.shadow 脈絡翻譯失敗")
            return
        with self._context_lock:
            self._context_parts = parts
            self._context_speakers = speakers

    def on_audience_changed(self) -> None:
        """audience 切換：丟掉上一輪脈絡與待配對原文，避免舊 audience 的內容進新 audience 的 state。"""
        with self._context_lock:
            self._context_parts = None
            self._context_speakers = frozenset()
            self._recent_text.clear()

    def wait_context(self, timeout: float | None = None) -> None:
        """等背景脈絡翻譯結束；測試與診斷用。"""
        for thread in list(self._context_threads):
            thread.join(timeout)

    # ---- 提議 ----

    def propose(self, candidate: TurnCandidate) -> Proposal:
        agent = self._agent
        if agent is None:
            reason = "laya_load_failed" if self._load_error else "laya_warming_up"
            self.metrics[reason] += 1
            return _shadow_fallback(candidate, reason)
        assistant, message, context = self._assistant_name, candidate.text, NO_CONTEXT
        if self._translator is not None:
            translated = self._translator.translate(candidate.text)
            if translated.text_en is not None:
                assistant, message = self._translator.placeholder, translated.text_en
                with self._context_lock:
                    if self._context_parts is not None:
                        prev = self._context_speakers
                        same = not prev or not candidate.speakers or bool(prev & set(candidate.speakers))
                        context = format_recent_context(*self._context_parts, same_speaker=same)
            else:
                self.metrics["translate_unavailable"] += 1
                if not self._send_original_on_unavailable:
                    return _shadow_fallback(candidate, "translate_unavailable")
            with self._context_lock:
                self._recent_text[candidate.candidate_id] = candidate.text
                while len(self._recent_text) > 16:
                    self._recent_text.popitem(last=False)
        state = build_state(candidate, assistant, message=message, recent_context=context, capabilities=self._capabilities)
        result = agent.predict(state, QUESTIONS)
        answers = result["answers"]
        reply = answers["reply_mode"]
        route = answers["route"]["choice"]
        if route not in ROUTES:
            raise ValueError(f"laya 回傳未知的 route：{route!r}")
        mode = ReplyMode(reply["choice"])  # 未知值拋 ValueError，由 shadow 記為失敗
        probs = reply.get("probabilities")
        confidence = float(probs.get(reply["choice"], 0.0)) if isinstance(probs, dict) else 0.0
        self.metrics["laya_proposed"] += 1
        # compound（是否含兩個以上獨立指示）讀出後放進提案欄位並計數，不影響本次提案內容；拆分與連鎖由後續流程處理
        compound = _read_compound(answers)
        if compound:
            self.metrics["laya_compound_yes"] += 1
        return Proposal(
            reply_mode=mode, addressed=float(answers["addressed"]["noul"]) >= 0.5,
            confidence=min(1.0, max(0.0, confidence)), route=route, reason_code="laya_model",
            proposer_id=self.proposer_id, proposer_version=self.version, compound=compound,
        )


def _read_compound(answers: dict) -> bool | None:
    """讀 compound 題的答案（noul 機率 >= 0.5 視為 yes）；舊模型或答案缺漏回 None。"""
    try:
        return float(answers["compound"]["noul"]) >= 0.5
    except (KeyError, TypeError, ValueError):
        return None


def read_absent_compound(answers: dict) -> bool | None:
    """讀 ``absent.compound`` 題的答案（noul 機率 >= 0.5 視為 yes）；舊模型或答案缺漏回 None。"""
    try:
        return float(answers["absent.compound"]["noul"]) >= 0.5
    except (KeyError, TypeError, ValueError):
        return None


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
    translator = None
    if getattr(st, "decision_translate", False):
        translator = create_boundary_translator(st, extra_aliases=cfg.bot_identity.all_names)
    on_unavailable = (getattr(st, "translate_on_unavailable", None) or "").strip().lower()
    return LayaProposer(
        translator=translator,
        on_translate_unavailable="original" if on_unavailable == "original" else "fallback",
        model_id=model_id,
        revision=revision,
        device=(getattr(st, "laya_device", None) or "").strip() or DEFAULT_LAYA_DEVICE,
        assistant_name=cfg.bot_identity.primary_name,
        source_revision=source_revision,
    )


# ---------------------------------------------------------------------------
# laya.primary：Laya 為主、舊規則為底的組合式 proposer
# ---------------------------------------------------------------------------

LAYA_PRIMARY_ID = "laya.primary"
# 每一句是誰決定的（寫進 Proposal.reason_code 的前綴，經決策日誌 primary 列的 reason_code 欄留存；
# fallback 的完整格式為 ``<label>:<規則原本的原因碼>``）
SOURCE_LAYA = "laya_model"  # Laya 決定（沿用 LayaProposer 的 reason_code，不加後綴）
SOURCE_RULES_TIMEOUT = "rules_fallback_timeout"  # 翻譯／預算逾時、翻譯不可用、推論例外或提案無效：舊規則
SOURCE_RULES_WARMING = "rules_fallback_warming"  # Laya 尚未載入完成：舊規則
SOURCE_RULES_LOAD_FAILED = "rules_fallback_load_failed"  # Laya 載入失敗（之後一律退回，需要人處理）：舊規則
_VERDICT_KEEP = 32  # 保留最近多少句的 Laya 判斷（供 tick 比對最後一句）
_INNER_MARGIN_S = 0.03  # 內層等待 Laya 的期限比外層預算提早這麼多，讓外層還來得及收到舊規則的結果
_LISTENING = "LISTENING"


class LayaRulesProposer:
    """``laya.primary``：每句先讓舊規則 ``RuleProposer`` 跑（餵 buffer 與耦合度，也是 Laya 不可用時的底），
    再問 Laya；Laya 有結果就以它的 reply_mode／route／addressed／compound 為準，否則回舊規則的提案。

    - 衝突時以 Laya 為準：舊規則只決定「什麼時候可以插話」（``tick``）；Laya 對該插話對應的最後一句判 pass／ignore 時丟棄。
      只管針對對話內容的插話：``RuleProposer.tick`` 只由被動模式的 engagement buffer 產生，計時、提醒、事件觸發的主動輸出
      走 ``proactive.initiate``／schedule，根本不經過 proposer，所以不受影響。
    - 決策來源三種（見 ``SOURCE_*``），寫進 reason_code 前綴並累計 ``metrics``。
    - 舊規則與 Laya 在 reply_mode／route／addressed 不一致時，留一筆 ``ShadowDiff``，由 ``TurnReplyEngine`` 補上決策 ID 後
      走既有 shadow 紀錄與決策日誌（只有模式與原因碼，無原文）。
    - 內層以自己的 ``ProposerGuard`` 等 Laya，期限取自 ``candidate.deadline``（外層預算）扣掉小邊界，
      逾時時外層仍有時間收到舊規則的結果，而不是整個掉到 D80 預設。
    """

    proposer_id = LAYA_PRIMARY_ID
    uses_laya_budget = True  # 讓 Orchestrator 對 turn.reply 套用 DecisionConfig.laya_budget_ms

    def __init__(self, laya: LayaProposer, rules: RuleProposer, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._laya = laya
        self._rules = rules
        self._clock = clock
        self._guard = ProposerGuard("laya-primary-inner")
        self.metrics: Counter[str] = Counter()
        self._sink: Counter[str] | None = None
        self._lock = threading.Lock()
        self._verdicts: OrderedDict[str, tuple[ReplyMode, str]] = OrderedDict()  # 最後一個 utterance ref -> (mode, route)
        self._diffs: OrderedDict[str, ShadowDiff] = OrderedDict()  # candidate_id -> 差異，等 engine 取走

    # ---- 身分與委派給規則的唯讀介面 ----

    @property
    def version(self) -> str:
        return self._laya.version if self._laya.loaded else self._rules.version

    @property
    def laya(self) -> LayaProposer:
        return self._laya

    @property
    def rules(self) -> RuleProposer:
        return self._rules

    @property
    def wake(self) -> Any:
        return self._rules.wake

    @property
    def channel_mode(self) -> Any:
        return self._rules.channel_mode

    @property
    def coupling_value(self) -> float:
        return self._rules.coupling_value

    @property
    def topic_term(self) -> float:
        return self._rules.topic_term

    @property
    def pending_topics(self) -> int:
        return self._rules.pending_topics

    def add_mode_listener(self, listener: Callable[[Any], None]) -> None:
        self._rules.add_mode_listener(listener)

    def set_participant_count(self, count: int) -> None:
        self._rules.set_participant_count(count)

    def set_mode_override(self, mode: Any) -> Any:
        return self._rules.set_mode_override(mode)

    def is_listener_continuation(self, text: str) -> bool:
        return self._rules.is_listener_continuation(text)

    def reset(self, user_count: int = 0) -> None:
        self._rules.reset(user_count)
        self._clear_laya_state()

    def on_audience_changed(self) -> None:
        self._rules.on_audience_changed()
        self._clear_laya_state()

    def on_participant_left(self, speaker_ref: str) -> None:
        self._rules.on_participant_left(speaker_ref)

    def _clear_laya_state(self) -> None:
        with self._lock:
            self._verdicts.clear()
            self._diffs.clear()
        self._laya.on_audience_changed()

    def close(self) -> None:
        self._guard.close()

    def bind_metrics(self, sink: Counter[str]) -> None:
        """把計數同步進 Orchestrator 的 metrics（鍵加 ``laya_primary.`` 前綴）；由 ``TurnReplyEngine`` 建構時呼叫。"""
        self._sink = sink

    def _bump(self, name: str) -> None:
        self.metrics[name] += 1
        if self._sink is not None:
            self._sink[f"laya_primary.{name}"] += 1

    # ---- TurnProposer ----

    def propose(self, candidate: TurnCandidate) -> Proposal:
        started = self._clock()
        # 規則一定先跑：它要餵 buffer 與耦合度，也是 Laya 不可用時的底。規則的例外不拖累 Laya 的結果
        rules_prop: Proposal | None = None
        rules_exc: Exception | None = None
        try:
            rules_prop = self._rules.propose(candidate)
        except Exception as exc:
            rules_exc = exc
            self._bump("rules_error")
            logger.exception("laya.primary：規則 proposer 失敗")
        laya_prop, fallback_source = self._ask_laya(candidate)
        if laya_prop is not None:
            merged = self._merge(candidate, laya_prop)
            self._remember(candidate, merged, rules_prop, int((self._clock() - started) * 1000))
            self._bump(SOURCE_LAYA)
            return merged
        if rules_prop is None:
            raise rules_exc if rules_exc is not None else RuntimeError("laya.primary：Laya 與規則都沒有結果")
        self._bump(fallback_source)
        return replace(
            rules_prop, proposer_id=LAYA_PRIMARY_ID,
            reason_code=_safe_code(f"{fallback_source}:{rules_prop.reason_code}")[:64],
        )

    def _ask_laya(self, candidate: TurnCandidate) -> tuple[Proposal | None, str]:
        """問 Laya：回 ``(提案, "")``；不可用時回 ``(None, 退回來源標籤)``。"""
        remaining: float | None = None
        if candidate.deadline is not None:
            remaining = candidate.deadline - self._clock() - _INNER_MARGIN_S
            if remaining <= 0:
                self._bump("laya_no_time")
                return None, SOURCE_RULES_TIMEOUT
        result, status = self._guard.call(self._laya.propose, candidate, remaining)
        # 外層預算逾時（卡在規則或 guard 之外）時 engine 直接回 D80 預設，規則的待結算話題項不會被結算，
        # 只會在 _PENDING_MAX 上限內自然被擠掉；這裡的內層期限已讓 Laya 逾時走不到這一步，故不另處理
        if status != "ok":
            self._bump(f"laya_{status}")  # timeout／busy／error
            return None, SOURCE_RULES_TIMEOUT
        if not isinstance(result, Proposal):
            self._bump("laya_invalid")
            return None, SOURCE_RULES_TIMEOUT
        if result.reason_code != SOURCE_LAYA:  # LayaProposer 的 shadow 預設：尚未載入、載入失敗或翻譯不可用
            if result.reason_code == "laya_warming_up":
                return None, SOURCE_RULES_WARMING
            if result.reason_code == "laya_load_failed":
                return None, SOURCE_RULES_LOAD_FAILED
            return None, SOURCE_RULES_TIMEOUT
        if validate_proposal(self._merge(candidate, result)) is not None:
            self._bump("laya_invalid")
            return None, SOURCE_RULES_TIMEOUT
        return result, ""

    def _merge(self, candidate: TurnCandidate, laya_prop: Proposal) -> Proposal:
        """以 Laya 的 reply_mode／route／addressed／compound 為準；Interaction 軸沿用規則 pass 的慣例（IDLE 才轉 LISTENING）。"""
        listening = _LISTENING if laya_prop.reply_mode is ReplyMode.PASS and candidate.interaction_state == "IDLE" else None
        return replace(laya_prop, proposer_id=LAYA_PRIMARY_ID, interaction_to=listening)

    def _remember(self, candidate: TurnCandidate, merged: Proposal, rules_prop: Proposal | None, elapsed_ms: int) -> None:
        """記下 Laya 對這一句的判斷（供 tick 比對）、與規則的差異（供 engine 取走），並在 Laya 判 pass 時結算規則的待結算話題項。"""
        if candidate.utterance_refs:
            with self._lock:
                self._verdicts[candidate.utterance_refs[-1]] = (merged.reply_mode, merged.route)
                while len(self._verdicts) > _VERDICT_KEEP:
                    self._verdicts.popitem(last=False)
        if rules_prop is None:
            return
        fields = tuple(
            name for name, differs in (
                ("mode", merged.reply_mode is not rules_prop.reply_mode),
                ("route", merged.route != rules_prop.route),
                ("addressed", merged.addressed != rules_prop.addressed),
            ) if differs
        )
        if fields:
            self._bump("shadow_diff")
            diff = ShadowDiff(
                shadow_id=getattr(self._rules, "proposer_id", "rules.default"), primary=merged, shadow=rules_prop,
                fields=fields, elapsed_ms=elapsed_ms,
            )
            with self._lock:
                self._diffs[candidate.candidate_id] = diff
                while len(self._diffs) > _VERDICT_KEEP:
                    self._diffs.popitem(last=False)
        if merged.reply_mode in (ReplyMode.PASS, ReplyMode.DEFER) and rules_prop.reply_mode is not ReplyMode.PASS:
            # 規則提議回應但 Laya 判 pass／defer：Orchestrator 不開回合，規則等不到 outcome，主動模式下的話題項會一直待結算。
            # speakers 刻意留空：規則的 _passed／_failed 會清掉說話者的「生成中」標記與窗口，
            # 而這位說話者可能另有真正進行中的回合，不能被合成的結果誤清（只結算話題項）。
            # pass＝LLM 判 PASS，耦合度下降；defer＝沒有開始（started=False），不增不減
            is_pass = merged.reply_mode is ReplyMode.PASS
            self._feed_rules_outcome(TurnOutcomeFacts(
                turn_id="", candidate_id=candidate.candidate_id, speakers=(),
                reply_mode=merged.reply_mode.value, reason_code=rules_prop.reason_code, status="skipped",
                end_reason="pass" if is_pass else "laya_defer", started=is_pass,
            ))

    def take_shadow_diff(self, candidate_id: str) -> ShadowDiff | None:
        """取走（並移除）這一句與規則的差異；沒有差異回 None。"""
        with self._lock:
            return self._diffs.pop(candidate_id, None)

    # ---- tick：規則決定何時可插話，Laya 對最後一句判 pass／ignore 時不插 ----

    def tick(self) -> list[Interjection]:
        interjections = self._rules.tick()
        if not interjections:
            return interjections
        kept: list[Interjection] = []
        for ij in interjections:
            if self._laya_vetoes(ij):
                self._bump("interjection_dropped_by_laya")
                self._release_dropped(ij)
            else:
                kept.append(ij)
        return kept

    def _laya_vetoes(self, ij: Interjection) -> bool:
        if not ij.utterance_refs:
            return False
        with self._lock:
            verdict = self._verdicts.get(ij.utterance_refs[-1])
        # pass／ignore：與她無關；defer：她在等使用者確認，不該自己插話
        return verdict is not None and (verdict[0] in (ReplyMode.PASS, ReplyMode.DEFER) or verdict[1] == "ignore")

    def _release_dropped(self, ij: Interjection) -> None:
        """被丟棄的插話：解除規則的 in-flight 並套用冷卻（等同「沒能開始」），避免下個 tick 立刻重提同一批。"""
        self._feed_rules_outcome(TurnOutcomeFacts(
            turn_id="", candidate_id="tick:laya_drop", speakers=ij.speakers, reply_mode=ij.proposal.reply_mode.value,
            reason_code=ij.proposal.reason_code, status="skipped", end_reason="laya_pass", started=False,
        ))

    # ---- OutcomeObserver：started 只給規則；outcome 兩邊都收 ----

    def on_turn_started(self, turn_id: str, candidate_id: str, speakers: tuple[str, ...]) -> None:
        self._rules.on_turn_started(turn_id, candidate_id, speakers)

    def on_turn_outcome(self, facts: TurnOutcomeFacts) -> None:
        self._feed_rules_outcome(facts)
        try:
            self._laya.on_turn_outcome(facts)
        except Exception:
            logger.exception("laya.primary：Laya on_turn_outcome 失敗")

    def _feed_rules_outcome(self, facts: TurnOutcomeFacts) -> None:
        try:
            self._rules.on_turn_outcome(facts)
        except Exception:
            logger.exception("laya.primary：規則 on_turn_outcome 失敗")


def create_laya_primary_proposer(
    config: DecisionConfig | None = None, settings: object | None = None, **kwargs: object
) -> LayaRulesProposer:
    """registry 工廠（``laya.primary``）：Laya 設定同 ``create_laya_proposer``，規則部分用同一份 ``DecisionConfig``。"""
    cfg = config or DecisionConfig()
    laya = create_laya_proposer(cfg, settings)
    rules_kwargs = {k: v for k, v in kwargs.items() if k in ("clock", "on_mode_change")}
    return LayaRulesProposer(laya, RuleProposer(cfg, **rules_kwargs))  # type: ignore[arg-type]
