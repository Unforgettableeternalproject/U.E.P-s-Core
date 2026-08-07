"""Phase 3 B 注入 hook 的測試。

測的重點是三道閘門與「輪」的邊界——那是實作時真的踩到的地方：
只比對檔案的話 `overlap>=2` 在真實語料上觸發率是 0.0%，
而符號要跨同一輪的多次工具呼叫累積才湊得出來。

執行：``python -m pytest agent_memory_spike/test_inject.py -q``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import hook_pretooluse  # noqa: E402
from hook_pretooluse import load_pool, run, select  # noqa: E402


def _concept(cid, anchors, surprisal=1.0, scope="proj"):
    return {"id": cid, "statement": f"statement {cid}", "anchors": anchors,
            "surprisal": surprisal, "scope": scope}


def _payload(**kwargs):
    base = {
        "session_id": "s1",
        "prompt_id": "p1",
        "tool_name": "Edit",
        "tool_input": {"file_path": "C:/repo/proj/src/api/tracking.ts"},
    }
    base.update(kwargs)
    return base


def _isolate(tmp_path, monkeypatch, concepts):
    concept_path = tmp_path / "concepts.json"
    concept_path.write_text(json.dumps(concepts), encoding="utf-8")
    monkeypatch.setattr(hook_pretooluse, "CONCEPT_PATH", concept_path)
    monkeypatch.setattr(hook_pretooluse, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(hook_pretooluse, "INJECTION_LOG", tmp_path / "injections.jsonl")
    return concept_path


# --- 閘門 -------------------------------------------------------------------

def test_uncalibrated_memories_never_get_injected(tmp_path, monkeypatch):
    """未校準不是「低價值」而是「不知道價值」，同樣不該進 context。

    注入模型本來就會的東西是負價值：佔掉 context 卻不改變任何行為。
    """
    path = _isolate(tmp_path, monkeypatch, [
        _concept("c-1", ["src/api/tracking.ts", "fetchTracking"], surprisal=None),
        _concept("c-2", ["src/api/tracking.ts", "fetchTracking"], surprisal=0.4),
        _concept("c-3", ["src/api/tracking.ts", "fetchTracking"], surprisal=0.8),
    ])
    assert [c["id"] for c in load_pool(path)] == ["c-3"]


def test_symbols_are_what_make_the_threshold_reachable(tmp_path, monkeypatch):
    """只比對檔案時 overlap>=2 湊不出來——實測真實語料觸發率 0.0%。

    多數 concept 只有一個檔案錨點，兩個檔案重疊在單一次編輯裡不可能發生。
    符號補上這一項之後才有東西可注入。
    """
    _isolate(tmp_path, monkeypatch, [])
    pool = [_concept("c-1", ["src/api/tracking.ts", "fetchTracking"])]
    touched = {"repo/src/api/tracking.ts", "src/api/tracking.ts"}

    assert select(pool, touched, set(), "proj", set()) == []
    picked = select(pool, touched, {"fetchtracking"}, "proj", set())
    assert [c["id"] for c in picked] == ["c-1"]


def test_scope_keeps_other_repos_out(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch, [])
    pool = [_concept("c-1", ["src/api/tracking.ts", "fetchTracking"], scope="other")]
    assert select(pool, {"src/api/tracking.ts"}, {"fetchtracking"}, "proj", set()) == []


def test_top_k_caps_what_goes_into_context(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch, [])
    pool = [_concept(f"c-{i}", ["src/api/tracking.ts", "fetchTracking"]) for i in range(6)]
    picked = select(pool, {"src/api/tracking.ts"}, {"fetchtracking"}, "proj", set())
    assert len(picked) == hook_pretooluse.INJECT_TOP_K


# --- 輪的邊界與節流 ---------------------------------------------------------

def test_symbols_accumulate_within_a_turn(tmp_path, monkeypatch):
    """一輪內的多次工具呼叫要累積，否則單次呼叫湊不到門檻。"""
    _isolate(tmp_path, monkeypatch, [
        _concept("c-1", ["src/api/tracking.ts", "alpha", "beta"]),
    ])
    # 第一次只碰到 alpha：檔案 1 + 符號 1 = 2，剛好過門檻
    first = run(_payload(tool_input={"file_path": "C:/repo/proj/other.ts",
                                     "new_string": "alpha"}))
    assert first is None  # other.ts 不是錨點，只有符號 alpha → overlap 1

    second = run(_payload(tool_input={"file_path": "C:/repo/proj/again.ts",
                                      "new_string": "beta"}))
    # alpha 跨呼叫留了下來，加上 beta 才湊到 2
    assert second is not None
    assert "statement c-1" in second["hookSpecificOutput"]["additionalContext"]


def test_a_new_turn_resets_the_accumulation(tmp_path, monkeypatch):
    """跨輪累積會讓門檻越來越鬆，等於門檻形同虛設。"""
    _isolate(tmp_path, monkeypatch, [
        _concept("c-1", ["src/api/tracking.ts", "alpha", "beta"]),
    ])
    assert run(_payload(tool_input={"file_path": "C:/repo/proj/x.ts",
                                    "new_string": "alpha"})) is None
    # 換一輪，先前的 alpha 不該留著
    assert run(_payload(prompt_id="p2",
                        tool_input={"file_path": "C:/repo/proj/y.ts",
                                    "new_string": "beta"})) is None


def test_the_same_memory_is_not_injected_twice_in_one_session(tmp_path, monkeypatch):
    """PreToolUse 每次工具呼叫都觸發，不節流的話同一條會反覆洗版。"""
    _isolate(tmp_path, monkeypatch, [
        _concept("c-1", ["src/api/tracking.ts", "fetchTracking"]),
    ])
    payload = _payload(tool_input={"file_path": "C:/repo/proj/src/api/tracking.ts",
                                   "new_string": "fetchTracking()"})
    assert run(payload) is not None
    assert run(payload) is None


def test_injection_is_recorded_for_the_corpus(tmp_path, monkeypatch):
    """side-car 是「哪些輪次被記憶影響過」的唯一依據，漏寫就查不出來了。"""
    _isolate(tmp_path, monkeypatch, [
        _concept("c-1", ["src/api/tracking.ts", "fetchTracking"]),
    ])
    run(_payload(tool_input={"file_path": "C:/repo/proj/src/api/tracking.ts",
                             "new_string": "fetchTracking()"}))
    lines = (tmp_path / "injections.jsonl").read_text(encoding="utf-8").strip().splitlines()
    record = json.loads(lines[0])
    assert record["session_id"] == "s1"
    assert record["prompt_id"] == "p1"
    assert record["injected"] == ["c-1"]


def test_dry_run_leaves_no_trace(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch, [
        _concept("c-1", ["src/api/tracking.ts", "fetchTracking"]),
    ])
    payload = _payload(tool_input={"file_path": "C:/repo/proj/src/api/tracking.ts",
                                   "new_string": "fetchTracking()"})
    assert run(payload, dry_run=True) is not None
    assert not (tmp_path / "injections.jsonl").exists()
    # 沒留狀態，所以同一次還會再算出同樣的結果
    assert run(payload, dry_run=True) is not None


def test_non_edit_tools_are_ignored(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch, [
        _concept("c-1", ["src/api/tracking.ts", "fetchTracking"]),
    ])
    assert run(_payload(tool_name="Bash", tool_input={"command": "ls"})) is None
