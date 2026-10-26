"""uep 設定載入：資料庫連線字串自環境變數或 repo 根 ``.env`` 讀取，不硬編碼。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[1]


class UepSettings(BaseSettings):
    """環境變數優先於 ``.env``；未設定的值為 None，由使用端決定如何處理。"""

    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    database_url: SecretStr | None = Field(default=None, validation_alias="UEP_DATABASE_URL")
    test_database_url: SecretStr | None = Field(default=None, validation_alias="UEP_TEST_DATABASE_URL")
    # Host 本機 epoch 檔（D26，DB 外）；未設定時用使用者資料目錄的預設位置
    host_epoch_file: Path | None = Field(default=None, validation_alias="UEP_HOST_EPOCH_FILE")
    # 全域停止入口的 token（D36）；未設定時入口一律拒絕（fail-closed），不使用預設值
    stop_token: SecretStr | None = Field(default=None, validation_alias="UEP_STOP_TOKEN")


@lru_cache(maxsize=1)
def get_settings() -> UepSettings:
    return UepSettings()
