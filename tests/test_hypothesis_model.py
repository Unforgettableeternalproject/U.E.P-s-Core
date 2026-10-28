"""
tests/test_hypothesis_model.py - M5-D：Hypothesis 資料模型、存儲與結構約束

涵蓋：schema 容錯；store 原子寫／pair 唯一／損毀 fail-closed／唯讀；
證據資格與獨立佐證純函式；結構測試（除 ConceptInhibitor 外無模組持 writer handle）。
全部使用假 llm_fn，不載入真模型。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from echo_memory.schemas.evidence import EvidenceUnit
from echo_memory.schemas.hypothesis import (
    Hypothesis,
    evaluate_hypothesis_support,
    hypothesis_evidence_kind,
    make_pair_key,
    split_pair_key,
)
from echo_memory.storage.hypothesis import (
    HypothesisStore,
    HypothesisStoreError,
    HypothesisWriter,
)
from tests.hypothesis_support import DM, T0, build_env

REPO = Path(__file__).resolve().parent.parent


# ─── schema ────────────────────────────────────────────────────────────────


def test_pair_key_is_sorted_and_symmetric():
    assert make_pair_key("b", "a") == make_pair_key("a", "b") == "a|b"
    assert split_pair_key("a|b") == ("a", "b")
    assert split_pair_key("a|a") is None and split_pair_key("nope") is None


def test_proposed_stores_no_assertion_text():
    h = Hypothesis(pair_key="a|b")
    assert h.statement is None and h.status == "proposed" and h.id.startswith("hyp_")


def test_roundtrip():
    h = Hypothesis(pair_key="a|b", pair_digest="d", claim_kind="state", attempts=2)
    h.origin.bridge_id, h.origin.confirm_count = "a|b", 3
    h.statement = "有關"
    back = Hypothesis.from_dict(h.to_dict())
    assert back.to_dict() == h.to_dict()


@pytest.mark.parametrize("bad", [None, 3, "x", [], {}, {"pair_key": "only-one"}, {"pair_key": "a|a"}])
def test_from_dict_unusable_returns_none(bad):
    assert Hypothesis.from_dict(bad) is None


def test_from_dict_tolerates_bad_values():
    h = Hypothesis.from_dict({
        "pair_key": "a|b", "status": "weird", "attempts": "x", "version": -4,
        "claim_kind": "??", "review": "not-a-dict", "origin": 7, "outcome": [],
        "next_review_at": "garbage", "created_at": "garbage", "unknown_field": 1,
        "statement": 123,
    })
    assert h is not None
    # 壞狀態不得被當成有效主張：視為 expired
    assert h.status == "expired" and h.outcome.expire_reason == "corrupt"
    assert h.attempts == 0 and h.version == 1 and h.claim_kind == "fact"
    assert h.review.status == "not_checked" and h.statement is None
    assert h.next_review_at is None and h.created_at.tzinfo is not None


# ─── store ─────────────────────────────────────────────────────────────────


def _writer(tmp_path) -> HypothesisWriter:
    return HypothesisWriter(HypothesisStore(tmp_path))


def test_store_atomic_write_and_reload(tmp_path):
    w = _writer(tmp_path)
    h = w.put(Hypothesis(pair_key="a|b", pair_digest="d1"))
    assert h.version == 1
    # 另一個唯讀實例看得到
    other = HypothesisStore(tmp_path)
    assert other.get(h.id).pair_digest == "d1"
    assert (tmp_path / "hypotheses.json").exists()
    assert not list(tmp_path.glob("*.tmp"))
    raw = json.loads((tmp_path / "hypotheses.json").read_text(encoding="utf-8"))
    assert raw["records"][0]["id"] == h.id


def test_store_version_is_monotonic_and_checked(tmp_path):
    w = _writer(tmp_path)
    h = w.put(Hypothesis(pair_key="a|b"))
    h2 = w.store.get(h.id)
    w.put(h2, expected_version=1)
    assert w.store.get(h.id).version == 2
    with pytest.raises(HypothesisStoreError):
        w.put(h2, expected_version=1)  # 已被改過


def test_store_pair_key_unique(tmp_path):
    w = _writer(tmp_path)
    w.put(Hypothesis(pair_key="a|b"))
    with pytest.raises(HypothesisStoreError):
        w.put(Hypothesis(pair_key="a|b"))
    assert w.store.count() == 1


def test_store_returns_copies(tmp_path):
    w = _writer(tmp_path)
    h = w.put(Hypothesis(pair_key="a|b"))
    got = w.store.get(h.id)
    got.status = "supported"
    got.statement = "改了"
    assert w.store.get(h.id).status == "proposed"


def test_store_corrupt_file_is_fail_closed(tmp_path):
    path = tmp_path / "hypotheses.json"
    path.write_text("{ not json", encoding="utf-8")
    store = HypothesisStore(tmp_path)
    assert store.list() == [] and store.is_corrupt
    with pytest.raises(HypothesisStoreError):
        HypothesisWriter(store).put(Hypothesis(pair_key="a|b"))
    assert path.read_text(encoding="utf-8") == "{ not json"  # 原檔不被覆蓋


def test_store_skips_bad_and_duplicate_pair_rows(tmp_path):
    good = Hypothesis(pair_key="a|b").to_dict()
    dup = Hypothesis(pair_key="a|b").to_dict()
    (tmp_path / "hypotheses.json").write_text(
        json.dumps({"version": 1, "records": [good, dup, {"pair_key": "bad"}, 5]}), encoding="utf-8",
    )
    store = HypothesisStore(tmp_path)
    assert store.count() == 1


def test_store_blocked_digest_rules(tmp_path):
    w = _writer(tmp_path)
    h = Hypothesis(pair_key="a|b", pair_digest="d1", status="refuted")
    w.put(h)
    s = w.store
    assert s.is_blocked("a", "b", "d1") and s.is_pair_refuted("b", "a")
    assert not s.is_blocked("a", "b", "d2")  # 內容變了可再提案
    h2 = s.get(h.id)
    h2.status = "expired"
    h2.outcome.expire_reason = "insufficient_exhausted"
    w.put(h2)
    assert s.is_blocked("a", "b", "d1") and not s.is_pair_refuted("a", "b")
    h3 = s.get(h.id)
    h3.outcome.expire_reason = "ttl"
    w.put(h3)
    assert not s.is_blocked("a", "b", "d1")  # ttl 過期可重新提案


def test_store_has_no_write_methods():
    public = {n for n in dir(HypothesisStore) if not n.startswith("_")}
    assert not public & {"put", "remove", "restore", "insert", "save", "write", "delete", "update"}


# ─── 證據資格與獨立佐證 ───────────────────────────────────────────────────────


def _unit(uid, source_type="user", speaker="u1", episode="ep1", status="known", **kw) -> EvidenceUnit:
    kw.setdefault("attrs", dict(DM))
    return EvidenceUnit(
        id=uid, episode_id=episode, source_type=source_type, content="x", occurred_at=T0,
        speaker_id=speaker, provenance_status=status, **kw,
    )


def test_evidence_kind_only_user_and_observation():
    assert hypothesis_evidence_kind(_unit("u")) == "user"
    assert hypothesis_evidence_kind(_unit("o", "observation", None, None)) == "observation"
    for st in ("agent", "reflection", "dream", "tool"):
        assert hypothesis_evidence_kind(_unit("x", st)) is None
    assert hypothesis_evidence_kind(_unit("l", status="legacy_unknown")) is None
    redacted = _unit("r", tombstoned_at=datetime.now(timezone.utc))
    assert hypothesis_evidence_kind(redacted) is None


def test_support_same_speaker_is_subjective_only():
    sup = evaluate_hypothesis_support([_unit("a", episode="e1"), _unit("b", episode="e2")])
    assert sup.strength >= 0.9 and not sup.independent and sup.status == "insufficient"
    assert "subjective_only_no_independent_corroboration" in sup.notes


def test_support_other_speaker_or_observation_is_independent():
    two = evaluate_hypothesis_support(
        [_unit("a", speaker="u1", episode="e1"), _unit("b", speaker="u2", episode="e2")],
    )
    assert two.independent and two.status == "supported"
    obs = evaluate_hypothesis_support(
        [_unit("a"), _unit("o", "observation", None, None)],
    )
    assert obs.independent and obs.status == "supported" and "o" in obs.independent_ids


def test_support_unknown_speaker_is_not_independent():
    sup = evaluate_hypothesis_support(
        [_unit("a", speaker="u1", episode="e1"), _unit("b", speaker=None, episode="e2")],
    )
    assert not sup.independent and sup.status == "insufficient"


def test_support_observation_alone_is_not_enough_and_agent_ignored():
    obs_only = evaluate_hypothesis_support([_unit("o", "observation", None, None)])
    assert obs_only.status == "insufficient"
    agent = evaluate_hypothesis_support([_unit("g", "agent", "agent")])
    assert agent.status == "insufficient" and "no_eligible_citation" in agent.notes


def test_support_threshold_and_intention_reuse_m4_rules():
    # 主張者自述不計入佐證強度；他人佐證權重不足（w=0.08）-> insufficient
    claim = _unit("a", speaker="u1", episode="e1")
    weak = _unit("b", speaker="u2", episode="e2",
                 attrs={"channel_kind": "voice", "audience_size": 6})
    sup = evaluate_hypothesis_support([claim, weak], claim_kind="fact")
    assert sup.strength < 0.9 and sup.status == "insufficient"


# ─── 結構約束：寫入口只在 ConceptInhibitor（Gate）───────────────────────────────


def _py_files():
    for sub in ("echo_memory", "mcp_server", "llm_integration"):
        yield from (REPO / sub).rglob("*.py")


def test_only_gate_modules_reference_writer():
    allowed = {
        "echo_memory/storage/hypothesis.py",  # 定義
        "echo_memory/affect/hypothesis_gate.py",  # ConceptInhibitor 的 mixin，唯一建構處
    }
    offenders = []
    for path in _py_files():
        rel = path.relative_to(REPO).as_posix()
        if rel in allowed:
            continue
        if "HypothesisWriter" in path.read_text(encoding="utf-8"):
            offenders.append(rel)
    assert offenders == []


def _walk(obj, seen, depth=0):
    """走訪物件圖；遇到 ConceptInhibitor（Gate）就停，不穿過它去找它自己的 writer。"""
    from echo_memory.affect.concept_inhibitor import ConceptInhibitor

    if id(obj) in seen or depth > 6:
        return
    seen.add(id(obj))
    yield obj
    if isinstance(obj, ConceptInhibitor):
        return
    values = []
    if isinstance(obj, dict):
        values = list(obj.values())
    elif isinstance(obj, (list, tuple, set)):
        values = list(obj)
    elif hasattr(obj, "__dict__") and type(obj).__module__.startswith(("echo_memory", "tests")):
        values = list(vars(obj).values())
    for v in values:
        yield from _walk(v, seen, depth + 1)


def test_no_object_other_than_gate_holds_a_writer(tmp_path):
    env = build_env(tmp_path, llm=lambda p: "{}")
    env.gate.process_hypothesis_patch("claim", {"hypothesis_id": "none"})  # 觸發 writer 惰性建構
    gate_writer = env.gate._hypothesis_writer()
    assert isinstance(gate_writer, HypothesisWriter)

    dream = env.engine._ensure_dream_engine()
    holders = {
        "phase": env.phase, "reviewer": env.reviewer, "store": env.store,
        "dream_engine": dream, "pruning": dream._pruning,
        "coordinator": dream._patch_coordinator, "engine": env.engine,
    }
    for name, root in holders.items():
        seen: set[int] = set()
        writers = [o for o in _walk(root, seen) if isinstance(o, HypothesisWriter)]
        assert writers == [], f"{name} 持有 HypothesisWriter"
    for name in ("phase", "reviewer"):
        direct = [v for v in vars(holders[name]).values() if isinstance(v, HypothesisWriter)]
        assert direct == []
    assert not any(isinstance(v, HypothesisWriter) for v in vars(dream).values())
    assert not any(isinstance(v, HypothesisWriter) for v in vars(dream._pruning).values())
    assert not hasattr(env.store, "put")


def test_dream_wires_refuted_suppression_to_readonly_store(tmp_path):
    env = build_env(tmp_path)
    dream = env.engine._ensure_dream_engine()
    assert dream._pruning._bridge_suppressed is not None
    assert dream._hypothesis_store is not None
    assert dream._pruning._is_suppressed("x", "y") is False


def test_config_defaults():
    from echo_memory.config import get_config

    cfg = get_config()
    assert cfg.hypothesis_enabled is True
    assert cfg.hypothesis_min_confirm_count == 2
    assert cfg.hypothesis_max_new_per_cycle == 3 and cfg.hypothesis_max_open == 20
    assert cfg.hypothesis_proposed_ttl_days == 30 and cfg.hypothesis_refuted_retention_days == 180
    assert cfg.hypothesis_max_attempts == 3
