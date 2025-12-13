"""
自動化構建腳本 - 用於打包 U.E.P 應用程式

此腳本會：
1. 清理舊的構建文件
2. 檢查依賴項
3. 使用 PyInstaller 打包應用程式
4. 創建分發包
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path
from datetime import datetime
import yaml

class BuildManager:
    """構建管理器"""
    
    def __init__(self):
        """初始化構建管理器"""
        self.project_root = Path.cwd()
        self.build_dir = self.project_root / "build"
        self.dist_dir = self.project_root / "dist"
        self.spec_file = self.project_root / "build.spec"
        
        # 版本資訊
        self.version = self._get_version()
        self.build_time = datetime.now().strftime("%Y%m%d_%H%M%S")
        
    def _get_version(self) -> str:
        """從配置文件獲取版本號"""
        try:
            from configs.config_loader import load_config
            config = load_config()
            return config.get("metadata", {}).get("system_version", "0.0.0")
        except Exception as e:
            print(f"⚠️ 無法讀取版本號: {e}")
            return "0.0.0"
    
    def backup_and_modify_config(self):
        """備份並修改配置為生產模式"""
        import yaml
        
        config_path = self.project_root / "configs" / "config.yaml"
        backup_path = self.project_root / "configs" / "config.yaml.backup"
        
        if not config_path.exists():
            print("⚠️  找不到 config.yaml，跳過配置修改")
            return
        
        # 備份原配置
        shutil.copy2(config_path, backup_path)
        print(f"✓ 已備份配置: {backup_path}")
        
        # 讀取並修改配置
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        # 強制設置為生產模式
        if 'debug' in config:
            config['debug']['enabled'] = False
            print("✓ 已將 debug.enabled 設為 False")
        
        if 'logging' in config:
            config['logging']['enable_console_output'] = False
            print("✓ 已將 logging.enable_console_output 設為 False")
        
        # 寫回配置
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
        
        print("✓ 配置已修改為生產模式\n")
    
    def restore_config(self):
        """還原配置"""
        config_path = self.project_root / "configs" / "config.yaml"
        backup_path = self.project_root / "configs" / "config.yaml.backup"
        
        if backup_path.exists():
            shutil.copy2(backup_path, config_path)
            backup_path.unlink()
            print("✓ 已還原原始配置")
    
    def clean(self):
        """清理舊的構建文件"""
        print("\n" + "=" * 70)
        print("清理舊的構建文件...")
        print("=" * 70)
        
        dirs_to_clean = [self.build_dir, self.dist_dir]
        
        for dir_path in dirs_to_clean:
            if dir_path.exists():
                print(f"  刪除: {dir_path}")
                shutil.rmtree(dir_path)
            else:
                print(f"  跳過: {dir_path} (不存在)")
        
        # 清理 __pycache__
        print("\n清理 Python 緩存文件...")
        for pycache in self.project_root.rglob("__pycache__"):
            print(f"  刪除: {pycache}")
            shutil.rmtree(pycache)
        
        print("✓ 清理完成\n")
    
    def check_dependencies(self) -> bool:
        """檢查必要的依賴項"""
        print("\n" + "=" * 70)
        print("檢查依賴項...")
        print("=" * 70)
        
        required_packages = [
            ("pyinstaller", "PyInstaller"),
            ("PyQt5", "PyQt5.QtCore"),
            ("torch", "torch"),
            ("numpy", "numpy"),
        ]
        
        missing = []
        
        for display_name, import_name in required_packages:
            try:
                __import__(import_name)
                print(f"  ✓ {display_name}")
            except ImportError:
                print(f"  ✗ {display_name} (缺失)")
                missing.append(display_name)
        
        if missing:
            print(f"\n❌ 缺少以下依賴項: {', '.join(missing)}")
            print("\n請先安裝缺失的依賴項:")
            print(f"  pip install {' '.join(missing)}")
            return False
        
        print("\n✓ 所有依賴項都已就緒\n")
        return True
    
    def check_spec_file(self) -> bool:
        """檢查 spec 文件是否存在"""
        if not self.spec_file.exists():
            print(f"❌ 找不到 build.spec 文件: {self.spec_file}")
            print("請確保 build.spec 文件存在於專案根目錄")
            return False
        
        print(f"✓ 找到 spec 文件: {self.spec_file}")
        return True
    
    def build(self) -> bool:
        """執行打包"""
        print("\n" + "=" * 70)
        print(f"開始打包 U.E.P v{self.version}")
        print("=" * 70)
        
        print("\n這可能需要幾分鐘時間，請耐心等待...\n")
        
        try:
            # 執行 PyInstaller
            cmd = [
                sys.executable,
                "-m", "PyInstaller",
                "--clean",  # 清理臨時文件
                str(self.spec_file)
            ]
            
            print(f"執行命令: {' '.join(cmd)}\n")
            
            result = subprocess.run(
                cmd,
                cwd=str(self.project_root),
                check=True,
                text=True
            )
            
            if result.returncode == 0:
                print("\n✓ 打包成功")
                return True
            else:
                print(f"\n❌ 打包失敗，返回碼: {result.returncode}")
                return False
                
        except subprocess.CalledProcessError as e:
            print(f"\n❌ 打包過程中發生錯誤: {e}")
            return False
        except Exception as e:
            print(f"\n❌ 發生未預期的錯誤: {e}")
            return False
    
    def create_distribution(self):
        """創建分發包"""
        print("\n" + "=" * 70)
        print("創建分發包...")
        print("=" * 70)
        
        dist_app_dir = self.dist_dir / "UEP"
        
        if not dist_app_dir.exists():
            print(f"❌ 找不到構建輸出: {dist_app_dir}")
            return False
        
        # 創建版本化的分發目錄名稱
        release_name = f"UEP_v{self.version}_{self.build_time}"
        release_dir = self.dist_dir / release_name
        
        # 複製應用程式
        print(f"\n複製應用程式到: {release_dir}")
        shutil.copytree(dist_app_dir, release_dir)
        
        # 創建必要的目錄結構
        print("\n創建必要的目錄結構...")
        required_dirs = [
            release_dir / "models" / "nlp" / "bio_tagger",
            release_dir / "models" / "stt" / "whisper",
            release_dir / "models" / "tts",  # TTS 角色特徵檔案 (.pt)
            release_dir / "logs",
            release_dir / "memory",
            release_dir / "outputs",
            release_dir / "temp",
        ]
        
        for dir_path in required_dirs:
            dir_path.mkdir(parents=True, exist_ok=True)
            # 創建 .gitkeep
            (dir_path / ".gitkeep").touch()
            print(f"  ✓ {dir_path.relative_to(release_dir)}")
        
        # 複製重要文件
        print("\n複製文檔和說明文件...")
        docs_to_copy = [
            "README.md",
            "README.zh-tw.md",
            "CHANGELOG.md",
            "VERSION_HISTORY.md",
        ]
        
        for doc in docs_to_copy:
            src = self.project_root / doc
            if src.exists():
                shutil.copy2(src, release_dir / doc)
                print(f"  ✓ {doc}")
        
        # 創建 README 說明
        readme_content = f"""# U.E.P v{self.version} - 發行版

