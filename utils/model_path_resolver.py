"""
模型路徑解析工具
用於打包後的應用程式能夠從多個可能的位置查找模型文件

使用場景：
1. 開發環境：models/ 在專案根目錄下
2. 打包環境：models/ 在可執行文件所在目錄旁邊
3. 使用者環境：使用者手動放置 models/ 資料夾

查找順序：
1. 可執行文件所在目錄/../models/
2. 當前工作目錄/models/
3. sys._MEIPASS/models/ (PyInstaller 臨時目錄，通常沒有模型)
4. 原始專案路徑 (僅在開發環境)
"""

import os
import sys
from pathlib import Path
from typing import Optional
from utils.debug_helper import debug_log


def get_application_root() -> Path:
    """
    獲取應用程式根目錄
    
    Returns:
        Path: 應用程式根目錄路徑
        - 開發環境：專案根目錄
        - 打包環境：可執行文件所在目錄
    """
    if getattr(sys, 'frozen', False):
        # 打包環境：獲取可執行文件所在目錄
        # sys.executable 是 UEP.exe 的路徑
        # 我們需要返回 UEP.exe 所在的目錄
        return Path(sys.executable).parent
    else:
        # 開發環境：返回專案根目錄
        # 假設此文件在 utils/ 目錄下
        return Path(__file__).parent.parent


def resolve_model_path(relative_path: str) -> Optional[Path]:
    """
    解析模型相對路徑為絕對路徑
    
    查找順序：
    1. 應用程式根目錄旁的 models/ (打包環境優先)
    2. 當前工作目錄的 models/
    3. PyInstaller 臨時目錄的 models/ (通常為空)
    
    Args:
        relative_path: 相對於 models/ 的路徑，例如 "nlp/bio_tagger"
        
    Returns:
        Optional[Path]: 找到的模型絕對路徑，如果未找到則返回 None
        
    Example:
        >>> path = resolve_model_path("nlp/bio_tagger")
        >>> # 可能返回: C:/UEP/models/nlp/bio_tagger
    """
    # 清理路徑
    relative_path = relative_path.replace('\\', '/')
    if relative_path.startswith('models/'):
        relative_path = relative_path[7:]  # 移除 'models/' 前綴
    if relative_path.startswith('./models/'):
        relative_path = relative_path[9:]  # 移除 './models/' 前綴
    
    search_paths = []
    
    # 1. 應用程式根目錄旁的 models/
    app_root = get_application_root()
    app_models = app_root / "models" / relative_path
    search_paths.append(("應用程式目錄", app_models))
    
    # 2. 當前工作目錄的 models/
    cwd_models = Path.cwd() / "models" / relative_path
    search_paths.append(("當前工作目錄", cwd_models))
    
    # 3. PyInstaller 臨時目錄 (通常不包含大模型文件)
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        meipass_models = Path(sys._MEIPASS) / "models" / relative_path
        search_paths.append(("PyInstaller 臨時目錄", meipass_models))
    
    # 按順序查找
    debug_log(3, f"[ModelPathResolver] 開始查找模型: {relative_path}")
    for location, path in search_paths:
        debug_log(3, f"[ModelPathResolver]   檢查 {location}: {path}")
        if path.exists():
            debug_log(2, f"[ModelPathResolver] ✓ 找到模型於 {location}: {path}")
            return path
    
    # 未找到模型
    debug_log(1, f"[ModelPathResolver] ✗ 未找到模型: {relative_path}")
    debug_log(1, f"[ModelPathResolver]   已搜索位置:")
    for location, path in search_paths:
        debug_log(1, f"[ModelPathResolver]     - {location}: {path}")
    
    return None


def get_models_directory() -> Path:
    """
    獲取 models 目錄的路徑
    
    Returns:
        Path: models 目錄路徑
        - 打包環境：可執行文件所在目錄/models/
        - 開發環境：專案根目錄/models/
    """
    app_root = get_application_root()
    models_dir = app_root / "models"
    
    # 如果目錄不存在，記錄警告
    if not models_dir.exists():
        debug_log(1, f"[ModelPathResolver] 警告: models 目錄不存在: {models_dir}")
        debug_log(1, f"[ModelPathResolver] 使用者需要在此位置創建 models 資料夾並放置模型文件")
    
    return models_dir


def check_model_availability(model_type: str, model_name: str) -> dict:
    """
    檢查特定模型是否可用
    
    Args:
        model_type: 模型類型，如 'nlp', 'stt', 'tts'
        model_name: 模型名稱，如 'bio_tagger', 'whisper-large-v3'
        
    Returns:
        dict: {
            'available': bool,  # 模型是否可用
            'path': Optional[Path],  # 模型路徑
            'message': str  # 狀態消息
        }
    """
    relative_path = f"{model_type}/{model_name}"
    model_path = resolve_model_path(relative_path)
    
    if model_path and model_path.exists():
        return {
            'available': True,
            'path': model_path,
            'message': f'模型可用: {model_path}'
        }
    else:
        models_dir = get_models_directory()
        expected_path = models_dir / model_type / model_name
        return {
            'available': False,
            'path': None,
            'message': f'模型不可用。請將 {model_name} 放置於: {expected_path}'
        }


if __name__ == "__main__":
    # 測試模組
    print("=" * 60)
    print("模型路徑解析器測試")
    print("=" * 60)
    
    print(f"\n應用程式根目錄: {get_application_root()}")
    print(f"Models 目錄: {get_models_directory()}")
    
    # 測試常用模型路徑
    test_models = [
        "nlp/bio_tagger",
        "stt/whisper/whisper-large-v3",
        "tts/checkpoints"
    ]
    
    print("\n模型查找測試:")
    for model in test_models:
        result = resolve_model_path(model)
        status = "✓ 找到" if result else "✗ 未找到"
        print(f"  {status} - {model}")
        if result:
            print(f"         位置: {result}")
