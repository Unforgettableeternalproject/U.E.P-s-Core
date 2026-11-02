"""
T-12: Config Loader 測試
"""

import os
import pytest
from echo_stt.core.config import load_config, get, reset_cache


@pytest.fixture(autouse=True)
def _clear_cache():
    """每個測試前清除快取"""
    reset_cache()
    yield
    reset_cache()


class TestLoadConfig:
    def test_loads_default_config(self):
        """預設路徑的 stt_config.yaml 應可載入"""
        config = load_config()
        assert isinstance(config, dict)
        # 我們知道 stt_config.yaml 存在且有 whisper key
        assert "whisper" in config

    def test_missing_file_returns_empty(self, tmp_path):
        """不存在的檔案不 crash，回傳空 dict"""
        config = load_config(str(tmp_path / "nonexistent.yaml"))
        assert config == {}

    def test_force_reload(self):
        """force_reload 應重新讀取"""
        config1 = load_config()
        config2 = load_config(force_reload=True)
        assert config1 == config2


class TestGet:
    def test_dot_notation(self):
        """可用 dot-notation 取值"""
        config = load_config()
        model_id = get("whisper.model_id", config=config)
        assert model_id is not None
        assert "whisper" in model_id.lower()

    def test_nested_key(self):
        """巢狀 key 存取"""
        config = load_config()
        sample_rate = get("audio.sample_rate", config=config)
        assert sample_rate == 16000

    def test_default_value(self):
        """不存在的 key 回傳預設值"""
        config = load_config()
        val = get("nonexistent.key.path", "fallback", config=config)
        assert val == "fallback"

    def test_top_level_key(self):
        """頂層 key 存取"""
        config = load_config()
        val = get("whisper", config=config)
        assert isinstance(val, dict)
