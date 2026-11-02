"""Memory adapter：Echo Memory（Phase 4a）。

**這是唯一 import echo_memory 的地方**，其餘程式碼只認
:class:`EchoMemoryAdapter` 的幾個方法（retrieve / store / set_session /
tool_definitions / execute_tool）。

## 為什麼是 adapter 而不是直接用 MemoryEngine

echo_memory 是獨立子系統（EcphoryRAG + Dream Engine + 神經化學狀態），
API 是同步的、回傳自己的 dataclass。Echo Stream 的管線是 asyncio，
think stage 只需要「一段可以塞進 prompt 的文字」。adapter 負責：

* 同步 → 非同步（``run_in_executor``，不擋 event loop）
* ``MemoryContext`` → 截斷過的純文字（token 預算是管線的責任）
* session 邊界（``exclude_session_id`` 防自我回聲）

## ⚠️ 分支依賴

需要 echo_memory repo 在 ``develop`` 分支——
``namespace_id`` / ``exclude_session_id`` / ``llm_fn`` 這些 API
已併入 develop（舊的 spike 分支不再需要；master 落後）。build_engine 會驗 API 形狀，
缺了就給出可行動的錯誤而不是深處的 TypeError。

## 設計決策（見 docs/hidden/phase4-memory-integration-plan.md）

* **namespace 固定、跨會話**——記憶跨會話是它存在的意義。
  session_id 只用於 episode 標記與 exclude。
* **store 是旁路**：失敗 log 一筆，不炸管線。
* **retrieve 也是旁路**：與 LLM prefill 平行跑（§7.4），結果下一輪生效。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import sys
import threading
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..contracts import memory_tools as _tools

logger = logging.getLogger(__name__)

DEFAULT_NAMESPACE = "echo-stream"
DEFAULT_TOP_K = 3
FINE_POOL_WORKERS = 4
"""細粒度模式的 executor 工作緒數（retrieve／store 並行 + dream 佔一條）。"""
DEFAULT_MAX_CHARS = 500
"""注入 prompt 的記憶文字上限。記憶放在 user message 內（保 prompt cache），
太長會吃掉 token 預算、也會影響切分行為——先保守，profiling 後再調。"""
DEFAULT_MIN_MATCH = 0.45
"""檢索相似度下限。echo_memory 預設 0.3，實測 35-41% 的匹配是純雜訊
（「攀岩」撈出「深藍色」），注入只會讓 LLM 亂講。"""
DEFAULT_TOOLS = "deep_recall,force_remember"
"""預設開放給 LLM 的記憶工具（ECHO_STREAM_MEMORY_TOOLS，逗號分隔，``off`` 關閉）。
echo_memory 有四個：deep_recall（純讀）、force_remember（寫一筆高顯著性 episode）、
adjust_importance（改既有 episode 的 salience）、trigger_dream（觸發鞏固）。
後兩個讓模型自主決定風險高，預設不給——要開自己寫進 .env。"""

FORCE_REMEMBER_DESCRIPTION = (
    "只有在使用者明確要求記住時才呼叫（例如「記住」「幫我記」「別忘了」這類明確指示）。"
    "一般對話內容系統每輪都會自動寫入記憶，不需要也不要為此呼叫。"
    "content 寫成一句完整的第三人稱事實。"
    "Only call when the user explicitly asks you to remember something; ordinary "
    "conversation is already stored automatically every turn."
)
"""覆寫 echo_memory 原本偏寬的 force_remember 描述，避免模型把一般事實也當成要強制記憶。"""
DEFAULT_TOOL_MAX_CHARS = _tools.DEFAULT_TOOL_MAX_CHARS
MEMBER_RECALL = _tools.MEMBER_RECALL
MEMBER_RECALL_SCHEMA = _tools.MEMBER_RECALL_SCHEMA
"""member_recall 不在 echo_memory 的 TOOL_DECLARATIONS 裡；schema 與字數上限取自共用契約
（``contracts.memory_tools``，與 Core Access Layer 同一份，N5 去重）。預設不開（DEFAULT_TOOLS 不含它），
由宿主在 ECHO_STREAM_MEMORY_TOOLS 加上。"""
RETRACT_ABOUT_ME = "retract_about_me"
OPT_OUT_ABOUT_ME = "opt_out_about_me"
CORRECT_ABOUT_ME = "correct_about_me"
ABOUT_ME_TOOLS = (RETRACT_ABOUT_ME, OPT_OUT_ABOUT_ME, CORRECT_ABOUT_ME)
"""「關於我」自助工具：對象永遠是說話的人本人（actor 由宿主經 context 傳入，參數不得帶 uid）。
全部預設不開（DEFAULT_TOOLS 不含），要在 ECHO_STREAM_MEMORY_TOOLS 明列才曝光。"""
ABOUT_ME_REF_DESCRIPTION = (
    "要處理的旁證編號 N（member_recall 結果中「〔旁證N〕」那行的數字）。"
    "只能指定你本人那次查詢（查詢對象是目前說話的人）結果裡的旁證；查別人的結果沒有可用編號。"
)
RETRACT_ABOUT_ME_SCHEMA = {
    "name": RETRACT_ABOUT_ME,
    "description": (
        "當使用者明確要求撤回「別人說的關於他自己」的某一條旁證時呼叫（例如「那句別人說我的話，"
        "請不要再提」）。ref 填 member_recall 結果裡該行的旁證編號。只影響之後的檢索，"
        "不會改寫既有回覆。使用者沒有明確要求就不要呼叫。"
        "Call only when the user explicitly asks to retract one statement others made about them."
    ),
    "parameters": {
        "type": "object",
        "properties": {"ref": {"type": "integer", "description": ABOUT_ME_REF_DESCRIPTION}},
        "required": ["ref"],
    },
}
OPT_OUT_ABOUT_ME_SCHEMA = {
    "name": OPT_OUT_ABOUT_ME,
    "description": (
        "當使用者明確要求「不要再在伺服器裡被提起／被查詢」，或要求恢復時呼叫。"
        "enabled=true 為退出（之後其他人查詢時，關於他的旁證與「最近在玩」那行都不再出現），"
        "false 為恢復。只影響之後的檢索。使用者沒有明確要求就不要呼叫。"
        "Call only on an explicit request to opt out of (or back into) being surfaced to others."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean", "description": "true 退出；false 恢復。"},
        },
        "required": ["enabled"],
    },
}
CORRECT_ABOUT_ME_SCHEMA = {
    "name": CORRECT_ABOUT_ME,
    "description": (
        "當使用者明確指出「別人說我的某條旁證不對」並說出正確說法時呼叫：該條旁證立即被遮蔽，"
        "並把使用者的更正存下。ref 填旁證編號，correction_text 用使用者自己的話寫出更正內容。"
        "只影響之後的檢索。使用者沒有明確更正就不要呼叫。"
        "Call only when the user explicitly says a statement others made about them is wrong."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ref": {"type": "integer", "description": ABOUT_ME_REF_DESCRIPTION},
            "correction_text": {"type": "string", "description": "使用者提供的更正內容。"},
        },
        "required": ["ref", "correction_text"],
    },
}
ABOUT_ME_SCHEMAS = {
    RETRACT_ABOUT_ME: RETRACT_ABOUT_ME_SCHEMA,
    OPT_OUT_ABOUT_ME: OPT_OUT_ABOUT_ME_SCHEMA,
    CORRECT_ABOUT_ME: CORRECT_ABOUT_ME_SCHEMA,
}
ABOUT_UNSUPPORTED = "目前記憶引擎不支援這個操作。"
ABOUT_NO_CONTEXT = "目前無法確認你的身分或所在場域，無法處理。"
ABOUT_NO_REF = "目前無法指定對象，請先查詢相關內容再試。"
ABOUT_BAD_REF = "找不到這個編號對應的內容，請先查詢相關內容再試。"
ABOUT_REASON_TEXT = {
    "already_hidden": "這條內容先前已經處理過了。",
}
MEMBER_NOT_FOUND = "無法確認是哪位成員。"
MEMBER_NAME_MAX_CHARS = _tools.MEMBER_LABEL_MAX_CHARS
ASSESS_MODES = ("llm", "heuristic", "off")
"""salience 評估模式（ECHO_STREAM_MEMORY_ASSESS）：
llm = SubconsciousAssessor 打一次 LLM（背景，不在熱路徑）；
heuristic = echo_memory 的文字啟發式，零 token；off = 全部 0.5（Phase 4a 行為）。"""


_declares_param = _tools.declares_param


def _env(name: str, default: str | None = None) -> str | None:
    """讀環境變數（空字串視為未設）。F1：不再讀 ``.env``——Core 路徑的設定一律由 wiring 明確傳入，這些鍵只是 ES 時代的覆寫入口。"""
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def function_tool_definition(decl: dict[str, Any]) -> dict[str, Any]:
    """echo_memory 的工具宣告（Gemini 格式 dict）-> OpenAI function schema；``force_remember`` 覆寫為收斂過的描述。
    adapter 的 ``tool_definitions`` 與 Core 的 ``EchoMemoryToolBackend.definitions`` 共用這一處。"""
    fn = dict(decl)
    if fn.get("name") == "force_remember":
        fn["description"] = FORCE_REMEMBER_DESCRIPTION
    return {"type": "function", "function": fn}


def _parse_ref(raw: Any) -> int | None:
    """旁證編號：整數、整數值的 float（1.0）、純整數字串可接受；bool 與其他一律 None。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else None
    if isinstance(raw, str):
        text = raw.strip()
        if text.isascii() and text.isdigit():
            return int(text)
    return None


