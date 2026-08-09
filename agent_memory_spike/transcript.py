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

import hashlib
import json
import os
import re
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

# 注入紀錄的 side-car。**注入 hook 自己寫，不從 transcript 反推**——
# additionalContext 在 transcript 裡的形狀沒有保證，靠猜會得到一個
# 「看起來正常但其實對不上」的欄位，那正是這個專案反覆踩到的坑。
INJECTION_LOG = Path.home() / ".claude" / "agent-memory-spike" / "injections.jsonl"

# 注入 hook 每次看到一個編輯目標就記一行。**這是「hook 有沒有漏看」的唯一依據。**
#
# 實測發生過一次：一個 Write 沒被記進 `inject_state` 的 touched，
# 隔離環境重跑三次都正常，複現不出、根因不明。`inject_state` 只保留當前輪
# （換 prompt_id 就重置），所以事後完全無從對帳——那次遺漏是靠人眼看出來的。
#
# 這份 append-only 的紀錄讓 doctor 能比對「語料說這輪改了哪些檔案」與
# 「hook 說它看到了哪些」。抓不到根因至少要抓得到症狀，
# 不然掛上全域之後同型的遺漏只會安靜地累積。
TOUCH_LOG = Path.home() / ".claude" / "agent-memory-spike" / "touches.jsonl"

# SessionStart 注入用的哨兵 prompt_id。那個觸發點在第一輪之前就跑完，
# payload 裡根本沒有 prompt_id，而它的影響及於整個 session 而非某一輪。
# 刻意選一個真實 UUID 不可能長成的樣子，避免與正常的 promptId 相撞。
SESSION_WIDE_PROMPT_ID = "__session__"

# 以指紋歸屬時的鍵前綴，同樣是為了不與真實 promptId 相撞。
FINGERPRINT_KEY_PREFIX = "__fp__:"


def prompt_fingerprint(text: str) -> str:
    """使用者輸入的指紋。

    **不再是注入紀錄的鍵**——查證後 PreToolUse 的 payload 直接帶 ``prompt_id``，
    那比指紋精確：指紋要求兩邊對 ``user_text`` 的組法完全一致，
    而 episode 的 user_text 是一輪內多筆 user 記錄 join 起來的，hook 看到的未必相同。
    留著這個函式是因為別的地方（跨 session 去重）也在用同樣的手法。
    """
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


def load_injections(path: Path = INJECTION_LOG) -> dict[tuple[str, str], list[str]]:
    """讀注入紀錄，鍵是 (session_id, prompt_id)。

    同一個 promptId 在一份 transcript 裡可能出現兩次（session 起始的 meta 注入
    每次 resume 重現且沿用同一個 id），所以這把鍵會讓那種情況多標記一輪。
    那是保守的方向——寧可多標記幾輪為「可能被影響」，也不要漏標而把
    被污染的輪次當成乾淨語料。

    **為什麼需要這個欄位**：注入記憶之後，語料就變成「已被記憶影響過的行為」，
    再拿它校準 surprisal 會有系統性偏誤——這是 hook 遲遲不掛的唯一理由。
    但問題不在注入本身，在於**分不出哪些輪次被影響過**。
    標記起來，校準時就能排除，同時這還是比實驗室注入實驗更真實的線上效果資料。
    """
    if not path.exists():
        return {}
    found: dict[tuple[str, str], list[str]] = {}
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # UserPromptSubmit 的 payload 不保證帶 prompt_id（PreToolUse 帶，
                # 那是查證過的；這個沒有）。沒有 prompt_id 就無法歸屬到某一輪，
                # 而「注入了卻標記不到」正是這整套追蹤要防的事，所以退回指紋。
                # 指紋的已知弱點是兩邊對 user_text 的組法必須一致——
                # 寧可用比較弱的鍵，也不要沒有鍵。
                raw_prompt_id = record.get("prompt_id")
                if raw_prompt_id:
                    key = (str(record.get("session_id")), str(raw_prompt_id))
                elif record.get("prompt_fingerprint"):
                    key = (str(record.get("session_id")),
                           FINGERPRINT_KEY_PREFIX + str(record["prompt_fingerprint"]))
                else:
                    continue
                # 同一輪可能被注入多次（一輪會改好幾個檔案，每次 PreToolUse 都召回一批），
                # 累積而不是覆蓋——漏掉任何一條都會讓「這輪看過什麼」失真
                merged = found.setdefault(key, [])
                for concept_id in record.get("injected") or []:
                    if concept_id not in merged:
                        merged.append(concept_id)
    except OSError:
        return {}
    return found


def load_touches(path: Path = TOUCH_LOG) -> dict[tuple[str, str], set[str]]:
    """讀 hook 的觀察紀錄，鍵是 (session_id, prompt_id)，值是 file_key 集合。

    值存的是 **file_key（末幾段）而不是完整路徑**：hook 拿到的是絕對路徑、
    語料存的是 repo 相對路徑，兩邊唯一能對齊的就是這個鍵。
    這正是先前檔案錨點完全失效的那個坑——同名的量不一定是同一個量。
    """
    if not path.exists():
        return {}
    found: dict[tuple[str, str], set[str]] = {}
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                session_id = record.get("session_id")
                prompt_id = record.get("prompt_id")
                key = record.get("file_key")
                if not session_id or not prompt_id or not key:
                    continue
                found.setdefault((str(session_id), str(prompt_id)), set()).add(str(key))
    except OSError:
        return {}
    return found


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

# 從編輯內容裡抽識別符：一般程式碼識別符，加上 CSS 自訂屬性（--uep-island-z 那種）。
# 3 字元起跳是為了濾掉 `id`、`fn` 這類短到無法辨識來源的東西。
_IDENTIFIER_RE = re.compile(r"--[A-Za-z][\w-]{2,}|[A-Za-z_][A-Za-z0-9_]{2,}")

