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
import os
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


_repo_root_cache: dict[str, Path | None] = {}

# 帶檔案路徑的工具，以及路徑放在哪個參數裡。
# 分成「改」與「讀」兩組是刻意的：結構訊號要找的是反覆**修改**同一個檔案，
# 讀取沒有這個意涵——搜尋、確認、瀏覽都會讀，混在一起就淹掉了。
EDIT_TOOL_PATH_KEYS = {
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
READ_TOOL_PATH_KEYS = {
    "Read": "file_path",
}


def repo_root(cwd: str) -> Path | None:
    """從 cwd 往上找 ``.git``，回傳 repo 根目錄的路徑。

    不能直接用 ``Path(cwd).name``：bash 進入子目錄後 cwd 就是子目錄。
    實測把 Eternity 的 episode 標成 ``islands`` 和 ``pages``——那是元件目錄，
    不是 repo。聚合時會把同一個 repo 拆成好幾個，而且看起來完全像正常資料。

    有 cache 是因為同一個 session 內 cwd 高度重複，而這裡會碰檔案系統。
    """
    if cwd in _repo_root_cache:
        return _repo_root_cache[cwd]

    result: Path | None = None
    try:
        current = Path(cwd).resolve()
        for candidate in (current, *current.parents):
            if (candidate / ".git").exists():
                result = candidate
                break
    except OSError:
        pass

    _repo_root_cache[cwd] = result
    return result


def repo_root_name(cwd: str) -> str | None:
    """repo 根目錄名。"""
    root = repo_root(cwd)
    return root.name if root is not None else None


def normalize_path(raw: str, root: Path | None) -> str:
    """把檔案路徑正規化成 repo 相對、正斜線的形式。

    **兩個來源的路徑形狀不同**：``file-history-delta.trackingPath`` 已經是 repo 相對
    （``apps\\uep\\src\\...``），而 ``Edit.file_path`` 是絕對路徑
    （``C:\\Users\\...\\Eternity\\apps\\uep\\src\\...``）。不統一的話同一個檔案會有
    兩種表示，任何「同一檔案被反覆修改」的比對都必然失效——而那正是要找的訊號。

    刻意不用 ``resolve()``：那會碰檔案系統，``--sync-all`` 掃數百份 transcript 時
    成本可觀，而且檔案早已刪除時行為不一致。純字串比對就夠了。
    """
    if not raw:
        return ""
    if root is not None:
        try:
            normalized_root = os.path.normcase(os.path.normpath(str(root)))
            absolute = os.path.normpath(raw)
            if os.path.normcase(absolute).startswith(normalized_root + os.sep):
                return absolute[len(normalized_root) + 1:].replace("\\", "/")
        except (OSError, ValueError):
            pass
    return raw.replace("\\", "/")


# 比對鍵取路徑的最後幾段。3 段夠獨特（單靠檔名的話 `types.ts`、`index.ts`
# 會把不相干的檔案黏在一起），又短到不受前綴差異影響
FILE_KEY_SEGMENTS = 3


def file_key(path: str, segments: int = FILE_KEY_SEGMENTS) -> str:
    """把任意形式的檔案路徑收斂成一個穩定的比對鍵。

    **存在的理由是 `normalize_path` 的基準點會浮動。** 它切掉的是「往上最近的 `.git`」，
    但實測 AI-Website 是 nested git repos（父層與子層都有 `.git`），
    加上 bash 會切目錄，於是**同一個檔案在不同輪次被切成三種字串**：

        cwd=mind-door/AI-Website        → AI-Website-API/src/routes/compliance-v2/types.ts
        cwd=.../AI-Website-Web          → C:/Users/.../AI-Website-API/src/routes/compliance-v2/types.ts
        cwd=.../AI-Website-API          → src/routes/compliance-v2/types.ts

    而「同一檔案被反覆修改」是粗篩訊號與檔案訊號**共同的基礎**，
    路徑對不起來，那個比對就必然漏判。

    刻意只用於比對、不寫回語料：改 schema 就得 `--repair-all` 重建全部語料，
    而 `repo` / `scope` 是從同一個 root 推導的，動了它們，
    既有 concept 的 scope 會整批對不上。比對鍵是可以隨時重算的衍生值，
    語料裡的原始路徑保留溯源價值。
    """
    cleaned = (path or "").replace("\\", "/").strip("/")
    if not cleaned:
        return ""
    parts = [p for p in cleaned.split("/") if p and p != "."]
    return "/".join(parts[-segments:]).lower()


def file_keys(paths: list[str] | None) -> set[str]:
    """一組路徑的比對鍵集合。"""
    return {key for key in (file_key(p) for p in (paths or [])) if key}


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


def build_episode(prompt_id: str, records: list[dict[str, Any]], turn_index: int = 0) -> dict[str, Any]:
    """把一輪的記錄組裝成 episode。

    刻意記錄的東西與理由：

    - ``origin``：非 human 的輪次必須可辨識，否則 agent 通知會被當成使用者指示
    - ``cwd`` / ``git_branch`` 用 list：實測同一 session 內兩者都會變（切分支、bash 進子目錄），
      存單一值會失真
    - ``tool_sequence``：這輪實際做了什麼的骨架，``last_assistant_message`` 完全拿不到
    - ``files_edited`` / ``files_read``：從 ``tool_use`` 的路徑參數取，
      並補上 ``file-history-delta.trackingPath``（見下方註解，只靠後者會漏掉大半）
    - ``thinking_blocks`` 只存數量：內容是內部推理，體積大且無召回價值
    """
    user_texts: list[str] = []
    assistant_texts: list[str] = []
    tools: Counter[str] = Counter()
    mcp_tools: set[str] = set()
    skills: set[str] = set()
    # 先收原始路徑，等 cwds 收集完、repo root 確定後才正規化
    raw_edited: list[str] = []
    raw_read: list[str] = []
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
            if isinstance(path, str):
                raw_edited.append(path)
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
                            # file-history-delta 只涵蓋一部分編輯：實測抽樣 6 份 transcript
                            # 有 279 次 Edit/Write 卻只有 62 筆 delta 記錄，
                            # 1372 輪語料中僅 409 輪有檔案資料，而 Edit 共出現 5996 次。
                            # 少掉的部分不會報錯，只會讓「同一檔案被反覆修改」這類訊號
                            # 量出接近零的結果（實測相鄰輪重疊檔案只有 1 筆）。
                            # tool_use 的參數才是完整來源。
                            payload = block.get("input")
                            if isinstance(payload, dict):
                                for keys, bucket in (
                                    (EDIT_TOOL_PATH_KEYS, raw_edited),
                                    (READ_TOOL_PATH_KEYS, raw_read),
                                ):
                                    key = keys.get(name)
                                    value = payload.get(key) if key else None
                                    if isinstance(value, str) and value:
                                        bucket.append(value)
                    elif btype == "thinking":
                        thinking_count += 1
            text = _text_from_content(content)
            if text.strip():
                assistant_texts.append(text)

    # 逐一嘗試，取第一個解析得出 repo 根的 cwd——
    # 同一輪的 cwd 若都在同一個 repo 內，結果一致；解析不出來才退回目錄名
    root: Path | None = None
    for candidate in cwds:
        root = repo_root(candidate)
        if root is not None:
            break
    repo: str | None = root.name if root is not None else (Path(cwds[0]).name if cwds else None)

    def _dedup(paths: list[str]) -> list[str]:
        # 順序有意義（同一輪內的編輯順序），所以不能直接用 set
        seen: set[str] = set()
        result: list[str] = []
        for raw in paths:
            normalized = normalize_path(raw, root)
            if normalized and normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
        return result

    files_edited = _dedup(raw_edited)
    files_read = _dedup(raw_read)

    return {
        "prompt_id": prompt_id,
        # promptId 不足以當唯一鍵：session 起始的 meta 注入在每次 resume 時會重新出現，
        # 且沿用同一個 promptId——實測某個 id 在 7/30 和 8/02 各出現一次，內容不同。
        # 加上這輪在 session 內的序號才構成唯一鍵，
        # 而 resume 產生的完整複本序號一致，所以跨 session 去重仍然有效。
        "turn_index": turn_index,
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
        "files_edited": files_edited,
        "files_read": files_read,
        "thinking_blocks": thinking_count,
    }


def episodes_from_transcript(path: Path) -> list[dict[str, Any]]:
    """讀整份 transcript，回傳所有 episode。"""
    records = load_records(path)
    return [
        build_episode(pid, group, turn_index=i)
        for i, (pid, group) in enumerate(iter_prompt_groups(records))
    ]


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
