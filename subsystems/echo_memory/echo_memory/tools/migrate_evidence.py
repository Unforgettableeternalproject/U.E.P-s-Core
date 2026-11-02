"""
既有資料遷移 CLI：為舊 Episode 補上 legacy_unknown 證據（EM-008）。

用法：
    python -m echo_memory.tools.migrate_evidence <data_dir> [--namespace ID | --all-namespaces] [--dry-run | --apply]

預設 dry-run，只報告將遷移多少 Episode／新增多少 unit，不寫任何檔案。
--apply 才會實際遷移（先備份 episodes.json，idempotent，可重跑）。
<data_dir> 為 storage root；有 --namespace 時解析成其 namespace 目錄，
未指定則依 MemoryEngine 預設（local-default）。
--all-namespaces 掃整個資料根：預設 namespace（資料根本身）加 namespaces/ 下每個子目錄；
沒有 episodes.json 的空 namespace 會明確略過，不算錯誤（回 0）。
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
    parser.add_argument(
        "--all-namespaces", action="store_true",
        help="掃整個資料根的所有 namespace（忽略 --namespace）",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只報告，不寫入（預設）")
    mode.add_argument("--apply", action="store_true", help="實際執行遷移（會先備份）")
    args = parser.parse_args(argv)

    root = Path(args.data_dir)
    if not root.is_dir():
        print(f"錯誤：找不到目錄 {root}", file=sys.stderr)
        return 2
    dry_run = not args.apply
    mode_label = "dry-run（未寫入）" if dry_run else "已執行"

    if args.all_namespaces:
        targets = [root]
        ns_root = root / "namespaces"
        if ns_root.is_dir():
            targets.extend(sorted(p for p in ns_root.iterdir() if p.is_dir()))
    else:
        ns_dir = resolve_namespace_path(root, normalize_namespace_id(args.namespace))
        if not ns_dir.is_dir():
            print(f"錯誤：找不到 namespace 目錄 {ns_dir}", file=sys.stderr)
            return 2
        targets = [ns_dir]

    for ns_dir in targets:
        if not (ns_dir / "episodic" / "episodes.json").exists():
            print(f"[略過] {ns_dir}：沒有 episodic/episodes.json（空 namespace，無需遷移）")
            continue
        report = migrate_evidence(ns_dir, dry_run=dry_run)
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