# 每輪存的識別符上限。一次大改可以動到上千個符號，全存會讓 episode 檔膨脹，
# 而排在後面的多半是同一段程式碼的重複用字，資訊量遞減
MAX_SYMBOLS_PER_TURN = 300


def extract_symbols(payload: dict[str, Any]) -> list[str]:
    """從一次編輯的參數裡抽出被碰到的識別符。

    **為什麼要存這個**：`anchors` 現在的粒度到函式名與欄位名
    （`hasSufficientCache`、`--uep-island-z`），但 episode 原本只存檔案路徑，
    比對是集合交集，於是**符號級錨點永遠不可能命中**——
    要求蒸餾者把粒度做細，卻沒有同步改比對的另一邊。

    **只存符號、不存原文**是刻意的：編輯內容體積大，而且會含機敏資訊
    （語料涵蓋商業專案），而比對只需要符號本身。

    不過濾常見關鍵字（`return`、`const`）：那會變成一份硬編碼清單，
    而比對的另一邊是 `anchors`——蒸餾者不會把 `return` 當錨點，
    所以雜訊符號單純不會被查詢到，不需要在寫入端處理。
    """
    chunks: list[str] = []
    for key in ("old_string", "new_string", "content", "new_source"):
        value = payload.get(key)
        if isinstance(value, str):
            chunks.append(value)
    # MultiEdit 的編輯放在陣列裡，形狀跟單次 Edit 不同
    for edit in payload.get("edits") or []:
        if isinstance(edit, dict):
            for key in ("old_string", "new_string"):
                value = edit.get(key)
                if isinstance(value, str):
                    chunks.append(value)

    symbols: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        for match in _IDENTIFIER_RE.findall(chunk):
            if match not in seen:
                seen.add(match)
                symbols.append(match)
                if len(symbols) >= MAX_SYMBOLS_PER_TURN:
                    return symbols
    return symbols


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


def build_episode(prompt_id: str, records: list[dict[str, Any]], turn_index: int = 0,
                  injections: dict[tuple[str, str], list[str]] | None = None) -> dict[str, Any]:
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
    raw_symbols: list[str] = []
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
                                if name in EDIT_TOOL_PATH_KEYS:
                                    # 符號級錨點要比對的另一邊。只在編輯工具上抽，
                                    # 讀取不算——理由同 files_read 的分流
                                    raw_symbols.extend(extract_symbols(payload))
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

    def _dedup_symbols(symbols: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for symbol in symbols:
            if symbol not in seen:
                seen.add(symbol)
                result.append(symbol)
                if len(result) >= MAX_SYMBOLS_PER_TURN:
                    break
        return result

    files_edited = _dedup(raw_edited)
    files_read = _dedup(raw_read)

    user_text = "\n\n".join(user_texts)
    # 空 list 與缺欄位要分得開：前者是「這輪沒被注入」，後者是「這筆語料早於這個 schema」。
    # 舊語料停在舊格式而沒有任何標示，是先前踩過的坑
    injected = list((injections or {}).get((str(session_id), str(prompt_id)), []))
    # SessionStart 的注入沒有 prompt_id——它在第一輪之前就發生，影響的是整個 session。
    # 用哨兵鍵記錄，這裡展開到該 session 的每一輪：注入的記憶留在 context 裡，
    # 第五輪跟第一輪一樣看得到它。少標記任何一輪，那輪就會被當成乾淨語料拿去校準。
    for concept_id in (injections or {}).get((str(session_id), SESSION_WIDE_PROMPT_ID), []):
        if concept_id not in injected:
            injected.append(concept_id)
    # 沒有 prompt_id 的注入來源（UserPromptSubmit）改以使用者輸入的指紋歸屬。
    # 這裡的 user_text 是一輪內多筆 user 記錄 join 起來的，而 hook 只看得到
    # 當下那一筆——兩者不一致時就對不上，是這把鍵已知且未解的弱點。
    fingerprint_key = (str(session_id), FINGERPRINT_KEY_PREFIX + prompt_fingerprint(user_text))
    for concept_id in (injections or {}).get(fingerprint_key, []):
        if concept_id not in injected:
            injected.append(concept_id)

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
        "user_text": user_text,
        "assistant_text": "\n\n".join(assistant_texts),
        # 這輪開始前注入了哪幾條記憶。校準時要排除非空的輪次——
        # 它們是「已被記憶影響過的行為」，拿來量 surprisal 會系統性偏低
        "injected": injected,
        "tool_sequence": [{"name": n, "count": c} for n, c in tools.most_common()],
        "tool_calls_total": sum(tools.values()),
        "mcp_tools": sorted(mcp_tools),
        "skills": sorted(skills),
        "files_edited": files_edited,
        "files_read": files_read,
        # 這輪碰過的識別符。存在的理由是 anchors 的粒度到函式名與欄位名，
        # 而檔案路徑比對不到那一層——見 extract_symbols
        "symbols_edited": _dedup_symbols(raw_symbols),
        "thinking_blocks": thinking_count,
    }


def episodes_from_transcript(path: Path,
                             injections: dict[tuple[str, str], list[str]] | None = None
                             ) -> list[dict[str, Any]]:
    """讀整份 transcript，回傳所有 episode。

    ``injections`` 傳 None 時自己載入。呼叫端要跑幾百份 transcript 時
    （``--sync-all`` / ``--repair-all`` / ``--doctor``）該自己載入一次傳進來。
    """
    if injections is None:
        injections = load_injections()
    records = load_records(path)
    return [
        build_episode(pid, group, turn_index=i, injections=injections)
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
