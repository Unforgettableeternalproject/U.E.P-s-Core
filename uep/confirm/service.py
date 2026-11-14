"""確認服務（契約 §3A.2～§3A.4、§3A.6）：提案、辨認確認／拒絕、逾時、取代；結束階段與強制睡眠（PM-6）共用。

- 確認服務**不知道動作的內容**：只認 ``action`` 字串與註冊的 handler（``ConfirmationActionHandler``）；handler 不知道話語如何被辨認。
- 狀態轉移只在這裡寫入（單一寫入者，內部 RLock），終態不可再變；同一 proposal 只會有一次轉移。
- 只在有「已送達、未逾時」的 pending 時才辨認；無 pending 的「好」「是」只是一般對話。
- 確認者必須是提案時的同一已認證 actor（同平台、同對話、階級不低於 policy.min_tier）；他人的話完全不處理
  （只產生計數事件 ``confirmation.foreign_actor_ignored``，不含內容）。
- 確認話語須對她說（addressed ∨ 1 對 1 ∨ 回覆她的提示，D3）；否則不處理。
- 審計事件只留在內部佇列（``drain_events``），由呼叫端依自己的 ctx 發布；payload 只有 id／固定碼／計數，不含原文。
- 存放在記憶體：重啟＝全部視同過期，不補發事件（fail-closed）。
"""
from __future__ import annotations

import dataclasses
import logging
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from uep.confirm.lexicon import classify, parse_nonce
from uep.confirm.store import ConfirmationStore, InMemoryConfirmationStore, key_of
from uep.contracts.confirmation import (
    REASON_BINDING_STALE,
    REASON_CONFIRMED,
    REASON_DECLINED,
    REASON_DIRECT_ISSUED,
    REASON_INTERVENING,
    REASON_REPLACED,
    REASON_TTL,
    REASON_UNDELIVERED,
    ConfirmationAction,
    ConfirmationActionHandler,
    ConfirmationPolicy,
    ConfirmationPromptPolisher,
    ConfirmationProposal,
)
from uep.contracts.permission import RequesterTier
from uep.ids import new_id

logger = logging.getLogger("uep.confirm.service")

TTL_MIN_S = 30.0
TTL_MAX_S = 300.0
DEFAULT_TTL_ABANDON_S = 90.0
DEFAULT_TTL_FORCED_SLEEP_S = 120.0
DEFAULT_PROMPT_COOLDOWN_S = 20.0
DEFAULT_DECLINE_COOLDOWN_S = 300.0
DEFAULT_MAX_PENDING = 8
_MAX_PROMPT_LEN = 200

EVENT_PROPOSED = "confirmation.proposed"
EVENT_PROMPT_DELIVERED = "confirmation.prompt_delivered"
EVENT_CONFIRMED = "confirmation.confirmed"
EVENT_DECLINED = "confirmation.declined"
EVENT_EXPIRED = "confirmation.expired"
EVENT_SUPERSEDED = "confirmation.superseded"
EVENT_FOREIGN_IGNORED = "confirmation.foreign_actor_ignored"
CONFIRMATION_EVENT_TYPES = (
    EVENT_PROPOSED, EVENT_PROMPT_DELIVERED, EVENT_CONFIRMED, EVENT_DECLINED, EVENT_EXPIRED, EVENT_SUPERSEDED,
    EVENT_FOREIGN_IGNORED,
)
# 審計事件 payload 允許的額外鍵（登記於 uep/events/registry.py；只放 id／固定碼／計數）
CONFIRMATION_EVENT_KEYS = ("proposal_id", "action", "requester_ref", "path", "ttl_s", "reason", "count")


