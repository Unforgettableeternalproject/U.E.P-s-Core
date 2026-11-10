"""``InMemoryLoopContextStore``：``LoopContextStore`` 的離線實作（child session 與 parent 寫入都在記憶體）。

child 以 ``messages_to_json`` 存放（與 production 持久化同一份表示），取回時 ``messages_from_json``；
「崩潰」＝丟棄 AgentLoop 物件、只留這個 store。``tamper``／``lose`` 供測試模擬 child 被改或遺失。
"""
from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from uep.agent.loop_context import ForkManifest, MergeBack
from uep.models.port import ChatMessage, messages_from_json, messages_to_json


@dataclass
class ParentEntry:
    key: str
    kind: str  # return｜status
    payload: MergeBack | None = None
    state: str = ""
    reason_code: str | None = None


@dataclass
class InMemoryLoopContextStore:
    children: dict[str, str] = field(default_factory=dict)
    revisions: dict[str, int] = field(default_factory=dict)
    manifests: dict[str, ForkManifest] = field(default_factory=dict)
    parent_entries: dict[str, list[ParentEntry]] = field(default_factory=lambda: defaultdict(list))
    _keys: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def create_child(self, ref: str, messages: Sequence[ChatMessage], manifest: ForkManifest) -> int:
        with self._lock:
            self.children[ref] = messages_to_json(messages)
            self.revisions[ref] = 1
            self.manifests[ref] = manifest
            return 1

    def save_child(self, ref: str, messages: Sequence[ChatMessage]) -> int:
        with self._lock:
            self.children[ref] = messages_to_json(messages)
            self.revisions[ref] = self.revisions.get(ref, 0) + 1
            return self.revisions[ref]

    def load_child(self, ref: str) -> tuple[list[ChatMessage], int] | None:
        with self._lock:
            raw = self.children.get(ref)
            return None if raw is None else (messages_from_json(raw), self.revisions.get(ref, 0))

    def merge_into_parent(self, key: str, parent_id: str, payload: MergeBack) -> bool:
        with self._lock:
            if key in self._keys:
                return False
            self._keys.add(key)
            self.parent_entries[parent_id].append(ParentEntry(key, "return", payload=payload))
            return True

    def mark_parent_status(self, key: str, parent_id: str, state: str, reason_code: str | None) -> bool:
        with self._lock:
            if key in self._keys:
                return False
            self._keys.add(key)
            self.parent_entries[parent_id].append(ParentEntry(key, "status", state=state, reason_code=reason_code))
            return True

    def discard_run(self, run_id: str, parent_id: str | None = None) -> None:
        """Run 終態時清掉該 Run 的 child 與 parent 寫入（production 暫用本類別時不得無限增長）。
        child 的 ref 與冪等鍵都含 run_id；parent 的條目以 key 比對。"""
        with self._lock:
            for ref in [r for r in self.children if run_id in r]:
                self.children.pop(ref, None)
                self.revisions.pop(ref, None)
                self.manifests.pop(ref, None)
            for pid in ([parent_id] if parent_id is not None else list(self.parent_entries)):
                kept = [e for e in self.parent_entries.get(pid, []) if run_id not in e.key]
                if kept:
                    self.parent_entries[pid] = kept
                else:
                    self.parent_entries.pop(pid, None)
            self._keys = {k for k in self._keys if run_id not in k}

    # ---- 測試用 ----
    def lose(self, ref: str) -> None:
        with self._lock:
            self.children.pop(ref, None)

    def tamper(self, ref: str) -> None:
        """改動 child 的一則訊息內容（模擬被改動）。"""
        with self._lock:
            messages = messages_from_json(self.children[ref])
            last = messages[-1]
            messages[-1] = ChatMessage(last.role, last.content + "!", tool_calls=last.tool_calls, tool_call_id=last.tool_call_id)
            self.children[ref] = messages_to_json(messages)
