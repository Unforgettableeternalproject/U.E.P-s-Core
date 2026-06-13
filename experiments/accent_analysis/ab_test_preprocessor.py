"""
A/B 測試：台灣腔前處理器對 IndexTTS2 輸出的影響

載入 IndexTTS2 一次，然後分別用：
  A) 原始文字
  B) 台灣腔前處理過的文字
生成語音，保存到 outputs/comparison/ab_test/

用法：
  python ab_test_preprocessor.py --device cuda
"""

import os
import sys
import time

# 設定路徑
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, SCRIPT_DIR)

from pathlib import Path
from taiwan_preprocessor import TaiwanAccentPreprocessor, TaiwanPreprocessConfig

OUTPUT_DIR = os.path.join(PROJECT_ROOT, "outputs", "comparison", "ab_test")
os.makedirs(OUTPUT_DIR, exist_ok=True)


# ── 測試用例 ──
TEST_CASES = [
    {
        "id": "01_erhua",
        "original": "那個小孩兒在哪兒玩呢？",
        "description": "兒化音 + 捲舌詞替換",
    },
    {
        "id": "02_vocab",
        "original": "我搭出租車去地鐵站買東西",
        "description": "台灣詞彙替換 + 輕聲恢復",
    },
    {
        "id": "03_tones",
        "original": "她很漂亮，喜歡在什麼時候去那個地方",
        "description": "多個輕聲恢復",
    },
    {
        "id": "04_mixed",
        "original": "你在哪兒呢？那個小孩兒搭出租車去買東西了，她覺得很漂亮",
        "description": "混合測試：兒化+詞彙+聲調",
    },
    {
        "id": "05_breezy_ref",
        "original": "知道為什麼鴨嘴獸泰瑞都不會被認出來嗎",
        "description": "BreezyVoice 預設測試句（無特殊修改預期）",
    },
]


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--no-fp16", action="store_true")
    args = parser.parse_args()

    print("=" * 60)
    print("  A/B 測試：台灣腔前處理器 vs 原始文字")
    print("=" * 60)
    print()

    # ── 初始化前處理器 ──
    preprocessor = TaiwanAccentPreprocessor(
        TaiwanPreprocessConfig(verbose=True)
    )

    # ── 先處理所有文字，顯示 A/B 對照 ──
    pairs = []
    for case in TEST_CASES:
        original = case["original"]
        processed = preprocessor.process(original)
        pairs.append((case, original, processed))
        print(f"[{case['id']}] {case['description']}")
        print(f"  A (原始): {original}")
        print(f"  B (台灣): {processed}")
        changed = "✅ 有修改" if original != processed else "⚪ 無修改"
        print(f"  {changed}")
        print()

    # ── 載入 IndexTTS2 ──
    print("🔧 載入 IndexTTS2 模型...")
    t0 = time.perf_counter()

    from Entry import build_legacy_session

    session = build_legacy_session(
        project_root=PROJECT_ROOT,
        output_dir=OUTPUT_DIR,
        device=args.device,
        use_fp16=not args.no_fp16,
        use_cuda_kernel=True,
        playback=False,  # 不播放
    )

    # 觸發模型載入
    session.load()
    load_time = time.perf_counter() - t0
    print(f"✅ 模型載入完成 ({load_time:.1f}s)")
    print()

    # ── 生成 A/B 對照 ──
    print("🔊 開始生成 A/B 對照...")
    print()

    results = []
    for case, original, processed in pairs:
        case_id = case["id"]

        # A: 原始
        print(f"  [{case_id}] A (原始): {original}")
        out_a = os.path.join(OUTPUT_DIR, f"{case_id}_A_original.wav")
        t0 = time.perf_counter()
        session.save(original, out_a)
        time_a = time.perf_counter() - t0
        print(f"    → {out_a} ({time_a:.1f}s)")

        # B: 台灣腔前處理
        if original != processed:
            print(f"  [{case_id}] B (台灣): {processed}")
            out_b = os.path.join(OUTPUT_DIR, f"{case_id}_B_taiwan.wav")
            t0 = time.perf_counter()
            session.save(processed, out_b)
            time_b = time.perf_counter() - t0
            print(f"    → {out_b} ({time_b:.1f}s)")
        else:
            print(f"  [{case_id}] B: 跳過（無修改）")
            out_b = None
            time_b = 0

        results.append({
            "id": case_id,
            "description": case["description"],
            "original": original,
            "processed": processed,
            "changed": original != processed,
            "path_a": out_a,
            "path_b": out_b,
            "time_a": time_a,
            "time_b": time_b,
        })
        print()

    # ── 報告 ──
    print("=" * 60)
    print("  A/B 測試完成")
    print("=" * 60)
    print()
    print(f"  輸出目錄: {OUTPUT_DIR}")
    print()
    for r in results:
        marker = "🔄" if r["changed"] else "⚪"
        print(f"  {marker} [{r['id']}] {r['description']}")
        print(f"     A: {r['path_a']}")
        if r["path_b"]:
            print(f"     B: {r['path_b']}")
        print()

    print("  請比較聆聽 A vs B 的差異，特別注意：")
    print("  - 兒化音是否消失")
    print("  - 詞彙替換後的發音是否正確")
    print("  - 拼音注入（聲調微調）是否改變了韻律")
    print()


if __name__ == "__main__":
    main()
