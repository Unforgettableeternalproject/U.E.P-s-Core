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
    # TOUCH_LOG 一度沒隔離：跑 dry_run=False 的測試往真實的 touches.jsonl
    # 寫了 21 筆 session_id=s1 的假觀察。測試沒隔離的全域狀態等於沒測
    monkeypatch.setattr(hook_pretooluse, "TOUCH_LOG", tmp_path / "touches.jsonl")
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


def test_bare_filename_anchor_matches_deeper_touch_key(tmp_path, monkeypatch):
    """裸檔名錨點必須比得中 3 段 touch 鍵——2026-09-06 盲測抓到的靜默失效。

    蒸餾出的錨點常常只有檔名（`lite_engine.py`），而 touch 鍵固定收斂成
    最後 3 段（`modules/tts_module/lite_engine.py`）。全等比對下這批錨點的
    檔案訊號整個死掉且不報錯：實測 c-1326 在符號命中 1（emo_bias）、
    檔案也確實碰到的情況下 overlap 只算到 1，該注入而沒注入。
    """
    _isolate(tmp_path, monkeypatch, [])
    pool = [_concept("c-1326", ["lite_engine.py", "normalize_vector", "emo_bias"])]
    touched = {"modules/tts_module/lite_engine.py"}

    picked = select(pool, touched, {"emo_bias"}, "proj", set())
    assert [c["id"] for c in picked] == ["c-1326"]
    # 反向也要通：錨點帶路徑、touch 鍵只剩檔名（淺層檔案）
    pool2 = [_concept("c-x", ["src/api/tracking.ts", "fetchTracking"])]
    picked2 = select(pool2, {"tracking.ts"}, {"fetchtracking"}, "proj", set())
    assert [c["id"] for c in picked2] == ["c-x"]


def test_suffix_match_requires_segment_boundary(tmp_path, monkeypatch):
    """尾段吻合要以段界為準——`engine.py` 不可以比中 `lite_engine.py`。"""
    _isolate(tmp_path, monkeypatch, [])
    pool = [_concept("c-1", ["engine.py", "spin"])]
    picked = select(pool, {"modules/tts_module/lite_engine.py"}, {"spin"}, "proj", set())
    assert picked == []


def test_absolute_tool_paths_match_repo_relative_anchors(tmp_path, monkeypatch):
    """`tool_input.file_path` 是絕對路徑，`anchors` 是 repo 相對路徑。

    實測抓到的形狀：`file_key` 取末 3 段，於是

        絕對路徑 → testseperatememorysystem/agent_memory_spike/retrieve.py
        錨點     →                          agent_memory_spike/retrieve.py

    兩邊永遠不相等，**hook 裡的檔案錨點完全失效**，只剩符號在起作用。
    上面那些測試都直接餵 `select()` 的集合，所以繞過了這一段看不到。

    A1 量觸發率時比對的兩邊都是語料裡的相對路徑，那組數字同樣看不到——
    與「`overlap>=2` 搬進 hook 就歸零」是同一型的錯：同名的量不一定是同一個量。
    """
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    target = repo / "agent_memory_spike" / "retrieve.py"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")

    _isolate(tmp_path, monkeypatch, [
        _concept("c-1", ["agent_memory_spike/retrieve.py", "cue"]),
    ])
    result = run(_payload(
        cwd=str(repo),
        tool_input={"file_path": str(target), "new_string": "with_cue and cue"},
    ), dry_run=True)
    assert result is not None
    assert "statement c-1" in result["hookSpecificOutput"]["additionalContext"]


def test_normalization_base_comes_from_the_target_not_cwd(tmp_path, monkeypatch):
    """cwd 在別的 repo 時，正規化基準要從目標檔案往上找。

    實測抓到的形狀：bash `cd` 進 nested 子 repo 後，對父 repo 根目錄檔案的
    Write 不在 `repo_root(cwd)` 底下，正規化整個不動作，key 變成絕對路徑的
    末 3 段（`mind-door/ai-website/append-2199-scss.js`），與語料端的
    `append-2199-scss.js` 永遠對不上——doctor 對帳時 18/81 輪「漏看」，
    多數是這個基準漂移，不是 hook 真的沒看到。
    """
    outer = tmp_path / "outer"
    (outer / ".git").mkdir(parents=True)
    inner = outer / "inner"
    (inner / ".git").mkdir(parents=True)
    target = outer / "append-cards.js"
    target.write_text("x", encoding="utf-8")

    # scope 用 None（跨專案）：scope 閘門比對的是 cwd 推出的 repo 名，
    # 這個測試的重點是路徑基準，不是 scope
    _isolate(tmp_path, monkeypatch, [
        _concept("c-1", ["append-cards.js", "cue"], scope=None),
    ])
    # cwd 在 inner，目標在 outer 根目錄——舊實作用 repo_root(cwd)=inner 當基準，
    # 目標不在底下，key 會是絕對路徑末 3 段
    result = run(_payload(
        cwd=str(inner),
        tool_input={"file_path": str(target), "new_string": "with cue"},
    ), dry_run=True)
    assert result is not None
    assert "statement c-1" in result["hookSpecificOutput"]["additionalContext"]


def test_scope_keeps_other_repos_out(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch, [])
    pool = [_concept("c-1", ["src/api/tracking.ts", "fetchTracking"], scope="other")]
    assert select(pool, {"src/api/tracking.ts"}, {"fetchtracking"}, "proj", set()) == []


def test_global_memories_reach_every_repo(tmp_path, monkeypatch):
    """跨專案通用的記憶（`scope=None`）在任何 repo 都要放行。

    通用知識被鎖在單一 repo 是整個 scope 修復的起點，三條路都要有這道測試——
    只在 SessionStart 測過的話，另外兩條分岔了也看不見。
    """
    _isolate(tmp_path, monkeypatch, [])
    anchors = ["src/api/tracking.ts", "fetchTracking"]
    for scope_value in (None, "global", "*"):
        pool = [_concept("c-1", anchors, scope=scope_value)]
        picked = select(pool, {"src/api/tracking.ts"}, {"fetchtracking"}, "proj", set())
        assert [c["id"] for c in picked] == ["c-1"], scope_value


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


# --- repo 改名 -------------------------------------------------------------

def test_scope_matches_across_repo_rename(monkeypatch):
    """記憶的 scope 是蒸餾當下的 repo 名，改名後不該就此失聯。

    2026-08-22 實測：309 條記憶（池子的 38%）的 scope 指向已改名的 repo，
    注入率因此掉到零，而且完全沒有錯誤訊息。
    """
    import transcript
    monkeypatch.setitem(transcript.REPO_ALIASES, "OldRepo", "NewRepo")
    assert hook_pretooluse.scope_matches("OldRepo", "NewRepo")
    assert hook_pretooluse.scope_matches("NewRepo", "NewRepo")
    assert not hook_pretooluse.scope_matches("OtherRepo", "NewRepo")
    # global 不受影響，沒有 scope 的情境也不能誤放行
    assert hook_pretooluse.scope_matches(None, "NewRepo")
    assert not hook_pretooluse.scope_matches("OldRepo", None)
