"""決策中樞行為基線評測集：golden（現況行為快照）逐筆比對，以及資料檔規格檢查（無 DB、離線）。"""
from __future__ import annotations

import json

import pytest

from tests.uep import decision_eval_runner as runner

_HINT = (
    "規則行為改變 -> 先確認是預期的變更，再依 tests/uep/fixtures/decision_eval/README.md 的流程重產 golden；"
    "不可直接覆蓋 golden。"
)


@pytest.fixture(scope="module")
def candidates():
    return runner.read_jsonl_file(runner.CANDIDATES_PATH)


@pytest.fixture(scope="module")
def golden():
    return runner.read_jsonl_file(runner.GOLDEN_PATH)


def test_golden_matches_current_behavior(candidates, golden):
    actual = runner.run_eval(candidates)
    mismatched: list[str] = []
    for record in golden:
        for pid in runner.PROPOSER_IDS:
            expected = (record[pid]["outcome"], record[pid]["reason_code"])
            if actual[pid].get(record["id"]) != expected:
                mismatched.append(f"{record['id']}[{pid}] golden={expected} actual={actual[pid].get(record['id'])}")
    assert not mismatched, f"{len(mismatched)} 筆不一致：\n" + "\n".join(mismatched) + f"\n{_HINT}"


def test_golden_covers_exactly_the_candidates(candidates, golden):
    cand_ids = [c["id"] for c in candidates]
    gold_ids = [g["id"] for g in golden]
    assert gold_ids == cand_ids, f"candidates 與 golden 的 id 不一致（新增或修改候選後需重產）。{_HINT}"


def test_golden_meta_records_version_and_commit(golden):
    meta = json.loads(runner.META_PATH.read_text(encoding="utf-8"))
    assert meta["records"] == len(golden)
    assert meta["commit"] and meta["commit"] != "unknown"
    assert meta["proposers"] == runner.engine_versions(), f"proposer 版本字串已變動。{_HINT}"


def test_candidate_fields_follow_spec(candidates):
    for item in candidates:
        missing = [f for f in runner.REQUIRED_FIELDS if f not in item]
        assert not missing, f"{item.get('id')} 缺欄位 {missing}"
        assert set(item["ctx"]) == set(runner.CTX_FIELDS), f"{item['id']} ctx 欄位與 _CTX_FIELDS 不符"
        assert isinstance(item["utterance_refs"], list) and isinstance(item["speakers"], list)
        assert isinstance(item["text"], str) and isinstance(item["presence_active"], bool)
        assert isinstance(item["addressed_hint"], bool) and isinstance(item["segments"], int)
        assert item["participant_count"] is None or isinstance(item["participant_count"], int)
        assert item["lang"] in {"en", "zh"}


def test_ids_are_unique(candidates):
    ids = [c["id"] for c in candidates]
    assert len(ids) == len(set(ids))
    cids = [c["candidate_id"] for c in candidates]
    assert len(cids) == len(set(cids))


def test_both_languages_present_and_sized(candidates):
    langs = {c["lang"] for c in candidates}
    assert langs == {"en", "zh"}
    assert 30 <= len(candidates) <= 60


def test_golden_covers_main_rule_branches(golden):
    reasons = {g["rules.default"]["reason_code"] for g in golden}
    assert {"called", "active", "observe", "not_meaningful", "tentative_unconfirmed"} <= reasons
