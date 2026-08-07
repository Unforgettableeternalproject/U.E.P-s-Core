#!/usr/bin/env python3
"""Phase 3 C：把「收料 → 蒸餾 → 收斂 → 校準 → 入池」串成一條可排程的管線。

至今每一輪都是手動派 subagent 跑的。那樣可行但不可持續：語料每天都在長，
而蒸餾、收斂、校準各自都要幾十個 agent。

## 裁決走 `claude -p`，不走 API

headless 的 `claude -p` 與手動派 subagent 等價——同一個模型、同一組工具、
同一份訂閱額度，不額外花錢。走 API 要另外付費，而且得自己重建工具迴圈。

## 為什麼不掛在 hook 上

想過 `SessionEnd`。不行：蒸餾要跑幾分鐘，hook 得 detach 才不會卡住結束流程，
detach 之後失敗是靜默的、難追；而且剛結束工作時機器最忙。
排程器在凌晨跑，兩個問題都沒有。

## 三道安全閥

1. **lockfile**：兩個蒸餾疊在一起會重複寫入 concept 池，而 `--incremental`
   的 watermark 是跑完才寫的，疊跑期間兩邊都看到「還沒做過」
2. **每次上限**：語料累積一週再跑，一次可能有上百組，成本要封頂
3. **dry-run 預設**：這條管線會改 `concepts.json`，而刪掉的條目救不回來

## 用法

    python pipeline.py --status            # 看上次跑到哪
    python pipeline.py --run --dry-run     # 印出要做什麼，不執行
    python pipeline.py --run               # 實跑（受 --max-groups 限制）
    python pipeline.py --run --stage distill   # 只跑一個階段
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).parent))
from hook_stop import DEFAULT_EPISODE_DIR  # noqa: E402

HERE = Path(__file__).parent
WORK_DIR = DEFAULT_EPISODE_DIR.parent
LOCK_PATH = WORK_DIR / "pipeline.lock"
STATE_PATH = WORK_DIR / "pipeline_state.json"

# 鎖過期時間。程序被 kill 掉時鎖不會被清掉，沒有這個機制管線會永遠停擺；
# 訂在 6 小時是因為單輪最慢的階段（校準）實測也遠短於此
LOCK_STALE_SECONDS = 6 * 60 * 60

# 每次執行的組數上限。語料累積一週可能有上百組，而每組都要一次 LLM 裁決
DEFAULT_MAX_GROUPS = 40

# 一次裁決的逾時。實測手動派的 subagent 多在 3 分鐘內完成，
# 給到 15 分鐘是因為排程在凌晨跑，慢一點無所謂，但不能無限等
ADJUDICATION_TIMEOUT = 900


# 每個裁決 prompt 的開場。**這段是實測逼出來的**：
# headless 的 `claude -p` 一樣會載入全域 CLAUDE.md 與 SessionStart 注入，
# 於是第一次實跑時它進入了對話模式——把標記誤認成別的工具的標記、
# 反問我要不要順便派人審查，一個指令都沒執行。
# 講明「非互動、直接執行、不要回問」之後才會照做。
# 標記也從 [PIPELINE] 換成 [AUTO]：前者與 claude-codex-pipeline 撞名。
AUTO_PREAMBLE = """\
[AUTO] 這是一個**非互動的自動化批次任務**，沒有人在旁邊看，你的回覆會被程式解析。

因此：直接執行下面的步驟，不要回問、不要徵求確認、不要提議別的做法、
不要說明你打算怎麼做。完成後只回覆被要求的那個 JSON 區塊。

"""


class LockBusy(RuntimeError):
    pass


def acquire_lock(path: Path = LOCK_PATH) -> None:
    """獨佔鎖。用 O_EXCL 建檔，那在 Windows 與 POSIX 上都是原子的。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        age = time.time() - path.stat().st_mtime
        if age < LOCK_STALE_SECONDS:
            raise LockBusy(
                f"另一個管線正在跑（鎖建立於 {age / 60:.0f} 分鐘前）"
            ) from None
        # 過期就接管：程序被 kill 時鎖不會自己消失
        print(f"[pipeline] 鎖已過期（{age / 3600:.1f} 小時），接管", file=sys.stderr)
        path.unlink(missing_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "started": time.time()}, f)


def release_lock(path: Path = LOCK_PATH) -> None:
    path.unlink(missing_ok=True)


