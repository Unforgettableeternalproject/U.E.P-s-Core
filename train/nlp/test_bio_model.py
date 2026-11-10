"""
快速手動檢視 BIO 模型與意圖分類器的輸出（不是 pytest 測試）

使用新 Core 的載入器 ``uep.echo.nlp_wiring``，不依賴已刪除的 legacy 模組。
用法（repo 根目錄）：
    python train/nlp/test_bio_model.py --help
    python train/nlp/test_bio_model.py [--model-dir models/nlp]
"""

import argparse
import sys
from pathlib import Path

# 添加專案根目錄到 Python 路徑（讓 uep 可被 import）
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

CASES = [
    "Hello there",
    "How are you doing today?",
    "Open the file manager",
    "Search for documents from last week",
    "Once the download finishes",
    "Hey, can you help me organize my files?",
    "Good morning! Please start the backup",
    "After the update completes, restart the system",
    "Hi there, after finishing the report, create a backup and send it to my manager",
    "Hmm...",
    "asdfghjkl",
]


def run_cases(model_dir: Path) -> int:
    from uep.echo import nlp_wiring

    missing = nlp_wiring.missing_model_files(model_dir)
    if missing:
        print(f"找不到模型檔（{model_dir}）：{', '.join(missing)}")
        return 1
    print("載入模型...")
    backend, version = nlp_wiring.load_backend(model_dir)
    print(f"模型載入完成：{version}\n")
    for i, text in enumerate(CASES, 1):
        features = backend.analyze(text)
        print(f"[測試 {i}] {text}")
        print(f"  意圖分類：{features.intent}（{features.intent_confidence:.3f}）")
        for seg in features.segments:
            print(f"  BIO 區段：{seg.label}（{seg.words} 詞，信心度 {seg.confidence:.3f}）")
        if not features.segments:
            print("  BIO 區段：無")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="手動檢視 BIO 標註模型與意圖分類器的輸出")
    parser.add_argument("--model-dir", default=str(project_root / "models" / "nlp"), help="models/nlp 目錄")
    args = parser.parse_args(argv)
    return run_cases(Path(args.model_dir))


if __name__ == "__main__":
    sys.exit(main())
