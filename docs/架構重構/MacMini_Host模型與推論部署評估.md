# U.E.P：Mac mini Host 的模型與推論部署評估

性質：提案文件，供艾斯維爾裁決；純文件，不含程式碼。作者：奈也／奈留（ARCHITECT）。日期：2026-11-01。
基準：D70（in-process）、D73（逐句 Output＋首音預算）、D80（proposer 300ms）、D90（Mac 前過渡期於 Windows 同行程推論）、D91（平台宣告與遠端 executor）；`uep/echo/inference_wiring.py`（TTS／STT／Think 組裝）、`uep/echo/memory_wiring.py`（embedding 設定由 `UepSettings` 給）；`SYS契約增補_平台宣告與遠端執行.md`（下稱「D91 增補」）。
證據標記：【查】＝本次上網查到（來源見 §7）；【房】＝房內轉述事實，未獨立驗證；【推】＝本文推論，必須在 Mac 到手後量測。

## 0. 結論

1. **Embedding 沿用 bge-m3，不 reindex 是硬約束，runtime 的選擇由 cosine 驗證決定，不由偏好決定。** 建議 Mac 上以「sentence-transformers（MPS／CPU）」為第一選項：與 Core 現行預設、既有索引的產生方式同源，向量最可能逐位一致；MLX、GGUF（llama.cpp／Ollama）列為驗證通過才採用的備案。bge-m3 的 MLX 權重目前只有社群量化版，且僅含 dense head【查】；Ollama／GGUF 路徑的向量與原版是否一致沒有可引用的證據，量化版有 0.97–0.98 級別的漂移報告【查】，所以必須實測，不得假設。
2. **TTS 是最大風險，Mac 本機延遲不可預期，建議把 TTS 保留在 Windows GPU 機，走 D91 遠端 executor／遠端 Speak stage。** VOX 的 0.2s 首包與 RTF 0.46–0.57 是 CUDA＋torch.compile＋triton 的數字【房】；VoxCPM2 在 Apple Silicon 只有社群 MLX 移植（官方 mlx-audio 後端只到 VoxCPM 1.x）【查】，成熟度與音質、首包皆無數據。Mac 本機 TTS 只當離線備援，量測過關才升格。
3. **STT 在 Mac 本機換 backend：whisper.cpp（Metal／Core ML）或 mlx-whisper，不再用 faster-whisper。** faster-whisper／CTranslate2 在 Mac 只能跑 CPU【查，二手來源】。STT 是輸入熱路徑，遠端化會把網路 RTT 加進每個 utterance，不建議遠端。
4. **LLM 主對話維持雲端 API；Dream／fallback 由 Ollama 換成 llama.cpp server 或 MLX-LM（OpenAI 相容端點），二擇一待 Mac 上實測。** 這與「棄用 Ollama」一致，且 Dream 為背景工作，延遲不敏感。
5. **對接 D91：遠端 executor 的定位是「工具」，不是「推論 stage」。** D91 增補承載的是平台工具（發訊息、查名冊等）與短時同步呼叫，且明列「長時操作」為非目標（RX-14）。TTS 串流音訊若要走遠端，不能直接套用 D91 的 `dispatch`／`receipt` 同步模型，需要另一個「遠端推論 stage」契約（見 §4.3）。這是本提案最需要艾斯維爾裁決的架構問題（Q1）。

## 1. 現況與約束

