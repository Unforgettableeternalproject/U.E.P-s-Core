"""重產決策評測集的 golden（行為快照）。必須明確帶 ``--confirm-regenerate``，否則只顯示差異、不寫檔。

用法（repo 根目錄）::

    python scripts/regen_decision_eval_golden.py                       # 只顯示與現有 golden 的差異
    python scripts/regen_decision_eval_golden.py --confirm-regenerate  # 確認是預期的行為改變後才覆寫

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


def _git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--confirm-regenerate", action="store_true", help="確認行為改變為預期後，覆寫 golden")
    sys.stdout.reconfigure(encoding="utf-8")
    args = parser.parse_args(argv)

    snapshot = runner.current_snapshot()
    if runner.GOLDEN_PATH.exists():
        old = {r["id"]: r for r in runner.read_jsonl_file(runner.GOLDEN_PATH)}
        diff = [r["id"] for r in snapshot if old.get(r["id"]) != r]
        diff += [i for i in old if i not in {r["id"] for r in snapshot}]
        print(f"與現有 golden 不同：{len(diff)} 筆" + (f"（{', '.join(diff)}）" if diff else ""))
    if not args.confirm_regenerate:
        print("未帶 --confirm-regenerate，未寫入。")
        return 0

    runner.GOLDEN_PATH.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in snapshot), encoding="utf-8", newline="\n"
    )
    meta = {
        "kind": "characterization",
        "note": "現況行為快照，非人工真值，不可用於訓練",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": _git_commit(),
        "proposers": runner.engine_versions(),
        "records": len(snapshot),
    }
    runner.META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"已寫入 {runner.GOLDEN_PATH.name}（{len(snapshot)} 筆）與 {runner.META_PATH.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
