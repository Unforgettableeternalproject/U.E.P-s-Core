"""
Phase 2 功能測試 - 測試新搬運的進階功能

測試項目：
1. news_summary - RSS 和 Selenium 爬蟲
2. translate_document - 多格式翻譯
3. media_control - 音樂播放控制
4. local_calendar - 本地 SQLite 日曆（已取代 google_calendar_agent）
"""

import pytest
from pathlib import Path
from modules.sys_module.actions.integrations import news_summary
from modules.sys_module.actions.automation_helper import media_control, local_calendar
from modules.sys_module.actions.file_interaction import translate_document


class TestNewsFeatures:
    """測試新聞爬取功能"""
    
    def test_news_rss_mode(self):
        """測試 RSS 模式（不使用 Selenium）"""
        result = news_summary(
            rss_url="https://news.google.com/rss?hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
            max_items=3,
            use_full_crawler=False
        )
        
        assert result != ""
        assert "-" in result  # 應該有列表格式
        print(f"RSS 新聞摘要：\n{result[:200]}")
    
    @pytest.mark.skipif(True, reason="Selenium 爬蟲需要 ChromeDriver，且執行較慢")
    def test_news_selenium_mode(self):
        """測試 Selenium 完整爬取模式（需要 ChromeDriver）"""
        result = news_summary(
            rss_url="https://news.google.com/rss?hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
            max_items=2,
            use_full_crawler=True
        )
        
        assert result != ""
        assert "【" in result  # 完整模式應該有標題格式
        print(f"Selenium 新聞內容：\n{result[:300]}")


class TestTranslationFeatures:
    """測試翻譯功能"""
    
    @pytest.fixture
    def sample_txt_file(self, tmp_path):
        """建立測試用 TXT 檔案"""
        file_path = tmp_path / "sample.txt"
        file_path.write_text("Hello, this is a test document.", encoding="utf-8")
        return str(file_path)
    
    @pytest.mark.skipif(True, reason="googletrans 需要網路連線且可能不穩定")
    def test_translate_txt_file(self, sample_txt_file):
        """測試翻譯 TXT 檔案"""
        result = translate_document(
            file_path=sample_txt_file,
            target_lang="zh-tw",
            source_lang="auto"
        )
        
        assert result != ""
        assert Path(result).exists()
        print(f"翻譯檔案已生成：{result}")
    
    def test_translate_unsupported_format(self):
        """測試不支援的格式"""
        with pytest.raises(ValueError):
            translate_document(
                file_path="test.xyz",
                target_lang="zh-tw"
            )


class TestMusicControlFeatures:
    """測試音樂控制功能"""
    
    def test_media_control_search(self, tmp_path):
        """測試搜尋功能（不實際播放）"""
        # 建立測試音樂資料夾
        music_folder = tmp_path / "music"
        music_folder.mkdir()
        
        # 建立測試音樂檔案（空檔案）
        (music_folder / "test_song.mp3").touch()
        
        result = media_control(
            action="search",
            song_query="test",
            music_folder=str(music_folder)
        )
        
        assert "test_song" in result or "找不到" in result
        print(f"搜尋結果：{result}")
    
    @pytest.mark.skipif(True, reason="YouTube 播放需要瀏覽器，跳過自動測試")
    def test_media_control_youtube(self):
        """測試 YouTube 播放（需要瀏覽器）"""
        result = media_control(
            action="youtube",
            song_query="test music",
            youtube=True
        )
        
        assert "YouTube" in result
        print(f"YouTube 播放結果：{result}")


class TestLocalCalendarFeatures:
    """測試本地日曆功能（SQLite）"""
    
    def test_calendar_create_event(self):
        """測試建立事件"""
        from datetime import datetime, timedelta
        
        start = (datetime.now() + timedelta(days=1)).isoformat()
        end = (datetime.now() + timedelta(days=1, hours=2)).isoformat()
        
        result = local_calendar(
            action="create",
            summary="測試會議",
            start_time=start,
            end_time=end,
            description="單元測試建立的事件"
        )
        
        assert result["status"] == "ok"
        assert "event_id" in result
        print(f"建立事件成功：ID {result['event_id']}")
    
    def test_calendar_list_events(self):
        """測試列出事件"""
        result = local_calendar(action="list")
        
        assert result["status"] == "ok"
        assert "events" in result
        print(f"事件列表：{len(result['events'])} 個事件")


class TestIntegrationReadiness:
    """測試整合準備度（檢查導入和基本結構）"""
    
    def test_all_functions_importable(self):
        """確認所有函數可以正常導入"""
        from modules.sys_module.actions.integrations import (
            news_summary,
            get_weather,
            get_world_time,
            code_analysis
        )
        from modules.sys_module.actions.file_interaction import (
            drop_and_read,
            intelligent_archive,
            summarize_tag,
            clean_trash_bin,
            translate_document
        )
        from modules.sys_module.actions.automation_helper import (
            set_reminder,
            generate_backup_script,
            monitor_folder,
            media_control,
            local_calendar
        )
        
        assert True  # 如果能導入就通過
        print("✓ 所有 Phase 2 函數都可以正常導入")
    
    def test_functions_yaml_updated(self):
        """確認 functions.yaml 包含新功能"""
        import yaml
        with open("modules/sys_module/functions.yaml", "r", encoding="utf-8") as f:
            functions = yaml.safe_load(f)
        
        assert "translate_document" in functions
        assert "local_calendar" in functions
        assert "media_control" in functions
        assert "news_summary" in functions
        
        print("✓ functions.yaml 已更新所有新功能")
    
    def test_config_yaml_updated(self):
        """確認 config.yaml 包含新功能"""
        import yaml
        with open("modules/sys_module/config.yaml", "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        
        modes = config.get("modes", [])
        assert "translate_document" in modes
        assert "local_calendar" in modes
        assert "media_control" in modes
        
        print("✓ config.yaml 已啟用所有新功能")


if __name__ == "__main__":
    # 可以直接執行此檔案進行測試
    pytest.main([__file__, "-v", "-s"])