| 項目 | 現況 | 來源 |
|---|---|---|
| 部署 | Core 之後在 Mac mini（Apple Silicon）；Mac 未到手；過渡期 Windows＋RTX 5070 Ti 同行程推論 | 任務背景、D90 |
| 行程模型 | D70：ES 在 Core 內專屬 loop 執行緒；P8 前再評估獨立行程 | D70 |
| TTS | `inference_wiring.py` 支援 `indextts2`、`vox`；backend 預建、`warmup` 在 Host 啟動 `prepare`；vox prepare 實測約 110–180 秒；`tts_device` 預設 `cuda` | 程式 |
| STT | `echo_stream` 的 STT stage 在 prepare 載入 faster-whisper | 程式、【房】 |
| Think | LLM backend 為 API client，無本地模型 | `default_think_backend` |
| Embedding | `EchoEngineFactory` 透過 `embedding_backend`／`embedding_model` 傳給 `MemoryEngine`；EM 目前只支援 sentence-transformers 與 ollama；Core 預設 sentence-transformers，bot 走 Ollama bge-m3 | 程式、【房】 |
| 降級 | speak／think／input 三個 stage 都有 prepare 失敗降級守衛 | 程式 |
| 延遲預算 | D73 首音增量上限暫定 +150ms（以 ES Web 路徑實測為零點）；D80 proposer 300ms | DECISIONS |

奈留註記：D73 的 +150ms 是「逐句 OutputIntent 相對 Web 路徑的增量」，不是 TTS 首包預算；TTS 遠端化若引入網路 RTT，會吃掉這個增量，兩者必須在 S6 同一次量測中一起看。

## 2. 各類模型的部署選項比較

評分用語：延遲（相對現行 Windows CUDA 同行程）、維護成本、離線能力、reindex 風險（僅 embedding 有意義）。

### 2.1 Embedding（bge-m3，1024 維 dense）

| 選項 | 延遲 | 維護成本 | 離線 | reindex 風險 | 備註 |
|---|---|---|---|---|---|
| A. Mac 本機 sentence-transformers（MPS 或 CPU） | 【推】單句短文本 CPU/MPS 皆為數十 ms 級，寫入與檢索量不大，足夠 | 低：EM 已支援，零新增 backend | 完全離線 | **最低**：與現行索引同一 runtime 與權重；MPS 與 CUDA 的浮點差異預期遠小於 1e-4，但仍需量 | 建議首選 |
| B. Mac 本機 MLX（mlx-embeddings＋社群 bge-m3 權重） | 【推】應不差於 A | 中：需在 `storage/episodic.py` 新增 backend（EM 估工作量小）；社群 4/6/8-bit 權重非官方【查】 | 完全離線 | **中到高**：量化權重必然偏離；僅 dense head，無 sparse／ColBERT【查】。某 8-bit 社群卡聲稱與 oMLX 向量 cosine=1.0，但那是對自家伺服器，不是對 BAAI 原版【查】 | 只在 A 的延遲不達標時考慮，且必須用 bf16／fp16 權重驗證 |
| C. Mac 本機 llama.cpp GGUF（f16／Q8） | 【推】同量級 | 中：新增 backend 或走 OpenAI 相容端點；需確認 pooling=CLS 與 L2 normalize | 完全離線 | **中到高**：pooling／normalize 設定錯誤會直接偏離；Q4 級別報告 cosine 約 0.97–0.98（對 f16）【查】 | 與「棄用 Ollama」同屬 GGUF 家族，但沒有 Ollama 的服務層 |
| D. 沿用 Ollama bge-m3 | 同上 | 低（現狀）但與「棄用 Ollama」目標衝突 | 完全離線 | **零**（對 bot 既有索引） | 這是現狀；若 Core 與 bot 索引本來就不同 runtime，兩邊向量本來就未必一致，見下方警告 |
| E. Windows 遠端 embedding（D91 遠端 executor） | 加 LAN RTT；每次 retrieve 多一跳 | 高：Mac 對 Windows 形成寫入／檢索的硬依賴，Windows 關機即記憶失能 | 否 | 同 Windows 上所用 runtime | 不建議：記憶是核心路徑，不該依賴開發機 |
| F. 雲端 embedding API | 加 WAN RTT | 低 | 否 | **換模型即全量 reindex**，違反約束 | 排除 |

**警告（奈留）**：房內提出「Core 預設 sentence-transformers、bot 走 Ollama bge-m3」。若兩套索引由不同 runtime 產生，它們彼此之間可能已有漂移；在 Mac 上換 runtime 前，先量「Core 現有索引 vs 現行 runtime 重算」與「Ollama vs sentence-transformers」兩組基線，才知道 0.9999 門檻是否對現有資料本身成立。

