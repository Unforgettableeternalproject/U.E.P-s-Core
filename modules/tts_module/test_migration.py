"""
測試IndexTTS遷移是否成功

這個測試會驗證:
1. 所有必要的套件是否正確安裝
2. 模型文件是否正確放置
3. lite_engine.py 是否能正常導入和初始化
4. 基本的語音合成功能是否正常
"""

import sys
import os
from pathlib import Path

# 添加專案根目錄到路徑
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

def test_imports():
    """測試所有必要的套件導入"""
    print("=" * 60)
    print("測試 1: 檢查套件導入")
    print("=" * 60)
    
    try:
        import torch
        print(f"✅ torch {torch.__version__}")
        print(f"   CUDA available: {torch.cuda.is_available()}")
        if torch.cuda.is_available():
            print(f"   CUDA device: {torch.cuda.get_device_name(0)}")
    except ImportError as e:
        print(f"❌ torch 導入失敗: {e}")
        return False
    
    try:
        import torchaudio
        print(f"✅ torchaudio {torchaudio.__version__}")
    except ImportError as e:
        print(f"❌ torchaudio 導入失敗: {e}")
        return False
    
    try:
        import transformers
        print(f"✅ transformers {transformers.__version__}")
    except ImportError as e:
        print(f"❌ transformers 導入失敗: {e}")
        return False
    
    try:
        from huggingface_hub import hf_hub_download
        print(f"✅ huggingface_hub")
    except ImportError as e:
        print(f"❌ huggingface_hub 導入失敗: {e}")
        return False
    
    try:
        import safetensors
        print(f"✅ safetensors")
    except ImportError as e:
        print(f"❌ safetensors 導入失敗: {e}")
        return False
    
    try:
        from omegaconf import OmegaConf
        print(f"✅ omegaconf")
    except ImportError as e:
        print(f"❌ omegaconf 導入失敗: {e}")
        return False
    
    try:
        import librosa
        print(f"✅ librosa {librosa.__version__}")
    except ImportError as e:
        print(f"❌ librosa 導入失敗: {e}")
        return False
    
    print()
    return True


def test_file_structure():
    """測試文件結構是否正確"""
    print("=" * 60)
    print("測試 2: 檢查文件結構")
    print("=" * 60)
    
    base_path = Path(__file__).parent
    
    # 檢查必要的目錄
    required_dirs = [
        "gpt",
        "s2mel/modules",
        "utils",
        "checkpoints",
        "checkpoints/hf_cache",
    ]
    
    all_exist = True
    for dir_path in required_dirs:
        full_path = base_path / dir_path
        if full_path.exists():
            print(f"✅ {dir_path}/")
        else:
            print(f"❌ {dir_path}/ 不存在")
            all_exist = False
    
    # 檢查必要的文件
    required_files = [
        "lite_engine.py",
        "gpt/model_v2.py",
        "s2mel/modules/commons.py",
        "utils/common.py",
        "checkpoints/config.yaml",
        "checkpoints/gpt.pth",
        "checkpoints/s2mel.pth",
    ]
    
    for file_path in required_files:
        full_path = base_path / file_path
        if full_path.exists():
            size = full_path.stat().st_size
            size_str = f"{size / (1024*1024):.2f} MB" if size > 1024*1024 else f"{size / 1024:.2f} KB"
            print(f"✅ {file_path} ({size_str})")
        else:
            print(f"❌ {file_path} 不存在")
            all_exist = False
    
    # 檢查角色模型
    character_path = project_root / "models" / "tts" / "uep.pt"
    if character_path.exists():
        size = character_path.stat().st_size / 1024
        print(f"✅ models/tts/uep.pt ({size:.2f} KB)")
    else:
        print(f"❌ models/tts/uep.pt 不存在")
        all_exist = False
    
    print()
    return all_exist


def test_lite_engine_import():
    """測試lite_engine能否正常導入"""
    print("=" * 60)
    print("測試 3: 導入 lite_engine")
    print("=" * 60)
    
    try:
        # 確保專案根目錄在路徑中
        import sys
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))
        
        # 使用完整包路徑導入
        import modules.tts_module.lite_engine as lite_engine_module
        IndexTTSLite = lite_engine_module.IndexTTSLite
        
        print("✅ IndexTTSLite 導入成功")
        print()
        # 將IndexTTSLite存儲在全局變數以供後續測試使用
        globals()['IndexTTSLite'] = IndexTTSLite
        return True
    except Exception as e:
        print(f"❌ IndexTTSLite 導入失敗: {e}")
        import traceback
        traceback.print_exc()
        print()
        return False


