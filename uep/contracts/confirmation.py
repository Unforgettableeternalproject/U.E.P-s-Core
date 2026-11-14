"""確認流程契約型別（PM-2b；權限與指示分級契約 §3A.2）：結束階段（abandon_item）與強制睡眠（forced_sleep，PM-6）共用。

- 零依賴純契約：只含資料型別與 Protocol，不含辨認規則、存放或行程知識（那些在 ``uep/confirm`` 與動作端 handler）。
- ``ConfirmationSummary`` 只放模板參數，**不含任何話語原文**（INV-07、E-11）。
- 提案與確認一律只綁「同一已認證 actor」；確認只為這一次授權（只綁該 proposal 的 binding），不累積。
- 不在場（absent）路徑沒有口語結束／確認（D7）：``path`` 只有 ``turn``。
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from uep.contracts.permission import RequesterTier

ConfirmationAction = Literal["abandon_item", "forced_sleep"]  # 閉集；新增＝改程式碼＋測試＋審核
ConfirmationStatus = Literal["pending", "confirmed", "declined", "expired", "superseded"]
CONFIRMATION_ACTIONS: tuple[str, ...] = ("abandon_item", "forced_sleep")
CONFIRMATION_STATUSES: tuple[str, ...] = ("pending", "confirmed", "declined", "expired", "superseded")
TERMINAL_STATUSES = frozenset({"confirmed", "declined", "expired", "superseded"})

# resolved_reason 固定碼
REASON_CONFIRMED = "confirmed"
REASON_DECLINED = "declined"
REASON_TTL = "ttl"
REASON_INTERVENING = "intervening"
REASON_BINDING_STALE = "binding_stale"
REASON_REPLACED = "replaced"
REASON_DIRECT_ISSUED = "direct_issued"
REASON_UNDELIVERED = "undelivered"
RESOLVED_REASONS = frozenset({
    REASON_CONFIRMED, REASON_DECLINED, REASON_TTL, REASON_INTERVENING, REASON_BINDING_STALE, REASON_REPLACED,
    REASON_DIRECT_ISSUED, REASON_UNDELIVERED,
})

_PROPOSAL_ID = re.compile(r"^cfm_[0-9A-HJKMNP-TV-Z]{26}$")
_NONCE = re.compile(r"^[a-z0-9]{8,32}$")
_REF = re.compile(r"^[A-Za-z0-9_.:\-]{1,160}$")


@dataclass(frozen=True)
class ConfirmationSummary:
    """複述內容：只有模板參數，不含話語原文。"""

    action: ConfirmationAction
    activity_kind: str | None = None  # work／social（行程類別固定代碼）；強制睡眠為 None
    window: tuple[datetime, datetime | None] | None = None  # 該行程項本次出現的起訖（本地時間渲染）
    location: str | None = None  # 平台代碼或 host；公開 audience 時渲染為「目前的行程」
    extras: Mapping[str, int] = field(default_factory=dict)  # 強制睡眠：將取消的格數、受影響承諾數等計數


@dataclass(frozen=True)
class ConfirmationPolicy:
    """每個 action 一份，Core 程式常數（只能收緊，PERM-9）。"""

    ttl_s: float  # 逾時秒數
    allow_colloquial: bool  # 是否接受口語確認（是／好）；False＝只認 nonce 或 console
    require_human_origin: bool  # 強制睡眠 True（§5.2）
    min_tier: RequesterTier  # 提案者與確認者的階級下限
    max_intervening: int  # 同一 actor 的無關發言容忍次數，超過即 expired（0＝任何無關發言即作廢）

    def __post_init__(self) -> None:
        if not (isinstance(self.ttl_s, (int, float)) and self.ttl_s > 0):
            raise ValueError("ttl_s 必須為正數")
        if not (isinstance(self.max_intervening, int) and self.max_intervening >= 0):
            raise ValueError("max_intervening 必須為非負整數")


@dataclass(frozen=True)
class PreparedConfirmation:
    """動作端 ``prepare`` 的結果：綁定的最小事實＋複述參數。"""

    binding: Mapping[str, str]  # abandon_item→{item_ref, planned_start_at, round_start}；forced_sleep→{round_start, plan_revision}
    summary: ConfirmationSummary


@dataclass(frozen=True)
class ConfirmationProposal:
    """一次待確認的提案。終態不可再變；轉移只由 ``ConfirmationService`` 寫入。"""

    proposal_id: str  # cfm_<ULID>（小寫化後可作 reason_ref）
    action: ConfirmationAction
    nonce: str  # Core 產生的隨機字串（>= 8 字元，小寫英數）；確定性通道 `確認 <nonce>`／`confirm <nonce>`
    requester_ref: str  # 提案時的已認證 actor（平台認證 ID 形式；確認者必須同一人）
    source_platform: str  # 提案發生的平台（確認須同平台）
    conversation_ref: str  # 提案所在對話（conversation_id＋segment scope；確認須同對話）
    path: Literal["turn"]  # 不在場路徑沒有口語結束邏輯（D7）
    summary: ConfirmationSummary
    binding: Mapping[str, str]
    source_utterance_ref: str  # 觸發提案的 utterance id；冪等鍵 cfm:<utterance_id>
    created_at: datetime
    expires_at: datetime
    prompt_delivered_at: datetime | None = None  # 提示確實送出（Gate 接納）後才可確認
    status: ConfirmationStatus = "pending"
    resolved_at: datetime | None = None
    resolved_reason: str | None = None  # 固定碼，見 RESOLVED_REASONS

    def __post_init__(self) -> None:  # fail-closed
        if _PROPOSAL_ID.fullmatch(self.proposal_id or "") is None:
            raise ValueError("proposal_id 必須是 cfm_<ULID>")
        if self.action not in CONFIRMATION_ACTIONS:
            raise ValueError("action 不在閉集內")
        if _NONCE.fullmatch(self.nonce or "") is None:
            raise ValueError("nonce 必須是 8 字元以上的小寫英數")
        for name in ("requester_ref", "source_platform", "conversation_ref", "source_utterance_ref"):
            value = getattr(self, name)
            if not isinstance(value, str) or _REF.fullmatch(value) is None:
                raise ValueError(f"{name} 不符安全字元集")
        if self.path != "turn":
            raise ValueError("path 只能是 turn（不在場路徑不支援口語結束，D7）")
        if self.status not in CONFIRMATION_STATUSES:
            raise ValueError("status 不在閉集內")
        for name in ("created_at", "expires_at"):
            if getattr(self, name).tzinfo is None:
                raise ValueError(f"{name} 必須帶時區")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at 必須晚於 created_at")
        if self.status != "pending" and (self.resolved_at is None or self.resolved_reason not in RESOLVED_REASONS):
            raise ValueError("終態必須帶 resolved_at 與固定的 resolved_reason")

    @property
    def reason_ref(self) -> str:
        """小寫化的 ``confirmation:<proposal_id>``，符合 AbandonWorkSignal 的 blob 引用格式。"""
        return f"confirmation:{self.proposal_id.lower()}"


@runtime_checkable
class ConfirmationActionHandler(Protocol):
    """動作端（持有行程投影等）：確認服務只知道 action 字串與 handler，不知道動作內容。"""

    action: ConfirmationAction

    def prepare(self, *, actor_ref: str, now: datetime) -> PreparedConfirmation | None:
        """讀當下狀態：可動作 → 回 binding＋summary；否則 None（不提案，不打擾）。"""
        ...

    def validate(self, binding: Mapping[str, str], *, now: datetime) -> bool:
        """確認時重驗 binding；不符 → 提案 superseded(binding_stale)。"""
        ...

    def render(self, summary: ConfirmationSummary, *, audience: str, locale: str) -> str:
        """固定模板（zh-TW）；公開 audience 時只渲染最小資訊。"""
        ...


class ConfirmationPromptPolisher(Protocol):
    """可選的提示潤飾擴充點（D8，預設關：``UEP_CONFIRM_POLISH_ENABLED``）。

    輸入固定模板字串與摘要，輸出潤飾後文字；結果須通過服務端驗證（仍含必要語意詞），失敗或例外一律退回固定模板。"""

    def polish(self, template_text: str, summary: ConfirmationSummary) -> str | None: ...


__all__ = [
    "CONFIRMATION_ACTIONS",
    "CONFIRMATION_STATUSES",
    "REASON_BINDING_STALE",
    "REASON_CONFIRMED",
    "REASON_DECLINED",
    "REASON_DIRECT_ISSUED",
    "REASON_INTERVENING",
    "REASON_REPLACED",
    "REASON_TTL",
    "REASON_UNDELIVERED",
    "RESOLVED_REASONS",
    "TERMINAL_STATUSES",
    "ConfirmationAction",
    "ConfirmationActionHandler",
    "ConfirmationPolicy",
    "ConfirmationPromptPolisher",
    "ConfirmationProposal",
    "ConfirmationStatus",
    "ConfirmationSummary",
    "PreparedConfirmation",
]