def bounded_ttl(value: object, default: float) -> float:
    """TTL 設定載入驗證：只接受 [30, 300] 秒內的數字，其餘回預設（D2）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value) if TTL_MIN_S <= value <= TTL_MAX_S else default


def default_policies(
    *, ttl_abandon_s: object = DEFAULT_TTL_ABANDON_S, ttl_forced_sleep_s: object = DEFAULT_TTL_FORCED_SLEEP_S,
) -> dict[str, ConfirmationPolicy]:
    """每個 action 一份政策（Core 程式常數，只能收緊）；TTL 由設定在 [30, 300] 內調整。"""
    return {
        "abandon_item": ConfirmationPolicy(
            ttl_s=bounded_ttl(ttl_abandon_s, DEFAULT_TTL_ABANDON_S), allow_colloquial=True,
            require_human_origin=False, min_tier=RequesterTier.OWNER, max_intervening=2,
        ),
        "forced_sleep": ConfirmationPolicy(
            ttl_s=bounded_ttl(ttl_forced_sleep_s, DEFAULT_TTL_FORCED_SLEEP_S), allow_colloquial=False,
            require_human_origin=True, min_tier=RequesterTier.OWNER, max_intervening=0,
        ),
    }


@dataclass(frozen=True)
class ConfirmationEvent:
    """審計事件（待呼叫端發布）。``conversation_ref`` 供呼叫端找回 ctx。"""

    event_type: str
    payload: Mapping[str, object]
    proposal_id: str
    conversation_ref: str


ProposeStatus = Literal[
    "created", "refreshed", "duplicate", "cooldown", "declined_cooldown", "unavailable", "limit",
]


@dataclass(frozen=True)
class ProposeResult:
    status: ProposeStatus
    proposal: ConfirmationProposal | None = None
    send_prompt: bool = False  # created／refreshed 才要（重新）送出提示


ResolveOutcome = Literal["none", "ignored", "foreign_ignored", "confirmed", "declined", "superseded", "expired", "unrelated"]


@dataclass(frozen=True)
class ResolveResult:
    outcome: ResolveOutcome
    proposal: ConfirmationProposal | None = None
    consumed: bool = False  # True＝這句話是確認／拒絕（含 binding 失效）本身：呼叫端不再進放棄／承諾判斷


class ConfirmationService:
    """確認提案的唯一寫入者。"""

    def __init__(
        self, *, store: ConfirmationStore | None = None, policies: Mapping[str, ConfirmationPolicy] | None = None,
        clock: Callable[[], datetime], prompt_cooldown_s: float = DEFAULT_PROMPT_COOLDOWN_S,
        decline_cooldown_s: float = DEFAULT_DECLINE_COOLDOWN_S, max_pending: int = DEFAULT_MAX_PENDING,
        polisher: ConfirmationPromptPolisher | None = None, polish_enabled: bool = False,
    ) -> None:
        self._store: ConfirmationStore = store if store is not None else InMemoryConfirmationStore()
        self._policies = dict(policies) if policies is not None else default_policies()
        self._clock = clock
        self._prompt_cooldown = timedelta(seconds=prompt_cooldown_s)
        self._decline_cooldown = timedelta(seconds=decline_cooldown_s)
        self._max_pending = max_pending
        self._polisher = polisher
        self._polish_enabled = bool(polish_enabled)
        self._handlers: dict[str, ConfirmationActionHandler] = {}
        self._lock = threading.RLock()
        self._events: list[ConfirmationEvent] = []
        self._last_prompt: dict[tuple[str, str], datetime] = {}  # (requester_ref, action) -> 最近一次提示送達
        self._last_declined: dict[tuple[str, str], datetime] = {}  # (conversation_ref, action) -> 最近一次拒絕
        self.metrics: dict[str, int] = {}

    # ---- 註冊與查詢 ----

    def register_handler(self, handler: ConfirmationActionHandler, *, replace: bool = False) -> None:
        """註冊動作端 handler；action 必須有對應政策。重複註冊需 ``replace=True``。"""
        action = handler.action
        if action not in self._policies:
            raise ValueError(f"沒有 {action!r} 的政策")
        with self._lock:
            if action in self._handlers and not replace:
                raise ValueError(f"{action!r} 已有 handler")
            self._handlers[action] = handler

    def has_handler(self, action: str) -> bool:
        return action in self._handlers

    def policy(self, action: str) -> ConfirmationPolicy | None:
        return self._policies.get(action)

    def get(self, proposal_id: str) -> ConfirmationProposal | None:
        return self._store.get(proposal_id)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._store.pending())

    def drain_events(self) -> list[ConfirmationEvent]:
        """取走並清空待發布的審計事件。"""
        with self._lock:
            events, self._events = self._events, []
            return events

    def _count(self, name: str) -> None:
        self.metrics[name] = self.metrics.get(name, 0) + 1

    def _emit(self, event_type: str, proposal: ConfirmationProposal, **extra: object) -> None:
        payload: dict[str, object] = {
            "proposal_id": proposal.proposal_id, "action": proposal.action, "requester_ref": proposal.requester_ref,
        }
        payload.update(extra)
        self._events.append(ConfirmationEvent(event_type, payload, proposal.proposal_id, proposal.conversation_ref))

    # ---- 狀態轉移（只在這裡寫）----

    def _finish(
        self, proposal: ConfirmationProposal, status: Literal["confirmed", "declined", "expired", "superseded"],
        reason: str, now: datetime,
    ) -> ConfirmationProposal:
        if proposal.status != "pending":  # 終態不可再變
            return proposal
        done = dataclasses.replace(proposal, status=status, resolved_at=now, resolved_reason=reason)
        self._store.put(done)
        event = {
            "confirmed": EVENT_CONFIRMED, "declined": EVENT_DECLINED, "expired": EVENT_EXPIRED,
            "superseded": EVENT_SUPERSEDED,
        }[status]
        extra: dict[str, object] = {} if status in ("confirmed", "declined") else {"reason": reason}
        self._emit(event, done, **extra)
        self._count(f"confirmation.{status}")
        return done

    def _sweep(self, now: datetime) -> list[ConfirmationProposal]:
        expired = []
        for p in self._store.pending():
            if now >= p.expires_at:
                expired.append(self._finish(p, "expired", REASON_TTL, now))
        return expired

    def expire_due(self, now: datetime | None = None) -> list[ConfirmationProposal]:
        """掃描逾時（orchestrator tick 呼叫）。過期只記事件，不通知使用者。"""
        with self._lock:
            return self._sweep(now or self._clock())

    # ---- 提案 ----

    def propose(
        self, *, action: ConfirmationAction, requester_ref: str, source_platform: str, conversation_ref: str,
        source_utterance_ref: str, actor_tier: RequesterTier,
    ) -> ProposeResult:
        """建立提案（呼叫端已確認已認證 owner、addressed）；handler.prepare 為 None 或政策不允許 → ``unavailable``。"""
        with self._lock:
            now = self._clock()
            self._sweep(now)
            policy, handler = self._policies.get(action), self._handlers.get(action)
            if policy is None or handler is None or actor_tier < policy.min_tier:
                return ProposeResult("unavailable")
            existing = self._store.by_utterance(source_utterance_ref)
            if existing is not None:  # 同一句重送：冪等，不重發提示
                return ProposeResult("duplicate", existing)
            declined_at = self._last_declined.get((conversation_ref, action))
            if declined_at is not None and now - declined_at < self._decline_cooldown:
                self._count("confirmation.declined_cooldown")
                return ProposeResult("declined_cooldown")
            try:
                prepared = handler.prepare(actor_ref=requester_ref, now=now)
            except Exception:
                logger.exception("確認 handler.prepare 失敗（當作不可動作）")
                prepared = None
            if prepared is None:
                return ProposeResult("unavailable")
            key = (conversation_ref, requester_ref, action)
            pending = self._store.pending_for_key(key)
            if pending is not None:
                if dict(pending.binding) == dict(prepared.binding):  # 同一個行程項：不重發提示，冷卻後才更新期限
                    last = self._last_prompt.get((requester_ref, action))
                    if last is not None and now - last < self._prompt_cooldown:
                        self._count("confirmation.prompt_cooldown")
                        return ProposeResult("cooldown", pending)
                    refreshed = dataclasses.replace(pending, expires_at=now + timedelta(seconds=policy.ttl_s))
                    self._store.put(refreshed)
                    return ProposeResult("refreshed", refreshed, send_prompt=True)
                self._finish(pending, "superseded", REASON_REPLACED, now)
            else:
                last = self._last_prompt.get((requester_ref, action))
                if last is not None and now - last < self._prompt_cooldown:
                    self._count("confirmation.prompt_cooldown")
                    return ProposeResult("cooldown")
            if len(self._store.pending()) >= self._max_pending:
                self._count("confirmation.proposal_limit")
                return ProposeResult("limit")
            proposal = ConfirmationProposal(
                proposal_id=new_id("cfm"), action=action, nonce=secrets.token_hex(4), requester_ref=requester_ref,
                source_platform=source_platform, conversation_ref=conversation_ref, path="turn",
                summary=prepared.summary, binding=dict(prepared.binding), source_utterance_ref=source_utterance_ref,
                created_at=now, expires_at=now + timedelta(seconds=policy.ttl_s),
            )
            self._store.put(proposal)
            self._emit(EVENT_PROPOSED, proposal, path=proposal.path, ttl_s=int(policy.ttl_s))
            self._count("confirmation.proposed")
            return ProposeResult("created", proposal, send_prompt=True)

    def mark_prompt_delivered(self, proposal_id: str) -> ConfirmationProposal | None:
        """提示確實送出（Gate 接納）後呼叫；之後的發言才可確認。"""
        with self._lock:
            now = self._clock()
            p = self._store.get(proposal_id)
            if p is None or p.status != "pending":
                return None
            delivered = dataclasses.replace(p, prompt_delivered_at=p.prompt_delivered_at or now)
            self._store.put(delivered)
            self._last_prompt[(p.requester_ref, p.action)] = now
            self._emit(EVENT_PROMPT_DELIVERED, delivered)
            return delivered

    def mark_undelivered(self, proposal_id: str) -> ConfirmationProposal | None:
        """提示送不出去（被 Gate 擋、沒有輸出管線）→ superseded(undelivered)。"""
        with self._lock:
            p = self._store.get(proposal_id)
            if p is None or p.status != "pending":
                return None
            return self._finish(p, "superseded", REASON_UNDELIVERED, self._clock())

    def supersede_for_direct(self, *, conversation_ref: str, requester_ref: str) -> list[ConfirmationProposal]:
        """owner 改說 direct 指示：同對話同 actor 的 pending 全部 superseded(direct_issued)；direct 照常生效。"""
        with self._lock:
            now = self._clock()
            return [
                self._finish(p, "superseded", REASON_DIRECT_ISSUED, now)
                for p in self._store.pending_in_conversation(conversation_ref) if p.requester_ref == requester_ref
            ]

    # ---- 辨認 ----

    def try_resolve(
        self, *, conversation_ref: str, actor_ref: str, actor_authenticated: bool, source_platform: str, text: str,
        addressed: bool, solo: bool, actor_tier: RequesterTier, is_prompt_reply: bool = False,
        is_trigger: bool = False,
    ) -> ResolveResult:
        """每個 turn 的入口先呼叫：同 actor 有已送達的 pending 時辨認確認／拒絕／無關。

        ``is_trigger``：這句本身是結束類觸發（direct／needs_confirmation 的句型）時不計入 ``max_intervening``。
        回傳 ``consumed=True`` 時呼叫端不得再把這句送進放棄／承諾判斷（仍進一般對話流程）。"""
        with self._lock:
            now = self._clock()
            self._sweep(now)
            live = [
                p for p in self._store.pending_in_conversation(conversation_ref)
                if p.prompt_delivered_at is not None and now < p.expires_at
            ]
            if not live:
                return ResolveResult("none")
            mine = [
                p for p in live
                if actor_authenticated and actor_ref == p.requester_ref and source_platform == p.source_platform
                and actor_tier >= self._policies[p.action].min_tier
            ]
            nonce = parse_nonce(text)
            kind = classify(text)
            if not mine:
                if nonce is not None or kind != "other":
                    self._emit_foreign(live[0])
                return ResolveResult("foreign_ignored" if nonce is not None or kind != "other" else "none")
            if not (addressed or solo or is_prompt_reply):  # D3：不是對她說的話不處理
                return ResolveResult("ignored")
            mine.sort(key=lambda p: p.created_at, reverse=True)  # 最新的優先
            target: ConfirmationProposal | None = None
            verdict: Literal["confirm", "decline", "other"] = "other"
            if nonce is not None:
                target = next((p for p in mine if p.nonce == nonce), None)
                if target is not None:
                    verdict = "confirm"
            elif kind == "confirm":
                target = next((p for p in mine if self._policies[p.action].allow_colloquial), None)
                if target is not None:
                    verdict = "confirm"
            elif kind == "decline":
                target, verdict = mine[0], "decline"
            if target is None or verdict == "other":
                return self._unrelated(mine, now, count=not is_trigger)
            if verdict == "decline":
                self._last_declined[(target.conversation_ref, target.action)] = now
                return ResolveResult("declined", self._finish(target, "declined", REASON_DECLINED, now), consumed=True)
            handler = self._handlers.get(target.action)
            try:
                valid = handler is not None and handler.validate(target.binding, now=now)
            except Exception:
                logger.exception("確認 handler.validate 失敗（當作 binding 失效）")
                valid = False
            if not valid:
                return ResolveResult(
                    "superseded", self._finish(target, "superseded", REASON_BINDING_STALE, now), consumed=True)
            return ResolveResult("confirmed", self._finish(target, "confirmed", REASON_CONFIRMED, now), consumed=True)

    def _unrelated(self, mine: list[ConfirmationProposal], now: datetime, *, count: bool) -> ResolveResult:
        if not count:
            return ResolveResult("unrelated", mine[0])
        expired: ConfirmationProposal | None = None
        for p in mine:
            if self._store.bump_intervening(p.proposal_id) > self._policies[p.action].max_intervening:
                expired = self._finish(p, "expired", REASON_INTERVENING, now)
        if expired is not None:
            return ResolveResult("expired", expired)
        return ResolveResult("unrelated", mine[0])

    def _emit_foreign(self, proposal: ConfirmationProposal) -> None:
        self._emit(EVENT_FOREIGN_IGNORED, proposal, count=1)
        self._count("confirmation.foreign_actor_ignored")

    # ---- 提示文字 ----

    def render_prompt(self, proposal: ConfirmationProposal, *, audience: str, locale: str = "zh-TW") -> str | None:
        """固定模板（handler.render）；可選潤飾擴充點預設關，失敗或驗證不過一律退回固定模板。無 handler 回 None。"""
        handler = self._handlers.get(proposal.action)
        if handler is None:
            return None
        try:
            text = handler.render(proposal.summary, audience=audience, locale=locale)
        except Exception:
            logger.exception("確認 handler.render 失敗")
            return None
        if not self._polish_enabled or self._polisher is None:
            return text
        try:
            polished = self._polisher.polish(text, proposal.summary)
        except Exception:
            logger.warning("提示潤飾失敗，退回固定模板", exc_info=True)
            return text
        terms_fn = getattr(handler, "required_terms", None)
        terms = tuple(terms_fn(proposal.summary, audience=audience)) if callable(terms_fn) else ()
        if (
            isinstance(polished, str) and polished.strip() and len(polished) <= _MAX_PROMPT_LEN
            and all(t in polished for t in terms)
        ):
            return polished
        self._count("confirmation.polish_rejected")
        return text


__all__ = [
    "CONFIRMATION_EVENT_KEYS", "CONFIRMATION_EVENT_TYPES", "ConfirmationEvent", "ConfirmationService",
    "ProposeResult", "ResolveResult", "bounded_ttl", "default_policies", "key_of",
]
