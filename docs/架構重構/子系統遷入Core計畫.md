# 子系統遷入 Core 計畫

狀態：**提案（供艾斯維爾裁決）**，純文件；不含程式變更、不 commit。撰寫：奈也／奈留（架構），2026-11-01，依 MIN 派遣與艾斯維爾 #203 補充。
範圍：把 Echo-Stream、TestSeperateMemorySystem、TestSeparateSTTSystem、TestSeparateSessionControl、TestSeparateTTSSystem 五個子系統併入 `U.E.P-s-Core`，並規劃 ES 在 Core 內的角色、三平台接口、工具層收斂與舊 `modules/` 退場。Discord bot（`../U.E.P's Mind Reflourished`）維持獨立 repo。

證據標記：**[執行]** 實際跑過的唯讀指令（只做 `pytest --collect-only`，沒有啟動任何應用、沒有跑 GPU 測試）；**[讀碼]** 讀檔或 grep；**[推論]** 由前兩者推得、未驗證。

---

## 0. 結論（先看這段）

### 0.1 一句話

**先搬、後融。** 第一階段只做「行為不變的實體搬遷」：保留原頂層套件名（`echo_stream`、`echo_memory`、`echo_stt`、`echo_thought_core`、`echo_tts`、`llm_integration`、`mcp_server`），放進 Core repo 的新目錄 `subsystems/<name>/`，沿用共用環境；每個子系統在 Core 內把自己的全套測試跑到與搬遷前相同的數字才算完成。第二階段才做「融合」：ES 與 `uep/` 重疊的概念合併、工具層收斂到 Part S、平台接口統一。兩階段不混在同一個 PR。

### 0.2 為什麼這樣排

1. **搬遷本身風險不高、融合風險很高。** 五個子系統合計約 13.7 萬行 Python（含 TTS 內嵌引擎 6.6 萬行）、約 4,150 個測試；Core 已經在 import 它們、bot 也是（約 110 行 import）。保留套件名，Core 的 20 行 ES import、bot 的全部 import、各子系統自己的約 1,550 行內部 import 一行都不用改。
2. **真正的阻礙不是路徑，是「同名頂層套件」與「相對於 repo 根的路徑假設」**（見 §2.3、§3.2）：`echo_tts` 內部用 `from modules.…`、`from utils.debug_helper …`，靠 `sys.path.insert(0, echo_tts/)` 才解析得到；Core 的 legacy 根目錄恰好也有 `modules/`、`utils/`（`modules/tts_module/s2mel`、`utils/debug_helper.py` 都存在，[執行] 已確認），bot 也有自己的 `utils/`。這件事必須在 TTS 遷入前修好，而且修法與「放在哪裡」無關。
3. **模型與資料不跟著進 git。** TTS 權重 8.98 GB、STT 權重 2.9 GB 都被 `.gitignore` 排除，搬遷時只搬程式，路徑改由環境變數指向既有位置。
4. **早開始不等 TTS。** Session、EM（含 `llm_integration`、`mcp_server`）、STT 三個不碰 VOX，可以先遷；ES 次之；TTS 最後，且只要求「合入現有兩條在途分支」，不要求 VOX 調音完成（見 Q4）。

### 0.3 建議方案摘要

| 項目 | 建議 |
|---|---|
| 佈局 | 保留原套件名；放 `subsystems/<name>/`，內含該 repo 原結構（`<pkg>/`、`tests/`、`pyproject.toml`），不拆平（保住 `configs/`、`.env`、`./data` 的相對路徑假設） |
| 安裝 | Core 的 hatch 設定以 editable 只提供各**套件**（`subsystems/<name>/<pkg>`），**子系統根目錄不得進 sys.path**（否則其 `tests/`、`configs/`、`Entry.py` 會變成頂層可 import）；做法需 M0 spike 驗證（hatch `dev-mode-exact` 或等效）；移除五個 editable 安裝與 `UEP_INFER_*_REPO` |
| git 歷史 | `git subtree add`（不 squash），保留雜湊與 GPG 簽章；`git filter-repo` 為備選（見 §6） |
| 遷移順序 | M0 準備 → M1 Session → M2 EM 三包 → M3 STT → M4 ES → M5 TTS → M6 收尾 |
| ES 在 Core 內 | 機制留在 ES、權威在 Core（沿用 D71）；**合併** Addressing／Turn 偵測實作、DreamScheduler、設定系統、重複的 member_recall 實作；**升格** 為 Core 一等公民的是設定、身分／addressing、dream 排程與記憶工具定義；維持 adapter 層的是五個子系統 adapter 與 Discord／Web 音訊轉接 |
| 平台接口 | 三平面（控制／輸入／輸出）＋既有 Presence 與 D91 工具通道；bot 分四階段從「行程內自帶 ES」改為「透過接口」（B0–B3） |
| 工具層 | `ToolSpec` 為唯一來源；六個來源、四種執行位置收斂為「Core 註冊表＋三種 provider」（core、platform_remote、mcp_static）；記憶工具是第一批遷移對象，legacy 工作流最後 |
| legacy 退場 | 分 R0–R4 五批；`stt/tts/mem/llm_module` 隨 M 系列被取代後先退；`core/`、`ui`、`ani`、`mov`、`nlp`、`sys` 要等 Desktop Adapter、Part S S5 才能退 |

### 0.4 需要艾斯維爾裁決（摘要；完整版見 §10）

| # | 問題 | 建議 |
|---|---|---|
| Q1 | 遷移分支的基線：Core 在 `feature/complete_revamp`，領先 `develop` 73 個 commit，`develop` 停在 v0.9.4 | 先把 `complete_revamp` 合入 `develop`（或明訂它暫代 develop），遷移分支再從它長出 |
| Q2 | 佈局與命名 | 保留原名＋`subsystems/<name>/`（§4） |
| Q3 | git 歷史 | subtree 不 squash（§6） |
| Q4 | TTS 凍結點 | 等 `feature/tts-model-eval`、`feature/vox-emotion` 兩條合入 develop 即凍；之後的調音在 Core 內做 |
| Q5 | 工具收斂的衝突取捨（Core Access Layer 與 ES adapter 兩套 member_recall、about-me 三工具缺口） | ToolSpec 為單一來源，ES adapter 的工具定義降為實作細節 |
| Q6 | `echo_thought_core.backends` 是否升格為 Model Gateway 實作 | 是；Core 不再另建一套 LLM client |
| Q7 | ES 的 `web/server.py`（2,138 行）、`cli.py` 去留 | 不併入 `echo_stream` 套件的對外面，改為 Website Adapter 的起點與開發工具 |
| Q8 | legacy 的保存與 Desktop 過渡 | 原位保留到 P9，凍結＋標記，不搬 `legacy/` |

---

## 1. 盤點

### 1.1 套件結構、大小與測試

| 子系統（repo 目錄） | 頂層套件 | Python 檔／行數 | 追蹤檔數／`.git` | commits | 測試檔／收集數 [執行] | 測試執行方式與坑 |
|---|---|---|---|---|---|---|
| Echo-Stream | `echo_stream` | 45／17,075 | 111／10 MB | 166 | 48／**999** | `pytest`（`asyncio_mode=auto`，`addopts=-q`） |
| TestSeperateMemorySystem | `echo_memory`、`llm_integration`、`mcp_server` | 80／35,893；6／1,817；6／1,365 | 265／20 MB | 276 | 104／**2,167**（另 12 個 deselect：`live_api`、`slow`） | `addopts` 內含 `--ignore` 兩個檔（互動式腳本、診斷腳本）與 `-m "not live_api and not slow"` |
| TestSeparateSTTSystem | `echo_stt` | 31／7,082 | 65／5 MB | 43 | 15／**271** | 收集耗時 20 秒（import torch／whisper）；`echo_stt/test_files/` 內有 2.8 MB 音檔跟著套件走 |
| TestSeparateSessionControl | `echo_thought_core` | 40／9,130 | 68／4 MB | 63 | 13／**327** | **`addopts` 帶 `--cov`，共用環境沒有 pytest-cov，直接 `pytest` 會報 unrecognized arguments**；需 `-o addopts=""`；`e2e` 標記預設排除 |
| TestSeparateTTSSystem | `echo_tts` | 230／65,935（含內嵌 IndexTTS 引擎） | 325／18 MB | 114 | 29／**384** | 含 GPU `skipif`（`test_vox_backend_gpu.py` 等）與 `importorskip`（torch、peft、pyopenjtalk）；pass 數隨機器而異，收集數不等於 pass 數 |
| **合計** | 7 個頂層套件 | 約 13.7 萬行 | 約 830 檔／約 57 MB | 662 | **4,148** | |

對照：Core `uep/` 20,706 行、`tests/uep` 1,490 收集（MIN 轉述 1,486 passed）[執行]。

注意事項：

- 上表測試數是**收集數**，沒有實跑；實跑基線（pass／skip／fail）要在 M0 於共用環境逐一記錄。
- 追蹤檔小（總計 < 20 MB），體積問題幾乎全在**未追蹤**的資源：

| 資源 | 位置 | 大小 | git 狀態 |
|---|---|---|---|
| IndexTTS2 V25 權重 | `TestSeparateTTSSystem/echo_tts/modules/tts_module/checkpoints_v25/`（僅 `config.yaml` 追蹤） | 8,976 MB | 忽略 |
| TTS LoRA／角色特徵 | `echo_tts/models/{lora,tts}/`（`uep_v25.pt` 等） | 27 MB | 忽略（`/echo_tts/models/`） |
| VOX 參考音 | `echo_tts/assets/vox/*.wav`、`assets/reference/*.wav` | 約 4 MB | 追蹤（6 個 wav） |
| STT whisper | `TestSeparateSTTSystem/models/stt/whisper/` | 2,949 MB | 忽略 |
| Core 自己的 `models/` | `nlp/`、`stt/`（**空**）、`tts/`（RVC 與 `uep.pt`） | 1,546 MB | 忽略 |

- 子系統內不屬於套件但含程式的目錄：`TestSeperateMemorySystem/agent_memory_spike`（28 檔，pyproject 沒打包）、`examples/`、`scripts/`、一個自己的 `env/`；TTS 的 `experiments/`（29 檔）、`webapp/`、`benchmarks/`。這些建議不遷（Q9）。

### 1.2 Core 現在 import 了什麼 [讀碼]

| Core 檔 | import 的子系統內容 | 備註 |
|---|---|---|
| `uep/echo/host.py`、`outcomes.py`、`policy.py`、`sink.py` | `echo_stream.contracts.{cancellation,output,stages,types,turn}`、`echo_stream.core.{coordinator,tracer}` | 頂層 import；ES 必裝 |
| `uep/echo/inference_wiring.py` | 延遲 import：`echo_tts.runtime.{indextts_backend,indextts_generate}`、`echo_stream.adapters.{think_llm,speak_indextts,speak_vox,input_multichannel,input_stt}` | 另以 `sys.path.insert` 加入 `UEP_INFER_{TTS,STT,SESSION}_REPO` |
| `uep/echo/memory_wiring.py` | 延遲 import：`echo_memory.MemoryEngine`、`echo_stream.adapters.memory_echo`、`llm_integration.tools.{definitions,executor}` | |
| `uep/memory/`、其餘 | 無（只認 `uep/memory/protocol.py` 的 Protocol） | `tests/uep/test_import_boundary.py` 鎖「只有 `uep/echo/` 可以 import `echo_stream`／`echo_memory`」 |

Core 內 import 統計：`echo_stream` 20 行（`uep/`）＋37 行（`tests/`）；`echo_memory` 1 行；`echo_tts` 2 行；`llm_integration` 2 行。

`uep/config.py` 的路徑類設定：`UEP_INFER_{TTS,STT,SESSION}_REPO`（`inference_wiring._require_repo` 缺就拋錯，fail-closed），沒有 ES、記憶的 repo 路徑設定（記憶 repo 靠 editable）。

### 1.3 子系統彼此的依賴 [讀碼]

```
echo_stream ──(adapters only, lazy)──► echo_stt         (input_stt, input_multichannel, stt_discord)
            ├──────────────────────► echo_thought_core  (think_llm, session_store)
            ├──────────────────────► echo_memory        (memory_echo)
            ├──────────────────────► llm_integration    (memory_echo，2 處)
            └──────────────────────► echo_tts           (speak_indextts、speak_vox 以 importlib 載入 echo_tts.runtime.vox_backend)

llm_integration ─► echo_memory      mcp_server ─► echo_memory
echo_stt、echo_thought_core、echo_tts：互不依賴，也不依賴 echo_stream、echo_memory
```

- ES 的 `contracts/`、`core/` 不 import 任何子系統，只有 `adapters/` 可以（ES CLAUDE.md 的硬規則）；跨子系統 import 在 ES 內共 22 處，全在 adapters。
- 反向：只有 `TestSeperateMemorySystem/tests/m7/sim.py` 提到 `echo_stream`，其餘子系統不認識 ES。
- 因此依賴圖是**以 ES 為中心的星形**，四個葉節點互相獨立，遷移順序可以依葉節點先、ES 後排。

### 1.4 路徑與資源假設（sys.path／subsystem_path 機制與其他）[讀碼]

| 機制 | 位置 | 假設 | 搬進 Core 後的影響 |
|---|---|---|---|
| `subsystem_path(kind)` | ES `config.py:135` | 先看 `ECHO_STREAM_{KIND}_REPO`，否則猜 ES 所在目錄的**同層**資料夾（`_SIBLING_GUESSES`） | ES 搬進 Core 後同層猜測落到錯誤位置；`inference_wiring` 的註解已點出這點 |
| `ensure_importable(path)` | ES `config.py:164` | `sys.path.insert(0, repo)` | 與 venv 內的同名套件形成雙來源，誰先贏取決於 path 順序 |
| `UEP_INFER_*_REPO` | Core `uep/config.py`、`inference_wiring._ensure_importable` | 同上，由 Core 明確給 | 遷入後應刪除；否則舊 repo 路徑殘留會在新套件缺失時**靜默載入舊版** |
| `config.get("ECHO_STREAM_*")` | ES，44 處呼叫、58 個不同鍵 | 從專案根目錄 `.env` 讀 | ES 有自己的設定系統，與 `UepSettings` 重複（§5.3） |
| bot 的 `echo_stream_bridge.py` | bot | `sys.path.append`（刻意 append，不蓋掉 bot 自己的 `tests`／`config`） | 套件改由共用環境提供後可改為 no-op |
| `default_generation_paths(project_root)` | `echo_tts/runtime/indextts_generate.py:32` | `<root>/echo_tts/modules/tts_module/checkpoints_v25`、`<root>/echo_tts/models/tts/uep_v25.pt`；`root` 預設 `cwd` | 要改為由環境變數或 Core 設定給模型根目錄 |
| `sys.path.insert(0, engine_root)` | `echo_tts/runtime/indextts_backend.py:53`、`indextts_generate.py:95` | `engine_root = <root>/echo_tts`，讓 `modules.*`、`utils.*` 可被當頂層套件 import | **見 §2.3，核心阻礙** |
| STT 設定檔 | `echo_stt/core/config.py:17` | `<repo>/configs/stt_config.yaml`（套件上三層） | 若套件被搬成 `subsystems/echo_stt/echo_stt/…`，上三層是 `subsystems/echo_stt/`，nested 佈局下仍成立；拆平則斷 |
| Session `.env`、記憶 `.env` | `echo_thought_core/llm_client.py:46`、`echo_memory/config.py:24` | `Path(__file__).parent.parent / ".env"` | 同上，依賴「套件的上一層就是 repo 根」 |
| 記憶資料 | `echo_memory/config.py:59,310` | `MEMORY_STORAGE_PATH` 預設 `./data`、log `./data/echo_memory.log`（相對 cwd） | Host 以 `storage_path` 明確傳入時沒事；其餘路徑相對 cwd |
| `tests` 套件遮蔽 | Core commit 6424794 | 補 `tests/__init__.py` 避免被 site-packages 的 `tests` 遮蔽 | 子系統測試在 Core 內各自 `tests/`，不要同進程混跑（§7.3） |