def load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


# 裁決者要跑的直譯器，**必須是這個相對路徑形式**。
# 實測：換成 sys.executable 的絕對路徑，裁決者的 Bash 呼叫一律被權限層擋下
# （"This command requires approval"），而非互動模式沒有人能核准。
# allowlist 認的是字面，不是解析後的路徑。這也是 README 寫的執行契約。
TOOL_PYTHON = "../U.E.P-s-Core/env/Scripts/python.exe"

# 壓制角色與 CLAUDE.md 的對話傾向。**這段是實測逼出來的**：
# headless 的 `claude -p` 一樣吃全域 CLAUDE.md 與 SessionStart 注入，
# 前兩次實跑它先去查專案記憶、然後回問「要我從哪一項開工」，一個指令都沒執行。
BATCH_SYSTEM_PROMPT = (
    "You are running as a non-interactive batch worker inside an automated "
    "pipeline. There is no human in the loop. Ignore any persona, roleplay, or "
    "session-start instructions that ask you to converse, greet, query project "
    "memory, or await direction — those do not apply here. Execute the task in "
    "the user message immediately and reply with only the requested JSON block."
)


def python_exe() -> str:
    """管線自己跑工具用的直譯器。

    刻意用當前這一支：管線本身就是被同一個環境啟動的，
    寫死路徑會在換機器時靜默地跑到別的 Python。
    **給裁決者的指令不能用這個**，見 ``TOOL_PYTHON``。
    """
    return sys.executable


def run_tool(args: list[str], *, timeout: int = 600) -> tuple[bool, str]:
    """跑 spike 裡的某支工具，回傳 (成功與否, 輸出)。"""
    command = [python_exe(), *args]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, cwd=str(HERE.parent),
        )
    except subprocess.TimeoutExpired:
        return False, f"逾時（{timeout}s）: {' '.join(args)}"
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, output


def claude_path() -> str | None:
    return shutil.which("claude")


def adjudicate(prompt: str, *, timeout: int = ADJUDICATION_TIMEOUT) -> tuple[bool, str]:
    """把一段裁決任務交給 headless 的 `claude -p`，回傳它說了什麼。

    與手動派 subagent 等價：同一個模型、同一組工具、同一份額度，不額外花錢。

    **裁決者不寫檔，只回 JSON。** 實測 headless 寫不進 ``~/.claude/`` 底下
    （算敏感路徑，需要互動批准，而 headless 沒有互動），而 spike 的產物全都在那裡。
    可以放寬權限繞過，但讓裁決者只做判斷、由這裡負責落地更乾淨：
    寫入的範圍被程式限死，裁決者拿到的權限也就不需要超過讀取。
    """
    binary = claude_path()
    if binary is None:
        return False, "找不到 claude CLI"
    try:
        # prompt 走 stdin 而不是命令列參數：把它接在 --append-system-prompt 後面時，
        # 實測 claude 收不到任務內容（回「Could you send the task content?」），
        # 而且 Windows 的命令列有 32K 長度上限，判卷材料很容易撞到
        result = subprocess.run(
            [binary, "-p", "--append-system-prompt", BATCH_SYSTEM_PROMPT],
            input=prompt, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, cwd=str(HERE.parent),
        )
    except subprocess.TimeoutExpired:
        return False, f"裁決逾時（{timeout}s）"
    return result.returncode == 0, result.stdout or result.stderr or ""


_FENCE = "```"