def _parse_top_n(raw: Any) -> int:
    """解析常玩筆數設定；非數字或負數一律視為停用（0）。"""
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        return 0


def _format_last_played(value: Any) -> str:
    """last_played（datetime／ISO 字串／None）→ ``MM/DD``；無法解析回空字串。"""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%m/%d")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        try:
            # Python 3.10 的 fromisoformat 不吃結尾 Z
            return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%m/%d")
        except ValueError:
            return ""
    return ""


def _format_activity_line(items: Iterable[dict[str, Any]] | None) -> str:
    """把 activity_profile 的結果格式化成一行；沒有有效項目回空字串。

    例：``常玩：Deadlock（最近 10/27）、Minecraft（最近 10/20）``。
    last_played 缺漏或無法解析時省略括號。
    """
    parts: list[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        date = _format_last_played(item.get("last_played"))
        parts.append(f"{name}（最近 {date}）" if date else name)
    return f"常玩：{'、'.join(parts)}" if parts else ""


def _coerce_evidence(item: Any) -> Any:
    """dict → ``EvidenceInput``；已是 ``EvidenceInput`` 原樣回傳。"""
    from echo_memory.schemas.evidence import EvidenceInput, SourceRef, TextSpan

    if isinstance(item, EvidenceInput):
        return item
    if not isinstance(item, dict):
        raise TypeError(f"evidence 項目必須是 EvidenceInput 或 dict，收到 {type(item).__name__}")
    data = dict(item)
    ref = data.get("source_ref")
    if isinstance(ref, dict):
        ref = dict(ref)
        ref["ref_ids"] = tuple(ref.get("ref_ids") or ())
        data["source_ref"] = SourceRef(**ref)
    span = data.get("text_span")
    if isinstance(span, dict):
        data["text_span"] = TextSpan(**span)
    for key in ("origin_evidence_ids", "derived_from"):
        if key in data:
            data[key] = tuple(data[key] or ())
    return EvidenceInput(**data)


def build_engine(
    *,
    data_path: str | Path | None = None,
    namespace_id: str | None = None,
    llm_fn: Callable[[str], str] | None = None,
) -> Any:
    """建一個 MemoryEngine（echo_memory 已在 Core 內 ``subsystems/echo_memory``，由環境直接 import）。

    這只是 ES 時代的退路：Core 路徑的 engine 由 ``uep.echo.memory_wiring.EchoEngineFactory`` 建並以 ``engine=`` 傳入，
    embedding 後端與模型（預設 ``sentence-transformers``／``BAAI/bge-m3``，1024 維）來自 ``UepSettings``。
    這裡不指定 embedding，沿用 echo_memory 的環境預設；``embedding_index.json`` 的 model／dimension 不符時
    echo_memory 會判定索引需重建而非靜默混用，所以不要讓不同 embedding 設定共用同一個資料目錄。
    """
    # echo_memory 自帶的 APScheduler 是 lazy + 無差別的（見 core/dream_scheduler.py），
    # 觸發時機改由 Echo Stream 的 DreamScheduler 決定。其 Config 在 import 時讀 env，
    # 所以要在 import 前設；已 import 過則直接改 config 物件。setdefault 保留人工覆寫。
    import os

    os.environ.setdefault("DREAM_ENABLE_IDLE", "false")
    os.environ.setdefault("DREAM_ENABLE_CRON", "false")

    from echo_memory import MemoryEngine

    try:
        from echo_memory.config import get_config

        cfg = get_config()
        cfg.dream_enable_idle = os.environ["DREAM_ENABLE_IDLE"].lower() == "true"
        cfg.dream_enable_cron = os.environ["DREAM_ENABLE_CRON"].lower() == "true"
    except Exception:  # noqa: BLE001 - 舊版沒有 get_config 就算了
        pass

    params = inspect.signature(MemoryEngine.__init__).parameters
    if "namespace_id" not in params:
        raise RuntimeError(
            "echo_memory 的 MemoryEngine 沒有 namespace_id 參數——"
            "repo 不在 develop 分支上。\n"
            "到 TestSeperateMemorySystem 執行："
            "git checkout develop"
        )

    data = Path(
        data_path
        or _env("ECHO_STREAM_MEMORY_DATA")
        or Path(__file__).resolve().parent.parent.parent / "data" / "memory"
    )
    return MemoryEngine(
        storage_path=data,
        device="cpu",
        llm_fn=llm_fn,
        namespace_id=namespace_id
        or _env("ECHO_STREAM_MEMORY_NAMESPACE", DEFAULT_NAMESPACE),
    )


@dataclass
class SharedRetrieval:
    """``retrieve_shared`` 的結果：渲染文字與 projection ids 一起給 bot。

    ``text`` 無項目時為空字串；``ids`` 回覆後傳給 ``store`` 的 ``shared_context_ids``。
    """

    text: str = ""
    ids: list[str] = field(default_factory=list)
    scope_id: str = ""
    degraded: bool = False
    degraded_reason: str = ""
    about_refs: dict[int, str] = field(default_factory=dict)
    """旁證編號 N -> projection_id（引擎 ``SharedContext.about_refs()``；舊引擎為空）。"""


def _extract_about_refs(result: Any) -> dict[int, str]:
    """取 ``result.about_refs()``；不存在或失敗回空 dict（不猜 id）。"""
    fn = getattr(result, "about_refs", None)
    if not callable(fn):
        return {}
    try:
        raw = fn()
        return {int(k): str(v) for k, v in dict(raw).items()}
    except Exception as exc:  # noqa: BLE001 - 編號只是加分項
        logger.warning("about_refs 取得失敗，略過：%s: %s", type(exc).__name__, exc)
        return {}


def _rendered_ids(result: Any) -> list[str]:
    """依渲染順序取 projection ids：本人段在前、旁證段在後，各段只取實際渲染的筆數。

    引擎的 ``to_text`` 先渲染本人段、再渲染旁證段（D87），且各段有上限；ids 必須和
    渲染出的行一一對應，截斷登記與 ``shared_context_ids`` 才不會歸錯。舊引擎沒有分段
    屬性時沿用 ``result.ids``。
    """
    owner = getattr(result, "owner_items", None)
    about = getattr(result, "about_items", None)
    if owner is None or about is None:
        return list(getattr(result, "ids", []) or [])
    module = sys.modules.get(type(result).__module__)
    cap = getattr(module, "SHARED_MAX_ITEMS", None)
    if not isinstance(cap, int) or cap <= 0:
        cap = len(owner) + len(about)
    return [it.projection_id for it in list(owner)[:cap] + list(about)[:cap]]


class _SharedExclusiveLock:
    """共享／獨佔鎖（threading 原語，在 executor 執行緒內使用）。

    共享者（store／record）可並行；獨佔者（dream）等所有共享者離開才進入，
    且等待中的獨佔者會擋住新的共享者（寫者優先，避免 dream 被連續 store 餓死）。
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @contextmanager
    def shared(self) -> Iterator[None]:
        with self._cond:
            while self._writer or self._waiting_writers:
                self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._cond:
                self._readers -= 1
                self._cond.notify_all()

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        with self._cond:
            self._waiting_writers += 1
            try:
                while self._writer or self._readers:
                    self._cond.wait()
            finally:
                self._waiting_writers -= 1
            self._writer = True
        try:
            yield
        finally:
            with self._cond:
                self._writer = False
                self._cond.notify_all()


class EchoMemoryAdapter:
    """把 MemoryEngine 包成管線期望的形狀。

    stage 實例服務多個 turn；session 邊界用 :meth:`set_session` 更新，
    與 SttInputStage / SessionStore 的會話生命週期同步。
    """

    def __init__(
        self,
        engine: Any = None,
        *,
        top_k: int | None = None,
        max_chars: int | None = None,
        llm_fn: Callable[[str], str] | None = None,
        assess_llm_fn: Callable[[str], str] | None = None,
        executor: ThreadPoolExecutor | None = None,
        engine_lock: threading.Lock | None = None,
        member_resolver: Callable[[str], str | None] | None = None,
    ) -> None:
        self._engine = engine
        self._owns_executor = executor is None
        """外部注入的 executor 由注入方管理生命週期，``aclose`` 不關它。"""
        self._engine_lock = engine_lock
        """可選的 engine 互斥鎖（``threading.Lock``）。同一個 engine 若另有執行緒
        會碰（例如 bot 文字頻道路徑），雙方持同一把鎖才不會撞出 WinError 5。
        不給 = 只靠單工作緒 executor 序列化（原行為）。"""
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="echo-memory"
        )
        """engine 專用的單工作緒 executor——**所有** engine 呼叫都走這裡序列化。

        echo_memory 不是 thread-safe 的：EntityStore._save 是「固定 tmp 檔名 +
        整檔 replace」且無鎖，retrieve（會更新 mention）/ store / dream / 工具
        呼叫若各自丟共用執行緒池，兩條執行緒同時進 engine 就會撞出
        WinError 5（replace 時檔案被另一條執行緒開著，2026-08-24 實錄）。
        代價：dream 蒸餾期間 retrieve 會排隊——但併發進去換到的不只是
        WinError 5，還有整份 JSON 互相覆寫的資料損毀，序列化是底線。"""
        self._shared_lock = _SharedExclusiveLock()
        """細粒度模式的寫者互斥：store／record 取共享，dream 取獨佔。"""
        self._fine_pool: ThreadPoolExecutor | None = None
        """細粒度模式專用的多工作緒池（自建 executor 時才有；惰性建立）。"""
        self._fine_grained: bool | None = None
        """引擎是否自帶並行安全。engine 尚未建構時為 None（視同舊引擎）。"""
        self._llm_fn = llm_fn
        self._assess_llm_fn = assess_llm_fn
        """評估用 LLM。五維打分不需要 reasoning，server 會給一個壓低 effort 的版本；
        沒給就退回 dream 用的 llm_fn。"""
        self._assessor: Any = None
        self._judge: Any = None
        self.assess_mode = _env("ECHO_STREAM_MEMORY_ASSESS", "llm") or "llm"
        if self.assess_mode not in ASSESS_MODES:
            logger.warning("ECHO_STREAM_MEMORY_ASSESS=%r 不認得，改用 llm", self.assess_mode)
            self.assess_mode = "llm"
        self._contexts: dict[str, Any] = {}
        """retrieve 的 MemoryContext 暫存（key = user text），給同一輪的 store
        評估 novelty / dream_resonance 用。retrieve 與 store 之間隔一整輪對話，
        所以不能只留「最後一個」。"""
        self.top_k = top_k or int(
            _env("ECHO_STREAM_MEMORY_TOP_K", str(DEFAULT_TOP_K))
        )
        self.max_chars = max_chars or int(
            _env("ECHO_STREAM_MEMORY_MAX_CHARS", str(DEFAULT_MAX_CHARS))
        )
        self.min_match = float(
            _env("ECHO_STREAM_MEMORY_MIN_MATCH", str(DEFAULT_MIN_MATCH))
        )
        self._session_id: str | None = None
        self.load_seconds: float | None = None
        self.last_retrieve_degraded = False
        """最近一次 retrieve 是否被引擎標為降級（``MemoryContext.degraded``）。
        retrieve 對外只回文字，降級旗標存這裡給宿主 log 讀。"""
        self.last_degraded_reason = ""
        self.last_retrieve_timings_ms: dict[str, float] = {}
        """最近一次 retrieve 的引擎分段耗時（``MemoryContext.timings_ms``，含 embedding）。"""
        self._evidence_supported: bool | None = None
        self._idempotency_supported: bool | None = None
        self._tool_executor: Any = None
        self.member_resolver: Callable[[str], str | None] | None = member_resolver
        """``member_recall`` 用：成員名稱 -> uid（解析不到回 None）。宿主注入。"""
        self._warned_no_resolver = False
        self.tool_max_chars = int(
            _env("ECHO_STREAM_MEMORY_TOOL_MAX_CHARS", str(DEFAULT_TOOL_MAX_CHARS))
        )
        raw_tools = _env("ECHO_STREAM_MEMORY_TOOLS", DEFAULT_TOOLS) or DEFAULT_TOOLS
        self.enabled_tools: list[str] = (
            [] if raw_tools.strip().lower() in ("off", "none", "false", "0")
            else [t.strip() for t in raw_tools.split(",") if t.strip()]
        )

    @property
    def engine(self) -> Any:
        if self._engine is None:
            import time

            t0 = time.perf_counter()
            self._engine = build_engine(llm_fn=self._llm_fn)
            self.load_seconds = time.perf_counter() - t0
        return self._engine

    def _is_fine_grained(self) -> bool:
        """引擎是否自帶並行安全（記憶層 M-1b）。

        偵測依據：公開 ``ensure_initialized`` 方法。M-1b 以同一批引入
        ``ensure_initialized``（全部延遲屬性在啟動時單執行緒建好）、``storage_lock``
        與 ``warmup``，retrieve／store_episode／record_observation 的並行安全建立在
        前兩者之上；``warmup`` 比 ``ensure_initialized`` 早出現，不能當並行安全的指標。
        舊引擎沒有 ``ensure_initialized``，也不是執行緒安全的。
        engine 尚未建構時（惰性建立）視同舊引擎走全包鎖，建好後才切換。
        """
        if self._fine_grained is None:
            if self._engine is None:
                return False
            self._fine_grained = callable(getattr(self._engine, "ensure_initialized", None))
            if self._fine_grained:
                logger.info("記憶引擎自帶並行安全：採細粒度鎖")
        return self._fine_grained

    def _locked(self, fn: Callable[[], Any], kind: str = "read", *, fine: bool = False
                ) -> Callable[[], Any]:
        """依模式包鎖。

        舊引擎（``fine=False``）：有 ``engine_lock`` 就全包持鎖，沒有就原樣（靠單工作緒）。
        新引擎（``fine=True``），按 ``kind``：
        - ``read``：不持任何鎖（retrieve／profile／activity_profile 等）。
        - ``write``：取共享鎖（store／record_observation 可並行，但 dream 期間要等）。
        - ``dream``：先取 ``engine_lock``（與宿主端 reflection 互斥），再取獨佔鎖
          （擋所有 store；retrieve 不擋）。固定先 engine_lock 後獨佔鎖，不會死結。
        """
        lock = self._engine_lock
        if not fine:
            if lock is None:
                return fn

            def run_all() -> Any:
                with lock:
                    return fn()

            return run_all

        if kind == "read":
            return fn
        if kind == "write":
            def run_write() -> Any:
                with self._shared_lock.shared():
                    return fn()

            return run_write

        def run_dream() -> Any:
            with (lock if lock is not None else nullcontext()):
                with self._shared_lock.exclusive():
                    return fn()

        return run_dream

    async def _run(self, fn: Callable[[], Any], kind: str = "read") -> Any:
        """把 engine 工作丟進 executor。舊引擎：單工作緒 + 全包鎖；新引擎：多工作緒 + 細粒度鎖。"""
        fine = self._is_fine_grained()
        executor = self._executor
        if fine and self._owns_executor:
            if self._fine_pool is None:
                self._fine_pool = ThreadPoolExecutor(
                    max_workers=FINE_POOL_WORKERS, thread_name_prefix="echo-memory-fine"
                )
            executor = self._fine_pool
        elif fine and getattr(self._executor, "_max_workers", 2) == 1:
            logger.warning("注入的 executor 只有單工作緒，細粒度鎖無法並行；請改給多工作緒")
        return await asyncio.get_running_loop().run_in_executor(
            executor, self._locked(fn, kind, fine=fine)
        )

    async def prepare(self) -> None:
        """提早建立 engine——設定錯誤在暖機階段就爆，而不是第一次 retrieve。

        embedding 模型（sentence-transformers）的首載也發生在這裡之後的
        第一次呼叫；真正的冷啟成本用 memory-check 量。
        """
        await self._run(lambda: self.engine)

    async def warmup(self) -> bool:
        """觸發 embedding 暖機入口；回傳是否成功觸發，絕不往外拋。

        - engine 有公開 ``warmup`` → 呼叫它（記憶層 feature/memory-reliability 提供）。
        - 沒有 → 退路：存取 ``engine.episodic`` 一次，首次存取會在
          engine 內啟動背景 embedding 暖機。
        - 兩者都沒有 → False。

        走 :meth:`_run`（executor + engine_lock）：engine 首次建構與 ``episodic``
        載入都不是執行緒安全的，需與其他路徑序列化；暖機本身是背景執行緒、
        呼叫立即返回，因此持鎖時間只有觸發那一下，不會擋住 retrieve / store。
        """

        def _work() -> bool:
            engine = self.engine
            fn = getattr(engine, "warmup", None)
            if callable(fn):
                fn()
                return True
            # hasattr 會實際取值，即觸發 episodic 的延遲載入與背景暖機
            return hasattr(engine, "episodic")

        try:
            return bool(await self._run(_work))
        except Exception as exc:  # noqa: BLE001 - 暖機失敗不得影響管線
            logger.warning("記憶 embedding 暖機失敗：%s", exc)
            return False

    async def aclose(self) -> None:
        """關閉 engine 專用 executor。

        不取消排隊中的工作——store / dream 是寫入，丟掉就是記憶遺失；
        等它們跑完再收，且不擋 event loop。重複呼叫是安全的。
        外部注入的 executor 不在這裡關（可能還有別的 adapter 在用）。
        """
        if not self._owns_executor:
            return
        def _shutdown() -> None:
            self._executor.shutdown(wait=True)
            if self._fine_pool is not None:
                self._fine_pool.shutdown(wait=True)

        await asyncio.get_running_loop().run_in_executor(None, _shutdown)

    def set_session(self, session_id: str | None) -> None:
        """換會話。之後的 retrieve 會排除這個會話的 episodes（防自我回聲），
        store 會把 episodes 標上這個 session_id。"""
        self._session_id = session_id
        self._tool_executor = None  # echo_memory 的 ToolExecutor 綁 session_id，換會話重建

    # --- 工具呼叫（C 段）---

    def tool_definitions(self) -> list[dict[str, Any]]:
        """開放給 LLM 的工具，OpenAI function schema。空列表 = 不給工具。

        schema 來自 echo_memory 的 ``TOOL_DECLARATIONS``（它是 Gemini 格式的 dict，
        欄位剛好與 OpenAI 的 function 一致，只差外面要包一層 ``type: function``）。
        """
        if not self.enabled_tools:
            return []
        try:
            from llm_integration.tools.definitions import TOOL_DECLARATIONS
        except ImportError as exc:
            logger.warning("echo_memory 沒有 llm_integration.tools——只保留 ES 自帶工具：%s", exc)
            TOOL_DECLARATIONS = []
        known = {d["name"]: d for d in TOOL_DECLARATIONS}
        known[MEMBER_RECALL] = MEMBER_RECALL_SCHEMA
        known.update(ABOUT_ME_SCHEMAS)
        out = []
        for name in self.enabled_tools:
            if name == MEMBER_RECALL and self.member_resolver is None:
                if not self._warned_no_resolver:
                    self._warned_no_resolver = True
                    logger.warning("member_recall 已啟用但未注入 member_resolver——不曝光此工具")
                continue
            decl = known.get(name)
            if decl is None:
                logger.warning(
                    "ECHO_STREAM_MEMORY_TOOLS 裡的 %r 不是 echo_memory 的工具，略過", name
                )
                continue
            out.append(function_tool_definition(decl))
        return out

    @property
    def tool_executor(self) -> Any:
        """echo_memory 的 ToolExecutor，lazy、綁當前 session。"""
        if self._tool_executor is None:
            from llm_integration.tools.executor import ToolExecutor

            self._tool_executor = ToolExecutor(
                self.engine, session_id=self._session_id or "unknown"
            )
        return self._tool_executor

    async def execute_tool(
        self, name: str, args: dict[str, Any], *, context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """給 LLMThinkStage 的 ``tool_executor``。

        白名單再擋一次——schema 只給了兩個，模型照理叫不出別的，
        但「照理」不是防線。結果截到 ``tool_max_chars``。

        ``context``：本輪宿主放在 ``Utterance.metadata["tool_context"]`` 的 dict，由 think stage
        隨工具呼叫傳入（不存在 adapter 上，交錯的輪次不會串）。結構見 :meth:`_member_recall`。
        """
        if name not in self.enabled_tools:
            return {"success": False, "result": f"Tool {name!r} is not available."}
        if name == MEMBER_RECALL:
            return await self._member_recall(args, context)
        if name in ABOUT_ME_TOOLS:
            return await self._about_me_tool(name, args, context)

        def _work() -> dict[str, Any]:
            outcome = self.tool_executor.execute(name, dict(args))
            text = str(outcome.get("result", ""))
            if len(text) > self.tool_max_chars:
                outcome = {**outcome, "result": text[: self.tool_max_chars] + "…"}
            return outcome

        # trigger_dream 即 dream（獨佔）；deep_recall 是讀；其餘（force_remember 等）是寫
        kind = "dream" if name == "trigger_dream" else (
            "read" if name == "deep_recall" else "write")
        return await self._run(_work, kind)

    # --- member_recall（D85 指名查詢）---

    def set_member_resolver(self, resolver: Callable[[str], str | None] | None) -> None:
        """注入成員名稱 -> uid 的解析器（解析不到回 None）。None = 停用解析。

        resolver 本身沒有每輪狀態；每輪的場域脈絡走 ``execute_tool(context=...)``。
        """
        self.member_resolver = resolver

    async def _member_recall(
        self, args: dict[str, Any], context: dict[str, Any] | None
    ) -> dict[str, Any]:
        """解析成員 -> 帶 owner_uid 呼叫 retrieve_shared；脈絡缺漏或解析不到不碰引擎。

        ``context`` 結構（宿主每輪建一個自己的 dict）::

            {"member_recall": {"scope_id": str, "viewer_uid": str,
                               "present_uids": [str, ...],      # 可省
                               "display_name": Callable[[str], str],  # 可省
                               "shared_adapter": EchoMemoryAdapter},  # 可省，缺省用 self
             "member_recall_ids": []}  # 可省；工具把 LLM 實際看到的 projection ids 寫進這裡

        ids 只登記截斷後 LLM 真的看得到的項目，回覆後宿主從同一個 dict 讀取。
        """
        member = str(args.get("member") or "").strip()
        query = str(args.get("query") or "").strip()
        cfg = context.get("member_recall") if isinstance(context, dict) else None
        scope_id = cfg.get("scope_id") if isinstance(cfg, dict) else None
        viewer_uid = cfg.get("viewer_uid") if isinstance(cfg, dict) else None
        if not (member and scope_id and viewer_uid and self.member_resolver is not None):
            return {"success": True, "result": MEMBER_NOT_FOUND}
        try:
            owner_uid = self.member_resolver(member) or None
        except Exception as exc:  # noqa: BLE001 - 解析失敗視同無法確認
            logger.warning("member_resolver 失敗：%s: %s", type(exc).__name__, exc)
            owner_uid = None
        if owner_uid is None:
            return {"success": True, "result": MEMBER_NOT_FOUND}
        display_name = cfg.get("display_name")
        label = ""
        if callable(display_name):
            try:
                label = " ".join(str(display_name(owner_uid) or "").split())
            except Exception:  # noqa: BLE001 - 名稱只是顯示
                label = ""
        label = (label or _tools.UNKNOWN_MEMBER_LABEL)[:MEMBER_NAME_MAX_CHARS]
        target = cfg.get("shared_adapter") or self
        result = await target.retrieve_shared(
            query,
            scope_id=scope_id,
            viewer_uid=viewer_uid,
            present_uids=tuple(cfg.get("present_uids") or ()),
            display_name=display_name if callable(display_name) else None,
            owner_uid=owner_uid,
        )
        if result is None:
            return {"success": False, "result": "目前無法查詢這位成員的共享內容。"}
        if not result.text:
            return {
                "success": True,
                "result": f"「{label}」是本伺服器成員，但目前沒有可分享的內容。",
            }
        text = (
            f"以下是關於「{label}」（本伺服器成員）在伺服器裡可分享的內容；"
            f"「提到〈…〉」的是其他成員的說法，與本人說法不同時以本人為準：\n{result.text}"
        )
        visible = list(result.ids)
        complete_text = text  # 旁證編號只認完整的行；被截斷的半行不登記
        if len(text) > self.tool_max_chars:
            cut = text[: self.tool_max_chars]
            # 渲染格式是一項一行（"- " 開頭）；行數對不上 ids 就不登記，寧缺勿濫
            full_items = sum(1 for ln in text.split("\n") if ln.startswith("- "))
            seen = sum(1 for ln in cut.split("\n")[:-1] if ln.startswith("- "))
            visible = visible[:seen] if full_items == len(visible) else []
            complete_text = "\n".join(cut.split("\n")[:-1])
            text = cut + "…"
        if isinstance(context, dict) and owner_uid == self._about_me_actor(context):
            # 旁證編號每個 SharedContext 都從 1 開始，且只有指名查詢本人時才渲染旁證：
            # 只在查本人時整份覆蓋（不合併、不留舊份），查別人完全不動。
            # 只登記 LLM 實際看得到的旁證編號（截斷後仍在完整行中的）。
            context["about_refs"] = {
                n: pid for n, pid in result.about_refs.items()
                if f"〔旁證{n}〕" in complete_text
            }
        if isinstance(context, dict):
            bucket = context.setdefault("member_recall_ids", [])
            for pid in visible:
                if pid not in bucket:
                    bucket.append(pid)
        return {"success": True, "result": text}

    # --- 關於我（撤回／退出／糾正）---

    @staticmethod
    def _about_me_cfg(context: dict[str, Any] | None) -> dict[str, Any] | None:
        """宿主明確提供的 ``context["about_me"]``；沒有（或型別不對）回 None。

        fail-closed：不退回 ``member_recall`` 的 scope_id／viewer_uid／shared_adapter，
        宿主沒有明確授權「關於我」就一律不動作。
        """
        cfg = context.get("about_me") if isinstance(context, dict) else None
        return cfg if isinstance(cfg, dict) else None

    @classmethod
    def _about_me_actor(cls, context: dict[str, Any] | None) -> str | None:
        """說話者本人的 uid，只取 ``about_me.actor_uid``；沒有 about_me 回 None。"""
        cfg = cls._about_me_cfg(context)
        actor = cfg.get("actor_uid") if cfg else None
        return str(actor) if actor else None

    def _about_me_scope(
        self, context: dict[str, Any] | None
    ) -> tuple[Any, str, str, str | None] | None:
        """從 ``context["about_me"]`` 取 (target adapter, scope_id, actor_uid, session_id)。

        沒有 ``about_me``、或缺 ``scope_id`` / ``actor_uid`` 一律回 None（fail-closed，
        不退回 ``member_recall`` 設定）。``adapter`` 缺省用 self。actor 只來自宿主，
        工具參數不得帶。

        ``session_id`` 宜由宿主提供**說話者**的 session_id；沒給就傳 None，不回退
        ``target._session_id``（那是 scope adapter 的 session，不是說話者的）。
        """
        cfg = self._about_me_cfg(context)
        if cfg is None:
            return None
        scope_id = cfg.get("scope_id")
        actor_uid = cfg.get("actor_uid")
        if not (scope_id and actor_uid):
            return None
        target = cfg.get("adapter") or self
        return target, str(scope_id), str(actor_uid), cfg.get("session_id")

    @staticmethod
    def _about_me_evidence(context: dict[str, Any] | None) -> Any:
        """宿主放在 ``context["about_me"]["evidence"]`` 的本輪 evidence（與 store 同一份）。

        只讀宿主 context，LLM 參數無法提供。
        """
        cfg = context.get("about_me") if isinstance(context, dict) else None
        return cfg.get("evidence") if isinstance(cfg, dict) else None

    async def _about_me_tool(
        self, name: str, args: dict[str, Any], context: dict[str, Any] | None
    ) -> dict[str, Any]:
        """三個「關於我」工具的共用入口：驗脈絡、轉 ref、呼叫引擎、翻成 LLM 讀的文字。"""
        scope = self._about_me_scope(context)
        if scope is None:
            return {"success": False, "result": ABOUT_NO_CONTEXT}
        target, scope_id, actor_uid, session_id = scope
        projection_id = ""
        if name != OPT_OUT_ABOUT_ME:
            refs = context.get("about_refs") if isinstance(context, dict) else None
            if not refs:
                return {"success": False, "result": ABOUT_NO_REF}
            ref = _parse_ref(args.get("ref"))
            if ref is None:
                return {"success": False, "result": ABOUT_BAD_REF}
            projection_id = refs.get(ref) or ""
            if not projection_id:
                return {"success": False, "result": ABOUT_BAD_REF}
        if name == RETRACT_ABOUT_ME:
            res = await target.retract_about_me(scope_id, actor_uid, projection_id)
        elif name == OPT_OUT_ABOUT_ME:
            enabled = args.get("enabled")
            if not isinstance(enabled, bool):
                return {"success": False, "result": "enabled 必須是 true 或 false。"}
            res = await target.opt_out_about_me(scope_id, actor_uid, enabled)
        else:
            text = str(args.get("correction_text") or "").strip()
            if not text:
                return {"success": False, "result": "更正內容是空的，請使用者說出正確的說法。"}
            res = await target.correct_about_me(
                scope_id, actor_uid, projection_id, text,
                evidence=self._about_me_evidence(context),
                session_id=session_id,
            )
        return {"success": bool(res and res.get("ok")), "result": self._about_me_text(name, res)}

    @staticmethod
    def _about_me_text(name: str, res: dict[str, Any] | None) -> str:
        """引擎結果 -> 給 LLM 的文字：分辨不支援／被拒絕（帶 reason）／成功。"""
        if res is None:
            return ABOUT_UNSUPPORTED
        reason = str(res.get("reason") or "")
        if not res.get("ok"):
            return f"被拒絕或失敗（原因：{reason or '未註明'}），沒有任何變更。"
        if name == CORRECT_ABOUT_ME and res.get("correction_stored") is False:
            # 優先於 already_hidden：遮蔽已生效但內容沒存，必須請使用者重送
            return ("那條旁證已遮蔽，但更正內容未保存，需要請使用者重送一次更正；"
                    "只影響之後的檢索。")
        if reason in ABOUT_REASON_TEXT:
            return ABOUT_REASON_TEXT[reason]
        if name == OPT_OUT_ABOUT_ME:
            if res.get("enabled"):
                return ("已設定退出：之後其他人查詢時，關於使用者的內容（含「最近在玩」）"
                        "都不會再出現；只影響之後的檢索。")
            return "已恢復：之後的檢索可再包含關於使用者的內容。"
        if name == CORRECT_ABOUT_ME:
            return "那條旁證已遮蔽，使用者的更正已保存；只影響之後的檢索。"
        return "已撤回那條旁證；只影響之後的檢索。"

    async def _about_call(self, method: str, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        """呼叫 scope engine 的同步方法（寫入路徑，丟 thread）。

        引擎沒這個方法回 None（= 不支援）；引擎丟例外回 ``ok=False`` 的 dict，
        reason 為 ``engine_error``，與「不支援」區分。引擎回的 dict 原樣保留。
        """
        fn = getattr(self.engine, method, None)
        if fn is None:
            return None
        try:
            res = await self._run(lambda: fn(*args, **kwargs), "write")
        except Exception as exc:  # noqa: BLE001 - 失敗要讓 LLM 知道沒成功
            logger.warning("%s 失敗：%s: %s", method, type(exc).__name__, exc)
            return {"ok": False, "reason": "engine_error"}
        return res if isinstance(res, dict) else {"ok": False, "reason": "bad_result"}

    async def retract_about_me(
        self, scope_id: str, actor_uid: str, projection_id: str
    ) -> dict[str, Any] | None:
        """撤回一條關於 actor 的旁證投影。回引擎 dict；引擎不支援回 None。"""
        return await self._about_call("retract_about_me", scope_id, actor_uid, projection_id)

    async def opt_out_about_me(
        self, scope_id: str, actor_uid: str, enabled: bool
    ) -> dict[str, Any] | None:
        """設定／解除 actor 的退出旗標。回引擎 dict；引擎不支援回 None。"""
        return await self._about_call("opt_out_about_me", scope_id, actor_uid, enabled)

    async def correct_about_me(
        self,
        scope_id: str,
        actor_uid: str,
        projection_id: str,
        correction_text: str,
        *,
        evidence: Any = None,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        """遮蔽一條旁證並存下 actor 的更正。回引擎 dict；引擎不支援回 None。

        ``evidence``：宿主本輪 store 用的同一份來源宣告（``EvidenceInput`` 或同欄位 dict 的
        清單，含 channel_kind／audience_size／scope）。為 None（或無法解析）時引擎把更正內容
        當**私有**使用者陳述寫入，不進共享投影——遮蔽仍生效，但更正不會出現在別人的查詢裡。
        """
        prepared = None
        if evidence:
            try:
                prepared = [_coerce_evidence(item) for item in evidence]
            except Exception as exc:  # noqa: BLE001 - 退回私有陳述，不擋遮蔽
                logger.warning("correct_about_me evidence 無法解析，退回 None：%s: %s",
                               type(exc).__name__, exc)
        return await self._about_call(
            "correct_about_me", scope_id, actor_uid, projection_id, correction_text,
            evidence=prepared, session_id=session_id,
        )

    # --- 管線介面 ---

    async def retrieve(self, text: str, **engine_kwargs: Any) -> str | None:
        """檢索相關記憶，回傳可塞進 prompt 的文字；沒撈到就 None。

        同步引擎丟 executor——EcphoryRAG 的向量搜尋 + 圖擴散是 CPU 工作，
        不能讓它擋住 event loop 上正在流的音訊。

        ``engine_kwargs``（例如 M5 的 ``shared_turn=True``）原樣透傳給 ``engine.retrieve``；
        引擎簽名不認得的參數會略過並記 debug，舊版引擎照常運作。
        """
        if not text or not text.strip():
            return None
        extra = self._accepted_kwargs(getattr(self.engine, "retrieve", None), engine_kwargs)

        def _work() -> str | None:
            context = self.engine.retrieve(
                text,
                top_k=self.top_k,
                min_similarity=self.min_match,
                exclude_session_id=self._session_id,
                **extra,
            )
            # 第二道門檻：EcphoryRAG 圖擴散出來的 episode 在 similarity_to_query
            # 回 None 時不會被 min_similarity 擋（2026-08-22 實測注入了 30-36%
            # 的東西）。顯示給 LLM 的匹配度必須是真的過了門檻的。
            sims = getattr(context, "similarities", {}) or {}
            kept = [
                ep for ep in context.episodes
                if sims.get(ep.id, 0.0) >= self.min_match
            ]
            if len(kept) != len(context.episodes):
                logger.info("memory retrieve：圖擴散漏過門檻，丟棄 %d 筆",
                            len(context.episodes) - len(kept))
                context.episodes = kept
            # 降級旗標：引擎在 embedding / 後端不可用時回 degraded=True（空結果也要知道原因）。
            # 用 ``is True`` 判斷，避免替身物件的任意屬性被當成真值
            degraded = getattr(context, "degraded", False) is True
            reason = getattr(context, "degraded_reason", "")
            self.last_retrieve_degraded = degraded
            self.last_degraded_reason = reason if degraded and isinstance(reason, str) else ""
            timings = getattr(context, "timings_ms", None)
            self.last_retrieve_timings_ms = dict(timings) if isinstance(timings, dict) else {}
            if degraded:
                logger.warning(
                    "memory retrieve 降級：%s", self.last_degraded_reason or "未註明原因"
                )
            self._remember_context(text, context)
            if context.is_empty():
                return None
            rendered = context.to_text()
            if len(rendered) > self.max_chars:
                rendered = rendered[: self.max_chars] + "…"
            return rendered

        return await self._run(_work)

    @staticmethod
    def _accepted_kwargs(fn: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
        """只留下 ``fn`` 簽名接受的 kwargs（有 ``**kwargs`` 就全收）；簽名讀不到時全收。"""
        if not kwargs:
            return {}
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            return dict(kwargs)
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return dict(kwargs)
        kept = {k: v for k, v in kwargs.items() if k in params}
        dropped = set(kwargs) - set(kept)
        if dropped:
            logger.debug("memory retrieve：引擎不支援參數 %s，略過", sorted(dropped))
        return kept

    _CONTEXT_KEEP = 8

    def _remember_context(self, text: str, context: Any) -> None:
        self._contexts[text] = context
        while len(self._contexts) > self._CONTEXT_KEEP:
            self._contexts.pop(next(iter(self._contexts)))

    # --- salience（Phase 4b）---

    @property
    def assessor(self) -> Any:
        """SubconsciousAssessor，lazy。heuristic 模式不給 LLM，它自己會降級。"""
        if self._assessor is None:
            from echo_memory.affect.subconscious import SubconsciousAssessor

            fn = None
            if self.assess_mode == "llm":
                fn = self._assess_llm_fn or self._llm_fn
                if fn is None:
                    logger.warning("assess=llm 但沒有 llm_fn，退回 heuristic")
            self._assessor = SubconsciousAssessor(llm_fn=fn)
        return self._assessor

    async def _evaluate_salience(
        self, user_text: str, spoken_text: str, context: Any
    ) -> dict[str, Any]:
        """Entry.py Step 3：assess → signals → 三值 → neurochem → StorageJudge 微調。

        推理全在 echo_memory；這裡只是把它們串起來。

        **鎖範圍**：assess 與 judge 是 LLM 呼叫（數秒），丟預設執行緒池、不持
        engine 鎖也不佔 engine 專用 executor；只有碰 ``engine.neurochem`` 的
        評估段（純 CPU、極短）走 :meth:`_run`（executor + 鎖）。

        StorageJudge 是第二段：OpenSoul 公式偏重 complexity/novelty，
        「我妹妹要去筑波大學」「別再糾正我的英文」這種簡單但該記住的話
        baseline 只有 0.35-0.45，過不了蒸餾門檻（2026-08-22 實測）。
        Judge 的 JudgeGate 只在信號進灰區才打 LLM，用 salience_delta /
        force_remember / metadata_tags（correction、preference…）補這一段。
        """
        from echo_memory.affect.salience import SalienceEvaluator
        from echo_memory.affect.signals import SalienceSignals

        loop = asyncio.get_running_loop()
        assessor = self.assessor
        assessment = await loop.run_in_executor(
            None, lambda: assessor.assess(user_text, context)
        )
        signals = SalienceSignals.from_subconscious(assessment)

        def _score() -> tuple[float, float, float]:
            evaluator = SalienceEvaluator(self.engine.neurochem)
            scored = evaluator.evaluate(
                signals, user_input=user_text, agent_response=spoken_text
            )
            evaluator.update_neurochem(signals)
            return scored

        baseline, da, ht = await self._run(_score)
        result: dict[str, Any] = {
            "salience": baseline,
            "da_weight": da,
            "ht_weight": ht,
            "assessment": assessment.to_dict(),
        }
        if self.judge_enabled:
            try:
                judge = self.judge
                decision = await loop.run_in_executor(
                    None,
                    lambda: judge.storage_decision(
                        user_input=user_text,
                        agent_response=spoken_text,
                        baseline_salience=baseline,
                        assessment=assessment,
                        context=context if context is not None and not context.is_empty()
                        else None,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - judge 是加分項，炸了用 baseline
                logger.warning("StorageJudge 失敗，用 baseline：%s: %s",
                               type(exc).__name__, exc)
            else:
                result["salience"] = max(0.0, min(1.0, baseline + decision.salience_delta))
                judge_info: dict[str, Any] = {"source": decision.source}
                if decision.salience_delta:
                    judge_info["delta"] = round(decision.salience_delta, 3)
                if decision.metadata_tags:
                    judge_info["tags"] = list(decision.metadata_tags)
                if decision.force_remember:
                    judge_info["force_remember"] = True
                    result["force_remember"] = True
                if decision.entity_triggers:
                    result["entity_triggers"] = True
                if decision.reasoning:
                    judge_info["reasoning"] = decision.reasoning[:200]
                result["judge"] = judge_info
        return result

    @property
    def judge_enabled(self) -> bool:
        return self.assess_mode == "llm" and (
            _env("ECHO_STREAM_MEMORY_JUDGE", "on") or "on"
        ).lower() not in ("off", "false", "0")

    @property
    def judge(self) -> Any:
        """StorageJudge，lazy。與 assessor 共用評估用的 LLM。"""
        if self._judge is None:
            from echo_memory.affect.judge import StorageJudge

            self._judge = StorageJudge(llm_fn=self._assess_llm_fn or self._llm_fn)
        return self._judge

    async def store(
        self,
        user_text: str,
        spoken_text: str,
        session_id: str | None = None,
        idempotency_key: str | None = None,
        **metadata: Any,
    ) -> dict[str, Any] | None:
        """把一輪對話寫進記憶。**寫 spoken 不寫 generated**——與歷史同一條語意。

        回傳存檔摘要（episode_id / salience 三值 / 評估維度）給 server 記 log；
        旁路：任何失敗只 log、回 None，不往上拋。metadata 建議帶 origin
        （voice/text、是否被 barge-in 截斷）——agent 產物混進使用者指示
        會污染記憶（spike 實測）。

        ``idempotency_key``：同 key 重送由引擎回原 episode_id、不重寫；引擎簽名沒有
        此參數（舊版）或為 None 時不傳。

        salience 評估失敗不擋寫入——退回 0.5 照存，寧可有一筆平淡的記憶
        也不要丟掉一輪對話。
        """
        if not user_text or not user_text.strip():
            return None
        sid = session_id or self._session_id or "unknown"
        context = self._contexts.pop(user_text, None)

        try:
            scores: dict[str, Any] = {}
            if self.assess_mode != "off":
                try:
                    scores = await self._evaluate_salience(user_text, spoken_text, context)
                except Exception as exc:  # noqa: BLE001 - 評估是加分項
                    logger.warning("salience 評估失敗，退回 0.5：%s: %s",
                                   type(exc).__name__, exc)
                    scores = {}
            assessment = scores.pop("assessment", None)
            extra = dict(metadata)
            if assessment is not None:
                extra["assessment"] = assessment
            evidence = self._prepare_evidence(extra.pop("evidence", None))
            if evidence:
                extra["evidence"] = evidence
            if idempotency_key is not None and self._supports_idempotency():
                extra["idempotency_key"] = idempotency_key

            def _write() -> str:
                return self.engine.store_episode(
                    user_input=user_text,
                    agent_response=spoken_text,
                    session_id=sid,
                    **scores,
                    **extra,
                )

            episode_id = await self._run(_write, "write")
            unit_ids = await self._episode_user_unit_ids(episode_id)
            return {"episode_id": episode_id, **scores, "assessment": assessment,
                    "unit_ids": unit_ids}
        except Exception as exc:  # noqa: BLE001 - 旁路，失敗不該炸管線
            logger.warning("memory store 失敗（不影響對話）：%s: %s",
                           type(exc).__name__, exc)
            return None

    async def _episode_user_unit_ids(self, episode_id: Any) -> list[str]:
        """查本輪 episode 的使用者 evidence unit id（供 restrict_unit／scan_privacy_intent）。

        引擎沒有 ``evidence.get_by_episode`` 或查詢失敗都回空清單，不影響 store 結果。
        """
        evidence = getattr(self.engine, "evidence", None)
        getter = getattr(evidence, "get_by_episode", None)
        if getter is None or not episode_id:
            return []
        try:
            units = await self._run(lambda: list(getter(episode_id)), "read")
            return [u.id for u in units if getattr(u, "source_type", "user") == "user"]
        except Exception as exc:  # noqa: BLE001 - 附加資訊，失敗只降級
            logger.warning("查本輪 unit_ids 失敗，回空清單：%s: %s", type(exc).__name__, exc)
            return []

    def _supports_evidence(self) -> bool:
        """引擎的 ``store_episode`` 是否有 ``evidence`` 參數（舊版沒有，不能傳）。"""
        if self._evidence_supported is None:
            try:
                params = inspect.signature(self.engine.store_episode).parameters
                self._evidence_supported = "evidence" in params
            except (TypeError, ValueError):
                self._evidence_supported = False
        return self._evidence_supported

    def _supports_idempotency(self) -> bool:
        """引擎的 ``store_episode`` 是否有 ``idempotency_key`` 參數（舊版沒有，不能傳）。"""
        if self._idempotency_supported is None:
            try:
                params = inspect.signature(self.engine.store_episode).parameters
                self._idempotency_supported = "idempotency_key" in params
            except (TypeError, ValueError):
                self._idempotency_supported = False
        return self._idempotency_supported

    def _prepare_evidence(self, evidence: Any) -> list[Any] | None:
        """把呼叫端的 evidence 轉成引擎吃的 ``EvidenceInput`` 清單。

        接受 ``EvidenceInput`` 或同欄位的 dict（``source_ref`` / ``text_span`` 也可為 dict），
        讓宿主不必 import echo_memory。引擎不支援 evidence（舊版）或宣告格式無法解析時
        退回不帶 evidence——寧可以 legacy_unknown 存一筆，也不要丟掉一輪對話。
        內容與 user_input / agent_response 不一致由引擎丟 ValueError，不在這裡吞掉。
        """
        if not evidence:
            return None
        if not self._supports_evidence():
            logger.debug("引擎不支援 evidence 參數，略過來源宣告")
            return None
        try:
            return [_coerce_evidence(item) for item in evidence]
        except (TypeError, ValueError, KeyError, ImportError) as exc:
            logger.warning("evidence 宣告無法解析，退回不帶來源：%s: %s",
                           type(exc).__name__, exc)
            return None

    async def profile(self) -> str:
        """使用者 profile 文字（subject=user 的 concept 全列）+ 常玩遊戲一行。沒有就空字串。

        常駐脈絡，不是檢索結果——server 在暖機與每次 dream 後刷新一次，
        不在每輪熱路徑呼叫。

        常玩行（``ECHO_STREAM_MEMORY_ACTIVITY_TOP_N``，0 停用）放在 profile 開頭：
        下游（如 bot 端 profile_max_chars）可能從頭截斷，放尾端會先被截掉。
        長度策略：activity 行短且資訊密度高，優先保留——傳給 ``get_profile`` 的
        ``max_chars`` 先扣掉 activity 行長度（含換行），總長不超過
        ``ECHO_STREAM_MEMORY_PROFILE_MAX_CHARS``。引擎沒有 get_profile 時，
        有遊玩資料仍只回 activity 行；activity 失敗只降級為不附加。
        """
        max_chars = int(_env("ECHO_STREAM_MEMORY_PROFILE_MAX_CHARS", "800"))
        top_n = _parse_top_n(_env("ECHO_STREAM_MEMORY_ACTIVITY_TOP_N", "3"))
        line = ""
        if top_n > 0:
            line = _format_activity_line(await self.activity_profile(top_n=top_n))
            line = line[:max_chars]
        # 扣掉 activity 行與分隔換行後，剩餘預算才給 concept 文字
        budget = max_chars - (len(line) + 1 if line else 0)
        get_profile = getattr(self.engine, "get_profile", None)
        if get_profile is None:
            logger.warning("echo_memory 沒有 get_profile——分支太舊，concept profile 停用")
            base = ""
        elif budget <= 0:
            base = ""
        else:
            base = await self._run(lambda: get_profile(max_chars=budget) or "")
        if not line:
            return base
        return f"{line}\n{base}" if base else line

    async def activity_profile(self, top_n: int = 3) -> list[dict[str, Any]]:
        """遊玩紀錄摘要（依累計時長排序）：[{name, play_count, total_minutes, last_played}]。

        引擎沒有此方法（舊版）或查詢失敗一律回 []——不讓 prompt 組裝因此失敗。
        """
        fn = getattr(self.engine, "activity_profile", None)
        if fn is None:
            return []
        try:
            return list(await self._run(lambda: fn(top_n=top_n)) or [])
        except Exception as exc:  # noqa: BLE001 - 加分項，失敗只降級
            logger.warning("activity_profile 失敗，退回空清單：%s: %s",
                           type(exc).__name__, exc)
            return []

    async def record_observation(
        self,
        *,
        kind: str,
        subject: str,
        started_at: datetime,
        ended_at: datetime,
        attrs: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        source_ref: Any = None,
    ) -> str | None:
        """記錄平台被動觀察（如遊戲時段），透傳引擎 ``record_observation``。

        走寫入路徑（細粒度模式取共享鎖、舊模式全包 engine_lock）。回傳 evidence id；
        引擎沒有此方法（舊版）或寫入失敗一律回 None——觀察是附帶資料，不讓呼叫端因此中斷。
        ``attrs`` / ``idempotency_key`` / ``source_ref`` 為 None 時不傳給引擎，沿用其預設。
        """
        fn = getattr(self.engine, "record_observation", None)
        if fn is None:
            return None
        kwargs: dict[str, Any] = {
            "kind": kind, "subject": subject,
            "started_at": started_at, "ended_at": ended_at,
        }
        if attrs is not None:
            kwargs["attrs"] = attrs
        if idempotency_key is not None:
            kwargs["idempotency_key"] = idempotency_key
        if source_ref is not None:
            kwargs["source_ref"] = source_ref
        try:
            return await self._run(lambda: fn(**kwargs), "write")
        except Exception as exc:  # noqa: BLE001 - 觀察寫入失敗只降級
            logger.warning("record_observation 失敗，略過：%s: %s", type(exc).__name__, exc)
            return None

    async def retrieve_shared(
        self,
        text: str,
        *,
        scope_id: str,
        viewer_uid: str,
        present_uids: Iterable[Any] = (),
        display_name: Callable[[str], str] | None = None,
        owner_uid: str | None = None,
    ) -> SharedRetrieval | None:
        """檢索場域共享摘要，透傳引擎 ``retrieve_shared``（唯讀，不持寫鎖）。

        應呼叫在 scope engine（namespace ``scope-g<guild_id>``）的 adapter 上；DM 不要呼叫。
        ``display_name``：uid -> 顯示名稱的 callable，缺省由引擎用「成員 <uid>」。
        回傳 ``SharedRetrieval``（text 無項目時為空字串、ids 供回覆後 store 的
        ``shared_context_ids``）；引擎沒有此方法或失敗回 None。引擎端失敗會自行回
        degraded 的空結果，此時 ``degraded=True``。

        ``owner_uid``：只回該成員的投影（指名查詢，``member_recall`` 用），此時 ``text``
        可為空（引擎改依時間排序）。引擎簽名不認得 ``owner_uid`` 時回空結果——
        不得退化成不篩選的全體查詢，否則會撈出別人的投影。
        """
        fn = getattr(self.engine, "retrieve_shared", None)
        if fn is None or (owner_uid is None and (not text or not text.strip())):
            return None
        kwargs: dict[str, Any] = {
            "scope_id": scope_id,
            "viewer_uid": viewer_uid,
            "present_uids": tuple(present_uids),
        }
        if owner_uid is not None:
            if not _declares_param(fn, "owner_uid"):
                logger.warning("引擎 retrieve_shared 不支援 owner_uid，指名查詢回空")
                return SharedRetrieval(scope_id=scope_id)
            kwargs["owner_uid"] = owner_uid
        try:
            result = await self._run(lambda: fn(text or "", **kwargs), "read")
            if getattr(result, "degraded", False) is True:
                logger.warning("retrieve_shared 降級：%s",
                               getattr(result, "degraded_reason", "") or "未註明原因")
            rendered = result.to_text(display_name) if display_name else result.to_text()
            return SharedRetrieval(
                text=rendered or "",
                ids=_rendered_ids(result),
                scope_id=getattr(result, "scope_id", scope_id),
                degraded=bool(getattr(result, "degraded", False)),
                degraded_reason=str(getattr(result, "degraded_reason", "") or ""),
                about_refs=_extract_about_refs(result),
            )
        except Exception as exc:  # noqa: BLE001 - 共享脈絡是加分項
            logger.warning("retrieve_shared 失敗，略過：%s: %s", type(exc).__name__, exc)
            return None

    async def restrict_unit(
        self,
        unit_id: str,
        *,
        reason_code: str = "user_restrict",
        triggered_by: str = "conversation",
    ) -> dict[str, Any] | None:
        """把某句話收緊為私密，透傳引擎 ``restrict_unit``（寫入路徑）。

        回傳引擎的結果 dict；引擎沒有此方法或失敗回 None。
        """
        fn = getattr(self.engine, "restrict_unit", None)
        if fn is None:
            return None
        try:
            return await self._run(
                lambda: fn(unit_id, reason_code=reason_code, triggered_by=triggered_by),
                "write",
            )
        except Exception as exc:  # noqa: BLE001 - 收緊失敗只降級，由呼叫端決定是否提示
            logger.warning("restrict_unit 失敗，略過：%s: %s", type(exc).__name__, exc)
            return None

    async def scan_privacy_intent(
        self,
        unit_ids: Iterable[str],
        *,
        timeout: float = 20.0,
        force: bool = False,
    ) -> dict[str, Any] | None:
        """熱路徑外的隱私意圖掃描，透傳引擎 ``scan_privacy_intent``。

        最長阻塞約 ``timeout`` 秒（LLM 在引擎鎖外），所以走 read 路徑、不持寫鎖。
        建議回覆送出後以 fire-and-forget 呼叫，不要在回覆路徑上 await。
        引擎沒有此方法或失敗回 None。
        """
        fn = getattr(self.engine, "scan_privacy_intent", None)
        if fn is None:
            return None
        ids = list(unit_ids)
        if not ids:
            return None
        try:
            return await self._run(lambda: fn(ids, timeout=timeout, force=force), "read")
        except Exception as exc:  # noqa: BLE001 - 掃描是附加防線
            logger.warning("scan_privacy_intent 失敗，略過：%s: %s", type(exc).__name__, exc)
            return None

    async def revoke_owner(
        self,
        scope: Any,
        owner_uid: str,
        *,
        reason_code: str = "member_left",
        block: bool = True,
    ) -> Any | None:
        """成員退出場域：撤回其在該 scope 的所有投影，透傳 ``engine.share_layer.revoke_owner``。

        場域層操作，應呼叫在 scope engine（namespace ``scope-g<guild_id>``）的 adapter 上。
        回傳引擎的 ``RevokeResult``（型別註記用 Any）；引擎沒有 share_layer／此方法或失敗回 None。
        """
        layer = getattr(self.engine, "share_layer", None)
        fn = getattr(layer, "revoke_owner", None)
        if fn is None:
            return None
        try:
            return await self._run(
                lambda: fn(scope, owner_uid, reason_code=reason_code, block=block), "write"
            )
        except Exception as exc:  # noqa: BLE001 - 退出流程不因撤回失敗中斷
            logger.warning("revoke_owner 失敗，略過：%s: %s", type(exc).__name__, exc)
            return None

    async def restore_owner(self, scope: Any, owner_uid: str) -> bool | None:
        """成員回到場域：解除 ``revoke_owner(block=True)`` 的封鎖。

        透傳 ``engine.share_layer.restore_owner``。

        舊投影維持撤回，下次 Dream 重判後才重新投影。應呼叫在 scope engine 的 adapter 上。
        回傳引擎的 bool；引擎沒有 share_layer／此方法或失敗回 None。
        """
        layer = getattr(self.engine, "share_layer", None)
        fn = getattr(layer, "restore_owner", None)
        if fn is None:
            return None
        try:
            return await self._run(lambda: fn(scope, owner_uid), "write")
        except Exception as exc:  # noqa: BLE001 - 回歸流程不因解除失敗中斷
            logger.warning("restore_owner 失敗，略過：%s: %s", type(exc).__name__, exc)
            return None

    async def forget_activity(self, kind: str = "game_activity") -> bool:
        """退出用：清除活動彙整並遮蔽對應 observation 證據，透傳引擎 ``forget_activity``。

        會改動證據與彙整，所以走寫入路徑。成功回 True；引擎沒有此方法或失敗回 False。
        """
        fn = getattr(self.engine, "forget_activity", None)
        if fn is None:
            return False
        try:
            await self._run(lambda: fn(kind), "write")
            return True
        except Exception as exc:  # noqa: BLE001 - 失敗只回報，不拋
            logger.warning("forget_activity 失敗：%s: %s", type(exc).__name__, exc)
            return False

    async def dream(self, triggered_by: str = "manual") -> dict[str, Any]:
        """離線鞏固（蒸餾/重播/修剪）。背景執行，不擋對話。

        蒸餾走 ``llm_fn``（gpt-5.6-luna，同一顆 backend 的同步 query）；
        沒有 llm_fn 時 Dream Engine 會優雅跳過蒸餾步驟。
        """
        return await self._run(lambda: self.engine.dream(triggered_by), "dream")

    DISTILL_MIN_SALIENCE = 0.5
    """fallback：echo_memory 無法 import 時才用，值對齊 KnowledgeDistillation.
    MIN_SALIENCE_THRESHOLD。平常由 :meth:`_distill_min_salience` 從來源讀取。"""

    @classmethod
    def _distill_min_salience(cls) -> float:
        """蒸餾顯著性門檻——直接讀 echo_memory，避免複製的常數日後漂移。

        低於這個值的 episode 蒸餾根本不會看，算進 pending 只會讓排程空跑。
        """
        try:
            from echo_memory.dream.distillation import KnowledgeDistillation

            return float(KnowledgeDistillation.MIN_SALIENCE_THRESHOLD)
        except Exception:  # noqa: BLE001 - 舊版或尚未可 import：退回複製的常數
            return cls.DISTILL_MIN_SALIENCE

    async def pending_dream_count(self) -> int:
        """有多少 episode 等著被蒸餾（未 dreamed 且顯著性過門檻）。

        DreamScheduler 用這個數字決定「跑了會不會有產出」。
        list_episodes 單頁上限 200，所以分頁掃。
        """

        threshold = self._distill_min_salience()

        def _work() -> int:
            episodic = self.engine.episodic
            total = episodic.count()
            pending = 0
            offset = 0
            while offset < total:
                page = episodic.list_episodes(limit=200, offset=offset)
                if not page:
                    break
                pending += sum(
                    1
                    for ep in page
                    if not ep.is_dreamed
                    and ep.salience_score >= threshold
                )
                offset += len(page)
            return pending

        return await self._run(_work)

    def set_llm_fn(
        self,
        llm_fn: Callable[[str], str] | None,
        assess_llm_fn: Callable[[str], str] | None = None,
    ) -> None:
        """事後補上 LLM——backend 建立時機晚於 adapter 時用。

        ``llm_fn`` 給 dream 蒸餾；``assess_llm_fn`` 給每輪的 salience 評估
        （可以是壓低 reasoning 的便宜版本）。
        """
        self._llm_fn = llm_fn
        self._assess_llm_fn = assess_llm_fn
        self._assessor = None  # 下次用到時以新的 fn 重建
        self._judge = None
        if self._engine is not None and hasattr(self._engine, "set_llm_fn"):
            self._engine.set_llm_fn(llm_fn)


__all__ = ["EchoMemoryAdapter", "build_engine", "DEFAULT_NAMESPACE"]