### 1.5 共用環境與打包 [讀碼]

- 共用環境 `~/.uep/venv-inference-312`（Python 3.12），第三方由 Core `uv.lock` 管（`inference` extra＋預設 dev group，`uv sync --frozen --extra inference --inexact`）。
- 五個子系統以 `uv pip install --no-deps -e <repo>` editable 安裝；setuptools editable 會在各 repo 寫 `*.egg-info`（盤點時五個 repo 都有）。
- Core 目前 `[tool.hatch.build.targets.wheel] packages = ["uep"]`，`requires-python = ">=3.12,<3.13"`；子系統自己的 pyproject 是 `>=3.10`。
- 子系統自己的 entry points：`echo-stream`、`echo-mcp`、`echo-chat`、`echo-thought`（遷入後要決定是否進 Core 的 `[project.scripts]`）。
- Core 的 CI（`.github/workflows/ci.yml`）：Python 3.10、只跑 `unit_tests/` 與 legacy flake8；**從未跑過 `tests/uep`**，也沒有 3.12／uv 的 job。「子系統測試在 Core 內跑過」目前沒有 CI 落點，見 §7.4。

### 1.6 在途工作（影響凍結）[執行]

| Repo | 未合入 develop 的分支（領先數、最後更新） | 其他 |
|---|---|---|
| TTS | `feature/tts-model-eval`（**18**，11/01）、`feature/vox-emotion`（**5**，11/01） | 另有三個外部工作樹 `TTS-wt-{cancel,model-eval,vox-emotion}`；主樹有 1 個未提交檔 |
| ES | `feature/d85-member-recall`（3，10/30）；`feat/vox-adapter`（0，11/01，但有 agent 工作樹且其分支 `worktree-agent-a0b1…` 領先 2） | `.claude/worktrees/` 下 9 個 agent 工作樹；**盤點期間 ES `develop` 從 `6309b8a` 前進到 `96962f1`，表示仍有 agent 在提交** |
| EM | 無領先分支；`feature/memory-reliability` 有獨立工作樹 `TestSeperateMemorySystem-reliability` | origin 另有 `m14-dream-accumulation`、`s2-host-interfaces` 等遠端分支 |
| Session | 無領先分支；`SC-wt-usage` 工作樹（`feature/stream-usage-deps`） | |
| STT | 無 | 只有 develop／master |

重要副作用：因為現行 Core 以 editable 指向各 repo 的 develop 主工作樹，**Core 的 1,486 passed 其實會隨他人在子系統 repo 的提交而漂移**（[推論]，由 editable 機制推得）。這是遷入的正面理由之一：遷入後 Core 的測試結果只取決於 Core 自己的 commit。

---

## 2. 為什麼不能只看目錄搬遷：三個結構性發現

### 2.1 「子系統」在 Core 內不是平行的五個模組

Core 已經把 ES 當作 Host 內的管線元件（`EchoPipelineHost`，D70(1) in-process 專屬 event loop 執行緒），把 EM 當作 `MemoryHost` 持有的 engine，把 STT／TTS／Session 當作 ES stage 的後端。也就是：**ES 是 Core 的內部組件，四個葉節點是 ES 的零件**。遷入計畫必須反映這個層次，而不是五個 repo 各自貼進去。

### 2.2 重疊最大的不在目錄，在三個概念

1. 「誰是 turn 的執行者與權威」：ES `TurnCoordinator` ↔ `uep/turn` Orchestrator／TurnTicket。已由 D71 與 `uep/echo/host.py` 落地為「機制在 ES、權威在 Core」。**註**：DECISIONS D71 狀態仍寫「待裁決」，但 P3 方案 §5.1–5.3 與 `host.py` 顯示已實作，建議把 D71 補成已定案，避免後續引用誤判。
2. 「Session」一詞有三種意思：legacy GS／CS／WS 生命週期、`echo_thought_core` 的 LLM 對話上下文與 token 優化、`uep` 的 Activity／Segment／Task／Run。文件與程式命名在遷入後必須分開講（建議把 `echo_thought_core` 對外稱 Thought／Context Engine）。
3. 「設定、身分、addressing、做夢」各有兩份以上實作（§5.3 列表）。

### 2.3 echo_tts 的同名頂層套件問題（本案最具體的阻礙）[讀碼＋執行]

- `echo_tts` 內有 10 處（不含 `deprecated/` 與 `.ipynb_checkpoints/`）使用 `from modules.…`／`import modules…`（`s2mel/modules/commons.py` 4、`quantize.py`、`dac/utils/__init__.py`、`tts_utils/maskgct/…/commons.py` 2、`runtime/indextts_backend.py`、`runtime/indextts_generate.py`），另有 `from utils.debug_helper import …`（`lite_engine.py`、`emotion_mapper.py`，包在 try/except，失敗退回 print）。
- 它們能解析，是因為 `runtime/` 在載入時 `sys.path.insert(0, <TTS repo>/echo_tts)`，讓 `echo_tts/modules` 與 `echo_tts/utils` 變成**頂層**的 `modules`、`utils`。
- Core 的 legacy 根目錄恰好有 `modules/`（含 `modules/tts_module/s2mel`，echo_tts 就是從這裡抽出去的）與 `utils/`（含 `utils/debug_helper.py`）。bot 也有自己的 `utils/`，且 bot 的 `voice_output.py:581` 已留下「sys.path 最前面，裡面也有 utils 套件」的註解，表示這個遮蔽已經咬過一次。
- 後果（遷入後若不處理）：誰先進 `sys.modules` 誰贏；`utils.debug_helper` 的 try/except 會**成功**落到 legacy 的那份，靜默引入舊依賴；`from modules.commons …` 可能解析到 legacy 或 echo_tts，視載入順序而定。
- 修法與佈局無關：**在 TTS repo 遷入前**（或遷入時第一個 commit）把內部 import 改為 `echo_tts.modules.…` 絕對路徑，移除 `sys.path.insert(0, engine_root)`，`utils.debug_helper` 改為 `logging`。這是 M5 的前置條件，不是 VOX 調音的前置條件。
- 這也決定了命名比較（§4）：若改名放進 `uep.subsystems.echo_tts`，`tests/uep/test_import_boundary.py` 會直接紅（它掃的就是 `uep/` 底下 import 了什麼，echo_tts 內部 import 了 `utils`、`modules`）；若保留頂層名放 `subsystems/`，boundary test 掃不到它，但問題仍在。**兩者的真實代價不在 import 行數。**

---

## 3. 分層分析

六層由下而上：資料與記憶層、推論 stage 層、ES 管線層、Host 與 Session 層、工具層、平台接口層。每層寫現況、目標、難點與風險、對遷移順序的影響。

### 3.1 資料與記憶層

**現況**

| 構件 | 規模 | 說明 |
|---|---|---|
| `echo_memory` | 35,893 行、2,167 測試 | MemoryEngine、affect（salience、judge、subconscious）、dream（distillation）、sharing（場域共享、about-me）、namespace |
| `llm_integration` | 1,817 行 | `tools/definitions.py`（6 個 TOOL_DECLARATIONS）、`tools/executor.py`、LLM client、router |
| `mcp_server` | 1,365 行 | stdio MCP；11 個工具（`store_episode`、`retrieve_memories`、`search_concepts`、`get_memory_stats`、`get_profile`、`trigger_dream`、`list_episodes`、`store_observation`、`list_hypotheses`、`request_hypothesis_review`、`review_concept`）＋resources；`access_policy` 以 namespace id 的結構 fail-closed |
| Core `uep/memory/` | 2,921 行 | MemoryHost（持有 namespace→engine registry）、Access Layer（audience／ACL）、identity、dream_loop／dream_planner（Host 全域做夢）、`protocol.py`（Port） |
| ES `adapters/memory_echo.py` | 1,612 行（ES 最大的 adapter） | `EchoMemoryAdapter`：檢索、salience 評估、StorageJudge、工具 schema 與 `execute_tool`、about-me 三工具、dream 蒸餾 |
| ES `core/dream_scheduler.py` | 175 行 | 累積門檻式做夢排程（D75 決議退場） |
| bot | `services/echo_memory_*`、`echo_decision/voice_memory.py` | 每位使用者各開 engine（D76 階段 X 並存，Y 隨 P4 退場） |
| legacy `modules/mem_module` | 10,260 行 | 舊 FAISS 記憶系統，與 echo_memory 資料不相容 |

資料位置：EM 以 `storage_path`（Host 傳入）與 `./data`（相對 cwd）；bot 有自己的資料目錄；D76 的條件是「Host 與 bot 資料目錄不相交」。

**目標**

- `echo_memory` 成為 Core 內的函式庫，只由 `uep/echo/memory_wiring.py` 接觸（boundary test 不變）；`llm_integration` 的 `TOOL_DECLARATIONS`／`ToolExecutor` 降為 echo_memory 工具的**實作細節**，對 LLM 的曝光由 Core 註冊表決定（§3.5）。
- 資料根目錄由 `UEP_DATA_DIR` 一處指定，**遷移不搬資料**。
- 做夢排程單一擁有者在 Core Host（D75／D92–D100）；ES `DreamScheduler` 與 bot 排程隨之退場。

**難點與風險**

1. **雙寫風險不是搬遷造成的，但搬遷會讓它更容易發生。** `FileLease` 只擋做夢互斥，不擋 retrieve／store 併發（D76 原文）。搬遷期間若 bot 與 Core Host 指向同一資料目錄，會踩到。守則：搬遷不改任何資料路徑，D76 的不相交條件照舊。
2. **測試最多（2,167）但最單純**：純 CPU、無 GPU、deterministic（有 `live_api`、`slow` 標記排除），適合當 M2 的第一個大型驗證。
3. **`./data` 相對 cwd**：在 Core 內以不同 cwd 跑測試會寫到不同位置。M0 前置修正：預設改讀 `UEP_DATA_DIR`，沒設才退回 `./data`。
4. **`agent_memory_spike`（28 檔）、`examples/`、repo 自己的 `env/`、`probe_data/`**：不遷，打 tag 保存（Q9）。
5. **legacy `mem_module` 資料**：不在本計畫遷移範圍，但退場（R2）前必須有「舊記憶要不要匯入 echo_memory」的決定（Q10）。
6. 做夢參數 D92–D100 尚待裁決，與搬遷無技術相依，但搬遷後 `uep/memory/dream_loop` 與 `echo_memory` 內建排程在同一 repo，要避免兩邊各自改參數。

**順序影響**：EM 三包是葉節點、測試最多、無 GPU，建議**第二個遷**（Session 之後），用它驗證整個「搬遷＋測試佈局＋shared venv 切換」流程。

### 3.2 推論 stage 層（STT、TTS、Session／LLM）

**現況**

| Stage | 後端 | 組裝 | 現有保護 |
|---|---|---|---|
| Input | `echo_stt`（faster-whisper＋silero VAD＋speaker ID，multichannel／single 兩種模式；`channel/{local_mic,discord_channel,manager}`） | `inference_wiring`：`SttInputStage`／`MultiChannelInputStage` | `prepare` 逾時 10 秒、失敗降級（標 not ready，文字路徑仍可用） |
| Think | `echo_thought_core.backends`（openai／ollama／registry）＋ES `LLMThinkStage` | `default_think_backend` → `build_backend` | `prepare` 逾時 8 秒；not ready 時輪 FAILED，Host 照常 |
| Speak | `echo_tts`：IndexTTS2 V25（LoRA）與 VOX（voxcpm2）二選一，不可同時常駐 | `_build_indextts2_speak`／`_build_vox_speak` | prepare 逾時 IndexTTS 40 秒、VOX 240 秒；失敗無聲降級 |

- 全部 in-process（D70(1)），共用 GPU（STT／TTS／embedding 同卡），Windows 專屬 wheel（`flash-attn` 預編 wheel、`triton-windows`）。
- 設定面：`UEP_INFER_*` 約 20 個鍵（device、fp16、accel、warmup、lora_scale、speed、language、timeouts…）。

**目標**

- 三個後端成為 Core 內函式庫，但**重依賴留在 extra**：Core 本體（`uep`、`echo_stream` 契約）仍可在無 torch 的機器（P8 Mac mini 的 Host）安裝；`inference` extra 才裝 torch 家族。目前各 stage 的 import 都是延遲的，這個性質要靠測試鎖住（`import echo_stream.adapters.speak_vox` 在沒有 torch 時應只在 `prepare` 時失敗）。
- 路徑假設全部改為「環境變數＋預設」（§1.4 的 TTS、STT、Session、EM 項目）。
- 支援「stage 遠端化」的接口形狀不變（P8 後 STT／TTS 可能轉到別台機器，見 `MacMini_Host模型與推論部署評估.md`）：stage 的 `prepare／stream／aclose` 契約不因遷入改變。

**難點與風險**

1. **echo_tts 同名套件**（§2.3），M5 的前置條件。
2. **TTS 在途工作**（`tts-model-eval` 18 commit、`vox-emotion` 5 commit，11/01 仍在動）。TTS 是最後遷，但凍結點的訊號要早發（Q4）。
3. **9 GB 權重**：不複製。M5 以 `UEP_TTS_MODEL_ROOT`（建議名）指向現有目錄，並在 `default_generation_paths` 前置修正；驗證用 `validate_generation_paths` 的 FileNotFoundError 訊息確認指到正確位置。
4. **STT 設定路徑**依賴套件上三層是 repo 根；nested 佈局保住此假設，拆平就要先改（所以 §4 建議 nested）。
5. **Session 的 `--cov` addopts** 在共用環境失效：遷入時改 pyproject（移除 `--cov` 預設，改由命令列指定）屬必要修改，不是可選。
6. **pyproject 依賴聯集**：Core 的 `inference` extra 已涵蓋大部分（記憶端、fugashi、unidic-lite、pysbd 在 commit 6424794 補上）。`mcp[cli]` 已在 lock 中（`mcp 1.30.0`）；要驗證 `llmlingua`、`ollama`／`requests` 等 Session 選配是否需要進 lock（[推論]：預設路徑不需要）。
7. **GPU 測試非同質**：TTS 384 個測試在無 GPU 機器上會 skip 一部分；驗收基線要記「在目標機器上的 pass／skip 數」，不是收集數。

