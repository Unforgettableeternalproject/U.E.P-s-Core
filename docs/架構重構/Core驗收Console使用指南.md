# Core 驗收 Console 使用指南

> 對象：艾斯維爾（人工驗收）與開發代理。適用分支 `feature/subsystem-migration`（2026-11-02 起）。
> 背景與驗收矩陣見 [子系統遷入Core計畫](子系統遷入Core計畫.md) §5.5；決策見 D104、D105。

`python -m uep.console` 是 Core 的端到端驗收入口，取代 Legacy `devtools/debug_api.py` 的「隔離模組測試」用途。它分三層：

| 層 | 用途 | 入口 |
|---|---|---|
| (a) 子系統隔離 | 單獨測 STT／Think／TTS／記憶，不經 Core | `/stt` `/llm` `/tts` `/mem embed` |
| (b) Core 隔離 | 子系統全換假元件，只測 Core 的 turn／presence／output | `--mode fake`（預設） |
| (c) 端到端 | Core＋所有真實子系統，從輸入跑到輸出 | `--mode real` |

---

## 1. 準備

### 1.1 環境與前置

- Python：共用環境 `%USERPROFILE%\.uep\venv-inference-312-t211`（torch 2.11）。舊的 `venv-inference-312` 不能用（缺 `echo_tts.config.model_paths`）。
- PostgreSQL：開發機資料庫要在跑，且已 `alembic upgrade head`。連線取 `UEP_DATABASE_URL`（Core `.env`）。
- LLM：Core `.env` 需有 `UEP_LLM_API_KEY` 等（目前模型 `gpt-6-luna`，人設取 `UEP_INFER_SYSTEM_PROMPT`）。
- GPU：real 模式的 STT／TTS 用 GPU，**bot 要先停**，並設 `UEP_E2E_ALLOW_GPU=1`。記憶 embedding（bge-m3）預設跑 CPU。
- 麥克風與播放（只有 A4 需要）：環境內裝 sounddevice，並**戴耳機**（外放會被麥克風收回而自我觸發）。

```powershell
# 在 Core repo 根目錄
$py = "$env:USERPROFILE\.uep\venv-inference-312-t211\Scripts\python.exe"
uv pip install --python $py sounddevice   # 只有 A4 需要，裝一次即可
```

### 1.2 記憶資料目錄

real 模式記憶寫入 `UEP_MEMORY_DATA_ROOT`；未設時用 `~/.uep/data/console-memory-bgem3`。該目錄已有開發時寫入的「小橘」測試資料，**要乾淨驗收請指到新目錄**：

```powershell
$env:UEP_MEMORY_DATA_ROOT = "$env:USERPROFILE\.uep\data\console-acceptance-1102"
```

### 1.3 啟動

```powershell
$env:UEP_E2E_ALLOW_GPU = "1"
& $py -m uep.console --mode real            # 端到端（文字輸入、音訊存檔）
& $py -m uep.console --mode real --play     # 同上並在本機播放（A4，戴耳機）
& $py -m uep.console                        # fake 模式（不需 GPU 與金鑰，只驗 Core 流程）
```

real 模式啟動會先暖機，**看到各元件 warmup 耗時印完、出現 `uep>` 提示才能輸入**。參考耗時：TTS（VOX）74–101 秒、STT 約 6 秒、記憶 embedding 約 17 秒（CPU）。

### 1.4 旗標

| 旗標 | 說明 |
|---|---|
| `--mode fake\|real` | fake（預設）或 real（全部真實，需 `UEP_E2E_ALLOW_GPU=1`） |
| `--think` `--tts` `--stt` `--memory` `fake\|real` | 個別覆寫，例如 `--mode real --tts fake`＝不用 TTS GPU |
| `--play` | 本機播放 TTS（需 sounddevice＋耳機） |
| `--no-tts` | 不落地音訊 |
| `--no-stt` | 等同 `--stt fake` |
| `--session-dir <目錄>` | 音訊輸出與 host epoch 檔位置，預設 `outputs\e2e` |

