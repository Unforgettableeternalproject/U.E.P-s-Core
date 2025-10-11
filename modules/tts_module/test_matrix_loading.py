"""
測試 lite_engine 的矩陣加載
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from lite_engine import IndexTTSLite

print("="*70)
print(" 測試矩陣加載 ".center(70))
print("="*70)

# 初始化引擎
engine = IndexTTSLite(
    cfg_path="checkpoints/config.yaml",
    model_dir="checkpoints",
    use_fp16=True,
    use_cuda_kernel=False
)

print("\n檢查矩陣:")
print(f"  emo_matrix type: {type(engine.emo_matrix)}")
print(f"  emo_matrix length: {len(engine.emo_matrix)}")
print(f"  spk_matrix type: {type(engine.spk_matrix)}")
print(f"  spk_matrix length: {len(engine.spk_matrix)}")
print(f"  emo_num: {engine.emo_num}")

for i, (emo, spk) in enumerate(zip(engine.emo_matrix, engine.spk_matrix)):
    print(f"\n  情感類別 {i}:")
    print(f"    emo_matrix shape: {emo.shape}")
    print(f"    spk_matrix shape: {spk.shape}")

print("\n✅ 矩陣加載測試完成!")