**順序影響**：Session（純 CPU、9.1k 行、327 測試）→ EM → STT（需 torch，2.9 GB 權重路徑）→ ES → TTS。

### 3.3 ES 管線層（ES 在 Core 內怎麼實現）

這一節回答 #203 的第一點：ES 不是外部函式庫照搬，要分析它與 Core 的重疊。

**ES 結構（17,075 行）**

| 區 | 主要檔（行數） | 職責 |
|---|---|---|
| `contracts/` | `output.py`（512）、`stages.py`（337）、`turn.py`（219）、`types.py`（196）、`cancellation.py`（175）、`style.py`（148） | Stage Protocol（Input／Think／Speak、AudioSource／AudioSink、MultiChannelInputStage、ThinkHostSurface）、TurnRequest／TurnTicket／TurnOutcome／PlayedEvidence、CancellationToken、TurnDetector／InterruptionDetector／AddressingClassifier 介面 |
| `core/` | `coordinator.py`（1,866）、`multichannel.py`（834）、`pipeline.py`（389）、`splitter.py`（375）、`tracer.py`（370）、`gap_filler.py`（264）、`channel_engine.py`（263）、`policies.py`（249）、`emotion.py`（222）、`channel.py`（215）、`dream_scheduler.py`（175）、`turn_detector.py`（141）、`vad.py`（135） | TurnCoordinator（排隊、讓位、regen、barge-in、結算）、PipelineRunner（單輪資料流）、SentenceSplitter、StreamChannel（有界通道＋push→pull 橋接）、LatencyTracer、多通道輸入引擎 |
| `adapters/` | `memory_echo`（1,612）、`think_llm`（1,318）、`speak_indextts`（863）、`stt_discord`（673）、`speak_vox`（473）、`input_stt`（435）、`input_multichannel`（156）、`discord_resample`（118）、`output_guard`（112）、`session_store`（48） | **唯一**允許 import 子系統的地方 |
| 其他 | `web/server.py`（2,138）、`cli.py`（716）、`fakes/`（~520）、`config.py`、`sources.py`、`sinks.py` | Web 前端 host、CLI、fake 三層、設定 |

**與 Core 的關係（現況）**：`uep/echo/` 2,952 行是轉接層：`host.py`（EsLoop 執行緒、CoreWorker 單一寫入緒、TurnTicket 簽發→TurnRequest、`cancel_turn` 原因碼）、`endpoint.py`（EchoEndpoint：Lease 持有者與 sink 第二接納）、`sink.py`（AdmittingSink 包 ES 的 AudioSink）、`policy.py`（CoreYieldPolicy、HostPolicySource）、`outcomes.py`、`tools.py`（CoreToolExecutor）、`memory_wiring.py`、`inference_wiring.py`。依賴方向 `uep → echo_stream`，ES 不 import `uep`（D3），boundary test 鎖「只有 `uep/echo/` 可以 import echo_stream」。

**概念對照與處置**

| ES 概念 | Core 對應 | 重疊程度 | 處置 |
|---|---|---|---|
| `PipelineRunner`（單輪 input→think→speak→sink 串接） | 無（提案 §6.2 禁止重複編排） | 無 | 留 ES，Core 不碰 |
| `TurnCoordinator`＋`TurnRequest／TurnTicket` | `uep/turn` Orchestrator 簽發 ticket、決策插槽（CC-7） | 已分工（D71） | 機制留 ES；ticket 驗證與政策快照是**穩定 ABI**，遷入後在 Core 補契約測試 |
| `TurnOutcome／OutcomeStatus／PlayedEvidence` | `uep/output` DeliveryEvidence；`task.*`／`turn.*` 事件 | 已有轉換（`uep/echo/outcomes.py`） | 契約留 ES，轉換留 `uep/echo` |
| `CancellationToken` | `uep/stop`（全域停止）、Presence revoke | 已橋接（`cancel_turn` 帶 Core 原因碼） | 不合併（ES `contracts/` 零依賴規則） |
| `AudioSink`、`AdmittingSink` | Presence Gate、Output Delivery | Gate 在 Core，sink 實體在平台 | 不合併；sink 實體歸平台 adapter（§3.6） |
| `TurnDetector／VAD／InterruptionDetector／AddressingClassifier` 介面（`contracts/turn.py`） | `uep/turn`（aggregator、engagement、wake、speech_filter、rule_proposer） | **高**：介面在 ES，實作散在 bot `echo_decision/addressing.py`、`engagement/`、`authority/`、`gate/` 與 `uep/turn` 兩處 | **合併為 Core 一處**（地方：`uep/turn`），ES 只留介面與 Smart Turn 偵測 |
| `DreamScheduler` | `uep/memory/dream_loop`（D75／D92–D100） | 完全重疊 | ES 版退場（D75 已決） |
| `config.py`（58 個 `ECHO_STREAM_*` 鍵、44 處呼叫） | `uep/config.py`（pydantic-settings，`UEP_*`） | **高** | 見下 |
| `adapters/memory_echo.py` 的 `member_recall`、about-me 工具 | `uep/memory/access.py` 的 `MEMBER_RECALL_SCHEMA` 與 `_execute_member_recall`（註解寫「與 ES 對齊（不 import）」） | **高（兩份實作）** | 跨 repo 時只能複製；同 repo 後合併為一份，歸 Core Access Layer（§3.5） |
| `adapters/session_store.py` + `echo_thought_core.session_store`（對話歷史持久化） | `uep/context`（ContextSnapshot、`history_owner`） | 中（D72(a) 例外：ES Think 自組上下文，Core 事後登記） | 遷移期不動；融合階段決定歷史單一擁有者 |
| `LatencyTracer` | `uep` metrics／trace 關聯 | 中 | 保留 ES tracer，關聯 id 打通 |
| `fakes/` | `uep/fakes/` | 低 | 各自保留；Core 新測試優先用 `uep/fakes` |
| `web/server.py`＋`cli.py`＋`sources.py`／`sinks.py` | 無（Website／Desktop adapter 的雛形） | — | 見 Q7；不應留在 `echo_stream` 套件的對外面 |

**哪些成為 Core 一等公民、哪些維持 adapter**

- **升格為 Core 一等公民**（實作歸 `uep/`，ES 退為呼叫者或移除）：
  1. 設定：單一 `UepSettings`，ES 的 `ECHO_STREAM_*` 保留為相容層（讀得到就用，順序在 Core 設定之後），不再新增鍵；
  2. 身分與 addressing：speaker→actor→owner 映射（D77）、是否被點名、權限（authority）、參與模式（engagement）；
  3. 做夢排程；
  4. 記憶工具定義與執行（Access Layer 為準，§3.5）。
- **維持 ES 內部**：PipelineRunner、SentenceSplitter、StreamChannel、LatencyTracer、TurnCoordinator 機制、多通道輸入引擎、fakes。
- **維持 adapter 層**（`echo_stream/adapters/`，仍是全 repo 唯一 import 子系統的地方，規則照舊）：`input_stt`、`input_multichannel`、`think_llm`、`speak_indextts`、`speak_vox`、`session_store`、`memory_echo` 的檢索／評估部分。遷入後移除其 `config.subsystem_path`／`ensure_importable` 的 sys.path 邏輯（改為 `importlib.util.find_spec` 回報套件位置，或直接移除）。
- **移出 `echo_stream` 對外面**：`web/`、`cli.py`（開發工具）；`stt_discord`、`discord_resample`（平台音訊轉接，歸 §3.6 的平台 adapter 家族；盤點時至少有四處與 Discord 音訊輸入相關：ES `adapters/stt_discord.py`、`echo_stt/channel/discord_channel.py`、bot `echo_decision/es_stt_input.py`、`echo_decision/discord_audio.py`，是否重複實作需要在融合階段逐一比對，本文不下結論）。

**事件驅動的關係**：ES 內部是純 asyncio、無事件匯流排；Core 是持久事件匯流排（journal／outbox）。兩者不合併：ES 的回呼只做記憶體計算與入佇列，所有 DB／Gateway 寫入由 `CoreWorker` 單緒執行（證據回報先於 `turn.completed`）。遷入 repo 不改變這個邊界，但**要把它從「跨 repo 的約定」變成「同 repo 的測試」**（見風險 R9）。

**難點與風險**

1. **邊界侵蝕**：同 repo 後，「順手在 ES 裡 import `uep`」或「順手在 `uep` 其他地方 import `echo_stream`」的成本接近零。需要兩條對稱的 boundary test：既有的「只有 `uep/echo` 可以 import `echo_stream`」，加上「`echo_stream`、`echo_*` 子系統不得 import `uep`」。
2. **ES 活躍分支多**（9 個 agent 工作樹、`d85-member-recall` 領先 3），ES 凍結期間 bot 整合會被擋住，所以 ES 排 M4，凍結窗口要短（見 §7）。
3. **ES 測試 999 個**中有 `test_web*.py`（約 56 個）綁 `web/`；若 Q7 決定移出 `web/`，這部分測試跟著走，不能計入 `echo_stream` 驗收基線。
4. **ES 的基準提交與 Core 測試的耦合**：`uep/echo/host.py` 註解寫「ES 基準為 develop e31eac0」，遷入後這種「依某個 ES 提交」的註解全部要改成「同 repo 的契約測試」。
5. **D83**（`VOICE_USE_ES_OUTPUT` 預設翻轉與舊路徑刪除）狀態仍是「待裁決」，影響 bot 何時能停用自己的輸出循環（§3.6）。

**順序影響**：ES 搬遷（M4）只是實體搬遷；融合動作（設定合併、addressing 合併、記憶工具去重）排在 M4 之後的獨立 PR（F 系列，§8.2）。

### 3.4 Host 與 Session 層

**現況**

| 構件 | 角色 |
|---|---|
| Core `CoreAssembly`（`uep/assembly.py`）＋ `EchoPipelineHost` ＋ `MemoryHost` ＋ Presence／Endpoint／Stop／Tasks／State | 新 Host；啟動順序已定（presence 先於 Delivery、endpoint socket 在 ES 之後），單進程 |
| `echo_thought_core`（9,130 行）：`session_store`、`persona`、`context_manager`／`echo_compressor`（壓縮）、`backends/*`（openai、ollama、registry）、`accounting/budget`、`llm_client`、`memory_bridge`、`prompt/` | 對話歷史、人設 prompt、上下文壓縮、LLM provider 抽象與用量 |
| `uep/context`（ContextSnapshot）、`uep/agent/core.py`（AgentCore）、`uep/adapters/runtime.py`（Runtime 協定） | Context Adapter 與 Agent Runtime 的契約；Model Gateway **尚未實作**（提案 §8.3） |
| legacy `core/sessions`（GS／CS／WS）、`controller.py`、`states/`、`working_context.py` | 舊生命週期，全記憶體、WS 無持久化、GS 判定分散三處（Core現況對照表 §1.3） |
| Core 常駐入口 | 盤點時 `uep/` 下只有 `endpoint`、`stop` 有 `__main__`，**未見組裝 CoreAssembly＋ES＋Memory 的常駐啟動入口**（[讀碼]，待確認）；舊的 `Entry.py` 仍是 legacy 入口 |

**目標**

- Host 擁有生命週期、持久狀態與授權；`echo_thought_core` 擁有「如何把歷史、人設、記憶組成送給 LLM 的內容、如何壓縮、如何呼叫 provider」。
- **`echo_thought_core.backends` 升格為 Model Gateway 實作**（Q6）：Core 目前直接依賴 `google-genai`、`openai`，ES 的 Think 經 Session registry 取 backend，Agent Runtime 之後也要打 LLM；若不收斂，會有 Core 自己的 client、Session 的 backend、Agent SDK 內建 client 三份 provider 抽象與三處 API key。
- 歷史所有權：現況 D72(a) 讓 ES Think 自組上下文（`history_owner=sdk_store`），Core 事後登記快照。融合階段決定是否收斂到「Core 先組 ContextSnapshot」(b)；遷入階段**不動**。
- Core 補一個正式的常駐入口（`python -m uep` 之類），由組裝層負責，不在子系統裡。

**難點與風險**

1. **Session 的 `--cov` 與 `[tool.setuptools] py-modules = ["Entry"]`**：Session 把頂層 `Entry.py` 當相容入口一起打包。nested 佈局下 `Entry` 模組名會落進 dev-mode 目錄，與 Core 根的 `Entry.py` 同名（兩個都是頂層模組 `Entry`）。M1 要把 Session 的 `Entry.py` 與 `py-modules` 移除或改名（它的相容目的已不存在，bot 與 Core 都不用）。
2. **用量與預算**：`accounting/budget` 的預算語意與 Core 的 Policy／預算（提案 §18）若分開實作，會出現兩套額度。融合階段決定。
3. **Session 的 `.env`／`llm_config.yaml`**：provider、model、API key 的設定來源與 `UepSettings.infer_llm_*` 重疊（`infer_llm_provider`、`infer_llm_model` 只是覆寫）。
4. **歷史持久化格式**：`session_store` 的資料檔格式在遷入時不改；bot 也使用（`services/session_service.py`），兩邊同時讀寫同一目錄的情境要在 B 系列（§3.6）前釐清。

**順序影響**：Session 是葉節點、最單純，作為 **M1**（最小風險先驗證流程）。Host 常駐入口與 Model Gateway 決定屬融合階段，不阻擋搬遷。

### 3.5 工具層（艾斯維爾認為最難）

#### 3.5.1 來源盤點