### 2.2 TTS（VOX voxcpm2 為主、IndexTTS2 為備）

| 選項 | 首包／RTF | 維護成本 | 離線 | 備註 |
|---|---|---|---|---|
| A. Mac 本機 PyTorch MPS | 【推】無 triton、`torch.compile` 收益不確定，延遲可能明顯變差；本次查無 VoxCPM 在 MPS 的任何公開數據 | 低（程式碼沿用），但 `tts_device="cuda"` 預設與 CUDA 專用的 accel 旗標需改 | 完全離線 | 首包與 RTF 完全未知，必須實測 |
| B. Mac 本機 MLX（VoxCPM2 社群移植，bf16 約 5GB／int8 約 3GB／int4 約 1.9GB） | 【推】可能可用，但無延遲數據；官方 mlx-audio 後端只支援 VoxCPM 1.0／1.5，VoxCPM2 為社群移植【查】 | 高：需新 backend 與 `VoxSpeakStage` 相容層；量化影響音質；非官方權重的供應鏈風險 | 完全離線 | LoRA（IndexTTS2 有 `lora_scale`）能否移植未知 |
| C. Windows 遠端推論（沿用 CUDA 基準） | 保持 0.2s／RTF 0.46–0.57 的推論數字【房】，再加 LAN RTT 與音訊串流傳輸 | 中：需遠端 stage 契約與兩機運維；Windows 須常開 | **否**：Windows 關機即無語音（降級為文字，現有 `_SpeakGuard` 已支援無聲降級） | 目前唯一有實測基準的路徑 |
| D. 雲端 TTS API | 取決於供應商；失去自家聲線 | 低 | 否 | 與 VOX 聲線資產衝突，列為最後備援 |

### 2.3 STT（目前 faster-whisper／ctranslate2）

| 選項 | 延遲 | 維護成本 | 離線 | 備註 |
|---|---|---|---|---|
| A. 留 faster-whisper 於 Mac（CPU） | 差：某基準 large-v3 CPU 約 3× 即時，對比 whisper.cpp Metal 約 10×【查，二手，M5 Pro】 | 最低 | 離線 | 僅作最後備援 |
| B. whisper.cpp（Metal；可選 Core ML encoder） | 好：Metal 全 GPU 推論；Core ML encoder 官方稱較 CPU 快 3 倍以上【查】 | 中：C++ 綁定或 server；需新 STT backend；Core ML 首次載入需編譯，且需 macOS Sonoma 以上【查】 | 離線 | 有 `whisper-stream` 範例但官方稱 naive【查】；ES 用 VAD 切 utterance 後整段辨識即可，不依賴其串流 |
| C. mlx-whisper | 好（MLX 原生 GPU）【推】 | 中：純 Python，整合最容易；pip 安裝【查】 | 離線 | 套件最新版本我只確認到 0.4.3，且為鏡像站資訊，發布日期不明【查，未達官方來源】；專案在 ml-explore/mlx-examples 下，是否仍活躍查不到 |
| D. Windows 遠端 STT | LAN RTT 加在每個 utterance；多聲道多人語音下音訊串流量大 | 高 | 否 | 不建議：輸入熱路徑 |
| E. 雲端 STT | WAN RTT；語音外送涉隱私（D77/audience 規則） | 低 | 否 | 排除 |

多語言與說話者數量：multichannel STT 是「每位 speaker 一路」。多路並行在 Mac 本機 GPU 上的吞吐是 B／C 的未量測風險（【推】）。

### 2.4 LLM

**主對話（Think，雲端 API）**：維持現狀。Mac 與 Windows 無差別，推理在雲端。離線時只剩降級（`_ThinkGuard`：輪失敗、Host 與記憶照常）。若艾斯維爾要「離線也能對話」，那是新需求，需要本地大模型，Mac mini 記憶體上限決定可行規模，不在本提案範圍（Q6 列為待問）。

