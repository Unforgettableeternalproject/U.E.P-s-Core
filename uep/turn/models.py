"""Turn Orchestrator 的資料型別（P3接入方案 §4.2、§1.5）。不依賴 echo_stream。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from uep.contracts.presence import AuthorizationTuple


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
    platform: str = "web"
    profile_actor_id: str | None = None  # 重設 profile 時以誰的長期記憶為準；None = 不帶 profile

    @property
    def event_scope(self) -> str:
        return self.scope_ref or self.audience_scope


@dataclass(frozen=True)
class UtteranceFact:
    """輸入端交來的一句話。``text`` 與 ``speaker_ref`` 是不受信資料：文字只走 payload_blob，身分只查表。"""

    speaker_ref: str  # 平台字串，如 ``discord:<id>``
    text: str
    seq: int  # session 內遞增；進冪等鍵 ``es:<session_ref>:<seq>``
    language: str | None = None
    confidence: float | None = None
    duration_ms: int | None = None
    legacy_ref: str | None = None  # 輸入端原 turn id（ES 的 uuid 前 12 碼），不當主鍵


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
    participant_count: int | None = None  # 在場人數（含說話者）；None = 未知
    addressed_hint: bool = False  # Core 以精確喚醒詞比對算出（與 proposer 無關），D80 逾時預設用
    deadline: float | None = None  # time.monotonic()；proposer 應在此之前回覆（Orchestrator 另以預算強制）


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


@dataclass(frozen=True)
class TurnOutcomeFacts:
    """回報給 proposer 的回合結果（選用的 ``OutcomeObserver``）。不含文字內容。"""

    turn_id: str
    candidate_id: str  # 對應 TurnCandidate.candidate_id（interjection 為 tick:<n>）
    speakers: tuple[str, ...]  # 這一輪回應的對象（speaker_ref）
    reply_mode: str
    reason_code: str  # Proposal 的原因碼（如 called、context、interject_silence）
    status: str  # completed／skipped／cancelled／voided／failed／dropped_busy
    end_reason: str | None = None  # 取消或跳過原因（pass、mode_silent、barge_in…）
    generation_ok: bool = False
    barge_in: bool = False
