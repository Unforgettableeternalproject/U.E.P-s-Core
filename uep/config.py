"""uep 設定載入：資料庫連線字串自環境變數或 repo 根 ``.env`` 讀取，不硬編碼。"""
from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from uep.stream.adapters.think_compress import CompressConfig
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
    # module.* 就緒事件訂閱（切片 F，註冊表端；事件名暫定，module coordinator 待設計）：預設關閉，
    # 只在 UEP_TOOL_PATH=agent 下有效。目前沒有任何 producer，關閉時不訂閱＝與舊行為完全一致
    tool_module_events: bool = Field(default=False, validation_alias="UEP_TOOL_MODULE_EVENTS")
    # 測試用工具組（test_echo／test_clock／test_sleep／test_approval_echo）：預設關閉；只在 UEP_TOOL_PATH=agent 下有效。
    # 開啟後組裝層才註冊這些工具與 executor，並把測試標籤併入 /loop（route=agent_loop）的 capability_tags；關閉時完全不存在
    tool_testkit: bool = Field(default=False, validation_alias="UEP_TOOL_TESTKIT")
    # Chatroom 原生工具（chatroom.read／wait／post）：全部設定齊了才可用，缺任一項工具恆為 unavailable（組裝不失敗）。
    # token 來源二選一：UEP_CHATROOM_TOKEN，或 UEP_CHATROOM_TOKEN_FILE（純 token 檔，或含 CHATROOM_TOKEN=... 行的 .env）；只在 executor 側使用
    chatroom_url: str | None = Field(default=None, validation_alias="UEP_CHATROOM_URL")
    chatroom_session_key: str | None = Field(default=None, validation_alias="UEP_CHATROOM_SESSION_KEY")
    chatroom_display_name: str | None = Field(default=None, validation_alias="UEP_CHATROOM_DISPLAY_NAME")
    chatroom_token: SecretStr | None = Field(default=None, validation_alias="UEP_CHATROOM_TOKEN")
    chatroom_token_file: Path | None = Field(default=None, validation_alias="UEP_CHATROOM_TOKEN_FILE")
    # 允許的房間 id（逗號分隔白名單）；executor 只對白名單內的房間自動加入、讀、等、發言
    chatroom_rooms: str | None = Field(default=None, validation_alias="UEP_CHATROOM_ROOMS")
    # 平台脈絡 shadow（平台宣告注入端 R0 第 3、4 步）：預設關閉；開啟時每個簽出的 turn 多一次唯讀 Presence SELECT（鎖外）
    platform_shadow: bool = Field(default=False, validation_alias="UEP_PLATFORM_SHADOW")
    # Output Gate 前置「端點須具 interaction 能力」（平台端點契約 IC-3、E3）：預設關＝shadow（只計數與記 log，照常放行）；
    # 開啟才回 REJECT_NOT_INTERACTION。開啟的時機另行裁決
    gate_require_interaction: bool = Field(default=False, validation_alias="UEP_GATE_REQUIRE_INTERACTION")
    # V18 缺席端輸入 fail-closed：開啟後 UtteranceFact 缺 source_location 即否決（預設關＝缺失放行並計數）。
    # 註冊任何非 desktop 輸入 adapter 前必須開啟（assembly 強制）
    require_source_location: bool = Field(default=False, validation_alias="UEP_REQUIRE_SOURCE_LOCATION")
    # 決策中樞 proposer 選擇（DL-1b）：primary 的 proposer_id（未設＝rules.default）；shadow 清單以逗號分隔
    # （未設＝respond.minimal，設為空字串＝關閉 shadow）。未註冊的 id 於 Host 組裝時 fail-fast
    decision_proposer: str | None = Field(default=None, validation_alias="UEP_DECISION_PROPOSER")
    decision_shadow_proposers: str | None = Field(default=None, validation_alias="UEP_DECISION_SHADOW_PROPOSERS")
    # laya.shadow（決策模型 shadow proposer）的模型設定；只有把 laya.shadow 放進 shadow 清單才會用到
    # UEP_LAYA_MODEL 未設時用 <UEP_LAYA_MODELS_DIR>/laya-multilingual（預設 ~/.uep/models/laya）；明確設值照原樣傳給 laya.load
    laya_models_dir: str | None = Field(default=None, validation_alias="UEP_LAYA_MODELS_DIR")
    laya_model: str | None = Field(default=None, validation_alias="UEP_LAYA_MODEL")
    laya_revision: str | None = Field(default=None, validation_alias="UEP_LAYA_REVISION")
    laya_device: str | None = Field(default=None, validation_alias="UEP_LAYA_DEVICE")
    # Boundary Translator（決策模型輸入 zh→en，決策中樞提案 §5.4）：預設關閉＝laya.shadow 收原文；開啟＝收英文
    # 模型目錄未設＝~/.uep/models/mt/opus-mt-zh-en；aliases 為逗號分隔（未設＝內建「小U、小優、小悠、U.E.P、UEP」）
    # unavailable 時：fallback＝laya.shadow 回 reason_code=translate_unavailable（預設，訓練只用英文）；original＝改送原文
    decision_translate: bool = Field(default=False, validation_alias="UEP_DECISION_TRANSLATE")
    translate_model_dir: str | None = Field(default=None, validation_alias="UEP_TRANSLATE_MODEL_DIR")
    translate_device: str | None = Field(default=None, validation_alias="UEP_TRANSLATE_DEVICE")
    translate_timeout_ms: int | None = Field(default=None, validation_alias="UEP_TRANSLATE_TIMEOUT_MS")
    translate_aliases: str | None = Field(default=None, validation_alias="UEP_TRANSLATE_ALIASES")
    translate_placeholder: str | None = Field(default=None, validation_alias="UEP_TRANSLATE_PLACEHOLDER")
    translate_cpu_fallback: bool = Field(default=True, validation_alias="UEP_TRANSLATE_CPU_FALLBACK")
    translate_on_unavailable: str | None = Field(default=None, validation_alias="UEP_TRANSLATE_ON_UNAVAILABLE")
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

    # Host 全域做夢迴圈（D92～D100，docs/架構重構/Host全域做夢迴圈設計.md）：只有在真實記憶組裝（MemoryHost）時有意義。
    # 預設啟用但 shadow（D100）：迴圈照常評估並在 log／指標記下 would_run，不呼叫 dream()；觀察後再以 UEP_DREAM_SHADOW=0 轉真執行。
    # 窗口預設 03:00–05:30 Asia/Taipei（FixedWindowPolicy）；白天驗證時以 start／end 覆寫（格式 HH:MM，end<=start 視為跨午夜）。
    # 只給 start 或只給 end 時，另一端沿用預設值。
    dream_enabled: bool = Field(default=True, validation_alias="UEP_DREAM_ENABLED")
    dream_shadow: bool = Field(default=True, validation_alias="UEP_DREAM_SHADOW")
    dream_window_start: str | None = Field(default=None, validation_alias="UEP_DREAM_WINDOW_START")
    dream_window_end: str | None = Field(default=None, validation_alias="UEP_DREAM_WINDOW_END")
    dream_window_tz: str | None = Field(default=None, validation_alias="UEP_DREAM_WINDOW_TZ")

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
    # TTS 引擎：vox（預設）或 indextts2（V25，可選）；兩者不能同時常駐，由此單選
    infer_tts_engine: str = Field(default="vox", validation_alias="UEP_INFER_TTS_ENGINE")
    # VOX backend 設定：JSON 物件字串（原樣轉給 VoxBackend(config)）；空值＝不傳
    infer_vox_config: str | None = Field(default=None, validation_alias="UEP_INFER_VOX_CONFIG")
    # VOX 語速與停頓（bot 預設皆為 None＝交給 backend／adapter 預設；未設就不傳）。speed 未設時退回 UEP_INFER_TTS_SPEED
    infer_vox_speed: float | None = Field(default=None, validation_alias="UEP_INFER_VOX_SPEED")
    infer_vox_segment_silence_ms: float | None = Field(default=None, ge=0, validation_alias="UEP_INFER_VOX_SEGMENT_SILENCE_MS")
    infer_vox_terminal_pause_ms: float | None = Field(default=None, ge=0, validation_alias="UEP_INFER_VOX_TERMINAL_PAUSE_MS")
    infer_vox_secondary_pause_ms: float | None = Field(default=None, ge=0, validation_alias="UEP_INFER_VOX_SECONDARY_PAUSE_MS")
    # VOX 情緒：開關預設關（Bernie 11/11 裁決；real-mode 驗證後再開）；對照表為 JSON 物件字串（SpeechStyle 名 → 標籤或 {"emotion","intensity"}），疊在已定案的預設表上
    infer_vox_emotion_enabled: bool = Field(default=False, validation_alias="UEP_INFER_VOX_EMOTION_ENABLED")
    infer_vox_emotion_map: str | None = Field(default=None, validation_alias="UEP_INFER_VOX_EMOTION_MAP")
    infer_tts_device: str = Field(default="cuda", validation_alias="UEP_INFER_TTS_DEVICE")
    infer_tts_fp16: bool = Field(default=True, validation_alias="UEP_INFER_TTS_FP16")
    infer_tts_use_accel: bool = Field(default=False, validation_alias="UEP_INFER_TTS_USE_ACCEL")
    infer_tts_warmup: bool = Field(default=True, validation_alias="UEP_INFER_TTS_WARMUP")
    infer_tts_lora_scale: float | None = Field(default=None, validation_alias="UEP_INFER_TTS_LORA_SCALE")
    infer_tts_speed: float | None = Field(default=None, validation_alias="UEP_INFER_TTS_SPEED")
    infer_tts_language: str | None = Field(default=None, validation_alias="UEP_INFER_TTS_LANGUAGE")
    infer_stt_mode: str = Field(default="multichannel", validation_alias="UEP_INFER_STT_MODE")
    infer_stt_language: str | None = Field(default=None, validation_alias="UEP_INFER_STT_LANGUAGE")
    # STT 單 job 牆鐘逾時（multichannel）：實際上限 = UEP_STT_JOB_TIMEOUT_S + UEP_STT_JOB_TIMEOUT_PER_AUDIO_S × 音訊秒數；
    # 預設 4.0／0.5（bot 實測值）。UEP_STT_JOB_TIMEOUT_S 設 0 代表停用（不丟棄卡住的 job）
    stt_job_timeout_s: float = Field(default=4.0, ge=0, validation_alias="UEP_STT_JOB_TIMEOUT_S")
    stt_job_timeout_per_audio_s: float = Field(default=0.5, ge=0, validation_alias="UEP_STT_JOB_TIMEOUT_PER_AUDIO_S")
    # STT 斷句與 VAD（對齊 bot 現行 config/stt.yaml 的 es_input）：靜音多久判定一句話結束、短於多久不成一句，
    # 以及 EnergyVad 的 RMS 門檻（int16 滿刻度 = 1.0）、遲滯比與 hangover。兩個 STT 輸入 stage 都吃這組設定。
    # 注意：TurnPolicy.max_wait_s 在 Core 與 Echo-Stream 都沒有讀取端（SilenceTurnDetector 不看它），因此刻意不提供設定
    stt_silence_threshold_s: float = Field(default=0.8, gt=0, validation_alias="UEP_STT_SILENCE_THRESHOLD_S")
    stt_min_utterance_s: float = Field(default=0.3, ge=0, validation_alias="UEP_STT_MIN_UTTERANCE_S")
    stt_vad_threshold: float = Field(default=0.015, gt=0, validation_alias="UEP_STT_VAD_THRESHOLD")
    stt_vad_hysteresis: float = Field(default=0.6, gt=0, le=1, validation_alias="UEP_STT_VAD_HYSTERESIS")
    stt_vad_hangover_s: float = Field(default=0.15, ge=0, validation_alias="UEP_STT_VAD_HANGOVER_S")
    # 語音路徑的 turn 聚合（BufferedAggregator，搬自 bot turn_buffer）：預設啟用，只在組裝了真實 STT 輸入時才有作用；
    # 文字輸入（/say、submit_utterance 的文字）永遠逐句通過，不受影響。關閉則語音一句一個 turn
    voice_aggregator: bool = Field(default=True, validation_alias="UEP_VOICE_AGGREGATOR")
    infer_llm_provider: str | None = Field(default=None, validation_alias="UEP_INFER_LLM_PROVIDER")
    infer_llm_model: str | None = Field(default=None, validation_alias="UEP_INFER_LLM_MODEL")
    # 人設 system prompt：留空（含未設）時用 repo 內受版控的預設（uep/persona/persona.yaml）；env 有值以 env 為準
    infer_system_prompt: str = Field(default="", validate_default=True, validation_alias="UEP_INFER_SYSTEM_PROMPT")
    # 各 stage prepare 的逾時（秒）；Host 啟動總上限目前為 60 秒（EchoPipelineHost.start），三者相加須小於它（未設定 UEP_INFER_HOST_START_TIMEOUT_S 時由組裝自動推得）
    # speak 未設定（None）時依引擎取預設：indextts2 為 40 秒、vox 為 240 秒（見 inference_wiring）
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
    # Host 啟動時預熱 prompt cache（_startup：speak -> think -> prewarm -> 輸入）；預熱用的 effort 即 UEP_LLM_REASONING_EFFORT
    # 語音路徑記憶檢索逾時（秒）：bot 驗證值 1.5；0＝沿用 Access Layer 預設（3 秒）。逾時本輪不帶記憶
    memory_voice_retrieve_timeout_s: float = Field(default=1.5, ge=0, validation_alias="UEP_MEMORY_VOICE_RETRIEVE_TIMEOUT_S")
    # Windows：啟動時關閉本行程 EcoQoS（避免被排到 E 核拖慢 TTS／STT）；非 Windows 無作用
    disable_ecoqos: bool = Field(default=True, validation_alias="UEP_DISABLE_ECOQOS")
    llm_prewarm: bool = Field(default=True, validation_alias="UEP_LLM_PREWARM")
    # 工具呼叫前的墊話模式：model_then_fixed（bot 驗證過的預設）／model／fixed／off
    think_filler_mode: str = Field(default="model_then_fixed", validation_alias="UEP_THINK_FILLER_MODE")
    llm_output_guard: bool | None = Field(default=None, validation_alias="UEP_LLM_OUTPUT_GUARD")
    llm_output_blocklist_extra: str | None = Field(default=None, validation_alias="UEP_LLM_OUTPUT_BLOCKLIST_EXTRA")
    # 長對話壓縮（ContextManager 四層壓縮）：壓縮走 openai 後端，None＝採 CompressConfig 預設
    llm_compress: bool | None = Field(default=None, validation_alias="UEP_LLM_COMPRESS")
    llm_compress_model: str | None = Field(default=None, validation_alias="UEP_LLM_COMPRESS_MODEL")
    llm_context_window: int | None = Field(default=None, validation_alias="UEP_LLM_CONTEXT_WINDOW")
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

    @field_validator("infer_system_prompt")
    @classmethod
    def _default_persona_when_blank(cls, value: str) -> str:
        """空白（未設或設成空字串）時以受版控的人設檔為預設。"""
        if value.strip():
            return value
        from uep.persona import default_system_prompt

        return default_system_prompt()

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

    def compress_config(self) -> CompressConfig:
        """解析成長對話壓縮的 CompressConfig：``UEP_LLM_COMPRESS*``／``UEP_LLM_CONTEXT_WINDOW`` → 預設。"""
        from uep.stream.adapters.think_compress import CompressConfig

        defaults = CompressConfig()
        return CompressConfig(
            enabled=defaults.enabled if self.llm_compress is None else self.llm_compress,
            model=(self.llm_compress_model or "").strip() or defaults.model,
            context_window=self.llm_context_window if self.llm_context_window and self.llm_context_window > 0 else None,
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
