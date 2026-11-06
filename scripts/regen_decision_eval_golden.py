"""重產決策評測集的 golden（行為快照）。必須明確帶 ``--confirm-regenerate``，否則只顯示差異、不寫檔。

用法（repo 根目錄）::

    python scripts/regen_decision_eval_golden.py                       # 只顯示與現有 golden 的差異
    python scripts/regen_decision_eval_golden.py --confirm-regenerate  # 確認是預期的行為改變後才覆寫

兩組 golden（``golden.jsonl``、``timed_golden.jsonl``）一併處理，只覆寫內容有變的那組。
覆寫前必須先確認比對失敗是「預期的行為改變」；規格見 ``tests/uep/fixtures/decision_eval/README.md``。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.uep import decision_eval_runner as runner
from tests.uep import decision_eval_timed_runner as timed


def _git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _jsonl(rows: list[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


def _diff_ids(old_path: Path, snapshot: list[dict]) -> list[str] | None:
    """與現有 golden 不同的 id（檔案不存在回傳 None）。"""
    if not old_path.exists():
        return None
    old = {r["id"]: r for r in runner.read_jsonl_file(old_path)}
    diff = [r["id"] for r in snapshot if old.get(r["id"]) != r]
    return diff + [i for i in old if i not in {r["id"] for r in snapshot}]


def _report(label: str, diff: list[str] | None) -> None:
    if diff is None:
        print(f"{label}：尚無 golden 檔")
    else:
        print(f"{label}與現有 golden 不同：{len(diff)} 筆" + (f"（{', '.join(diff)}）" if diff else ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--confirm-regenerate", action="store_true", help="確認行為改變為預期後，覆寫 golden")
    sys.stdout.reconfigure(encoding="utf-8")
    args = parser.parse_args(argv)

    snapshot = runner.current_snapshot()
    timed_snapshot = timed.current_timed_snapshot()
    main_diff = _diff_ids(runner.GOLDEN_PATH, snapshot)
    timed_diff = _diff_ids(timed.TIMED_GOLDEN_PATH, timed_snapshot)
    _report("", main_diff)
    _report("timed ", timed_diff)
    if not args.confirm_regenerate:
        print("未帶 --confirm-regenerate，未寫入。")
        return 0

    # 兩組 golden 各自只在內容有變時才覆寫，避免無關的 meta 更新（commit／時間戳）
    meta = json.loads(runner.META_PATH.read_text(encoding="utf-8")) if runner.META_PATH.exists() else {}
    stamp = {"generated_at": datetime.now(UTC).isoformat(timespec="seconds"), "commit": _git_commit()}
    if main_diff is None or main_diff:
        runner.GOLDEN_PATH.write_text(_jsonl(snapshot), encoding="utf-8", newline="\n")
        meta.update({
            "kind": "characterization",
            "note": "現況行為快照，非人工真值，不可用於訓練",
            **stamp,
            "proposers": runner.engine_versions(),
            "records": len(snapshot),
        })
        print(f"已寫入 {runner.GOLDEN_PATH.name}（{len(snapshot)} 筆）")
    if timed_diff is None or timed_diff:
        timed.TIMED_GOLDEN_PATH.write_text(_jsonl(timed_snapshot), encoding="utf-8", newline="\n")
        meta["timed"] = {**stamp, "proposers": {"rules.default": runner.engine_versions()["rules.default"]},
                         "records": len(timed_snapshot)}
        print(f"已寫入 {timed.TIMED_GOLDEN_PATH.name}（{len(timed_snapshot)} 筆）")
    runner.META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"已更新 {runner.META_PATH.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