| # | 來源 | 工具（數量） | 定義位置 | 執行位置 | 權限模型 | 狀態 |
|---|---|---|---|---|---|---|
| T1 | Core Part S：`ToolSpec`／`StaticToolRegistry`／`ToolGateway`／`AgentCore` | 註冊表機制；無實際對 LLM 曝光的工具 | `uep/tools/registry.py`（40 行）、`gateway.py`（~70）、`uep/agent/core.py`（168）、`uep/adapters/tool.py` | Core 內 `ToolExecutor` 協定（`lookup_authoritative`、`execute`、`lookup_receipt`），由 `assemble_core(executors=…)` 靜態字串→executor 映射 | `side_effect_class`×`idempotency`×`presence_bound`×`tool_version`×核准分級；`TaskManager.create_action`／`execute_action` 四階段、params_hash、receipt | 契約定案（SYS契約草案、D53–D62、D91）；Gateway／Agent 為骨架，S3 工具呼叫流程尚未接上 ES |
| T2 | Core `uep/echo/tools.py` `CoreToolExecutor` | 3：`deep_recall`、`force_remember`、`member_recall` | schema 取自 echo_memory `TOOL_DECLARATIONS`（前兩個）與 `uep/memory/access.py` 的 `MEMBER_RECALL_SCHEMA`（第三個） | Core 行程內，經 Access Layer（阻塞呼叫丟 executor）；ES 的 `LLMThinkStage` 以 `tool_executor(name, args, context=tool_context)` 呼叫 | `TOOL_CLASSES` 白名單（`deep_recall`／`member_recall`＝`internal_read`，`force_remember`＝`reversible_local`）；身分與 audience 只讀 Core 每輪的 `tool_context`，LLM 引數不得指定範圍；缺漏 fail-closed；事後補記 `action.*` 事件（payload 無原文） | D79 過渡：**不走 ActionIntent**、無 receipt、無核准 |
| T3 | ES `adapters/memory_echo.py` | 6：上列 3 個＋`retract_about_me`、`opt_out_about_me`、`correct_about_me` | 該檔內的 schema 常數（`MEMBER_RECALL_SCHEMA`、`ABOUT_ME_SCHEMAS`）、`FORCE_REMEMBER_DESCRIPTION`；`think_llm.py` 內 `TOOLS_PROMPT`／`ABOUT_ME_RULE`（使用時機文字） | ES adapter（`EchoMemoryAdapter.execute_tool`），bot 與 ES Web 使用 | `enabled_tools` 清單；`member_resolver` 缺就不曝光 | **Core 的 `TOOL_CLASSES` 沒有 about-me 三工具**：Core 路徑下它們目前不可用，bot 路徑下可用 |
| T4 | EM `llm_integration/tools` | 6：`force_remember`、`deep_recall`、`adjust_importance`、`trigger_dream`、`list_hypotheses`、`request_hypothesis_review` | `definitions.py`、`executor.py` | 呼叫端行程內（ES adapter、bot、Core 都各自建 `ToolExecutor`） | 無獨立權限；依呼叫端 | Core 只用其中 2 個 |
| T5 | EM `mcp_server` | 11 個＋resources | `handlers.py` | **獨立行程**（stdio MCP，`echo-mcp`） | `NamespacePolicy`：以 namespace id 結構判斷（使用者 namespace 預設拒絕；`scope-g*` 寫入一律拒絕、讀取僅限開發者） | 面向開發者與外部 agent；含 `trigger_dream`、`store_episode`、`review_concept` 等不該對 LLM 曝光的高權限操作 |
| T6 | legacy `modules/sys_module` | 20 條 workflow（`drop_and_read`…`generate_prep_todos`）＋ MCP 工具定義＋ actions（`mischief`、`window_control`、`monitoring_*`） | `workflows/workflow_definitions.yaml`、`mcp_server/tool_definitions.py` | 進程內物件呼叫（`mcp_client.handle_request` 直呼）；工作流引擎、`workflow_executor`（單例、in-memory）、桌面端同進程 | `PermissionManager`（`PermissionType`：FILE_CREATE／MODIFY／DELETE、APP_LAUNCH、SYSTEM_COMMAND）；無副作用分類、無 receipt | SYS契約草案 §7.2 已有分類對照（FILE_DELETE→`high_impact` 等）；`ConditionalStep` 不承襲 |
| T7 | legacy `mem_module` 的 `register_memory_tools_to_mcp` | 舊記憶工具 | `mem_module.py:1991` | 同 T6 的進程內 server | 無 | 隨 mem_module 退場 |
| T8 | bot 本地 | 只有記憶工具（`_memory_tool_defs`：依 `memory.tools.enabled` 白名單＋hub 條件曝光 `member_recall`、about-me）；Discord slash 指令（音樂、代幣、擲骰…）**不是** LLM 工具 | `cogs/voice_ai.py`、`echo_decision/voice_memory.py`（`ABOUT_ME_TOOLS`、`MEMBER_RECALL_TOOL`） | bot 行程內（自建 `EchoMemoryAdapter`／`VoiceMemoryHub`） | 設定白名單；DM 不給 `member_recall` | D76 階段 Y 退場 |
| T9 | Part S S0 原生工具（規劃中） | Lore Vault、Chatroom | SYS契約草案 §5 | Core 內 executor（`origin=builtin`／`mcp_static`） | ToolSpec | 規劃 |
| T10 | 平台宣告工具（D91，規劃中） | Desktop 本機工具、Discord guild 工具 | 平台 `declare` 訊息（**候選**，分類權在 Core 核准清單） | **平台端**（遠端 executor，兩個固定鍵：權威／非權威） | `platform_approved`（核准清單項）＋可見性條件＋每訊框 MAC | 已裁決（D91，DQ-1～17），未實作 |

四種執行位置：**Core 行程內（T1、T2）**、**ES adapter／呼叫端行程內（T3、T4、T8）**、**獨立 MCP 行程（T5）**、**平台遠端（T10；legacy T6 本質上是「與桌面同進程」，遷移後屬此類）**。

#### 3.5.2 權限模型對照

| 模型 | 授權依據 | 粒度 | 缺口 |
|---|---|---|---|
| Part S（T1） | 工具分類（註冊表，Core 人工審核）＋ Task scope＋核准 | 每次 action | 實作未接到對話回合（S3 之前，回合內工具走 D79 白名單） |
| Access Layer（T2） | 每輪 `tool_context`（actor、audience、guild、在場名單）fail-closed | 每次呼叫 | 無核准、無 receipt、`correct_about_me` 這類不可逆操作沒有分類 |
| NamespacePolicy（T5） | namespace id 的字串結構 | 每次呼叫 | 只保護記憶命名空間；與 Core 的 scope 模型不同語言 |
| PermissionManager（T6） | 操作類型 | 每個 workflow 步驟 | 無 side-effect／idempotency／receipt，無 Presence 綁定 |
| bot 白名單（T8） | 設定檔＋hub 可用性 | 每個 session | 純設定，無稽核 |

#### 3.5.3 衝突點

| # | 衝突 | 說明 | 建議收斂 |
|---|---|---|---|
| C1 | **同一組記憶工具有三份 schema** | EM `TOOL_DECLARATIONS`、ES `MEMBER_RECALL_SCHEMA／ABOUT_ME_SCHEMAS`、Core `MEMBER_RECALL_SCHEMA`（註解自承「與 ES 對齊（不 import）」） | 單一來源＝Core 註冊表中的 `ToolSpec`（`params_schema`）；其他兩處降為實作細節，同 repo 後以測試鎖住一致 |
| C2 | **同一個 `member_recall` 有兩份實作** | ES `memory_echo` 與 Core `access.py::_execute_member_recall` | 保留 Core 的（audience／ACL 在這裡），ES 的版本在 Core 路徑下不再註冊；ES 獨立執行（Web、CLI）才用 |
| C3 | **about-me 三工具在 Core 沒有分類** | Core `TOOL_CLASSES` 只有 3 個；D91 DQ-16：`opt_out`／`retract` 暫定 `reversible_local`、`correct` 不可逆，分類待 G1 | 把這三個列入第一批 ToolSpec；`correct_about_me` 的分類需裁決（Q5） |
| C4 | **曝光與使用時機文字綁在一起** | ES `TOOLS_PROMPT`／`ABOUT_ME_RULE` 依「實際曝光的工具名」動態加規則（已有 `names` gating），但文字寫死在 `think_llm.py` | prompt 片段改為 ToolSpec 的 `usage_hint` 欄位或由 Context Adapter 組裝；SYS 增補「平台說明不進 LLM」要求遵守同一原則 |
| C5 | **執行權威與副作用記帳** | T2 事後補記 `action.*`，Part S 要求先 `create_action`（params_hash、核准、receipt） | 分兩步：先把三個 internal_read／reversible_local 工具包成 ActionIntent（`internal_read` 重跑無副作用，風險最低）；寫入類（`force_remember`）等 S3 |
| C6 | **MCP 行程 vs Core 行程** | T5 是獨立行程、獨立 namespace 政策；若直接當 `mcp_static` 註冊給 LLM，會繞過 Access Layer | T5 保持**開發者／外部 agent 專用**，不進 LLM 可見集合；需要的子集（`retrieve_memories` 等）另以 Core executor 實作 |
| C7 | **legacy 工作流是有狀態的多步流程** | 20 條 workflow、`WorkflowEngine`、`ConditionalStep` 競態債；與 Part S 的「Agent 步驟＋checkpoint＋`WAITING_INPUT`」模型不同構 | 逐條決定：註冊工具／Task 範本／捨棄（SYS契約草案 §7.2 已有表）；**最後遷** |
| C8 | **工具在哪台機器執行** | legacy `sys_module` 與桌面同進程；新 Core 常駐 Mac mini，桌面工具必須在使用者電腦執行（D91 平台遠端 executor，Part H） | 桌面類工具（檔案、視窗、應用）一律走 `platform_remote`；不在 Core 內保留任何「直接操作本機檔案系統」的 executor |
| C9 | **bot 本地工具的退場時機** | T8 與 D76 Y、D83 綁定 | 與記憶 Y 同時退場；之後 bot 只剩平台宣告工具（若有） |
| C10 | **Agent SDK 內建工具** | TC-1 禁止 hosted／built-in 工具 | 遷入不影響；列為驗收斷言 |

#### 3.5.4 收斂目標與順序

```
          ┌────────────────────────────────────────────┐
          │ Core 註冊表（ToolSpec，靜態、人工審核）       │  ← 唯一來源（T1）
          └──────────────┬─────────────────────────────┘
                         │ resolve / capability_set（可見集合）
          ┌──────────────▼─────────────┐
          │ ToolGateway + AgentCore +   │  schema/路徑/URL 驗證、核准、receipt
          │ TaskManager.execute_action  │
          └───┬──────────┬──────────┬───┘
   provider=core   provider=platform_remote   origin=mcp_static
   (記憶工具：     (Desktop／Discord／Website   (Lore Vault、Chatroom；
    Access Layer)   遠端 executor，D91)          T5 不在此集合)
```

| 步驟 | 內容 | 前置 |
|---|---|---|
| TL0（隨 M2 之後，設計為主） | 為 9 個記憶工具（deep_recall、force_remember、member_recall、retract／opt_out／correct_about_me、adjust_importance、list_hypotheses、request_hypothesis_review）寫 ToolSpec 草案與分類表（含 `trigger_dream` 為何**不**曝光） | M2 |
| TL1（M4 之後） | 去重 C1／C2：schema 單一來源、`member_recall` 實作歸 Access Layer；about-me 三工具進 `TOOL_CLASSES` | M4、Q5 |
| TL2 | 三個 `internal_read`／低風險工具改走 ActionIntent（保留 `action.*` 事件相容） | Part S S3 的對話回合流程 |
| TL3 | `force_remember`、about-me 寫入類改走完整四階段 | TL2、DQ-16 |
| TL4 | Desktop／Discord 平台宣告工具（D91 R0–R3）；legacy 桌面類 workflow 以 `platform_remote` 重寫 | Part H、P4、每訊框 MAC |
| TL5 | legacy 工作流逐條遷移或捨棄；`sys_module` 退場 | TL4、S5 |

**風險**：(1) 工具層是融合階段最大的設計面，而且與 Part S 實作進度（S2 註冊表之後的 Gateway／S3）綁定，搬遷計畫**不得把 TL2 之後列為 M 系列的完成條件**；(2) C1／C2 的去重要在 ES 與 Core 同 repo 之後才能做，所以 M4 是 TL1 的前置；(3) T5 若被誤當成 LLM 可見工具，會繞過 Access Layer，需要註冊表層級的斷言（`origin` 白名單）。

### 3.6 平台接口層

這一節回答 #203 的第二點：給三平台的統一接口，以及 bot 從「行程內使用 ES」改為「透過接口」。

#### 3.6.1 現況

| 平台 | 現有實作 | 規模 | 特徵 |
|---|---|---|---|
| Desktop | legacy `ui_module`（PyQt5）、`ani_module`、`mov_module`、`frontend_bridge`；新 Core 的 `CliEndpoint`（最小 CLI 端點＋watchdog，P2） | ui 20.9k、mov 7.8k、ani 0.9k 行 | Qt 主執行緒；Python 側沒有口型事件來源；ON_CALL 可繞過 Presence（提案 §20 要求改掉）；Desktop 工具在使用者電腦 |
| Discord | bot（獨立 repo）：`cogs`（9.5k 行）、`echo_decision`（13.5k，含決策、addressing、authority、engagement、gate、aggregator）、`presence`（1.3k，join／leave 決策）、`services`（3.6k，含 echo_llm、echo_memory 服務、session）；內有自己的 ES 掛載（`echo_stream_bridge`、`es_voice_output`、`es_stt_input`、`VOICE_USE_ES_OUTPUT` 旗標） | 約 3.6 萬行（不含 93 個測試檔、2.7 萬行） | py-cord＋DAVE；per-user 音訊串流；bot 自帶 turn 循環（舊）與 ES coordinator 路徑（新）並存 |
| Website | ES `web/server.py`（2,138 行）：WebSocket、整條管線 host，是原型 | 2.1k 行 | 多分頁規則（提案 §13.4）尚無；公開頁與互動端點需分開 |

Core 側已有：`uep/endpoint/`（`CoreApi` 協定、`RpcCoreApi`、HMAC hello、`reconnect`、`watchdog`、`service`）、`uep/presence/`（Lease／epoch／Gate）、`uep/control/`（控制通道、`socket_transport`，限 loopback）、`uep/output/`（`OutputIntent`：`text`、`audio_stream`、`typing`、`avatar_enter_leave`、`animation`、`desktop_ui_action`、`system_notice`；DeliveryEvidence）、`uep/echo/endpoint.py`（`EchoEndpoint`）。**尚未有**：平台 `hello/capabilities`（提案 §13.1，D91 §0.3 查證確認程式未實作）、平台輸入提交 API、音訊面協定、ConnectionDirectory／ConnectionTransport。

#### 3.6.2 目標：三平面接口

提案 §13.1 的邏輯方法清單（`hello/capabilities`、`prepare_presence`、`activate`、`quiesce`、`heartbeat`、`submit_input`、`deliver_output`、`report_delivery`、`request_transfer`、`disconnect`）是對的，這裡把它整理成三個平面，並標明哪些 Core 已有。

