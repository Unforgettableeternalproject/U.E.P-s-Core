"""
tests/sharing_support.py - M10 共享層測試共用輔助（非測試檔，不被 pytest 蒐集）

全部使用假 llm_fn 與 conftest 的確定性 embedding，不載入真模型。
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from echo_memory.affect.concept_inhibitor import ConceptInhibitor
from echo_memory.affect.evidence_reviewer import EvidenceReviewer
from echo_memory.dream.patch_coordinator import DreamPatchCoordinator
from echo_memory.dream.sharing_phase import SharingPhase
from echo_memory.engine import MemoryEngine
from echo_memory.schemas.evidence import EvidenceInput
from echo_memory.schemas.evidence_review import EvidenceReview, compute_basis_digest
from echo_memory.storage import locking
from echo_memory.storage.locking import storage_lock
from echo_memory.storage.share_layer import ScopeShareLayer
from tests.conftest import _deterministic_embedding

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
VOICE3 = {"channel_kind": "voice", "audience_size": 3}
VOICE6 = {"channel_kind": "voice", "audience_size": 6}
DM = {"channel_kind": "dm", "audience_size": 1}
G42 = "guild:42"
G43 = "guild:43"

SUMMARY = "某成員喜歡爵士樂"


def lock_is_free(path: Path, wait: float = 1.0) -> bool:
    """從另一條執行緒探測 storage_lock 的 thread lock 是否空閒。"""
    lock = locking._thread_lock(Path(path) / ".echo-memory.lock")
    result: dict[str, bool] = {}

    def _probe() -> None:
        got = lock.acquire(timeout=wait)
        result["free"] = got
        if got:
            lock.release()

    t = threading.Thread(target=_probe)
    t.start()
    t.join(timeout=wait + 5)
    return result.get("free", False)


def make_layer(root: Path, **kw: Any) -> ScopeShareLayer:
    return ScopeShareLayer(root, embed_fn=_deterministic_embedding, **kw)


class ShareLLM:
    """假 LLM：依 prompt 種類回傳結構化 JSON（只在測試用 marker 辨識 prompt 種類）。"""

    def __init__(
        self,
        summary: str | Callable[[str], str] = SUMMARY,
        sensitivity: str = "none",
        claim_kind: str = "fact",
        restrict_all: bool = False,
        supports: bool = True,
        corroborate: bool = True,
        probe: Callable[[str], None] | None = None,
        raw: str | None = None,
        corroboration_class: str | None = None,
    ):
        self.summary = summary
        self.sensitivity = sensitivity
        self.claim_kind = claim_kind
        self.restrict_all = restrict_all
        self.supports = supports
        self.corroborate = corroborate
        self.probe = probe
        self.raw = raw
        self.corroboration_class = corroboration_class
        self.calls: list[str] = []

    @property
    def share_calls(self) -> list[str]:
        return [c for c in self.calls if "分享審核員" in c]

    def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        if self.probe is not None:
            self.probe(prompt)
        if self.raw is not None:
            return self.raw
        if "分享審核員" in prompt:
            ids = re.findall(r"^\[([^\]]+)\] ", prompt, re.M)
            summary = self.summary(prompt) if callable(self.summary) else self.summary
            payload = {
                "sensitivity": self.sensitivity,
                "claim_kind": self.claim_kind,
                "cited_unit_ids": ids,
                "supports_summary_from_cited_only": self.supports,
                "shareable_summary": summary,
                "privacy_restrict_unit_ids": ids if self.restrict_all else [],
                "reason": "stub",
            }
            if self.corroboration_class is not None:  # None＝不輸出欄位（模擬舊輸出）
                payload["corroboration_class"] = self.corroboration_class
            return json.dumps(payload, ensure_ascii=False)
        if "交叉佐證審核員" in prompt:
            ids = re.findall(r"^\[(proj_[^\]]+)\] ", prompt, re.M)
            return json.dumps(
                {"corroborating_projection_ids": ids if self.corroborate else [], "reason": "stub"},
            )
        return "{}"


class Owner:
    """一個擁有者 namespace：engine＋真 Gate／ledger／coordinator／reviewer／SharingPhase。"""

    def __init__(
        self,
        root: Path,
        uid: str,
        layer: ScopeShareLayer,
        llm: Callable[[str], str] | None,
    ):
        self.uid = uid
        self.layer = layer
        self.llm = llm
        self.engine = MemoryEngine(storage_path=root, namespace_id=uid)
        self.gate = ConceptInhibitor(
            semantic=self.engine.semantic,
            llm_fn=None,
            evidence_lookup=self.engine.evidence.get,
        )
        self.ledger = self.gate.privacy_ledger
        self.coordinator = DreamPatchCoordinator(inhibitor=self.gate, semantic=self.engine.semantic)
        self.reviewer = EvidenceReviewer(
            llm_fn=llm,
            guard=self.gate,
            semantic=self.engine.semantic,
            provenance=self.engine._provenance_resolver(),
            evidence=self.engine.evidence,
            lock_factory=lambda: storage_lock(self.engine.storage_path),
            privacy_ledger=self.ledger,
        )
        self.phase = SharingPhase(
            owner_uid=uid,
            semantic=self.engine.semantic,
            reviewer=self.reviewer,
            share_layer=layer,
            patch_coordinator=self.coordinator,
            lock_factory=lambda: storage_lock(self.engine.storage_path),
        )

    def phase_without_llm(self) -> SharingPhase:
        reviewer = EvidenceReviewer(
            llm_fn=None, guard=self.gate, semantic=self.engine.semantic,
            provenance=self.engine._provenance_resolver(), evidence=self.engine.evidence,
            lock_factory=lambda: storage_lock(self.engine.storage_path),
            privacy_ledger=self.ledger,
        )
        return SharingPhase(
            owner_uid=self.uid, semantic=self.engine.semantic, reviewer=reviewer,
            share_layer=self.layer, patch_coordinator=self.coordinator,
            lock_factory=lambda: storage_lock(self.engine.storage_path),
        )

    @property
    def path(self) -> Path:
        return self.engine.storage_path

    def say(
        self,
        text: str,
        *,
        scope: str = G42,
        attrs: dict[str, Any] | None = None,
        at: datetime = T0,
    ) -> str:
        return self.engine.store_episode(
            text, "", "s1",
            evidence=[EvidenceInput(
                source_type="user", content=text, speaker_id=self.uid, occurred_at=at,
                scope=scope, attrs=dict(VOICE3 if attrs is None else attrs),
            )],
        )

    def unit_ids(self, ep_id: str) -> list[str]:
        return list(self.engine.episodic.get(ep_id).evidence_ids)

    def concept(self, name: str, eps: list[str], desc: str | None = None) -> str:
        cid, tx = self.gate.process_extractor_patch(
            noun=name, desc=desc or f"{name} 的描述", episode_id=eps[0],
            evidence_kind="distillation", triggered_by="dream",
            source_episode_ids=eps, subject="user", support_source="user",
        )
        assert cid is not None and tx.status == "committed"
        return cid

    def get(self, cid: str):
        return self.engine.semantic.get(cid)

    def review(
        self, cid: str, *, status: str = "insufficient", claim_kind: str = "fact",
        strength: float = 0.95,
    ) -> None:
        concept = self.get(cid)
        ok, tx = self.gate.process_review_patch(cid, EvidenceReview(
            status=status, support_strength=strength, basis_digest=compute_basis_digest(concept),
            reviewer="llm", claim_kind=claim_kind, reason="test",
            cited_evidence_ids=list(self.engine.semantic.get(cid).source_episode_ids[:0]),
        ))
        assert ok, tx.patches[0].rejection_reason if tx.patches else "no patch"

    def sharing_entry(self, cid: str, scope: str = G42) -> dict[str, Any]:
        by_scope = (self.get(cid).sharing or {}).get("by_scope") or {}
        return dict(by_scope.get(scope) or {})

    def live_pid(self, cid: str, scope: str = G42) -> str:
        return str(self.sharing_entry(cid, scope).get("projection_id") or "")