## 安裝說明

### 1. 首次運行前的準備

由於模型文件過大（約 40 GB），未包含在此發行包中。
首次運行時，系統會提示您安裝必要的模型文件。

### 2. 安裝模型

**方法 A: 從開發環境複製**
如果您有原始的開發環境，請將 models 目錄複製到此應用程式資料夾。

**方法 B: 重新下載**
請參考 `models/model_manifest.json` 中列出的模型清單。
某些模型可以從 HuggingFace 或其他來源下載。

### 3. 運行應用程式

**Windows:**
```
UEP.exe --production
```

**查看幫助:**
```
UEP.exe --help
```

### 4. 配置

所有配置文件位於 `configs/` 目錄下：
- `config.yaml` - 主配置文件
- `user_settings.yaml` - 使用者設定

### 5. 日誌

系統日誌儲存在 `logs/` 目錄下：
- `runtime/` - 運行日誌
- `error/` - 錯誤日誌
- `debug/` - 除錯日誌

## 故障排除

### 問題：找不到模型文件
**解決方案：** 請確保您已按照上述說明安裝模型文件。

### 問題：缺少 DLL 文件
**解決方案：** 請安裝 Visual C++ Redistributable：
https://aka.ms/vs/17/release/vc_redist.x64.exe

### 問題：無法啟動
**解決方案：** 檢查 logs/error/ 目錄下的錯誤日誌。