| 平面 | 載體 | 內容 | Core 已有？ |
|---|---|---|---|
| **控制面**（長連線、每訊框 MAC） | 現有 `CoreApi`／`socket_transport` 家族＋D91 Connection Registry | `hello`（platform_id、protocol_version、粗粒度 capability、nonce、ledger_id）、`declare`／`venues_update`／`roster_update`（D91）、`heartbeat`、Presence：`acquire／renew／release／confirm_quiesced`、全域停止、`revoke`、`goodbye` | Presence 與 hello(HMAC) 有；平台宣告、roster、Connection Registry 無；MAC 只有 loopback 信任（D91 RX-11 要求遠端前補） |
| **輸入面** | 控制面旁的獨立資料通道 | 文字：`{platform_id, venue, speaker_ref, text, msg_id}` → Gateway 認證後轉 `Utterance`／`input.utterance_final`。音訊：per-speaker PCM 幀（序號、時間戳、取樣率協商），語意同 ES `MultiChannelInputStage`（`feed／join／leave／discard`）；STT 放在邊緣或 Core（取決於網路與隱私，提案 §6.2） | Gateway 的 `input.utterance_final` 內部事件有；平台對外 API 無 |
| **輸出面** | 獨立資料通道＋控制面的 stop | Core→平台：`OutputIntent`（逐句，D73(A)）＋音訊幀（generation／sequence）；平台→Core：delivery evidence（`played_evidence`：`playback_confirmed／transport_sent／…`，D74 的 `spoken_rule=auto` 依此切換 PARTIAL／WHOLE_SENTENCE）。**stop／revoke 走控制面，不排在音訊資料後面**（提案 §6.4-4） | `OutputIntent`、DeliveryEvidence、`EchoEndpoint` 有；網路化的幀協定無 |
| Presence | 控制面 | 單一排他 Lease，Gate 在 Core，端點第二接納＋本地 watchdog | 有（P2） |
| 工具 | D91 專用工具通道（與 Presence 金鑰分開，PD-1） | `declare`→核准清單→`dispatch／ack／receipt／lookup／cancel` | 契約有，實作無 |

三平台差異只體現在：支援哪些 capability、`played_evidence` 強度、輸入來源、以及 Presence 取得方式。

| | 輸入 | 輸出 | 證據強度 | Presence | 工具 |
|---|---|---|---|---|---|
| Desktop | 本機麥克風（`echo_stt.channel.local_mic`）、文字、UI 事件（ON_CALL→到場請求，不得直接啟動） | 本地播放、動畫／`desktop_ui_action`／`avatar_enter_leave`（Qt 本地渲染，Core 只發語意指令） | `local_playback`（強） | Lease＋本地單調 watchdog（`CliEndpoint` 為原型） | 本機 executor（Part H） |
| Discord | per-user 48 kHz Opus→PCM、文字訊息、DM | 語音頻道音訊、文字、typing | `transport_sent`（弱，自動走整句制） | 加入／離開語音頻道＝acquire／release；bot 的 `presence/`（join／leave 決策）→ Presence Planner（P5） | guild venue 工具（`venue_policy=origin_venue`）、roster 更新 |
| Website | WebSocket PCM／文字 | 音訊幀到瀏覽器、文字 | `remote_render`（中） | 多分頁以 endpoint instance 競爭同一 Lease；主分頁關閉先撤銷再允許另一端 | 首版無 |

#### 3.6.3 bot 從行程內 ES 改為透過接口

現況 bot 是**自己組裝並持有** ES 元件（stage、coordinator、sink、memory hub、session），這正是 #221「bot 不得保留自己的 turn 執行循環」要消除的。建議分四階段，每階段 bot 都能運作、可回退：

| 階段 | bot 的樣子 | 傳輸 | 完成條件 | 回退 |
|---|---|---|---|---|
| B0（遷入期間） | 不變；`import echo_stream` 由共用環境內的 Core 提供，`echo_stream_bridge` 的 sys.path 邏輯改 no-op | 無（行程內函式庫） | bot 全套測試（`tests/`，93 檔）在新環境與搬遷前 pass 數一致；`echo_stream`、`echo_memory`、`echo_stt`、`echo_thought_core`、`llm_integration` 皆解析到 Core 內的路徑（檢查 `__file__`） | 重裝舊 editable |
| B1 | `VOICE_USE_ES_OUTPUT` 預設 True（D83），刪除 `VoiceTurn`／`_run_locked` 舊路徑 | 無 | D83 觀察 7 天無退化 | 旗標回滾 |
| B2 | bot 不再自建 ES stage／memory engine／session；改持有一個 `PlatformSession`（行程內物件，實作輸入／輸出／控制三面的 Python Protocol）連到同一行程內的 Core Host | **行程內**（呼叫 Protocol） | D76 Y 的退場條件 E1–E6；bot 不再 import `echo_memory`、`echo_thought_core`、`llm_integration` | 退回 B1 |
| B3 | bot 獨立行程，三面走 socket／WebSocket；每訊框 MAC | 網路 | D91 R3（MAC 與通道綁定）完成；P8 前 | 退回 B2 |

重點：**B2 與 B3 之間介面不變，只換傳輸**。這和 D70(1) 的「轉接層保持傳輸中立」一致。bot 的 `echo_decision`（決策、addressing、authority、engagement）在 B2 之前仍留在 bot；P3 方案 §4 的決策插槽把「該不該回、誰先、讓不讓位」放到 Core，因此 `echo_decision` 的大部分在 B2–B3 之間遷往 `uep/turn`（§3.3 的 addressing 合併），bot 只留 Discord 機制（指令、語音收發、成員 roster 提供、音樂、代幣）。

#### 3.6.4 難點與風險

1. **音訊面是新設計**：控制面與 Presence 大半已有，音訊幀協定（序號、抖動緩衝、取樣率協商、丟幀語意）沒有；先以行程內 Protocol 實作（B2），網路版延到 B3，不要在遷入期間設計。
2. **信任邊界**：`auth.py` 檔頭明載 hello 證明「抗冒名、不抗 relay」，回應不簽章；跨機器前需每則 MAC。B3 之前不得開網路面。
3. **Desktop 沒有新 Adapter 之前，legacy 桌面殼不能退**；P9（Avatar）才取代 ui／ani／mov。Desktop 在遷入期間只是「繼續跑 legacy」，不要為了好看提早拆。
4. **多份 Discord 音訊程式**（§3.3 末段列的四處）需要在融合階段盤清楚，否則 B2 會把重複實作一起搬進 Core。
5. **bot 與 Core 同機共用 GPU／同一份資料目錄**（D76 X）。
6. **ES Web 原型的處置**（Q7）：它是 Website Adapter 最接近的現成起點，但 2.1k 行混了 HTTP、WebSocket、管線 host、設定面板；直接當 Adapter 會把 ES 的自有 host 邏輯帶進 Core 的 Gateway。

---

## 4. 目標結構與命名

### 4.1 三個方案

| 方案 | 佈局 | 說明 |
|---|---|---|
| A（建議） | `subsystems/<name>/`（內含 `<pkg>/`、`tests/`、`pyproject.toml`…），**保留原頂層套件名** | repo 根目錄新增一個非套件目錄；hatch 的 dev-mode 目錄指向各 `subsystems/<name>` |
| B | 套件直接放 repo 根（`echo_stream/`、`echo_memory/` …）與 `uep/` 並列 | 與 legacy 的 `core/ modules/ utils/` 同層 |
| C | 改名為 `uep.subsystems.<name>`（或併入 `uep.echo.*`） | 全部 import 改寫 |

### 4.2 比較

| 面向 | A（保名＋`subsystems/`） | B（保名放根） | C（改名進 `uep`） |
|---|---|---|---|
| 子系統內部 import 改動 | **0**（echo_memory 955 行、ES 231、Session 203、STT 99、TTS 62，加上 `llm_integration` 27、`mcp_server` 17，共約 1,590 行不動） | 0 | 約 1,590 行機械改寫，**外加字串形式的引用**：`mock.patch("echo_…")` 字串、`importlib.import_module("echo_tts.runtime.vox_backend")`、logger 名稱、`package-data` 鍵、`python -m echo_stream`、文件與 README 範例 |
| Core 的改動 | 0（20＋37 行 ES import 不動） | 0 | 約 60 行；且 `tests/uep/test_import_boundary.py` 要重寫（它以 `uep/` 目錄為掃描範圍，子系統一進去，ES→其他子系統的 22 處跨 import 與 echo_tts 的 `modules`／`utils` 立即違規） |
| bot 相容性 | **完全相容**：bot 與 Core 共用同一個 venv，`import echo_stream` 自動解析到 Core 內；不需要 shim | 同 A | bot 約 110 行 import 必須同步改，且與 bot 的合併是鎖步；或要寫 meta-path 別名 shim（醜且易出錯） |
| 測試搬遷 | `subsystems/<name>/tests/` 保留自己的 `conftest.py`、`pyproject` pytest 設定，**各自獨立進程執行**，與 `tests/uep` 零交集 | 需把五套 `tests/` 改名分目錄，conftest／fixture 互相覆蓋風險（EM 有 `tests/baseline/fixtures/snapshot_v1/*.json` 必須同搬） | 同 B，且還要改 patch 字串 |
| 「相對 repo 根」的路徑假設（`configs/`、`.env`、`./data`） | **保住**（nested，套件的上一層仍是子系統根） | 破壞（變成 Core 根，與 Core 自己的 `configs/` 同名：`configs/config.yaml` 是 Core 的，`configs/stt_config.yaml` 會與之混在一起） | 破壞 |
| 同名套件遮蔽（`modules`、`utils`、`tests`、`configs`、`config`） | **前提**：子系統根目錄不進 sys.path，只暴露套件本身（hatch `dev-mode-exact` 或等效，M0 spike 必須證明）；若 editable 把 `subsystems/<name>/` 根放進 sys.path，子系統的 `tests/`、`configs/`、`Entry.py` 會變成頂層可 import，並遮蔽 Core 根的 `tests`（pytest 解析 `tests.uep` 會失敗），此時方案 A 的優勢不成立。`echo_tts` 的 `modules`／`utils` 問題另要靠 §2.3 的 import 修正解決 | 根上已有 legacy 的 `modules/`、`utils/`、`configs/`，風險最高 | 命名空間隔離最好，但 echo_tts 內部的絕對 `modules.*` import 照樣要改（改成 `uep.subsystems.echo_tts.modules.*`） |
| 對「ES 融入 Core」的語意 | 中性：搬遷與融合分開 | 中性 | 暗示「整個 ES 是 uep 的一部分」，與 D3（`echo_stream` 不 import `uep`）矛盾；且融合時未必整包都留 |
| 後悔成本 | 低：之後要改名成 C，是純機械重構，可獨立 PR | 低 | 高：一次改動大、回退難 |

### 4.3 建議與 ADR

#### ADR-M1：保留原套件名，放 `subsystems/<name>/`

- 背景：Core 與 bot 都已依賴這些名字；搬遷的目標是行為不變。
- 決策：方案 A。`uep/` 之外新增 `subsystems/`，各子系統保留其 repo 內部結構。
- 奈也觀點：bot 不用動、現有流程（`python -m echo_stream` 等）不用重學，這是讓大家願意跟著搬的關鍵；之後想改名，隨時可以。
- 奈留觀點：可以，但條件是同名套件問題（§2.3）與路徑假設要先修，不能用「放在 `subsystems/`」當作藉口把它帶進來；另外 boundary test 要補反向規則，否則同 repo 之後邊界會慢慢爛掉。
- 替代方案：B（根目錄污染且與 legacy 同名風險最大）、C（改動面最大，且與 D3 的語意矛盾）。
- 後果：Core repo 根目錄多一個 `subsystems/`；每個子系統保留一份自己的 `pyproject.toml`（只作 pytest 設定與依賴註記，不再用於建置）；Core 的 `pyproject.toml` 需讓 hatch 以 editable 只提供 7 個套件本身並在 wheel 中包含它們。**M0 空殼 spike 的通過條件**：安裝後 `sys.path` 不含任何 `subsystems/<name>/`；`import tests, configs` 仍解析到 Core 根；`import echo_stream` 等七個名稱解析到 `subsystems/<name>/<pkg>`；若 hatch 預設把套件父目錄寫進 `.pth`，改用 `dev-mode-exact = true` 或等效設定；做不到則退回 B 的變體（套件攤平到 `subsystems/` 單層，並先處理 `configs/`、`.env` 相對路徑假設）。

> 注：MIN 派遣範例寫 `uep/subsystems/<name>`；本文刻意不採用，理由見上表「Core 的改動」「對 ES 融入 Core 的語意」兩列。若艾斯維爾仍偏好 `uep/` 底下，應視為 C 的變體，需同時裁決 boundary test 的重寫與 echo_tts 的 import 改寫。

### 4.4 搬遷後 bot 怎麼取得這些套件

- bot 的 `bot.bat` 已固定使用 `%USERPROFILE%\.uep\venv-inference-312\Scripts\python.exe`，與 Core 同一個環境。遷入後該環境以 Core 的 hatch editable 提供 `echo_stream` 等名稱，bot **不需要任何改動即可運作**。
- bot 的 `echo_stream_bridge.py` 的 `sys.path.append(<ES repo>)`：遷入後應移除。若不移除，append 在 path 尾端，不會壓過 venv 內的 Core 版本，**但一旦 Core 版本因安裝問題缺失，會靜默退回舊 repo**。對策：遷移完成後把舊 repo 目錄改名（`_archived_…`）讓錯誤顯性化。
- bot 的 `.env` 內 `ECHO_STREAM_*_REPO` 變成無用鍵；ES 的 `subsystem_path()` 改為回傳套件位置，舊鍵保持「讀得到、被忽略並警告一次」。

---

## 5. 遷移順序與每步完成條件

### 5.1 原則

1. 葉節點先、ES 後、TTS 最後（理由見 §3）。
2. 每個子系統一個 `feature/migrate-<name>` 分支、一個 PR（squash 進 develop 的規則依專案）；PR 內**只有搬遷與必要路徑修正**，不含重構。
3. 每步的完成條件固定四項：(a) 該子系統**全套測試**在 Core 的共用環境內，用其原本的命令（見 §5.3 基線表）跑出與搬遷前相同的 pass／skip／fail 數；(b) `tests/uep` 仍為 1,490 收集、1,486 passed（或當時基線）；(c) bot 全套測試與搬遷前相同；(d) 解析檢查：`python -c "import X; print(X.__file__)"` 指向 Core 內路徑。
4. 每步回退＝在 Core 還原該 merge commit，並在共用環境重新 editable 安裝舊 repo（舊 repo 在 M6 之前保持原樣、不刪、不改）。

### 5.2 步驟

