"""UserPromptSubmit 注入 hook 的測試。

重點在這條路獨有的兩個弱點：
query 品質（大量輸入根本沒有主題訊號）與**分數的尺度**
（BM25 的 idf 依賴池子大小，門檻沒處理好會讓小 scope 永遠不觸發）。

執行：``python -m pytest agent_memory_spike/test_prompt_inject.py -q``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import hook_pretooluse  # noqa: E402
import hook_userpromptsubmit  # noqa: E402
from hook_userpromptsubmit import INJECT_TOP_K, run, select  # noqa: E402
from transcript import build_episode, load_injections, prompt_fingerprint  # noqa: E402

# 與 concept 的 cue 明確同主題的句子，長度也過得了 MIN_QUERY_CHARS
QUERY = "多租戶反查 users 的時候要注意什麼，我這邊拿到的資料好像混到別家公司了"


def _concept(cid, scope="proj", cue="多租戶 反查 users 要帶 companyid 避免跨公司資料外洩"):
    return {"id": cid, "statement": f"statement {cid}", "anchors": [],
            "surprisal": 1.0, "scope": scope, "cue": cue}


def _isolate(tmp_path, monkeypatch, concepts):
    concept_path = tmp_path / "concepts.json"
    concept_path.write_text(json.dumps(concepts), encoding="utf-8")
    for module in (hook_pretooluse, hook_userpromptsubmit):
        monkeypatch.setattr(module, "CONCEPT_PATH", concept_path, raising=False)
        monkeypatch.setattr(module, "INJECTION_LOG", tmp_path / "injections.jsonl",
                            raising=False)
    monkeypatch.setattr(hook_pretooluse, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(hook_userpromptsubmit, "repo_root_name", lambda _cwd: "proj")
    return tmp_path / "injections.jsonl"


def _payload(**kwargs):
    base = {"session_id": "s1", "prompt": QUERY, "cwd": "C:/repo/proj"}
    base.update(kwargs)
    return base


# --- 閘門 -------------------------------------------------------------------

def test_short_prompts_retrieve_nothing():
    """語料實測 human 輪次中位數只有 58 字元，大量是「繼續」「可以」。

    那種輸入本來就不該召回任何東西——硬查只會拿雜訊填滿 context。
    """
    pool = [_concept("c-1")]
    assert select(pool, "繼續", "proj", set()) == []
    assert select(pool, "可以，那就這樣", "proj", set()) == []
    assert select(pool, QUERY, "proj", set()) != []


def test_other_repos_stay_out():
    """別的專案的記憶配上剛好撞詞的 query 是這條路最典型的假陽性。"""
    pool = [_concept("c-other", scope="another-repo")]
    assert select(pool, QUERY, "proj", set()) == []


def test_global_memories_reach_every_repo():
    """跨專案通用的記憶在任何 repo 都要放行，三條路的判斷共用同一份實作。"""
    for scope_value in (None, "global", "*"):
        pool = [_concept("c-1", scope=scope_value)]
        assert [c["id"] for c in select(pool, QUERY, "proj", set())] == ["c-1"], scope_value


def test_the_threshold_is_scale_free_across_pool_sizes():
    """**這是實作時真的踩到的坑。**

    BM25 的 idf 依賴池子大小：87 條時稀有詞 idf 約 4.5，只有 2 條時上限是
    ``log(2)=0.69``。門檻若只除以 token 數，等於用大 scope 的分布去卡小 scope，
    結果是小 scope **永遠不觸發**——而且完全靜默，看起來就像「沒有相關記憶」。
    """
    small = [_concept("c-1"), _concept("c-2", cue="完全無關的主題 部署 憑證 輪替")]
    large = small + [_concept(f"c-pad-{i}", cue=f"無關主題 {i} " * 5) for i in range(40)]

    assert [c["id"] for c in select(small, QUERY, "proj", set())] == ["c-1"]
    assert [c["id"] for c in select(large, QUERY, "proj", set())] == ["c-1"]


def test_top_k_caps_what_goes_into_context():
    pool = [_concept(f"c-{i}") for i in range(INJECT_TOP_K + 3)]
    assert len(select(pool, QUERY, "proj", set())) == INJECT_TOP_K


def test_already_injected_memories_are_not_repeated(tmp_path, monkeypatch):
    """三個入口共用同一份節流狀態，否則同一條記憶會被注入好幾次。"""
    _isolate(tmp_path, monkeypatch, [_concept("c-1")])
    assert run(_payload()) is not None
    assert run(_payload()) is None


def test_dry_run_leaves_no_trace(tmp_path, monkeypatch):
    log = _isolate(tmp_path, monkeypatch, [_concept("c-1")])
    assert run(_payload(), dry_run=True) is not None
    assert not log.exists()
    assert hook_pretooluse.load_state("s1")["injected"] == []


# --- 語料標記 ---------------------------------------------------------------

def test_prompt_id_is_used_when_present(tmp_path, monkeypatch):
    log = _isolate(tmp_path, monkeypatch, [_concept("c-1")])
    run(_payload(prompt_id="p-real"))
    record = json.loads(log.read_text(encoding="utf-8").strip())
    assert record["prompt_id"] == "p-real"
    assert record["prompt_fingerprint"] == prompt_fingerprint(QUERY)


def test_fingerprint_carries_attribution_when_prompt_id_is_missing(tmp_path, monkeypatch):
    """UserPromptSubmit 的 payload 不保證帶 prompt_id。

    沒有鍵就等於「注入了卻標記不到」，而那會讓之後的校準系統性偏低
    且完全看不出來——寧可用比較弱的鍵。
    """
    log = _isolate(tmp_path, monkeypatch, [_concept("c-1")])
    run(_payload())
    record = json.loads(log.read_text(encoding="utf-8").strip())
    assert record["prompt_id"] is None

    rec = {"type": "user", "promptId": "p1", "cwd": "C:/repo/proj", "gitBranch": "main",
           "sessionId": "s1", "version": "2.1.216",
           "timestamp": "2026-07-25T00:00:00.000Z",
           "message": {"role": "user", "content": QUERY}, "origin": {"kind": "human"}}
    episode = build_episode("p1", [rec], injections=load_injections(log))
    assert episode["injected"] == ["c-1"]


def test_records_without_any_key_are_dropped(tmp_path):
    """既沒有 prompt_id 也沒有指紋的紀錄無法歸屬，直接丟掉。

    留著會讓 ``--doctor`` 的「注入 N 筆、語料對上 M 輪」出現永遠追不平的差額。
    """
    log = tmp_path / "injections.jsonl"
    log.write_text(json.dumps({"session_id": "s1", "injected": ["c-1"]}) + "\n",
                   encoding="utf-8")
    assert load_injections(log) == {}
