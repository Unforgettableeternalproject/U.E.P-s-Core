"""決策中樞行為基線評測集：golden（現況行為快照）逐筆比對，以及資料檔規格檢查（無 DB、離線）。"""
from __future__ import annotations

import json

import pytest

from tests.uep import decision_eval_runner as runner
from tests.uep import decision_eval_timed_runner as timed

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
    assert 30 <= len(candidates) <= 70


def test_golden_covers_main_rule_branches(golden):
    reasons = {g["rules.default"]["reason_code"] for g in golden}
    assert {"called", "active", "observe", "not_meaningful", "tentative_unconfirmed"} <= reasons



# ---- DL-2b：時間相關情境（假 clock 回放；規格見 README 的 timed 段落）----

_TIMED_HINT = (
    "時間相關規則行為改變 -> 先確認是預期的變更，再依 README 的流程重產 timed_golden.jsonl；不可直接覆蓋 golden。"
)
# 每種情境（scenario 名稱去掉語言前綴）中「因 advance 推進時間而改變」的事件：(id 後綴, 有 advance 的結果, 不 advance 的對照結果)
# tick 的結果為插話 reason_code，沒有插話為 None
_TIME_DRIVEN = {
    "silence": ("06", "interject_silence", None),
    "interject-context": ("13", "interject_context", None),
    "context": ("07", "active", "context"),
    "context-passive": ("07", "observe", "context_passive"),
    "barge-cooldown": ("11", "context_passive", "barge_in_cooldown"),
}


@pytest.fixture(scope="module")
def timed_scripts():
    return timed.read_scripts()


@pytest.fixture(scope="module")
def timed_golden():
    return timed.read_golden()


def _result_reason(row):
    """candidate 事件取 reason_code；tick 事件取第一個插話的 reason_code（沒有插話為 None）。"""
    if row["type"] == "candidate":
        return row["reason_code"]
    return row["interjections"][0]["reason_code"] if row["interjections"] else None


def test_timed_golden_matches_current_behavior(timed_scripts, timed_golden):
    actual = {row["id"]: row for rows in timed.run_timed(timed_scripts).values() for row in rows}
    mismatched = [
        f"{row['id']} golden={row} actual={actual.get(row['id'])}" for row in timed_golden if actual.get(row["id"]) != row
    ]
    assert not mismatched, f"{len(mismatched)} 筆不一致：\n" + "\n".join(mismatched) + f"\n{_TIMED_HINT}"


def test_timed_golden_covers_exactly_the_result_events(timed_scripts, timed_golden):
    expected = [e["id"] for e in timed_scripts if e["type"] in {"candidate", "tick"}]
    assert sorted(g["id"] for g in timed_golden) == sorted(expected), f"事件腳本與 timed golden 的 id 不一致。{_TIMED_HINT}"


def test_timed_golden_meta_records_version_and_commit(timed_golden):
    meta = json.loads(runner.META_PATH.read_text(encoding="utf-8"))
    assert meta["timed"]["records"] == len(timed_golden)
    assert meta["timed"]["commit"] and meta["timed"]["commit"] != "unknown"
    assert meta["timed"]["proposers"] == {"rules.default": runner.engine_versions()["rules.default"]}, _TIMED_HINT


def test_timed_script_fields_follow_spec(timed_scripts):
    ids = [e["id"] for e in timed_scripts]
    assert len(ids) == len(set(ids))
    for event in timed_scripts:
        missing = [f for f in timed.BASE_FIELDS if f not in event]
        assert not missing, f"{event.get('id')} 缺欄位 {missing}"
        assert event["type"] in timed.EVENT_TYPES and event["lang"] in {"en", "zh"}
        assert event["id"].startswith(event["scenario"] + "-")
        if event["type"] == "candidate":
            missing = [f for f in runner.REQUIRED_FIELDS if f not in event]
            assert not missing, f"{event['id']} 缺候選欄位 {missing}"
            assert set(event["ctx"]) == set(runner.CTX_FIELDS)
        elif event["type"] == "advance":
            assert isinstance(event["seconds"], int | float) and event["seconds"] > 0
        elif event["type"] in {"turn_started", "turn_outcome"}:
            assert {"turn_id", "candidate_id", "speakers"} <= set(event)
    cids = [e["candidate_id"] for e in timed_scripts if e["type"] == "candidate"]
    assert len(cids) == len(set(cids))
    # 與 DL-2a 的候選不得 id 衝突
    assert not set(ids) & {c["id"] for c in runner.read_jsonl_file(runner.CANDIDATES_PATH)}


def test_timed_scenarios_cover_all_branches_in_both_languages(timed_scripts, timed_golden):
    reasons = {_result_reason(g) for g in timed_golden}
    assert {
        "interject_silence", "interject_context", "context", "context_passive", "barge_in_cooldown",
    } <= reasons
    scenarios = {e["scenario"]: e["lang"] for e in timed_scripts}
    for kind in _TIME_DRIVEN:
        assert {scenarios.get(f"timed-{lang}-{kind}") for lang in ("en", "zh")} == {"en", "zh"}, kind


@pytest.mark.parametrize("lang", ["en", "zh"])
@pytest.mark.parametrize("kind", sorted(_TIME_DRIVEN))
def test_result_is_driven_by_advancing_time(timed_scripts, lang, kind):
    """同一序列：有 advance 與不 advance（clock 凍結）的結果在指定事件上必須不同，證明是時間驅動。"""
    name = f"timed-{lang}-{kind}"
    suffix, with_advance, without_advance = _TIME_DRIVEN[kind]
    events = timed.group_by_scenario(timed_scripts)[name]
    assert any(e["type"] == "advance" for e in events)
    target = f"{name}-{suffix}"
    real = {r["id"]: r for r in timed.replay_scenario(events)}
    frozen = {r["id"]: r for r in timed.replay_scenario(events, skip_advance=True)}
    assert _result_reason(real[target]) == with_advance, f"{target}（有 advance）\n{_TIMED_HINT}"
    assert _result_reason(frozen[target]) == without_advance, f"{target}（無 advance 對照）\n{_TIMED_HINT}"
    assert real[target] != frozen[target]


def test_timed_replay_is_deterministic_and_scenarios_are_isolated(timed_scripts):
    first = timed.run_timed(timed_scripts)
    assert timed.run_timed(timed_scripts) == first
    # 單獨回放某一組與整檔回放的結果一致 -> 組與組之間不共用 proposer／clock 狀態
    for name, events in timed.group_by_scenario(timed_scripts).items():
        assert timed.replay_scenario(events) == first[name]