| 步驟 | 內容 | 前置修正（在來源 repo 以 `feature/prep-core-migration` 先合入其 develop，或遷入 PR 的第一個 commit） | 完成條件 | 驗證 | 回退 |
|---|---|---|---|---|---|
| **M0 準備**（不搬碼） | 決定基線分支（Q1）；建立 `subsystems/` 骨架與 pyproject spike（hatch editable 只暴露套件）；補 boundary 反向測試；記錄五個子系統目前的**實跑基線**（pass／skip／fail 與耗時，含機器標註）；訂定本地閘門腳本 `scripts/run_all_tests.ps1`（§7.4）；發出凍結預告 | 無 | spike 在一次性 venv 證明 ADR-M1 的通過條件（sys.path 不含 `subsystems/<name>/`、`tests`／`configs` 仍解析到 Core 根）；基線表填完 | 基線腳本連跑兩次結果一致（排除 flaky） | 刪 spike 分支 |
| **M1 Session**（`echo_thought_core`，9.1k 行／327 測試；**已完成 2026-11-02，見 §5.2.1**） | 以 subtree 導入；移除 `py-modules=["Entry"]` 與頂層 `Entry.py`（避免與 Core 根的 `Entry` 重名）；pyproject pytest 設定移除預設 `--cov` | `.env` 查找改為 `UEP_ENV_FILE`／cwd 優先，退回 `parent.parent`；`llm_config.yaml` 在套件內不需改 | `pytest -o addopts=""` 在 `subsystems/echo_thought_core` 得 327 收集、pass 數同基線；ES 的 `think_llm`（仍指舊路徑時）與新路徑兩種情況下 ES 測試不變 | 在共用環境 `pip uninstall echo-thought-core` 後 `import echo_thought_core` 指向 Core；Core `tests/uep/test_p3_s6_inference_wiring.py` 綠 | revert merge；`uv pip install --no-deps -e ../TestSeparateSessionControl` |
| **M2 EM 三包**（`echo_memory`＋`llm_integration`＋`mcp_server`，2,167 測試） | 以 subtree 導入整個 repo；`agent_memory_spike`、`examples`、`probe_*` 在導入後的第一個 commit 刪除（歷史仍在；`env/` 若被追蹤才刪，多半已 gitignore）；三包同一步（`llm_integration`、`mcp_server` 依賴 echo_memory） | `MEMORY_STORAGE_PATH`／log 預設改讀 `UEP_DATA_DIR`；`.env` 查找同 M1；`mcp_server/server.py:26` 的 `sys.path.insert` 移除 | 2,167 收集／12 deselect 不變；pass 數同基線；Core `tests/uep/test_p3_s6_memory_wiring.py`、`test_p3_s2_memory_host.py` 綠；資料目錄**未被觸碰**（比對 `UEP_DATA_DIR` 內容雜湊或 mtime） | `llm_integration`、`mcp_server` 的 import 可用；`echo-mcp` 入口決定去留（Q8） | revert；重裝 editable；資料無需回復 |
| **M3 STT**（`echo_stt`，271 測試） | subtree；`echo_stt/test_files/`（2.8 MB 音檔）移到 `tests/data/`（避免進 wheel） | `configs/stt_config.yaml` 路徑支援 `UEP_STT_CONFIG`；whisper 路徑支援 `UEP_MODELS_DIR`（預設仍讀既有 `models/stt/whisper`，用連結／環境變數指向現有 2.9 GB，不複製） | 271 收集、pass 同基線；`MultiChannelSTTEngine` 能以新路徑 `prepare`（單次，不跑 GPU 長測，只驗載入）；bot `cogs/stt.py` 的 `echo_stt` import 解析到 Core | `UEP_INFER_STT_REPO` 設定被忽略並警告一次 | revert；重裝 editable |
| **M4 ES**（`echo_stream`，999 測試） | 凍結窗口短（建議 ≤ 3 天）；subtree；`config.subsystem_path`／`ensure_importable` 改為 `find_spec`；`UEP_INFER_*_REPO` 對已遷子系統失效；`web/`、`cli.py` 依 Q7 處理 | `_project_root()` 與 `.env`／`echo_stream.toml` 查找改為可由 `UEP_ENV_FILE`、`UEP_ES_TOML` 指定；ES 對 TTS 仍用 `UEP_INFER_TTS_REPO`（TTS 未遷） | 999 收集（扣除 Q7 移走的 `test_web*`，另列基線）；`tests/uep` 全綠（含 `test_import_boundary`、`test_p3_s3_*`）；bot 測試同基線；`python -m echo_stream demo --fake`（零依賴，不啟動伺服器、不吃 GPU）可跑 | boundary 反向測試綠：`echo_stream` 不 import `uep` | revert；重裝 editable；bot 設回舊 `ECHO_STREAM_*_REPO` |
| **M5 TTS**（`echo_tts`，384 測試） | 先合 `tts-model-eval`、`vox-emotion`（見 Q4）；subtree；`experiments/`、`webapp/`、`benchmarks/`（若不需要）導入後刪除 | **§2.3 的 import 修正**（`echo_tts.modules.*`、拿掉 `sys.path.insert`、`utils.debug_helper`→`logging`）；`default_generation_paths` 接受 `UEP_TTS_MODEL_ROOT`；VOX 資產路徑已是套件相對，不動 | 384 收集、**在有 GPU 的目標機器**上 pass／skip 與基線一致；`validate_generation_paths` 通過（指到既有 9 GB 目錄）；`python -c "import modules"` **不得**解析到 echo_tts（驗證不再污染頂層）；`UEP_INFER_TTS_ENGINE=indextts2|vox` 兩種都 `prepare` 成功（人工，GPU，一次） | 以 `-X importtime`／`sys.modules` 檢查 `utils`、`modules` 未被 echo_tts 佔用 | revert；重裝 editable（舊 repo 仍有 9 GB 權重原位） |
| **M6 收尾** | 刪 `UEP_INFER_*_REPO`；`setup_infer_env.ps1` 移除五個 editable 與五個路徑參數（§7.3）；舊 repo 打 `migrated-to-core/<sha>` tag 並在 GitHub 設為 archived（**觀察期後**）；README／CLAUDE.md／DECISIONS 更新；清掉 `*.egg-info` | — | 全新機器／全新 venv 只靠 Core repo 與 `uv sync` 能還原環境；七個套件解析位置全在 Core；觀察期 7 天（沿用 D76 習慣）無回退 | `scripts/run_all_tests.ps1` 全綠 | 還原 setup 腳本；舊 repo 取消 archived |
| **F 系列（融合，M4 後獨立排程）** | F1 設定合併（ES `ECHO_STREAM_*` 相容層）；F2 member_recall／schema 去重（TL1）；F3 Addressing／authority 合併到 `uep/turn`；F4 DreamScheduler 退場；F5 Model Gateway（Q6）；F6 Core 常駐入口 | 各自另開 | 各自驗收 | | |

### 5.2.1 M1 Session 完成紀錄（2026-11-02，THEFANTASIAS，CPU-only）

**狀態：完成，未 push。** 分支 `feature/subsystem-migration`，三個簽章 commit：

| commit | 內容 |
|---|---|
| `842c6dc` | `git subtree add --prefix=subsystems/echo_thought_core ../TestSeparateSessionControl develop`（不 squash；來源 `8c0e950`）。subtree 以 commit-tree 產生 merge，不吃 `commit.gpgsign`，導入後以 `git commit --amend -S` 補簽章（本地、未推送，未動 parents／tree） |
| `01f90df` | hatch `packages` 加入 `subsystems/echo_thought_core/echo_thought_core` 與 `dev-mode-exact = true`；dependencies 補 `editables~=0.3` 與 Session 的 `python-dotenv`、`rich`、`tiktoken`（pydantic、pyyaml 已在 core）；`uv lock` 只新增 `editables 0.6`，既有版本無變動；`setup_infer_env.ps1` 移除 `-Session` 與舊 Session repo 的 `-e`、新增 `import editables` 檢查；Session 子系統移除 `Entry.py`／`py-modules`、pytest addopts 移除 `--cov`、`run.bat` 改 `-m echo_thought_core.cli` |
| `05aacf5` | `default_think_backend` 不再強制 `UEP_INFER_SESSION_REPO`（未設時傳 `None`，由 ES `ensure_subsystem` 走環境已裝的套件；設了仍須存在，保留為可選覆寫）；`run_all_tests.ps1` 新增 `$migrated` 清單，session 不論 Layout 一律走 `subsystems/echo_thought_core`；對應測試改寫並新增 1 個 |

環境切換（共用環境 `~/.uep/venv-inference-312`）：`uv pip uninstall echo-thought-core` → `uv pip install --no-deps -e .`（Core 以 hatch exact 重裝）→ `uv pip install --no-deps editables==0.6`。**重裝 Core 後、補 editables 前，環境內任何 python 啟動都會印 `.pth` 的 `ModuleNotFoundError: editables`**（與 spike 預測一致；setup 腳本已把檢查放在 `uv sync` 之後、其他安裝之前）。

證據：`echo_thought_core.__file__` 指向 `U.E.P-s-Core/subsystems/echo_thought_core/echo_thought_core/__init__.py`（從 `/tmp` 與 `Echo-Stream` 目錄啟動皆同）；`sys.path` 無含 `subsystems` 的項目；在 Core 根 `import tests` 指向 Core 的 `tests/`；`echo_stream`、`echo_memory`、`llm_integration`、`mcp_server`、`echo_stt`、`echo_tts` 仍各指向舊 repo。

驗收數字：

| 套件 | 結果 | 基線 | 備註 |
|---|---|---|---|
| Session（`subsystems/echo_thought_core`） | 294／18／0（15 deselect），3.9 s | 294／18／0 | 與基線一致 |
| Core `tests/uep` | 1,493 pass／0 skip／1 fail，615 s | 1,491～1,492 pass | 唯一失敗為已知的 `test_real_engine_smoke_through_host`；pass 多 1 是新增的 `test_think_backend_預設不需_session_路徑` |
| ES（`07e3ff8`） | 1,003／0／0，202 s | 1,001／0／0 | ES 於 M0 後有新 commit，收集數隨之增加；全過 |

行為變化須知：hatch `dev-mode-exact` 後，**Core 根目錄不再因 editable 而進入 `sys.path`**（原本會放）。從 Core 根以外的工作目錄 `import configs`、`import Entry` 不再成功。`uep/` 沒有這類 import，Core、ES、Session 測試皆不受影響；舊 `core/`、`modules/`、`Entry.py` 與 bot 若從別的目錄 import 這些名稱需注意，bot 的 `bot.bat` 路徑未在本步驗證（§5.3 bot 基線仍待補）。

### 5.3 驗收基線（M0 實跑結果）

實跑條件：機器 THEFANTASIAS（Windows 11）、2026-11-01 22:53–23:55、共用環境 `~/.uep/venv-inference-312`（Python 3.12.7）、`scripts/run_all_tests.ps1`（Sibling 佈局）。
**`CUDA_VISIBLE_DEVICES=""`：全程不佔用 GPU**，GPU 測試自動 skip；因此表內 pass／skip 是「CPU-only」基線，**不等於** M5 驗收所需的有 GPU 基線（TTS 的 GPU 項目仍須在目標機器另記）。
連跑兩次（第 1 次逐項、第 2 次整支腳本一次跑完）；兩次的 pass／skip 一致處標「一致」。

| 子系統 | 工作目錄（遷入後） | 命令（共用環境 Python；腳本額外加 `-p no:cacheprovider`） | 當時 commit（兄弟 repo） | 第 1 次 pass／skip／fail，耗時 | 第 2 次 pass／skip／fail，耗時 | 備註 |
|---|---|---|---|---|---|---|
| ES | `subsystems/echo_stream` | `pytest`（`PYTHONIOENCODING=utf-8`） | 第 1 次 `414fe54`；第 2 次 `cbbb5b8`（ES 於兩次之間有新 commit） | 997／0／1（+1 xfailed），199 s | 1001／0／0，201 s | 收集數隨 ES 變動（第 1 次 998 含 1 xfailed，第 2 次 1,001）；第 1 次失敗的 `tests/test_web.py::test_語音_start_會頂掉舊_session` 為本機 loopback 的 `ConnectionAbortedError [WinError 10053]`，第 2 次同類錯誤改出現在 Core 的 `test_cycle2_audit_b`，見下方 flaky 說明 |
| EM 三包 | `subsystems/echo_memory` | `pytest`（pyproject addopts 原樣） | `f0c62e9` | 2,166／1／0（12 deselect），465 s | 2,166／1／0（12 deselect），469 s | 一致；總收集 2,179，與 §1.1 的 2,167＋12 對得上 |
| STT | `subsystems/echo_stt` | `pytest` | `6d3ae9a` | 271／0／0，83 s | 271／0／0，48 s | 一致 |
| Session | `subsystems/echo_thought_core` | `pytest -o addopts= -m "not e2e"`（保留 e2e 排除、去掉 `--cov`） | `8c0e950` | 294／18／0（15 deselect），9 s | 294／18／0（15 deselect），9 s | 一致；294＋18＋15＝327 |
| TTS | `subsystems/echo_tts` | `pytest` | `100f214`（工作樹僅 `.claude/settings.local.json` 未提交） | 384／7／0，43 s | 384／7／0，47 s | 一致；GPU 被隱藏，7 個 skip 含 GPU 與 importorskip；pass 384＋skip 7＝391，比 §1.1 的收集數 384 多 7，原因未追查（可能是之後新增測試） |
| Core | repo 根（`feature/subsystem-migration`，`21f8660`） | `pytest tests/uep` | `21f8660` | 1,492／0／1，602 s | 1,491／0／2，610 s | 收集 1,493（§1.1 寫 1,490，已增）；**不穩定，見下** |
| bot | bot repo | `pytest tests/ -q` | `a215ee0` | 未測 | 未測 | M0 派遣範圍只含五個子系統與 Core；M1 前補 |

Core 的兩類失敗（非遷移造成，M1 起須當作「已知基線雜訊」處理）：

1. `tests/uep/test_p3_s6_memory_wiring.py::test_real_engine_smoke_through_host`：**兩次都失敗**（`store` 回 `timeout` 而非 `stored`）。該測試載入真實 echo_memory embedding；`CUDA_VISIBLE_DEVICES=""` 下改走 CPU，冷啟動超過 MemoryHost 逾時。[推論] 與隱藏 GPU 有關，未在有 GPU 條件下驗證（本次不得佔用 GPU）。M2 的驗收要在有 GPU 的條件下再確認一次。
2. `test_cycle2_audit_b.py::test_resolve_recovery_api_requires_token_and_records_operator`（第 2 次）與 ES `test_web`（第 1 次）：同一個 `ConnectionAbortedError [WinError 10053]`，本機 loopback HTTP 連線被中止，隨機出現、重跑通過，視為 flaky。
3. Core 整套約 10 分鐘，貼近 600 秒的單項上限；腳本因此給 Core 獨立的 `-CoreTimeoutSec`（預設 1800）。曾有一次 Core 在 `test_acceptance_v21` 附近停滯 50 分鐘以上（被我手動中止，單獨重跑該檔 9 秒完成），原因未查明，疑為共用 PostgreSQL 側的鎖或連線殘留；之後兩次完整跑都正常。

| 子系統（舊表保留） | 收集數 [執行] |
|---|---|
| ES | 999（M0 前）；現 1,001 |
| EM 三包 | 2,167（12 deselect） |
| STT | 271 |
| Session | 327 |
| TTS | 384 |
| Core | 1,490（現 1,493） |

#### M0 佈局 spike 結論（ADR-M1 通過條件）

**結論：可行。** 在一次性空殼專案（scratchpad，共用環境未動）上，Core `pyproject.toml` 以 hatch `dev-mode-exact = true` 並把 `packages` 列為「套件本身的路徑」，`uv pip install -e .` 後：

