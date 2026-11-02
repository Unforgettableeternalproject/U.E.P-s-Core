"""
tests/test_sharing_model.py — M10 包 A：跨使用者分享的資料模型與 Patch Gate

涵蓋：origin_visibility（fail-closed 規則表）、scope 字串工具、Concept.sharing（舊 JSON
載入為 private、digest 綁定）、PrivacyLedger、corroborated_by 的 sp: 參考、
share patch（project／retract／restrict）的 build／apply／rollback／stale／audit-before-mutate，
以及 Golden S1（私密不外洩，資料面）、S4（覆寫收緊與 pinned，資料面）、S9（fail-closed）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from echo_memory.affect.concept_inhibitor import ConceptInhibitor
from echo_memory.config import get_config
from echo_memory.schemas.concept import Concept
from echo_memory.schemas.evidence import EvidenceUnit
from echo_memory.schemas.evidence_review import (
    EvidenceReview,
    compute_basis_digest,
    make_sp_ref,
    parse_sp_ref,
    split_corroboration,
    validate_corroborated_by,
)
from echo_memory.schemas.sharing import (
    LEVEL_PRIVATE,
    LEVEL_SCOPE_SHARED,
    ScopeSharing,
    SharingState,
    guild_scope,
    namespace_to_scope,
    origin_scope,
    origin_visibility,
    parse_guild_scope,
    scope_to_namespace_id,
)
from echo_memory.storage.privacy_ledger import PrivacyLedger, PrivacyLedgerError
from echo_memory.storage.semantic import SemanticMemory

SCOPE = "guild:123"
DIGEST = "a" * 64


def _unit(uid="u1", *, scope=SCOPE, source="user", channel="voice", audience=3,
          attrs="default", tombstoned=False) -> EvidenceUnit:
    if attrs == "default":
        attrs_value = {}
        if channel is not None:
            attrs_value["channel_kind"] = channel
        if audience is not None:
            attrs_value["audience_size"] = audience
    else:
        attrs_value = attrs
    now = datetime.now(timezone.utc)
    return EvidenceUnit(
        id=uid, episode_id=None, source_type=source, content="x", occurred_at=now,
        scope=scope, attrs=attrs_value, tombstoned_at=now if tombstoned else None,
    )


class _Ledger:
    def __init__(self, restricted=()):
        self.restricted = set(restricted)

    def is_restricted(self, uid):
        return uid in self.restricted


@pytest.fixture
def semantic(tmp_path: Path) -> SemanticMemory:
    ns = tmp_path / "ns"
    sem = SemanticMemory(storage_path=ns / "semantic")
    for name, desc in [("喜歡咖啡", "使用者喜歡喝咖啡"), ("討厭香菜", "使用者討厭香菜")]:
        c = Concept(name=name, description=desc, embedding=[1.0, 0.0],
                    source_episode_ids=["ep1", "ep2"])
        sem._concepts[name] = c
        sem._concepts_by_id[c.id] = c
    sem._save()
    return sem


@pytest.fixture
def units() -> dict[str, EvidenceUnit]:
    return {u.id: u for u in [_unit("u1"), _unit("u2")]}


@pytest.fixture
def inhibitor(semantic, units) -> ConceptInhibitor:
    # llm_fn 一律炸開：share patch 規則層 approve，不得呼叫 LLM
    def _boom(prompt: str) -> str:
        raise AssertionError("share patch 不得呼叫 LLM")

    return ConceptInhibitor(
        semantic=semantic, llm_fn=_boom,
        evidence_lookup=lambda ids: [units[i] for i in ids if i in units],
        privacy_ledger=PrivacyLedger(Path(semantic.storage_path).parent),
    )


def _coffee(semantic) -> Concept:
    return semantic.get_by_name("喜歡咖啡")


def _payload(concept, **kw) -> dict:
    base = dict(
        cited_unit_ids=["u1", "u2"],
        basis_digest=compute_basis_digest(concept),
        sensitivity="none",
        summary_digest="s" * 64,
        projection_id="proj_1",
        supports_summary_from_cited_only=True,
    )
    base.update(kw)
    return base


def _project(inhibitor, semantic, **kw):
    c = _coffee(semantic)
    return inhibitor.process_share_patch(c.id, "project", SCOPE, _payload(c, **kw))


def _audit(semantic) -> list[dict]:
    path = semantic.storage_path / "audit_log.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# ─── scope 字串工具 ──────────────────────────────────────────────────────────


class TestScopeTools:
    def test_roundtrip(self):
        assert guild_scope("123") == "guild:123"
        assert parse_guild_scope("guild:123") == "123"
        assert scope_to_namespace_id("guild:123") == "scope-g123"
        assert namespace_to_scope("scope-g123") == "guild:123"

    @pytest.mark.parametrize("bad", [
        None, "", "guild:", "guild:a b", "guild:../x", "guild:-1", "namespace", "g:1",
        "guild:" + "9" * 49, 123,
    ])
    def test_invalid_scope(self, bad):
        assert parse_guild_scope(bad) is None
        with pytest.raises(ValueError):
            scope_to_namespace_id(bad if isinstance(bad, str) else "x")

    @pytest.mark.parametrize("bad", [None, "123", "scope-g", "scope-g a", "scope-x1", 5])
    def test_invalid_namespace(self, bad):
        assert namespace_to_scope(bad) is None

    def test_guild_scope_rejects_invalid(self):
        with pytest.raises(ValueError):
            guild_scope("a b")

    def test_user_namespace_never_collides(self):
        # 使用者 namespace 為純數字，前綴 scope-g 不會碰撞
        assert namespace_to_scope("123456789012345678") is None


# ─── origin_visibility ───────────────────────────────────────────────────────


class TestOriginVisibility:
    def test_group_user_unit_is_scope_shared(self):
        u = _unit()
        assert origin_visibility(u, _Ledger()) == LEVEL_SCOPE_SHARED
        assert origin_scope(u, _Ledger()) == SCOPE

    def test_audience_boundary_is_two(self):
        assert origin_visibility(_unit(audience=2), _Ledger()) == LEVEL_SCOPE_SHARED
        assert origin_visibility(_unit(audience=1), _Ledger()) == LEVEL_PRIVATE

    @pytest.mark.parametrize("kw", [
        dict(attrs={}),                                   # 缺 attrs
        dict(attrs=None),                                 # attrs 壞值
        dict(channel=None),                               # 缺 channel_kind
        dict(audience=None),                              # 缺 audience_size
        dict(channel="dm", audience=5),                   # dm 即使人數大也 private
        dict(audience=1),                                 # 單人
        dict(audience=0),
        dict(audience=True),                              # bool 不是人數
        dict(audience="abc"),
        dict(channel="cli"),
        dict(channel="mcp"),
        dict(scope="namespace"),                          # 缺 scope
        dict(scope="session:abc"),
        dict(scope="guild:"),
        dict(source="agent"),
        dict(source="tool"),
        dict(source="observation"),
        dict(tombstoned=True),
    ])
    def test_fail_closed(self, kw):
        assert origin_visibility(_unit(**kw), _Ledger()) == LEVEL_PRIVATE
        assert origin_scope(_unit(**kw), _Ledger()) is None

    def test_ledger_override_wins(self):
        assert origin_visibility(_unit("u9"), _Ledger({"u9"})) == LEVEL_PRIVATE

    def test_missing_or_broken_ledger_is_private(self):
        assert origin_visibility(_unit(), None) == LEVEL_PRIVATE

        class Broken:
            def is_restricted(self, uid):
                raise RuntimeError("boom")

        assert origin_visibility(_unit(), Broken()) == LEVEL_PRIVATE

    def test_none_unit(self):
        assert origin_visibility(None, _Ledger()) == LEVEL_PRIVATE


# ─── Concept.sharing ─────────────────────────────────────────────────────────


class TestConceptSharing:
    def test_default_is_private_and_empty(self):
        c = Concept(name="n", description="d")
        assert c.sharing == {}
        assert c.effective_sharing_level(SCOPE) == LEVEL_PRIVATE
        assert c.to_dict()["sharing"] == {}

    def test_old_json_without_sharing_loads_private(self):
        c = Concept(name="n", description="d")
        data = c.to_dict()
        data.pop("sharing")
        loaded = Concept.from_dict(data)
        assert loaded.sharing == {}
        assert loaded.effective_sharing_level(SCOPE) == LEVEL_PRIVATE

    def test_old_semantic_file_loads_private(self, tmp_path):
        sem = SemanticMemory(storage_path=tmp_path / "semantic")
        c = Concept(name="舊概念", description="沒有 sharing 欄位", embedding=[1.0, 0.0])
        sem._concepts[c.name] = c
        sem._concepts_by_id[c.id] = c
        sem._save()
        path = next(p for p in (tmp_path / "semantic").glob("*.json") if "concept" in p.name)
        raw = json.loads(path.read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else raw.get("concepts", raw)
        for item in (items.values() if isinstance(items, dict) else items):
            if isinstance(item, dict):
                item.pop("sharing", None)
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        reloaded = SemanticMemory(storage_path=tmp_path / "semantic")
        got = reloaded.get_by_name("舊概念")
        assert got is not None and got.sharing == {}
        assert got.effective_sharing_level(SCOPE) == LEVEL_PRIVATE

    @pytest.mark.parametrize("bad", [None, "x", 5, [], {"by_scope": "x"}, {"by_scope": []}])
    def test_bad_sharing_value_is_private(self, bad):
        c = Concept(name="n", description="d")
        data = c.to_dict()
        data["sharing"] = bad
        loaded = Concept.from_dict(data)
        assert loaded.effective_sharing_level(SCOPE) == LEVEL_PRIVATE
        assert loaded.sharing_state().by_scope == {}

    def test_public_and_unknown_level_degrade_to_private(self):
        for level in ("public", "everyone", "", None, 5):
            entry = ScopeSharing.from_dict({"level": level})
            assert entry.level == LEVEL_PRIVATE

    def test_invalid_scope_keys_dropped(self):
        state = SharingState.from_dict({"by_scope": {"bogus": {"level": "scope_shared"}}})
        assert state.by_scope == {}

    def test_effective_level_bound_to_basis_digest(self):
        c = Concept(name="n", description="d", embedding=[1.0])
        c.sharing = {"by_scope": {SCOPE: ScopeSharing(
            level=LEVEL_SCOPE_SHARED, basis_digest=compute_basis_digest(c),
        ).to_dict()}}
        assert c.effective_sharing_level(SCOPE) == LEVEL_SCOPE_SHARED
        assert c.effective_sharing_level("guild:other") == LEVEL_PRIVATE
        c.description = "d changed"
        assert c.effective_sharing_level(SCOPE) == LEVEL_PRIVATE

    def test_deprecated_or_missing_digest_is_private(self):
        c = Concept(name="n", description="d")
        c.sharing = {"by_scope": {SCOPE: ScopeSharing(level=LEVEL_SCOPE_SHARED).to_dict()}}
        assert c.effective_sharing_level(SCOPE) == LEVEL_PRIVATE  # 缺 digest
        c.sharing["by_scope"][SCOPE]["basis_digest"] = compute_basis_digest(c)
        assert c.effective_sharing_level(SCOPE) == LEVEL_SCOPE_SHARED
        c.status = "deprecated"
        assert c.effective_sharing_level(SCOPE) == LEVEL_PRIVATE

    def test_roundtrip_through_dict(self):
        c = Concept(name="n", description="d")
        c.sharing = SharingState(by_scope={SCOPE: ScopeSharing(
            level=LEVEL_SCOPE_SHARED, decided_by="llm", cited_unit_ids=["u1"], projection_id="p",
        )}).to_dict()
        again = Concept.from_dict(c.to_dict())
        assert again.sharing == c.sharing


# ─── PrivacyLedger ───────────────────────────────────────────────────────────


class TestPrivacyLedger:
    def test_restrict_idempotent_and_persistent(self, tmp_path):
        led = PrivacyLedger(tmp_path)
        assert led.restrict("u1", patch_id="p1") is True
        assert led.restrict("u1", patch_id="p2") is False
        assert led.is_restricted("u1") and not led.is_restricted("u2")
        assert PrivacyLedger(tmp_path).is_restricted("u1")
        assert len(led) == 1
        assert led.get("u1")["patch_id"] == "p1"

    def test_ledger_has_no_content(self, tmp_path):
        PrivacyLedger(tmp_path).restrict("u1", patch_id="p1", reason_code="r")
        raw = json.loads((tmp_path / "privacy_ledger.json").read_text(encoding="utf-8"))
        assert set(raw["units"]["u1"]) == {"restricted_at", "by", "patch_id", "reason_code"}

    def test_corrupt_ledger_restricts_everything_and_refuses_write(self, tmp_path):
        (tmp_path / "privacy_ledger.json").write_text("{not json", encoding="utf-8")
        led = PrivacyLedger(tmp_path)
        assert led.is_corrupt
        assert led.is_restricted("anything") is True
        with pytest.raises(PrivacyLedgerError):
            led.restrict("u1")
        assert (tmp_path / "privacy_ledger.json").read_text(encoding="utf-8") == "{not json"

    def test_wrong_structure_is_corrupt(self, tmp_path):
        (tmp_path / "privacy_ledger.json").write_text('{"units": []}', encoding="utf-8")
        assert PrivacyLedger(tmp_path).is_restricted("u1") is True

    def test_missing_file_is_empty(self, tmp_path):
        led = PrivacyLedger(tmp_path)
        assert not led.is_restricted("u1") and len(led) == 0

    def test_sees_external_change(self, tmp_path):
        a, b = PrivacyLedger(tmp_path), PrivacyLedger(tmp_path)
        assert not a.is_restricted("u1")
        b.restrict("u1")
        assert a.is_restricted("u1")

    def test_unrestrict_only_removes_matching_patch(self, tmp_path):
        led = PrivacyLedger(tmp_path)
        led.restrict("u1", patch_id="p1")
        led.restrict("u2", patch_id="p2")
        assert led.unrestrict(["u1", "u2"], patch_id="p1") == ["u1"]
        assert not led.is_restricted("u1") and led.is_restricted("u2")
        with pytest.raises(ValueError):
            led.unrestrict(["u2"], patch_id="")

    def test_invalid_unit_id(self, tmp_path):
        with pytest.raises(ValueError):
            PrivacyLedger(tmp_path).restrict("")


# ─── corroborated_by 的 sp: 參考 ─────────────────────────────────────────────


class TestSpRefs:
    def test_make_and_parse(self):
        ref = make_sp_ref(SCOPE, "proj_1", DIGEST)
        assert ref == f"sp:guild:123:proj_1:{DIGEST}"
        parsed = parse_sp_ref(ref)
        assert (parsed.scope, parsed.projection_id, parsed.source_digest) == (SCOPE, "proj_1", DIGEST)
        assert str(parsed) == ref

    @pytest.mark.parametrize("bad", [
        "sp:", "sp:guild:123", "sp:guild:123:p", f"sp:namespace:p:{DIGEST}",
        f"sp:guild:123::{DIGEST}", "sp:guild:123:p:short", f"sp:guild:123:p:{'Z' * 64}",
        f"sp:guild:123:p q:{DIGEST}", "u1", None, 5,
    ])
    def test_parse_rejects(self, bad):
        assert parse_sp_ref(bad) is None

    def test_make_rejects_bad_fields(self):
        with pytest.raises(ValueError):
            make_sp_ref("namespace", "p", DIGEST)

    def test_split_and_validate(self):
        ref = make_sp_ref(SCOPE, "p", DIGEST)
        units, sps = split_corroboration(["u1", ref, "sp:bad", ""])
        assert units == ["u1"] and [str(s) for s in sps] == [ref]
        assert validate_corroborated_by(["u1", ref]) is None
        assert validate_corroborated_by([]) is None
        assert validate_corroborated_by(None) is None
        assert validate_corroborated_by("u1") is not None
        assert validate_corroborated_by(["sp:bad"]) is not None
        assert validate_corroborated_by([""]) is not None
        assert validate_corroborated_by([5]) is not None

    def test_review_patch_accepts_sp_ref_for_intention(self, inhibitor, semantic):
        c = _coffee(semantic)
        review = EvidenceReview(
            status="supported", support_strength=0.5, cited_evidence_ids=["u1"],
            independent_count=2, basis_digest=compute_basis_digest(c), reviewer="llm",
            claim_kind="intention", corroborated_by=[make_sp_ref(SCOPE, "p", DIGEST)],
        )
        ok, _ = inhibitor.process_review_patch(c.id, review)
        assert ok is True
        assert c.effective_review_status() == "supported"

    def test_review_patch_rejects_malformed_sp_ref(self, inhibitor, semantic):
        c = _coffee(semantic)
        review = EvidenceReview(
            status="supported", support_strength=0.95, cited_evidence_ids=["u1"],
            independent_count=2, basis_digest=compute_basis_digest(c), reviewer="llm",
            claim_kind="intention", corroborated_by=["sp:not-a-ref"],
        )
        ok, tx = inhibitor.process_review_patch(c.id, review)
        assert ok is False
        assert "sp:" in tx.patches[0].rejection_reason
        assert c.evidence_review == {}

    def test_effective_status_revalidates_sp_refs(self, inhibitor, semantic):
        c = _coffee(semantic)
        ref = make_sp_ref(SCOPE, "p", DIGEST)
        review = EvidenceReview(
            status="supported", support_strength=0.5, cited_evidence_ids=["u1"],
            independent_count=2, basis_digest=compute_basis_digest(c), reviewer="llm",
            claim_kind="intention", corroborated_by=[ref],
        )
        assert inhibitor.process_review_patch(c.id, review)[0]
        assert c.effective_review_status(sp_validator=lambda r: True) == "supported"
        assert c.effective_review_status(sp_validator=lambda r: False) == "insufficient"

        def boom(r):
            raise RuntimeError("shared layer down")

        assert c.effective_review_status(sp_validator=boom) == "insufficient"

    def test_unit_id_corroboration_survives_invalid_sp(self, inhibitor, semantic):
        c = _coffee(semantic)
        review = EvidenceReview(
            status="supported", support_strength=0.5, cited_evidence_ids=["u1"],
            independent_count=2, basis_digest=compute_basis_digest(c), reviewer="llm",
            claim_kind="intention", corroborated_by=["u7", make_sp_ref(SCOPE, "p", DIGEST)],
        )
        assert inhibitor.process_review_patch(c.id, review)[0]
        assert c.effective_review_status(sp_validator=lambda r: False) == "supported"


# ─── Gate：project ───────────────────────────────────────────────────────────


class TestShareProject:
    def test_project_applies_and_audits(self, inhibitor, semantic):
        ok, tx = _project(inhibitor, semantic)
        assert ok is True and tx.status == "committed"
        c = _coffee(semantic)
        entry = c.sharing["by_scope"][SCOPE]
        assert entry["level"] == LEVEL_SCOPE_SHARED
        assert entry["decided_by"] == "llm" and entry["pinned"] is False
        assert entry["cited_unit_ids"] == ["u1", "u2"]
        assert entry["projection_id"] == "proj_1"
        assert entry["patch_id"] == tx.patches[0].patch_id
        assert c.effective_sharing_level(SCOPE) == LEVEL_SCOPE_SHARED
        # 持久化
        reloaded = SemanticMemory(storage_path=semantic.storage_path)
        assert reloaded.get(c.id).effective_sharing_level(SCOPE) == LEVEL_SCOPE_SHARED
        # audit：approved 的預寫 + committed 的終態
        statuses = [a["patches"][0]["status"] for a in _audit(semantic)]
        assert statuses == ["approved", "applied"]
        patch = tx.patches[0]
        assert patch.action == "share" and patch.risk_level == "medium"
        assert patch.target_path == "sharing.by_scope"

    def test_project_does_not_change_description_or_digest(self, inhibitor, semantic):
        c = _coffee(semantic)
        before = compute_basis_digest(c)
        _project(inhibitor, semantic)
        assert compute_basis_digest(c) == before

    def test_audit_before_mutate(self, inhibitor, semantic, monkeypatch):
        monkeypatch.setattr(inhibitor, "_try_write_audit", lambda tx: False)
        ok, tx = _project(inhibitor, semantic)
        assert ok is False
        assert tx.patches[0].status == "rejected"
        assert "audit" in tx.patches[0].rejection_reason
        assert _coffee(semantic).sharing == {}

    def test_audit_failure_blocks_restrict_ledger_write(self, inhibitor, semantic, monkeypatch):
        monkeypatch.setattr(inhibitor, "_try_write_audit", lambda tx: False)
        ok, _ = inhibitor.process_share_patch(None, "restrict", None, {"unit_ids": ["u1"]})
        assert ok is False
        assert not inhibitor.privacy_ledger.is_restricted("u1")

    def test_audit_written_before_apply(self, inhibitor, semantic, monkeypatch):
        seen = {}
        orig = inhibitor._apply_share

        def spy(concept, patch):
            seen["audit_rows"] = len(_audit(semantic))
            return orig(concept, patch)

        monkeypatch.setattr(inhibitor, "_apply_share", spy)
        assert _project(inhibitor, semantic)[0]
        assert seen["audit_rows"] == 1

    @pytest.mark.parametrize("override,needle", [
        (dict(sensitivity="sensitive"), "sensitivity"),
        (dict(sensitivity="bogus"), "sensitivity"),
        (dict(supports_summary_from_cited_only=False), "摘要"),
        (dict(supports_summary_from_cited_only=None), "摘要"),
        (dict(cited_unit_ids=[]), "cited_unit_ids"),
        (dict(cited_unit_ids="u1"), "cited_unit_ids"),
        (dict(basis_digest="0" * 64), "stale"),
        (dict(summary_digest=""), "summary_digest"),
        (dict(projection_id=""), "projection_id"),
        (dict(projection_id="a b"), "projection_id"),
        (dict(cited_unit_ids=["ghost"]), "不存在"),
        (dict(decided_by="user"), "LLM"),
    ])
    def test_rejections(self, inhibitor, semantic, override, needle):
        ok, tx = _project(inhibitor, semantic, **override)
        assert ok is False
        assert needle in tx.patches[0].rejection_reason
        assert _coffee(semantic).sharing == {}

    def test_rejected_patch_is_audited(self, inhibitor, semantic):
        _project(inhibitor, semantic, sensitivity="sensitive")
        rows = _audit(semantic)
        assert len(rows) == 1 and rows[0]["patches"][0]["status"] == "rejected"

    def test_invalid_scope_rejected(self, inhibitor, semantic):
        c = _coffee(semantic)
        for bad in ("namespace", "", None, "guild:"):
            ok, _ = inhibitor.process_share_patch(c.id, "project", bad, _payload(c))
            assert ok is False
        assert c.sharing == {}

    def test_unknown_op_rejected(self, inhibitor, semantic):
        ok, tx = inhibitor.process_share_patch(_coffee(semantic).id, "public", SCOPE, {})
        assert ok is False and tx.patches[0].status == "rejected"

    def test_missing_concept_fails_without_audit(self, inhibitor, semantic):
        ok, tx = inhibitor.process_share_patch("con_nope", "project", SCOPE, {})
        assert ok is False and tx.status == "failed" and tx.patches == []
        assert _audit(semantic) == []

    def test_deprecated_concept_not_projected(self, inhibitor, semantic):
        c = _coffee(semantic)
        c.status = "deprecated"
        ok, tx = inhibitor.process_share_patch(c.id, "project", SCOPE, _payload(c))
        assert ok is False and "active" in tx.patches[0].rejection_reason

    def test_sharing_disabled(self, inhibitor, semantic, monkeypatch):
        monkeypatch.setattr(get_config(), "sharing_enabled", False)
        ok, tx = _project(inhibitor, semantic)
        assert ok is False and "停用" in tx.patches[0].rejection_reason

    def test_reproject_replaces_entry_and_rollback_restores(self, inhibitor, semantic):
        ok1, _ = _project(inhibitor, semantic)
        ok2, tx2 = _project(inhibitor, semantic, projection_id="proj_2", cited_unit_ids=["u1"])
        assert ok1 and ok2
        assert _coffee(semantic).sharing["by_scope"][SCOPE]["projection_id"] == "proj_2"
        inhibitor.rollback_share_patch(tx2)
        assert _coffee(semantic).sharing["by_scope"][SCOPE]["projection_id"] == "proj_1"
        assert tx2.patches[0].status == "rolled_back"

    def test_project_rollback_of_first_removes_entry(self, inhibitor, semantic):
        _, tx = _project(inhibitor, semantic)
        inhibitor.rollback_share_patch(tx)
        assert _coffee(semantic).sharing in ({}, {"by_scope": {}})
        assert _coffee(semantic).effective_sharing_level(SCOPE) == LEVEL_PRIVATE

    def test_project_rollback_does_not_override_later_restrict(self, inhibitor, semantic):
        _, tx = _project(inhibitor, semantic)
        c = _coffee(semantic)
        assert inhibitor.process_share_patch(c.id, "restrict", SCOPE, {"unit_ids": ["u1"]})[0]
        inhibitor.rollback_share_patch(tx)
        assert c.effective_sharing_level(SCOPE) == LEVEL_PRIVATE
        assert c.sharing["by_scope"][SCOPE]["pinned"] is True

    def test_project_apply_failure_rolls_back(self, inhibitor, semantic, monkeypatch):
        monkeypatch.setattr(semantic, "_save", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
        ok, tx = _project(inhibitor, semantic)
        assert ok is False and tx.status == "failed"
        assert _coffee(semantic).effective_sharing_level(SCOPE) == LEVEL_PRIVATE

    def test_stale_detection(self, inhibitor, semantic):
        c = _coffee(semantic)
        ok, tx = _project(inhibitor, semantic)
        patch = tx.patches[0]
        assert inhibitor._is_stale_patch(patch) is False
        c.description = "使用者其實更愛茶"
        assert inhibitor._is_stale_patch(patch) is True

    def test_share_patch_never_goes_through_generic_evaluate(self, inhibitor, semantic):
        ok, tx = _project(inhibitor, semantic)
        patch = tx.patches[0]
        patch.status = "pending"
        inhibitor._evaluate_patch(patch, "", "")
        assert patch.status == "rejected"


# ─── Golden S1：私密不外洩（資料面）──────────────────────────────────────────


class TestGoldenS1PrivateNeverShared:
    @pytest.mark.parametrize("name,unit", [
        ("dm", _unit("p1", channel="dm", audience=5)),
        ("solo", _unit("p1", audience=1)),
        ("no_attrs", _unit("p1", attrs={})),
        ("no_audience", _unit("p1", audience=None)),
        ("no_channel", _unit("p1", channel=None)),
        ("no_scope", _unit("p1", scope="namespace")),
        ("agent", _unit("p1", source="agent")),
    ])
    def test_private_origin_blocks_projection(self, semantic, name, unit):
        inh = ConceptInhibitor(
            semantic=semantic, llm_fn=None,
            evidence_lookup=lambda ids: [unit for i in ids if i == unit.id],
            privacy_ledger=PrivacyLedger(Path(semantic.storage_path).parent),
        )
        c = _coffee(semantic)
        ok, tx = inh.process_share_patch(
            c.id, "project", SCOPE, _payload(c, cited_unit_ids=["p1"]),
        )
        assert ok is False
        assert "scope_shared" in tx.patches[0].rejection_reason
        assert c.sharing == {}
        assert c.effective_sharing_level(SCOPE) == LEVEL_PRIVATE

    def test_mixed_cited_with_one_private_unit_rejected(self, semantic):
        pool = {"g": _unit("g"), "d": _unit("d", channel="dm", audience=1)}
        inh = ConceptInhibitor(
            semantic=semantic, llm_fn=None,
            evidence_lookup=lambda ids: [pool[i] for i in ids if i in pool],
            privacy_ledger=PrivacyLedger(Path(semantic.storage_path).parent),
        )
        c = _coffee(semantic)
        ok, _ = inh.process_share_patch(c.id, "project", SCOPE, _payload(c, cited_unit_ids=["g", "d"]))
        assert ok is False and c.sharing == {}

    def test_unit_from_other_scope_rejected(self, semantic):
        pool = {"other": _unit("other", scope="guild:999")}
        inh = ConceptInhibitor(
            semantic=semantic, llm_fn=None,
            evidence_lookup=lambda ids: [pool[i] for i in ids if i in pool],
            privacy_ledger=PrivacyLedger(Path(semantic.storage_path).parent),
        )
        c = _coffee(semantic)
        ok, _ = inh.process_share_patch(c.id, "project", SCOPE, _payload(c, cited_unit_ids=["other"]))
        assert ok is False and c.sharing == {}
        # 同一個 unit 對它自己的場域則可以
        ok2, _ = inh.process_share_patch(
            c.id, "project", "guild:999", _payload(c, cited_unit_ids=["other"]),
        )
        assert ok2 is True
        assert c.effective_sharing_level("guild:999") == LEVEL_SCOPE_SHARED
        assert c.effective_sharing_level(SCOPE) == LEVEL_PRIVATE


# ─── Gate：retract ───────────────────────────────────────────────────────────


class TestShareRetract:
    def test_retract_clears_projection_keeps_pinned(self, inhibitor, semantic):
        _project(inhibitor, semantic)
        c = _coffee(semantic)
        ok, tx = inhibitor.process_share_patch(
            c.id, "retract", SCOPE, {"reason_code": "modify", "decided_by": "llm"},
        )
        assert ok is True and tx.patches[0].risk_level == "low"
        entry = c.sharing["by_scope"][SCOPE]
        assert entry["level"] == LEVEL_PRIVATE and entry["projection_id"] == ""
        assert entry["decided_by"] == "llm"
        assert c.effective_sharing_level(SCOPE) == LEVEL_PRIVATE

    def test_retract_without_entry_is_noop_rejected(self, inhibitor, semantic):
        ok, tx = inhibitor.process_share_patch(_coffee(semantic).id, "retract", SCOPE, {})
        assert ok is False and tx.patches[0].rejection_reason.startswith("noop:")

    def test_retract_preserves_pinned(self, inhibitor, semantic):
        c = _coffee(semantic)
        inhibitor.process_share_patch(c.id, "restrict", SCOPE, {})
        ok, _ = inhibitor.process_share_patch(c.id, "retract", SCOPE, {})
        assert ok is True
        assert c.sharing["by_scope"][SCOPE]["pinned"] is True

    def test_retract_rollback_never_reexposes(self, inhibitor, semantic):
        _project(inhibitor, semantic)
        c = _coffee(semantic)
        _, tx = inhibitor.process_share_patch(c.id, "retract", SCOPE, {})
        inhibitor.rollback_share_patch(tx)
        assert c.effective_sharing_level(SCOPE) == LEVEL_PRIVATE

    def test_unknown_decided_by_falls_back_to_default(self, inhibitor, semantic):
        _project(inhibitor, semantic)
        c = _coffee(semantic)
        inhibitor.process_share_patch(c.id, "retract", SCOPE, {"decided_by": "root"})
        assert c.sharing["by_scope"][SCOPE]["decided_by"] == "default"


# ─── Gate：restrict ──────────────────────────────────────────────────────────


class TestShareRestrict:
    def test_restrict_pins_private_and_writes_ledger(self, inhibitor, semantic):
        _project(inhibitor, semantic)
        c = _coffee(semantic)
        ok, tx = inhibitor.process_share_patch(
            c.id, "restrict", SCOPE, {"unit_ids": ["u1"], "reason_code": "user_private"},
        )
        assert ok is True and tx.patches[0].risk_level == "low"
        entry = c.sharing["by_scope"][SCOPE]
        assert entry["level"] == LEVEL_PRIVATE and entry["pinned"] is True
        assert entry["decided_by"] == "user"
        assert entry["projection_id"] == "proj_1"  # 保留供共享層撤回
        assert inhibitor.privacy_ledger.is_restricted("u1")
        assert c.effective_sharing_level(SCOPE) == LEVEL_PRIVATE

    def test_restrict_creates_pinned_entry_when_none(self, inhibitor, semantic):
        c = _coffee(semantic)
        ok, _ = inhibitor.process_share_patch(c.id, "restrict", SCOPE, {})
        assert ok is True
        assert c.sharing["by_scope"][SCOPE]["pinned"] is True

    def test_restrict_all_scopes(self, inhibitor, semantic):
        c = _coffee(semantic)
        _project(inhibitor, semantic)
        inhibitor.process_share_patch(
            c.id, "project", "guild:999", _payload(c, cited_unit_ids=["u1"], projection_id="p9"),
        )  # u1 屬 guild:123，被拒 -> 只剩 guild:123
        inhibitor.process_share_patch(c.id, "restrict", "*", {})
        assert set(c.sharing["by_scope"]) == {SCOPE}
        assert c.sharing["by_scope"][SCOPE]["pinned"] is True

    def test_ledger_only_restrict(self, inhibitor, semantic):
        ok, tx = inhibitor.process_share_patch(None, "restrict", None, {"unit_ids": ["u1", "u2"]})
        assert ok is True
        patch = tx.patches[0]
        assert patch.concept_id is None and patch.target_path == "privacy_ledger"
        assert inhibitor.privacy_ledger.restricted_ids() == {"u1", "u2"}
        assert _audit(semantic)[-1]["patches"][0]["status"] == "applied"

    def test_restrict_requires_target(self, inhibitor):
        ok, tx = inhibitor.process_share_patch(None, "restrict", None, {})
        assert ok is False and "restrict" in tx.patches[0].rejection_reason

    def test_restrict_rejects_bad_unit_ids(self, inhibitor, semantic):
        for bad in ([""], [5], "u1"):
            ok, _ = inhibitor.process_share_patch(None, "restrict", None, {"unit_ids": bad})
            assert ok is False
        assert len(inhibitor.privacy_ledger) == 0

    def test_restrict_with_invalid_scope_rejected(self, inhibitor, semantic):
        ok, _ = inhibitor.process_share_patch(_coffee(semantic).id, "restrict", "bogus", {})
        assert ok is False

    def test_golden_s4_data_pinned_blocks_reproject(self, inhibitor, semantic):
        """S4 資料面：收緊後 Dream 不得再放寬；被收緊的 unit 也不能再被引用。"""
        _project(inhibitor, semantic)
        c = _coffee(semantic)
        inhibitor.process_share_patch(c.id, "restrict", SCOPE, {"unit_ids": ["u1"]})
        ok, tx = _project(inhibitor, semantic, projection_id="proj_2")
        assert ok is False and "pinned" in tx.patches[0].rejection_reason
        # 即使是另一個概念，引用已被收緊的 unit 也被拒（來源可見度為 private）
        other = semantic.get_by_name("討厭香菜")
        ok2, tx2 = inhibitor.process_share_patch(
            other.id, "project", SCOPE, _payload(other, cited_unit_ids=["u1"]),
        )
        assert ok2 is False and "scope_shared" in tx2.patches[0].rejection_reason
        # 只引用未被收緊的 u2 則可以
        ok3, _ = inhibitor.process_share_patch(
            other.id, "project", SCOPE, _payload(other, cited_unit_ids=["u2"], projection_id="p3"),
        )
        assert ok3 is True

    def test_restrict_units_cascade(self, inhibitor, semantic):
        _project(inhibitor, semantic)  # coffee cites u1,u2
        other = semantic.get_by_name("討厭香菜")
        inhibitor.process_share_patch(
            other.id, "project", SCOPE, _payload(other, cited_unit_ids=["u2"], projection_id="p3"),
        )
        results = inhibitor.restrict_units(["u1"], reason_code="user_private")
        assert all(ok for ok, _ in results) and len(results) == 2  # ledger + coffee
        assert inhibitor.privacy_ledger.is_restricted("u1")
        assert _coffee(semantic).sharing["by_scope"][SCOPE]["pinned"] is True
        # 不依賴 u1 的概念不受影響
        assert other.effective_sharing_level(SCOPE) == LEVEL_SCOPE_SHARED

    def test_restrict_units_empty(self, inhibitor):
        assert inhibitor.restrict_units([]) == []
        assert inhibitor.restrict_units(["", 5]) == []  # type: ignore[list-item]

    def test_restrict_rollback_removes_ledger_and_pin_without_reprojecting(self, inhibitor, semantic):
        _project(inhibitor, semantic)
        c = _coffee(semantic)
        _, tx = inhibitor.process_share_patch(c.id, "restrict", SCOPE, {"unit_ids": ["u1"]})
        inhibitor.rollback_share_patch(tx)
        assert not inhibitor.privacy_ledger.is_restricted("u1")
        entry = c.sharing["by_scope"][SCOPE]
        assert entry["pinned"] is False
        assert entry["level"] == LEVEL_PRIVATE  # 不自動重新投影
        assert c.effective_sharing_level(SCOPE) == LEVEL_PRIVATE
        assert tx.patches[0].status == "rolled_back" and tx.status == "rolled_back"

    def test_restrict_rollback_removes_entry_it_created(self, inhibitor, semantic):
        c = _coffee(semantic)
        _, tx = inhibitor.process_share_patch(c.id, "restrict", SCOPE, {})
        inhibitor.rollback_share_patch(tx)
        assert SCOPE not in (c.sharing or {}).get("by_scope", {})

    def test_restrict_rollback_keeps_preexisting_ledger_entries(self, inhibitor, semantic):
        inhibitor.process_share_patch(None, "restrict", None, {"unit_ids": ["u1"]})
        _, tx2 = inhibitor.process_share_patch(None, "restrict", None, {"unit_ids": ["u1", "u2"]})
        inhibitor.rollback_share_patch(tx2)
        assert inhibitor.privacy_ledger.is_restricted("u1")  # 先前覆寫不被誤刪
        assert not inhibitor.privacy_ledger.is_restricted("u2")

    def test_ledger_only_restrict_rollback(self, inhibitor):
        _, tx = inhibitor.process_share_patch(None, "restrict", None, {"unit_ids": ["u1"]})
        inhibitor.rollback_share_patch(tx)
        assert not inhibitor.privacy_ledger.is_restricted("u1")

    def test_restrict_works_even_if_sharing_disabled(self, inhibitor, semantic, monkeypatch):
        monkeypatch.setattr(get_config(), "sharing_enabled", False)
        ok, _ = inhibitor.process_share_patch(None, "restrict", None, {"unit_ids": ["u1"]})
        assert ok is True

    def test_restrict_survives_corrupt_ledger_as_failure_not_loosening(self, inhibitor, semantic):
        (Path(semantic.storage_path).parent / "privacy_ledger.json").write_text("{x", encoding="utf-8")
        ok, tx = inhibitor.process_share_patch(None, "restrict", None, {"unit_ids": ["u1"]})
        assert ok is False and tx.patches[0].status == "rejected"
        assert inhibitor.privacy_ledger.is_restricted("anything")  # 仍 fail-closed


# ─── Golden S9：fail-closed（資料面）──────────────────────────────────────────


class TestGoldenS9FailClosed:
    def test_evidence_lookup_failure_blocks_projection(self, semantic):
        def boom(ids):
            raise RuntimeError("evidence store down")

        inh = ConceptInhibitor(
            semantic=semantic, llm_fn=None, evidence_lookup=boom,
            privacy_ledger=PrivacyLedger(Path(semantic.storage_path).parent),
        )
        c = _coffee(semantic)
        ok, _ = inh.process_share_patch(c.id, "project", SCOPE, _payload(c))
        assert ok is False and c.sharing == {}

    def test_no_lookup_injected_empty_store_blocks_projection(self, semantic):
        inh = ConceptInhibitor(semantic=semantic, llm_fn=None)
        c = _coffee(semantic)
        ok, _ = inh.process_share_patch(c.id, "project", SCOPE, _payload(c))
        assert ok is False and c.sharing == {}

    def test_corrupt_ledger_blocks_projection(self, inhibitor, semantic):
        (Path(semantic.storage_path).parent / "privacy_ledger.json").write_text("garbage", encoding="utf-8")
        ok, tx = _project(inhibitor, semantic)
        assert ok is False and "scope_shared" in tx.patches[0].rejection_reason
        assert _coffee(semantic).sharing == {}

    def test_no_llm_needed_for_gate_but_no_llm_means_nothing_auto_projected(self, semantic, units):
        # Gate 本身規則層 approve；沒有任何呼叫端提供 payload 時什麼都不會被分享
        inh = ConceptInhibitor(
            semantic=semantic, llm_fn=None,
            evidence_lookup=lambda ids: [units[i] for i in ids if i in units],
            privacy_ledger=PrivacyLedger(Path(semantic.storage_path).parent),
        )
        for c in semantic.list_concepts(limit=None):
            assert c.effective_sharing_level(SCOPE) == LEVEL_PRIVATE
        ok, _ = inh.process_share_patch(_coffee(semantic).id, "project", SCOPE, {})
        assert ok is False

    def test_missing_payload_fields_rejected(self, inhibitor, semantic):
        c = _coffee(semantic)
        ok, _ = inhibitor.process_share_patch(c.id, "project", SCOPE, None)
        assert ok is False and c.sharing == {}

    def test_no_keyword_or_regex_in_sharing_modules(self):
        root = Path(__file__).resolve().parents[1] / "echo_memory"
        for rel in ("schemas/sharing.py", "storage/privacy_ledger.py"):
            text = (root / rel).read_text(encoding="utf-8")
            assert "import re\n" not in text and "re.compile" not in text
            assert "keyword" not in text.lower().replace("keyword／regex", "")


# ─── 概念變更監聽（共享投影即時撤回）─────────────────────────────────────────


def _gate_patch(concept, action, **kw):
    from echo_memory.affect.concept_patch import ConceptPatch, ExpandMode, PatchEvidence

    return ConceptPatch(
        transaction_id="tx_t", concept_id=concept.id, concept_name=concept.name,
        action=action, target_path=kw.pop("target_path", ""),
        evidence=PatchEvidence(
            evidence_kind="explicit_user_claim", evidence_strength=0.9,
            verifier_status="valid", verifier_reason="t",
        ),
        expand_mode=ExpandMode.ADD_FACT if action == "expand" else None,
        **kw,
    )


class TestChangeListener:
    @pytest.fixture
    def events(self, inhibitor):
        got: list[tuple[str, str]] = []
        inhibitor.register_change_listener(lambda cid, reason: got.append((cid, reason)))
        return got

    def test_expand_modify_deprecate_notify(self, inhibitor, semantic, events):
        c = _coffee(semantic)
        assert inhibitor._apply_patch(_gate_patch(
            c, "expand", target_path="metadata.facts", new_value={"text": "加奶"}))
        assert inhibitor._apply_patch(_gate_patch(
            c, "modify", target_path="description", old_value=c.description, new_value="新描述"))
        assert inhibitor._apply_patch(_gate_patch(
            c, "deprecate", old_value="active", new_value={"deprecated_reason": "過時"}))
        assert events == [(c.id, "expand"), (c.id, "modify"), (c.id, "deprecate")]

    def test_notified_after_save(self, inhibitor, semantic):
        c = _coffee(semantic)
        seen = {}

        def fn(cid, reason):
            seen["on_disk"] = SemanticMemory(storage_path=semantic.storage_path).get(cid).description

        inhibitor.register_change_listener(fn)
        inhibitor._apply_patch(_gate_patch(
            c, "modify", target_path="description", old_value=c.description, new_value="已存檔"))
        assert seen["on_disk"] == "已存檔"

    def test_not_notified_for_share_review_or_failure(self, inhibitor, semantic, events):
        c = _coffee(semantic)
        _project(inhibitor, semantic)
        inhibitor.process_share_patch(c.id, "restrict", SCOPE, {})
        assert events == []
        ghost = _gate_patch(c, "modify", new_value="x")
        ghost.concept_id = "con_ghost"
        assert inhibitor._apply_patch(ghost) is False
        assert events == []

    def test_rollback_notifies_once(self, inhibitor, semantic, events):
        c = _coffee(semantic)
        p = _gate_patch(c, "modify", target_path="description",
                        old_value=c.description, new_value="新描述", old_confidence=c.confidence)
        assert inhibitor._apply_patch(p)
        events.clear()
        inhibitor._rollback_patch(p)
        assert events == [(c.id, "rollback:modify")]

    def test_listener_exception_does_not_affect_patch(self, inhibitor, semantic):
        c = _coffee(semantic)

        def boom(cid, reason):
            raise RuntimeError("revoke failed")

        inhibitor.register_change_listener(boom)
        good: list = []
        inhibitor.register_change_listener(lambda cid, r: good.append(cid))
        assert inhibitor._apply_patch(_gate_patch(
            c, "modify", target_path="description", old_value=c.description, new_value="新描述"))
        assert c.description == "新描述" and good == [c.id]

    def test_register_dedup_and_unregister(self, inhibitor, semantic):
        c = _coffee(semantic)
        got: list = []

        def fn(cid, r):
            got.append(cid)

        inhibitor.register_change_listener(fn)
        inhibitor.register_change_listener(fn)
        inhibitor._apply_patch(_gate_patch(c, "expand", target_path="metadata.facts", new_value={"text": "a"}))
        assert got == [c.id]
        inhibitor.unregister_change_listener(fn)
        inhibitor._apply_patch(_gate_patch(c, "expand", target_path="metadata.facts", new_value={"text": "b"}))
        assert got == [c.id]

    def test_expand_source_notifies_only_when_changed(self, inhibitor, semantic, events):
        c = _coffee(semantic)
        p = _gate_patch(c, "expand", target_path="source_episode_ids", new_value="ep9")
        assert inhibitor._apply_expand_source(p)
        assert events == [(c.id, "expand")]
        events.clear()
        assert inhibitor._apply_expand_source(
            _gate_patch(c, "expand", target_path="source_episode_ids", new_value="ep9"))
        assert events == []

    def test_listener_not_called_while_holding_semantic_lock(self, inhibitor, semantic):
        c = _coffee(semantic)
        held = {}

        def fn(cid, r):
            # 另一執行緒若能拿到鎖，代表通知時本執行緒已釋放
            import threading
            ok = []
            t = threading.Thread(target=lambda: ok.append(semantic._lock.acquire(timeout=1)))
            t.start()
            t.join()
            held["free"] = bool(ok and ok[0])
            if held["free"]:
                semantic._lock.release()

        inhibitor.register_change_listener(fn)
        inhibitor._apply_patch(_gate_patch(
            c, "modify", target_path="description", old_value=c.description, new_value="n"))
        assert held["free"] is True

    def test_merge_notifies_primary_and_secondary(self, inhibitor, semantic, events):
        a, b = _coffee(semantic), semantic.get_by_name("討厭香菜")
        ok, result = inhibitor.process_reflection_patch(
            "merge", a.name, {"merge_with": b.name}, "test")
        assert ok, result
        assert {(a.id, "merge"), (b.id, "merge")} <= set(events)

    def test_same_name_create_overwriting_projected_concept_notifies_once(
        self, inhibitor, semantic, events,
    ):
        assert _project(inhibitor, semantic)[0]
        c = _coffee(semantic)
        events.clear()
        p = _gate_patch(
            c, "create", new_value={"name": c.name, "description": "被覆寫的新描述", "episode_id": "ep9"},
        )
        ok, cid = inhibitor._apply_create(p)
        assert ok and cid == c.id
        assert events == [(c.id, "create")]
        # 描述已變 -> 投影綁定的 digest 失效，讀取端也判 private
        assert c.effective_sharing_level(SCOPE) == LEVEL_PRIVATE

    def test_new_create_notifies_and_rollback_notifies(self, inhibitor, semantic, events):
        from echo_memory.affect.concept_patch import ConceptPatch, PatchEvidence

        p = ConceptPatch(
            transaction_id="tx_c", concept_name="全新概念", action="create",
            evidence=PatchEvidence(
                evidence_kind="explicit_user_claim", evidence_strength=0.9,
                verifier_status="valid", verifier_reason="t",
            ),
            new_value={"name": "全新概念", "description": "d", "episode_id": "ep1"},
        )
        ok, cid = inhibitor._apply_create(p, embedding=[1.0, 0.0])
        assert ok and events == [(cid, "create")]
        events.clear()
        p.concept_id = cid
        inhibitor._rollback_patch(p)
        assert events == [(cid, "rollback:create")]


# ─── guild id 純數字與小寫正規化 ─────────────────────────────────────────────


class TestGuildIdAndNormalization:
    @pytest.mark.parametrize("bad", ["abc", "12a", "1_2", "1-2", "٣٤"])
    def test_non_digit_ids_rejected(self, bad):
        assert parse_guild_scope(f"guild:{bad}") is None
        with pytest.raises(ValueError):
            guild_scope(bad)

    def test_snowflake_ok(self):
        assert guild_scope("123456789012345678") == "guild:123456789012345678"

    def test_scope_and_namespace_lowercased(self):
        from echo_memory.schemas.sharing import normalize_scope

        assert normalize_scope("GUILD:123") == "guild:123"
        assert parse_guild_scope(" Guild:123 ") == "123"
        assert scope_to_namespace_id("GUILD:123") == "scope-g123"
        assert namespace_to_scope("SCOPE-G123") == "guild:123"
        assert normalize_scope("guild:x") is None

    def test_unit_scope_uppercase_prefix_normalized(self):
        assert origin_scope(_unit(scope="GUILD:123"), _Ledger()) == "guild:123"

    def test_sharing_state_keys_normalized(self):
        state = SharingState.from_dict({"by_scope": {"GUILD:5": {"level": "scope_shared"}}})
        assert list(state.by_scope) == ["guild:5"]

    def test_gate_normalizes_scope_id(self, inhibitor, semantic):
        c = _coffee(semantic)
        ok, _ = inhibitor.process_share_patch(c.id, "project", "GUILD:123", _payload(c))
        assert ok is True
        assert list(c.sharing["by_scope"]) == ["guild:123"]


# ─── PrivacyLedger 跨行程鎖 ───────────────────────────────────────────────────


def _ledger_worker(path: str, prefix: str, n: int) -> None:
    led = PrivacyLedger(path)
    for i in range(n):
        led.restrict(f"{prefix}{i}", patch_id=prefix)


class TestPrivacyLedgerCrossProcess:
    def test_concurrent_processes_do_not_lose_restrictions(self, tmp_path):
        import multiprocessing as mp

        ctx = mp.get_context("spawn")
        procs = [ctx.Process(target=_ledger_worker, args=(str(tmp_path), p, 15)) for p in ("a", "b", "c")]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=120)
            assert p.exitcode == 0
        led = PrivacyLedger(tmp_path)
        assert len(led) == 45
        assert (tmp_path / ".privacy_ledger.lock").exists()

    def test_reentrant_guard_and_stale_cache_refresh(self, tmp_path):
        a, b = PrivacyLedger(tmp_path), PrivacyLedger(tmp_path)
        a.restrict("u1")
        b.restrict("u2")  # b 的快取尚未見過 u1：取得鎖後必須重讀
        assert PrivacyLedger(tmp_path).restricted_ids() == {"u1", "u2"}
        with a._write_guard():
            with a._write_guard():
                a.restrict("u3")
        assert a.is_restricted("u3")
