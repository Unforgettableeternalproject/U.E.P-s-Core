#!/usr/bin/env python3
"""Phase 1：Stop hook — 把已完成的輪次寫成 episode。

**只寫入，不召回。** Phase 1 的目的是累積真實語料；
surprisal 校準需要真實使用情境才划算，人工出題測人工資料只會測到出題品質。

## 為什麼不寫「當前這一輪」

初版的設計是用 Stop hook payload 裡的 ``prompt_id`` 定位當前輪並寫入。實測是錯的：
Stop hook 觸發時，該輪的記錄**不保證已經完整寫進 transcript**。

實際抓到的後果——某一輪存進去時 ``assistant_text`` 只有 670 字元、5 次 tool call，
而該輪真正的內容是 2225 字元、14 次 tool call。少了七成，而且因為
「prompt_id 已記錄就跳過」的去重邏輯，這筆殘缺資料永遠不會被更新，
也沒有任何欄位標示它不完整。累積數週後語料會佈滿這種截斷紀錄且無從察覺。

所以改成：**每次觸發都做一次增量同步，並排除最新的一輪**。
有下一輪開始 = 前一輪必定已經結束，這個不變式保證寫進去的每一筆都是完整的。

代價是 episode 永遠落後一輪，session 的最後一輪要等下次 resume 才補得到
（見「已知限制」）。用完整性換即時性是划算的——殘缺的語料比晚到的語料糟得多。

## 用法

手動測試::

    echo '{"session_id":"...","transcript_path":"..."}' | python hook_stop.py

    python hook_stop.py --sync <transcript_path>      # 手動同步一份 transcript
    python hook_stop.py --repair <transcript_path>    # 重建，修復殘缺紀錄
    python hook_stop.py --dry-run ...                 # 只解析不寫入

存儲：每個 session 一個 jsonl，append 寫入。
不同 session 落在不同檔案，天然沒有跨程序寫入衝突。

隱私備註：``user_text`` / ``assistant_text`` 是原文，可能含機敏內容。
存放位置刻意在 repo 外（見 DEFAULT_EPISODE_DIR），
等 Phase 2 要把這些內容送回 context 時必須先過一次消毒。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_T0 = time.perf_counter()

# 刻意存在 repo 外面。這個 hook 是全域掛載的，會收到所有專案的對話原文，
# 包含商業專案。放在 repo 內就算有 gitignore，仍有 `git add -f` 或規則變動而外洩的風險；
# 放在 ~/.claude 底下則從根本上不可能被誤 commit。
# 跨專案集中存放是刻意的——Phase 2 要驗證的正是跨專案一致性。
DEFAULT_EPISODE_DIR = Path.home() / ".claude" / "agent-memory-spike" / "episodes"

sys.path.insert(0, str(Path(__file__).parent))
from transcript import ORIGIN_HUMAN, episodes_from_transcript  # noqa: E402


def episode_path(episode_dir: Path, session_id: str) -> Path:
    # session_id 來自 hook payload，理論上是 UUID，但它決定檔名所以仍要擋路徑穿越
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    return episode_dir / f"{safe or 'unknown'}.jsonl"


def _key(rec: dict[str, Any]) -> tuple[str, int]:
    """episode 的唯一鍵。

    只用 prompt_id 不夠：session 起始的 meta 注入在每次 resume 會重新出現且沿用同一個
    promptId。加上 turn_index 才唯一，而 resume 的完整複本序號一致，跨 session 去重仍有效。
    """
    return (str(rec.get("prompt_id")), int(rec.get("turn_index") or 0))


def recorded_prompt_ids(path: Path) -> set[tuple[str, int]]:
    """讀出檔案裡已記錄的所有 prompt_id。

    早期版本只比對最後一筆，因為當時假設 hook 每次只寫最新的一輪。
    那個假設在批次寫入時直接崩掉——寫第一輪時最後一筆是上次的最後一輪，
    比對永遠不中，於是整份重複寫入。實測抓到，所以改成讀全部。
    """
    if not path.exists():
        return set()
    ids: set[tuple[str, int]] = set()
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ids.add(_key(json.loads(line)))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return set()
    return ids


def completed_episodes(transcript: Path) -> list[dict[str, Any]]:
    """只回傳可以確定已經結束的輪次。

    最新的一輪被排除：Stop hook 觸發時它可能只寫了一半，
    此時寫入會留下永久殘缺的紀錄。有後續輪次存在就代表前一輪確實結束了。
    """
    episodes = episodes_from_transcript(transcript)
    return episodes[:-1] if episodes else []


def append_episode(path: Path, episode: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # append 模式下單行寫入在一般情況是原子的；跨 session 本來就不會撞同一個檔案
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(episode, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def rewrite_episodes(path: Path, episodes: list[dict[str, Any]]) -> None:
    """整檔重建，用 temp + replace 做原子替換，避免中途失敗留下半截檔案。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for ep in episodes:
            f.write(json.dumps(ep, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def sync(transcript: Path, episode_dir: Path, session_id: str, *, dry_run: bool = False) -> tuple[int, int]:
    """增量同步：寫入所有已完成但尚未記錄的輪次。回傳 (寫入數, 跳過數)。"""
    episodes = completed_episodes(transcript)
    if not episodes:
        return 0, 0

    path = episode_path(episode_dir, session_id)
    recorded = recorded_prompt_ids(path)

    written = skipped = 0
    for ep in episodes:
        if _key(ep) in recorded:
            skipped += 1
            continue
        if not dry_run:
            append_episode(path, ep)
            recorded.add(_key(ep))
        written += 1
    return written, skipped


def repair(transcript: Path, episode_dir: Path, session_id: str) -> tuple[int, int]:
    """從 transcript 全量重建，修掉殘缺的紀錄。回傳 (重建後筆數, 修正筆數)。

    需要這個是因為早期版本會寫入進行中的輪次，留下永久截斷的資料。
    """
    episodes = completed_episodes(transcript)
    path = episode_path(episode_dir, session_id)

    existing = {}
    if path.exists():
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    existing[_key(rec)] = rec
        except OSError:
            pass

    fixed = 0
    for ep in episodes:
        old = existing.get(_key(ep))
        if old is None:
            continue
        if (len(old.get("assistant_text", "")) != len(ep["assistant_text"])
                or old.get("tool_calls_total") != ep["tool_calls_total"]):
            fixed += 1

    rewrite_episodes(path, episodes)
    return len(episodes), fixed


def find_transcript(session_id: str) -> Path | None:
    """在 ~/.claude/projects 底下找對應的 transcript。檔名就是 session id。"""
    root = Path.home() / ".claude" / "projects"
    if not root.exists():
        return None
    for candidate in root.glob(f"*/{session_id}.jsonl"):
        return candidate
    return None


def iter_all_transcripts() -> list[Path]:
    root = Path.home() / ".claude" / "projects"
    return sorted(root.glob("*/*.jsonl")) if root.exists() else []


def sync_all(episode_dir: Path) -> int:
    """掃過所有 transcript 補齊。

    Stop hook 只會補到「有下一輪」的輪次，所以 session 一旦結束，
    最後一到兩輪就永遠等不到下一次觸發。實測有個 session 尾端積了 7 輪未記錄——
    session 被中斷時遺失的不只一輪。

    這支拿來定期收尾，比為此再掛一個 SessionEnd hook 簡單，
    而且能一併回填 hook 裝設之前就存在的 session。
    """
    transcripts = iter_all_transcripts()
    if not transcripts:
        print("[sync-all] 找不到任何 transcript", file=sys.stderr)
        return 0

    total_written = 0
    touched = 0
    for tp in transcripts:
        written, _ = sync(tp, episode_dir, tp.stem)
        if written:
            touched += 1
            total_written += written
    print(
        f"[sync-all] 掃過 {len(transcripts)} 份 transcript，"
        f"補上 {total_written} 輪（{touched} 個 session）",
        file=sys.stderr,
    )
    return 0


def load_deduped(episode_dir: Path) -> tuple[list[dict[str, Any]], int]:
    """讀出全部 episode 並去重，回傳 (去重後清單, 重複筆數)。

    prompt_id 是全域唯一的，但 resume/fork 會讓同一批輪次落進多個 session 檔——
    實測一條三代 resume 鏈造成 10.3% 的膨脹。

    寫入端仍維持每 session 一檔（併發簡單），去重放在讀取端。
    重複的副本內容實測完全一致，但仍取最完整的一份，
    以防某次寫入剛好撞上 transcript 尚未寫完。
    """
    best: dict[tuple[str, int], dict[str, Any]] = {}
    total = 0
    for fp in sorted(episode_dir.glob("*.jsonl")) if episode_dir.exists() else []:
        try:
            for line in fp.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                total += 1
                pid = _key(rec)
                prev = best.get(pid)
                if prev is None or (
                    len(rec.get("assistant_text", "")), rec.get("tool_calls_total", 0)
                ) > (len(prev.get("assistant_text", "")), prev.get("tool_calls_total", 0)):
                    best[pid] = rec
        except (OSError, json.JSONDecodeError):
            continue
    return list(best.values()), total - len(best)


def doctor(episode_dir: Path) -> int:
    """唯讀健檢：比對存檔與 transcript，並報告語料覆蓋度。

    存在的理由：兩天內抓到兩個「靜默寫錯資料」的 bug（截斷七成、repo 標成子目錄名），
    兩個都不會報錯，都要拿存檔跟來源逐筆比對才看得出來。
    hook 的失敗路徑一律 exit 0 不阻斷 session，代價就是壞掉不會有人通知你。
    """
    files = sorted(episode_dir.glob("*.jsonl")) if episode_dir.exists() else []
    if not files:
        print("[doctor] 沒有任何 episode 檔", file=sys.stderr)
        return 0

    problems: list[str] = []
    total = 0
    repos: dict[str, int] = {}
    origins: dict[str, int] = {}
    agents: dict[str, int] = {}
    no_transcript = 0
    pending = 0

    for fp in files:
        session_id = fp.stem
        try:
            stored = [json.loads(x) for x in fp.read_text(encoding="utf-8").splitlines() if x.strip()]
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"{session_id[:8]}: 讀取失敗 {exc}")
            continue

        total += len(stored)
        for rec in stored:
            repos[rec.get("repo") or "?"] = repos.get(rec.get("repo") or "?", 0) + 1
            origins[rec.get("origin") or "?"] = origins.get(rec.get("origin") or "?", 0) + 1
            agents[rec.get("agent") or "(未標記)"] = agents.get(rec.get("agent") or "(未標記)", 0) + 1

        transcript = find_transcript(session_id)
        if transcript is None:
            # transcript 可能已被 cleanupPeriodDays 清掉，不算錯誤
            no_transcript += 1
            continue

        live = {_key(e): e for e in completed_episodes(transcript)}
        seen = set()
        for rec in stored:
            pid = _key(rec)
            if pid in seen:
                problems.append(f"{session_id[:8]}: 重複 {pid[0][:8]}#{pid[1]}")
            seen.add(pid)
            ref = live.get(pid)
            if ref is None:
                continue
            if len(rec.get("assistant_text", "")) != len(ref["assistant_text"]):
                problems.append(
                    f"{session_id[:8]}: {pid[0][:8]}#{pid[1]} assistant_text "
                    f"{len(rec.get('assistant_text',''))} != {len(ref['assistant_text'])}"
                )
            if rec.get("tool_calls_total") != ref["tool_calls_total"]:
                problems.append(
                    f"{session_id[:8]}: {pid[0][:8]}#{pid[1]} tool_calls "
                    f"{rec.get('tool_calls_total')} != {ref['tool_calls_total']}"
                )
            if rec.get("repo") != ref["repo"]:
                problems.append(f"{session_id[:8]}: {pid[0][:8]}#{pid[1]} repo {rec.get('repo')} != {ref['repo']}")
        # 尾端的缺漏是設計的必然落後，不是故障：最新一輪一律排除，
        # 而它的前一輪要等下一次 Stop hook 觸發才補得進來。
        # 缺在中間才代表真的漏了——那是 hook 沒跑成功或寫入失敗。
        order = [_key(e) for e in completed_episodes(transcript)]
        missing_idx = [i for i, pid in enumerate(order) if pid not in seen]
        if missing_idx:
            trailing = len(order) - missing_idx[0] == len(missing_idx)
            if trailing and len(missing_idx) <= 2:
                pending += len(missing_idx)
            else:
                problems.append(
                    f"{session_id[:8]}: {len(missing_idx)} 輪未記錄"
                    f"{'（尾端待補）' if trailing else '（缺在中間，可能是 hook 未執行）'}"
                )

    out = sys.stderr
    print(f"[doctor] {len(files)} 個 session、{total} 輪", file=out)
    print(f"  repo    : {repos}", file=out)
    print(f"  origin  : {origins}", file=out)
    print(f"  agent   : {agents}", file=out)
    if no_transcript:
        print(f"  （{no_transcript} 個 session 的 transcript 已不存在，略過比對）", file=out)
    if pending:
        print(f"  （{pending} 輪在尾端待補，下次 Stop hook 觸發時寫入——這是正常的）", file=out)

    # resume/fork 會讓同一批輪次落進多個 session 檔，膨脹要從語料量裡扣掉
    deduped, dupes = load_deduped(episode_dir)
    if dupes:
        print(f"\n  重複 {dupes} 筆（{dupes/total*100:.1f}%），來自 session resume/fork", file=out)

    # 覆蓋度：Phase 0 顯示有價值的記憶需要跨多個 repo 的真實開發
    dedup_origins: dict[str, int] = {}
    dedup_repos: dict[str, int] = {}
    for rec in deduped:
        dedup_origins[rec.get("origin") or "?"] = dedup_origins.get(rec.get("origin") or "?", 0) + 1
        dedup_repos[rec.get("repo") or "?"] = dedup_repos.get(rec.get("repo") or "?", 0) + 1
    human = dedup_origins.get(ORIGIN_HUMAN, 0)
    real_repos = [r for r in dedup_repos if r not in ("?", None)]
    print(f"\n  去重後：{len(deduped)} 輪、{len(real_repos)} 個 repo、{human} 輪 human 輸入", file=out)
    if len(real_repos) < 3:
        print("  → repo 數偏少，跨專案價值還測不出來", file=out)

    if problems:
        print(f"\n[doctor] 發現 {len(problems)} 個問題：", file=out)
        for p in problems[:20]:
            print(f"  - {p}", file=out)
        if len(problems) > 20:
            print(f"  ...另外 {len(problems)-20} 個", file=out)
        print("\n  修復：對受影響的 session 跑 --repair <transcript_path>", file=out)
        return 1

    print("\n[doctor] 未發現不一致", file=out)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 1 Stop hook")
    parser.add_argument("--doctor", action="store_true", help="唯讀健檢：比對存檔與 transcript")
    parser.add_argument("--sync-all", action="store_true", help="掃過所有 transcript 補齊遺漏")
    parser.add_argument("--sync", type=Path, help="手動同步指定的 transcript")
    parser.add_argument("--repair", type=Path, help="全量重建，修復殘缺紀錄")
    parser.add_argument("--episode-dir", type=Path, default=DEFAULT_EPISODE_DIR)
    parser.add_argument("--session-id", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true", help="只解析不寫入")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

    def resolve_session(transcript: Path) -> str:
        if args.session_id:
            return args.session_id
        return transcript.stem  # transcript 檔名就是 session id

    if args.sync_all:
        return sync_all(args.episode_dir)

    if args.doctor:
        return doctor(args.episode_dir)

    if args.repair:
        total, fixed = repair(args.repair, args.episode_dir, resolve_session(args.repair))
        print(f"[spike] repair 完成：重建 {total} 筆，其中修正 {fixed} 筆殘缺紀錄", file=sys.stderr)
        return 0

    if args.sync:
        written, skipped = sync(args.sync, args.episode_dir, resolve_session(args.sync),
                                dry_run=args.dry_run)
        print(f"[spike] sync 完成：寫入 {written}、跳過 {skipped}", file=sys.stderr)
        return 0

    payload: dict = {}
    try:
        raw = sys.stdin.read()
        if raw.strip():
            payload = json.loads(raw)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[spike] stdin payload 解析失敗: {exc}", file=sys.stderr)
        return 0

    transcript_path = payload.get("transcript_path")
    if not transcript_path:
        print("[spike] payload 缺 transcript_path，略過", file=sys.stderr)
        return 0

    transcript = Path(transcript_path)
    session_id = payload.get("session_id") or transcript.stem

    written, skipped = sync(transcript, args.episode_dir, session_id, dry_run=args.dry_run)
    elapsed = (time.perf_counter() - _T0) * 1000

    if written:
        episodes = completed_episodes(transcript)
        human = sum(1 for e in episodes if e["origin"] == ORIGIN_HUMAN)
        print(
            f"[spike] 寫入 {written} 輪（累計 {len(episodes)}，human {human}）"
            f"{' [dry-run]' if args.dry_run else ''} | {elapsed:.1f} ms",
            file=sys.stderr,
        )
    else:
        print(f"[spike] 無新增（已記錄 {skipped}）| {elapsed:.1f} ms", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
