"""Phase 1：把 Claude Code 的 transcript 切成 coding 版的 episode。

為什麼不能直接用 Stop hook 的 ``last_assistant_message``：
一輪對話在 coding agent 裡可能是幾十次 tool call 加最後一段結論，
``last_assistant_message`` 只給得到那段結論，中間做了什麼全部遺失。
所以這裡自己讀 ``transcript_path`` 重建。

維持零第三方依賴，理由同 hook_session_start.py（冷啟動延遲要可控）。

實測 transcript 結構（Claude Code 2.1.216）::

    373 行 / type 分布：assistant 152、user 87、其餘為 metadata
    assistant 的 content block：tool_use 82、thinking 40、text 30
    user 的 content：tool_result 81、真正的文字輸入僅 6 筆

也就是說 ``user`` 型記錄裡有 93% 不是使用者說的話。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterator

# 這輪內容的來源。**區分 human 與其他來源是必要的**：
# 背景 agent 完成時會以 user 記錄的形式送進 task-notification（實測有一筆 3402 字元），
# 若當成使用者指示存進記憶，等於把 agent 自己的輸出偽裝成使用者的要求。
# 產生記憶的 harness。Codex 的 rollout jsonl 有同構的問題——
# 實測一份 session：event_msg.payload.type=="user_message" 有 5 筆，
# 但 response_item.role=="user" 有 6 筆，多的那筆是系統注入的 environment_context。
# 也就是說 Codex 的 role 欄位一樣不可信，可信訊號是 event_msg.user_message。
AGENT_CLAUDE_CODE = "claude-code"

ORIGIN_HUMAN = "human"
ORIGIN_TASK_NOTIFICATION = "task-notification"
ORIGIN_SYSTEM = "system"
ORIGIN_META = "meta"
ORIGIN_UNKNOWN = "unknown"


_repo_root_cache: dict[str, str | None] = {}


def repo_root_name(cwd: str) -> str | None:
    """從 cwd 往上找 ``.git``，回傳 repo 根目錄名。

    不能直接用 ``Path(cwd).name``：bash 進入子目錄後 cwd 就是子目錄。
    實測把 Eternity 的 episode 標成 ``islands`` 和 ``pages``——那是元件目錄，
    不是 repo。聚合時會把同一個 repo 拆成好幾個，而且看起來完全像正常資料。

    有 cache 是因為同一個 session 內 cwd 高度重複，而這裡會碰檔案系統。
    """
    if cwd in _repo_root_cache:
        return _repo_root_cache[cwd]

    result: str | None = None
    try:
        current = Path(cwd).resolve()
        for candidate in (current, *current.parents):
            if (candidate / ".git").exists():
                result = candidate.name
                break
    except OSError:
        pass

    _repo_root_cache[cwd] = result
    return result


def load_records(path: Path) -> list[dict[str, Any]]:
    """讀 jsonl。

    容錯是刻意的：Stop hook 觸發時 transcript 的尾端可能還沒 flush 完，
    最後一行有機會是半截的 JSON。這種情況下丟掉那一行即可，不該讓 hook 失敗。
    """
    records: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return records


def classify_origin(record: dict[str, Any]) -> str:
    """判斷一筆 user 記錄的真實來源。

    實測到的組合：
    - ``promptSource='typed'`` + ``origin={'kind':'human'}``  → 使用者真的打的字
    - ``promptSource='system'`` + ``origin={'kind':'task-notification'}`` → 背景 agent 回報
    - ``isMeta=True`` → 系統注入的 meta 訊息
    - 三者皆無 → session 起始注入（CLAUDE.md / hook additionalContext 之類）
    """
    if record.get("isMeta"):
        return ORIGIN_META

    origin = record.get("origin")
    if isinstance(origin, dict):
        kind = origin.get("kind")
        if kind == "human":
            return ORIGIN_HUMAN
        if kind:
            return str(kind)

    source = record.get("promptSource")
    if source == "typed":
        return ORIGIN_HUMAN
    if source == "system":
        return ORIGIN_SYSTEM
    if source:
        return str(source)

    return ORIGIN_UNKNOWN


def _text_from_content(content: Any) -> str:
    """把 message.content 拉平成純文字，略過 thinking 與 tool 相關 block。

    thinking 內容刻意不取：體積大，而且是模型的內部推理，不是這輪的產出。
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def is_tool_result(record: dict[str, Any]) -> bool:
    """tool_result 是以 user 記錄的形式存在的，不是使用者輸入。"""
    return record.get("toolUseResult") is not None


def iter_prompt_groups(records: list[dict[str, Any]]) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """按 promptId 切輪，保持原始順序。

    只有 user 型記錄帶 promptId；assistant 記錄要靠位置歸屬到當前這輪。
    """
    current_id: str | None = None
    current: list[dict[str, Any]] = []

    for record in records:
        prompt_id = record.get("promptId")
        if prompt_id and prompt_id != current_id:
            if current_id is not None:
                yield current_id, current
            current_id = prompt_id
            current = []
        if current_id is not None:
            current.append(record)

    if current_id is not None and current:
        yield current_id, current


