"""SessionStart 注入 hook 的測試。

重點在這條路與 PreToolUse **不同**的地方：
沒有相關性訊號（排序只能靠 surprisal）、影響及於整個 session（語料標記要展開到每一輪）、
以及兩條路共用節流狀態（同一條記憶不該從兩個入口各注入一次）。

執行：``python -m pytest agent_memory_spike/test_session_inject.py -q``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import hook_pretooluse  # noqa: E402
import hook_session_start  # noqa: E402
from hook_session_start import SESSION_TOP_K, run, select  # noqa: E402
from transcript import (  # noqa: E402
    SESSION_WIDE_PROMPT_ID,
    build_episode,
    load_injections,
)


def _concept(cid, surprisal=1.0, scope="proj"):
    return {"id": cid, "statement": f"statement {cid}", "anchors": [],
            "surprisal": surprisal, "scope": scope}


def _isolate(tmp_path, monkeypatch, concepts):
    """把三個落地點都導到 tmp_path。

    ``CONCEPT_PATH`` 等常數在 hook_pretooluse 裡定義、被 hook_session_start 匯入，
    兩邊都要 patch——匯入時綁的是值不是參照，只 patch 一邊會讓測試讀到真實檔案。
    """
    concept_path = tmp_path / "concepts.json"
    concept_path.write_text(json.dumps(concepts), encoding="utf-8")
    for module in (hook_pretooluse, hook_session_start):
        monkeypatch.setattr(module, "CONCEPT_PATH", concept_path, raising=False)
        monkeypatch.setattr(module, "INJECTION_LOG", tmp_path / "injections.jsonl",
                            raising=False)
    monkeypatch.setattr(hook_pretooluse, "STATE_DIR", tmp_path / "state")
    # payload 裡的 cwd 是假路徑，真的去找 .git 會得到 None（那是「認不出 repo」，
    # 語意與「在 proj 底下」完全不同），所以直接固定 scope
    monkeypatch.setattr(hook_session_start, "repo_root_name", lambda _cwd: "proj")
    return tmp_path / "injections.jsonl"


def _payload(**kwargs):
    base = {"session_id": "s1", "cwd": "C:/repo/proj", "source": "startup"}
    base.update(kwargs)
    return base


# --- 篩選 -------------------------------------------------------------------

def test_only_calibrated_memories_are_injected(tmp_path, monkeypatch):
    """未校準的條目不是「低價值」而是「不知道價值」，兩者都不該進 context。"""
    _isolate(tmp_path, monkeypatch, [
        _concept("c-1", surprisal=None),
        _concept("c-2", surprisal=0.4),
        _concept("c-3", surprisal=0.8),
    ])
    assert "statement c-3" in run(_payload(), dry_run=True)
    assert "statement c-1" not in run(_payload(), dry_run=True)
    assert "statement c-2" not in run(_payload(), dry_run=True)


def test_other_repos_stay_out_but_global_memories_travel(tmp_path, monkeypatch):
    """scope 過濾是這條路唯一的相關性依據。

    global 那半目前池子裡一條都沒有（蒸餾端還沒產出這個分類），
    但讀取端要先支援——不然通用知識會被永久鎖在單一 repo 裡。
    """
    pool = [_concept("c-mine", scope="proj"),
            _concept("c-other", scope="another-repo"),
            _concept("c-global", scope="global"),
            _concept("c-none", scope=None)]
    picked = [c["id"] for c in select(pool, "proj", set())]
    assert sorted(picked) == ["c-global", "c-mine", "c-none"]


def test_an_unrecognised_repo_only_gets_global_memories(tmp_path, monkeypatch):
    """**刻意與 hook_pretooluse 的「認不出就不過濾」相反。**

    那裡還有檔案與符號的重疊當相關性依據；這裡什麼都沒有。
    認不出 repo 還把別的專案的記憶倒進來，注入的是純雜訊，
    而且會留在整個 session 的 context 裡。
    """
    pool = [_concept("c-proj", scope="proj"), _concept("c-global", scope="global")]
    assert [c["id"] for c in select(pool, None, set())] == ["c-global"]


def test_ranking_is_by_surprisal_and_deterministic(tmp_path, monkeypatch):
    """沒有 query 就沒有相關性可算，只能靠 surprisal 排。

    同分用 id 決定：不確定的排序會讓「注入有沒有用」變成不可重現的觀察。
    """
    pool = [_concept("c-b", surprisal=0.9), _concept("c-a", surprisal=0.9),
            _concept("c-top", surprisal=1.0)]
    assert [c["id"] for c in select(pool, "proj", set())] == ["c-top", "c-a", "c-b"]


def test_the_cap_limits_what_a_whole_session_carries(tmp_path, monkeypatch):
    """無差別注入的東西留在整個 session 的 context 裡，錯了的成本一路付到底。"""
    pool = [_concept(f"c-{i}") for i in range(SESSION_TOP_K + 5)]
    assert len(select(pool, "proj", set())) == SESSION_TOP_K


# --- 與 PreToolUse 共用的節流 -------------------------------------------------

def test_resume_does_not_re_inject_the_same_memories(tmp_path, monkeypatch):
    """resume / clear 會再次觸發 SessionStart，但舊 context 還在。"""
    _isolate(tmp_path, monkeypatch, [_concept("c-1"), _concept("c-2")])
    first = run(_payload())
    assert "statement c-1" in first
    assert run(_payload()) is None


def test_session_start_seeds_the_state_pretooluse_reads(tmp_path, monkeypatch):
    """兩個入口共用同一份節流狀態，否則同一條記憶會被注入兩次。"""
    _isolate(tmp_path, monkeypatch, [_concept("c-1")])
    run(_payload())
    state = hook_pretooluse.load_state("s1")
    assert state["injected"] == ["c-1"]
    # PreToolUse 累積的欄位要保持完整，否則它那一輪的 overlap 會從零重算
    assert state["touched"] == [] and state["symbols"] == []


def test_dry_run_leaves_no_trace(tmp_path, monkeypatch):
    log = _isolate(tmp_path, monkeypatch, [_concept("c-1")])
    assert run(_payload(), dry_run=True) is not None
    assert not log.exists()
    assert hook_pretooluse.load_state("s1")["injected"] == []


# --- 語料標記 ---------------------------------------------------------------

def test_injection_is_recorded_under_the_session_wide_key(tmp_path, monkeypatch):
    """SessionStart 沒有 prompt_id，用哨兵鍵記錄。"""
    log = _isolate(tmp_path, monkeypatch, [_concept("c-1")])
    run(_payload())
    record = json.loads(log.read_text(encoding="utf-8").strip())
    assert record["prompt_id"] == SESSION_WIDE_PROMPT_ID
    assert record["injected"] == ["c-1"]


def test_session_wide_injection_marks_every_turn(tmp_path):
    """**這是這條路能不能安全掛載的關鍵。**

    注入的記憶留在 context 裡，第五輪跟第一輪一樣看得到它。
    少標記任何一輪，那輪就會被當成乾淨語料拿去校準 surprisal，
    而偏誤的方向是系統性偏低且完全看不出來——這正是 hook 遲遲不掛的理由。
    """
    log = tmp_path / "injections.jsonl"
    log.write_text(json.dumps({
        "session_id": "sess-1", "prompt_id": SESSION_WIDE_PROMPT_ID,
        "injected": ["c-1"],
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    injections = load_injections(log)

    def _turn(prompt_id, index):
        rec = {"type": "user", "promptId": prompt_id, "cwd": "C:/repo/proj",
               "gitBranch": "main", "sessionId": "sess-1", "version": "2.1.216",
               "timestamp": "2026-07-25T00:00:00.000Z",
               "message": {"role": "user", "content": "hi"},
               "origin": {"kind": "human"}}
        return build_episode(prompt_id, [rec], turn_index=index, injections=injections)

    assert _turn("p1", 0)["injected"] == ["c-1"]
    assert _turn("p9", 8)["injected"] == ["c-1"]


def test_per_turn_and_session_wide_injections_merge(tmp_path):
    """一輪可能同時被兩個入口注入，兩邊都要留下，否則「這輪看過什麼」失真。"""
    log = tmp_path / "injections.jsonl"
    log.write_text("\n".join([
        json.dumps({"session_id": "sess-1", "prompt_id": SESSION_WIDE_PROMPT_ID,
                    "injected": ["c-start"]}),
        json.dumps({"session_id": "sess-1", "prompt_id": "p1", "injected": ["c-edit"]}),
    ]) + "\n", encoding="utf-8")
    rec = {"type": "user", "promptId": "p1", "cwd": "C:/repo/proj", "gitBranch": "main",
           "sessionId": "sess-1", "version": "2.1.216",
           "timestamp": "2026-07-25T00:00:00.000Z",
           "message": {"role": "user", "content": "hi"}, "origin": {"kind": "human"}}
    episode = build_episode("p1", [rec], injections=load_injections(log))
    assert sorted(episode["injected"]) == ["c-edit", "c-start"]