TTS 引擎預設 VOX，改用 IndexTTS 2.5 設 `UEP_INFER_TTS_ENGINE=indextts2`（另需 `UEP_TTS_INDEXTTS_DIR` 指向權重）。

### 1.5 系統日誌

進入點（console／endpoint／stop）啟動時自動設定日誌：console 只印 WARNING 以上到 **stderr**（REPL 的 stdout 不受影響），完整日誌寫到 repo 的 `logs/`（每個行程一組檔案：`uep-<啟動時間>-pid<pid>.log`、`.error.log`，開 JSON 時多 `.jsonl`）。每行自動標註來源，例如 `[core.tasks] uep/tasks/manager.py:123 execute_action | ...`，呼叫端不必改；子系統日誌標成 `echo_tts` 等名稱。密鑰形式（`api_key=`、`token=`、`secret=`、`Bearer xxx`）會被遮成 `***`。

| 環境變數 | 說明 |
|---|---|
| `UEP_LOG_DEPTH` | 1..4 ＝ KEY／INFO／DEBUG／TRACE 門檻（預設 2，INFO）；WARNING 以上永遠輸出 |
| `UEP_LOG_LEVEL` | 直接指定 root 等級（如 `DEBUG`），優先於 DEPTH |
| `UEP_LOG_LEVELS` | 命名空間覆寫，例：`uep.decision=DEBUG,echo_tts=WARNING`；格式錯的項目會警告一次並忽略 |
| `UEP_LOG_CONSOLE_LEVEL` | console 門檻（預設 `WARNING`） |
| `UEP_LOG_FILE` / `UEP_LOG_DIR` | 檔案輸出開關（預設開）與目錄（預設 repo `logs/`） |
| `UEP_LOG_JSON` | 設 `1` 另寫 JSONL（`ts/level/subsystem/logger/where/msg/context`），供查核 |

REPL：`/log` 看目前等級；`/log level <名稱空間> <等級>` 即時調整（例：`/log level uep.decision DEBUG`）；`/log console <等級>` 調整 console 門檻。httpx、urllib3、torch、transformers、sqlalchemy.engine 等第三方預設壓在 WARNING，可用 `UEP_LOG_LEVELS` 覆寫。程式內：`from uep.log import get_logger, log_context`，`with log_context(turn_id=...):` 內的日誌會自動帶上該欄位。

---

## 2. 指令

不加斜線直接打字＝`/say`。

### 2.1 對話與流程

| 指令 | 說明 |
|---|---|
| `/say <文字>` | 送一句話，等這一輪結束，顯示回應、首音延遲（TTFA）與 wav 路徑 |
| `/wav <路徑>` | 用音檔當輸入。real 模式走真實 STT；長音檔若因停頓被切成多句，後句會依設計取代前句，console 追到最後一輪才結束（之後多等 2 秒） |
| `/mic` | 麥克風：按 Enter 開始、說話、再按 Enter 結束，送進 STT 跑整輪（目前無自動斷句） |
| `/cancel` | 取消進行中的一輪（用來驗插話打斷） |
| `/session new` | 開新 session：清掉對話歷史；驗跨 session 記憶時要用 |
| `/status` | Presence／Interaction 狀態、turn 狀態、host 健康 |
| `/trace [all]` | 最近一輪的事件與各段延遲（`all`＝所有輪摘要） |
| `/help`、`/quit` | 說明、結束 |

### 2.2 記憶

| 指令 | 說明 |
|---|---|
| `/mem` | 目前 namespace、episode 數、最近寫入 |
| `/mem recall <文字>` | 經 Core 記憶存取層檢索 |
| `/mem embed <文字> [--device cpu\|cuda] [--n 次數]` | 量 embedding 載入、首查、p50／p95 |

### 2.3 子系統隔離

