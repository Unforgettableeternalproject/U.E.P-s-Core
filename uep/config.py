"""uep 設定載入：資料庫連線字串自環境變數或 repo 根 ``.env`` 讀取，不硬編碼。"""
from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from uep.stream.adapters.think_config import ThinkConfig

_REPO_ROOT = Path(__file__).resolve().parents[1]
logger = logging.getLogger(__name__)

_LEGACY_WARNED: set[str] = set()


class UepSettings(BaseSettings):
    """環境變數優先於 ``.env``；未設定的值為 None，由使用端決定如何處理。"""

    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    database_url: SecretStr | None = Field(default=None, validation_alias="UEP_DATABASE_URL")
    # Supervisor（切片 F-3 接線；SYS §2.7、D67）：預設開啟。無 Task 時 tick／pause_all 為空操作，legacy 工具路徑行為不變
    supervisor_enabled: bool = Field(default=True, validation_alias="UEP_SUPERVISOR_ENABLED")
    supervisor_interval_s: float = Field(default=30.0, gt=0, validation_alias="UEP_SUPERVISOR_INTERVAL_S")
    supervisor_shutdown_grace_s: float = Field(default=10.0, ge=0, validation_alias="UEP_SUPERVISOR_SHUTDOWN_GRACE_S")
    supervisor_deadline_grace_s: float = Field(default=30.0, gt=0, validation_alias="UEP_SUPERVISOR_DEADLINE_GRACE_S")
    supervisor_cancel_timeout_s: float = Field(default=5.0, gt=0, validation_alias="UEP_SUPERVISOR_CANCEL_TIMEOUT_S")
    # 回合 Task 孤兒掃描門檻（秒）：必須大於 TURN_MAX_DURATION（600 秒），組裝時驗證
    supervisor_orphan_older_than_s: float = Field(default=1800.0, gt=0, validation_alias="UEP_SUPERVISOR_ORPHAN_OLDER_THAN_S")
    # 工具路徑分流（切片 C）：legacy＝舊衍生路徑（預設，位元組不變）；agent＝註冊表 ``project``＋ActionIntent 的曝光
    tool_path: Literal["legacy", "agent"] = Field(default="legacy", validation_alias="UEP_TOOL_PATH")
    # route=agent_loop 的 production 入口（DL-4 PR4）：預設關閉；只在 UEP_TOOL_PATH=agent 下有效，開啟後
    # route=agent_loop 的回合由 AgentLoop 處理（不再走 direct_chat 回覆）
    agent_loop_entry: bool = Field(default=False, validation_alias="UEP_AGENT_LOOP_ENTRY")
    # 平台脈絡 shadow（平台宣告注入端 R0 第 3、4 步）：預設關閉；開啟時每個簽出的 turn 多一次唯讀 Presence SELECT（鎖外）
    platform_shadow: bool = Field(default=False, validation_alias="UEP_PLATFORM_SHADOW")
    # V18 缺席端輸入 fail-closed：開啟後 UtteranceFact 缺 source_location 即否決（預設關＝缺失放行並計數）。
    # 註冊任何非 desktop 輸入 adapter 前必須開啟（assembly 強制）
    require_source_location: bool = Field(default=False, validation_alias="UEP_REQUIRE_SOURCE_LOCATION")
    # 決策中樞 proposer 選擇（DL-1b）：primary 的 proposer_id（未設＝rules.default）；shadow 清單以逗號分隔
    # （未設＝respond.minimal，設為空字串＝關閉 shadow）。未註冊的 id 於 Host 組裝時 fail-fast
    decision_proposer: str | None = Field(default=None, validation_alias="UEP_DECISION_PROPOSER")
    decision_shadow_proposers: str | None = Field(default=None, validation_alias="UEP_DECISION_SHADOW_PROPOSERS")
    # laya.shadow（決策模型 shadow proposer）的模型設定；只有把 laya.shadow 放進 shadow 清單才會用到
    laya_model: str | None = Field(default=None, validation_alias="UEP_LAYA_MODEL")
    laya_revision: str | None = Field(default=None, validation_alias="UEP_LAYA_REVISION")
    laya_device: str | None = Field(default=None, validation_alias="UEP_LAYA_DEVICE")
    # nlp.legacy（現有英文 NLP shadow proposer）的模型目錄；未設＝repo 相對的 models/nlp。只有放進 shadow 清單才會用到
    nlp_model_dir: str | None = Field(default=None, validation_alias="UEP_NLP_MODEL_DIR")
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
    # bge-m3 embedding 的裝置（艾斯維爾裁決：預設 CPU）；未設時沿用 memory_device
    memory_embedding_device: str | None = Field(default=None, validation_alias="UEP_MEMORY_EMBEDDING_DEVICE")
    # N5（D103／D105 Q5）：Core 記憶路徑統一 ``BAAI/bge-m3``（1024 維），由 sentence-transformers 在本機直載，脫離 Ollama
    # （bot 實測 Ollama embed 2004ms 超過 2.0s 上限而降級）。真 engine 的 embedding 後端與模型只認建構參數（MemoryEngine 的
    # 參數預設，不讀 EMBEDDING_* 環境變數），所以這裡明確給出，不隨 ``.env`` 位置或行程環境漂移。
    # 換模型必須用**全新的資料目錄**（``UEP_MEMORY_DATA_ROOT``，例如在舊目錄名後加 ``-bgem3``）：舊目錄的
    # ``embedding_index.json`` 是 bge-small-zh（512 維），Core 的 EchoEngineFactory 偵測到 model／維度不符會拒絕建 engine，
    # 不會悄悄重建或混用舊索引。舊目錄不動不刪。API 金鑰不在此處，照舊由行程環境變數提供。
    memory_embedding_backend: str | None = Field(default="sentence-transformers", validation_alias="UEP_MEMORY_EMBEDDING_BACKEND")
    memory_embedding_model: str | None = Field(default="BAAI/bge-m3", validation_alias="UEP_MEMORY_EMBEDDING_MODEL")
    # 向量檢索的 cosine 下限；未設時沿用 EM 的 ``RETRIEVAL_MIN_SIMILARITY``（預設 0.35）。bge-m3 對短中文的 cosine 分布偏高
    # （N5 實測：相關 0.49–0.69、無關最近鄰 0.30–0.56，重疊大），0.35 幾乎不過濾；要收緊以 A5 實資料校準後在此設定。
    memory_min_similarity: float | None = Field(default=None, validation_alias="UEP_MEMORY_MIN_SIMILARITY")

    # 系統日誌（uep/log.py，只在進入點 configure_logging 讀取）：UEP_LOG_LEVEL 優先於 UEP_LOG_DEPTH（1..4＝KEY／INFO／DEBUG／TRACE）；
    # log_levels 為 "uep.decision=DEBUG,echo_tts=WARNING" 的命名空間覆寫；console 輸出到 stderr；檔案每行程一組，JSONL 供查核
    log_level: str | None = Field(default=None, validation_alias="UEP_LOG_LEVEL")
    log_depth: str | None = Field(default=None, validation_alias="UEP_LOG_DEPTH")
    log_levels: str | None = Field(default=None, validation_alias="UEP_LOG_LEVELS")
    log_console_level: str = Field(default="WARNING", validation_alias="UEP_LOG_CONSOLE_LEVEL")
    log_file: bool = Field(default=True, validation_alias="UEP_LOG_FILE")
    log_dir: Path | None = Field(default=None, validation_alias="UEP_LOG_DIR")
    log_json: bool = Field(default=False, validation_alias="UEP_LOG_JSON")

    # 推理 Host 組裝（P3 S6，uep/echo/inference_wiring.py）：子系統 repo 路徑一律由設定明確給（不猜同層目錄；
    # ES 的同層猜測在非 editable 安裝下指向 site-packages 之上而失效）。未設定時對應的 stage 拒絕組裝。
    infer_tts_repo: Path | None = Field(default=None, validation_alias="UEP_INFER_TTS_REPO")
    # M3：echo_stt 已遷入 Core，此設定不再需要（相容讀取，設了會被忽略並警告一次）
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

    # Think／LLM（N3）：UEP_LLM_* 為正式名稱。舊名 ECHO_STREAM_LLM_*／ECHO_STREAM_OUTPUT_*／OPENAI_* 保留相容讀取，
    # 使用時每個舊名各警告一次。金鑰只從環境變數或 Core repo 根 ``.env`` 取，SecretStr 不進 repr／日誌。
    llm_provider: str | None = Field(default=None, validation_alias="UEP_LLM_PROVIDER")
    llm_model: str | None = Field(default=None, validation_alias="UEP_LLM_MODEL")
    llm_api: str | None = Field(default=None, validation_alias="UEP_LLM_API")
    llm_reasoning_effort: str | None = Field(default=None, validation_alias="UEP_LLM_REASONING_EFFORT")
    llm_max_tokens: int | None = Field(default=None, validation_alias="UEP_LLM_MAX_TOKENS")
    llm_temperature: float | None = Field(default=None, validation_alias="UEP_LLM_TEMPERATURE")
    llm_api_key: SecretStr | None = Field(default=None, validation_alias="UEP_LLM_API_KEY")
    llm_project: str | None = Field(default=None, validation_alias="UEP_LLM_PROJECT")
    llm_organization: str | None = Field(default=None, validation_alias="UEP_LLM_ORGANIZATION")
    llm_base_url: str | None = Field(default=None, validation_alias="UEP_LLM_BASE_URL")
    llm_prompt_cache_key: str | None = Field(default=None, validation_alias="UEP_LLM_PROMPT_CACHE_KEY")
    llm_output_guard: bool | None = Field(default=None, validation_alias="UEP_LLM_OUTPUT_GUARD")
    llm_output_blocklist_extra: str | None = Field(default=None, validation_alias="UEP_LLM_OUTPUT_BLOCKLIST_EXTRA")
    # 舊名（相容讀取）
    legacy_llm_provider: str | None = Field(default=None, validation_alias="ECHO_STREAM_LLM_PROVIDER")
    legacy_llm_model: str | None = Field(default=None, validation_alias="ECHO_STREAM_LLM_MODEL")
    legacy_llm_api: str | None = Field(default=None, validation_alias="ECHO_STREAM_LLM_API")
    legacy_llm_reasoning_effort: str | None = Field(default=None, validation_alias="ECHO_STREAM_LLM_REASONING_EFFORT")
    legacy_llm_max_tokens: int | None = Field(default=None, validation_alias="ECHO_STREAM_LLM_MAX_TOKENS")
    legacy_llm_temperature: float | None = Field(default=None, validation_alias="ECHO_STREAM_LLM_TEMPERATURE")
    legacy_llm_prompt_cache_key: str | None = Field(default=None, validation_alias="ECHO_STREAM_LLM_PROMPT_CACHE_KEY")
    legacy_output_guard: bool | None = Field(default=None, validation_alias="ECHO_STREAM_OUTPUT_GUARD")
    legacy_output_blocklist_extra: str | None = Field(default=None, validation_alias="ECHO_STREAM_OUTPUT_BLOCKLIST_EXTRA")
    legacy_openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    legacy_openai_project: str | None = Field(default=None, validation_alias="OPENAI_PROJECT")
    legacy_openai_org: str | None = Field(default=None, validation_alias="OPENAI_ORG")
    legacy_openai_base_url: str | None = Field(default=None, validation_alias="OPENAI_BASE_URL")

    def think_config(self) -> "ThinkConfig":
        """解析成 Think adapter 的 ThinkConfig：``UEP_LLM_*`` → ``UEP_INFER_LLM_*`` → 舊名（警告一次）→ 預設。"""
        from uep.stream.adapters.think_config import ThinkConfig

        def pick(new: str, legacy: str, legacy_name: str, *, also: str | None = None):
            value = getattr(self, new)
            if value is None and also is not None:
                value = getattr(self, also)
            if value is None:
                value = getattr(self, legacy)
                if value is not None:
                    _warn_legacy(legacy_name, new_name_of(new))
            return value.get_secret_value() if isinstance(value, SecretStr) else value

        defaults = ThinkConfig()
        extra_raw = pick("llm_output_blocklist_extra", "legacy_output_blocklist_extra", "ECHO_STREAM_OUTPUT_BLOCKLIST_EXTRA")
        guard = pick("llm_output_guard", "legacy_output_guard", "ECHO_STREAM_OUTPUT_GUARD")
        max_tokens = pick("llm_max_tokens", "legacy_llm_max_tokens", "ECHO_STREAM_LLM_MAX_TOKENS")
        cache_key = pick("llm_prompt_cache_key", "legacy_llm_prompt_cache_key", "ECHO_STREAM_LLM_PROMPT_CACHE_KEY")
        return ThinkConfig(
            provider=pick("llm_provider", "legacy_llm_provider", "ECHO_STREAM_LLM_PROVIDER", also="infer_llm_provider")
            or defaults.provider,
            model=pick("llm_model", "legacy_llm_model", "ECHO_STREAM_LLM_MODEL", also="infer_llm_model") or defaults.model,
            api=pick("llm_api", "legacy_llm_api", "ECHO_STREAM_LLM_API") or defaults.api,
            reasoning_effort=pick("llm_reasoning_effort", "legacy_llm_reasoning_effort", "ECHO_STREAM_LLM_REASONING_EFFORT")
            or defaults.reasoning_effort,
            max_output_tokens=max_tokens if max_tokens else defaults.max_output_tokens,
            temperature=pick("llm_temperature", "legacy_llm_temperature", "ECHO_STREAM_LLM_TEMPERATURE"),
            api_key=pick("llm_api_key", "legacy_openai_api_key", "OPENAI_API_KEY") or None,
            project=pick("llm_project", "legacy_openai_project", "OPENAI_PROJECT"),
            organization=pick("llm_organization", "legacy_openai_org", "OPENAI_ORG"),
            base_url=pick("llm_base_url", "legacy_openai_base_url", "OPENAI_BASE_URL"),
            prompt_cache_key=(cache_key or "").strip() or None,
            output_guard_enabled=True if guard is None else bool(guard),
            output_blocklist_extra=tuple(p.strip() for p in re.split(r"[,，、]", extra_raw or "") if p.strip()),
        )


def new_name_of(field_name: str) -> str:
    """欄位名轉成正式環境變數名（警告訊息用）。"""
    return "UEP_" + field_name.upper()


def _warn_legacy(old: str, new: str) -> None:
    """舊環境變數名只警告一次（行程內）。"""
    if old not in _LEGACY_WARNED:
        _LEGACY_WARNED.add(old)
        logger.warning("環境變數 %s 為舊名，請改用 %s（仍相容讀取）。", old, new)


@lru_cache(maxsize=1)
def get_settings() -> UepSettings:
    return UepSettings()