- `sys.path` 不含任何 `subsystems/<name>/`（`.pth` 只有一行 `import _editable_impl_uep`，由 `editables` 的 `RedirectingFinder` 逐個套件名映射到實際位置）。
- `import tests`、`import configs`、`import Entry` 仍解析到 Core 根；子系統內的同名 `tests/`、`configs/`、`Entry.py` 不外洩。
- `echo_stream`、`echo_memory`、`llm_integration`、`mcp_server`、`echo_stt`、`echo_thought_core`、`echo_tts` 全部解析到 `subsystems/<name>/<pkg>/__init__.py`；子模組（`echo_stream.sub`）也可 import。

M1 起 Core `pyproject.toml` 的對應設定（本步只記錄，未改）：

```toml
[tool.hatch.build.targets.wheel]
packages = [
    "uep",
    "subsystems/echo_stream/echo_stream",
    "subsystems/echo_memory/echo_memory",
    "subsystems/echo_memory/llm_integration",
    "subsystems/echo_memory/mcp_server",
    "subsystems/echo_stt/echo_stt",
    "subsystems/echo_thought_core/echo_thought_core",
    "subsystems/echo_tts/echo_tts",
]
dev-mode-exact = true
```

注意事項：

- `dev-mode-exact` 在執行期依賴 `editables`（hatch 已把 `editables~=0.3` 寫入 wheel 的 `Requires-Dist`），但 spike 中 `uv pip install -e .` **沒有自動裝進去**，導致 `.pth` 載入失敗（`ModuleNotFoundError: editables`）、套件不可 import。M1 須把 `editables` 加進 Core 的 dependencies／`uv.lock` 並確認 `uv sync` 會裝；setup 腳本也要驗證 `import editables`。
- 子系統的 `tests/` 在 pytest 內仍會因 rootdir／`tests/__init__.py` 把 `subsystems/<name>/` 在該行程內 prepend 到 `sys.path`（現在兄弟 repo 就是如此）；各自獨立行程跑即不互相影響，這是 §5.3 下方「獨立進程」的另一個理由。
- 以上僅驗證空殼；真實子系統內部的路徑假設（`.env`、`configs/`、`parent.parent`）仍須在各步驟處理。未測 hatch 預設（非 exact）模式的 `.pth` 內容，因 exact 已通過，不需要退回攤平佈局（B 變體）。

> 為什麼要各自獨立進程：五個子系統的 `tests/` 都叫 `tests`（且 Core 還有 `tests/__init__.py`），每個有自己的 `conftest.py`；同進程混跑會互相遮蔽或覆蓋 fixture。獨立進程也讓各自的 `addopts` 與標記保持原樣。

---

## 6. git 歷史處理

### 6.1 三種做法比較

| | git subtree（不 squash） | `git filter-repo` 搬目錄後 merge | 只複製快照＋記錄來源 commit |
|---|---|---|---|
| 歷史保留 | 完整（只含 develop） | 完整，且路徑已改寫到 `subsystems/<name>/…` | 無，來源 repo 保留為唯讀存檔 |
| commit 雜湊 | **保留**（DECISIONS 與文件常引用雜湊，例如 ES e31eac0、TTS cbcbc42） | 全部改變（需保存 commit-map） | 不適用 |
| GPG 簽章 | **保留**（專案規定 commit 簽章；盤點的近期 commit 皆為 `G`） | 重寫後原簽章失效（驗證狀態丟失） | 不適用 |
| `git log -- path`／blame | 搬入點之前看不到（路徑不同）；要看舊歷史得從合併前的 parent 追 | **正常** | 舊歷史在舊 repo |
| 工具 | git 內建 | 需另裝 `git-filter-repo`（目前環境 **未安裝**） | 無 |
| Core `.git` 增量 | 約 +57 MB（五個 `.git` 合計；TTS 18 MB 含 wav） | 略小（可排除 experiments 等路徑） | 幾乎 0 |
| 風險 | 合併提交含各 repo 全部檔案，需導入後的清理 commit | 重寫期間若漏濾路徑會洩出不要的檔；操作較重 | 失去 blame 與 bisect 跨界能力 |

### 6.2 建議

**ADR-M3：subtree 不 squash。**

- 奈也：雜湊與簽章保留，等於不需要跟任何人解釋「那個 commit 去哪了」；DECISIONS 與房內訊息的引用都還找得到。
- 奈留：同意，但要誠實記錄代價：導入前的歷史不能直接 `git log -- 新路徑` 追到。補救：每個子系統遷入時建立 tag `migrated-from/<repo>/<sha>`，並在子系統目錄放一份 `MIGRATION.md`（來源 repo、來源 branch、最後 commit、導入日期）。若艾斯維爾重視 blame，改走 filter-repo，條件是接受簽章與雜湊改變，並保存 commit-map 與 hash 對照表放入 `docs/`。
- 無論哪種：只導入 `develop`（不導入 `worktree-agent-*`、遠端 feature 殘枝；那些 0 領先的分支已合入）；未合入的在途分支在凍結前處理（§7.1）。

操作草案（M 系列各步，供實作者參考，本文不執行）：

```text
git remote add <name> <path-to-repo>; git fetch <name> develop
git subtree add --prefix=subsystems/<name> <name> develop   # 不加 --squash
git commit -S ...                                           # 清理 commit：刪不遷目錄、移除 Entry 等
git tag migrated-from/<repo>/<sha>
```

分支：全部 `feature/migrate-*` → develop。Q1 未決前，目標分支是 `feature/complete_revamp`（見下節）。

### 6.3 分支基線（Q1 的內容）

`feature/complete_revamp` 領先 `develop` 73 個 commit，`develop` 停在 v0.9.4（2025-12）。專案規定 `feature/* → develop`，但 `uep/` 只在 `complete_revamp` 上。三種做法：

| 做法 | 說明 | 評估 |
|---|---|---|
| a. 先把 `complete_revamp` 合入 `develop`（以 PR），遷移分支從 `develop` 長 | 流程合規 | develop 會從「v0.9.4 穩定」變成「新 Core＋legacy 並存」；legacy CI（3.10）仍在 develop 上，要先調整 CI |
| b. 明訂 `complete_revamp` 在遷移期暫代 develop，遷移分支從它長 | 最小改動 | 偏離專案規定，需要書面例外 |
| c. 從 develop 長，之後 rebase 到 complete_revamp | 不可行：拿不到 `uep/` |

建議 a，且在 M0 完成（理由：遷移本質上是 Core 的主線工作，不應長期掛在一條「非主線」上；CI 缺口也要同步補）。

---

## 7. 過渡期協作

### 7.1 凍結與在途工作收斂

凍結定義：某子系統 repo 的 `develop` 不再接受新功能 merge，只接受阻礙遷入的 hotfix，且 hotfix 要在遷入 PR 合併前以 cherry-pick 同步一次。

| Repo | 建議凍結時點 | 在途工作處理 |
|---|---|---|
| Session | M1 開始前 1 天 | 無領先分支；`SC-wt-usage` 工作樹的分支已 0 領先，直接移除工作樹 |
| EM | M2 前 2 天 | `feature/memory-reliability` 工作樹確認已合入再移除；origin 的 `m14-dream-accumulation` 等遠端分支確認已合入（它們出現在 origin 但本機無領先，視為已合或棄置，**由記憶線確認**） |
| STT | M3 前 1 天 | 無在途分支 |
| ES | M4 前 3 天，窗口 ≤ 3 天 | `feature/d85-member-recall`（領先 3）先合入；`feat/vox-adapter` 的 agent 工作樹（領先 2）確認是否為 `speak_vox` 最終版（Core 的 `inference_wiring` 已引用 `speak_vox`，所以應已在 develop；仍需核對 worktree 分支那兩個 commit）；9 個 `.claude/worktrees/agent-*` 逐一確認已合入後清除；盤點期間 develop 還在前進，凍結前後各查一次 `git rev-parse develop` |
| TTS | 見 Q4：合入 `tts-model-eval`（18）與 `vox-emotion`（5）後即凍；M5 與 VOX 調音互不依賴 | 外部工作樹 `TTS-wt-*` 三個；調音期間產生的 LoRA／權重不進 git，只更新 `UEP_TTS_MODEL_ROOT` 指向的目錄 |

收斂機制：每個凍結前 48 小時，在房內（聊天室）通知所有可能在這個 repo 工作的 agent：列出未合入分支與未提交變更；凍結後該 repo 的 `develop` 加保護（或口頭約定＋標題標「FROZEN — 遷入 Core 中」於 README 頂部）。**本文不替任何 agent 決定誰在進行什麼**，只提供清單與方法。

遷入後，在途分支若還有未合入內容：以 `git format-patch` 匯出，改用 `git am --directory=subsystems/<name>` 套到 Core 的新 feature 分支（路徑 prefix 對應 nested 佈局，因此是單純 prefix，不需改檔名）。

### 7.2 editable 安裝的切換

- 現況（commit 6424794）：`uv pip install --no-deps -e <repo>` ×5，各 repo 寫 `*.egg-info`。
- 切換：每步 M1–M5 各自 (1) 在共用環境 `uv pip uninstall <dist-name>`（dist 名：`echo-thought-core`、`echo-memory`、`echo-stt`、`echo-stream`、`echo-tts`），(2) 以 Core 的 hatch editable 提供新位置，(3) 檢查 `__file__`。
- **逐包切換的前提**：任何時間只有一個來源提供同一個名稱。editable 的 finder 與 `.pth` 目錄在 sys.path 上的先後不好預測，所以**不要讓新舊同時存在**；每步的 `uninstall` 與 `uv sync` 在同一個命令序列內完成。
- 舊 repo 內的 `*.egg-info` 在 M6 清掉（它們會被 pip 誤認為已安裝的 dist）。
- Core 的 hatch 以 editable 只暴露各套件（子系統根不進 sys.path，見 ADR-M1 的 spike 條件）；非 editable 的 wheel（打包 `build_app.py`／PyInstaller 對 legacy）不在本計畫。

### 7.3 共用環境 setup 腳本的調整（`scripts/setup_infer_env.ps1`）

| 項目 | 現況 | M6 後 |
|---|---|---|
| 參數 | `-Es -Memory -Tts -Stt -Session -EnvPath` | 只留 `-EnvPath` |
| 同步 | `uv sync --frozen --extra inference --inexact` | 不變 |
| 子系統安裝 | 迴圈 `uv pip install --no-deps -e <repo>` | 刪除（由 Core editable 提供） |
| 檢查 | `uv pip check`，排除 voxcpm 的訓練類依賴 | 保留；新增「七個套件的 `__file__` 皆在 Core 內」檢查，新增「`import utils, modules` 不得來自 echo_tts」檢查 |
| 副作用 | setuptools 在各 repo 寫 egg-info | 消失 |
| 過渡期 | — | M1–M5 期間腳本接受 `-Legacy <name>[,…]` 指出哪些子系統**仍**走舊 editable，讓每步可單獨回退 |

另：`uv.lock` 需檢查子系統 pyproject 依賴聯集是否已涵蓋（EM 的 `apscheduler`、`networkx`、`rich`、`tiktoken` 等已補；`mcp[cli]` 在 lock 內）；任何新增依賴走 Core 的 `uv lock`，不在子系統 pyproject 留第二份權威。

### 7.4 測試與 CI

| 問題 | 說明 | 處置 |
|---|---|---|
| Core CI 為 3.10 且只跑 legacy `unit_tests/` | 子系統測試、`tests/uep` 都沒有 CI 落點；shared venv 是 3.12＋CUDA wheel，GitHub 標準 runner 裝不起來 | M0 建「本地閘門」：`scripts/run_all_tests.ps1` 依序在各子系統目錄跑（§5.3 命令），彙整 pass／skip／fail 表；CI 另加 3.12＋CPU-only 的子集 job（`tests/uep`、ES、Session、EM 的 CPU 測試；需先確認 EM 的 embedding 測試是否可 fake）；GPU 部分維持手動 |
| 子系統 addopts 差異 | Session `--cov`、EM `--ignore`＋`-m`、ES `-q`＋asyncio auto | 保留各自 pyproject 的 pytest 設定，移除 Session 預設 `--cov` |
| 測試非同質 | GPU、live_api、slow、importorskip | 基線記 pass／skip，不記收集數 |
| 漂移 | editable 指向主工作樹，結果隨他人提交變動 | 遷入完成即解決 |

---

## 8. 舊 `modules/`、`core/`、`Entry.py` 退場

### 8.1 新舊對照（誰已被取代，誰還沒有）

| legacy | 規模（行） | 新 Core 對應 | 狀態 | 備註 |
|---|---|---|---|---|
| `stt_module` | 3,426 | `echo_stt`＋ES Input stage | **已取代**（M3／M4 後） | 需做功能對照清單：speaker 識別、喚醒詞等 legacy 專屬功能是否在 echo_stt／`uep/turn/wake` |
| `tts_module` | 48,461（含 deprecated 與引擎本體） | `echo_tts` | **已取代**（M5 後） | RVC 工具留在 Python 3.10 的 `env/`（pyproject 註記 A14） |
| `mem_module` | 10,260 | `echo_memory`＋`uep/memory` | **已取代**（功能），**資料未遷** | 舊記憶資料是否匯入需決定（Q10） |
| `llm_module` | 8,031 | `echo_thought_core`＋ES Think＋`uep/context`；MCP client→Part S Gateway | **大部分取代** | Gemini 上下文快取（`hidden_docs/上下文快取的實現.md`）為 provider 特定功能，Session 是否承接待確認 |
| `nlp_module` | 5,537 | **無完整對應**：`uep/turn`（aggregator、engagement、wake、rule_proposer）覆蓋一部分 | **未取代** | BIOS 意圖分段、身分管理、狀態決策權；提案 §20 定位是「transition proposal，非唯一狀態寫入者」，不要求為 SDK 刪除 NLU；需獨立決定去留（Q11） |
| `sys_module` | 18,293 | Part S（S5 遷移邊界已寫） | **未取代** | 20 條 workflow、PermissionManager、桌面動作；§3.5 TL5 |
| `ui_module` | 20,853 | 無（Desktop Adapter＋P9 Avatar） | **未取代** | 退場條件＝Desktop Adapter 上線 |
| `ani_module`／`mov_module` | 934／7,774 | 無（Desktop Adapter 本地渲染） | **未取代** | 同上；Python 側無口型事件來源 |
| `core/`（event_bus、controller、sessions、states、module_coordinator、router、system_loop、framework、registry、working_context、frontend_bridge…） | 17,100 | `uep/events`（event bus）、`uep/presence`＋`uep/state`（狀態）、`uep/turn`＋`uep/echo`（取代 coordinator／router／system_loop）、`uep/assembly`（取代 framework／registry）、`uep/context`（working_context） | **執行面已取代，但被 ui／ani／mov／sys／nlp 依賴** | `frontend_bridge` 仍把 legacy UI 接在 legacy event bus 上；`router.py`、`reload_coordinator.py`（死碼，無引用）可先清 |
| `utils/`（logger、debug_helper、prompt_builder、model_manager…） | 2,442 | `logging`／`uep`；`prompt_builder` 對應 Session persona／prompt | **部分** | 與 bot、echo_tts 同名風險（§2.3） |
| `devtools/` | 5,065 | — | 隨桌面保留 | animation_tester 等桌面工具 |
| `Entry.py`、`production_runner`、`system_initializer` | 219＋ | **無常駐入口**（F6） | **未取代** | 先有 F6 才能讓 `Entry.py` 退 |
| `unit_tests/`（362）、`integration_tests/`（44）、`additional_tests/` | 7,796＋5,860 | 隨各模組 | 隨模組退場 | |