| 指令 | 說明 |
|---|---|
| `/stt <wav> [--model 名] [--lang 碼]` | 只跑語音辨識，印文字與耗時（未設 GPU 旗標時強制 CPU＋base 模型） |
| `/llm <文字>` | 只跑 Think（真實 LLM），印切句與耗時 |
| `/tts <文字> [--engine vox\|indextts2]` | 只跑合成，寫 wav，印首音延遲、總耗時、VRAM 峰值（需 GPU） |

---

## 3. 驗收步驟

前置：bot 已停、`UEP_E2E_ALLOW_GPU=1`、記憶目錄指向新目錄，`--mode real` 啟動並等暖機完成。每項記錄「通過／不通過＋觀察」。

| 編號 | 驗什麼 | 怎麼做 | 通過條件 |
|---|---|---|---|
| A1 | 文字→真實 LLM | `/say 你好，請簡單介紹你自己` | 有回應，且以人設回答（自稱 U，非 ChatGPT） |
| A2 | 文字→LLM→TTS | 同上，看輸出的 TTFA 與 wav；用 `--play` 可直接聽 | 有聲音、內容與文字一致；TTFA 記錄下來（參考 1.9–3.2 秒） |
| A3 | 音檔→STT→LLM→TTS | `/wav subsystems/echo_stt/tests/data/zh_test.wav` | 顯示辨識文字，最後一輪有回應與 wav |
| A4 | 麥克風＋插話 | `--play` 啟動，戴耳機；`/mic` 說一句；她回答播放中再用 `/cancel` | 辨識正確、有語音回應；`/cancel` 後立即停止 |
| A5 | 跨 session 記憶 | `/say 我養了一隻叫小橘的貓` → `/session new` → `/say 你記得我的寵物叫什麼嗎`；再 `/mem`、`/mem recall 寵物` | 新 session 答出「小橘」；`/mem recall` 命中 |
| A7 | 降級 | 以 `--mode real --tts fake --no-tts` 啟動，`/say` 一句 | 不用真實 TTS 時整輪仍完成、文字回應正常、不報錯 |
| A8 | 延遲 | 做完上面幾輪後 `/trace all` | 記錄各輪 TTFA，與 ES 舊基線約 5.1 秒對照（不設硬門檻） |

順帶可驗（會影響後續工作，有空再做）：

- 讀音：`/say` 讓她回覆含「謝謝、試著、模糊、混淆、捨不得」的句子，或 `/tts` 直接合成這些詞。
- 第一輪延遲：重啟後的第一句與之後幾句的 TTFA 差距（暖機是否完整）。

### 3.1 agent 回合測試（UEP_TOOL_PATH=agent）

驗「真模型驅動 agent 管線能工作」：模型經 `PydanticAIModelPort`（openai 後端，模型與 dialect 取 `UEP_LLM_*`，預設 `gpt-6-luna`／chat）叫工具，Core 執行並回傳，最後只回覆一次。不需要 GPU（STT／TTS 維持 fake）。

**環境變數**（PowerShell，在 Core repo 根目錄）：

```powershell
$env:UEP_TOOL_PATH = "agent"            # 必須；改走 AgentThinkStage + ModelPort
$env:UEP_AGENT_LOOP_ENTRY = "1"         # 要測 /loop（route=agent_loop）才需要；只測一般對話可不設
$env:UEP_LLM_API_KEY = "<金鑰>"         # 或寫在 Core .env
# 可選：UEP_LLM_MODEL（預設 gpt-6-luna）、UEP_LLM_API（chat｜responses，預設 chat）
& $py -m uep.console --think real                 # 記憶為行程內 fake（結束即消失）
& $py -m uep.console --think real --memory real   # 記憶用真實 EM（CPU 即可；資料根見 1.2，建議指新目錄）
```

`UEP_TOOL_PATH=agent` 搭配 fake Think（沒有 `--think real`）會直接拒絕啟動；缺金鑰也會在啟動時報錯，不會等到第一句話。

**可用的工具**（以現行註冊表 `memory_registry()` 為準，沒有時間或網路類工具）：