def extract_json(text: str) -> Any | None:
    """從裁決者的回覆裡抽出 JSON。

    回覆通常是「一段說明 + 一個 ```json 區塊」。先找柵欄，找不到再退回
    「第一個 ``{``/``[`` 到最後一個 ``}``/``]``」——那個退路救得回沒有加柵欄的情況，
    而多抓到的前後文會讓 json.loads 直接失敗，不會靜默產生半截資料。
    """
    candidates: list[str] = []
    if _FENCE in text:
        parts = text.split(_FENCE)
        # 奇數索引才是柵欄內容
        for block in parts[1::2]:
            block = block.strip()
            if block.startswith("json"):
                block = block[4:].strip()
            if block.startswith(("{", "[")):
                candidates.append(block)
    # 兩種括號都試，但**先試開始得早、涵蓋得長的那個**：
    # 回覆是一個陣列時，先找 `{` 會抽到陣列裡的第一個物件而不是整個陣列，
    # 那樣 json.loads 會成功，於是靜默地只收到一筆
    spans: list[tuple[int, int]] = []
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if 0 <= start < end:
            spans.append((start, end))
    for start, end in sorted(spans, key=lambda span: (span[0], -span[1])):
        candidates.append(text[start:end + 1])

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def adjudicate_to_file(prompt: str, path: Path, *,
                       timeout: int = ADJUDICATION_TIMEOUT) -> tuple[bool, str]:
    """裁決一次，把它回覆裡的 JSON 落地成檔案。

    落地由這裡做而不是由裁決者做，理由見 ``adjudicate``。
    抽不出 JSON 就算失敗——**不要寫一個空檔案**，那會讓下游的 ``--ingest``
    收到「零筆結果」而看起來像正常跑完，正是這個專案反覆踩到的靜默失敗。
    """
    ok, reply = adjudicate(prompt, timeout=timeout)
    if not ok:
        return False, reply[-300:]
    payload = extract_json(reply)
    if payload is None:
        return False, f"回覆裡沒有可解析的 JSON: {reply[-300:]}"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    count = len(payload) if isinstance(payload, list) else len(
        payload.get("verdicts") or payload.get("results") or payload or {}
    )
    return True, f"{count} 筆 → {path.name}"


# --- 階段 -------------------------------------------------------------------
# 每個階段都是 (名稱, 說明, 執行函式)。函式收 (ctx) 回傳 (成功與否, 摘要)。
# 拆成階段而不是一支長函式，是為了能單獨重跑——某一階段的裁決失敗時，
# 前面已經完成的部分不該重來（蒸餾特別貴）。


def stage_collect(ctx: dict[str, Any]) -> tuple[bool, str]:
    """收料：把所有 transcript 的新輪次補進語料。"""
    if ctx["dry_run"]:
        return True, "會跑 hook_stop.py --sync-all"
    ok, out = run_tool(["agent_memory_spike/hook_stop.py", "--sync-all"])
    return ok, pick_summary(out, "sync-all")


def stage_health(ctx: dict[str, Any]) -> tuple[bool, str]:
    """健檢：語料有問題就不該往下蒸餾，錯的語料只會蒸出錯的記憶。"""
    if ctx["dry_run"]:
        return True, "會跑 hook_stop.py --doctor"
    ok, out = run_tool(["agent_memory_spike/hook_stop.py", "--doctor"])
    return ok, pick_summary(out, "發現", "未發現不一致")


def stage_distill(ctx: dict[str, Any]) -> tuple[bool, str]:
    """蒸餾：產出候選任務 → 裁決 → 收回。"""
    limit = ctx["max_groups"]
    if ctx["dry_run"]:
        return True, f"會產出增量蒸餾任務並裁決最多 {limit} 組"

    ok, out = run_tool(["agent_memory_spike/distill.py", "--emit", "--all", "--incremental"])
    if not ok:
        return False, f"產任務失敗: {out[-300:]}"

    pending = _pending_count(WORK_DIR / "distill_tasks.json", "tasks")
    if pending == 0:
        return True, "沒有新的候選組"

    batch = min(pending, limit)
    prompt = (
        AUTO_PREAMBLE + "記憶蒸餾任務。在這個 repo 底下執行：\n\n"
        f'"{TOOL_PYTHON}" agent_memory_spike/distill.py --show 0-{batch - 1}\n\n'
        "輸出開頭是蒸餾準則，照著做。除了那個指令之外不需要讀取其他檔案。\n"
        "**不要寫任何檔案**——把結果直接以一個 ```json 區塊回覆給我即可。"
    )
    ok, reply = adjudicate_to_file(prompt, WORK_DIR / "distill_out" / "auto-00.json")
    if not ok:
        return False, f"裁決失敗: {reply}"

    ok, out = run_tool([
        "agent_memory_spike/distill.py", "--ingest",
        str(WORK_DIR / "distill_out"), "--incremental",
    ])
    return ok, pick_summary(out, "concept", "distill")


