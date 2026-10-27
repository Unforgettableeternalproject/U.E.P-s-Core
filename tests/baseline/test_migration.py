"""EM-008：既有資料遷移與舊版檢索相容。

用 EM-001 快照（未含 evidence 的舊格式）還原一份資料，驗證 migrate_evidence、
遷移前後 Episode 檢索排序與 golden 一致，以及遷移 CLI 的 dry-run／apply。
只在 tmp 目錄操作，不碰真實 data/。
"""

from __future__ import annotations

import json

import pytest

from echo_memory.engine import MemoryEngine
from echo_memory.schemas.evidence import can_attest_agent_said, can_attest_user_claim
from echo_memory.storage.evidence import migrate_evidence
from tests.baseline._support import GOLDEN_PATH, SNAPSHOT_DIR, tree_digest
from tests.baseline.test_snapshot_restore import _compute_recall, _manifest


def _digest(root):
    """內容雜湊，忽略 storage_lock 產生的鎖檔。"""
    return {k: v for k, v in tree_digest(root).items() if not k.endswith(".lock")}


def _episodes_json(root):
    return json.loads((root / "episodic" / "episodes.json").read_text(encoding="utf-8"))


def test_snapshot_is_pre_evidence_format():
    """前提：快照是舊格式，遷移才有意義。"""
    assert not (SNAPSHOT_DIR / "evidence").exists()
    assert all(not ep.get("evidence_ids") for ep in _episodes_json(SNAPSHOT_DIR))


def test_migrate_snapshot_integration(restored_dir):
    n = len(_manifest()["episode_ids"])
    before_eps = _episodes_json(restored_dir)
    original = (restored_dir / "episodic" / "episodes.json").read_bytes()

    report = migrate_evidence(restored_dir)

    # 備份存在且是遷移前原樣
    assert report.backup_path and report.backup_path.exists()
    assert report.backup_path.read_bytes() == original
    # episode 數不變、每個都有 evidence_ids
    after_eps = _episodes_json(restored_dir)
    assert len(after_eps) == len(before_eps) == n
    assert report.episodes_total == n and report.episodes_migrated == n
    assert all(ep["evidence_ids"] for ep in after_eps)
    # 原欄位不被改動（只新增 evidence_ids／episode_kind）
    for old, new in zip(before_eps, after_eps):
        for key, value in old.items():
            if key not in ("evidence_ids", "episode_kind"):
                assert new[key] == value, key

    eng = MemoryEngine(storage_path=restored_dir)
    all_units = []
    for ep in eng.episodic._episodes.values():
        units = eng.evidence.get(ep.evidence_ids)
        assert len(units) == len(ep.evidence_ids) >= 1
        all_units.extend(units)
    assert report.units_added == len(all_units) == eng.evidence.count()
    assert {u.provenance_status for u in all_units} == {"legacy_unknown"}
    assert not any(can_attest_user_claim(u) or can_attest_agent_said(u) for u in all_units)
    # legacy agent 的 delivery_state 只降不升
    assert {u.delivery_state for u in all_units if u.source_type == "agent"} <= {"unknown", "partial"}

    # 重跑：零變化、無新備份
    digest = _digest(restored_dir)
    again = migrate_evidence(restored_dir)
    assert again.episodes_migrated == 0 and again.units_added == 0 and again.backup_path is None
    assert _digest(restored_dir) == digest


def test_engine_loads_and_writes_after_migration(restored_dir):
    migrate_evidence(restored_dir)
    eng = MemoryEngine(storage_path=restored_dir)
    n = len(_manifest()["episode_ids"])
    assert eng.episodic.count() == n

    new_id = eng.store_episode("遷移後的新輸入", "遷移後的新回覆", "sess_new")
    assert eng.episodic.count() == n + 1
    # 重開仍一致，且舊 Episode 證據仍可解析
    eng2 = MemoryEngine(storage_path=restored_dir)
    assert eng2.episodic.get(new_id) is not None
    assert all(eng2.get_episode_evidence(eid) for eid in eng2.episodic._episodes)
    first = eng2.retrieve_evidence(episode_ids=["ep_base_001"], source_types=["user"])[0]
    assert first.content == "我最近迷上手沖咖啡"


def test_retrieval_order_unchanged_by_migration(engine_factory, frozen_retrieval_clock):
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    pre = engine_factory()
    before = _compute_recall(pre)

    post = engine_factory()
    migrate_evidence(post.storage_path)
    post_engine = MemoryEngine(storage_path=post.storage_path)
    after = _compute_recall(post_engine)

    for name, expected in golden.items():
        assert before[name]["episode_ids"] == expected["episode_ids"], f"{name}: 遷移前偏離 golden"
        assert after[name]["episode_ids"] == before[name]["episode_ids"], f"{name}: 遷移改變 Episode 排序"
        assert after[name]["concept_names"] == before[name]["concept_names"], f"{name}: 遷移改變 Concept"


def test_migration_does_not_touch_vector_and_graph_files(restored_dir):
    keep = ["embedding_index.json", "embeddings.npy", "memory_graph.gpickle"]
    before = {k: (restored_dir / "episodic" / k).read_bytes() for k in keep}
    migrate_evidence(restored_dir)
    assert {k: (restored_dir / "episodic" / k).read_bytes() for k in keep} == before


# ─── CLI ────────────────────────────────────────────────────────


def test_cli_defaults_to_dry_run(restored_dir, capsys):
    from echo_memory.tools.migrate_evidence import main

    digest = _digest(restored_dir)
    assert main([str(restored_dir)]) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out and "--apply" in out
    assert _digest(restored_dir) == digest  # 完全沒寫入、沒備份


def test_cli_apply_migrates_then_noops(restored_dir, capsys):
    from echo_memory.tools.migrate_evidence import main

    assert main([str(restored_dir), "--apply"]) == 0
    assert all(ep["evidence_ids"] for ep in _episodes_json(restored_dir))
    assert list((restored_dir / "episodic").glob("episodes.json.pre-evidence-*.bak"))
    capsys.readouterr()
    digest = _digest(restored_dir)
    assert main([str(restored_dir), "--apply"]) == 0
    assert _digest(restored_dir) == digest


def test_cli_rejects_missing_dir_and_both_flags(tmp_path):
    from echo_memory.tools.migrate_evidence import main

    assert main([str(tmp_path / "nope")]) == 2
    assert main([str(tmp_path)]) == 2  # 沒有 episodes.json
    with pytest.raises(SystemExit):
        main([str(tmp_path), "--dry-run", "--apply"])
