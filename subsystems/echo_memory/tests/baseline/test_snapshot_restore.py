"""EM-001：可還原的測試環境 + Recall 基線 golden。

更新 golden（顯式）：
    UPDATE_GOLDEN=1 python -m pytest tests/baseline/test_snapshot_restore.py -q
"""

from __future__ import annotations

import json
import os

import pytest

from tests.baseline._support import (
    GOLDEN_PATH,
    SNAPSHOT_DIR,
    copy_from_data_dir,
    directed_edges,
    edge_pairs,
    tree_digest,
    tx_audit,
)

MANIFEST_PATH = SNAPSHOT_DIR.parent / "snapshot_v1.manifest.json"


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


# ─── 還原完整性 ──────────────────────────────────────────────────────────────


def test_restore_gives_engine_matching_manifest(restored_engine):
    m = _manifest()
    assert sorted(restored_engine.episodic._episodes) == m["episode_ids"]
    assert restored_engine.episodic.count() == len(m["episode_ids"])
    assert sorted(c.name for c in restored_engine.semantic.list_concepts(limit=None)) == m["concept_names"]
    assert restored_engine.semantic.count() == len(m["concept_names"])


def test_restore_has_relation_graph(restored_engine):
    m = _manifest()
    assert len(edge_pairs(restored_engine, "RELATES_TO")) == m["relates_to_edges"]
    assert len(directed_edges(restored_engine, "DERIVED_FROM")) == m["derived_from_edges"]
    # DERIVED_FROM 的終點必須是快照內的 Episode
    ep_ids = set(m["episode_ids"])
    assert all(dst in ep_ids for _src, dst in directed_edges(restored_engine, "DERIVED_FROM"))


def test_restore_has_audit_and_patch_history(restored_engine):
    m = _manifest()
    audit = tx_audit(restored_engine)
    assert len(audit) == m["audit_tx_count"]
    held = [p for tx in audit for p in tx["patches"] if p["status"] == "held"]
    assert len(held) == m["held_patch_count"] == 1

    coffee = restored_engine.semantic.get_by_name("手沖咖啡")
    assert coffee.metadata.get("facts"), "expand patch 應留下 fact"
    assert coffee.metadata.get("patch_history"), "應有 patch_history 索引"
    tsukuba = restored_engine.semantic.get_by_name("筑波大學")
    assert tsukuba.description == "使用者妹妹考上並將就讀的日本大學"
    # held 的 modify 不得改動概念
    taipei = restored_engine.semantic.get_by_name("台北工作")
    assert taipei.description == "使用者在台北的軟體公司上班"
    assert not taipei.metadata.get("patch_history")


def test_restores_are_independent_and_do_not_touch_snapshot(engine_factory):
    before = tree_digest(SNAPSHOT_DIR)
    e1 = engine_factory()
    e2 = engine_factory()
    assert e1.storage_path != e2.storage_path

    e1.semantic.upsert_concept("只在 e1", "寫入 e1 的概念")
    e1.episodic.clear()

    assert e2.semantic.get_by_name("只在 e1") is None
    assert e2.episodic.count() == len(_manifest()["episode_ids"])
    # 快照目錄逐檔雜湊不變
    assert tree_digest(SNAPSHOT_DIR) == before


def test_restore_survives_engine_reopen(restored_dir):
    """還原目錄寫入後重開 engine，資料持久化一致。"""
    from echo_memory.engine import MemoryEngine

    e1 = MemoryEngine(storage_path=restored_dir)
    e1.semantic.upsert_concept("重開測試", "重開後仍存在")
    e2 = MemoryEngine(storage_path=restored_dir)
    assert e2.semantic.get_by_name("重開測試") is not None
    assert e2.episodic.count() == len(_manifest()["episode_ids"])


# ─── 快照產生工具 ────────────────────────────────────────────────────────────


def test_generator_is_reproducible(tmp_path):
    """固定腳本重跑，應得到與已提交快照相同的內容摘要。"""
    from tests.baseline.make_snapshot import build_snapshot

    manifest = build_snapshot(tmp_path / "regen")
    assert manifest == _manifest()


