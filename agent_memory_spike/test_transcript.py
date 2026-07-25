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

from hook_stop import (  # noqa: E402
    completed_episodes,
    episode_path,
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
    for name in tools:
        content.append({"type": "tool_use", "name": name, "input": {}})
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


def test_files_touched_from_file_history_delta():
    records = [
        _user("p1", origin={"kind": "human"}),
        {"type": "file-history-delta", "trackingPath": "C:/repo/proj/a.py",
         "timestamp": "2026-07-25T00:00:02.000Z"},
        {"type": "file-history-delta", "trackingPath": "C:/repo/proj/a.py",
         "timestamp": "2026-07-25T00:00:03.000Z"},
    ]
    ep = build_episode("p1", records)
    assert ep["files_touched"] == ["C:/repo/proj/a.py"]  # 去重


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
        "\n".join(json.dumps({"prompt_id": f"p{i}"}) for i in range(1, 6)) + "\n",
        encoding="utf-8",
    )
    ids = recorded_prompt_ids(p)
    assert ids == {"p1", "p2", "p3", "p4", "p5"}
    # 關鍵：第一筆也要被認出來，不能只看最後一筆
    assert "p1" in ids


def test_recorded_prompt_ids_tolerates_garbage(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text('{"prompt_id": "p1"}\nnot json\n{"prompt_id": "p2"}\n', encoding="utf-8")
    assert recorded_prompt_ids(p) == {"p1", "p2"}


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
    assert recorded_prompt_ids(episode_path(ep_dir, "sess")) == {"p1"}


def test_sync_is_incremental_across_calls(tmp_path):
    """後續輪次要能被自動補上，不必依賴 hook 每次都成功。"""
    ep_dir = tmp_path / "eps"
    sync(_write_transcript(tmp_path, 2), ep_dir, "sess")
    written, skipped = sync(_write_transcript(tmp_path, 4), ep_dir, "sess")
    assert written == 2 and skipped == 1
    assert recorded_prompt_ids(episode_path(ep_dir, "sess")) == {"p1", "p2", "p3"}


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