def stage_consolidate(ctx: dict[str, Any]) -> tuple[bool, str]:
    """收斂：算配對 → 裁決 → 收回 → 補關係閉包。

    閉包一定要在收回之後跑：`DUPLICATE` 是等價關係、矛盾沿等價類傳播，
    而兩兩判定不會自己閉合。
    """
    if ctx["dry_run"]:
        return True, "會算相似對、裁決、收回，然後補關係閉包"

    ok, out = run_tool(["agent_memory_spike/consolidate.py", "--pairs"], timeout=1800)
    if not ok:
        return False, f"算配對失敗: {out[-300:]}"

    pending = _pending_count(WORK_DIR / "consolidate_pairs.json", "pairs")
    if pending == 0:
        return True, "沒有值得判定的配對"

    batch = min(pending, ctx["max_groups"])
    prompt = (
        AUTO_PREAMBLE + "記憶池收斂的配對判定。在這個 repo 底下執行：\n\n"
        f'"{TOOL_PYTHON}" agent_memory_spike/consolidate.py --show 0-{batch - 1}\n\n'
        "輸出開頭是判定準則，照著做。**判斷紀律那節要嚴格遵守：不確定就給 DISTINCT**，"
        "誤判會不可逆地刪掉真實記憶。\n"
        "**不要寫任何檔案**——把結果直接以一個 ```json 區塊回覆給我即可。"
    )
    ok, reply = adjudicate_to_file(prompt, WORK_DIR / "consolidate_out_auto" / "batch-00.json")
    if not ok:
        return False, f"裁決失敗: {reply}"

    ok, out = run_tool([
        "agent_memory_spike/consolidate.py", "--ingest",
        str(WORK_DIR / "consolidate_out_auto"),
    ])
    if not ok:
        return False, f"收回失敗: {out[-300:]}"

    ok2, out2 = run_tool([
        "agent_memory_spike/consolidate.py", "--transitive",
        str(WORK_DIR / "consolidate_out_auto"), "--apply",
    ])
    return ok and ok2, (pick_summary(out, "→", "移除")
                        + " | " + pick_summary(out2, "→", "沒有需要處理"))


def stage_calibrate(ctx: dict[str, Any]) -> tuple[bool, str]:
    """校準：出題 → 受測 → 判卷 → 收回。

    受測與判卷**必須是兩次獨立的裁決**：合成一次就等於讓受測者看到答案，
    整場測試退化成自評，而自評在最有價值的條目上系統性失準。
    """
    if ctx["dry_run"]:
        return True, f"會對最多 {ctx['max_groups']} 條未校準的記憶跑行為測試"

    ok, out = run_tool([
        "agent_memory_spike/calibrate.py", "--emit",
        "--sample", str(ctx["max_groups"]),
    ])
    if not ok:
        return False, f"出題失敗: {out[-300:]}"

    pending = _pending_count(WORK_DIR / "probe_tasks.json", "probes")
    if pending == 0:
        return True, "沒有待校準的條目"

    answer_dir = WORK_DIR / "probe_out_auto"
    probe_prompt = (
        AUTO_PREAMBLE + "這批是同事丟過來的開發問題，需要你逐題作答。在這個 repo 底下執行：\n\n"
        f'"{TOOL_PYTHON}" agent_memory_spike/calibrate.py --show-probes 0-{pending - 1}\n\n'
        "那份輸出開頭有作答須知，照著做。**除了那一個指令之外不要執行任何其他指令、"
        "不要讀取任何檔案、不要 grep、不要搜尋**——這些專案不在你手上，查了也找不到對的東西。\n"
        "**不要寫任何檔案**——把作答結果直接以一個 ```json 區塊回覆給我，"
        '格式 [{"id": "c-000", "answer": "..."}]。'
    )
    ok, reply = adjudicate_to_file(probe_prompt, answer_dir / "answers-00.json")
    if not ok:
        return False, f"受測失敗: {reply}"

    judge_prompt = (
        AUTO_PREAMBLE + "判卷任務。在這個 repo 底下執行：\n\n"
        f'"{TOOL_PYTHON}" agent_memory_spike/calibrate.py '
        f"--show-judge 0-{pending - 1} --answer-path {answer_dir}\n\n"
        "輸出開頭是判卷準則，逐題照著判，判定要嚴格。\n"
        "**不要寫任何檔案**——把結果直接以一個 ```json 區塊回覆給我即可。"
    )
    ok, reply = adjudicate_to_file(judge_prompt, WORK_DIR / "verdicts_auto" / "verdicts-00.json")
    if not ok:
        return False, f"判卷失敗: {reply}"

    ok, out = run_tool([
        "agent_memory_spike/calibrate.py", "--ingest", str(WORK_DIR / "verdicts_auto"),
    ])
    return ok, pick_summary(out, "已校準", "更新")


