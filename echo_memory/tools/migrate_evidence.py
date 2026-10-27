"""
既有資料遷移 CLI：為舊 Episode 補上 legacy_unknown 證據（EM-008）。

用法：
    python -m echo_memory.tools.migrate_evidence <data_dir> [--namespace ID] [--dry-run | --apply]

預設 dry-run，只報告將遷移多少 Episode／新增多少 unit，不寫任何檔案。
--apply 才會實際遷移（先備份 episodes.json，idempotent，可重跑）。
<data_dir> 為 storage root；有 --namespace 時解析成其 namespace 目錄，
未指定則依 MemoryEngine 預設（local-default）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from echo_memory.namespace import normalize_namespace_id, resolve_namespace_path
from echo_memory.storage.evidence import migrate_evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m echo_memory.tools.migrate_evidence",
        description="為舊 Episode 補上 legacy_unknown 證據（預設 dry-run）。",
    )
    parser.add_argument("data_dir", help="storage root 目錄")
    parser.add_argument("--namespace", default="local-default", help="namespace id（預設 local-default）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只報告，不寫入（預設）")
    mode.add_argument("--apply", action="store_true", help="實際執行遷移（會先備份）")
    args = parser.parse_args(argv)

    root = Path(args.data_dir)
    if not root.is_dir():
        print(f"錯誤：找不到目錄 {root}", file=sys.stderr)
        return 2
    ns_dir = resolve_namespace_path(root, normalize_namespace_id(args.namespace))
    if not (ns_dir / "episodic" / "episodes.json").exists():
        print(f"錯誤：{ns_dir} 下沒有 episodic/episodes.json", file=sys.stderr)
        return 2

    dry_run = not args.apply
    report = migrate_evidence(ns_dir, dry_run=dry_run)
    mode_label = "dry-run（未寫入）" if dry_run else "已執行"
    print(f"[{mode_label}] namespace 目錄: {ns_dir}")
    print(f"Episode 總數: {report.episodes_total}")
    print(f"需遷移／已遷移 Episode: {report.episodes_migrated}")
    print(f"新增 legacy_unknown unit: {report.units_added}")
    if report.backup_path:
        print(f"備份: {report.backup_path}")
    if dry_run and report.episodes_migrated:
        print("加上 --apply 才會實際遷移。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