| 工具 | 能力 | 在 Console 可見性 |
|---|---|---|
| `deep_recall` | memory.read（唯讀） | 一般對話與 `/loop` 都可見 |
| `force_remember` | memory.write（可還原，無需核准） | 只在一般對話可見（`agent_loop` 路徑只開放唯讀能力） |
| `member_recall`、`retract_about_me`、`opt_out_about_me` | 場域（guild）成員記憶 | Console 沒有 guild，不曝光 |

**步驟與預期**：

| 步驟 | 輸入 | 預期 |
|---|---|---|
| G1 一般對話 | `你好，介紹一下你自己` | `turn ...：turn.completed`，印一則「回應：…」；沒有 Run／工具資訊 |
| G2 寫入（`--memory real` 較有意義） | `請記住我養了一隻叫小橘的貓` | 模型呼叫 `force_remember`，回應確認；`/mem` 可見新 episode |
| G3 agent_loop 查詢 | `/loop 幫我查一下我的寵物叫什麼名字` | 印 `回應：…` 一則（**只回覆一次**）、`Run run_…：SUCCEEDED（Task SUCCEEDED）`、`工具呼叫：deep_recall[SUCCEEDED]`（可多次） |
| G4 一次性 | 接著送一般句子 | 回到 G1 的形狀（`/loop` 只影響下一句） |

`/loop <文字>` 把下一個 respond 的提議改成 `route=agent_loop`（Console 專用，規則 proposer 本身不會產出這個 route）。沒設 `UEP_AGENT_LOOP_ENTRY=1` 時會提示並退回一般回覆。失敗時使用者只會看到固定系統訊息「這次處理失敗了，沒有產生回覆。」，不是模型編的答案；`/status` 的 Host 指標與 `/trace` 可輔助看哪一輪失敗。

**核准（Risky 工具）**：現行註冊表沒有任何工具需要核准（全部 `approval_level=none`），所以 G1–G4 不會遇到核准。Console **尚未提供核准指令**：若之後註冊了需核准的工具，agent_loop 會把 Run 停在 `WAITING_APPROVAL`（`awaiting_approval`，回合不會有回覆），Console 目前無法放行或否決，要等 Approval 通道接上 Console 才能測。

### 3.2 週期二補漏重點（2026-11-11 合入，Bernie 親自驗證時重點關注）

這一批是「週期一設計並經 bot 驗證、但遷入時沒帶進 Core」的內容。fake 與單元測試都已通過，**真機都還沒驗過**，請以 `--mode real` 逐項確認。

**測試前**：
- 本機 `.env` 的 `UEP_INFER_SYSTEM_PROMPT` 要刪掉或清空；否則會覆寫 repo 的人設檔 `uep/persona/persona.yaml`（legacy 原版＋顏文字句）。
- `.env` 不要設 `UEP_INFER_TTS_ENGINE`（驗預設已是 vox）、`UEP_LLM_REASONING_EFFORT`（驗預設已是 medium）。

