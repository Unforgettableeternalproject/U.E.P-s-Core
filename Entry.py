#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TestSeparateSTTSystem — Entry Point

獨立 STT 模組的入口點。
"""

import argparse
import sys
import os


def main():
    parser = argparse.ArgumentParser(
        description="TestSeparateSTTSystem — 獨立 STT 實驗專案"
    )
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # smoke test
    smoke_parser = subparsers.add_parser("smoke", help="快速煙霧測試")
    smoke_parser.add_argument("--audio", type=str, help="測試用音檔路徑")

    # benchmark
    bench_parser = subparsers.add_parser("benchmark", help="效能基準測試")
    bench_parser.add_argument("--repeat", type=int, default=3, help="重複次數")

    args = parser.parse_args()

    if args.command is None:
        # 預設進入 Rich CLI（待實作）
        print("TestSeparateSTTSystem v0.1.0")
        print("使用 --help 查看可用命令")
        print()
        print("⚠️  Rich CLI 尚未實作，請使用子命令：")
        print("  smoke     — 快速煙霧測試")
        print("  benchmark — 效能基準測試")
        return

    if args.command == "smoke":
        print("[Smoke Test] STT 模組煙霧測試（待實作）")
        return

    if args.command == "benchmark":
        print(f"[Benchmark] 效能基準測試 x{args.repeat}（待實作）")
        return


if __name__ == "__main__":
    main()