**Dream／fallback（背景，目前 Ollama，`OLLAMA_FALLBACK_NUM_GPU=0`）**：

| 選項 | 延遲 | 維護成本 | 離線 | 備註 |
|---|---|---|---|---|
| A. 留 Ollama | 無關緊要（背景） | 低 | 離線 | 與棄用目標衝突 |
| B. llama.cpp server（Metal） | 背景可接受 | 中：自管服務與模型檔 | 離線 | OpenAI 相容端點，Dream 的 `llm_fn` 只需改 base URL；【推】 |
| C. MLX-LM server | 背景可接受 | 中 | 離線 | 本次未查證其成熟度，需 Mac 上試 |
| D. 雲端 API 當唯一路徑 | — | 低 | 否 | 記憶內容（含私人）外送受 `data_egress` 規則約束，Dream 是私人資料最密集處，不建議無離線備援 |
| E. Windows 遠端 LLM | LAN RTT | 高 | 否 | Windows 關機即失效，與 Ollama fallback 的初衷（雲端掛了還能做事）矛盾 |

注意：`OLLAMA_FALLBACK_NUM_GPU=0` 表示現行 fallback 刻意只用 CPU（【房】，動機未在本次資料中說明，推測為避免與 TTS／STT 搶 VRAM）。Mac 為統一記憶體，沒有「獨立 VRAM」，此約束的前提在 Mac 上不同，需重新決定（Q5）。

## 3. 建議方案

| 模型 | 建議 | 理由 |
|---|---|---|
| Embedding | Mac 本機 sentence-transformers（MPS，不穩則 CPU）；MLX／GGUF 為備案，通過 cosine 驗證才採用 | reindex 風險最低、零新 backend |
| STT | Mac 本機 whisper.cpp（Metal）或 mlx-whisper，二者在 Mac 上對量後擇一 | faster-whisper 無 Metal；STT 不可遠端 |
| TTS | **過渡與預設：Windows 遠端推論**；同時在 Mac 上評估 MLX（VoxCPM2 社群移植）與 MPS，達標才遷回本機 | 唯一有基準的路徑；Mac 本機結果未知 |
| LLM 主對話 | 雲端 API，不變 | — |
| Dream／fallback | Mac 本機 llama.cpp server 或 MLX-LM，取代 Ollama | 背景工作，延遲不敏感，需離線 |

奈也：先讓聲音在 Mac 版本「活起來」比追求全本機重要，遠端 TTS 讓 Mac 到手那天就能開始整合其他東西，不被 TTS 卡住。
奈留：遠端 TTS 是新的單點故障，且 Windows 關機等於語音死亡；降級路徑（無聲、文字照常）已經存在，但「常開 Windows」必須明確寫成部署前提，否則 Mac 本機化只是時間問題。

### 3.1 為何不全本機、也不全遠端

- 全本機：TTS 無數據，賭 Mac 上 VOX 能達標風險過高；但 Mac 本機 TTS 仍必須評估，因為它是離線能力與去除對 Windows 依賴的唯一出路。
- 全遠端：記憶與 STT 在熱路徑，遠端化會讓核心對話依賴開發機；且 D91 的 RX-14 與同步 `dispatch` 模型不適合串流推論。

## 4. 與 D91 契約的對接點

### 4.1 D91 能直接承載的部分

D91 增補的遠端 executor 是 `endpoint_auth`／`endpoint_best_effort` 兩鍵、同步 `ToolExecutor`、`transport_timeout_max` 起點 30 秒、單次 dispatch→receipt（RX-1、RX-3、RX-7、RX-14）。**非串流、可冪等、短時**的呼叫適用，例如：

- 一次性 embedding 批次重算（若選擇在 Windows 上做 reindex 工具）；
- Dream 的單次 LLM 摘要呼叫（若未來把 Dream LLM 放 Windows；本提案不建議）；
- 模型健康／版本查詢。