def pick_summary(output: str, *keywords: str) -> str:
    """從工具輸出裡挑一行當摘要。

    取最後一行不行——各支工具的結尾常常是排版或範例，
    實測 `calibrate --report` 的最後一行是某條記憶的 scope，那對排程報告毫無意義。
    """
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for keyword in keywords:
        for line in lines:
            if keyword in line:
                return line
    return lines[-1] if lines else "（無輸出）"


def _pending_count(path: Path, key: str) -> int:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    return len(payload.get(key) or [])


STAGES: list[tuple[str, str, Callable[[dict[str, Any]], tuple[bool, str]]]] = [
    ("collect", "收料：補齊所有 transcript 的新輪次", stage_collect),
    ("health", "健檢：語料有問題就不往下走", stage_health),
    ("distill", "蒸餾：語料 → concept 候選", stage_distill),
    ("consolidate", "收斂：去重、矛盾偵測、關係閉包", stage_consolidate),
    ("calibrate", "校準：行為測試量 surprisal", stage_calibrate),
]


def run_pipeline(*, dry_run: bool, max_groups: int, only: str | None) -> int:
    """依序跑各階段。

    **前一階段失敗就停下**：語料壞掉時蒸餾只會蒸出錯的記憶，
    收斂沒跑完就校準則會把等一下要被刪掉的條目也測一遍。
    """
    ctx = {"dry_run": dry_run, "max_groups": max_groups}
    state = load_state()
    results: list[dict[str, Any]] = []
    out = sys.stderr

    for name, description, func in STAGES:
        if only and name != only:
            continue
        print(f"\n[pipeline] === {name} — {description}", file=out)
        started = time.time()
        try:
            ok, summary = func(ctx)
        except Exception as exc:  # noqa: BLE001 — 一個階段炸掉不該讓整條管線沒有紀錄
            ok, summary = False, f"例外: {exc}"
        elapsed = time.time() - started
        print(f"[pipeline] {'OK ' if ok else 'FAIL'} {name} ({elapsed:.0f}s): {summary}",
              file=out)
        results.append({"stage": name, "ok": ok, "summary": summary,
                        "seconds": round(elapsed, 1)})
        if not ok:
            print(f"[pipeline] {name} 失敗，停止後續階段", file=out)
            break

    if not dry_run:
        state["last_run"] = {"results": results, "max_groups": max_groups}
        save_state(state)
    return 0 if all(r["ok"] for r in results) else 1


def show_status() -> int:
    out = sys.stderr
    state = load_state()
    last = state.get("last_run")
    if not last:
        print("[pipeline] 還沒跑過", file=out)
    else:
        print(f"[pipeline] 上次執行（上限 {last.get('max_groups')} 組）：", file=out)
        for entry in last.get("results", []):
            print(f"  {'OK ' if entry['ok'] else 'FAIL'} {entry['stage']:<12} "
                  f"{entry['seconds']:>6.1f}s  {entry['summary']}", file=out)
    if LOCK_PATH.exists():
        age = (time.time() - LOCK_PATH.stat().st_mtime) / 60
        print(f"  ⚠ 鎖存在（{age:.0f} 分鐘前建立）——有管線正在跑，或上次沒有正常結束",
              file=out)
    print(f"  claude CLI: {claude_path() or '找不到'}", file=out)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 3 自動化管線")
    parser.add_argument("--run", action="store_true", help="執行管線")
    parser.add_argument("--status", action="store_true", help="看上次跑到哪")
    parser.add_argument("--dry-run", action="store_true", help="只印出要做什麼")
    parser.add_argument("--stage", type=str, help="只跑指定階段")
    parser.add_argument("--max-groups", type=int, default=DEFAULT_MAX_GROUPS,
                        help="每次執行的組數上限（成本封頂）")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

    if args.status or not args.run:
        return show_status()

    try:
        acquire_lock()
    except LockBusy as exc:
        print(f"[pipeline] {exc}", file=sys.stderr)
        return 1
    try:
        return run_pipeline(dry_run=args.dry_run, max_groups=args.max_groups,
                            only=args.stage)
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