| 編號 | 驗什麼 | 怎麼做 | 通過條件／觀察重點 |
|---|---|---|---|
| C1 | 人設 | A1 | 自稱 U、口吻輕鬆，用顏文字、不用 emoji |
| C2 | TTS 預設 Vox＋48k | 不設引擎直接 `--mode real` 跑 A2 | 啟動暖機顯示 VOX；`outputs\e2e` 的 wav 取樣率 48000（檔案內容或播放器資訊可看）；音質與 bot 相當、句尾沒爆音 |
| C3 | 預熱與首音 | 重啟後第一句與第二句比 TTFA（`/trace all`） | 第一句不再明顯慢於後面（啟動時已用人設預熱 prompt cache） |
| C4 | 長對話壓縮 | 連續 `/say` 超過 25 輪（可用短句），第 1 輪講一件具體的事（例如「我今天中午吃了牛肉麵」），最後問「我今天中午吃了什麼」 | 能答出第 1 輪內容（舊版只留 20 則會忘）；過程中沒有卡頓或錯誤；`logs/` 搜 `compress` 可看到壓縮紀錄 |
| C5 | 喚醒詞與熱詞 | `/mic` 說「小U，……」與「U.E.P，……」；說「Bernie」 | 辨識文字正確寫出 小U／U.E.P／Bernie |
| C6 | STT 幻聽過濾 | `/mic` 按開始後保持安靜幾秒再結束；或錄一段只有背景雜音 | 不會冒出「謝謝觀看」「字幕」這類幻聽句、不開回合 |
| C7 | 敘述中保持聆聽 | 先正常對話一兩句讓頻道進入主動模式（`/status` 看 channel mode＝active；只在主動模式生效），再 `/wav` 一段 10 秒以上、中間有短停頓的長句音檔（會被切成多段） | 她等你講完、安靜約 2.5 秒才回，不會在第一段就接話；`/trace` 看各段併成同一輪 |
| C8 | 墊話 | `/say 你還記得我上次說過什麼嗎`（會觸發記憶工具） | 工具執行期間先有一句自然的墊話（模型產生，失敗退固定句），不會長時間沉默 |
| C9 | 做夢迴圈（shadow） | 啟動前設 `UEP_DREAM_WINDOW_START=09:00`、`UEP_DREAM_WINDOW_END=23:59`（涵蓋當下）與 `--memory real`；寫入 5 筆以上記憶後靜置 5 分鐘以上 | `/mem` 出現「做夢迴圈：running=True shadow=True」，且 `shadow would_run` > 0 或最近決策有原因碼；shadow 不會真的做夢（不寫活動紀錄） |
| C10 | agent 回合 | 依 3.1 的 G1–G4 | G3 只回覆一次、Run SUCCEEDED |
| C11 | 情緒語音（可選） | 設 `UEP_INFER_VOX_EMOTION_ENABLED=1` 後跑 A2，請她用開心或難過的語氣說一句 | 中文語氣有差異、音色不跑掉；不滿意就維持預設關 |

**順帶留意**：
- 語音記憶檢索逾時改為 1.5 秒（bot 值），**Console 打字輸入也套用**；若打字時常出現「這輪沒帶到記憶」，回報後再區分語音與文字路徑。
- STT 單一工作逾時 4.0 秒＋每秒音訊 0.5 秒；長時間 `/mic` 不應卡死佇列。
- Windows 下啟動時會關閉本行程的 EcoQoS（避免背景降頻拉長延遲）。
- 有任何一項不通過，在開發房間貼編號＋現象＋`/trace` 輸出即可，綜合會分派修正。

## 4. 目前限制

- A6 多人定址：console 沒有多人輸入入口，只有 pytest 覆蓋，待平台 Adapter。
- `/mic` 是按 Enter 結束，沒有 VAD 自動斷句，也沒有「說話中直接插話」的真正打斷；打斷用 `/cancel` 代替。
- 停頓超過 0.7 秒會切成兩句。主動模式下連續 2 段或 6 秒以上會進入「敘述中」併成同一輪（C7）；被動模式仍是後句取代前句。
- 全域 Stop 的實機行為未驗（A7 只驗降級）。

## 5. 回報方式

