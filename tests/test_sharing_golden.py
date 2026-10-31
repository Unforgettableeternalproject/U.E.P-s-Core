"""
tests/test_sharing_golden.py - M10 跨使用者分享 Golden 驗收

包 B 範圍：S3 跨場域不外洩、S5 退出後消失、S6 forget／modify 連動、S7 交叉驗證、
S9 fail-closed（共享層與 Dream phase 部分）。
其餘情境（S1、S2、S4、S8、S10、S11）由包 A／C 在同檔補上。

全部使用假 llm_fn 與確定性 embedding，不載入真模型。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from echo_memory.schemas.evidence_review import EvidenceReview
from echo_memory.storage.share_layer import REVOCATIONS_FILENAME, ScopeShareLayer
from tests.conftest import _deterministic_embedding
from tests.sharing_support import (
    DM,
    G42,
    G43,
    SUMMARY,
    VOICE6,
    Owner,
    ShareLLM,
    lock_is_free,
    make_layer,
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def layer(root: Path) -> ScopeShareLayer:
    return make_layer(root)


def vec(text: str):
    return _deterministic_embedding(text)


def sp_validator(layer: ScopeShareLayer):
    return lambda ref: layer.validate_sp_ref(str(ref))


AUD2 = {"channel_kind": "voice", "audience_size": 2}  # 學習權重 0.6


def concept_with_weight(owner: Owner, name: str, text: str, n: int, attrs: dict) -> str:
    """n 個獨立 episode、各自 attrs 的權重 -> 投影的 support_strength 由 U_S 重算（noisy-OR）。"""
    return owner.concept(name, [owner.say(f"{text}{i}", attrs=attrs) for i in range(n)])


def intention_pair(root: Path, layer: ScopeShareLayer, bob_strength: float = 0.95):
    """A 的 intention 概念（insufficient）已投影；B 說了同一件事、投影帶 U_S 重算的 support_strength。

    bob_strength：0.95＝6 個 n=3 episode（1-0.6^6）；0.6＝1 個 n=2；0.08＝1 個五人以上。
    """
    summary = "某成員打算明天去買票"
    alice = Owner(root, "1001", layer, ShareLLM(summary=summary, claim_kind="intention"))
    cid_a = alice.concept("明天買票", [alice.say("我打算明天去買票")])
    alice.review(cid_a, status="insufficient", claim_kind="intention", strength=0.5)
    assert alice.phase.run().projected == 1

    bob = Owner(root, "1002", layer, ShareLLM(summary=summary, claim_kind="fact"))
    n, attrs = {0.95: (6, None), 0.6: (1, AUD2), 0.08: (1, VOICE6)}[bob_strength]
    cid_b = concept_with_weight(bob, "買票", "我也要去買票", n, attrs or {"channel_kind": "voice", "audience_size": 3})
    assert bob.phase.run().projected == 1
    return alice, bob, cid_a, cid_b


# ─── S3 跨場域不外洩 ──────────────────────────────────────────────────────────


class TestS3CrossScope:
    def test_same_concept_judged_per_scope_and_never_crosses(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM(
            summary=lambda p: "摘要四十三" if "G43文字" in p else "摘要四十二",
        ))
        ep42 = owner.say("G42文字 我喜歡爵士樂", scope=G42)
        ep43 = owner.say("G43文字 我喜歡爵士樂", scope=G43)
        cid = owner.concept("喜歡爵士樂", [ep42, ep43])
        rep = owner.phase.run()
        assert rep.judged == 2 and rep.projected == 2

        # 各自判定：prompt 只含該場域的來源
        p42 = next(c for c in owner.llm.share_calls if "G42文字" in c)
        p43 = next(c for c in owner.llm.share_calls if "G43文字" in c)
        assert "G43文字" not in p42 and "G42文字" not in p43

        # 各場域只看到自己的摘要；共享 store 物理上分開
        assert [v.summary for v in layer.list_projections(G42)] == ["摘要四十二"]
        assert [v.summary for v in layer.list_projections(G43)] == ["摘要四十三"]
        assert layer.search_projections(G42, vec("摘要四十三"), min_similarity=0.3) == []
        assert layer.search_projections(G43, vec("摘要四十二"), min_similarity=0.3) == []
        assert owner.sharing_entry(cid, G42)["projection_id"] != owner.sharing_entry(cid, G43)["projection_id"]

    def test_scope_without_projection_returns_empty(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM())
        owner.concept("喜歡爵士樂", [owner.say("我喜歡爵士樂", scope=G42)])
        owner.phase.run()
        assert layer.list_projections("guild:2") == []
        assert layer.search_projections("guild:2", vec(SUMMARY), min_similarity=0.0) == []
        assert layer.list_projections(G43) == []
        assert not (root / "namespaces" / "scope-g2").exists()

    def test_revoking_in_one_scope_leaves_other_untouched(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM())
        cid = owner.concept("喜歡爵士樂", [
            owner.say("我喜歡爵士樂", scope=G42), owner.say("我喜歡爵士樂", scope=G43),
        ])
        owner.phase.run()
        layer.revoke_owner(G42, "1001")
        assert layer.list_projections(G42) == []
        assert layer.is_live(G43, owner.live_pid(cid, G43))


# ─── S5 退出後消失 ────────────────────────────────────────────────────────────


class TestS5MemberLeaves:
    def test_revoke_owner_hides_all_projections(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM())
        c1 = owner.concept("喜歡爵士樂", [owner.say("我喜歡爵士樂")])
        c2 = owner.concept("養貓", [owner.say("我養了一隻貓")])
        owner.phase.run()
        pids = [owner.live_pid(c1), owner.live_pid(c2)]
        assert all(pids) and all(layer.is_live(G42, p) for p in pids)
        layer.revoke_owner(G42, "1001")
        assert not any(layer.is_live(G42, p) for p in pids)
        assert layer.search_projections(G42, vec(SUMMARY), min_similarity=0.0) == []

    def test_corroboration_invalid_after_member_leaves(self, root, layer):
        alice, bob, cid_a, _ = intention_pair(root, layer)
        assert alice.phase.run().corroborated == 1
        concept = alice.get(cid_a)
        assert concept.effective_review_status(sp_validator(layer)) == "supported"

        layer.revoke_owner(G42, "1002")  # B 退出場域
        # 讀取端立即回落（不需通知 A）
        assert concept.effective_review_status(sp_validator(layer)) == "insufficient"
        # Dream 對帳後持久化回落
        rep = alice.phase.reconcile()
        assert rep.corroborations_invalidated == 1
        review = EvidenceReview.from_dict(alice.get(cid_a).evidence_review)
        assert review.status == "insufficient" and review.corroborated_by == []

    def test_returning_member_reprojects_only_after_rejudge(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM())
        cid = owner.concept("喜歡爵士樂", [owner.say("我喜歡爵士樂")])
        owner.phase.run()
        old = owner.live_pid(cid)
        layer.revoke_owner(G42, "1001")
        owner.phase.reconcile()
        layer.restore_owner(G42, "1001")
        assert not layer.is_live(G42, old)
        assert owner.phase.run().projected == 1
        assert layer.is_live(G42, owner.live_pid(cid))

    def test_pinned_restriction_not_relaxed_by_returning(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM())
        ep = owner.say("我喜歡爵士樂")
        cid = owner.concept("喜歡爵士樂", [ep])
        owner.phase.run()
        owner.gate.restrict_units(owner.unit_ids(ep), reason_code="user")
        owner.phase.reconcile()
        layer.revoke_owner(G42, "1001")
        layer.restore_owner(G42, "1001")
        owner.engine.semantic.get(cid).add_source_episode(owner.say("再說一次喜歡爵士樂"))
        owner.phase.run()
        assert owner.sharing_entry(cid)["pinned"] is True
        assert layer.list_projections(G42) == []


# ─── S6 forget／modify 連動 ───────────────────────────────────────────────────


class TestS6ForgetModify:
    def test_deleting_source_episode_revokes_projection(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM())
        ep = owner.say("我喜歡爵士樂")
        cid = owner.concept("喜歡爵士樂", [ep])
        owner.phase.run()
        pid = owner.live_pid(cid)
        assert layer.is_live(G42, pid)
        owner.engine.delete_episode(ep)
        owner.phase.reconcile()
        assert not layer.is_live(G42, pid)
        assert owner.live_pid(cid) == ""
        # 重判：來源已不可分享 -> 不會自己回來
        assert owner.phase.run().projected == 0
        assert layer.list_projections(G42) == []

    def test_modify_revokes_then_rejudge_may_reproject(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM())
        cid = owner.concept("喜歡爵士樂", [owner.say("我喜歡爵士樂")])
        owner.phase.run()
        old = owner.live_pid(cid)
        owner.engine.semantic.get(cid).description = "喜歡爵士樂（改寫後的描述）"
        owner.phase.reconcile()
        assert not layer.is_live(G42, old)  # 先撤回
        assert layer.list_projections(G42) == []
        assert owner.phase.run().projected == 1  # 重判後才重新出現
        assert layer.is_live(G42, owner.live_pid(cid))
        assert owner.live_pid(cid) != old

    def test_expanded_sources_trigger_rejudge(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM())
        cid = owner.concept("喜歡爵士樂", [owner.say("我喜歡爵士樂")])
        owner.phase.run()
        old = owner.live_pid(cid)
        ep2 = owner.say("我常聽爵士樂")
        owner.engine.semantic.get(cid).add_source_episode(ep2)
        owner.phase.reconcile()
        assert not layer.is_live(G42, old)

    def test_cascade_event_reaches_consumer_when_owner_forgets(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM())
        ep = owner.say("我喜歡爵士樂")
        cid = owner.concept("喜歡爵士樂", [ep])
        owner.phase.run()
        pid = owner.live_pid(cid)
        layer.record_consumption(
            G42, [pid], consumer_uid="1002", episode_id="ep_b", agent_unit_ids=["evu_b_agent"],
        )
        events = []
        layer.set_cascade_hook(lambda e: events.append(e) or True)
        owner.engine.delete_episode(ep)
        owner.phase.reconcile()
        assert [(e.consumer_uid, e.agent_unit_ids) for e in events] == [("1002", ("evu_b_agent",))]


# ─── S7 交叉驗證 ──────────────────────────────────────────────────────────────


class TestS7CrossValidation:
    def test_peer_projection_corroborates_intention(self, root, layer):
        alice, bob, cid_a, _ = intention_pair(root, layer)
        assert EvidenceReview.from_dict(alice.get(cid_a).evidence_review).status == "insufficient"
        rep = alice.phase.run()
        assert rep.corroborated == 1
        review = EvidenceReview.from_dict(alice.get(cid_a).evidence_review)
        assert review.status == "supported"
        assert len(review.corroborated_by) == 1 and review.corroborated_by[0].startswith("sp:guild:42:")
        # 只存參考，不複製 B 的文字
        assert "我也要去買票" not in str(alice.get(cid_a).evidence_review)

    def test_corroboration_falls_back_when_peer_revokes(self, root, layer):
        alice, bob, cid_a, cid_b = intention_pair(root, layer)
        alice.phase.run()
        layer.revoke(G42, [bob.live_pid(cid_b)], reason_code="user_restrict")
        assert alice.get(cid_a).effective_review_status(sp_validator(layer)) == "insufficient"
        assert alice.phase.reconcile().corroborations_invalidated == 1
        assert EvidenceReview.from_dict(alice.get(cid_a).evidence_review).status == "insufficient"

    def test_peer_digest_change_invalidates(self, root, layer):
        alice, bob, cid_a, cid_b = intention_pair(root, layer)
        alice.phase.run()
        layer.project(  # B 的概念被改過，重新投影（舊 digest 的投影被取代）
            G42, owner_uid="1002", source_concept_id=cid_b, source_digest="d" * 64,
            summary="某成員打算明天去買票", embedding=vec("某成員打算明天去買票"),
        )
        assert alice.get(cid_a).effective_review_status(sp_validator(layer)) == "insufficient"

    def test_large_audience_peer_too_weak(self, root, layer):
        """B 在五人以上場合說的話權重低（support_strength 小），單獨無法佐證。"""
        alice, bob, cid_a, _ = intention_pair(root, layer, bob_strength=0.08)
        rep = alice.phase.run()
        assert rep.corroborated == 0
        assert EvidenceReview.from_dict(alice.get(cid_a).evidence_review).status == "insufficient"

    def test_same_owner_does_not_add_up(self, root, layer):
        alice, bob, cid_a, cid_b = intention_pair(root, layer, bob_strength=0.6)
        concept_with_weight(bob, "買票二", "我還是會去買票", 1, AUD2)
        bob.phase.run()
        assert alice.phase.run().corroborated == 0  # 同一人多筆只取最大，noisy-OR 不加分

    def test_two_independent_peers_combine(self, root, layer):
        alice, bob, cid_a, _ = intention_pair(root, layer, bob_strength=0.6)
        carol = Owner(root, "1003", layer, ShareLLM(summary="某成員打算明天去買票"))
        concept_with_weight(carol, "買票三", "我明天也去買票", 2, AUD2)  # 1-0.4^2=0.84
        assert carol.phase.run().projected == 1
        assert alice.phase.run().corroborated == 1  # 1-(1-0.6)(1-0.84)=0.936 >= 0.9

    def test_circular_injection_not_independent(self, root, layer):
        """B 的 episode 曾被注入 A 的該投影 -> B 對同一主張的陳述不算獨立佐證。"""
        summary = "某成員打算明天去買票"
        alice = Owner(root, "1001", layer, ShareLLM(summary=summary, claim_kind="intention"))
        cid_a = alice.concept("明天買票", [alice.say("我打算明天去買票")])
        alice.review(cid_a, status="insufficient", claim_kind="intention", strength=0.5)
        alice.phase.run()
        pid_a = alice.live_pid(cid_a)
        layer.record_consumption(G42, [pid_a], consumer_uid="1002", episode_id="ep_b")

        bob = Owner(root, "1002", layer, ShareLLM(summary=summary))
        concept_with_weight(bob, "買票", "聽說你要去買票", 6, {"channel_kind": "voice", "audience_size": 3})
        assert bob.phase.run().projected == 1
        assert alice.phase.run().corroborated == 0
        assert EvidenceReview.from_dict(alice.get(cid_a).evidence_review).status == "insufficient"

    def test_corroboration_llm_runs_outside_locks(self, root, layer):
        alice, bob, cid_a, _ = intention_pair(root, layer)
        seen: list[tuple[bool, bool]] = []
        scope_path = root / "namespaces" / "scope-g42"
        real = alice.llm

        def probe(prompt):
            seen.append((lock_is_free(alice.path), lock_is_free(scope_path)))

        real.probe = probe
        alice.phase.run()
        assert seen and all(a and b for a, b in seen)

    def test_non_intention_concept_never_corroborated(self, root, layer):
        alice = Owner(root, "1001", layer, ShareLLM(claim_kind="preference"))
        cid = alice.concept("喜歡爵士樂", [alice.say("我喜歡爵士樂")])
        alice.review(cid, status="insufficient", claim_kind="preference", strength=0.5)
        alice.phase.run()
        bob = Owner(root, "1002", layer, ShareLLM())
        concept_with_weight(bob, "爵士樂", "我也喜歡爵士樂", 6, {"channel_kind": "voice", "audience_size": 3})
        bob.phase.run()
        assert alice.phase.run().corroborated == 0


# ─── S9 fail-closed ───────────────────────────────────────────────────────────


class TestS9FailClosed:
    def test_no_llm_nothing_shared(self, root, layer):
        owner = Owner(root, "1001", layer, None)
        cid = owner.concept("喜歡爵士樂", [owner.say("我喜歡爵士樂")])
        rep = owner.phase.run()
        assert rep.projected == 0 and layer.list_projections(G42) == []
        assert owner.sharing_entry(cid) == {}

    def test_llm_exception_nothing_shared(self, root, layer):
        def boom(_prompt):
            raise RuntimeError("llm down")

        owner = Owner(root, "1001", layer, boom)
        cid = owner.concept("喜歡爵士樂", [owner.say("我喜歡爵士樂")])
        rep = owner.phase.run()
        assert rep.projected == 0 and rep.held == 1
        assert layer.list_projections(G42) == [] and owner.sharing_entry(cid) == {}

    @pytest.mark.parametrize("raw", [
        "not json at all",
        '{"sensitivity": "unknown"}',
        '{"sensitivity": "none", "cited_unit_ids": "oops"}',
    ])
    def test_invalid_llm_output_nothing_shared(self, root, layer, raw):
        owner = Owner(root, "1001", layer, ShareLLM(raw=raw))
        owner.concept("喜歡爵士樂", [owner.say("我喜歡爵士樂")])
        rep = owner.phase.run()
        assert rep.projected == 0 and rep.held == 1
        assert layer.list_projections(G42) == []

    def test_unknown_cited_id_dropped_then_nothing_shared(self, root, layer):
        # 未知 cited id 丟棄（不 hold）；清空後走 no_cited_units，fail-closed 不分享
        raw = ('{"sensitivity": "none", "cited_unit_ids": ["evu_ghost"], '
               '"supports_summary_from_cited_only": true, "shareable_summary": "x"}')
        owner = Owner(root, "1001", layer, ShareLLM(raw=raw))
        owner.concept("喜歡爵士樂", [owner.say("我喜歡爵士樂")])
        rep = owner.phase.run()
        assert rep.projected == 0 and rep.held == 0
        assert layer.list_projections(G42) == []

    def test_summary_over_limit_nothing_shared(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM(summary="字" * 121))
        owner.concept("喜歡爵士樂", [owner.say("我喜歡爵士樂")])
        rep = owner.phase.run()
        assert rep.projected == 0 and rep.held == 1

    def test_missing_scope_nothing_shared(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM())
        owner.concept("喜歡爵士樂", [owner.say("我喜歡爵士樂", scope="namespace")])
        owner.phase.run()
        assert owner.llm.calls == [] and layer.list_projections(G42) == []

    @pytest.mark.parametrize("attrs", [DM, {}, {"channel_kind": "voice"}, {"audience_size": 4}])
    def test_missing_or_private_context_nothing_shared(self, root, layer, attrs):
        owner = Owner(root, "1001", layer, ShareLLM())
        owner.concept("喜歡爵士樂", [owner.say("我喜歡爵士樂", attrs=attrs)])
        owner.phase.run()
        assert owner.llm.calls == [] and layer.list_projections(G42) == []

    def test_large_audience_is_shareable(self, root, layer):
        """裁決：>=2 人（非 dm）即可全場域分享；人數多不影響資格。"""
        owner = Owner(root, "1001", layer, ShareLLM())
        owner.concept("喜歡爵士樂", [owner.say("我喜歡爵士樂", attrs=VOICE6)])
        assert owner.phase.run().projected == 1

    def test_visibility_function_error_nothing_shared(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM())

        def boom(unit, ledger):
            raise RuntimeError("bad")

        owner.reviewer._visibility_fn = boom
        owner.concept("喜歡爵士樂", [owner.say("我喜歡爵士樂")])
        owner.phase.run()
        assert owner.llm.calls == [] and layer.list_projections(G42) == []

    def test_corrupt_index_hides_everything_and_blocks_projection(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM())
        owner.concept("喜歡爵士樂", [owner.say("我喜歡爵士樂")])
        owner.phase.run()
        assert len(layer.list_projections(G42)) == 1
        (root / "namespaces" / "scope-g42" / REVOCATIONS_FILENAME).write_text(
            "garbage\n", encoding="utf-8",
        )
        assert layer.list_projections(G42) == []
        assert layer.search_projections(G42, vec(SUMMARY), min_similarity=0.0) == []
        owner2 = Owner(root, "1002", layer, ShareLLM())
        owner2.concept("養貓", [owner2.say("我養了一隻貓")])
        assert owner2.phase.run().projected == 0
        assert layer.list_projections(G42) == []

    def test_lost_index_hides_everything(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM())
        owner.concept("喜歡爵士樂", [owner.say("我喜歡爵士樂")])
        owner.phase.run()
        (root / "namespaces" / "scope-g42" / REVOCATIONS_FILENAME).unlink()
        assert layer.list_projections(G42) == []
        assert layer.search_projections(G42, vec(SUMMARY), min_similarity=0.0) == []

    def test_sensitive_content_not_shared(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM(sensitivity="sensitive"))
        owner.concept("健康狀況", [owner.say("我最近在看醫生")])
        assert owner.phase.run().projected == 0
        assert layer.list_projections(G42) == []

    def test_gate_without_share_support_nothing_shared(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM())
        owner.concept("喜歡爵士樂", [owner.say("我喜歡爵士樂")])
        owner.coordinator._inhibitor = object()  # 沒有 process_share_patch 的 Gate
        rep = owner.phase.run()
        assert rep.projected == 0 and rep.skip_reason == "gate_without_share"
        assert layer.list_projections(G42) == []

    def test_sharing_disabled_nothing_shared(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM())
        owner.concept("喜歡爵士樂", [owner.say("我喜歡爵士樂")])

        class Off:
            sharing_enabled = False

        owner.phase._cfg = Off()
        assert owner.phase.run().skip_reason == "sharing_disabled"
        assert layer.list_projections(G42) == []

    def test_dream_engine_without_share_layer_never_shares(self, root, layer):
        """DreamEngine 沒有注入共享層（預設）-> 沒有分享 phase，行為與 M4 相同。"""
        from echo_memory.dream.engine import DreamEngine

        owner = Owner(root, "1001", layer, ShareLLM())
        owner.concept("喜歡爵士樂", [owner.say("我喜歡爵士樂")])
        dream = DreamEngine(
            graph=owner.engine.episodic.graph, episodic=owner.engine.episodic,
            semantic=owner.engine.semantic, llm_fn=owner.llm,
            concept_inhibitor=owner.gate, evidence_store=owner.engine.evidence,
        )
        assert dream._sharing_phase is None
        assert layer.list_projections(G42) == []


# ━━━ 包 C 範圍：S2／S4／S8／S10／S11 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# （各自獨立的 class，imports 放在 class 內，避免與其他包同時改到同一段）

import json  # noqa: E402

C_GUILD = "guild:42"
C_VERBATIM = "我超愛爵士樂，尤其是週五晚上和我老婆小芳在陽台聽唱片"


def _c_scope_engine(root, layer):
    from echo_memory.engine import MemoryEngine

    engine = MemoryEngine(storage_path=root, namespace_id="scope-g42")
    engine.attach_share_layer(layer)
    return engine


def _c_owner(root, layer, uid="1001", summary=SUMMARY):
    owner = Owner(root, uid, layer, ShareLLM(summary=summary))
    owner.engine.attach_share_layer(layer)
    owner.engine._concept_inhibitor = owner.gate
    return owner


def _c_share(owner, text=C_VERBATIM, name="喜歡爵士樂"):
    ep = owner.say(text)
    cid = owner.concept(name, [ep])
    assert owner.phase.run().projected == 1
    return ep, cid, owner.live_pid(cid)


class TestS2VerbatimNeverLeaks:
    """B 的 prompt 區塊只含 shareable_summary；共享 store 與回傳物件不含 A 的 evidence 原文。"""

    def test_prompt_block_only_has_summary(self, root, layer):
        scope = _c_scope_engine(root, layer)
        alice = _c_owner(root, layer)
        _c_share(alice)

        shared = scope.retrieve_shared(SUMMARY, scope_id=C_GUILD, viewer_uid="1002")
        assert shared.items and shared.items[0].summary == SUMMARY
        text = shared.to_text(lambda uid: "小愛")
        assert SUMMARY in text
        for fragment in ("小芳", "老婆", "陽台", "唱片", "週五"):
            assert fragment not in text
        assert C_VERBATIM not in repr(shared)

    def test_scope_store_has_no_verbatim(self, root, layer):
        scope = _c_scope_engine(root, layer)
        alice = _c_owner(root, layer)
        _c_share(alice)
        assert scope.retrieve_shared(SUMMARY, scope_id=C_GUILD, viewer_uid="1002").items
        # 場域 namespace 的所有檔案（含索引、audit、concepts.json）都不含原文片段
        blob = "".join(
            p.read_text(encoding="utf-8", errors="ignore")
            for p in (root / "namespaces" / "scope-g42").rglob("*") if p.is_file()
        )
        assert blob and "小芳" not in blob and "陽台" not in blob and C_VERBATIM not in blob
        assert scope.episodic.count() == 0 and scope.evidence.count() == 0


class TestS4OverrideTakesEffectImmediately:
    """偵測到收緊意圖後，下一次 retrieve_shared 立即回空（撤回索引），即使本體尚未刪除。"""

    def test_scan_intent_then_next_retrieve_is_empty_even_if_body_remains(
        self, root, layer, monkeypatch,
    ):
        scope = _c_scope_engine(root, layer)
        alice = _c_owner(root, layer)
        ep, cid, pid = _c_share(alice)
        unit_id = alice.unit_ids(ep)[0]
        assert scope.retrieve_shared(SUMMARY, scope_id=C_GUILD, viewer_uid="1002").items

        # 讓第二步（刪本體）失敗：只靠撤回索引也必須立即消失
        sem = layer._semantic("scope-g42")
        monkeypatch.setattr(sem, "remove_concept", lambda name: (_ for _ in ()).throw(OSError("disk")))
        alice.engine.set_llm_fn(
            lambda prompt: json.dumps({"privacy_restrict_unit_ids": [unit_id]}),
        )

        out = alice.engine.scan_privacy_intent([unit_id])
        assert out["restricted"] == [unit_id]
        assert scope.retrieve_shared(SUMMARY, scope_id=C_GUILD, viewer_uid="1002").items == []
        assert any(
            (c.metadata or {}).get("projection_id") == pid for c in sem.list_concepts(limit=None)
        ), "測試前提：本體仍在"

    def test_pinned_concept_is_not_widened_by_dream(self, root, layer):
        scope = _c_scope_engine(root, layer)
        alice = _c_owner(root, layer)
        ep, cid, _ = _c_share(alice)
        alice.engine.restrict_unit(alice.unit_ids(ep)[0])
        entry = alice.sharing_entry(cid)
        assert entry["level"] == "private" and entry["pinned"] is True

        assert alice.phase.run().projected == 0  # Dream 重判也不得放寬
        assert alice.sharing_entry(cid)["level"] == "private"
        assert scope.retrieve_shared(SUMMARY, scope_id=C_GUILD, viewer_uid="1002").items == []

    def test_private_intent_never_comes_from_keywords(self, root, layer):
        """沒有 LLM 就沒有掃描：不靠關鍵字猜隱私意圖。"""
        alice = _c_owner(root, layer)
        ep = alice.say("這件事別說出去")
        out = alice.engine.scan_privacy_intent(alice.unit_ids(ep))
        assert out["reason"] == "no_llm" and out["restricted"] == []


class TestS8DeveloperCannotSeePrivate:
    def test_admin_has_no_owner_handle_and_only_counts(self, root, layer):
        from echo_memory.engine import MemoryEngine
        from echo_memory.share_admin import AccessContext, ShareAdmin

        alice = _c_owner(root, layer)
        priv = alice.say("我的健康檢查報告出來了", attrs=DM)
        alice.concept("私密健康", [priv])
        _c_share(alice)

        admin = ShareAdmin(AccessContext("9001", "developer"), root, developer_uids=("9001",))
        assert not any(isinstance(v, MemoryEngine) for v in vars(admin).values())
        assert admin._layer._provider is None and admin._layer._digest_resolver is None
        blob = json.dumps(
            [admin.list_shared(C_GUILD), admin.stats(), admin.audit(C_GUILD)], ensure_ascii=False,
        )
        assert "健康" not in blob and "私密" not in blob
        assert all(isinstance(v, int) for v in admin.stats()[C_GUILD].values())

    def test_mcp_refuses_user_namespace(self):
        from mcp_server.access_policy import (
            NamespaceAccessDenied,
            NamespacePolicy,
            check_namespace_access,
        )

        policy = NamespacePolicy(default_namespace="local-default")
        with pytest.raises(NamespaceAccessDenied):
            check_namespace_access("1001", policy)
        with pytest.raises(NamespaceAccessDenied):
            check_namespace_access("scope-g42", policy)


class TestS10NoReabsorption:
    """帶入 shared 的輪次，agent unit 不成為新概念來源；A 撤回後 B 的概念層無殘留。"""

    BOB_USER = "我想週末去露營"
    BOB_AGENT = "據說有位成員喜歡爵士樂 週末去露營也不錯"

    def _bob_turn(self, root, layer, pid):
        from echo_memory.engine import MemoryEngine
        from echo_memory.schemas.evidence import EvidenceInput

        bob = MemoryEngine(storage_path=root, namespace_id="1002")
        bob.attach_share_layer(layer)
        ep = bob.store_episode(
            self.BOB_USER, self.BOB_AGENT, "s2", salience=0.9,
            evidence=[
                EvidenceInput(source_type="user", content=self.BOB_USER, speaker_id="1002",
                              scope=C_GUILD, attrs=dict(VOICE6)),
                EvidenceInput(source_type="agent", content=self.BOB_AGENT, scope=C_GUILD),
            ],
            shared_context_ids=[pid], shared_scope_id=C_GUILD,
        )
        return bob, ep

    def test_distillation_prompt_excludes_shared_reply(self, root, layer):
        from echo_memory.dream.distillation import KnowledgeDistillation

        alice = _c_owner(root, layer)
        _, _, pid = _c_share(alice)
        bob, _ = self._bob_turn(root, layer, pid)
        seen: list[str] = []

        def llm(prompt: str) -> str:
            seen.append(prompt)
            return json.dumps([{
                "name": "露營", "description": "想週末去露營", "concept_type": "entity",
                "subject": "user", "source_indices": [0],
            }])

        KnowledgeDistillation(
            bob.episodic, bob.semantic, bob.episodic.graph, llm_fn=llm,
            evidence_store=bob.evidence,
        ).run(min_salience=0.0)
        assert seen and self.BOB_USER in seen[0]
        assert "爵士" not in seen[0]
        assert all("爵士" not in (c.description + c.name) for c in bob.semantic.list_concepts(limit=None))

    def test_revoke_leaves_no_trace_in_consumer_concept_layer(self, root, layer):
        from echo_memory.engine import make_cascade_hook

        alice = _c_owner(root, layer)
        _, _, pid = _c_share(alice)
        bob, ep_id = self._bob_turn(root, layer, pid)
        layer.set_cascade_hook(make_cascade_hook(lambda uid: bob))

        layer.revoke(C_GUILD, [pid], owner_uid="1001", reason_code="user_restrict")

        ep = bob.episodic.get(ep_id)
        assert ep.user_input == self.BOB_USER and ep.agent_response == ""
        assert "爵士" not in ep.content_summary
        units = {u.source_type: u for u in bob.get_episode_evidence(ep_id)}
        assert units["user"].content == self.BOB_USER and units["agent"].content == ""
        assert all("爵士" not in (c.description + c.name) for c in bob.semantic.list_concepts(limit=None))


class TestS11ArchitectureBan:
    """靜態掃描：sharing 相關模組不以關鍵字／regex 判斷隱私意圖或敏感度；salience 不當分享門檻；
    每輪仍存 episode。"""

    ROOT = Path(__file__).resolve().parent.parent
    FILES = (
        "echo_memory/engine.py",
        "echo_memory/retrieval/shared.py",
        "echo_memory/share_admin.py",
        "echo_memory/schemas/memory_context.py",
        "mcp_server/access_policy.py",
        "echo_memory/storage/share_layer.py",
        "echo_memory/dream/sharing_phase.py",
        "echo_memory/affect/evidence_reviewer.py",
    )

    def _source(self, rel: str) -> str:
        return (self.ROOT / rel).read_text(encoding="utf-8")

    def test_no_regex_or_keyword_privacy_judgement(self):
        import ast

        for rel in self.FILES:
            tree = ast.parse(self._source(rel))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
                    assert "re" not in names, f"{rel} 不得 import re"
        engine = self._source("echo_memory/engine.py")
        scan = engine[engine.index("def scan_privacy_intent"):engine.index("def _parse_restrict_ids")]
        for banned in ("keyword", "關鍵字", "re.search", "re.match", "re.compile", "regex"):
            assert banned not in scan.replace("不用任何關鍵字／正則", "")

    def test_salience_is_not_a_sharing_gate(self):
        for rel in ("echo_memory/retrieval/shared.py", "echo_memory/share_admin.py",
                    "mcp_server/access_policy.py"):
            assert "salience" not in self._source(rel).lower()
        engine = self._source("echo_memory/engine.py")
        block = engine[engine.index("# ─── 跨使用者分享（M10）"):engine.index("def dream(")]
        assert "salience" not in block.lower()

    def test_every_turn_is_still_stored(self, root):
        from echo_memory.engine import MemoryEngine

        bob = MemoryEngine(storage_path=root, namespace_id="1002")
        for i, ids in enumerate(([], ["proj_x"], ["proj_y"])):
            ep = bob.store_episode(f"第{i}輪", f"回覆{i}", "s", salience=0.0, shared_context_ids=ids)
            assert ep
        assert bob.episodic.count() == 3
