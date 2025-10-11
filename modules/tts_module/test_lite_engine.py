"""
IndexTTS Lite Engine 獨立測試
直接測試lite_engine.py的功能,不依賴模組初始化
"""

import sys
import os
from pathlib import Path

# 將tts_module目錄添加到路徑,使lite_engine能使用相對導入
tts_module_dir = Path(__file__).parent
project_root = tts_module_dir.parent.parent

# 創建虛擬的modules.tts_module包
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# 手動設置包結構
import types
modules = types.ModuleType('modules')
modules.__path__ = [str(project_root / 'modules')]
sys.modules['modules'] = modules

tts_module_pkg = types.ModuleType('modules.tts_module')
tts_module_pkg.__path__ = [str(tts_module_dir)]
tts_module_pkg.__package__ = 'modules.tts_module'
sys.modules['modules.tts_module'] = tts_module_pkg

print("\n" + "=" * 60)
print("IndexTTS Lite Engine - 快速測試")
print("=" * 60 + "\n")

# 測試 1: 檢查文件
print("1. 檢查必要文件...")
checkpoints_dir = tts_module_dir / "checkpoints"
character_file = project_root / "models" / "tts" / "uep.pt"

required_files = [
    checkpoints_dir / "config.yaml",
    checkpoints_dir / "gpt.pth",
    checkpoints_dir / "s2mel.pth",
    character_file,
]

all_files_exist = True
for file_path in required_files:
    if file_path.exists():
        size_mb = file_path.stat().st_size / (1024*1024)
        print(f"  ✅ {file_path.name} ({size_mb:.1f} MB)")
    else:
        print(f"  ❌ {file_path.name} 不存在")
        all_files_exist = False

if not all_files_exist:
    print("\n❌ 缺少必要文件,無法繼續測試")
    sys.exit(1)

print()

# 測試 2: 導入模組
print("2. 導入 lite_engine...")
try:
    from modules.tts_module.lite_engine import IndexTTSLite
    print("  ✅ 導入成功\n")
except Exception as e:
    print(f"  ❌ 導入失敗: {e}\n")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# 測試 3: 初始化引擎
print("3. 初始化引擎 (可能需要幾分鐘)...")
try:
    engine = IndexTTSLite(
        cfg_path=str(checkpoints_dir / "config.yaml"),
        model_dir=str(checkpoints_dir)
    )
    print("  ✅ 引擎初始化成功\n")
except Exception as e:
    print(f"  ❌ 初始化失敗: {e}\n")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# 測試 4: 加載角色
print("4. 加載角色特徵...")
try:
    engine.load_character(str(character_file))
    print("  ✅ 角色加載成功\n")
except Exception as e:
    print(f"  ❌ 角色加載失敗: {e}\n")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# 測試 5: 語音合成
print("5. 測試語音合成...")
try:
    output_dir = tts_module_dir / "temp"
    output_dir.mkdir(exist_ok=True)
    output_file = output_dir / "test_output.wav"
    
    test_text = "Hello, this is a test."
    emotion_vector = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]  # Happy
    
    print(f"  文本: {test_text}")
    print(f"  情緒: {emotion_vector}")
    print(f"  輸出: {output_file.name}")
    print("  合成中...")
    
    engine.synthesize(
        text=test_text,
        output_path=str(output_file),
        emotion_vector=emotion_vector,
        max_emotion_strength=0.3
    )
    
    if output_file.exists():
        size_kb = output_file.stat().st_size / 1024
        print(f"  ✅ 合成成功! 文件大小: {size_kb:.1f} KB\n")
    else:
        print("  ❌ 輸出文件未生成\n")
        sys.exit(1)
        
except Exception as e:
    print(f"  ❌ 合成失敗: {e}\n")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("=" * 60)
print("🎉 所有測試通過! IndexTTS遷移成功!")
print("=" * 60)
print()