def test_engine_initialization():
    """測試引擎初始化"""
    print("=" * 60)
    print("測試 4: 初始化引擎")
    print("=" * 60)
    
    try:
        IndexTTSLite = globals().get('IndexTTSLite')
        if not IndexTTSLite:
            raise RuntimeError("IndexTTSLite未能成功導入")
        
        base_path = Path(__file__).parent
        cfg_path = base_path / "checkpoints" / "config.yaml"
        model_dir = base_path / "checkpoints"
        
        print(f"配置文件: {cfg_path}")
        print(f"模型目錄: {model_dir}")
        print()
        
        print("正在初始化引擎 (這可能需要一些時間)...")
        engine = IndexTTSLite(
            cfg_path=str(cfg_path),
            model_dir=str(model_dir)
        )
        print("✅ 引擎初始化成功")
        print()
        return engine
    except Exception as e:
        print(f"❌ 引擎初始化失敗: {e}")
        import traceback
        traceback.print_exc()
        print()
        return None


def test_character_loading(engine):
    """測試角色加載"""
    print("=" * 60)
    print("測試 5: 加載角色特徵")
    print("=" * 60)
    
    try:
        character_path = project_root / "models" / "tts" / "uep.pt"
        print(f"角色文件: {character_path}")
        print()
        
        print("正在加載角色特徵...")
        engine.load_character(str(character_path))
        print("✅ 角色加載成功")
        print()
        return True
    except Exception as e:
        print(f"❌ 角色加載失敗: {e}")
        import traceback
        traceback.print_exc()
        print()
        return False


def test_synthesis(engine):
    """測試語音合成"""
    print("=" * 60)
    print("測試 6: 語音合成")
    print("=" * 60)
    
    try:
        output_dir = Path(__file__).parent / "temp"
        output_dir.mkdir(exist_ok=True)
        output_path = output_dir / "test_output.wav"
        
        test_text = "Hello, this is a test of the IndexTTS engine."
        emotion_vector = [0.3, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]  # Happy
        
        print(f"測試文本: {test_text}")
        print(f"情緒向量: {emotion_vector}")
        print(f"輸出路徑: {output_path}")
        print()
        
        print("正在合成語音 (這可能需要一些時間)...")
        engine.synthesize(
            text=test_text,
            output_path=str(output_path),
            emotion_vector=emotion_vector,
            max_emotion_strength=0.3
        )
        
        if output_path.exists():
            size = output_path.stat().st_size / 1024
            print(f"✅ 語音合成成功! 文件大小: {size:.2f} KB")
            print(f"   輸出文件: {output_path}")
        else:
            print("❌ 語音文件未生成")
            return False
        
        print()
        return True
    except Exception as e:
        print(f"❌ 語音合成失敗: {e}")
        import traceback
        traceback.print_exc()
        print()
        return False


def main():
    """執行所有測試"""
    print("\n")
    print("╔" + "=" * 58 + "╗")
    print("║" + " IndexTTS 遷移測試 ".center(58) + "║")
    print("╚" + "=" * 58 + "╝")
    print()
    
    results = {
        "套件導入": False,
        "文件結構": False,
        "lite_engine導入": False,
        "引擎初始化": False,
        "角色加載": False,
        "語音合成": False,
    }
    
    # 測試 1: 套件導入
    results["套件導入"] = test_imports()
    if not results["套件導入"]:
        print("⚠️  套件導入失敗，請檢查依賴安裝")
        print_summary(results)
        return
    
    # 測試 2: 文件結構
    results["文件結構"] = test_file_structure()
    if not results["文件結構"]:
        print("⚠️  文件結構不完整，請檢查遷移")
        print_summary(results)
        return
    
    # 測試 3: lite_engine導入
    results["lite_engine導入"] = test_lite_engine_import()
    if not results["lite_engine導入"]:
        print("⚠️  lite_engine導入失敗，可能有路徑或依賴問題")
        print_summary(results)
        return
    
    # 測試 4: 引擎初始化
    engine = test_engine_initialization()
    results["引擎初始化"] = engine is not None
    if not results["引擎初始化"]:
        print("⚠️  引擎初始化失敗")
        print_summary(results)
        return
    
    # 測試 5: 角色加載
    results["角色加載"] = test_character_loading(engine)
    if not results["角色加載"]:
        print("⚠️  角色加載失敗")
        print_summary(results)
        return
    
    # 測試 6: 語音合成
    results["語音合成"] = test_synthesis(engine)
    
    # 總結
    print_summary(results)


def print_summary(results):
    """打印測試總結"""
    print("\n")
    print("=" * 60)
    print("測試總結")
    print("=" * 60)
    
    for test_name, result in results.items():
        status = "✅ 通過" if result else "❌ 失敗"
        print(f"{test_name:12} : {status}")
    
    passed = sum(results.values())
    total = len(results)
    
    print()
    print(f"通過: {passed}/{total}")
    
    if passed == total:
        print()
        print("🎉 所有測試通過! IndexTTS遷移成功!")
    else:
        print()
        print("⚠️  部分測試失敗，請檢查上述錯誤信息")
    
    print("=" * 60)
    print()


if __name__ == "__main__":
    main()