def test_copy_from_data_dir_never_modifies_source(tmp_path):
    src = tmp_path / "real_data"
    src.mkdir()
    (src / "a.json").write_text('{"k": 1}', encoding="utf-8")
    (src / "sub").mkdir()
    (src / "sub" / "b.txt").write_text("資料", encoding="utf-8")
    (src / ".echo-memory.lock").write_text("", encoding="utf-8")
    before = tree_digest(src)

    dest = copy_from_data_dir(src, tmp_path / "snap")
    assert (dest / "a.json").read_text(encoding="utf-8") == '{"k": 1}'
    assert not (dest / ".echo-memory.lock").exists(), "鎖檔不應被複製"
    (dest / "a.json").write_text("changed", encoding="utf-8")
    assert tree_digest(src) == before


def test_copy_from_data_dir_rejects_nested_destination(tmp_path):
    src = tmp_path / "real_data"
    src.mkdir()
    with pytest.raises(ValueError):
        copy_from_data_dir(src, src / "inside")


# ─── Recall 基線 golden ──────────────────────────────────────────────────────

# 每筆：(名稱, query, top_k, min_similarity, exclude_episode_ids)
# 精確文字查詢 -> 雜湊 embedding 的 cosine = 1.0；permissive（min_similarity=-1）
# 讓排名由 sim/recency/salience 融合決定，才能反映排序行為。
RECALL_QUERIES = [
    ("exact_coffee_episode", "使用者迷上手沖咖啡", 5, 0.3, None),
    ("exact_cat_episode", "使用者養了一隻叫小橘的貓", 5, 0.3, None),
    ("exact_sister_episode", "使用者的妹妹考上筑波大學", 5, 0.3, None),
    ("exact_concept_text", "筑波大學: 使用者妹妹考上並將就讀的日本大學", 5, 0.3, None),
    ("permissive_work", "工作", 5, -1.0, None),
    ("permissive_sleep", "睡眠作息", 6, -1.0, None),
    ("permissive_with_exclusion", "工作", 5, -1.0, {"ep_base_005", "ep_base_001"}),
    ("no_match", "完全無關的量子力學", 5, 0.3, None),
]


def _compute_recall(engine) -> dict:
    result = {}
    for name, query, top_k, min_sim, exclude in RECALL_QUERIES:
        ctx = engine.retrieve(
            query,
            dopamine=0.5, serotonin=0.5,  # 不吃 neurochem 狀態
            top_k=top_k, min_similarity=min_sim,
            exclude_episode_ids=exclude,
        )
        result[name] = {
            "query": query,
            "top_k": top_k,
            "min_similarity": min_sim,
            "episode_ids": [ep.id for ep in ctx.episodes],  # 保留排序
            "concept_names": sorted(c.name for c in ctx.concepts),
        }
    return result


def test_recall_matches_golden(restored_engine, frozen_retrieval_clock):
    actual = _compute_recall(restored_engine)

    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(
            json.dumps(actual, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        pytest.skip(f"已更新 golden: {GOLDEN_PATH}")

    assert GOLDEN_PATH.exists(), "缺少 golden；請以 UPDATE_GOLDEN=1 產生"
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert set(actual) == set(golden), "查詢集與 golden 不一致"
    for name, expected in golden.items():
        assert actual[name]["episode_ids"] == expected["episode_ids"], f"{name}: Episode 排序改變"
        assert actual[name]["concept_names"] == expected["concept_names"], f"{name}: Concept 集合改變"


def test_golden_is_not_degenerate():
    """golden 本身要有鑑別力：不能全空，且 permissive 查詢要有多筆排序結果。"""
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert golden["no_match"]["episode_ids"] == []
    assert golden["exact_coffee_episode"]["episode_ids"][0] == "ep_base_001"
    assert "筑波大學" in golden["exact_concept_text"]["concept_names"]  # 另含 RELATES_TO 1-hop 展開
    assert len(golden["permissive_work"]["episode_ids"]) >= 4
    assert golden["permissive_with_exclusion"]["episode_ids"]
    assert not {"ep_base_005", "ep_base_001"} & set(golden["permissive_with_exclusion"]["episode_ids"])


def test_recall_is_deterministic_across_restores(engine_factory, frozen_retrieval_clock):
    assert _compute_recall(engine_factory()) == _compute_recall(engine_factory())