### 8.2 分批退場

| 批 | 內容 | 條件 | 不做什麼 |
|---|---|---|---|
| R0（現在） | 標記 legacy 為 frozen（README／CLAUDE.md），legacy 測試分離（CI 另 job）；清明確死碼：`core/reload_coordinator.py`、未用的 build 產物（`dist/`、`build/`、`wheel/`、`clipboard_history.json`、`Animation Tester Program.exe`——**先確認是否被版控與是否仍需要**） | 不影響任何執行路徑 | 不動 `modules/*` |
| R1（M3＋M4 之後） | 退 `stt_module`、`llm_module` 的 MCP client 部分 | 新路徑功能對照清單全綠（speaker、喚醒詞、上下文快取確認去向）；沒有其他 legacy 檔 import 它們（grep＋`unit_tests` 重跑） | 不刪 `llm_module` 的 Gemini 專屬功能，除非 Q 確認 Session 承接 |
| R2（M5 之後＋Q10 決定） | 退 `tts_module`（保留 `deprecated_rvc` 與 `models/tts/` 資產說明）、`mem_module` | 資料匯入或明確放棄（Q10）；桌面殼（尚在 legacy）不再要求它們：桌面殼要嘛改走 Core 的 endpoint，要嘛隨 R4 一起 | |
| R3（Part S S5 之後） | 退 `sys_module`、`nlp_module`（依 Q11） | 20 條 workflow 逐條有去向；`PermissionType` 對照表落地；NLP 若保留則作為 `uep/turn` 的 proposer 來源 | |
| R4（Desktop Adapter／P9 之後） | 退 `ui_module`、`ani_module`、`mov_module`、`core/` 其餘、`devtools/`、`Entry.py`、legacy 測試與 CI | 桌面端能以新接口運作（Presence、輸出、動畫指令）；F6 常駐入口上線 | |

保存：legacy 保持原位（不搬 `legacy/`，因為其 import 皆相對 repo 根，搬了會斷；legacy 另有獨立 Python 3.10 的 `env/`），v0.9.4-stable 已有 tag 與 release，退場時各批打 tag `legacy/<batch>-removed`，需要時可從 tag 還原（Q8 要艾斯維爾確認這個保存方式）。

---

## 9. 風險與技術債

| # | 風險 | 嚴重度 | 緩解 |
|---|---|---|---|
| R1 | `echo_tts` 頂層 `modules`／`utils` 遮蔽（§2.3） | 高 | M5 前置 import 修正＋驗證項；不修就不遷 |
| R2 | 相對 repo 根的路徑假設（STT config、Session／EM `.env`、`./data`、TTS `project_root`） | 高 | nested 佈局保住假設；同時加環境變數覆寫；拆平作為後續選項 |
| R3 | 分支基線與流程不合（Q1） | 高 | 先裁決，M0 內完成 |
| R4 | CI 缺口；測試命令與標記各異；GPU 測試非同質 | 高 | 本地閘門腳本、基線記 pass／skip、CPU 子集 CI |
| R5 | Core 目前的綠燈依賴子系統主工作樹，會漂移 | 中 | 遷入即解；遷入前基線在同一時點記錄 |
| R6 | 凍結窗口內 agent 仍在提交（ES develop 在盤點期間前進） | 中 | 凍結預告＋ 凍結前後各驗一次 HEAD；format-patch／am 補件 |
| R7 | 簽章：filter-repo 會使歷史簽章失效 | 中 | 預設 subtree |
| R8 | bot 與 Core 並行使用同名套件：舊 repo 殘留 path 靜默載入舊版 | 中 | 移除 `UEP_INFER_*_REPO` 與 bot 的 sys.path.append；舊目錄改名 |
| R9 | 同 repo 後邊界侵蝕（ES↔uep、子系統↔uep） | 中 | 對稱 boundary test；ES 的 `contracts/`、`core/` 不 import 任何子系統的規則保留 |
| R10 | 資料雙寫（bot 與 Core Host 同資料目錄） | 高（若發生） | 搬遷不改資料路徑；D76 X／Y 條件不變 |
| R11 | Windows 專屬 wheel（`flash-attn`、`triton-windows`）與 P8 Mac mini | 中 | inference extra 與 core 分離；import 保持延遲；以測試鎖 |
| R12 | 工具層三份 schema、two member_recall、about-me 缺口（§3.5.3） | 中 | TL0–TL1；不在 M 系列內處理 |
| R13 | Core 無常駐入口；`Entry.py` 退場前沒有替代 | 中 | F6 |
| R14 | hatch editable 是否能只暴露套件、不把子系統根（含 `tests/`、`configs/`、`Entry.py`）放進 sys.path——方案 A 的前提，未驗證 | 高 | M0 spike 通過條件見 ADR-M1；失敗則改攤平佈局 |
| R15 | `tests/__init__.py`（Core 為了避免被 site-packages 的 `tests` 遮蔽而加）的歷史成因：舊子系統 wheel 把 `tests` 打進 site-packages | 低 | 遷入後不再安裝獨立 wheel，原因自然消失；仍保留該檔 |
| R16 | Session 的 `Entry` 模組與 Core 根的 `Entry.py` 重名 | 低 | M1 移除 |
| R17 | D76 觀察期（7 天）與遷移時程相撞 | 低 | 排程錯開 B2 |

技術債標記：ES `web/server.py` 2,138 行單檔；`memory_echo.py` 1,612 行單檔；`echo_tts/modules/tts_module` 的內嵌引擎含 `.ipynb_checkpoints`、`deprecated/`；EM 與 TTS 各自的 `env/`、`experiments/`。

---

## 10. 需要艾斯維爾裁決（附建議）

| # | 問題 | 選項 | 建議 | 最晚定案 |
|---|---|---|---|---|
| **Q1** | 遷移基線分支 | (a) 先把 `complete_revamp` 合入 `develop`；(b) 暫代 develop；(c) 從 develop 長（不可行） | **(a)**，M0 內完成；同時補 CI | M0 |
| **Q2** | 佈局與命名 | (A) 保留原名＋`subsystems/<name>/`；(B) 保留原名放 repo 根；(C) 改名 `uep.subsystems.*` | **(A)**；C 作為日後可獨立執行的機械重構 | M0 |
| **Q3** | git 歷史 | (a) subtree 不 squash；(b) filter-repo；(c) 只複製快照＋記來源 commit | **(a)**（保雜湊與簽章）；若重視 blame 才選 (b)，並接受簽章失效 | M1 前 |
| **Q4** | TTS 凍結點與 VOX 調音的關係 | (a) 合入 `tts-model-eval`、`vox-emotion` 後即凍並遷，調音在 Core 內繼續；(b) 等調音完成再遷；(c) 雙向同步窗口 | **(a)**：不等 VOX，但先合兩條在途分支；權重不進 git | M5 前 2 週發預告 |
| **Q5** | 工具收斂的取捨：`member_recall`／schema 單一來源歸 Core；`correct_about_me` 分類（不可逆？是否核准） | (a) ToolSpec 單一來源，ES adapter 降為實作細節；(b) 維持兩份靠測試對齊 | **(a)**；`correct_about_me` 暫定 `external_effect`＋需核准，與 DQ-16 併案 | TL1 前 |
| **Q6** | `echo_thought_core.backends` 是否作為 Model Gateway 實作 | (a) 是；(b) Core 另建 | **(a)** | F5 前 |
| **Q7** | ES `web/server.py`、`cli.py` 去留 | (a) 移出 `echo_stream` 套件，成為 Website Adapter 起點與開發工具；(b) 原樣保留；(c) 刪除 | **(a)**，但 M4 先原樣搬、F 系列再移（避免 M4 範圍膨脹） | M4 |
| **Q8** | `echo-mcp`、`echo-chat` 等 entry point 去留；legacy 保存形式（原位＋tag） | | `mcp_server` 保留為開發者工具、不進 LLM 可見集合；`echo-chat`／`echo-thought` 若無人用則不進 Core 的 scripts；legacy 原位＋每批 tag | M2／R0 |
| **Q9** | 不遷移清單：`agent_memory_spike`、EM `examples/`、`probe_*`、`env/`；TTS `experiments/`、`webapp/`、`benchmarks/`；STT `outputs/`、`memory/` | | 全部不遷，舊 repo 打 tag 保存 | M0 |
| **Q10** | legacy `mem_module` 既有記憶資料是否匯入 echo_memory | (a) 匯入；(b) 放棄；(c) 保留唯讀備份 | **(c)**，之後視需要再寫匯入工具 | R2 前 |
| **Q11** | `nlp_module` 去留 | (a) 保留作為 `uep/turn` 的 proposer 來源；(b) 以規則／LLM 取代後刪除；(c) 隨 legacy 全退 | 先 (a) 保持現況，R3 前做功能對照再決定 | R3 前 |
| **Q12** | CI 策略 | (a) 本地閘門腳本＋CPU 子集 CI；(b) self-hosted GPU runner；(c) 只本地 | **(a)** | M0 |
| **Q13** | 設定單一來源：ES 58 個 `ECHO_STREAM_*` 鍵的處置 | (a) 相容層保留、不再新增、逐步映射到 `UepSettings`；(b) 一次性改寫 | **(a)** | F1 前 |
| **Q14** | 平台接口首版傳輸（B2 vs B3 順序）與音訊 STT 位置 | (a) 先行程內 Protocol（B2），網路版在 D91 R3 之後（B3）；STT 位置先放 Core 側（平台上傳 PCM）；(b) 直接做網路版 | **(a)** | B1 前 |
| **Q15** | DECISIONS D71 狀態（已實作但標「待裁決」）、D83（`VOICE_USE_ES_OUTPUT` 翻轉）仍待裁決 | | D71 補記為已定案；D83 在 B1 前裁決 | M4 前 |

---

## 11. 附錄

### 11.1 每步必改的程式點清單（本文不修改，列給實作者）

| 位置 | 內容 | 步驟 |
|---|---|---|
| `Core/pyproject.toml` | hatch editable 只暴露七個套件（`dev-mode-exact` 或等效）、wheel `packages` 帶前綴；`testpaths` 保持 `tests/uep`；子系統測試獨立執行 | M0 |
| `Core/tests/uep/test_import_boundary.py` | 新增反向規則：`subsystems/` 內不得 import `uep`；ES 的 `contracts/`、`core/` 不得 import 子系統 | M0 |
| `Core/uep/config.py`、`uep/echo/inference_wiring.py`（`_require_repo`、`_ensure_importable`） | 移除 `UEP_INFER_{TTS,STT,SESSION}_REPO` 與 sys.path 插入 | M6（逐步容忍：對已遷子系統忽略並警告） |
| `Core/scripts/setup_infer_env.ps1` | §7.3 | M6（M1–M5 加 `-Legacy`） |
| ES `config.py`（`subsystem_path`、`ensure_importable`、`_project_root`） | 改 `find_spec`；`.env`／toml 查找可覆寫 | M4 |
| ES `cli.py`、`web/server.py` 中的 `ECHO_STREAM_*_REPO` 提示訊息與 `ensure_importable(subsystem_path("TTS"))` | 同上 | M4／M5 |
| bot `echo_decision/echo_stream_bridge.py`、`cogs/stt.py`、`cogs/voice_ai.py` | sys.path.append 與 repo 路徑解析移除 | B0 |
| echo_tts：`runtime/indextts_backend.py:53`、`runtime/indextts_generate.py:95`、`s2mel/modules/commons.py`、`quantize.py`、`dac/utils/__init__.py`、`tts_utils/maskgct/…/commons.py`、`lite_engine.py`、`emotion_mapper.py` | §2.3 | M5 前置 |
| echo_tts `default_generation_paths` | `UEP_TTS_MODEL_ROOT` | M5 |
| echo_stt `core/config.py`、`core/engine.py:97` | `UEP_STT_CONFIG`、`UEP_MODELS_DIR` | M3 |
| echo_thought_core `llm_client.py:46`、`persona.py`；`pyproject.toml`（`py-modules`、`addopts`） | `.env` 查找、移除 `Entry` 與 `--cov` | M1 |
| echo_memory `config.py:24,59,310`；`mcp_server/server.py:26` | `UEP_DATA_DIR`、`.env` 查找、移除 sys.path 插入 | M2 |

### 11.2 建議新增的環境變數（命名為建議）

| 變數 | 用途 | 預設 |
|---|---|---|
| `UEP_DATA_DIR` | 記憶與執行期資料根目錄 | 子系統自己的 `./data`（相容） |
| `UEP_MODELS_DIR` | 模型根（STT whisper、TTS 角色特徵等） | Core 的 `models/` 或既有位置 |
| `UEP_TTS_MODEL_ROOT` | IndexTTS2 V25 權重與設定所在 | 既有 `…/checkpoints_v25` 位置 |
| `UEP_STT_CONFIG` | STT 設定檔 | `subsystems/echo_stt/configs/stt_config.yaml` |
| `UEP_ENV_FILE` | `.env` 單一來源 | cwd 的 `.env`，退回舊位置 |

### 11.3 盤點方法與限制

- 只讀：檔案列舉、`grep`、`git log／branch／worktree list`、`pytest --collect-only`（未跑任何測試主體、未啟動應用或 dev server、未載入 GPU 模型）。
- 未驗證項：各子系統在共用環境的實跑 pass 數；hatch editable 只暴露套件而不暴露子系統根的行為；四處 Discord 音訊程式是否重複；Gemini 上下文快取是否由 Session 承接；EM embedding 測試是否可在無 GPU 的 CI 跑。
- 盤點期間觀察到 ES `develop` 由 `6309b8a` 前進到 `96962f1`，TTS 主樹有 1 個未提交檔，說明子系統仍有他人在動；實際數字以 M0 當日為準。
- 參考文件：`docs/架構重構/{P3接入方案,Core現況對照表,SYS契約草案,SYS契約增補_平台宣告與遠端執行,MacMini_Host模型與推論部署評估}.md`、`hidden_docs/DECISIONS.md`（D3、D70–D91）、`hidden_docs/TASKS.md`、`hidden_docs/架構重構/UEP_新架構研究/UEP_新架構技術設計提案_2026-10-25.md`（§5、§6、§13、§20、§21）、各子系統 `pyproject.toml` 與 ES `CLAUDE.md`。