若走這條路，核准清單需宣告（沿用 D91 §2.4 欄位）：`provider=<platform_id>`（例 `windows_infer`）、`targeting=platform_remote`、`lookup_authority`（embedding／推論為純函式，`idempotency=native`，可標 `authoritative` 的前提是平台有持久 ledger，**純計算不寫 ledger 時應選 `non_authoritative`**）、`side_effect_class`（純推論＝`internal_read` 類）、`transport_timeout`（≤ `transport_timeout_max`）、`venue_policy`（不涉場域，`any`）、`injected_params`（無）。

### 4.2 D91 不能直接承載的部分

TTS 串流音訊與 STT 連續音訊是**長連線串流**，不是「工具呼叫」：

1. 同步 `dispatch`→`receipt` 無法表達逐 chunk 回傳；若把整句音訊塞在 `receipt.result`，首包延遲 = 整句合成時間，抵銷 VOX 的 0.2s 首包優勢（【推】）。
2. `receipt.result` 依 RX-13 走 blob 且為「不可信資料」；TTS 音訊是 Core 自己的輸出，不應被當不可信資料，也不該落成 journal blob。
3. RX-14 明確把「超過 `transport_timeout` 的長時操作」排除出 S2。

### 4.3 建議新增的契約（待裁決，Q1）

「遠端推論 stage」：與 D91 工具通道**共用連線層與每訊框 MAC（RX-11）、`platform_id` 金鑰、連線狀態機（§3.1）**，但訊息類型獨立：

- `stage_open`／`stage_chunk`／`stage_end`（Core→推論機送句子，推論機回音訊 chunk）；
- 宣告走 `hello` 的粗粒度 `capabilities`（如 `tts_stream`、`stt_stream`），**不進工具核准清單**，也不是 `ToolSpec`；LLM 不可見；
- 在 `inference_wiring.py` 的 `build_inference_wiring` 層以新增一個 `RemoteSpeakStage`（實作 ES `SpeakStage` 介面）取代 `VoxSpeakStage`；`_SpeakGuard` 現有降級（無聲）原樣套用，`prepare` 逾時即標 not ready。

這樣 Core 其餘部分（Orchestrator、Output Gate、D73 逐句 intent）不知道 TTS 在哪台機器，符合 P3-ADR-1「Core 只認識管線」。D90 已預告「遠端 Speak／Input stage 與遠端 sink 契約屬 P8 前工作」，本提案是這項工作的具體化。

**能力宣告建議**（hello `capabilities`，粗粒度）：`tts_stream`、`tts_engine:vox`、`tts_sample_rate`、`tts_languages`、`warm`（已暖機）。`warm=false` 時 Core 視為 SUSPECT 等價狀態（不派句）。

### 4.4 憑證與信任邊界

遠端推論機不持有任何平台憑證，僅持 `platform_id` 連線金鑰（RX-12 的最小權限）；傳送給它的文字是 Core 已決定要說的話，不含 Lore／Chatroom 憑證。此邊界與 D91 一致，無需放寬。

## 5. 驗證計畫（Mac 到手後）

原則：每項量測在**同一份測試資料、冷啟動與暖機後各一次**、並在 STT／TTS／embedding 同時負載下重複一次（呼應 D90「同卡負載下量測」）。門檻中標【暫定】者為本文提案，需艾斯維爾確認。

### 5.1 Embedding

| 量測 | 方法 | 門檻 |
|---|---|---|
| 基準自洽 | 現有 Core 索引文本抽樣 N≥1000（含中、英、日、混合、長短），以現行 runtime（sentence-transformers／CUDA）重算，對比既有儲存向量 | 逐筆 cosine ≥ 0.9999；先確認現有資料本身成立 |
| 跨 runtime | 候選 runtime（ST-MPS、ST-CPU、MLX、GGUF f16／Q8、Ollama）對同一批文本，與基準向量逐筆 cosine | **min cosine ≥ 0.9999**（房內既定）；同時記錄 mean／p1／min 與最差案例文本 |
| 檢索等價 | 以 200 個真實查詢對比 top-10 召回集合與排序 | top-10 集合完全一致為目標；差異者列出，供裁決 |
| 延遲 | 單句、32 句批次、長文本（接近 8192 token 上限）的 p50／p95 | 單句 p95 ≤ 100ms【暫定】；批次吞吐不低於 Windows 基準的 50%【暫定】 |
| 失敗後備 | 不過 0.9999 → 不得採用；改做「重算工具」並評估 reindex 成本（筆數、時長） | — |

