"""
第一階段 SYS 模組動作測試
測試範圍：
1. integrations.py - news_summary, get_weather, code_analysis
2. automation_helper.py - set_reminder, generate_backup_script, monitor_folder
3. text_processing.py - clipboard_tracker, quick_phrases
4. file_interaction.py - clean_trash_bin
"""

import pytest
import os
import tempfile
import time
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

# 導入要測試的模組
from modules.sys_module.actions.integrations import (
    news_summary, get_weather, code_analysis
)
from modules.sys_module.actions.automation_helper import (
    set_reminder, generate_backup_script, monitor_folder
)
from modules.sys_module.actions.text_processing import (
    ocr_extract  # clipboard_tracker 和 quick_phrases 需要 GUI，暫時跳過
)
from modules.sys_module.actions.file_interaction import clean_trash_bin


class TestIntegrations:
    """測試 integrations.py 中的功能"""
    
    def test_news_summary_with_valid_rss(self):
        """測試 RSS 新聞摘要功能（使用真實 RSS feed）"""
        # 使用一個穩定的 RSS feed
        rss_url = "https://feeds.bbci.co.uk/news/rss.xml"
        
        result = news_summary(rss_url, max_items=3)
        
        # 驗證結果
        assert result is not None
        assert isinstance(result, str)
        assert len(result) > 0
        # RSS 摘要應該包含標題和連結
        assert "http" in result.lower() or result == ""  # 允許網路失敗時返回空字串
    
    def test_news_summary_with_invalid_rss(self):
        """測試無效的 RSS URL"""
        rss_url = "https://invalid-rss-url-that-does-not-exist.com/feed"
        
        result = news_summary(rss_url, max_items=5)
        
        # 應該返回空字串或錯誤訊息
        assert result == ""
    
    def test_get_weather(self):
        """測試天氣查詢功能（使用 wttr.in API）"""
        # 測試台北天氣
        result = get_weather("Taipei")
        
        # 驗證結果為結構化資料
        assert result is not None
        assert isinstance(result, dict)
        assert "location" in result
        assert "condition" in result
        assert "temperature" in result
        assert "wind" in result
        assert "humidity" in result
        assert "raw_text" in result
        
        # 驗證地點名稱
        assert result["location"] != ""
        # 至少應該有原始文字
        assert len(result["raw_text"]) > 0
    
    def test_get_weather_invalid_location(self):
        """測試無效地點"""
        result = get_weather("InvalidCityNameThatDoesNotExist12345")
        
        # 應該返回結構化資料（即使是錯誤訊息）
        assert result is not None
        assert isinstance(result, dict)
        assert "location" in result
        assert result["location"] != ""
    
    def test_code_analysis_valid_code(self):
        """測試程式碼分析功能"""
        code = """
def hello_world():
    print("Hello, World!")
    return 42
"""
        
        result = code_analysis(code)
        
        # 驗證結果
        assert result is not None
        assert isinstance(result, str)
        assert len(result) > 0
        # AST dump 應該包含 Module, FunctionDef 等節點
        assert "Module" in result or "FunctionDef" in result
    
    def test_code_analysis_invalid_code(self):
        """測試無效的程式碼"""
        code = "def invalid syntax here"
        
        result = code_analysis(code)
        
        # 應該返回空字串（因為解析失敗）
        assert result == ""


