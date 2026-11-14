"""確認提案存放（契約 §3A.4 D5）：``ConfirmationStore`` Protocol＋記憶體實作。

記憶體存放：TTL 很短、重啟時一律視同過期（fail-closed）；審計事件才是持久紀錄。Protocol 保留之後換 DB 實作的空間，
不改上層。實作需執行緒安全；單一寫入者（``ConfirmationService``）負責狀態轉移。
"""
from __future__ import annotations

import threading
from typing import Protocol

from uep.contracts.confirmation import ConfirmationProposal

ProposalKey = tuple[str, str, str]  # (conversation_ref, requester_ref, action）；persona 由服務實例隱含


def key_of(proposal: ConfirmationProposal) -> ProposalKey:
    return (proposal.conversation_ref, proposal.requester_ref, proposal.action)


class ConfirmationStore(Protocol):
    def put(self, proposal: ConfirmationProposal) -> None: ...
    def get(self, proposal_id: str) -> ConfirmationProposal | None: ...
    def by_utterance(self, source_utterance_ref: str) -> ConfirmationProposal | None: ...
    def pending(self) -> list[ConfirmationProposal]: ...
    def pending_in_conversation(self, conversation_ref: str) -> list[ConfirmationProposal]: ...
    def pending_for_key(self, key: ProposalKey) -> ConfirmationProposal | None: ...
    def bump_intervening(self, proposal_id: str) -> int: ...
    def intervening(self, proposal_id: str) -> int: ...


class InMemoryConfirmationStore:
    """執行緒安全的記憶體 store；終態提案只保留最近 ``keep_terminal`` 筆（供冪等查詢），避免無限成長。"""

    def __init__(self, *, keep_terminal: int = 256) -> None:
        self._lock = threading.RLock()
        self._items: dict[str, ConfirmationProposal] = {}
        self._by_utterance: dict[str, str] = {}
        self._intervening: dict[str, int] = {}
        self._terminal_order: list[str] = []
        self._keep_terminal = keep_terminal

    def put(self, proposal: ConfirmationProposal) -> None:
        with self._lock:
            self._items[proposal.proposal_id] = proposal
            self._by_utterance[proposal.source_utterance_ref] = proposal.proposal_id
            if proposal.status != "pending":
                self._intervening.pop(proposal.proposal_id, None)
                if proposal.proposal_id not in self._terminal_order:
                    self._terminal_order.append(proposal.proposal_id)
                while len(self._terminal_order) > self._keep_terminal:
                    old = self._terminal_order.pop(0)
                    dropped = self._items.pop(old, None)
                    if dropped is not None and self._by_utterance.get(dropped.source_utterance_ref) == old:
                        del self._by_utterance[dropped.source_utterance_ref]

    def get(self, proposal_id: str) -> ConfirmationProposal | None:
        with self._lock:
            return self._items.get(proposal_id)

    def by_utterance(self, source_utterance_ref: str) -> ConfirmationProposal | None:
        with self._lock:
            pid = self._by_utterance.get(source_utterance_ref)
            return self._items.get(pid) if pid is not None else None

    def pending(self) -> list[ConfirmationProposal]:
        with self._lock:
            return [p for p in self._items.values() if p.status == "pending"]

    def pending_in_conversation(self, conversation_ref: str) -> list[ConfirmationProposal]:
        return [p for p in self.pending() if p.conversation_ref == conversation_ref]

    def pending_for_key(self, key: ProposalKey) -> ConfirmationProposal | None:
        for p in self.pending():
            if key_of(p) == key:
                return p
        return None

    def bump_intervening(self, proposal_id: str) -> int:
        with self._lock:
            self._intervening[proposal_id] = self._intervening.get(proposal_id, 0) + 1
            return self._intervening[proposal_id]

    def intervening(self, proposal_id: str) -> int:
        with self._lock:
            return self._intervening.get(proposal_id, 0)


