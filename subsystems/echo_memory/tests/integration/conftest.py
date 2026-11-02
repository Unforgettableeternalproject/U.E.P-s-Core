"""端到端整合測試共用 fixtures。"""
import os
import pytest

LIVE_API_ENV_VARS = ("GOOGLE_API_KEY", "GCP_PROJECT_ID")


def pytest_configure(config):
    """註冊自定義 markers。"""
    config.addinivalue_line("markers", "live_api: 需要真實 Gemini API")
    config.addinivalue_line("markers", "slow: 執行時間較長")


@pytest.fixture
def require_live_api():
    """跳過沒有設定 API key 的環境。"""
    if not any(os.getenv(v) for v in LIVE_API_ENV_VARS):
        pytest.skip("需要設定 GOOGLE_API_KEY 或 GCP_PROJECT_ID")