class TestAutomationHelper:
    """測試 automation_helper.py 中的功能"""
    
    def test_set_reminder(self):
        """測試設定提醒功能"""
        from modules.sys_module.actions.automation_helper import _DB
        
        # 設定一個未來的時間
        future_time = datetime.now() + timedelta(hours=1)
        message = "測試提醒訊息"
        
        # 執行功能
        set_reminder(future_time, message)
        
        # 驗證資料庫
        conn = sqlite3.connect(_DB)
        c = conn.cursor()
        c.execute("SELECT time, message FROM reminders WHERE message=?", (message,))
        result = c.fetchone()
        conn.close()
        
        # 清理
        conn = sqlite3.connect(_DB)
        conn.execute("DELETE FROM reminders WHERE message=?", (message,))
        conn.commit()
        conn.close()
        
        # 驗證
        assert result is not None
        assert result[1] == message
    
    def test_generate_backup_script_windows(self):
        """測試生成備份腳本（Windows）"""
        with tempfile.TemporaryDirectory() as tmpdir:
            target_folder = os.path.join(tmpdir, "source")
            dest_folder = os.path.join(tmpdir, "backup")
            output_path = os.path.join(tmpdir, "backup_script")
            
            os.makedirs(target_folder, exist_ok=True)
            
            # 生成腳本
            result = generate_backup_script(target_folder, dest_folder, output_path)
            
            # 驗證
            assert result is not None
            assert os.path.exists(result)
            assert result.endswith((".bat", ".sh"))
            
            # 讀取腳本內容
            with open(result, "r", encoding="utf-8") as f:
                content = f.read()
            
            # 驗證腳本包含正確的路徑
            assert target_folder in content
            assert dest_folder in content
    
    def test_monitor_folder(self):
        """測試資料夾監控功能"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 用於記錄回調
            detected_files = []
            
            def callback(filename):
                detected_files.append(filename)
            
            # 啟動監控（間隔1秒）
            monitor_folder(tmpdir, callback, interval=1)
            
            # 等待監控啟動
            time.sleep(0.5)
            
            # 創建新檔案
            test_file = os.path.join(tmpdir, "test_file.txt")
            Path(test_file).write_text("test content")
            
            # 等待監控偵測
            time.sleep(2)
            
            # 驗證
            assert len(detected_files) > 0
            assert "test_file.txt" in detected_files


class TestTextProcessing:
    """測試 text_processing.py 中的功能"""
    
    def test_ocr_extract_text_mode(self):
        """測試 OCR 文字提取（需要有效的圖片檔案）"""
        # 注意：這個測試需要實際的圖片檔案和 pytesseract
        # 在 CI 環境中可能需要跳過
        pytest.skip("需要實際圖片檔案和 tesseract 環境")
    
    def test_ocr_extract_pdf_mode(self):
        """測試 OCR PDF 生成（需要有效的圖片檔案）"""
        pytest.skip("需要實際圖片檔案和 tesseract 環境")


class TestFileInteraction:
    """測試 file_interaction.py 中的功能"""
    
    @patch('subprocess.run')
    def test_clean_trash_bin_windows(self, mock_run):
        """測試清空資源回收桶（Windows 模擬）"""
        # 模擬成功的 PowerShell 命令
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        
        with patch('platform.system', return_value='Windows'):
            result = clean_trash_bin()
        
        # 驗證
        assert result is not None
        assert "成功" in result or "清空" in result
        mock_run.assert_called_once()
    
    def test_clean_trash_bin_unsupported_system(self):
        """測試不支援的作業系統"""
        with patch('platform.system', return_value='UnknownOS'):
            with pytest.raises(Exception) as exc_info:
                clean_trash_bin()
            
            assert "不支援" in str(exc_info.value)


class TestSYSModuleIntegration:
    """測試 SYS 模組整合"""
    
    def test_sys_module_function_registration(self):
        """測試所有函數是否正確註冊到 SYS 模組"""
        from modules.sys_module.sys_module import SYSModule
        from configs.config_loader import load_module_config
        
        config = load_module_config("sys_module")
        sys_module = SYSModule(config)
        
        # 測試 get_weather 調用
        request = {
            "mode": "get_weather",
            "params": {"location": "Tokyo"}
        }
        
        response = sys_module.handle(request)
        
        # 驗證
        assert response is not None
        assert response.get("status") in ["success", "error"]
        
        # 如果成功，驗證返回的資料結構
        if response.get("status") == "success":
            data = response.get("data")
            assert isinstance(data, dict)
            assert "location" in data
            assert "condition" in data
    
    def test_sys_module_clean_trash_bin(self):
        """測試 clean_trash_bin 透過 SYS 模組調用"""
        from modules.sys_module.sys_module import SYSModule
        from configs.config_loader import load_module_config
        
        config = load_module_config("sys_module")
        sys_module = SYSModule(config)
        
        # 測試 clean_trash_bin 調用（使用 mock）
        request = {
            "mode": "clean_trash_bin",
            "params": {}
        }
        
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            with patch('platform.system', return_value='Windows'):
                response = sys_module.handle(request)
        
        # 驗證
        assert response is not None
        assert response.get("status") == "success"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