def build_episode(prompt_id: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    """把一輪的記錄組裝成 episode。

    刻意記錄的東西與理由：

    - ``origin``：非 human 的輪次必須可辨識，否則 agent 通知會被當成使用者指示
    - ``cwd`` / ``git_branch`` 用 list：實測同一 session 內兩者都會變（切分支、bash 進子目錄），
      存單一值會失真
    - ``tool_sequence``：這輪實際做了什麼的骨架，``last_assistant_message`` 完全拿不到
    - ``files_touched``：直接取自 ``file-history-delta.trackingPath``，不必另外呼叫 git
    - ``thinking_blocks`` 只存數量：內容是內部推理，體積大且無召回價值
    """
    user_texts: list[str] = []
    assistant_texts: list[str] = []
    tools: Counter[str] = Counter()
    mcp_tools: set[str] = set()
    skills: set[str] = set()
    files: list[str] = []
    cwds: list[str] = []
    branches: list[str] = []
    thinking_count = 0
    timestamps: list[str] = []
    origin = ORIGIN_UNKNOWN
    cc_version: str | None = None
    session_id: str | None = None

    for record in records:
        rtype = record.get("type")

        for key, bucket in (("cwd", cwds), ("gitBranch", branches)):
            value = record.get(key)
            if isinstance(value, str) and value not in bucket:
                bucket.append(value)

        cc_version = cc_version or record.get("version")
        session_id = session_id or record.get("sessionId")

        ts = record.get("timestamp")
        if isinstance(ts, str):
            timestamps.append(ts)

        if rtype == "file-history-delta":
            path = record.get("trackingPath")
            if isinstance(path, str) and path not in files:
                files.append(path)
            continue

        if rtype == "user":
            if is_tool_result(record):
                continue
            # 一輪裡第一筆非 tool_result 的 user 記錄決定這輪的來源
            if origin == ORIGIN_UNKNOWN:
                origin = classify_origin(record)
            text = _text_from_content(record.get("message", {}).get("content"))
            if text.strip():
                user_texts.append(text)
            continue

        if rtype == "assistant":
            for name_key, bucket in (
                ("attributionMcpTool", mcp_tools),
                ("attributionSkill", skills),
            ):
                value = record.get(name_key)
                if isinstance(value, str):
                    bucket.add(value)

            content = record.get("message", {}).get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "tool_use":
                        name = block.get("name")
                        if isinstance(name, str):
                            tools[name] += 1
                    elif btype == "thinking":
                        thinking_count += 1
            text = _text_from_content(content)
            if text.strip():
                assistant_texts.append(text)

    # 逐一嘗試，取第一個解析得出 repo 根的 cwd——
    # 同一輪的 cwd 若都在同一個 repo 內，結果一致；解析不出來才退回目錄名
    repo: str | None = None
    for candidate in cwds:
        repo = repo_root_name(candidate)
        if repo:
            break
    if repo is None and cwds:
        repo = Path(cwds[0]).name

    return {
        "prompt_id": prompt_id,
        "session_id": session_id,
        # 產生這筆記憶的 harness。現在只有一個值，但先佔位——
        # Codex 的 hooks schema 幾乎照搬 Claude Code，接同一套記憶是可行的，
        # 屆時沒有這個欄位就無法分辨記憶來自哪個 agent，而那是必要的：
        # 不同模型的知識邊界不同，同一條記憶對它們的價值也不同。
        # 現在加成本為零，等資料累積起來再加就要 migrate。
        "agent": AGENT_CLAUDE_CODE,
        "origin": origin,
        "started_at": timestamps[0] if timestamps else None,
        "ended_at": timestamps[-1] if timestamps else None,
        "cwd": cwds,
        "repo": repo,
        "git_branch": branches,
        "cc_version": cc_version,
        "user_text": "\n\n".join(user_texts),
        "assistant_text": "\n\n".join(assistant_texts),
        "tool_sequence": [{"name": n, "count": c} for n, c in tools.most_common()],
        "tool_calls_total": sum(tools.values()),
        "mcp_tools": sorted(mcp_tools),
        "skills": sorted(skills),
        "files_touched": files,
        "thinking_blocks": thinking_count,
    }


def episodes_from_transcript(path: Path) -> list[dict[str, Any]]:
    """讀整份 transcript，回傳所有 episode。"""
    records = load_records(path)
    return [build_episode(pid, group) for pid, group in iter_prompt_groups(records)]


def episode_for_prompt(path: Path, prompt_id: str) -> dict[str, Any] | None:
    """只取指定 promptId 的那一輪。

    Stop hook 的 payload 帶有 ``prompt_id``，所以不需要重建整份 transcript——
    每輪都全量重算的話，長 session 的成本會隨輪數線性上升。
    """
    records = load_records(path)
    for pid, group in iter_prompt_groups(records):
        if pid == prompt_id:
            return build_episode(pid, group)
    return None
