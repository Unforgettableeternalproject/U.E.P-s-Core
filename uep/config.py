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
    # payload_hash 的 per-install HMAC 密鑰（D42）；未設定時寫入私人 payload 一律拒絕（fail-closed）
    payload_hash_key: SecretStr | None = Field(default=None, validation_alias="UEP_PAYLOAD_HASH_KEY")
    # 記憶資料根目錄（P3 S6）：每個 namespace 在其下 ``namespaces/<ns>``；須與 bot 的 per-user 目錄不相交（階段 X）。
    # 未設定時不組裝真記憶（fail-closed，不猜預設位置）
    memory_data_root: Path | None = Field(default=None, validation_alias="UEP_MEMORY_DATA_ROOT")
    memory_device: str = Field(default="cpu", validation_alias="UEP_MEMORY_DEVICE")
    memory_embedding_backend: str | None = Field(default=None, validation_alias="UEP_MEMORY_EMBEDDING_BACKEND")
    memory_embedding_model: str | None = Field(default=None, validation_alias="UEP_MEMORY_EMBEDDING_MODEL")

    # 推理 Host 組裝（P3 S6，uep/echo/inference_wiring.py）：子系統 repo 路徑一律由設定明確給（不猜同層目錄；
    # ES 的同層猜測在非 editable 安裝下指向 site-packages 之上而失效）。未設定時對應的 stage 拒絕組裝。
    infer_tts_repo: Path | None = Field(default=None, validation_alias="UEP_INFER_TTS_REPO")
    infer_stt_repo: Path | None = Field(default=None, validation_alias="UEP_INFER_STT_REPO")
    # Session 已遷入 Core（subsystems/echo_thought_core）：預設不設，由 Core 的 editable 安裝提供；僅作可選覆寫
    infer_session_repo: Path | None = Field(default=None, validation_alias="UEP_INFER_SESSION_REPO")
    # TTS 引擎：indextts2（預設，V25）或 vox；兩者不能同時常駐，由此單選
    infer_tts_engine: str = Field(default="indextts2", validation_alias="UEP_INFER_TTS_ENGINE")
    # VOX backend 設定：JSON 物件字串（原樣轉給 VoxBackend(config)）；空值＝不傳
    infer_vox_config: str | None = Field(default=None, validation_alias="UEP_INFER_VOX_CONFIG")
    infer_tts_device: str = Field(default="cuda", validation_alias="UEP_INFER_TTS_DEVICE")
    infer_tts_fp16: bool = Field(default=True, validation_alias="UEP_INFER_TTS_FP16")
    infer_tts_use_accel: bool = Field(default=False, validation_alias="UEP_INFER_TTS_USE_ACCEL")
    infer_tts_warmup: bool = Field(default=True, validation_alias="UEP_INFER_TTS_WARMUP")
    infer_tts_lora_scale: float | None = Field(default=None, validation_alias="UEP_INFER_TTS_LORA_SCALE")
    infer_tts_speed: float | None = Field(default=None, validation_alias="UEP_INFER_TTS_SPEED")
    infer_tts_language: str | None = Field(default=None, validation_alias="UEP_INFER_TTS_LANGUAGE")
    infer_stt_mode: str = Field(default="multichannel", validation_alias="UEP_INFER_STT_MODE")
    infer_stt_language: str | None = Field(default=None, validation_alias="UEP_INFER_STT_LANGUAGE")
    infer_llm_provider: str | None = Field(default=None, validation_alias="UEP_INFER_LLM_PROVIDER")
    infer_llm_model: str | None = Field(default=None, validation_alias="UEP_INFER_LLM_MODEL")
    infer_system_prompt: str = Field(default="", validation_alias="UEP_INFER_SYSTEM_PROMPT")
    # 各 stage prepare 的逾時（秒）；Host 啟動總上限目前為 60 秒（EchoPipelineHost.start），三者相加須小於它（未設定 UEP_INFER_HOST_START_TIMEOUT_S 時由組裝自動推得）
    # speak 未設定（None）時依引擎取預設：indextts2 為 40 秒、vox 為 90 秒（見 inference_wiring）
    infer_speak_prepare_timeout_s: float | None = Field(default=None, validation_alias="UEP_INFER_SPEAK_PREPARE_TIMEOUT_S")
    infer_think_prepare_timeout_s: float = Field(default=8.0, validation_alias="UEP_INFER_THINK_PREPARE_TIMEOUT_S")
    infer_input_prepare_timeout_s: float = Field(default=10.0, validation_alias="UEP_INFER_INPUT_PREPARE_TIMEOUT_S")
    # Host 啟動總上限（秒）；None＝由三個 prepare 逾時加總加餘裕推得（見 inference_wiring）
    infer_host_start_timeout_s: float | None = Field(default=None, validation_alias="UEP_INFER_HOST_START_TIMEOUT_S")
    # 語音／LLM 載入失敗時是否降級（False：沿用原行為，prepare 例外讓 Host 啟動失敗）
    infer_degrade_on_failure: bool = Field(default=True, validation_alias="UEP_INFER_DEGRADE_ON_FAILURE")


@lru_cache(maxsize=1)
def get_settings() -> UepSettings:
    return UepSettings()