### 5.2 TTS

| 量測 | 方法 | 門檻 |
|---|---|---|
| 首包延遲 | 句子送入到第一個音訊 chunk 的時間，各語言（zh／en／ja）、短句與長句；暖機後 | 與 Windows 基準相比：本機候選 **首包 ≤ 0.5s**【暫定，Windows 為約 0.2s】；遠端候選 **首包 ≤ 0.2s＋LAN RTT p95＋50ms**【暫定】 |
| RTF | 合成時長 / 音訊時長，連續 20 句 | **RTF < 1.0 為可串流底線**；RTF ≤ 0.8 為本機採用門檻【暫定，Windows 為 0.46–0.57】 |
| 句間空檔 | 連續句子 chunk 間隙 p95；與 D73 +150ms 增量合併計 | 不得出現可聽見的斷點；p95 空檔 ≤ 150ms【暫定，沿用 D73 數值】 |
| 音質 | 同一批句子 Windows 版 vs Mac 版盲聽（艾斯維爾主觀），及 ASR 回轉字錯率 | 艾斯維爾主觀可接受；量化 MLX 版須單獨聽 |
| 冷啟動 | `prepare` 到 ready 的時間 | 現行 vox 約 110–180 秒；Mac 若更久需調 `speak_prepare_timeout_s`（預設 240 秒） |
| 負載下穩定 | 與 STT、Dream 同時運行 30 分鐘 | 無 OOM、無首包 p95 劣化超過 30%【暫定】 |
| 遠端（若選） | LAN RTT、chunk 抖動、斷線重連 | RTT p95 ≤ 10ms；斷線 30 秒內降級為文字且不卡輪 |

### 5.3 STT

| 量測 | 方法 | 門檻 |
|---|---|---|
| 延遲 | utterance 結束到最終文字，各長度（2s／8s／20s），單路與 N 路並行（N=3、5） | 單路 8s utterance ≤ 0.5s【暫定】；N 路並行下 p95 不劣化超過 2 倍 |
| 準確度 | 與 faster-whisper large 系列在同一批中文／英文／混合語料比字／詞錯率 | 相對劣化 ≤ 10%【暫定】；人名錯誤另列（D87 提到人名容錯） |
| 即時倍率 | 長音檔吞吐 | ≥ 5× 即時【暫定】；二手資料顯示 whisper.cpp Metal 約 10×、CPU faster-whisper 約 3×（M5 Pro）【查】，需在 Mac mini 實測 |
| 載入時間 | `prepare` 時間 | ≤ `input_prepare_timeout_s`（預設 10 秒）；超過則調整或預載 |

### 5.4 LLM（Dream／fallback）

| 量測 | 門檻 |
|---|---|
| Dream 單次呼叫 p95 延遲與吞吐 | 背景 900 秒週期（D95）內完成 LLM 40 次的預算可達 |
| 輸出品質 | 同批 Dream 輸入，與現行 Ollama 模型輸出對比 JSON 結構合法率 | ≥ 現行 |
| 記憶體占用 | 與 STT、TTS、embedding 同駐時，不觸發 swap | 無 swap 增長 |

### 5.5 驗證順序

1. embedding cosine（最先；不通過會影響所有後續記憶工作，且與其他項目互不依賴）；
2. STT 兩候選對比；
3. TTS 的 MPS 與 MLX 本機候選；
4. 若 TTS 本機不達標：Windows 遠端 TTS 的 LAN 量測；
5. 全部同載壓力測試；
6. Dream 本機 LLM。

## 6. 風險與技術債

