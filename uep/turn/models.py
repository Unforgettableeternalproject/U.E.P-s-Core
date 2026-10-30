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
    candidate_id: str
    utterance_refs: tuple[str, ...]  # 事件 ID
    speakers: tuple[str, ...]  # speaker_ref
    ctx: SessionContext
    interaction_state: str | None
    interaction_version: int | None
    presence_active: bool


@dataclass(frozen=True)
class Proposal:
    """proposer 的提議（只是提議；Orchestrator 驗證並保留否決權）。"""

    reply_mode: ReplyMode
    addressed: bool = True
    confidence: float = 1.0
    route: str = "direct_chat"
    interaction_to: str | None = None  # S5 才處理；S3 不執行
    reason_code: str = "default_respond"
    proposer_id: str = "core.decision"
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
    """``on_utterance`` 的結果：``ticket`` 為 None 表示不開 turn（原因見 ``reason_code``）。"""

    ticket: CoreTicket | None
    reason_code: str
    proposal: Proposal | None = None
    duplicate: bool = False
    utterance_event_id: str | None = None


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
