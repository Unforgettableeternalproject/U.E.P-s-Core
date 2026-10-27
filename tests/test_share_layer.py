"""
tests/test_share_layer.py - M10 包 B：ScopeShareLayer 與 Dream 分享 phase

涵蓋：投影只存摘要、撤回兩段式與即時消失、索引遺失／損毀 fail-closed、孤兒投影 reconcile、
成員退出 block／回來恢復、級聯 hook、LLM 不在任何鎖內、鎖序、無 LLM 不投影、
隱私意圖收緊（pinned 不再放寬）。全部使用假 llm_fn，不載入真模型。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from echo_memory.namespace import normalize_namespace_id, scope_namespace_id
from echo_memory.schemas.evidence_review import compute_basis_digest
from echo_memory.storage.share_layer import (
    REVOCATIONS_FILENAME,
    SUMMARY_MAX_CHARS,
    CascadeEvent,
    ScopeShareLayer,
    normalize_scope,
)
from tests.conftest import _deterministic_embedding
from tests.sharing_support import (
    DM,
    G42,
    G43,
    SUMMARY,
    Owner,
    ShareLLM,
    lock_is_free,
    make_layer,
)

DIGEST = "a" * 64


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def layer(root: Path) -> ScopeShareLayer:
    return make_layer(root)


def project(layer: ScopeShareLayer, **kw):
    args = dict(
        owner_uid="1001", source_concept_id="con_a", source_digest=DIGEST,
        summary=SUMMARY, claim_kind="preference", support_strength=0.5,
    )
    args.update(kw)
    return layer.project(G42, **args)


def vec(text: str = SUMMARY) -> list[float]:
    return _deterministic_embedding(text)


# ─── namespace／scope 工具 ────────────────────────────────────────────────────


class TestNamespace:
    def test_scope_namespace_id(self):
        assert scope_namespace_id("42") == "scope-g42"
        assert scope_namespace_id(123456789012345678) == "scope-g123456789012345678"
        assert normalize_namespace_id(scope_namespace_id("42")) == "scope-g42"

    def test_invalid_guild_id_rejected(self):
        for bad in ("", "../x", "a b", None):
            with pytest.raises(ValueError):
                scope_namespace_id(bad)

    def test_normalize_scope_accepts_both_forms(self):
        assert normalize_scope("guild:42") == ("guild:42", "scope-g42")
        assert normalize_scope("scope-g42") == ("guild:42", "scope-g42")
        with pytest.raises(ValueError):
            normalize_scope("namespace")


# ─── 投影 ─────────────────────────────────────────────────────────────────────


class TestProject:
    def test_project_then_search_returns_summary_only(self, layer):
        res = project(layer)
        assert res.ok and res.projection_id.startswith("proj_")
        hits = layer.search_projections(G42, vec(), exclude_owner="999", min_similarity=0.3)
        assert [h.projection_id for h in hits] == [res.projection_id]
        h = hits[0]
        assert h.summary == SUMMARY and h.owner_uid == "1001" and h.source_digest == DIGEST

    def test_search_excludes_viewer_own(self, layer):
        project(layer)
        assert layer.search_projections(G42, vec(), exclude_owner="1001") == []

    def test_store_has_no_verbatim_fields(self, layer, root):
        res = project(layer)
        sem = layer._semantic("scope-g42")
        meta = layer._projection_concepts(sem)[0].metadata
        assert meta["kind"] == "projection" and meta["projection_id"] == res.projection_id
        assert not (root / "namespaces" / "scope-g42" / "evidence").exists()
        assert not (root / "namespaces" / "scope-g42" / "episodic").exists()

    @pytest.mark.parametrize("kw,reason", [
        ({"sensitivity": "sensitive"}, "sensitivity_not_shareable"),
        ({"summary": "  "}, "empty_summary"),
        ({"summary": "字" * (SUMMARY_MAX_CHARS + 1)}, "summary_too_long"),
        ({"owner_uid": ""}, "missing_owner"),
        ({"source_digest": ""}, "missing_provenance"),
    ])
    def test_rejections(self, layer, kw, reason):
        res = project(layer, **kw)
        assert not res.ok and res.reason == reason
        assert layer.list_projections(G42) == []

    def test_invalid_scope_rejected(self, layer):
        res = layer.project(
            "namespace", owner_uid="1", source_concept_id="c", source_digest=DIGEST, summary="x",
        )
        assert not res.ok and res.reason == "invalid_scope"

    def test_no_embedding_rejected(self, root):
        layer = ScopeShareLayer(root)  # 無 embed_fn
        res = project(layer)
        assert not res.ok and res.reason == "embedding_unavailable"

    def test_embed_error_rejected(self, root):
        def boom(_t):
            raise RuntimeError("down")

        res = project(ScopeShareLayer(root, embed_fn=boom))
        assert not res.ok and res.reason == "embedding_error"

    def test_replace_revokes_previous_projection_of_same_source(self, layer):
        first = project(layer)
        second = project(layer, source_digest="b" * 64)
        assert second.ok and second.replaced_ids == [first.projection_id]
        live = layer.list_projections(G42)
        assert [v.projection_id for v in live] == [second.projection_id]
        assert not layer.is_live(G42, first.projection_id)

    def test_embedding_outside_scope_lock(self, root):
        scope_path = root / "namespaces" / "scope-g42"
        seen: list[bool] = []

        def embed(text):
            seen.append(lock_is_free(scope_path))
            return _deterministic_embedding(text)

        res = project(ScopeShareLayer(root, embed_fn=embed))
        assert res.ok and seen == [True]


# ─── 撤回（即時消失）──────────────────────────────────────────────────────────


class TestRevoke:
    def test_revoke_hides_immediately_even_if_body_delete_fails(self, layer, monkeypatch):
        res = project(layer)
        sem = layer._semantic("scope-g42")
        monkeypatch.setattr(sem, "remove_concept", lambda name: (_ for _ in ()).throw(OSError("disk")))
        out = layer.revoke(G42, [res.projection_id], reason_code="user_restrict")
        assert out.ok and out.revoked_ids == [res.projection_id]
        assert out.pending_delete == [res.projection_id]
        # 本體還在，但所有讀取端都看不到
        assert layer._projection_concepts(sem)
        assert layer.search_projections(G42, vec(), min_similarity=0.3) == []
        assert layer.list_projections(G42) == []

    def test_revoke_index_written_before_body_deleted(self, layer, root, monkeypatch):
        res = project(layer)
        sem = layer._semantic("scope-g42")
        index = root / "namespaces" / "scope-g42" / REVOCATIONS_FILENAME
        seen: list[str] = []
        real_remove = sem.remove_concept

        def spy(name):
            seen.append(index.read_text(encoding="utf-8"))
            return real_remove(name)

        monkeypatch.setattr(sem, "remove_concept", spy)
        layer.revoke(G42, [res.projection_id])
        assert seen and res.projection_id in seen[0]

    def test_reconcile_completes_pending_delete(self, layer, monkeypatch):
        res = project(layer)
        sem = layer._semantic("scope-g42")
        real_remove = sem.remove_concept
        monkeypatch.setattr(sem, "remove_concept", lambda name: (_ for _ in ()).throw(OSError("x")))
        layer.revoke(G42, [res.projection_id])
        monkeypatch.setattr(sem, "remove_concept", real_remove)
        report = layer.reconcile_owner("1001", lambda s, c: None)
        assert report.completed_deletes == 1
        assert layer._projection_concepts(sem) == []

    def test_revoke_is_idempotent_and_unknown_id_safe(self, layer):
        res = project(layer)
        assert layer.revoke(G42, [res.projection_id]).revoked_ids == [res.projection_id]
        assert layer.revoke(G42, [res.projection_id]).revoked_ids == []
        assert layer.revoke(G42, ["proj_ghost"]).ok
        assert not layer.is_live(G42, "proj_ghost")

    def test_revoke_unknown_scope_is_noop(self, layer, root):
        out = layer.revoke("guild:777", ["proj_x"])
        assert out.ok and out.revoked_ids == []
        assert not (root / "namespaces" / "scope-g777").exists()


# ─── fail-closed：索引遺失／損毀 ──────────────────────────────────────────────


class TestIndexFailClosed:
    def _index(self, root: Path) -> Path:
        return root / "namespaces" / "scope-g42" / REVOCATIONS_FILENAME

    def test_missing_index_returns_empty_and_refuses_project(self, layer, root):
        project(layer)
        self._index(root).unlink()
        assert layer.is_healthy(G42) is False
        assert layer.search_projections(G42, vec(), min_similarity=0.3) == []
        assert layer.list_projections(G42) == []
        res = project(layer, source_concept_id="con_b")
        assert not res.ok and res.reason == "index_unhealthy"

    def test_corrupt_index_returns_empty(self, layer, root):
        project(layer)
        with open(self._index(root), "a", encoding="utf-8") as f:
            f.write("{not json\n")
        assert layer.is_healthy(G42) is False
        assert layer.search_projections(G42, vec(), min_similarity=0.3) == []
        assert not project(layer, source_concept_id="con_b").ok

    def test_unknown_record_type_is_corrupt(self, layer, root):
        project(layer)
        with open(self._index(root), "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "whatever"}) + "\n")
        assert layer.is_healthy(G42) is False

    def test_blocked_owner_treated_blocked_when_unhealthy(self, layer, root):
        project(layer)
        self._index(root).unlink()
        assert layer.is_owner_blocked(G42, "1001") is True

    def test_quarantine_and_reset_recovers(self, layer, root):
        project(layer)
        self._index(root).unlink()
        assert layer.quarantine_and_reset(G42) == 1
        assert layer.is_healthy(G42) is True
        assert project(layer).ok

    def test_reconcile_reports_unhealthy_scope(self, layer, root):
        project(layer)
        self._index(root).unlink()
        report = layer.reconcile_owner("1001", lambda s, c: {"exists": True})
        assert report.unhealthy_scopes == [G42]

    def test_unknown_scope_has_empty_healthy_state(self, layer):
        assert layer.search_projections("guild:999", vec()) == []
        assert layer.is_healthy("guild:999") is True


# ─── 成員退出／回來 ───────────────────────────────────────────────────────────


class TestOwnerLifecycle:
    def test_revoke_owner_hides_all_and_blocks_new(self, layer):
        a1 = project(layer, source_concept_id="c1")
        a2 = project(layer, source_concept_id="c2", summary="另一則摘要")
        b = project(layer, owner_uid="1002", source_concept_id="c3", summary="別人的摘要")
        out = layer.revoke_owner(G42, "1001")
        assert sorted(out.revoked_ids) == sorted([a1.projection_id, a2.projection_id])
        views = layer.list_projections(G42)
        assert [v.projection_id for v in views] == [b.projection_id]
        res = project(layer, source_concept_id="c4")
        assert not res.ok and res.reason == "owner_blocked"

    def test_restore_owner_allows_reprojection_but_old_stay_revoked(self, layer):
        old = project(layer)
        layer.revoke_owner(G42, "1001")
        assert layer.restore_owner(G42, "1001") is True
        assert layer.is_owner_blocked(G42, "1001") is False
        assert not layer.is_live(G42, old.projection_id)
        new = project(layer)
        assert new.ok and layer.is_live(G42, new.projection_id)

    def test_revoke_owner_without_block(self, layer):
        project(layer)
        layer.revoke_owner(G42, "1001", block=False)
        assert project(layer, source_concept_id="c9").ok

    def test_block_hides_even_unlisted_projection(self, layer, root):
        """owner_block 單獨就足以讓該擁有者所有投影不可見（即使撤回清單沒涵蓋）。"""
        res = project(layer)
        layer._append_jsonl(
            root / "namespaces" / "scope-g42" / REVOCATIONS_FILENAME,
            [{"type": "owner_block", "owner_uid": "1001", "at": "x"}],
        )
        assert not layer.is_live(G42, res.projection_id)

    def test_revoke_owner_all_scopes(self, layer):
        a = project(layer)
        b = layer.project(
            G43, owner_uid="1001", source_concept_id="c2", source_digest=DIGEST, summary=SUMMARY,
            embedding=vec(),
        )
        other = layer.project(
            G43, owner_uid="1002", source_concept_id="c9", source_digest=DIGEST, summary="別人",
            embedding=vec("別人"),
        )
        out = layer.revoke_owner_all_scopes("1001")
        assert set(out) == {G42, G43}
        assert not layer.is_live(G42, a.projection_id)
        assert not layer.is_live(G43, b.projection_id)
        assert layer.is_live(G43, other.projection_id)
        assert layer.is_owner_blocked(G43, "1001")

    def test_list_scopes(self, layer):
        project(layer)
        layer.project(G43, owner_uid="1", source_concept_id="c", source_digest=DIGEST,
                      summary="x", embedding=vec("x"))
        assert layer.list_scopes() == [G42, G43]


# ─── reconcile（共享層端）─────────────────────────────────────────────────────


class TestReconcileOwner:
    def _info(self, pid, *, exists=True, level="scope_shared", digest=DIGEST):
        return {"exists": exists, "projection_id": pid, "level": level, "digest": digest}

    def test_orphan_when_owner_concept_missing(self, layer):
        res = project(layer)
        report = layer.reconcile_owner("1001", lambda s, c: {"exists": False})
        assert report.revoked == [res.projection_id]
        assert not layer.is_live(G42, res.projection_id)

    @pytest.mark.parametrize("info_kw", [
        {"level": "private"},
        {"digest": "c" * 64},
    ])
    def test_revokes_on_level_or_digest_change(self, layer, info_kw):
        res = project(layer)
        report = layer.reconcile_owner("1001", lambda s, c: self._info(res.projection_id, **info_kw))
        assert report.revoked == [res.projection_id]

    def test_revokes_on_projection_id_mismatch(self, layer):
        res = project(layer)
        report = layer.reconcile_owner("1001", lambda s, c: self._info("proj_other"))
        assert report.revoked == [res.projection_id]

    def test_consistent_projection_kept(self, layer):
        res = project(layer)
        report = layer.reconcile_owner("1001", lambda s, c: self._info(res.projection_id))
        assert report.revoked == []
        assert layer.is_live(G42, res.projection_id)

    def test_unknown_owner_state_not_revoked(self, layer):
        res = project(layer)

        def boom(s, c):
            raise RuntimeError("owner ns unavailable")

        assert layer.reconcile_owner("1001", boom).revoked == []
        assert layer.reconcile_owner("1001", lambda s, c: None).revoked == []
        assert layer.is_live(G42, res.projection_id)

    def test_only_touches_given_owner(self, layer):
        mine = project(layer)
        theirs = project(layer, owner_uid="1002", source_concept_id="x", summary="別人")
        layer.reconcile_owner("1001", lambda s, c: {"exists": False})
        assert not layer.is_live(G42, mine.projection_id)
        assert layer.is_live(G42, theirs.projection_id)


# ─── 級聯 hook ────────────────────────────────────────────────────────────────


class TestCascade:
    def test_hook_receives_consumer_events_outside_scope_lock(self, layer, root):
        res = project(layer)
        layer.record_consumption(
            G42, [res.projection_id], consumer_uid="1002", episode_id="ep_b",
            agent_unit_ids=["evu_agent1"],
        )
        got: list[CascadeEvent] = []
        free: list[bool] = []

        def hook(event: CascadeEvent) -> bool:
            got.append(event)
            free.append(lock_is_free(root / "namespaces" / "scope-g42"))
            return True

        layer.set_cascade_hook(hook)
        layer.revoke(G42, [res.projection_id], reason_code="user_restrict")
        assert len(got) == 1
        ev = got[0]
        assert (ev.consumer_uid, ev.episode_id, ev.agent_unit_ids) == ("1002", "ep_b", ("evu_agent1",))
        assert ev.owner_uid == "1001" and ev.projection_id == res.projection_id
        assert free == [True]
        assert layer.pending_cascades(G42) == []

    def test_failed_hook_goes_pending_and_retries(self, layer):
        res = project(layer)
        layer.record_consumption(G42, [res.projection_id], consumer_uid="1002", episode_id="ep_b",
                                 agent_unit_ids=["u1"])
        layer.set_cascade_hook(lambda ev: False)
        layer.revoke(G42, [res.projection_id])
        assert len(layer.pending_cascades(G42)) == 1
        delivered: list[CascadeEvent] = []
        layer.set_cascade_hook(lambda ev: delivered.append(ev) or True)
        assert layer.run_pending_cascades() == 1
        assert len(delivered) == 1 and layer.pending_cascades(G42) == []

    def test_hook_exception_goes_pending(self, layer):
        res = project(layer)
        layer.record_consumption(G42, [res.projection_id], consumer_uid="1002", episode_id="e")

        def boom(ev):
            raise RuntimeError("consumer engine down")

        layer.set_cascade_hook(boom)
        assert layer.revoke(G42, [res.projection_id]).ok
        assert len(layer.pending_cascades(G42)) == 1

    def test_no_hook_registered_goes_pending(self, layer):
        res = project(layer)
        layer.record_consumption(G42, [res.projection_id], consumer_uid="1002", episode_id="e")
        layer.revoke(G42, [res.projection_id])
        assert len(layer.pending_cascades(G42)) == 1

    def test_revoke_owner_cascades_every_projection(self, layer):
        a = project(layer, source_concept_id="c1")
        b = project(layer, source_concept_id="c2", summary="第二則")
        layer.record_consumption(G42, [a.projection_id, b.projection_id], consumer_uid="1002",
                                 episode_id="ep")
        events: list[CascadeEvent] = []
        layer.set_cascade_hook(lambda ev: events.append(ev) or True)
        layer.revoke_owner(G42, "1001")
        assert {e.projection_id for e in events} == {a.projection_id, b.projection_id}
        assert {e.reason_code for e in events} == {"member_left"}

    def test_consumers_of(self, layer):
        res = project(layer)
        layer.record_consumption(G42, [res.projection_id], consumer_uid="1002", episode_id="e")
        assert layer.consumers_of(G42, res.projection_id) == {"1002"}
        assert layer.consumers_of(G42, "proj_none") == set()

    def test_unrelated_consumer_not_cascaded(self, layer):
        a = project(layer, source_concept_id="c1")
        b = project(layer, source_concept_id="c2", summary="第二則")
        layer.record_consumption(G42, [b.projection_id], consumer_uid="1003", episode_id="e")
        events: list[CascadeEvent] = []
        layer.set_cascade_hook(lambda ev: events.append(ev) or True)
        layer.revoke(G42, [a.projection_id])
        assert events == []


# ─── 唯讀查詢／統計 ───────────────────────────────────────────────────────────


class TestQueries:
    def test_stats_only_counts(self, layer):
        a = project(layer)
        project(layer, owner_uid="1002", source_concept_id="x", summary="別人")
        layer.revoke(G42, [a.projection_id])
        stats = layer.stats(G42)
        assert stats["projections_live"] == 1 and stats["revoked"] == 1 and stats["healthy"] == 1
        assert all(isinstance(v, int) for v in stats.values())

    def test_digest_resolver_drops_stale_projection(self, root):
        layer = make_layer(root, owner_digest_resolver=lambda uid, cid: "z" * 64)
        project(layer)
        assert layer.search_projections(G42, vec(), min_similarity=0.3) == []

    def test_digest_resolver_unknown_keeps(self, root):
        layer = make_layer(root, owner_digest_resolver=lambda uid, cid: None)
        res = project(layer)
        assert layer.is_live(G42, res.projection_id)

    def test_sensitive_projection_never_visible(self, layer):
        res = project(layer)
        sem = layer._semantic("scope-g42")
        concept = layer._projection_concepts(sem)[0]
        concept.metadata["sensitivity"] = "sensitive"
        assert not layer.is_live(G42, res.projection_id)


# ─── Dream 分享 phase ─────────────────────────────────────────────────────────


@pytest.fixture
def alice(root, layer):
    llm = ShareLLM()
    owner = Owner(root, "1001", layer, llm)
    owner.llm_stub = llm
    return owner


def one_concept(owner: Owner, text: str = "我喜歡爵士樂", name: str = "喜歡爵士樂", **say_kw) -> str:
    ep = owner.say(text, **say_kw)
    return owner.concept(name, [ep])


class TestPhase:
    def test_projects_scope_shared_concept(self, alice, layer):
        cid = one_concept(alice)
        rep = alice.phase.run()
        assert rep.projected == 1 and rep.judged == 1
        entry = alice.sharing_entry(cid)
        assert entry["level"] == "scope_shared" and entry["projection_id"]
        assert entry["basis_digest"] == compute_basis_digest(alice.get(cid))
        views = layer.list_projections(G42)
        assert [v.summary for v in views] == [SUMMARY]
        assert views[0].owner_uid == "1001" and views[0].source_concept_id == cid

    def test_dm_only_concept_never_judged(self, alice, layer):
        one_concept(alice, attrs=DM)
        rep = alice.phase.run()
        assert rep.projected == 0 and alice.llm_stub.calls == []
        assert layer.list_projections(G42) == []

    def test_no_scope_unit_never_judged(self, alice, layer):
        one_concept(alice, scope="namespace")
        alice.phase.run()
        assert alice.llm_stub.calls == [] and layer.list_projections(G42) == []

    def test_llm_input_contains_only_shareable_units(self, alice):
        ep_dm = alice.say("DM 裡的私密話", attrs=DM, scope="namespace")
        ep_g = alice.say("群組裡說的話")
        cid = alice.concept("混合概念", [ep_dm, ep_g])
        alice.phase.run()
        prompts = alice.llm_stub.share_calls
        assert len(prompts) == 1
        assert "群組裡說的話" in prompts[0] and "DM 裡的私密話" not in prompts[0]
        assert alice.sharing_entry(cid)["cited_unit_ids"] == alice.unit_ids(ep_g)

    def test_llm_runs_outside_every_lock(self, root, layer):
        scope_path = root / "namespaces" / "scope-g42"
        seen: list[tuple[bool, bool]] = []
        holder: dict[str, Owner] = {}

        def probe(_prompt):
            seen.append((lock_is_free(holder["o"].path), lock_is_free(scope_path)))

        owner = Owner(root, "1001", layer, ShareLLM(probe=probe))
        holder["o"] = owner
        one_concept(owner)
        rep = owner.phase.run()
        assert rep.projected == 1
        assert seen == [(True, True)]

    def test_layer_write_outside_owner_lock(self, root, layer, monkeypatch):
        """鎖序：先擁有者後 scope、不巢狀 —— 寫投影時擁有者鎖是放開的。"""
        owner = Owner(root, "1001", layer, ShareLLM())
        one_concept(owner)
        seen: list[bool] = []
        real = layer.project

        def spy(*a, **kw):
            seen.append(lock_is_free(owner.path))
            return real(*a, **kw)

        monkeypatch.setattr(layer, "project", spy)
        assert owner.phase.run().projected == 1
        assert seen == [True]

    def test_no_llm_no_projection(self, root, layer):
        owner = Owner(root, "1001", layer, None)
        one_concept(owner)
        rep = owner.phase.run()
        assert rep.projected == 0 and rep.skip_reason == "no_llm_or_ledger"
        assert layer.list_projections(G42) == []

    def test_no_ledger_no_projection(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM())
        owner.reviewer._privacy_ledger = None
        one_concept(owner)
        assert owner.phase.run().projected == 0
        assert owner.llm.calls == []

    def test_no_owner_uid_no_projection(self, alice, layer):
        alice.phase._owner = ""
        one_concept(alice)
        assert alice.phase.run().skip_reason == "no_owner_uid"
        assert layer.list_projections(G42) == []

    def test_sensitive_not_projected_and_not_rejudged(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM(sensitivity="sensitive"))
        cid = one_concept(owner)
        owner.phase.run()
        assert layer.list_projections(G42) == [] and owner.sharing_entry(cid) == {}
        owner.phase.run()
        assert len(owner.llm.share_calls) == 1  # digest 未變 -> 不重送

    def test_summary_not_supported_by_cited_only_not_projected(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM(supports=False))
        one_concept(owner)
        owner.phase.run()
        assert layer.list_projections(G42) == []

    def test_gate_rejection_revokes_written_projection(self, alice, layer, monkeypatch):
        one_concept(alice)
        monkeypatch.setattr(alice.coordinator, "submit_share", lambda *a, **k: (False, None))
        rep = alice.phase.run()
        assert rep.projected == 0
        assert layer.list_projections(G42) == []
        assert layer.stats(G42)["revoked"] == 1

    def test_gate_exception_does_not_leave_visible_orphan(self, alice, layer, monkeypatch):
        one_concept(alice)

        def boom(*a, **k):
            raise RuntimeError("crash")

        monkeypatch.setattr(alice.coordinator, "submit_share", boom)
        alice.phase.run()
        assert layer.list_projections(G42) == []

    def test_orphan_projection_revoked_by_reconcile(self, alice, layer):
        """步驟 3、4 之間崩潰：共享層有投影、擁有者端沒有 projection_id -> reconcile 撤回。"""
        cid = one_concept(alice)
        orphan = layer.project(
            G42, owner_uid="1001", source_concept_id=cid,
            source_digest=compute_basis_digest(alice.get(cid)), summary=SUMMARY, embedding=vec(),
        )
        assert layer.is_live(G42, orphan.projection_id)
        rep = alice.phase.reconcile()
        assert rep.revoked == 1
        assert not layer.is_live(G42, orphan.projection_id)

    def test_owner_side_dangling_projection_id_cleared(self, alice, layer):
        cid = one_concept(alice)
        alice.phase.run()
        pid = alice.live_pid(cid)
        layer.revoke(G42, [pid])  # 共享端消失（例如步驟 2 之後崩潰）
        alice.phase.reconcile()
        assert alice.live_pid(cid) == ""
        assert alice.sharing_entry(cid)["level"] == "private"

    def test_reconcile_after_dangling_allows_reprojection(self, alice, layer):
        cid = one_concept(alice)
        alice.phase.run()
        layer.revoke(G42, [alice.live_pid(cid)])
        alice.phase.reconcile()
        rep = alice.phase.run()
        assert rep.projected == 1 and alice.live_pid(cid)

    def test_reconcile_works_without_llm(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM())
        cid = one_concept(owner)
        owner.phase.run()
        owner.engine.semantic.get(cid).description = "被改過的描述"  # digest 改變
        rep = owner.phase_without_llm().reconcile()
        assert rep.revoked == 1 and layer.list_projections(G42) == []

    def test_blocked_owner_not_judged(self, alice, layer):
        one_concept(alice)
        layer.revoke_owner(G42, "1001")
        rep = alice.phase.run()
        assert rep.projected == 0 and alice.llm_stub.calls == []

    def test_restore_owner_reprojects_next_dream(self, alice, layer):
        cid = one_concept(alice)
        alice.phase.run()
        layer.revoke_owner(G42, "1001")
        alice.phase.reconcile()  # 擁有者端清掉 projection_id
        assert alice.phase.run().projected == 0
        layer.restore_owner(G42, "1001")
        rep = alice.phase.run()
        assert rep.projected == 1
        assert layer.is_live(G42, alice.live_pid(cid))

    def test_privacy_intent_restricts_and_never_projects(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM(restrict_all=True))
        cid = one_concept(owner, "這個別說出去 我喜歡爵士樂")
        rep = owner.phase.run()
        assert rep.projected == 0 and rep.restricted == 1
        assert layer.list_projections(G42) == []
        assert owner.sharing_entry(cid).get("level", "private") == "private"
        assert owner.ledger.restricted_ids() == set(owner.unit_ids(owner.get(cid).source_episode_ids[0]))
        owner.engine.semantic.get(cid).add_source_episode("ep_touch")  # 內容變動 -> 會重判
        owner.reviewer._llm_fn = ShareLLM()  # 之後 LLM 不再有意見也不得放寬
        owner.phase.run()
        assert layer.list_projections(G42) == []  # 被收緊的 unit 永遠不在 U_S

    def test_privacy_intent_revokes_existing_projection(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM())
        cid = one_concept(owner)
        owner.phase.run()
        assert layer.is_live(G42, owner.live_pid(cid))
        # 新的一輪對話出現隱私意圖 -> 內容改變重新判定
        ep2 = owner.say("剛剛那個別說出去")
        owner.engine.semantic.get(cid).add_source_episode(ep2)
        owner.reviewer._llm_fn = ShareLLM(restrict_all=True)
        pid = owner.live_pid(cid)
        owner.phase.run()
        assert not layer.is_live(G42, pid)
        assert owner.sharing_entry(cid)["pinned"] is True

    def test_forgotten_cited_unit_revokes_on_reconcile(self, alice, layer):
        ep = alice.say("我喜歡爵士樂")
        cid = alice.concept("喜歡爵士樂", [ep])
        alice.phase.run()
        pid = alice.live_pid(cid)
        assert layer.is_live(G42, pid)
        alice.engine.evidence.redact(alice.unit_ids(ep))  # forget／遮蔽
        alice.phase.reconcile()
        assert not layer.is_live(G42, pid)
        assert alice.live_pid(cid) == ""

    def test_modified_concept_revoked_then_reprojected_after_rejudge(self, alice, layer):
        cid = one_concept(alice)
        alice.phase.run()
        pid = alice.live_pid(cid)
        alice.engine.semantic.get(cid).description = "改過的描述"
        alice.phase.reconcile()
        assert not layer.is_live(G42, pid)
        # 重判後才可能重新出現
        alice.phase.run()
        new_pid = alice.live_pid(cid)
        assert new_pid and new_pid != pid and layer.is_live(G42, new_pid)

    def test_second_run_does_not_rejudge_unchanged_concept(self, alice):
        one_concept(alice)
        alice.phase.run()
        alice.phase.run()
        assert len(alice.llm_stub.share_calls) == 1

    def test_batch_limit(self, alice, layer):
        for i in range(3):
            one_concept(alice, f"我喜歡主題{i}", f"主題{i}")
        rep = alice.phase.run(limit=2)
        assert rep.judged == 2

    def test_holds_do_not_record_judged(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM(raw="not json"))
        one_concept(owner)
        assert owner.phase.run().held == 1
        owner.reviewer._llm_fn = ShareLLM()
        assert owner.phase.run().projected == 1


# ─── DreamEngine 整合 ─────────────────────────────────────────────────────────


def make_dream(owner: Owner, layer: ScopeShareLayer, llm):
    from echo_memory.dream.engine import DreamEngine
    from echo_memory.storage.locking import storage_lock

    return DreamEngine(
        graph=owner.engine.episodic.graph,
        episodic=owner.engine.episodic,
        semantic=owner.engine.semantic,
        llm_fn=llm,
        concept_inhibitor=owner.gate,
        evidence_store=owner.engine.evidence,
        enable_idle=False,
        enable_cron=False,
        lock_factory=lambda: storage_lock(owner.engine.storage_path),
        share_layer=layer,
        owner_uid=owner.uid,
    )


class TestDreamIntegration:
    def test_dream_now_runs_sharing_phase(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM())
        cid = one_concept(owner)
        dream = make_dream(owner, layer, owner.llm)
        report = dream.dream_now("manual")
        assert report.error is None
        assert report.sharing is not None and report.sharing.projected == 1
        assert report.sharing_reconcile is not None
        assert layer.is_live(G42, owner.live_pid(cid))
        assert "分享:" in report.summary()

    def test_dream_without_llm_only_reconciles(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM())
        cid = one_concept(owner)
        owner.phase.run()
        pid = owner.live_pid(cid)
        owner.engine.semantic.get(cid).description = "被改過"
        dream = make_dream(owner, layer, None)
        report = dream.dream_now("manual")
        assert report.error is None
        assert report.sharing.skip_reason == "no_llm_or_ledger"
        assert report.sharing_reconcile.revoked == 1
        assert not layer.is_live(G42, pid)

    def test_dream_without_share_layer_has_no_phase(self, root, layer):
        from echo_memory.dream.engine import DreamEngine

        owner = Owner(root, "1001", layer, ShareLLM())
        dream = DreamEngine(
            graph=owner.engine.episodic.graph, episodic=owner.engine.episodic,
            semantic=owner.engine.semantic, llm_fn=owner.llm, concept_inhibitor=owner.gate,
            evidence_store=owner.engine.evidence, enable_idle=False, enable_cron=False,
        )
        assert dream._sharing_phase is None
        report = dream.dream_now("manual")
        assert report.sharing is None and report.sharing_reconcile is None

    def test_dream_without_owner_uid_never_projects(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM())
        one_concept(owner)
        dream = make_dream(owner, layer, owner.llm)
        dream._sharing_phase._owner = ""
        report = dream.dream_now("manual")
        assert report.sharing.skip_reason == "no_owner_uid"
        assert layer.list_projections(G42) == []

    def test_sharing_failure_does_not_break_dream(self, root, layer, monkeypatch):
        owner = Owner(root, "1001", layer, ShareLLM())
        one_concept(owner)
        dream = make_dream(owner, layer, owner.llm)

        def boom(*a, **k):
            raise RuntimeError("sharing exploded")

        monkeypatch.setattr(dream._sharing_phase, "run", boom)
        monkeypatch.setattr(dream._sharing_phase, "reconcile", boom)
        report = dream.dream_now("manual")
        assert report.error is None and report.sharing is None



# ─── C1：分享判定只看 U_S 證據，不帶概念名稱／描述 ────────────────────────────


class TestPromptIsolation:
    def test_prompt_has_no_concept_name_or_description(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM())
        ep_dm = owner.say("DM私事ABC", attrs=DM, scope="namespace")
        ep_g = owner.say("群組公開事XYZ")
        cid = owner.concept(
            "含私事的概念名稱QQQ", [ep_dm, ep_g], desc="描述裡混了DM私事ABC與其他衍生內容",
        )
        assert owner.phase.run().projected == 1
        prompt = owner.llm.share_calls[0]
        assert "群組公開事XYZ" in prompt
        for banned in ("DM私事ABC", "含私事的概念名稱QQQ", "衍生內容"):
            assert banned not in prompt
        view = layer.list_projections(G42)[0]
        assert view.summary == SUMMARY and "DM私事ABC" not in view.summary
        assert owner.get(cid).description.startswith("描述裡混了")  # 擁有者端原樣保留

    def test_cross_guild_content_not_in_prompt(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM())
        cid = owner.concept(
            "跨場域概念", [owner.say("G42公開事", scope=G42), owner.say("G43機密事", scope=G43)],
            desc="描述提到G43機密事",
        )
        owner.phase.run()
        p42 = next(c for c in owner.llm.share_calls if "G42公開事" in c)
        assert "G43機密事" not in p42 and "描述提到" not in p42
        assert cid

    def test_strength_recomputed_from_shareable_units_only(self, root, layer):
        """DM 裡說了很多次（權重高），但投影的 support_strength 只依群組 U_S 重算。"""
        owner = Owner(root, "1001", layer, ShareLLM())
        eps = [owner.say(f"DM私事{i}", attrs=DM, scope="namespace") for i in range(4)]
        eps.append(owner.say("五人以上的群組說法", attrs={"channel_kind": "voice", "audience_size": 6}))
        owner.concept("混合來源", eps)
        owner.phase.run()
        view = layer.list_projections(G42)[0]
        assert view.support_strength == pytest.approx(0.08, abs=1e-3)
        assert view.review_status == "insufficient"


# ─── M5：多個 layer 實例交錯讀寫 ──────────────────────────────────────────────


def _proj(layer, owner, cid, text):
    return layer.project(
        G42, owner_uid=owner, source_concept_id=cid, source_digest=DIGEST,
        summary=text, embedding=vec(text),
    )


class TestMultiInstance:
    def test_interleaved_project_revoke_search(self, root):
        l1, l2 = make_layer(root), make_layer(root)
        a = _proj(l1, "1", "ca", "摘要甲")
        assert l2.is_live(G42, a.projection_id)  # l2 此刻才建立快取
        b = _proj(l1, "2", "cb", "摘要乙")
        assert l2.is_live(G42, b.projection_id)  # 過期快取依檔案簽章重載
        assert l2.search_projections(G42, vec("摘要乙"), min_similarity=0.9)
        c = _proj(l2, "3", "cc", "摘要丙")
        assert l1.revoke(G42, [a.projection_id]).ok  # l1 的寫入不得丟掉 l2 剛寫的 c
        l3 = make_layer(root)
        assert {v.projection_id for v in l3.list_projections(G42)} == {b.projection_id, c.projection_id}
        assert not l2.is_live(G42, a.projection_id)

    def test_stale_instance_cannot_resurrect_revoked_body(self, root):
        l1, l2 = make_layer(root), make_layer(root)
        a = _proj(l1, "1", "ca", "摘要甲")
        assert l2.is_live(G42, a.projection_id)
        l1.revoke(G42, [a.projection_id])
        _proj(l2, "2", "cb", "摘要乙")  # l2 的快取裡還有 a 的本體；寫入不得讓 a 復活
        l3 = make_layer(root)
        assert not l3.is_live(G42, a.projection_id)
        assert len(l3._projection_concepts(l3._semantic("scope-g42"))) == 1

    def test_quarantine_not_resurrected_by_stale_instance(self, root):
        l1, l2 = make_layer(root), make_layer(root)
        a = _proj(l1, "1", "ca", "摘要甲")
        assert l2.is_live(G42, a.projection_id)
        assert l1.quarantine_and_reset(G42) == 1
        assert l2.list_projections(G42) == []
        assert l2.search_projections(G42, vec("摘要甲"), min_similarity=0.0) == []
        l2.revoke(G42, [a.projection_id])
        d = _proj(l2, "2", "cd", "摘要丁")
        assert d.ok
        assert [v.projection_id for v in l1.list_projections(G42)] == [d.projection_id]

    def test_provider_instance_is_reloaded_too(self, root):
        shared = make_layer(root)
        a = _proj(shared, "1", "ca", "摘要甲")
        sem = shared._semantic("scope-g42")
        l2 = make_layer(root, semantic_provider=lambda ns: sem)
        assert l2.is_live(G42, a.projection_id)
        b = _proj(make_layer(root), "2", "cb", "摘要乙")
        assert l2.is_live(G42, b.projection_id)


# ─── quarantine 保留 block 並觸發級聯 ─────────────────────────────────────────


class TestQuarantine:
    def test_keeps_owner_block_and_queues_cascade(self, layer, root):
        a = project(layer)
        layer.revoke_owner(G42, "1002")  # 另一位成員被 block
        layer.record_consumption(G42, [a.projection_id], consumer_uid="1003", episode_id="ep")
        with open(root / "namespaces" / "scope-g42" / REVOCATIONS_FILENAME, "a", encoding="utf-8") as f:
            f.write("garbage\n")
        assert layer.is_healthy(G42) is False
        assert layer.quarantine_and_reset(G42) == 1
        assert layer.is_healthy(G42) is True
        assert layer.is_owner_blocked(G42, "1002")
        assert not layer.is_live(G42, a.projection_id)
        assert [e.projection_id for e in layer.pending_cascades(G42)] == [a.projection_id]
        assert layer.pending_cascades(G42)[0].reason_code == "quarantine"

    def test_cascade_delivered_when_hook_present(self, layer):
        a = project(layer)
        layer.record_consumption(G42, [a.projection_id], consumer_uid="1003", episode_id="ep")
        got = []
        layer.set_cascade_hook(lambda e: got.append(e) or True)
        layer.quarantine_and_reset(G42)
        assert len(got) == 1 and got[0].consumer_uid == "1003"


# ─── m1／m3：held 計次、restrict 失敗非終局 ───────────────────────────────────


class TestPhaseRobustness:
    def test_held_attempts_capped_then_abandoned(self, root, layer):
        owner = Owner(root, "1001", layer, ShareLLM(raw="not json"))
        cid = one_concept(owner)
        for _ in range(5):
            owner.phase.run()
        assert len(owner.llm.share_calls) == 3  # concept_held_max_retries 預設 3
        owner.get(cid).description = "改了"
        owner.reviewer._llm_fn = ShareLLM()
        assert owner.phase.run().projected == 1  # 內容改變 -> 重新送審

    def test_held_does_not_consume_batch(self, root, layer):
        def llm(prompt):
            if "壞掉的" in prompt:
                return "not json"
            return ShareLLM()(prompt)

        owner = Owner(root, "1001", layer, llm)
        one_concept(owner, "好的說法", "概念好")
        one_concept(owner, "壞掉的說法", "概念壞")  # 較新 -> 先處理
        rep = owner.phase.run(limit=1)
        assert rep.held == 1 and rep.projected == 1

    def test_summary_too_long_retried_once(self, root, layer):
        answers = iter(["字" * 150, SUMMARY])

        def llm(prompt):
            return ShareLLM(summary=next(answers))(prompt)

        owner = Owner(root, "1001", layer, llm)
        one_concept(owner)
        rep = owner.phase.run()
        assert rep.projected == 1 and rep.held == 0

    def test_restrict_failure_is_not_final(self, root, layer, monkeypatch):
        owner = Owner(root, "1001", layer, ShareLLM(restrict_all=True))
        one_concept(owner)
        monkeypatch.setattr(owner.coordinator, "submit_restrict_units", lambda *a, **k: [])
        rep = owner.phase.run()
        assert rep.projected == 0 and rep.restricted == 0
        assert layer.list_projections(G42) == []
        owner.phase.run()
        assert len(owner.llm.share_calls) == 2  # 未記入 judged -> 下次 Dream 重判
