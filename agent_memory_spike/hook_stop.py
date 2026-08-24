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
import hashlib
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
from transcript import (  # noqa: E402
    ORIGIN_HUMAN,
    episodes_from_transcript,
    file_keys,
    configure_streams,
    load_injections,
    load_touches,
)


def episode_path(episode_dir: Path, session_id: str) -> Path:
    # session_id 來自 hook payload，理論上是 UUID，但它決定檔名所以仍要擋路徑穿越
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    return episode_dir / f"{safe or 'unknown'}.jsonl"


def _key(rec: dict[str, Any]) -> tuple[str, int]:
    """episode 在**單一 session 檔內**的唯一鍵。

    只用 prompt_id 不夠：session 起始的 meta 注入在每次 resume 會重新出現且沿用同一個
    promptId。加上 turn_index 才唯一——同一個檔案內序號不會位移，這裡是安全的。

    **跨 session 的去重不能用這把鍵**（見 ``load_deduped``）：
    resume 產生的複本在另一個檔案裡序號會位移，比對必然失效。
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


def pinned_roots(records: list[dict[str, Any]]) -> dict[tuple[str, int], str]:
    """從存檔取出每一輪的 repo 根基準，給重建時沿用。

    重建既有輪次一定要帶這個。不帶的話 `repo_root()` 會用**當下的**檔案系統重算，
    而 repo 一改名，舊 cwd 就不存在了、往上找會撞到父層的 .git——
    2026-08-22 實際發生：739 輪的 repo 與檔案路徑基準集體漂移，doctor 報 888 個誤報。
    """
    out: dict[tuple[str, int], str] = {}
    for rec in records:
        root = rec.get("repo_root")
        if root:
            out[_key(rec)] = str(root)
    return out


def infer_repo_root(rec: dict[str, Any]) -> str | None:
    """替 `repo_root` 欄位問世之前的語料推導基準。

    純字串推導，**刻意不碰檔案系統**——會需要回填正是因為當下的檔案系統
    已經不是寫入當時的樣子了。從 cwd 裡找出等於 `repo` 的那一段，
    取到該段為止即是當時的 root。同名段取最右邊那個：巢狀 repo
    （AI-Website/AI-Website-API）要的是內層。
    """
    repo = rec.get("repo")
    if not repo:
        return None
    candidate = None
    for cwd in rec.get("cwd") or []:
        parts = str(cwd).replace("\\", "/").split("/")
        for i in range(len(parts) - 1, -1, -1):
            if parts[i] == repo:
                candidate = str(Path("/".join(parts[: i + 1])))
                break
        if candidate:
            break
    if candidate is None:
        return None

    # 🚨 `repo` 欄位有兩個來源，而欄位本身分不出是哪一個：git 根目錄名，
    # 或**解析不出 root 時**退回的 cwd 目錄名。後者當時的 root 是 None，
    # 檔案路徑因此沒有被正規化、原樣存成絕對路徑。
    # 對這種輪次推導出一個 root，會讓重建把路徑正規化成相對——比存檔「更正確」，
    # 但那是在改寫歷史，而且 doctor 會逐輪報不一致（實測 4 筆，全在 E:\ 的非 git 目錄）。
    # 判準純看資料：存檔裡還留著以這個 root 為前綴的絕對路徑，就證明當時沒有 root。
    prefix = candidate.replace("\\", "/").rstrip("/") + "/"
    for field in ("files_edited", "files_read"):
        for raw in rec.get(field) or []:
            if str(raw).replace("\\", "/").startswith(prefix):
                return None
    return candidate


def backfill_repo_root(episode_dir: Path, *, dry_run: bool = False) -> int:
    """一次性回填：把 repo 根基準寫進既有語料。

    這批語料的 repo 歸屬只存在於 `repo` + `cwd` 的組合裡，而那個組合會隨
    repo 改名失效。回填之後歸屬就凍結成欄位，不再依賴檔案系統的現況。
    """
    files = sorted(episode_dir.glob("*.jsonl")) if episode_dir.exists() else []
    if not files:
        print("[backfill] 沒有任何 episode 檔", file=sys.stderr)
        return 0

    filled = already = failed = 0
    for fp in files:
        try:
            records = [json.loads(x) for x in fp.read_text(encoding="utf-8").splitlines() if x.strip()]
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[backfill] {fp.stem[:8]} 讀取失敗 {exc}", file=sys.stderr)
            continue
        changed = False
        for rec in records:
            # 推導是純字串運算、與檔案系統現況無關，所以無條件重算是冪等的，
            # 而且判準修正後重跑就能自我修復——不必從備份還原
            root = infer_repo_root(rec)
            if root is None:
                # 推不出來就留空：寧可讓 doctor 說「這輪沒有基準」，
                # 也不要塞一個猜的路徑進去——那會變成看起來正常的錯資料
                if rec.pop("repo_root", None) is not None:
                    changed = True
                failed += 1
                continue
            if rec.get("repo_root") == root:
                already += 1
                continue
            rec["repo_root"] = root
            filled += 1
            changed = True
        if changed and not dry_run:
            rewrite_episodes(fp, records)

    print(f"[backfill] 寫入 {filled} 輪、已是最新 {already} 輪、推導不出 {failed} 輪"
          + ("（dry-run，未寫入）" if dry_run else ""), file=sys.stderr)
    return 0


def completed_episodes(transcript: Path,
                       injections: dict[tuple[str, str], list[str]] | None = None,
                       pinned: dict[tuple[str, int], str] | None = None
                       ) -> list[dict[str, Any]]:
    """只回傳可以確定已經結束的輪次。

    最新的一輪被排除：Stop hook 觸發時它可能只寫了一半，
    此時寫入會留下永久殘缺的紀錄。有後續輪次存在就代表前一輪確實結束了。
    """
    episodes = episodes_from_transcript(transcript, injections, pinned)
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


def sync(transcript: Path, episode_dir: Path, session_id: str, *, dry_run: bool = False,
         injections: dict[tuple[str, str], list[str]] | None = None) -> tuple[int, int]:
    """增量同步：寫入所有已完成但尚未記錄的輪次。回傳 (寫入數, 跳過數)。"""
    episodes = completed_episodes(transcript, injections)
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


def repair(transcript: Path, episode_dir: Path, session_id: str,
           injections: dict[tuple[str, str], list[str]] | None = None) -> tuple[int, int]:
    """從 transcript 全量重建，修掉殘缺的紀錄。回傳 (重建後筆數, 修正筆數)。

    需要這個是因為早期版本會寫入進行中的輪次，留下永久截斷的資料。
    """
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

    # 存檔要先讀完才能重建：repo 根基準沿用存檔的，不重算。
    # 順序反過來的話 --repair 會把 repo 改名前的語料「修」成新 repo 名，
    # 那是把歷史事實改掉，不是修復
    episodes = completed_episodes(transcript, injections, pinned_roots(list(existing.values())))

    fixed = 0
    for ep in episodes:
        old = existing.get(_key(ep))
        if old is None:
            continue
        # 比對所有欄位而非只看文字長度與工具數：schema 擴充（例如補 files_edited）
        # 造成的差異也算修正，否則 repair 會回報 0 而看起來像沒事發生
        if old != ep:
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

    # 一次載入，數百份 transcript 共用——否則每份都重讀一次 side-car
    injections = load_injections()
    total_written = 0
    touched = 0
    for tp in transcripts:
        written, _ = sync(tp, episode_dir, tp.stem, injections=injections)
        if written:
            touched += 1
            total_written += written
    print(
        f"[sync-all] 掃過 {len(transcripts)} 份 transcript，"
        f"補上 {total_written} 輪（{touched} 個 session）",
        file=sys.stderr,
    )
    return 0


def repair_all(episode_dir: Path) -> int:
    """對每個已存在的 episode 檔重跑 repair。

    ``--sync-all`` 只補「還沒記錄過」的輪次，對已寫入的紀錄完全不動——
    schema 一改（例如補上 files_edited/files_read），既有語料就永遠停在舊格式，
    而且沒有任何欄位標示它是舊的。這支負責從 transcript 全量重建。

    transcript 已被 cleanupPeriodDays 清掉的 session 只能維持原狀，會列在結尾。
    """
    files = sorted(episode_dir.glob("*.jsonl")) if episode_dir.exists() else []
    if not files:
        print("[repair-all] 沒有任何 episode 檔", file=sys.stderr)
        return 0

    injections = load_injections()
    rebuilt = fixed_total = skipped = 0
    for fp in files:
        transcript = find_transcript(fp.stem)
        if transcript is None:
            skipped += 1
            continue
        total, fixed = repair(transcript, episode_dir, fp.stem, injections)
        rebuilt += total
        fixed_total += fixed

    print(
        f"[repair-all] 重建 {rebuilt} 輪（{len(files) - skipped} 個 session），"
        f"其中 {fixed_total} 輪內容有變動",
        file=sys.stderr,
    )
    if skipped:
        print(f"  {skipped} 個 session 的 transcript 已不存在，維持原狀", file=sys.stderr)
    return 0


def load_deduped(episode_dir: Path) -> tuple[list[dict[str, Any]], int]:
    """讀出全部 episode 並去重，回傳 (去重後清單, 重複筆數)。

    prompt_id 是全域唯一的，但 resume/fork 會讓同一批輪次落進多個 session 檔——
    實測一條三代 resume 鏈造成 10.3% 的膨脹。

    寫入端仍維持每 session 一檔（併發簡單），去重放在讀取端。
    重複的副本內容實測完全一致，但仍取最完整的一份，
    以防某次寫入剛好撞上 transcript 尚未寫完。
    """
    # 先按 (prompt_id, user_text 指紋) 分桶。**不能用 turn_index**：
    # 實測同一輪在兩個 session 檔裡分別是 turn_index 2 和 3，序號會位移，
    # 原本的複合鍵因此完全擋不住跨 session 重複（實測 32 組、64 輪進了語料）。
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    total = 0
    for fp in sorted(episode_dir.glob("*.jsonl")) if episode_dir.exists() else []:
        try:
            for line in fp.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                total += 1
                digest = hashlib.sha1((rec.get("user_text") or "").encode("utf-8")).hexdigest()[:16]
                buckets.setdefault((str(rec.get("prompt_id")), digest), []).append(rec)
        except (OSError, json.JSONDecodeError):
            continue

    # 桶內未必是同一輪：同一個 promptId 配同一句話，也可能真的是分開的兩輪
    # （meta 注入每次 resume 都重現，實測某個 id 在 7/30 與 8/02 各出現一次）。
    # 區分的依據是 assistant_text 的**前綴關係**——殘缺的副本必然是完整版的前綴，
    # 而真正不同的兩輪，回覆內容從頭就不一樣。
    deduped: list[dict[str, Any]] = []
    for records in buckets.values():
        records.sort(key=lambda r: len(r.get("assistant_text") or ""))
        survivors: list[dict[str, Any]] = []
        for rec in records:
            text = rec.get("assistant_text") or ""
            for i, kept in enumerate(survivors):
                kept_text = kept.get("assistant_text") or ""
                if text.startswith(kept_text) or kept_text.startswith(text):
                    # 同一輪的兩份，取較完整的那份（自我修復：某次寫入撞上
                    # transcript 尚未寫完時，下次讀取會自動被完整版取代）
                    if (len(text), rec.get("tool_calls_total", 0)) > (
                        len(kept_text), kept.get("tool_calls_total", 0)
                    ):
                        survivors[i] = rec
                    break
            else:
                survivors.append(rec)
        deduped.extend(survivors)
    return deduped, total - len(deduped)


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
    # 警示與問題分開：問題代表語料不可信、要擋下蒸餾（exit 1），
    # 警示是「該看但不該擋」——例如 hook 的觀察缺口，傷的是注入效率不是語料
    warnings: list[str] = []
    total = 0
    repos: dict[str, int] = {}
    origins: dict[str, int] = {}
    agents: dict[str, int] = {}
    no_transcript = 0
    pending = 0
    # 空 assistant_text 的分流。實測 11.4% 的輪次是空的，先前 doctor 完全不看這個欄位——
    # 按「doctor 沒比對的欄位等於沒有保護」的教訓，這裡把它拆成
    # 「真的沒有回應」與「有回應卻存成空」兩類，只有後者是故障。
    empty_kinds: dict[str, int] = {}
    empty_human = 0
    # 注入紀錄與語料的對帳。注入 hook 寫 side-car、語料靠 (session_id, prompt 指紋)
    # 對回來，比對規則錯了就會靜默地整批對不上——而那正是「哪些輪次被記憶影響過」
    # 這件事的唯一依據，錯了會讓之後的校準失去意義
    injections = load_injections()
    # 對帳用 (session_id, prompt_id) 配對而不是數輪數：同一配對可能對到多輪
    # （resume 讓 promptId 重現），數輪數會讓兩邊的分母對不齊
    stored_injected: set[tuple[str, str]] = set()
    # 在 transcript 裡標記得到、但還沒入料的配對——多半是「最新一輪不寫」的
    # 設計性落後。指紋沒有失效：等下一輪出現，它入料時就會帶著 injected 標記
    transcript_injected: set[tuple[str, str]] = set()
    legacy_schema = 0
    legacy_unfixable = 0

    for fp in files:
        session_id = fp.stem
        try:
            stored = [json.loads(x) for x in fp.read_text(encoding="utf-8").splitlines() if x.strip()]
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"{session_id[:8]}: 讀取失敗 {exc}")
            continue

        total += len(stored)
        legacy_here = 0
        for rec in stored:
            if "injected" not in rec:
                legacy_here += 1
            elif rec.get("injected"):
                stored_injected.add((str(rec.get("session_id")), str(rec.get("prompt_id"))))
            repos[rec.get("repo") or "?"] = repos.get(rec.get("repo") or "?", 0) + 1
            origins[rec.get("origin") or "?"] = origins.get(rec.get("origin") or "?", 0) + 1
            agents[rec.get("agent") or "(未標記)"] = agents.get(rec.get("agent") or "(未標記)", 0) + 1

        transcript = find_transcript(session_id)
        if transcript is None:
            # transcript 可能已被 cleanupPeriodDays 清掉，不算錯誤。
            # 舊 schema 也一樣——沒有來源就重建不了，報成問題只會讓 doctor 永遠是紅的，
            # 真正的故障反而淹在裡面
            legacy_unfixable += legacy_here
            no_transcript += 1
            for rec in stored:
                if not (rec.get("assistant_text") or "").strip():
                    empty_kinds["無法驗證（transcript 已清）"] = (
                        empty_kinds.get("無法驗證（transcript 已清）", 0) + 1
                    )
                    empty_human += rec.get("origin") == ORIGIN_HUMAN
            continue

        # 解析一次就好。原本 completed_episodes 在這個迴圈裡被呼叫三次，
        # 每次都重讀並重建整份 transcript。
        all_episodes = episodes_from_transcript(transcript, injections, pinned_roots(stored))
        completed = all_episodes[:-1] if all_episodes else []
        live = {_key(e): e for e in completed}
        # 空 assistant_text 的比對要用**含最新輪**的版本：那 2 筆殘留正是
        # 落在「transcript 只有它自己一輪」的 session 裡，completed 把它排除掉，
        # 於是 ref is None → 靜默略過。這就是先前 doctor 全綠卻仍有殘留的原因。
        live_all = {_key(e): e for e in all_episodes}
        legacy_schema += legacy_here
        for ep in all_episodes:
            if ep.get("injected"):
                transcript_injected.add((str(ep.get("session_id")), str(ep.get("prompt_id"))))

        seen = set()
        for rec in stored:
            pid = _key(rec)
            if pid in seen:
                problems.append(f"{session_id[:8]}: 重複 {pid[0][:8]}#{pid[1]}")
            seen.add(pid)

            if not (rec.get("assistant_text") or "").strip():
                empty_human += rec.get("origin") == ORIGIN_HUMAN
                source = live_all.get(pid)
                if source is None:
                    empty_kinds["無法驗證（transcript 無此輪）"] = (
                        empty_kinds.get("無法驗證（transcript 無此輪）", 0) + 1
                    )
                elif (source.get("assistant_text") or "").strip():
                    # 唯一算故障的一類：來源有回應，存檔卻是空的
                    empty_kinds["殘留（來源有回應）"] = empty_kinds.get("殘留（來源有回應）", 0) + 1
                    problems.append(
                        f"{session_id[:8]}: {pid[0][:8]}#{pid[1]} assistant_text 空，"
                        f"但 transcript 有 {len(source['assistant_text'])} 字元"
                    )
                elif source.get("tool_calls_total"):
                    # 做了事但沒有文字結論——中斷發生在工具執行途中
                    empty_kinds["中斷於工具執行中"] = empty_kinds.get("中斷於工具執行中", 0) + 1
                else:
                    # 送出後立刻被中斷或訊息排隊，agent 根本沒回應。真實情況，不是故障
                    empty_kinds["無回應（中斷／排隊）"] = empty_kinds.get("無回應（中斷／排隊）", 0) + 1

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
            # 檔案欄位一度只取自 file-history-delta，漏掉大半編輯而毫無徵兆——
            # doctor 當時不比對這個欄位，所以完全看不見。現在比對。
            for field in ("files_edited", "files_read"):
                if rec.get(field) != ref[field]:
                    problems.append(
                        f"{session_id[:8]}: {pid[0][:8]}#{pid[1]} {field} "
                        f"{len(rec.get(field) or [])} != {len(ref[field])}"
                    )
        # 尾端的缺漏是設計的必然落後，不是故障：最新一輪一律排除，
        # 而它的前一輪要等下一次 Stop hook 觸發才補得進來。
        # 缺在中間才代表真的漏了——那是 hook 沒跑成功或寫入失敗。
        order = [_key(e) for e in completed]
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

    if legacy_unfixable:
        print(f"  （{legacy_unfixable} 輪停在舊 schema 且來源已消失，重建不了）", file=out)
    if legacy_schema:
        problems.append(
            f"{legacy_schema} 輪沒有 injected 欄位（早於這個 schema）——跑 --repair-all 補上"
        )
    if injections:
        matched = {pair for pair in injections if pair in stored_injected}
        pending_inject = {pair for pair in injections
                          if pair not in matched and pair in transcript_injected}
        lost = set(injections) - matched - pending_inject
        # 被注入的 session 可能連 episode 檔都還沒有（Stop hook 沒跑到），
        # 上面的迴圈只掃 episode 檔，這種 session 的 transcript 沒被看過。
        # 直接去 transcript 驗證：標記得到就是待補，不是指紋失效
        for pair in sorted(lost):
            transcript = find_transcript(pair[0])
            if transcript is None:
                continue
            for ep in episodes_from_transcript(transcript, injections):
                if ep.get("injected") and (str(ep.get("session_id")), str(ep.get("prompt_id"))) == pair:
                    pending_inject.add(pair)
                    break
        lost -= pending_inject
        print(f"\n  注入紀錄 {len(injections)} 筆，語料裡對上 {len(matched)} 筆"
              + (f"、尾端待補 {len(pending_inject)} 筆" if pending_inject else ""), file=out)
        if lost:
            problems.append(
                f"注入紀錄有 {len(lost)} 筆在 transcript 裡完全標記不到——"
                f"指紋比對失效，被影響過的輪次會被當成乾淨語料: "
                + ", ".join(f"{s[:8]}/{p[:8]}" for s, p in sorted(lost))
            )

    empty_total = sum(empty_kinds.values())
    if empty_total:
        print(
            f"\n  空 assistant_text：{empty_total} 輪（{empty_total / total * 100:.1f}%），"
            f"其中 human 輪 {empty_human}",
            file=out,
        )
        for kind, count in sorted(empty_kinds.items(), key=lambda kv: -kv[1]):
            print(f"    {kind}: {count}", file=out)

    # resume/fork 會讓同一批輪次落進多個 session 檔，膨脹要從語料量裡扣掉
    deduped, dupes = load_deduped(episode_dir)
    if dupes:
        print(f"\n  重複 {dupes} 筆（{dupes/total*100:.1f}%），來自 session resume/fork", file=out)

    # 注入 hook 的觀察 vs 語料實際改到的檔案。
    # **doctor 沒比對的欄位等於沒有保護**——這是這個專案自己記過的教訓，
    # 而 hook 的 touched 累積先前正是沒被比對的那一項：實測發生過一次 Write
    # 沒被記進 touched，複現不出，而且事後完全無從查證有沒有第二次。
    touches = load_touches()
    if touches:
        by_turn = {(str(rec.get("session_id")), str(rec.get("prompt_id"))): rec
                   for rec in deduped}
        checked = missed_turns = 0
        samples: list[str] = []

        def covered(seen_keys: set[str], key: str) -> bool:
            """hook 的 key 與語料的 key 是不是同一個檔案。

            不能只用相等：兩邊的正規化基準可能不同（nested repos + bash 切目錄，
            hook 曾以絕對路徑末 3 段記下 `mind-door/ai-website/append-2199-scss.js`，
            語料端是 `append-2199-scss.js`）。兩個 key 都收斂到同一個檔案結尾，
            所以「一方是另一方的尾段」就足以認定同一檔——比純檔名比對嚴，
            又吸收得掉基準差異。
            """
            return any(s == key or s.endswith("/" + key) or key.endswith("/" + s)
                       for s in seen_keys)

        for key, seen_keys in sorted(touches.items()):
            rec = by_turn.get(key)
            if rec is None:
                # hook 跑過但語料沒有這一輪：多半是尾端待補或 transcript 已清。
                # 那不是遺漏，所以不計入分母——把它算進去會讓比率永遠難看，
                # 真正的故障就淹在裡面了
                continue
            checked += 1
            # 只問一個方向：語料說這輪改了、hook 卻沒看到。
            # 反方向（hook 看到、語料沒有）是工具被擋或編輯失敗，不是遺漏
            unseen = {k for k in file_keys(rec.get("files_edited"))
                      if not covered(seen_keys, k)}
            if unseen:
                missed_turns += 1
                if len(samples) < 5:
                    samples.append(f"{key[1][:8]}… 漏看 {sorted(unseen)}")
        print(f"\n  hook 觀察紀錄 {len(touches)} 輪，可對帳 {checked} 輪、"
              f"漏看 {missed_turns} 輪", file=out)
        for sample in samples:
            print(f"    {sample}", file=out)
        if missed_turns:
            # 警示而不是問題：漏看傷的是注入效率（overlap 算在偏少的檔案集上，
            # 門檻被悄悄調高），語料本身沒有壞——不該為此擋下整條蒸餾管線。
            # 歷史紀錄裡也確實留著幾筆複現不出的遺漏，當問題會讓 doctor 永遠是紅的
            warnings.append(
                f"{missed_turns}/{checked} 輪有編輯是 hook 沒看到的——"
                f"overlap 會算在偏少的檔案集上，等於門檻被悄悄調高"
            )

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

    if warnings:
        print(f"\n[doctor] {len(warnings)} 個警示（不影響語料可信度，不擋管線）：", file=out)
        for w in warnings:
            print(f"  ⚠ {w}", file=out)

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
    configure_streams()
    parser = argparse.ArgumentParser(description="Phase 1 Stop hook")
    parser.add_argument("--doctor", action="store_true", help="唯讀健檢：比對存檔與 transcript")
    parser.add_argument("--sync-all", action="store_true", help="掃過所有 transcript 補齊遺漏")
    parser.add_argument("--repair-all", action="store_true", help="對所有既有 session 全量重建（schema 變更後使用）")
    parser.add_argument("--backfill-repo-root", action="store_true",
                        help="一次性回填 repo_root 欄位（從 repo + cwd 推導，不碰檔案系統）")
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

    if args.repair_all:
        return repair_all(args.episode_dir)

    if args.backfill_repo_root:
        return backfill_repo_root(args.episode_dir, dry_run=args.dry_run)

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
