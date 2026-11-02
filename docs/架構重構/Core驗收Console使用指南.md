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

## 4. 目前限制

- A6 多人定址：console 沒有多人輸入入口，只有 pytest 覆蓋，待平台 Adapter。
- `/mic` 是按 Enter 結束，沒有 VAD 自動斷句，也沒有「說話中直接插話」的真正打斷；打斷用 `/cancel` 代替。
- 停頓超過 0.7 秒會切成兩句，後句取代前句，前句內容不會併進後句（是否合併屬 turn 策略，另案）。
- 全域 Stop 的實機行為未驗（A7 只驗降級）。

## 5. 回報方式

在開發房間貼：每項編號＋通過／不通過＋一句觀察；不通過時附 `/trace` 輸出或大概時間點。wav 輸出在 `outputs\e2e\`（已 gitignore）。
