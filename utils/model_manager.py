"""
模型管理工具

處理首次運行時的模型檢查、下載和配置
由於本地模型文件過大（約40GB），不包含在打包結果中
"""

import os
import sys
import json
from pathlib import Path
from typing import Dict, List, Optional
from utils.debug_helper import info_log, error_log, warning_log


class ModelManager:
    """模型管理器 - 處理模型的檢查、下載和配置"""
    
    def __init__(self, config_path: str = "configs/config.yaml"):
        """初始化模型管理器"""
        self.config_path = config_path
        self.models_dir = Path("models")
        self.model_manifest = self.models_dir / "model_manifest.json"
        
        # 模型清單 - 記錄所有需要的模型
        self.required_models = {
            "nlp": [
                {
                    "name": "bio_tagger",
                    "path": "models/nlp/bio_tagger",
                    "size_gb": 0.1,
                    "description": "BIO 標記器 - 用於 NLP 意圖識別和實體提取",
                    "download_url": None,  # 訓練模型，需要從開發環境複製
                    "required": True
                },
            ],
            "stt": [
                {
                    "name": "whisper-large-v3",
                    "path": "models/stt/whisper/whisper-large-v3",
                    "size_gb": 23.2,
                    "description": "Whisper Large V3 語音辨識模型",
                    "download_url": "https://huggingface.co/openai/whisper-large-v3",
                    "required": True
                },
            ],
            # 注意: TTS 模型已內建在 modules/tts_module/checkpoints/ 中
            # 角色特徵文件位於 models/tts/*.pt
        }
    
    def check_models_exist(self) -> Dict[str, List[str]]:
        """
        檢查所有必要的模型是否存在
        
        Returns:
            Dict[str, List[str]]: 缺失的模型列表，按類別分組
        """
        missing = {}
        
        for category, models in self.required_models.items():
            missing_in_category = []
            
            for model in models:
                model_path = Path(model["path"])
                
                if not model_path.exists():
                    if model.get("required", True):
                        missing_in_category.append(model["name"])
                        warning_log(f"缺少必要模型: {model['name']} ({model['description']})")
                else:
                    info_log(f"✓ 模型已存在: {model['name']}")
            
            if missing_in_category:
                missing[category] = missing_in_category
        
        return missing
    
    def save_model_manifest(self):
        """儲存模型清單到 JSON 文件"""
        try:
            self.models_dir.mkdir(parents=True, exist_ok=True)
            
            with open(self.model_manifest, 'w', encoding='utf-8') as f:
                json.dump(self.required_models, f, ensure_ascii=False, indent=2)
            
            info_log(f"模型清單已儲存到: {self.model_manifest}")
            
        except Exception as e:
            error_log(f"儲存模型清單時發生錯誤: {e}")
    
    def load_model_manifest(self) -> Optional[Dict]:
        """從 JSON 文件載入模型清單"""
        try:
            if self.model_manifest.exists():
                with open(self.model_manifest, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return None
            
        except Exception as e:
            error_log(f"載入模型清單時發生錯誤: {e}")
            return None
    
    def print_model_requirements(self):
        """打印模型需求清單"""
        print("\n" + "=" * 70)
        print("U.E.P 系統模型需求清單")
        print("=" * 70)
        
        total_size = 0
        
        for category, models in self.required_models.items():
            print(f"\n【{category.upper()}】")
            
            for model in models:
                size_gb = model.get("size_gb", 0)
                total_size += size_gb
                required_mark = "✓ 必要" if model.get("required", True) else "  可選"
                
                print(f"  {required_mark} | {model['name']}")
                print(f"           {model['description']}")
                print(f"           大小: ~{size_gb:.1f} GB")
                print(f"           路徑: {model['path']}")
                
                if model.get("download_url"):
                    print(f"           下載: {model['download_url']}")
                
                print()
        
        print("=" * 70)
        print(f"總計需要約 {total_size:.1f} GB 的磁碟空間")
        print("=" * 70)
    
    def setup_first_run(self) -> bool:
        """
        首次運行設置 - 檢查並指引用戶安裝模型
        
        Returns:
            bool: 是否所有必要模型都已準備好
        """
        info_log("執行首次運行檢查...")
        
        # 檢查缺失的模型
        missing = self.check_models_exist()
        
        if not missing:
            info_log("✓ 所有必要的模型都已就緒")
            return True
        
        # 有缺失的模型
        print("\n" + "!" * 70)
        print("警告: 偵測到缺少必要的模型文件")
        print("!" * 70)
        
        print("\n缺少的模型:")
        for category, model_names in missing.items():
            print(f"\n【{category.upper()}】")
            for name in model_names:
                print(f"  - {name}")
        
        print("\n" + "=" * 70)
        print("如何安裝模型:")
        print("=" * 70)
        print("\n方法 1: 從原始開發環境複製")
        print("  如果您有原始的開發環境，可以直接複製 models 目錄到此應用程式資料夾")
        print(f"  目標路徑: {self.models_dir.absolute()}")
        
        print("\n方法 2: 重新下載模型")
        print("  某些模型可以從 HuggingFace 或其他來源下載")
        print("  請參考 DEPLOYMENT.md 中的詳細說明")
        
        print("\n方法 3: 手動配置")
        print("  如果模型已存在於其他位置，可以修改 configs/config.yaml 中的路徑配置")
        
        print("\n" + "=" * 70)
        
        # 保存模型清單供參考
        self.save_model_manifest()
        print(f"\n模型清單已儲存到: {self.model_manifest}")
        print("您可以查看此文件了解所有模型的詳細資訊")
        
        # 詢問用戶是否要繼續（在沒有模型的情況下運行可能會出錯）
        print("\n" + "?" * 70)
        response = input("是否要在沒有這些模型的情況下繼續運行？(y/N): ").strip().lower()
        
        if response == 'y':
            warning_log("用戶選擇在缺少模型的情況下繼續運行")
            print("\n⚠️  警告: 某些功能可能無法正常工作")
            return True
        else:
            info_log("用戶選擇中止運行，等待安裝模型")
            print("\n請先安裝必要的模型，然後重新啟動應用程式")
            return False
    
    def create_gitkeep_files(self):
        """在模型目錄中創建 .gitkeep 文件以保持目錄結構"""
        # 只為配置中的類別創建目錄
        for category in self.required_models.keys():
            category_dir = self.models_dir / category
            category_dir.mkdir(parents=True, exist_ok=True)
            
            gitkeep = category_dir / ".gitkeep"
            if not gitkeep.exists():
                gitkeep.touch()
                info_log(f"創建 .gitkeep: {gitkeep}")
        
        # TTS 角色特徵目錄
        tts_dir = self.models_dir / "tts"
        tts_dir.mkdir(parents=True, exist_ok=True)
        (tts_dir / ".gitkeep").touch()
        info_log(f"創建 .gitkeep: {tts_dir / '.gitkeep'}")


def check_and_setup_models() -> bool:
    """
    檢查並設置模型（供 Entry.py 或 production_runner.py 調用）
    
    Returns:
        bool: 是否可以繼續運行
    """
    manager = ModelManager()
    
    # 首次運行或模型檢查
    can_continue = manager.setup_first_run()
    
    if not can_continue:
        return False
    
    return True


if __name__ == "__main__":
    """獨立運行時顯示模型需求"""
    manager = ModelManager()
    manager.print_model_requirements()
    
    print("\n執行模型檢查...")
    missing = manager.check_models_exist()
    
    if missing:
        print("\n缺少以下模型:")
        for category, models in missing.items():
            print(f"  {category}: {', '.join(models)}")
    else:
        print("\n✓ 所有模型都已就緒")