| 風險 | 說明 | 緩解 |
|---|---|---|
| TTS 本機不達標 | 可能整個 Mac 方案要長期依賴 Windows | 遠端 stage 契約＋降級；及早量測 |
| Windows 常開依賴 | 遠端 TTS 成為新單點 | 明確寫入部署前提；降級為文字 |
| embedding 靜默漂移 | 向量略有偏差時檢索品質緩慢劣化，不會報錯 | 0.9999 門檻＋抽樣回歸測試納入 CI；記錄 runtime／模型哈希於索引中繼資料（需 EM 配合，屬建議） |
| 社群權重供應鏈 | MLX 的 bge-m3、VoxCPM2 皆為非官方移植 | 固定版本與雜湊、保存本機副本 |
| 統一記憶體競爭 | Mac 沒有獨立 VRAM，STT／TTS／embedding／本地 LLM 搶同一池 | 同載壓力測試；記憶體預算表 |
| 兩種 Python／環境 | Core 3.12（D90）與推論依賴在 macOS 上的 wheel 可得性（MeCab／fugashi、MLX 等）未驗證 | Mac 到手第一天先做依賴安裝演練 |
| `tts_device="cuda"` 與 CUDA 專用旗標寫死在 `InferenceConfig` 預設 | Mac 上需顯式覆寫，否則 prepare 失敗後降級為無聲而非報錯到使用者 | 屬實作切片；本文不改程式 |
| 本文引用的二手資料 | STT 速度與 CTranslate2 無 Metal 支援來自部落格比較，非官方文件 | Mac 上實測，不以本文數字決策 |

## 7. 資料來源與查證限度

【查】項目：

- whisper.cpp 官方 README（Metal、Core ML、`whisper-stream`、ANEForge 實驗）：https://github.com/ggml-org/whisper.cpp
- mlx-whisper：https://github.com/ml-explore/mlx-examples/tree/main/whisper（README 指示 `pip install mlx-whisper`；頁面未顯示發布日期與維護活躍度）；版本 0.4.3 與「約 9 個月前」來自 https://cloudsmith.com/navigator/pypi/mlx-whisper（鏡像站，非 PyPI 官方）
- bge-m3 MLX 社群權重：https://huggingface.co/YCF-AI/bge-m3-8bit-MLX、https://huggingface.co/TyKaoz/bge-m3-6bit、https://huggingface.co/TyKaoz/bge-m3-4bit（僅 dense head；非 BAAI 官方）；mlx-embeddings 對 bge-m3 的官方支援查無明文
- bge-m3 GGUF 社群版（量化對 f16 的 cosine 約 0.97–0.98 的報告、pooling 設定）：https://huggingface.co/bbvch-ai/bge-m3-GGUF 及同次搜尋之其他 GGUF 卡片
- VoxCPM 在 Apple Silicon：https://voxcpm.readthedocs.io/en/latest/deployment/mlx_audio.html（官方文件，mlx-audio 後端為社群維護，僅 VoxCPM 1.0／1.5）；VoxCPM2 社群移植 https://huggingface.co/hbasrisahin/VoxCPM2-MLX、https://huggingface.co/aufklarer/VoxCPM2-MLX-int4
- faster-whisper／CTranslate2 在 Mac 僅 CPU：https://pyvideotrans.com/blog/ctranslate2-whisper 及 https://www.promptquorum.com/fr/power-local-llm/local-whisper-stt-comparison（二手部落格，未查到 CTranslate2 官方 issue）

查不到、須明說：

1. **Ollama bge-m3 與 sentence-transformers bge-m3 向量實際 cosine**：無可引用的實測。
2. **mlx-whisper 的官方最新版本、最後發布日期與維護狀態**：只拿到鏡像資訊。
3. **VoxCPM2 於 PyTorch MPS 的任何數據**，以及 VoxCPM2 MLX 移植的首包、RTF、音質：皆查無。
4. **IndexTTS2 於 Apple Silicon 的狀態**：本次未查，任務所述「同理」僅為推論。
5. **MLX-LM、llama.cpp server 作為 Dream 後端的成熟度與 Mac mini 實測**：未查。
6. **Mac mini 具體型號與記憶體**：未知；本文所有容量判斷皆待型號確定（Q2）。
7. 本文沒有讀 ES／EM／TTS 子系統原始碼，STT／embedding backend 的實作細節僅取自房內轉述與 `*_wiring.py`。