在開發房間貼：每項編號＋通過／不通過＋一句觀察；不通過時附 `/trace` 輸出或大概時間點。wav 輸出在 `outputs\e2e\`（已 gitignore）。


## 6. 語音輸入：/mic、/stream、/wav（以本節為準）

> 本節補充並**取代**前文對 `/mic`、`/wav` 的描述（2.1 表格、第 4 節限制中「`/mic` 沒有 VAD 自動斷句」等句）。只有 real STT（`--mode real` 或 `--stt real`）才有語音指令。

### 6.1 三個語音指令

| 指令 | 路徑 | 說明 |
|---|---|---|
| `/mic` | **整段辨識** | Enter 開始、再按 Enter 結束。整段錄音直接交給**已載入的** STT backend 辨識（不經 VAD、不經聚合器、不載第二份模型），結果當**一句** utterance 走正常 turn 流程，一定只有一個 turn。錄音存成 `outputs\e2e\<session>\mic-*.wav` 並印出路徑 |
| `/stream` | **即時串流** | 麥克風持續收音，以真實時間餵進 Host：VAD 斷句、turn 判定、聚合、插話（barge-in）全走正式路徑。即時印出每句辨識（`STT 辨識`）、聚合後的 turn（`聚合 turn`）與 turn 結果；按 Enter 結束並回到指令列。輸入全程存成 `stream-*.wav`（不含結束時補的尾端靜音），結束時印出路徑、總秒數、utterance 數與 turn 數 |
| `/wav <路徑>` | **真實時間餵入** | 以音檔當輸入，**以真實時間速度**餵進 Host（聚合器靠牆鐘判斷停頓，加速餵入會失真），所以**會花與音檔等長的時間**（外加約 1.6 秒尾端靜音，再加聚合等待）。fake 模式不變 |

`/stream` 注意事項：
- 需要 `sounddevice` 與真實 STT，缺少時會印繁中提示；`UEP_INFER_STT_MODE=single` 不支援（沒有即時 feed）。
- 沒帶 `--play` 時只有文字、**沒有聲音**（TTS 仍會寫 wav）；要聽回應與測插話請帶 `--play` 並**戴耳機**，否則喇叭聲被麥克風收回會自我觸發。
- 結束後會補一小段靜音讓最後一句收尾，並等聚合器把緩衝中的 turn 交出，所以按下 Enter 後可能多等數秒。

### 6.2 語音聚合（BufferedAggregator）

語音辨識出來的句子先進 `BufferedAggregator`（搬自 bot 的 turn_buffer，含「敘述中保持聆聽」）合併成 turn；**文字輸入（`/say`、打字）與 `/mic` 的整段辨識不經緩衝**，逐句立即處理。

- 開關：`UEP_VOICE_AGGREGATOR`（預設開，只在有真實 STT 輸入時有作用）；關閉則語音一句一個 turn。
- 等待門檻沿用 bot：被動模式 `merge_gap 1.5s`、`max_wait 3.0s`（被叫名字的 turn 縮為 0.25s）；主動模式 0.25s，並啟用敘述中保持聆聽（連續 2 段或 6 秒以上併成一輪，安靜 2.5 秒才交出）。
- **Console 目前沒有 guild／在場人數，頻道模式恆為被動**，因此：語音說完後最多等 3 秒才開 turn（點名「小U／U.E.P」則 0.25 秒）；段間隔超過 1.5 秒的多段語音仍會分成多個 turn，敘述中保持聆聽在 Console 不會觸發。

### 6.3 斷句與 VAD 設定（`UEP_STT_*`）

預設對齊 bot 現行值，`/stream`、`/wav` 與正式組裝共用。

| 變數 | 預設 | 說明 |
|---|---|---|
| `UEP_STT_SILENCE_THRESHOLD_S` | 0.8 | 靜音多久判定一句話結束 |
| `UEP_STT_MIN_UTTERANCE_S` | 0.3 | 短於此的語音不成一句（咳嗽、雜訊） |
| `UEP_STT_VAD_THRESHOLD` | 0.015 | EnergyVad 進入語音的 RMS 門檻（int16 滿刻度為 1.0）；麥克風音量小可調低 |
| `UEP_STT_VAD_HYSTERESIS` | 0.6 | 離開語音的門檻比例 |
| `UEP_STT_VAD_HANGOVER_S` | 0.15 | 能量低於離開門檻後多久才切回靜音 |

沒有 `max_wait_s` 設定：`TurnPolicy.max_wait_s` 在 Core 與 Echo-Stream 都沒有讀取端（偵測器不看它），bot 設的 12 秒實際上無效，故不提供假設定。要量測 VAD 切段，用 `/stream` 存下的 wav 重放（`/wav`）。
