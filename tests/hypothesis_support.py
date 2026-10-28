"""
tests/hypothesis_support.py - M5-D Hypothesis 測試共用輔助（非測試檔，不被 pytest 蒐集）

全部使用假 llm_fn 與 conftest 的確定性 embedding，不載入真模型。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from echo_memory.affect.concept_inhibitor import ConceptInhibitor
from echo_memory.affect.hypothesis_reviewer import HypothesisReviewer
from echo_memory.dream.hypothesis_phase import HypothesisPhase
from echo_memory.engine import MemoryEngine
from echo_memory.schemas.evidence import EvidenceInput
from echo_memory.schemas.hypothesis import make_pair_key
from echo_memory.storage.locking import storage_lock

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
DM = {"channel_kind": "dm", "audience_size": 1}


@dataclass
class Env:
    engine: MemoryEngine
    gate: ConceptInhibitor
    phase: HypothesisPhase
    reviewer: HypothesisReviewer

    @property
    def store(self):
        return self.gate.hypothesis_store

    @property
    def graph(self):
        return self.engine.episodic.graph

    @property
    def root(self) -> Path:
        return self.engine.storage_path


def build_env(
    tmp_path: Path,
    llm: Callable[[str], str] | None = None,
    namespace_id: str = "ns1",
) -> Env:
    engine = MemoryEngine(storage_path=tmp_path / "data", namespace_id=namespace_id, llm_fn=llm)
    engine.ensure_initialized()
    gate = ConceptInhibitor(semantic=engine.semantic, llm_fn=None)
    engine.set_concept_inhibitor(gate)
    root = engine.storage_path
    lock = lambda: storage_lock(root)  # noqa: E731
    provenance = engine._provenance_resolver()
    phase = HypothesisPhase(
        semantic=engine.semantic, graph=engine.episodic.graph, store=gate.hypothesis_store,
        gate=gate, provenance=provenance, lock_factory=lock,
    )
    reviewer = HypothesisReviewer(
        llm_fn=llm, guard=gate, semantic=engine.semantic, provenance=provenance,
        store=gate.hypothesis_store, lock_factory=lock,
    )
    return Env(engine, gate, phase, reviewer)


def user_ep(
    engine: MemoryEngine, text: str, *, speaker: str = "u1", at: datetime = T0,
    attrs: dict | None = None, salience: float = 0.5,
) -> str:
    return engine.store_episode(
        text, "", "s1", salience=salience,
        evidence=[EvidenceInput(
            source_type="user", content=text, speaker_id=speaker, occurred_at=at,
            attrs=dict(DM if attrs is None else attrs),
        )],
    )


def agent_ep(engine: MemoryEngine, text: str, *, at: datetime = T0) -> str:
    return engine.store_episode(
        "", text, "s1",
        evidence=[EvidenceInput(source_type="agent", content=text, speaker_id="agent", occurred_at=at)],
    )


def unit_ids(engine: MemoryEngine, ep_id: str) -> list[str]:
    return list(engine.episodic.get(ep_id).evidence_ids)


def make_concept(
    env: Env, name: str, eps: list[str], *, support_source: str | None = "user",
) -> str:
    cid, tx = env.gate.process_extractor_patch(
        noun=name, desc=f"{name} 的描述", episode_id=eps[0],
        evidence_kind="distillation", triggered_by="dream",
        source_episode_ids=eps, subject="user", support_source=support_source,
    )
    assert cid is not None and tx.status == "committed"
    env.graph.add_concept_node(cid, name)  # 圖譜節點（正式流程由 Dream 蒸餾建立）
    env.graph.save()
    return cid


def make_bridge(
    env: Env, a: str, b: str, *, confirm: int = 2, similarity: float = 0.9, state: str = "active",
) -> str:
    """直接在圖譜建立 LATENT_BRIDGE（屬性與 GraphPruning 一致）。"""
    key = make_pair_key(a, b)
    now = time.time()
    with env.graph.lock:
        for src, dst in ((a, b), (b, a)):
            env.graph.graph.add_edge(
                src, dst, edge_type="LATENT_BRIDGE", weight=similarity, similarity=similarity,
                bridge_id=key, state=state, degraded_reason="", confirm_count=confirm,
                created_cycle_id="cyc_a", last_confirmed_cycle_id="cyc_b",
                created_at=now, last_confirmed_at=now, endpoint_digests={},
                provenance={"producer": "dream_bridge", "endpoints": [], "source_episode_ids": []},
            )
    env.graph.save()  # 之後 store_episode 會從磁碟重載圖譜，未存檔的邊會消失
    return key


def two_concepts(env: Env, *, same_speaker: bool = True) -> dict[str, Any]:
    """建立兩個概念：A 來自 u1 的 DM；B 來自 u1（或 u2）的 DM。回傳 id 與 unit id。"""
    ep_a = user_ep(env.engine, "我最近很迷咖啡拉花", speaker="u1")
    ep_b = user_ep(env.engine, "我每天早上都去那間咖啡店", speaker="u1" if same_speaker else "u2")
    a = make_concept(env, "咖啡拉花", [ep_a])
    b = make_concept(env, "早晨咖啡店", [ep_b])
    return {
        "a": a, "b": b, "ep_a": ep_a, "ep_b": ep_b,
        "ua": unit_ids(env.engine, ep_a)[0], "ub": unit_ids(env.engine, ep_b)[0],
    }


def propose(env: Env, a: str, b: str) -> str:
    """跑 phase 的 propose，回傳該對的假說 id。"""
    report = env.phase.propose()
    assert report.proposed >= 1, report
    return env.store.get_by_pair(a, b).id


def llm_reply(
    status: str = "supported", cited: list[str] | None = None, claim_kind: str = "fact",
    statement: str | None = "兩者都與咖啡文化有關", reason: str = "證據明確",
) -> str:
    return json.dumps({
        "status": status, "claim_kind": claim_kind,
        "cited_unit_ids": cited or [], "statement": statement, "reason": reason,
    }, ensure_ascii=False)


class ScriptedLLM:
    """依序回覆；回覆是 callable 時呼叫它（可丟例外或在 LLM 期間改動資料）。"""

    def __init__(self, *replies: Any):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return reply() if callable(reply) else reply


def edge_types(env: Env, a: str, b: str) -> set[str]:
    return env.graph.edge_types_between(a, b)


def future(days: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)


def rejection(tx) -> str:
    return tx.patches[0].rejection_reason or ""


def claim(env: Env, hid: str, token: str = "tok", **kw: Any):
    return env.gate.process_hypothesis_patch("claim", {"hypothesis_id": hid, "token": token, **kw})


def supported_env(tmp_path: Path) -> tuple[Env, dict[str, Any], str]:
    """兩位說話者各自陳述 -> 審核為 supported 的完整場景。回傳 (env, ids, hypothesis_id)。"""
    env = build_env(tmp_path)
    d = two_concepts(env, same_speaker=False)
    make_bridge(env, d["a"], d["b"])
    hid = propose(env, d["a"], d["b"])
    env.reviewer._llm_fn = ScriptedLLM(llm_reply("supported", [d["ua"], d["ub"]]))
    out = env.reviewer.review(hid)
    assert out.outcome == "reviewed" and out.status == "supported", out
    return env, d, hid