## 聯絡資訊

如有問題，請聯絡開發團隊或查閱完整文檔。

---
構建時間: {self.build_time}
"""
        
        with open(release_dir / "README_RELEASE.txt", "w", encoding="utf-8") as f:
            f.write(readme_content)
        
        print(f"\n✓ 創建 README_RELEASE.txt")
        
        # 創建啟動腳本
        print("\n創建啟動腳本...")
        
        # Windows 批處理文件
        batch_content = f"""@echo off
REM U.E.P v{self.version} 啟動腳本

echo ======================================
echo U.E.P v{self.version}
echo ======================================
echo.

REM 檢查是否為首次運行
if not exist "models\\nlp\\.gitkeep" (
    echo [警告] 偵測到首次運行
    echo.
    echo 請先閱讀 README_RELEASE.txt 了解如何安裝模型文件
    echo.
    pause
)

REM 啟動應用程式
echo 正在啟動 U.E.P...
echo.

UEP.exe --production

if errorlevel 1 (
    echo.
    echo [錯誤] 應用程式異常退出
    echo 請檢查 logs\\error 目錄下的錯誤日誌
    echo.
    pause
)
"""
        
        with open(release_dir / "start_uep.bat", "w", encoding="utf-8") as f:
            f.write(batch_content)
        
        print(f"  ✓ start_uep.bat")
        
        print("\n" + "=" * 70)
        print(f"✓ 分發包創建完成: {release_dir}")
        print("=" * 70)
        
        # 顯示分發包大小
        total_size = sum(f.stat().st_size for f in release_dir.rglob("*") if f.is_file())
        size_mb = total_size / (1024 * 1024)
        print(f"\n分發包大小: {size_mb:.2f} MB")
        print(f"(不含模型文件: ~40 GB)")
        
        return True
    
    def run(self):
        """執行完整的構建流程"""
        print("\n")
        print("*" * 70)
        print(f"  U.E.P 自動化構建工具")
        print(f"  版本: {self.version}")
        print(f"  構建時間: {self.build_time}")
        print("*" * 70)
        
        try:
            # 步驟 1: 清理
            self.clean()
            
            # 步驟 2: 備份並修改配置為生產模式
            print("\n" + "=" * 70)
            print("配置生產環境...")
            print("=" * 70)
            self.backup_and_modify_config()
            
            # 步驟 3: 檢查依賴
            if not self.check_dependencies():
                return False
            
            # 步驟 4: 檢查 spec 文件
            if not self.check_spec_file():
                return False
            
            # 步驟 5: 打包
            if not self.build():
                return False
            
            # 步驟 6: 創建分發包
            if not self.create_distribution():
                return False
            
            print("\n" + "*" * 70)
            print("  構建流程完成！")
            print("*" * 70)
        
        finally:
            # 無論成功失敗都還原配置
            print("\n" + "=" * 70)
            print("還原配置...")
            print("=" * 70)
            self.restore_config()
        print(f"\n分發包位置: {self.dist_dir}")
        print("\n下一步:")
        print("  1. 測試分發包中的應用程式")
        print("  2. 準備模型文件（約 40 GB）")
        print("  3. 創建安裝程式或壓縮包")
        print("\n")
        
        return True


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="U.E.P 自動化構建工具")
    parser.add_argument("--clean-only", action="store_true", help="僅清理構建文件")
    parser.add_argument("--no-clean", action="store_true", help="跳過清理步驟")
    
    args = parser.parse_args()
    
    builder = BuildManager()
    
    if args.clean_only:
        builder.clean()
        sys.exit(0)
    
    # 執行構建
    success = builder.run()
    
    sys.exit(0 if success else 1)
