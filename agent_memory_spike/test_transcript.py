"""Phase 1 解析層的測試。

刻意用合成資料而非真實 transcript：真實檔案會隨 session 增長而變動，
拿它當 fixture 的話測試會隨時間漂移。

不放在主專案的 `tests/` 底下，避免混進 echo_memory 的 suite。
執行：``python -m pytest agent_memory_spike/test_transcript.py -q``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import hook_stop  # noqa: E402
from hook_stop import (  # noqa: E402
    completed_episodes,
    doctor,
    episode_path,
    load_deduped,
    recorded_prompt_ids,
    repair,
    sync,
)
from transcript import (  # noqa: E402
    ORIGIN_HUMAN,
    ORIGIN_META,
    ORIGIN_TASK_NOTIFICATION,
    build_episode,
    classify_origin,
    episodes_from_transcript,
    is_tool_result,
    iter_prompt_groups,
)


def _user(prompt_id, *, text="hi", origin=None, prompt_source=None, is_meta=None,
          tool_result=None, cwd="C:/repo/proj", branch="main"):
    rec = {
        "type": "user",
        "promptId": prompt_id,
        "cwd": cwd,
        "gitBranch": branch,
        "sessionId": "sess-1",
        "version": "2.1.216",
        "timestamp": "2026-07-25T00:00:00.000Z",
        "message": {"role": "user", "content": text},
    }
    if origin is not None:
        rec["origin"] = origin
    if prompt_source is not None:
        rec["promptSource"] = prompt_source
    if is_meta is not None:
        rec["isMeta"] = is_meta
    if tool_result is not None:
        rec["toolUseResult"] = tool_result
    return rec


def _assistant(*, text=None, tools=(), thinking=0, cwd="C:/repo/proj", branch="main",
               mcp_tool=None, skill=None):
    content = []
    for _ in range(thinking):
        content.append({"type": "thinking", "thinking": "internal"})
    for entry in tools:
        # 元素可以是工具名，或 (工具名, input) — 後者用來測路徑參數的抽取
        name, payload = entry if isinstance(entry, tuple) else (entry, {})
        content.append({"type": "tool_use", "name": name, "input": payload})
    if text:
        content.append({"type": "text", "text": text})
    rec = {
        "type": "assistant",
        "cwd": cwd,
        "gitBranch": branch,
        "sessionId": "sess-1",
        "version": "2.1.216",
        "timestamp": "2026-07-25T00:00:01.000Z",
        "message": {"role": "assistant", "content": content},
    }
    if mcp_tool:
        rec["attributionMcpTool"] = mcp_tool
    if skill:
        rec["attributionSkill"] = skill
    return rec


# --- origin 分類 -----------------------------------------------------------
# 這組是整個 Phase 1 最要緊的正確性：背景 agent 的完成通知會以 user 記錄送進來，
# 若被當成使用者指示存起來，等於把 agent 自己的輸出偽裝成使用者的要求。

def test_typed_human_is_human():
    rec = _user("p1", origin={"kind": "human"}, prompt_source="typed")
    assert classify_origin(rec) == ORIGIN_HUMAN


def test_task_notification_is_not_human():
    rec = _user("p1", origin={"kind": "task-notification"}, prompt_source="system")
    assert classify_origin(rec) == ORIGIN_TASK_NOTIFICATION


def test_meta_flag_wins():
    rec = _user("p1", is_meta=True, origin={"kind": "human"})
    assert classify_origin(rec) == ORIGIN_META


def test_tool_result_is_detected():
    assert is_tool_result(_user("p1", tool_result={"stdout": "ok"}))
    assert not is_tool_result(_user("p1"))


# --- 分組 ------------------------------------------------------------------

def test_groups_split_by_prompt_id():
    records = [
        _user("p1", origin={"kind": "human"}),
        _assistant(tools=["Bash"]),
        _user("p2", origin={"kind": "human"}),
        _assistant(tools=["Read"]),
    ]
    groups = list(iter_prompt_groups(records))
    assert [g[0] for g in groups] == ["p1", "p2"]
    assert len(groups[0][1]) == 2


def test_assistant_records_attach_to_current_prompt():
    """assistant 記錄不帶 promptId，必須靠位置歸屬到當前這輪。"""
    records = [
        _user("p1", origin={"kind": "human"}),
        _assistant(tools=["Bash"]),
        _assistant(tools=["Read"]),
    ]
    _, group = next(iter(iter_prompt_groups(records)))
    ep = build_episode("p1", group)
    assert ep["tool_calls_total"] == 2


# --- episode 組裝 ----------------------------------------------------------

def test_tool_result_does_not_become_user_text():
    records = [
        _user("p1", text="真的問題", origin={"kind": "human"}),
        _assistant(tools=["Bash"]),
        _user("p1", text="工具輸出不該混進來", tool_result={"stdout": "x"}),
    ]
    ep = build_episode("p1", records)
    assert ep["user_text"] == "真的問題"


def test_repo_resolves_to_git_root_not_subdirectory(tmp_path):
    """迴歸測試：bash 進子目錄後 cwd 是子目錄，直接取 name 會標錯 repo。

    實測把 Eternity 的 episode 標成 'islands'（元件目錄），
    聚合時會把同一個 repo 拆成好幾個，且看起來完全像正常資料。
    """
    repo_root = tmp_path / "MyRepo"
    (repo_root / ".git").mkdir(parents=True)
    deep = repo_root / "apps" / "web" / "src" / "islands"
    deep.mkdir(parents=True)

    records = [
        _user("p1", origin={"kind": "human"}, cwd=str(deep)),
        _assistant(tools=["Bash"], cwd=str(deep)),
    ]
    ep = build_episode("p1", records)
    assert ep["repo"] == "MyRepo"


def test_repo_falls_back_when_no_git_root(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    ep = build_episode("p1", [_user("p1", origin={"kind": "human"}, cwd=str(plain))])
    assert ep["repo"] == "not-a-repo"


def test_cwd_and_branch_collect_all_values():
    """同一輪內切分支或進子目錄都會發生，存單一值會失真。"""
    records = [
        _user("p1", origin={"kind": "human"}, branch="develop"),
        _assistant(tools=["Bash"], branch="feature/x", cwd="C:/repo/proj/sub"),
    ]
    ep = build_episode("p1", records)
    assert ep["git_branch"] == ["develop", "feature/x"]
    assert len(ep["cwd"]) == 2


def test_thinking_content_is_counted_not_stored():
    records = [
        _user("p1", origin={"kind": "human"}),
        _assistant(text="結論", thinking=3),
    ]
    ep = build_episode("p1", records)
    assert ep["thinking_blocks"] == 3
    assert "internal" not in json.dumps(ep, ensure_ascii=False)


def test_files_edited_from_file_history_delta():
    records = [
        _user("p1", origin={"kind": "human"}),
        {"type": "file-history-delta", "trackingPath": "C:/repo/proj/a.py",
         "timestamp": "2026-07-25T00:00:02.000Z"},
        {"type": "file-history-delta", "trackingPath": "C:/repo/proj/a.py",
         "timestamp": "2026-07-25T00:00:03.000Z"},
    ]
    ep = build_episode("p1", records)
    assert ep["files_edited"] == ["C:/repo/proj/a.py"]  # 去重


def test_files_edited_from_tool_use_params():
    """file-history-delta 只涵蓋一部分編輯，tool_use 的參數才是完整來源。

    真實語料裡 Edit 出現 5996 次，但只有 409/1372 輪有 delta 記錄。
    """
    records = [
        _user("p1", origin={"kind": "human"}),
        _assistant(tools=[
            ("Edit", {"file_path": "C:/repo/proj/a.py"}),
            ("Write", {"file_path": "C:/repo/proj/b.py"}),
            ("NotebookEdit", {"notebook_path": "C:/repo/proj/n.ipynb"}),
            ("Read", {"file_path": "C:/repo/proj/c.py"}),
            ("Bash", {"command": "ls"}),
        ]),
    ]
    ep = build_episode("p1", records)
    assert ep["files_edited"] == ["C:/repo/proj/a.py", "C:/repo/proj/b.py",
                                  "C:/repo/proj/n.ipynb"]
    # 讀取不算修改：搜尋、確認、瀏覽都會讀，混進去就把訊號淹掉了
    assert ep["files_read"] == ["C:/repo/proj/c.py"]


def test_edited_paths_are_normalized_against_repo_root(tmp_path):
    """兩個來源的路徑形狀不同，不正規化的話同一個檔案會有兩種表示。

    delta 的 trackingPath 是 repo 相對，Edit 的 file_path 是絕對路徑——
    任何「同一檔案被反覆修改」的比對都會因此失效。
    """
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    records = [
        _user("p1", origin={"kind": "human"}, cwd=str(tmp_path)),
        {"type": "file-history-delta", "trackingPath": "src\\a.py",
         "timestamp": "2026-07-25T00:00:02.000Z"},
        _assistant(tools=[("Edit", {"file_path": str(tmp_path / "src" / "a.py")})],
                   cwd=str(tmp_path)),
    ]
    ep = build_episode("p1", records)
    assert ep["files_edited"] == ["src/a.py"]  # 兩個來源收斂成同一筆


def test_mcp_tools_and_skills_collected():
    records = [
        _user("p1", origin={"kind": "human"}),
        _assistant(tools=["X"], mcp_tool="mcp__foo__bar", skill="pm"),
    ]
    ep = build_episode("p1", records)
    assert ep["mcp_tools"] == ["mcp__foo__bar"]
    assert ep["skills"] == ["pm"]


# --- 容錯 ------------------------------------------------------------------

def test_malformed_lines_are_skipped(tmp_path):
    """Stop hook 觸發時 transcript 尾端可能還沒 flush 完，半截的 JSON 不該讓 hook 掛掉。"""
    p = tmp_path / "t.jsonl"
    p.write_text(
        json.dumps(_user("p1", origin={"kind": "human"})) + "\n"
        + '{"type": "assistant", "message": {"content": [{"typ\n'
        + json.dumps(_assistant(tools=["Bash"])) + "\n",
        encoding="utf-8",
    )
    episodes = episodes_from_transcript(p)
    assert len(episodes) == 1
    assert episodes[0]["tool_calls_total"] == 1


def test_missing_file_returns_empty(tmp_path):
    assert episodes_from_transcript(tmp_path / "nope.jsonl") == []


# --- 去重 ------------------------------------------------------------------
# 迴歸測試：早期版本只比對檔案最後一筆的 prompt_id，
# 在 backfill 批次寫入時每一輪都比不中，導致整份重複寫入。

def test_recorded_prompt_ids_reads_all_not_just_last(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(
        "\n".join(json.dumps({"prompt_id": f"p{i}", "turn_index": i - 1}) for i in range(1, 6)) + "\n",
        encoding="utf-8",
    )
    ids = recorded_prompt_ids(p)
    assert ids == {("p1", 0), ("p2", 1), ("p3", 2), ("p4", 3), ("p5", 4)}
    # 關鍵：第一筆也要被認出來，不能只看最後一筆
    assert ("p1", 0) in ids


def test_recorded_prompt_ids_tolerates_garbage(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(
        '{"prompt_id": "p1", "turn_index": 0}\nnot json\n{"prompt_id": "p2", "turn_index": 1}\n',
        encoding="utf-8",
    )
    assert recorded_prompt_ids(p) == {("p1", 0), ("p2", 1)}


def test_recorded_prompt_ids_missing_file(tmp_path):
    assert recorded_prompt_ids(tmp_path / "nope.jsonl") == set()


def test_episode_path_rejects_traversal(tmp_path):
    """session_id 來自 hook payload 且決定檔名。"""
    p = episode_path(tmp_path, "../../etc/passwd")
    assert p.parent == tmp_path
    assert ".." not in p.name


# --- 完整性：不可寫入尚未結束的輪次 -----------------------------------------
# 迴歸測試：初版用 Stop hook payload 的 prompt_id 定位「當前輪」並寫入，
# 但該輪在 hook 觸發時未必已完整落盤。實測某輪存進去只有 670 字元 / 5 次 tool call，
# 實際是 2225 字元 / 14 次——少七成，且因去重邏輯永遠不會被更新，也無任何殘缺標記。

def _write_transcript(tmp_path, n_turns, last_turn_tools=1):
    records = []
    for i in range(1, n_turns + 1):
        records.append(_user(f"p{i}", text=f"問題{i}", origin={"kind": "human"}))
        tools = ["Bash"] * (last_turn_tools if i == n_turns else 3)
        records.append(_assistant(text=f"回答{i}", tools=tools))
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return p


def test_latest_turn_is_excluded(tmp_path):
    """最新一輪可能還在進行中，一律不寫。"""
    t = _write_transcript(tmp_path, 3)
    eps = completed_episodes(t)
    assert [e["prompt_id"] for e in eps] == ["p1", "p2"]


def test_single_turn_yields_nothing(tmp_path):
    """只有一輪時無法確認它結束了，什麼都不該寫。"""
    t = _write_transcript(tmp_path, 1)
    assert completed_episodes(t) == []


def test_sync_never_writes_in_progress_turn(tmp_path):
    t = _write_transcript(tmp_path, 2)
    ep_dir = tmp_path / "eps"
    written, _ = sync(t, ep_dir, "sess")
    assert written == 1
    assert recorded_prompt_ids(episode_path(ep_dir, "sess")) == {("p1", 0)}


def test_sync_is_incremental_across_calls(tmp_path):
    """後續輪次要能被自動補上，不必依賴 hook 每次都成功。"""
    ep_dir = tmp_path / "eps"
    sync(_write_transcript(tmp_path, 2), ep_dir, "sess")
    written, skipped = sync(_write_transcript(tmp_path, 4), ep_dir, "sess")
    assert written == 2 and skipped == 1
    assert recorded_prompt_ids(episode_path(ep_dir, "sess")) == {("p1", 0), ("p2", 1), ("p3", 2)}


def test_sync_is_idempotent(tmp_path):
    t = _write_transcript(tmp_path, 3)
    ep_dir = tmp_path / "eps"
    sync(t, ep_dir, "sess")
    written, skipped = sync(t, ep_dir, "sess")
    assert written == 0 and skipped == 2


def test_repair_fixes_truncated_record(tmp_path):
    """修復早期版本寫入的殘缺紀錄。"""
    ep_dir = tmp_path / "eps"
    path = episode_path(ep_dir, "sess")
    path.parent.mkdir(parents=True)
    # 模擬一筆截斷的紀錄：只抓到 1 次 tool call，實際是 3 次
    path.write_text(
        json.dumps({"prompt_id": "p1", "assistant_text": "回", "tool_calls_total": 1},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    total, fixed = repair(_write_transcript(tmp_path, 3), ep_dir, "sess")
    assert total == 2 and fixed == 1
    recs = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert recs[0]["tool_calls_total"] == 3
    assert recs[0]["assistant_text"] == "回答1"


# --- promptId 不唯一 --------------------------------------------------------
# 迴歸測試：session 起始的 meta 注入在每次 resume 會重新出現且沿用同一個 promptId。
# 實測某個 id 在 7/30 與 8/02 各出現一次、內容不同，只用 prompt_id 當鍵會誤判成重複。

def test_same_prompt_id_in_separate_runs_are_distinct_turns(tmp_path):
    records = [
        _user("start", text="第一次注入", origin={"kind": "human"}),
        _assistant(text="A"),
        _user("mid", text="中間", origin={"kind": "human"}),
        _assistant(text="B"),
        _user("start", text="resume 後又注入一次", origin={"kind": "human"}),
        _assistant(text="C"),
        _user("tail", text="尾", origin={"kind": "human"}),
    ]
    t = tmp_path / "t.jsonl"
    t.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    eps = episodes_from_transcript(t)
    assert [(e["prompt_id"], e["turn_index"]) for e in eps] == [
        ("start", 0), ("mid", 1), ("start", 2), ("tail", 3)
    ]
    # 兩次 start 是不同的輪次，內容不同
    assert eps[0]["assistant_text"] == "A"
    assert eps[2]["assistant_text"] == "C"


def test_repeated_prompt_id_is_not_deduped_away(tmp_path):
    """兩次出現都要各自寫入，不能被當成同一輪跳過。"""
    records = [
        _user("start", origin={"kind": "human"}), _assistant(text="A"),
        _user("mid", origin={"kind": "human"}), _assistant(text="B"),
        _user("start", origin={"kind": "human"}), _assistant(text="C"),
        _user("tail", origin={"kind": "human"}),
    ]
    t = tmp_path / "t.jsonl"
    t.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    ep_dir = tmp_path / "eps"
    written, _ = sync(t, ep_dir, "sess")
    assert written == 3
    assert recorded_prompt_ids(episode_path(ep_dir, "sess")) == {("start", 0), ("mid", 1), ("start", 2)}


def _episode(prompt_id, turn_index, user_text, assistant_text, tool_calls=0, repo=None):
    episode = {
        "prompt_id": prompt_id,
        "turn_index": turn_index,
        "user_text": user_text,
        "assistant_text": assistant_text,
        "tool_calls_total": tool_calls,
    }
    if repo is not None:
        # doctor 會比對 repo / files_edited / files_read，只給文字欄位會被誤報成不一致；
        # injected 缺欄位會被判成舊 schema（那是刻意的，見 doctor 的 legacy_schema）
        episode.update({"repo": repo, "files_edited": [], "files_read": [], "injected": []})
    return episode


def test_dedup_survives_turn_index_drift_across_sessions(tmp_path):
    """resume 讓同一輪落進另一個 session 檔時，序號會位移，仍須去重。

    這是實測抓到的漏洞：原本的鍵是 (prompt_id, turn_index)，
    理由是「resume 的完整複本序號一致」——那個假設是錯的。
    32 組、64 輪就這樣重複進了語料。
    """
    (tmp_path / "a.jsonl").write_text(
        json.dumps(_episode("p1", 2, "改一下這裡", "好的，我改了")) + "\n", encoding="utf-8")
    (tmp_path / "b.jsonl").write_text(
        json.dumps(_episode("p1", 3, "改一下這裡", "好的，我改了")) + "\n", encoding="utf-8")

    episodes, duplicates = load_deduped(tmp_path)
    assert len(episodes) == 1
    assert duplicates == 1


def test_dedup_keeps_distinct_turns_that_share_a_prompt_id(tmp_path):
    """同一個 promptId 配同一句話，仍可能是真的兩輪——回覆完全不同就不能合併。

    meta 注入每次 resume 都重現且沿用同一個 promptId，
    實測某個 id 在 7/30 與 8/02 各出現一次。
    """
    (tmp_path / "a.jsonl").write_text(
        "\n".join([
            json.dumps(_episode("p1", 0, "繼續", "第一次的回覆內容")),
            json.dumps(_episode("p1", 5, "繼續", "完全不同的第二次回覆")),
        ]) + "\n", encoding="utf-8")

    episodes, _ = load_deduped(tmp_path)
    assert len(episodes) == 2


def test_dedup_prefers_the_more_complete_copy(tmp_path):
    """殘缺的副本是完整版的前綴，要被完整版取代——這是防殘缺寫入的唯一防線。"""
    (tmp_path / "a.jsonl").write_text(
        json.dumps(_episode("p1", 0, "做這件事", "我開始", tool_calls=5)) + "\n", encoding="utf-8")
    (tmp_path / "b.jsonl").write_text(
        json.dumps(_episode("p1", 0, "做這件事", "我開始做，然後完成了整件事", tool_calls=14)) + "\n",
        encoding="utf-8")

    episodes, _ = load_deduped(tmp_path)
    assert len(episodes) == 1
    assert episodes[0]["tool_calls_total"] == 14


# --- doctor 的空 assistant_text 檢查 ---------------------------------------
# 語料裡 11.4% 的輪次 assistant_text 是空的，而 doctor 一度完全不看這個欄位。
# 按「doctor 沒比對的欄位等於沒有保護」的教訓補上，難的地方在於
# **空不等於故障**：使用者送出後立刻中斷，agent 本來就沒有回應。

def _write_raw_transcript(tmp_path, session_id, records):
    path = tmp_path / f"{session_id}.transcript.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _stub_find_transcript(monkeypatch, mapping):
    monkeypatch.setattr(hook_stop, "find_transcript", lambda sid: mapping.get(sid))
    # 一併隔離注入紀錄：doctor 預設讀真實的 ~/.claude/.../injections.jsonl，
    # PreToolUse 上線前那個檔是空的、測試剛好綠；上線後開始有紀錄，
    # 這批測試就整批靜默失敗——測試沒隔離的全域狀態等於沒測
    monkeypatch.setattr(hook_stop, "load_injections", lambda: {})


def test_doctor_flags_empty_assistant_text_that_has_a_source(tmp_path, monkeypatch):
    """來源有回應、存檔卻是空的——這是唯一算故障的一類。

    **而且它落在舊 doctor 的盲點裡**：比對用的是排除最新一輪的 completed_episodes，
    所以 transcript 只有這一輪時 ref 是 None，整筆被靜默略過。
    """
    episode_dir = tmp_path / "episodes"
    episode_dir.mkdir()
    (episode_dir / "sess-1.jsonl").write_text(
        json.dumps(_episode("p1", 0, "做這件事", "")) + "\n", encoding="utf-8")

    transcript = _write_raw_transcript(tmp_path, "sess-1", [
        _user("p1", text="做這件事", origin={"kind": "human"}),
        _assistant(text="我做完了"),
    ])
    _stub_find_transcript(monkeypatch, {"sess-1": transcript})

    assert doctor(episode_dir) == 1


def test_doctor_accepts_empty_assistant_text_when_there_was_no_response(tmp_path, monkeypatch):
    """使用者送出後立刻中斷，transcript 裡那輪根本沒有 assistant 記錄。

    實測 135 輪屬於這類。把它報成問題的話 doctor 永遠是紅的，
    真正的故障就淹在裡面了。
    """
    episode_dir = tmp_path / "episodes"
    episode_dir.mkdir()
    (episode_dir / "sess-1.jsonl").write_text(
        json.dumps(_episode("p1", 0, "先等一下", "", repo="proj")) + "\n", encoding="utf-8")

    transcript = _write_raw_transcript(tmp_path, "sess-1", [
        _user("p1", text="先等一下", origin={"kind": "human"}),
        _user("p2", text="改成這樣", origin={"kind": "human"}),
        _assistant(text="好的"),
    ])
    _stub_find_transcript(monkeypatch, {"sess-1": transcript})

    assert doctor(episode_dir) == 0


def test_injected_marks_which_turns_saw_memory(tmp_path):
    """注入紀錄靠 (session_id, prompt_id) 對回語料。

    這個欄位是「哪些輪次被記憶影響過」的唯一依據——對不上的話，
    被污染的輪次會被當成乾淨語料拿去校準 surprisal。
    """
    from transcript import load_injections

    log = tmp_path / "injections.jsonl"
    log.write_text("\n".join(json.dumps(x) for x in [
        {"session_id": "sess-1", "prompt_id": "p1", "injected": ["c-001", "c-002"]},
        # 同一輪的第二次注入：一輪會改好幾個檔案，每次 PreToolUse 都召回一批
        {"session_id": "sess-1", "prompt_id": "p1", "injected": ["c-002", "c-003"]},
    ]) + "\n", encoding="utf-8")

    injections = load_injections(log)
    ep = build_episode("p1", [_user("p1", text="改一下這裡", origin={"kind": "human"}),
                              _assistant(text="好的")], injections=injections)
    # 累積而不是覆蓋，且不重複
    assert ep["injected"] == ["c-001", "c-002", "c-003"]

    # 沒有對應紀錄的輪次是空 list，不是缺欄位——兩者代表的意思不同
    other = build_episode("p9", [_user("p9", text="別的話", origin={"kind": "human"})],
                          injections=injections)
    assert other["injected"] == []


def test_doctor_accepts_empty_assistant_text_when_interrupted_mid_tool(tmp_path, monkeypatch):
    """做了事但沒有文字結論——中斷發生在工具執行途中，同樣不是故障。"""
    episode_dir = tmp_path / "episodes"
    episode_dir.mkdir()
    (episode_dir / "sess-1.jsonl").write_text(
        json.dumps(_episode("p1", 0, "查一下", "", tool_calls=2, repo="proj")) + "\n", encoding="utf-8")

    transcript = _write_raw_transcript(tmp_path, "sess-1", [
        _user("p1", text="查一下", origin={"kind": "human"}),
        _assistant(tools=("Read", "Grep")),
    ])
    _stub_find_transcript(monkeypatch, {"sess-1": transcript})

    assert doctor(episode_dir) == 0


# --- doctor 的 hook 觀察對帳 -------------------------------------------------
# 實測發生過一次 Write 沒被記進 `inject_state` 的 touched，隔離環境重跑三次都正常，
# 複現不出。`inject_state` 只保留當前輪，所以事後無從查證有沒有第二次——
# 這組測試鎖住的是「下次發生時看得見」，不是「不會再發生」。

def _touch_setup(tmp_path, monkeypatch, episode, touch_rows):
    episode_dir = tmp_path / "episodes"
    episode_dir.mkdir()
    (episode_dir / "sess-1.jsonl").write_text(json.dumps(episode) + "\n", encoding="utf-8")

    log = tmp_path / "touches.jsonl"
    log.write_text("".join(json.dumps(r) + "\n" for r in touch_rows), encoding="utf-8")
    monkeypatch.setattr(hook_stop, "load_touches", lambda: __import__(
        "transcript").load_touches(log))
    _stub_find_transcript(monkeypatch, {})
    return episode_dir


def _touched_episode(files_edited):
    episode = _episode("p1", 0, "改一下", "改好了", repo="proj")
    episode.update({"session_id": "sess-1", "files_edited": files_edited})
    return episode


def test_doctor_flags_edits_the_hook_never_saw(tmp_path, monkeypatch, capsys):
    """語料說這輪改了兩個檔案，hook 只看到一個 = 遺漏，要看得見。

    但它是**警示不是問題**：漏看傷的是注入效率（overlap 算在偏少的檔案集上，
    門檻被悄悄調高），語料本身沒有壞——不該為此擋下整條蒸餾管線。
    """
    episode_dir = _touch_setup(
        tmp_path, monkeypatch,
        _touched_episode(["src/a.ts", "src/b.ts"]),
        [{"session_id": "sess-1", "prompt_id": "p1", "file_key": "src/a.ts"}],
    )
    assert doctor(episode_dir) == 0
    assert "漏看 1 輪" in capsys.readouterr().err


def test_doctor_accepts_injections_still_waiting_in_the_tail(tmp_path, monkeypatch):
    """注入落在 session 的最後一輪時，語料裡還沒有它——那是設計性落後。

    「最新一輪不寫」是收料的鐵律（防半截資料），所以被注入的輪次
    永遠比注入紀錄晚一步入料。指紋沒有失效：transcript 裡標記得到，
    等下一輪出現它就會帶著 injected 標記進語料。報成問題會讓
    doctor 在每次注入後都紅一陣子。
    """
    episode_dir = tmp_path / "episodes"
    episode_dir.mkdir()
    (episode_dir / "sess-1.jsonl").write_text("", encoding="utf-8")
    transcript = _write_raw_transcript(tmp_path, "sess-1", [
        _user("p1", text="改一下", origin={"kind": "human"}),
        _assistant(text="好"),
    ])
    _stub_find_transcript(monkeypatch, {"sess-1": transcript})
    monkeypatch.setattr(hook_stop, "load_injections",
                        lambda: {("sess-1", "p1"): ["c-1"]})
    assert doctor(episode_dir) == 0


def test_doctor_flags_injections_no_turn_can_account_for(tmp_path, monkeypatch):
    """注入紀錄在 transcript 裡完全標記不到 = 指紋失效，這才是問題。

    被影響過的輪次會被當成乾淨語料，之後的校準有系統性偏誤——
    這是注入對帳存在的唯一理由。
    """
    episode_dir = tmp_path / "episodes"
    episode_dir.mkdir()
    (episode_dir / "sess-1.jsonl").write_text(
        json.dumps(_episode("p1", 0, "改一下", "好", repo="proj")) + "\n", encoding="utf-8")
    transcript = _write_raw_transcript(tmp_path, "sess-1", [
        _user("p1", text="改一下", origin={"kind": "human"}),
        _assistant(text="好"),
    ])
    _stub_find_transcript(monkeypatch, {"sess-1": transcript})
    monkeypatch.setattr(hook_stop, "load_injections",
                        lambda: {("sess-1", "p-gone"): ["c-1"]})
    assert doctor(episode_dir) == 1


def test_doctor_tolerates_key_base_drift_between_hook_and_corpus(tmp_path, monkeypatch, capsys):
    """兩邊 key 的正規化基準不同時，尾段吻合就算看到了。

    實測形狀：hook 曾以絕對路徑末 3 段記下
    `mind-door/ai-website/append-cards.js`，語料端是 `append-cards.js`。
    hook 明明看到了那次編輯，純相等比對卻把它報成遺漏——歷史紀錄裡
    這種 key 永遠修不回來，不吸收掉的話 doctor 永遠是紅的。
    """
    episode_dir = _touch_setup(
        tmp_path, monkeypatch,
        _touched_episode(["append-cards.js"]),
        [{"session_id": "sess-1", "prompt_id": "p1",
          "file_key": "mind-door/ai-website/append-cards.js"}],
    )
    assert doctor(episode_dir) == 0
    assert "漏看 0 輪" in capsys.readouterr().err


def test_doctor_is_quiet_when_the_hook_saw_everything(tmp_path, monkeypatch):
    episode_dir = _touch_setup(
        tmp_path, monkeypatch,
        _touched_episode(["src/a.ts", "src/b.ts"]),
        [{"session_id": "sess-1", "prompt_id": "p1", "file_key": "src/a.ts"},
         {"session_id": "sess-1", "prompt_id": "p1", "file_key": "src/b.ts"}],
    )
    assert doctor(episode_dir) == 0


def test_doctor_ignores_hook_observations_with_no_edit_in_the_corpus(tmp_path, monkeypatch):
    """反方向不是遺漏：hook 跑在編輯**之前**，工具被擋或失敗都會留下觀察卻沒有編輯。"""
    episode_dir = _touch_setup(
        tmp_path, monkeypatch,
        _touched_episode([]),
        [{"session_id": "sess-1", "prompt_id": "p1", "file_key": "src/a.ts"}],
    )
    assert doctor(episode_dir) == 0


def test_doctor_does_not_count_turns_the_corpus_has_not_caught_up_with(tmp_path, monkeypatch):
    """尾端待補的輪次語料裡還沒有，不能算進分母。

    算進去的話比率永遠難看，真正的故障就淹在裡面了——與空 assistant_text
    那條的取捨一致。
    """
    episode_dir = _touch_setup(
        tmp_path, monkeypatch,
        _touched_episode(["src/a.ts"]),
        [{"session_id": "sess-1", "prompt_id": "p1", "file_key": "src/a.ts"},
         {"session_id": "sess-1", "prompt_id": "p-later", "file_key": "src/z.ts"}],
    )
    assert doctor(episode_dir) == 0


# --- repo 歸屬凍結 ---------------------------------------------------------
# 2026-08-22 實際事故：AI-Website-API / AI-Website-Web 被改名，舊語料的 cwd 目錄
# 不存在了，`repo_root()` 往上找就撞到父層的 .git——739 輪的 repo 與檔案路徑基準
# 集體漂移，doctor 報 888 個誤報、夜間管線的 health 閘門連停三天。
# 這組測試走完整的 sync → 改名 → repair 路徑，不從中間切進去：
# 上一次的教訓正是「測試繞過路徑正規化那一段，前半段就沒有保護」。

def _repo_transcript(tmp_path: Path, repo: Path, turns: int = 3) -> Path:
    records = []
    for i in range(turns):
        pid = f"p{i}"
        records.append(_user(pid, origin={"kind": "human"}, cwd=str(repo)))
        records.append(_assistant(
            tools=[("Edit", {"file_path": str(repo / "src" / "a.py")})], cwd=str(repo)))
    t = tmp_path / "t.jsonl"
    t.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return t


def test_repo_root_is_recorded(tmp_path):
    """寫入當下就把基準存成欄位，之後不必再靠檔案系統推。"""
    repo = tmp_path / "parent" / "OldName"
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir()
    ep_dir = tmp_path / "eps"
    sync(_repo_transcript(tmp_path, repo), ep_dir, "sess")
    rec = json.loads(episode_path(ep_dir, "sess").read_text(encoding="utf-8").splitlines()[0])
    assert rec["repo"] == "OldName"
    assert Path(rec["repo_root"]) == repo
    assert rec["files_edited"] == ["src/a.py"]


def test_repo_rename_does_not_rewrite_history(tmp_path):
    """repo 改名後重建，repo 與檔案路徑都不能跟著漂。

    父目錄也放 .git：改名後 `repo_root(cwd)` 會往上撞到它，
    那正是事故當時把 AI-Website-API 全部標成 AI-Website 的機制。
    """
    parent = tmp_path / "parent"
    (parent / ".git").mkdir(parents=True)
    repo = parent / "OldName"
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir()

    ep_dir = tmp_path / "eps"
    transcript = _repo_transcript(tmp_path, repo)
    sync(transcript, ep_dir, "sess")

    (repo / ".git").rmdir()
    repo.rename(parent / "NewName")  # 改名：舊 cwd 從此不存在
    # 🚨 一定要清 cache，否則這個測試會說謊：`repo_root` 有 process 級 cache，
    # 同一支 process 裡 sync 已經算過這個 cwd，rename 後拿到的是快取的舊答案，
    # 於是**拿掉 pin 也照樣綠**（實測過）。真實事故發生在跨 process，沒有這層遮蔽。
    import transcript as _t
    _t._repo_root_cache.clear()

    total, fixed = repair(transcript, ep_dir, "sess")
    assert fixed == 0, "改名不該讓既有語料被判定成需要修正"
    rec = json.loads(episode_path(ep_dir, "sess").read_text(encoding="utf-8").splitlines()[0])
    assert rec["repo"] == "OldName"
    assert rec["files_edited"] == ["src/a.py"]


def test_backfill_infers_root_from_repo_and_cwd(tmp_path):
    """舊語料沒有這個欄位，從 repo + cwd 純字串推導——刻意不碰檔案系統。"""
    rec = {"prompt_id": "p1", "turn_index": 0, "repo": "OldName",
           "cwd": ["C:\\src\\parent\\OldName\\sub"],
           "files_edited": ["src/a.py"], "files_read": []}
    assert Path(hook_stop.infer_repo_root(rec)) == Path("C:/src/parent/OldName")


def test_backfill_skips_turns_that_had_no_root(tmp_path):
    """`repo` 也可能是「解析不出 root 時」退回的目錄名，那種輪次當時沒有基準。

    分辨方式純看資料：存檔裡還留著以推導 root 為前綴的絕對路徑，
    就證明當時沒有正規化過。硬補一個基準會讓重建把路徑改寫成相對——
    比存檔「更正確」，但那是改寫歷史，而且 doctor 會逐輪報不一致。
    """
    rec = {"prompt_id": "p1", "turn_index": 0, "repo": "Notes",
           "cwd": ["E:\\Documents\\Notes"],
           "files_edited": ["E:/Documents/Notes/INDEX.md"], "files_read": []}
    assert hook_stop.infer_repo_root(rec) is None