## 8. 需要艾斯維爾裁決的問題

| # | 問題 | 建議選項 | 理由 |
|---|---|---|---|
| Q1 | TTS（及日後 STT）若遠端化，是否新增「遠端推論 stage」契約（與 D91 共用連線與 MAC，但訊息類型獨立），而不是硬套 D91 的工具 `dispatch`／`receipt`？ | 新增，且列為 P8 前工作 | D91 RX-14 明確排除長時／串流；硬套會毀掉首包優勢 |
| Q2 | Mac mini 型號與記憶體（含是否為 M4 Pro／64GB 等級）？這決定能否同駐 STT＋TTS＋embedding＋本地 LLM | 先定型號再訂預算；未定前以 24GB 級保守估算 | 統一記憶體無獨立 VRAM |
| Q3 | Mac 上 TTS 預設：(a) Windows 遠端；(b) 先試 Mac 本機（MPS／MLX），不達標才遠端；(c) 雲端 | (b)：到貨後立即量測，量測期間以 (a) 兜底 | 避免在無數據時就把部署鎖死 |
| Q4 | 是否把「Windows 常開」列為明文部署前提（若 Q3 選 (a)/(b) 兜底）？關機時語音降級為文字是否可接受？ | 可接受降級；列為前提但非硬依賴 | 現有 `_SpeakGuard` 已支援 |
| Q5 | Dream／fallback 取代 Ollama：llama.cpp server 或 MLX-LM？`OLLAMA_FALLBACK_NUM_GPU=0` 的「不用 GPU」約束在 Mac 是否保留？ | llama.cpp server（成熟度較可預期）；Mac 上允許 Metal，但需壓力測試不干擾 TTS／STT | Mac 無獨立 VRAM，原約束前提改變 |
| Q6 | 是否需要「雲端全斷時的離線對話」（本地主對話 LLM）？ | 暫不要；先只保 Dream／fallback | 會大幅拉高 Mac 記憶體需求，屬新需求 |
| Q7 | Embedding 驗證門檻：沿用 cosine ≥ 0.9999（min，逐筆）？未過是否一律先做重算工具、不接受「平均夠高」？ | 沿用，且採 min 而非 mean；未過先做重算工具 | 房內既定；mean 會掩蓋少數最差案例 |
| Q8 | Embedding 第一順位：sentence-transformers（Mac）；MLX／GGUF 僅在 ST 延遲不達標時才驗證？ | 照此 | 向量同源，風險最低 |
| Q9 | Core 與 bot 的 embedding 是否須收斂到單一 runtime（現況 Core=ST、bot=Ollama）？並在索引中繼資料記錄 runtime 與模型雜湊？ | 收斂；P4 bot 退場（D76）時一併處理 | 兩套 runtime 的向量本身未必一致 |
| Q10 | STT 在 Mac：whisper.cpp 與 mlx-whisper 擇一的決策原則？ | 到貨後以 §5.3 量測，延遲達標且準確度相對劣化 ≤10% 者中，選整合成本低者（傾向 mlx-whisper 純 Python） | 兩者皆無本地數據 |
| Q11 | §5 各項【暫定】門檻（首包 ≤0.5s 本機、RTF ≤0.8、STT 單路 ≤0.5s 等）是否採用？ | 採用為初值，Mac 到手第一輪量測後可調整一次 | 無 Mac 基準前只能暫定 |
| Q12 | 非官方社群權重（bge-m3 MLX、VoxCPM2 MLX）是否可進入正式部署？ | 可以，但固定版本與雜湊並保存本機副本 | 供應鏈與可重現性 |
