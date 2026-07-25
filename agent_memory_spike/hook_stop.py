#!/usr/bin/env python3
"""Phase 1：Stop hook — 把剛結束的那一輪寫成 episode。

**只寫入，不召回。** Phase 1 的目的是累積真實語料；
surprisal 校準需要真實使用情境才划算，人工出題測人工資料只會測到出題品質。

用法（手動測試，目前不裝進 settings.json）::

    echo '{"session_id":"...","prompt_id":"...","transcript_path":"...","cwd":"..."}' \
      | python hook_stop.py

    # 補跑整份 transcript（Phase 1 初期回填用）
    python hook_stop.py --backfill <transcript_path>

存儲：每個 session 一個 jsonl，append 寫入。
不同 session 落在不同檔案，天然沒有跨程序寫入衝突——
把鎖的問題留到之後真的要做跨 session 聚合時再解，現在不需要付那個成本。

隱私備註：``user_text`` / ``assistant_text`` 是原文，可能含機敏內容。
目前純本機檔案、且不會被召回注入，風險可控；
等 Phase 2 要把這些內容送回 context 時必須先過一次消毒。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_T0 = time.perf_counter()

# 刻意存在 repo 外面。這個 hook 是全域掛載的，會收到所有專案的對話原文，
# 包含商業專案。放在 repo 內就算有 gitignore，仍有 `git add -f` 或規則變動而外洩的風險；
# 放在 ~/.claude 底下則從根本上不可能被誤 commit。
# 跨專案集中存放是刻意的——Phase 2 要驗證的正是跨專案一致性。
DEFAULT_EPISODE_DIR = Path.home() / ".claude" / "agent-memory-spike" / "episodes"

sys.path.insert(0, str(Path(__file__).parent))
from transcript import (  # noqa: E402
    ORIGIN_HUMAN,
    episode_for_prompt,
    episodes_from_transcript,
)


def episode_path(episode_dir: Path, session_id: str) -> Path:
    # session_id 來自 hook payload，理論上是 UUID，但它決定檔名所以仍要擋路徑穿越
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    return episode_dir / f"{safe or 'unknown'}.jsonl"


def recorded_prompt_ids(path: Path) -> set[str]:
    """讀出檔案裡已記錄的所有 prompt_id。

    早期版本只比對最後一筆，因為 Stop hook 每次只寫最新的一輪。
    那個假設在 backfill 批次寫入時直接崩掉——寫第一輪時最後一筆是上次的最後一輪，
    比對永遠不中，於是整份重複寫入。實測抓到，所以改成讀全部。

    成本是 O(檔案大小)：實測約 20KB/輪，百輪級的 session 約 2MB，
    解析大約數十毫秒。Stop hook 的 timeout 是 600 秒，正確性值得這個代價。
    """
    if not path.exists():
        return set()
    ids: set[str] = set()
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    pid = json.loads(line).get("prompt_id")
                except json.JSONDecodeError:
                    continue
                if isinstance(pid, str):
                    ids.add(pid)
    except OSError:
        return set()
    return ids


def append_episode(path: Path, episode: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # append 模式下單行寫入在一般情況是原子的；跨 session 本來就不會撞同一個檔案
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(episode, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def summarize(episode: dict) -> str:
    """給 stderr 看的一行摘要，不含對話內容。"""
    return (
        f"origin={episode['origin']} "
        f"repo={episode['repo']} "
        f"branch={','.join(episode['git_branch']) or '-'} "
        f"tools={episode['tool_calls_total']} "
        f"files={len(episode['files_touched'])} "
        f"user={len(episode['user_text'])}c "
        f"asst={len(episode['assistant_text'])}c"
    )


def run_backfill(transcript: Path, episode_dir: Path) -> int:
    episodes = episodes_from_transcript(transcript)
    if not episodes:
        print(f"[spike] backfill: 讀不到任何 episode（{transcript}）", file=sys.stderr)
        return 1

    written = skipped = 0
    # 每個檔案的已記錄集合只讀一次，並隨寫入更新——
    # 否則同批次內若有重複的 prompt_id 仍會漏掉
    seen_by_path: dict[Path, set[str]] = {}
    for ep in episodes:
        session_id = ep.get("session_id") or transcript.stem
        path = episode_path(episode_dir, session_id)
        seen = seen_by_path.setdefault(path, recorded_prompt_ids(path))
        if ep["prompt_id"] in seen:
            skipped += 1
            continue
        append_episode(path, ep)
        seen.add(ep["prompt_id"])
        written += 1

    human = sum(1 for e in episodes if e["origin"] == ORIGIN_HUMAN)
    print(
        f"[spike] backfill 完成：寫入 {written}、跳過 {skipped}，"
        f"共 {len(episodes)} 輪（human {human}）",
        file=sys.stderr,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 1 Stop hook spike")
    parser.add_argument("--backfill", type=Path, help="補跑整份 transcript")
    parser.add_argument("--episode-dir", type=Path, default=DEFAULT_EPISODE_DIR)
    parser.add_argument("--dry-run", action="store_true", help="只解析不寫入")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

    if args.backfill:
        return run_backfill(args.backfill, args.episode_dir)

    payload: dict = {}
    try:
        raw = sys.stdin.read()
        if raw.strip():
            payload = json.loads(raw)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[spike] stdin payload 解析失敗: {exc}", file=sys.stderr)
        return 0

    transcript_path = payload.get("transcript_path")
    prompt_id = payload.get("prompt_id")
    session_id = payload.get("session_id") or "unknown"

    if not transcript_path or not prompt_id:
        print(
            f"[spike] payload 缺欄位（transcript_path={bool(transcript_path)} "
            f"prompt_id={bool(prompt_id)}），略過",
            file=sys.stderr,
        )
        return 0

    episode = episode_for_prompt(Path(transcript_path), prompt_id)
    if episode is None:
        # Stop hook 觸發時 transcript 尾端可能還沒 flush 完，這是預期內的情況
        print(f"[spike] 找不到 prompt_id={prompt_id} 的輪次（尚未落盤？）", file=sys.stderr)
        return 0

    if args.dry_run:
        print(f"[spike] dry-run | {summarize(episode)}", file=sys.stderr)
        return 0

    path = episode_path(args.episode_dir, session_id)
    if prompt_id in recorded_prompt_ids(path):
        print(f"[spike] 已記錄過 prompt_id={prompt_id}，略過", file=sys.stderr)
        return 0

    append_episode(path, episode)
    elapsed = (time.perf_counter() - _T0) * 1000
    print(f"[spike] 已寫入 | {summarize(episode)} | {elapsed:.1f} ms", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
