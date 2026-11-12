"""Turn Orchestrator 的資料型別（P3接入方案 §4.2、§1.5）。不依賴 echo_stream。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal

from uep.contracts.platform import normalize_platform
from uep.contracts.presence import AuthorizationTuple, PresenceLocation


class ReplyMode(StrEnum):
    """Proposal 的 reply_mode 詞彙（Q6）；ES 只看到不透明字串。"""

    RESPOND = "respond"
    PASSIVE = "passive"
    PASS = "pass"
    DEFER = "defer"


@dataclass(frozen=True)
class SessionContext:
    """目前對話的範圍。``audience_scope`` 決定記憶與歷史的可見範圍（X-1）；``scope_ref`` 為事件信封的 scope。"""

    conversation_id: str
    segment_id: str
    audience_scope: str
    session_ref: str  # 只能含 [A-Za-z0-9_:.-]；進冪等鍵（E-20）
    scope_ref: str | None = None  # 預設與 audience_scope 相同
    guild_id: str | None = None  # 共享層成員與在場名單由它推導
    platform: str = "website"  # Presence location 詞彙；舊值 "web" 於建構時正規化（見 __post_init__）
    profile_actor_id: str | None = None  # 重設 profile 時以誰的長期記憶為準；None = 不帶 profile

    def __post_init__(self) -> None:
        # 讀取相容層（DQ-3-B）：舊值 "web" 只在這個邊界正規化成 "website"；寫入一律用新詞彙
        normalized = normalize_platform(self.platform)
        if normalized != self.platform:
            object.__setattr__(self, "platform", normalized)

    @property
    def event_scope(self) -> str:
        return self.scope_ref or self.audience_scope


@dataclass(frozen=True)
class UtteranceFact:
    """輸入端交來的一句話。``text`` 與 ``speaker_ref`` 是不受信資料：文字只走 payload_blob，身分只查表。"""

    speaker_ref: str  # 平台字串，如 ``discord:<id>``
    text: str = field(repr=False)
    seq: int  # session 內遞增；進冪等鍵 ``es:<session_ref>:<seq>``
    language: str | None = None
    confidence: float | None = None
    duration_ms: int | None = None
    legacy_ref: str | None = None  # 輸入端原 turn id（ES 的 uuid 前 12 碼），不當主鍵
    # V18：這句輸入實際從哪個場域進來（不是說話者身分，也不是 session.platform）。None = 來源未宣告（現狀放行並計數）。
    # 只能由行程內的簽入點填入（Host 輸入 stage、Console）；外部 adapter 的值由 Core ingress 以已驗證 endpoint 簽發，不得從 wire 反序列化
    source_location: PresenceLocation | None = None
    source_endpoint_id: str | None = None  # 選填，對應 §10.2 endpoint 粒度；目前只記錄、不比對
    # 這句是否由輸入 stage 辨識出來（語音路徑，由 Host 的 stage 消費點標記）：分流 aggregator 只緩衝語音，
    # 文字輸入與 Console 的整段辨識（/mic）一律逐句通過
    from_voice: bool = False


@dataclass(frozen=True)
class TurnCandidate:
    """聚合後的一個 turn（TurnAggregator 的輸出）送進 proposer 的輸入。

    ``text`` 偏離 §4.2（以 ref 取）：預設 proposer 需要文字判斷喚醒詞，行程內直接帶（``repr=False``，不進日誌）；
    外部實作應以 ``utterance_refs`` 自行在 audience 內讀取。
    """

    candidate_id: str
    utterance_refs: tuple[str, ...]  # 事件 ID
    speakers: tuple[str, ...]  # speaker_ref
    ctx: SessionContext
    interaction_state: str | None
    interaction_version: int | None
    presence_active: bool
    text: str = field(repr=False, default="")
    segments: int = 1  # 合併了幾句 utterance
    participant_count: int | None = None  # 在場人數（含說話者）；Orchestrator 在未知時填 1（與 D80 一致）
    addressed_hint: bool = False  # Core 以精確喚醒詞比對算出（與 proposer 無關），D80 逾時預設用
    deadline: float | None = None  # time.monotonic()；proposer 應在此之前回覆（Orchestrator 另以預算強制）


Degraded = Literal["action_unavailable"]  # CoreTicket.degraded 的唯一值：這個動作目前沒有可用的工具


@dataclass(frozen=True)
class Proposal:
    """proposer 的提議（只是提議；Orchestrator 驗證並保留否決權）。"""

    reply_mode: ReplyMode
    addressed: bool = True
    confidence: float = 1.0
    route: str = "direct_chat"
    interaction_to: str | None = None  # 建議的 Interaction 軸目標狀態；Orchestrator 驗證後以 core.decision 提 CC-7
    reason_code: str = "default_respond"
    proposer_id: str = "unknown"  # 實作識別（稽核用）；CC-7 的提議者身分是 Orchestrator 固定的 core.decision
    proposer_version: str = "0"
    # DL-4：只能收窄的 capability 提議（None = 不收窄；() = 要求空集合）；最終標籤由決策層純函式計算，不由此欄決定
    capability_hint: tuple[str, ...] | None = None
    # Chain 契約 §3.3：是否含兩個以上獨立指示（None = proposer 不支援）；目前只是欄位，不影響任何決策
    compound: bool | None = None
    # defer 因能力不足被轉成 respond 時，保留 proposer 原本的原因碼（決策日誌與稽核可對照；一般提議為 None）
    original_reason_code: str | None = None


@dataclass(frozen=True)
class CoreTicket:
    """Core 簽發的完整 ticket（ES 只看到其中的子集）。"""

    turn_id: str
    turn_generation: int
    source_turn_ref: str | None
    conversation_id: str
    segment_id: str
    audience_scope: str
    event_scope: str
    reply_mode: str
    auth: AuthorizationTuple
    valid_until: datetime
    snapshot_version: int
    speaker_ref: str
    actor_id: str
    user_text: str = field(repr=False, default="")
    # 降級事實（Chain 契約 Q-S）：agent_loop／task_request 因 capability_empty／capability_projection_empty
    # 退回 direct_chat 時為 "action_unavailable"，意思是「這個動作目前沒有可用的工具」。是事實不是指令，不含使用者可見字串
    degraded: Degraded | None = None


@dataclass(frozen=True)
class IntakeResult:
    """``on_utterance`` 與 ``tick`` 的結果：``ticket`` 為 None 表示不開 turn（原因見 ``reason_code``）。

    ``reason_code="buffered"``：utterance 已記錄、仍在 aggregator 等待合併（不發 ``turn.skipped``）。
    一次呼叫可能完成多個 turn：第一個在本物件上，其餘在 ``extra``。``fact`` 是該 turn 的代表事實（最後一句，
    文字為合併後全文），宿主據以建 ES 請求；``yield_for`` 為「對方又講了新內容」，宿主應讓該說話者尚未開播的輪讓位。
    """

    ticket: CoreTicket | None
    reason_code: str
    proposal: Proposal | None = None
    duplicate: bool = False
    utterance_event_id: str | None = None
    fact: UtteranceFact | None = None
    extra: tuple[IntakeResult, ...] = ()
    yield_for: str | None = None

    def all_results(self) -> tuple[IntakeResult, ...]:
        return (self, *self.extra)


@dataclass(frozen=True)
class TurnEndFacts:
    """ES 回報的回合結束事實（轉接層已轉成與 ES 無關的字串）。

    ``status``：completed／skipped／cancelled／voided／failed／dropped_busy。
    ``reason_code``：取消原因碼或跳過原因；``generation_ok``：文字生成確實完成（C4 outcome 推導）。
    """

    status: str
    reason_code: str | None = None
    generation_ok: bool = False
    generated_len: int = 0
    spoken_text: str = ""
    attempt_no: int = 1
    snapshot_version: int = 0
    barge_in: bool = False  # 使用者插話打斷了這一輪（outcome.barge_in）；Q-1 的冷卻由 proposer 讀它
    # D5：ES 的 cancel_turn 撤回結果（outcome.metadata）。None = outcome 沒有帶（非宿主取消）；
    # True = 確定這一輪不在歷史中；False = 不保證。error 只供 log 與 metric，不進任何事件。
    history_retracted: bool | None = None
    history_retract_error: str | None = None
    partial: bool = False  # 預算中斷（FAILED＋partial）：收到的是部分結果，不是完整回覆；記憶 store 不支援標記前不寫入


@dataclass(frozen=True)
class TurnOutcomeFacts:
    """回報給 proposer 的回合結果（選用的 ``OutcomeObserver``）。除 ``reply_text`` 外不含文字內容。"""

    turn_id: str
    candidate_id: str  # 對應 TurnCandidate.candidate_id（interjection 為 tick:<n>）
    speakers: tuple[str, ...]  # 這一輪回應的對象（speaker_ref）
    reply_mode: str
    reason_code: str  # Proposal 的原因碼（如 called、context、interject_silence）
    status: str  # completed／skipped／cancelled／voided／failed／dropped_busy
    end_reason: str | None = None  # 取消或跳過原因（pass、mode_silent、barge_in…）
    generation_ok: bool = False
    barge_in: bool = False
    started: bool = True  # ES 真的開始 think 過（False = 從未開始生成就結束：dropped_busy、void、讓位）
    # 她這一輪實際說出口的文字（行程內專用，不進日誌）；空字串 = 拿不到。供「單字附和」判斷她上一句是否在等回應
    reply_text: str = field(repr=False, default="")
