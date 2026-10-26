# Core 現況對照表（T-01）

對應提案：`UEP_新架構研究/UEP_新架構技術設計提案_2026-10-25.md` 第 23 章（V01–V04）與 §24.1 第 1 項。

## 0. 快照與方法

- 分支 `feature/complete_revamp`，HEAD `ece16cf`（2026-06-11）。
- 工作目錄 dirty state（唯讀盤點，未動）：`M .gitignore`；未追蹤 `devtools/generate_rvc_voice.py`、`docs/架構重構/UEP_新架構研究/`、`openspec/`。盤點途中另出現未追蹤的 `pyproject.toml`、`uv.lock`，非本任務產生。
- 證據類型：
  - **[讀碼]**：只靠閱讀與 grep，未執行，視為推論。
  - **[執行]**：實際跑過的指令，結果列於下方。
- 執行證據（皆不啟動應用程式）：
  1. `env/Scripts/python.exe -m pytest unit_tests/ --collect-only -q`：362 tests collected，無 collection error（僅 `PytestUnknownMarkWarning`）。
  2. `pytest integration_tests/ --collect-only -q`：44 tests collected。
  3. `pytest unit_tests/test_event_bus.py`：21 passed。
  4. `pytest unit_tests/test_session_lifecycle.py`：21 passed、4 skipped。
  5. `importlib.machinery.PathFinder.find_spec('workflows', ['modules/sys_module'])`（只解析路徑、不執行模組）：解析到 `modules/sys_module/workflows/__init__.py`，即套件勝過同名 `workflows.py`。
- 以上測試只證明單元層行為與可收集性，**不等於**端到端管線可運作。端到端（STT→NLP→LLM→TTS）沒有任何執行證據。
- 設計主張來源縮寫：SDD = `docs/SDD/0x_*.md`；CLAUDE = 專案 `CLAUDE.md`。

---

## 1. 現況對照表

### 1.1 啟動路徑

| 項目 | 本地設計主張 | 實際入口與呼叫點 | 證據 | 缺口 |
|---|---|---|---|---|
| 入口分流 | CLAUDE：`python Entry.py` 為正式模式，`--debug`／`--debug-gui` 為除錯 | `Entry.py:39` `debug_mode = config["debug"]["enabled"]`；`configs/config.yaml:34` 為 `true`。`Entry.py:193` `if debug_mode` → `debug_interactive()`（:201）；否則 `run_production_mode()`（:210）。`--debug-gui` 在 :127–185；沒有 `--engine-only` 參數（argparse 只有 :98–102 五個） | 讀碼 | **預設 `python Entry.py` 走 debug CLI，不走 SystemInitializer**；要走正式流程需 `--production` 或把 `debug.enabled` 改 false |
| debug CLI 路徑 | 無明文 | `devtools/debugger.py` → `devtools/debug_api.py`；`debug_api.safe_get_module`（:12–14）檢查 `modules_enabled`，通過才 `core.registry.get_module`（:20）。devtools 內無 `event_bus`、`system_loop`、`SystemInitializer` 引用（grep） | 讀碼 | 此路徑不經 Controller／Framework／Coordinator；`event_bus.start()` 只有 `system_loop.py:150` 與 `Entry.py:153`（debug-gui）會呼叫，**debug CLI 下 async 事件不會被分發**（推論，未啟動驗證） |
| 正式路徑 | SDD 02 §2.9：7 階段（Event Bus/State → Framework → Router → Session/Controller → WorkingContext → 模組 → 健康檢查） | `core/production_runner.py:428` `run_production_mode` → `ProductionRunner.run`（:37）→ `_initialize_system`（:80；:89–93 建 `SystemInitializer().initialize_system`）→ `_start_main_loop`（:111；:117 `system_loop`，:149 `system_loop.start()` 或 :139 Qt 包裝）→ `_keep_running`（:231）或 `_run_with_qt_event_loop`（:174） | 讀碼 | `system_loop` 是模組級單例，ProductionRunner 以 `debug.enable_frontend`（:161–172）決定是否走 Qt |
| SystemInitializer 實際順序 | 同上 SDD 7 階段 | `core/system_initializer.py:74–120`：Controller(:136) → Framework(:165) → Router(:350) → Managers(:369) → WorkingContext(:407) → 預設 Identity(:534) → Frontend(:461) → Health(:596) | 讀碼 | 與 SDD 順序不同：SDD 說 Event Bus/State 最先，實際 Controller 最先，Event Bus 要等 `system_loop.start()`（:432→:146）才啟動 |
| `_setup_module_connections` | SDD 無此階段 | `system_initializer.py:207–239`：`get_module("llm_module")`、`get_module("sys_module")`（:216–217），互相 `set_mcp_server` | 讀碼 | 見 §1.2「停用開關繞過」 |
| 預設 Identity | 無 | `system_initializer.py:534–594` 以 `core_framework.get_module('nlp')`（:550）設測試 Identity，檔內註明「臨時測試用」 | 讀碼 | minimal 下 nlp 未註冊，此階段走降級（未驗證失敗是否阻擋啟動；`initialize_system` :104 對失敗回 False） |

### 1.2 模組開關

| 項目 | 本地設計主張 | 實際入口與呼叫點 | 證據 | 缺口 |
|---|---|---|---|---|
| `engine_mode` | 完成報告：新增 `engine_mode: minimal` 並使 Framework 跳過模組 | 定義 `configs/config.yaml:3`。全 repo（不含 env/dist）只被 `test_minimal_system.py:34,43` 讀取；`core/`、`devtools/`、`modules/`、`utils/` 零引用 | 讀碼（grep） | **純標籤，無行為**；改成 `"full"` 不會改變任何載入 |
| `modules_enabled` | CLAUDE：模組開關 | 唯一的 gating 在 `core/framework.py:362–364`（`_try_register_module`）；另有 `devtools/debug_api.py:13`、`devtools/debugger.py:9`、`utils/module_status.py:20`、`ui_module/debug/module_manager.py:35` 讀來顯示或過濾 | 讀碼 | Framework 的 module_configs（:297）只有 stt/nlp/mem/llm/tts/sys/ui 七個；**ani/mov 從未由 Framework 註冊**，其開關無效，由 UI 內部經 debug_api 載入（framework.py 註解） |
| 實際值 | 完成報告：9/9 全停用 | `config.yaml:8` `stt_module: true`，其餘 8 個 false | 讀碼 | 與報告不符（見 §3） |
| `modules_refactored` | 無 | `config.yaml:17–25` 九個皆 true；只供 debugger 選單上色 | 讀碼 | 無行為 |
| 停用開關繞過 | 完成報告：「Framework 正確根據配置跳過」 | `core/registry.py:9–38` `get_module` **完全不檢查 `modules_enabled`**，任何呼叫都會 import 並 `register()`。繞過點：`system_initializer.py:216,217`（LLM、SYS）與 `_register_settings_callbacks` 的 :258（stt）、:267（tts）、:276（nlp）、:285（llm）、:315（mem）、:324（sys）；`production_runner.py:130,180,198`（ui、stt）；`frontend_bridge.py:101,109`（ani、mov） | 讀碼；未執行（載入模組會載模型，故未驗） | 正式路徑在 minimal 設定下，nlp/llm/mem/tts/sys 仍會被 `get_module` 載入，只是不進 `core_framework.modules`。已知要點屬實，且範圍比「LLM/SYS」更廣 |

### 1.3 core 元件

| 元件 | 本地設計主張 | 實際入口與呼叫點 | 證據 | 缺口 |
|---|---|---|---|---|
| event_bus.py | SDD 02 §2.2：發布/訂閱、`EventData`、`get_event_history`、`deque(maxlen=100)`、事件含 `GS_STARTED/GS_ENDED/ERROR_OCCURRED/SESSION_TIMEOUT` | `core/event_bus.py`：`SystemEvent` 44 項（:27）、`Event` dataclass（:105）、`EventBus`（:114）、全域 `event_bus`（:341）與 `publish_event/subscribe_event` 便捷函式。單元測試 21 項通過 | 讀碼＋執行（單元） | SDD 與程式不符：資料結構是 `Event(event_type,data:dict,source,timestamp,event_id)`；歷史用 list 切片；API 是 `get_recent_events`；**無 `GS_STARTED/GS_ENDED/ERROR_OCCURRED/SESSION_TIMEOUT`**。詳見 §4.1 |
| framework / registry | SDD 02 §2.5：模組自動發現、效能監控 | `core/framework.py`：`CoreFramework`（:212）、`initialize`(:271)→`_auto_discover_modules`(:293)→`_try_register_module`(:356)；`core/registry.py`：`_loaded_modules` 快取＋`get_module` | 讀碼 | 兩層註冊並存：registry 只管載入，framework `modules` 另存 `ModuleInfo`。framework 內另有 SLEEP 時卸載非關鍵模組的處理（`framework.py:898–926`），與睡眠機制耦合 |
| module_coordinator | SDD 02 §2.3：層級轉換、Flow-Based 去重 | `core/module_coordinator.py`：單例 :1602；訂閱 INPUT/PROCESSING_LAYER_COMPLETE、CYCLE_COMPLETED、STATE_ADVANCED、SESSION_ENDED（:135–168）與五個自動化事件（:175–179）；去重鍵 `"{session_id}:{cycle_index}:INPUT|PROCESSING"`（:204,304）；呼叫目標模組走 `core_framework.get_module(request.target_module)`（:1291） | 讀碼 | 去重只覆蓋 INPUT／PROCESSING，**OUTPUT 層無去重**；缺 `session_id` 時預設 `unknown:-1:*`（:199–200），不同事件會互相吃掉；超量清理 `list(set)[:N]`（:220）順序不確定，不保證淘汰最舊鍵。屬 V09 風險（推論） |
| system_loop | SDD 02 §2.10：主迴圈、週期追蹤 | `core/system_loop.py`：單例 :1492；`start`(:421)→`_start_event_bus`(:432→:146)；訂閱 OUTPUT_LAYER_COMPLETE、WORKFLOW_*、FILE_INPUT_PROVIDED、PROCESSING_LAYER_COMPLETE（:104–140）；週期結束呼叫 `unified_controller.check_gs_end_conditions()`（:1155–1156）；直接 import `modules.sys_module.workflows.WorkflowStep`（:1192,1231,1291）、`modules.stt_module.schemas`（:658） | 讀碼 | 1491 行，把 STT 喚醒、workflow 輸入、週期、GS 結束檢查混在一起；core→modules 反向依賴 |
| router.py | SDD 01/02/07：Router「已棄用，保留相容性」 | `core/router.py`：`system_initializer.py:358` 只 import；**唯一實際呼叫** `module_coordinator.py:619–628` `router.route_system_output(...)`，若 `target_module != "tts"` 則中止輸出層（:628–631）。`route_text`、`route_user_input` 在 core/modules/devtools 無呼叫者 | 讀碼 | 已知要點屬實：仍在輸出路徑上。`_decide_target_module`（:153）對 `SYSTEM_OUTPUT` 恆回 `"tts"`（:164–166），等於一個只做 emoji 過濾的固定閘門。CLAUDE.md 也列為「待清理」 |
| sessions/ | SDD 02 §2.6：`SessionInfo`、GS 5 分鐘／WS 10 分鐘超時、`SESSION_TIMEOUT` 事件 | `core/sessions/session_manager.py`（`UnifiedSessionManager`，單例 :827，別名 :830）＋`general_session.py`(:679)＋`chatting_session.py`(:512)＋`workflow_session.py`(:657) | 讀碼＋執行（單元：lifecycle 21 passed/4 skipped） | 無 `SessionInfo` 類；`SessionRecord` 在 `general_session.py:54` 與 `session_manager.py:57` 各有一份；超時由 Controller 輪詢 `check_session_timeouts`(:624) 驅動；**全部記憶體內，無持久化**（`core/sessions` 無檔案 IO）。依賴清單見 §4.2 |
| states/ | SDD 02 §2.7：狀態轉換、佇列與優先級 | `state_manager.py`(單例 :1080)、`state_queue.py`(`get_state_queue_manager` :1036)、`sleep_manager.py`(:393)、`wake_api.py` | 讀碼 | `state_manager` 反向呼叫 `unified_controller`（:125,160,427）與 `session_manager`（:136,205,293,420,520）；`state_queue.py:602` import `modules.nlp_module.intent_types`；`state_manager.py:665,715` import `modules.sys_module.actions.mischief`。NLP 直接寫 StateQueue（`nlp_module.py:718,1506`）與 `state_manager.set_state`（:1667），符合 SDD「狀態決策權在 NLP」，但耦合進 core |
| working_context | SDD 02 §2.8：`create_context/update_context/get_context`＋`ttl_seconds`；兩條協作通道 CHAT_MEM／WORK_SYS | `core/working_context.py`：`WorkingContextManager`(:205)、單例 :1117；`ContextScope`(SESSION/GLOBAL/PERSISTENT :31–35)、`ContextType` 11 項(:38–51) | 讀碼 | SDD 的 API 名與參數對不上現碼（現碼有 `set_data`、`set_context_data`、`global_context_data` 直取、`add_to_gs_history` 等，約 78 個方法）。`ContextType` 內**沒有** CHAT_MEM／WORK_SYS，通道概念在現碼是以 `MEM_EXTERNAL_ACCESS`／`SYS_WORKFLOW` 等實現（待確認）。PERSISTENT 會寫檔（:308,361）。僅依賴 `utils.debug_helper`，是乾淨的葉節點 |
| reload_coordinator | SDD 無；深度調查報告列為「開發工具 ?」 | `core/reload_coordinator.py`：`ReloadCoordinator`(:56)、單例 :496。全 repo 無任何 import／引用（grep，含字串；僅 docs 與 `hidden_docs/TASKS.md:60` 提及） | 讀碼（grep） | **死碼**，已知要點屬實；仍引用 `core_framework.get_module('stt')` 等舊 ID，也未被單元測試涵蓋 |
| controller | SDD 02 §2.4：監聽 INPUT_LAYER_COMPLETE 啟動 GS、OUTPUT_LAYER_COMPLETE 結束、發布 `GS_STARTED/GS_ENDED` | `core/controller.py`：單例 :1298；GS 創建由 `_monitoring_loop`(:167) 每秒輪詢 `_monitor_gs_lifecycle`(:231→:252 `_create_gs_for_processing`)，或 `state_manager.py:139` 以 `_pending_gs` 標記建立；結束由 `check_gs_end_conditions`(:264) 檢查「queue idle＋佇列空＋無活躍 CS/WS＋曾進非 IDLE」 | 讀碼 | 與 SDD 不符：非事件驅動；無 `GS_STARTED/GS_ENDED` 事件；`SESSION_STARTED` **全 repo 無發布者**（grep），`_on_gs_started`(:576) 永不觸發；`_on_gs_ended`(:585) 訂閱的 `SESSION_ENDED` 只有 CS／WS 會發，GS 結束不會發。`check_gs_end_conditions` 的例外只記 debug level 2（:326–327），失敗近乎靜默 |
| frontend_bridge / qt_system_loop / status_manager | SDD 06 展示層 | `frontend_bridge.py`(:12，`initialize(coordinator_only)` :35)；訂閱 SLEEP、WAKE、INTERACTION、三層完成、CYCLE、ON_CALL、GS_ADVANCED（:160–230）；`status_manager`(:588) | 讀碼 | 見 V04。`status_manager` 被 `state_manager.py:5`、TTS 情感向量使用，**不是**「前端相關」 |

### 1.4 SYS 模組

| 項目 | 本地設計主張 | 實際入口與呼叫點 | 證據 | 缺口 |
|---|---|---|---|---|
| `workflows.py` 與 `workflows/` 同名 | 深度調查報告只列 `workflows/` | `modules/sys_module/workflows.py`（1513 行）與套件 `modules/sys_module/workflows/`（`__init__.py` 45 行）並存。PathFinder 解析結果為**套件**。`workflows/__init__.py:9–13` 用 `importlib.util.spec_from_file_location("workflows_core", ...)` 手動執行 `workflows.py`，再把 `WorkflowType/Mode/StepResult/Engine/Definition/Step` 重新匯出（:17–22） | 執行（find_spec）＋讀碼 | 所有 `from modules.sys_module.workflows import ...`（`sys_module.py:27`、`step_templates.py:26`、`system_loop.py:1192,1231,1291`、`unit_tests/conftest.py:48,105,217`、`test_workflows.py:16`）實際讀的是套件重匯出的物件。`workflows.py` 沒有以 `workflows_core` 註冊到 `sys.modules`，無法被別處再 import 到同一實例。`step_templates.py` 又反向 import 該套件，靠「先匯出再 import」的順序避免循環 |
| 自建 MCP Server | SDD 04／CLAUDE：「MCP Server（sys_module）」 | 套件 `modules/sys_module/mcp_server/`（不是單檔 `mcp_server.py`）：`MCPServer`（`mcp_server.py:24`，708 行）、`handle_request`(:274) 為 async 方法。`sys_module.py:93` 建立 `MCPServer(sys_module=self)`；`system_initializer.py:216–230` 以 `llm_module.set_mcp_server(sys_module.mcp_server)` 注入 LLM；`modules/llm_module/mcp_client.py:87` 直接 `await self.mcp_server.handle_request(request)` | 讀碼 | **進程內物件呼叫，沒有傳輸層**（無 stdio／HTTP／socket），僅沿用 MCP 的 JSON-RPC 形狀。MEM 也透過 `register_memory_tools_to_mcp`（`mem_module.py:1991`）把工具掛進同一個 server；SYS 另在 `sys_module.py:328` 以 `registry.get_loaded('mem_module')` 取得 MEM。`MCPTool` 效能回報反向 import `core_framework`（`tool_definitions.py:213`） |
| 附帶發現 | — | `modules/llm_module/` 同時有 `mcp_client.py` 與 `mcp_client/` 目錄；`llm_module.py:34` `from .mcp_client import MCPClient`，目錄內無檔案時以 `.py` 優先 | 讀碼 | 同類命名衝突，新設計不要承襲 |

---

## 2. V01–V04 逐項對照

| 項 | 提案要求 | 讀碼推論 | 執行證據 | 結論／缺口 |
|---|---|---|---|---|
| **V01** 核對本地 01–07、較新設計與程式 call path，列出差異 | 附件只作歷史 | 本文 §1、§3 即差異清單。SDD 與現碼的主要差異：事件名稱與資料結構（§1.3 event_bus）、GS 非事件驅動（controller）、初始化順序（§1.1）、`working_context` API 名、Router 地位（SDD 稱已棄用但仍在輸出路徑）、`mcp_server` 為套件而非單檔 | `pytest --collect-only`：unit 362／integration 44，無 collection error。僅說明測試可 import，不驗行為 | **已完成差異列舉**；SDD 02／04 仍需另做逐字對照後再修訂，本文未逐條核對 SDD 03–06 的模組層內容 |
| **V02** Echo Stream 與 STT／MEM／Session／LLM／TTS／Discord 實際接入狀態 | 各自標明 | Core 與 modules 程式碼中**無任何 Echo Stream 或 Discord 引用**（grep core/modules/utils/configs/devtools，排除 `tts_module/deprecated` 與 docs）。Echo Stream 只存在於 `docs/架構重構/UEP_新架構研究/sources/Echo-Stream/` 的來源快照。現有鏈路為舊的事件鏈：NLP 發 `INPUT_LAYER_COMPLETE`（`nlp_module.py:1554`）→ Coordinator（`module_coordinator.py:189`）→ LLM／MEM 發 `PROCESSING_LAYER_COMPLETE`（`llm_module.py:723,3648,3745,3772,4303`；`mem_module.py:413`）→ Router → TTS 發 `OUTPUT_LAYER_COMPLETE`（`tts_module.py:336`） | 無 | **Echo Stream 的實際狀態無法由本 repo 驗證**（程式不在此處）；Core 側「接入」為零。舊鏈路的端到端未跑 |
| **V03** GS 啟動／雙條件終止、NLP 狀態權限、WS 持久化 | 不要只看註解 | **啟動**：兩條路徑——Controller 輪詢（`controller.py:231–256`）與 StateManager 以 `_pending_gs` 在 IDLE→非 IDLE 時建立（`state_manager.py:120–150`），再加 `_ensure_gs_exists`（:415–430）回呼 Controller。**終止**：`check_gs_end_conditions`（`controller.py:264–328`）實作的是「StateQueue idle＋佇列空＋無活躍 CS/WS＋曾進非 IDLE」；CS/WS 另有 `pending_end`（`chatting_session.py:104`、`workflow_session.py:139,413`）由 :329–376 掃描結束，即所謂雙條件。**NLP 狀態權限**：屬實，NLP 直接寫 `state_queue_manager.add_state`（`nlp_module.py:718`）與 `state_manager.set_state`（:1667）。**WS 持久化**：`core/sessions` 無檔案 IO；僅 Controller 把背景任務寫 `memory/background_tasks.json`（`controller.py:82,1060–1086`），`WorkingContext` 的 PERSISTENT scope 會寫檔（`working_context.py:308,361`） | `test_session_lifecycle.py` 21 passed／4 skipped；`test_event_bus.py` 21 passed。涵蓋單元層生命週期，不含 Controller 輪詢與 NLP 寫狀態 | **WS 無持久化**（重啟即失）；GS 生命週期判定分散在 controller／state_manager／state_queue 三處；4 項 skipped 未查原因 |
| **V04** Frontend Bridge、ON_CALL、SLEEP、口型動畫、播放器真實事件來源 | 事件真實來源 | **Frontend Bridge**：訂閱十餘類事件（含 STATE_CHANGED、SESSION_*、WORKFLOW_REQUIRES_INPUT）並轉給 UI／ANI／MOV（`frontend_bridge.py:160–230`），`coordinator_only` 模式只轉發。**ON_CALL**：`ON_CALL_TRIGGERED` 由 FrontendBridge 自己發（:698–705），來源是 UI `access_widget.trigger_on_call`（`access_widget.py:246`）；訂閱者在 Python 側只有 FrontendBridge 自己（:217,864），再由它呼叫 MOV 播放動畫。**SLEEP**：`sleep_manager.py:125` 以字串名 `_publish_sleep_event("SLEEP_ENTERED")` 發布（:340–350 逐一比對 enum value），Framework（`framework.py:898–926`）與 FrontendBridge 訂閱。**口型動畫**：core／ani／mov／ui 內 grep `lip/mouth/viseme` 無結果，Python 側無口型事件來源。**播放器**：TTS 以 `simpleaudio`（`tts_module.py:28`）播放；`get_playback_state()`（:174）靠輪詢播放物件；`_playback_state = COMPLETED` 在合成呼叫返回後即設（:534），不等於實際播完；`tts_module.py` **無 stop／cancel／interrupt 方法**（grep），也沒有播放回報事件，`OUTPUT_LAYER_COMPLETE` 在 :336 發布 | 無 | 口型與播放回報在現 Core 沒有真實事件來源；取消與插話（V06/V07）目前無對應機制。`MODULE_BUSY/MODULE_ERROR` 只有訂閱者（`frontend_bridge.py`），沒有發布者 |

---

## 3. 勘誤表（`docs/架構重構/` 三份舊報告）

### 3.1 最小化系統重構完成報告.md（2026-04-06）

| 報告內容 | 現碼事實 | 位置 |
|---|---|---|
| 「9 個模組已透過配置停用」「已停用模組 9/9」「已註冊模組 0 個」 | `stt_module: true`，其餘 8 個 false；config 註解自述「已切換為 whisper-base，但暫時停用」與值矛盾 | `configs/config.yaml:8` |
| 「新增 `engine_mode` 使系統最小化」 | `engine_mode` 無任何程式讀取，只被測試腳本印出 | `configs/config.yaml:3`；`test_minimal_system.py:34,43` |
| 「Framework 正確根據配置跳過所有模組」 | 僅 Framework 這一層成立；`registry.get_module` 不檢查開關，初始化器多處直接呼叫 | `core/framework.py:362`；`core/registry.py:9`；`core/system_initializer.py:216,217,258–324` |
| 啟動流程 `Entry.py → system_initializer → Framework.initialize()` | 預設 `debug.enabled: true` 走 debug CLI，不經 SystemInitializer；正式流程是 `Entry → production_runner → SystemInitializer`，Framework 是第 2 階段，前面還有 Controller | `Entry.py:39,193–211`；`config.yaml:34`；`production_runner.py:89–93`；`system_initializer.py:74–86` |
| 附錄 B「改 `engine_mode: full` 即恢復完整模式」 | 改該值無效，必須改 `modules_enabled` | `config.yaml:3,7–16` |
| 「保留 router.py 為核心」「ui/ani/mov 已停用、前端完全解耦」 | Router 仍在輸出路徑；`debug.enable_frontend: true` 且 `modules_refactored` 皆 true；ani/mov 本就不經 Framework 註冊；`frontend_bridge` 以 `get_module` 直接載入 | `module_coordinator.py:619`；`config.yaml:36,17–25`；`frontend_bridge.py:101,109` |
| 「啟動 ~0.1 秒」「記憶體：未測量」 | 無量測方法或紀錄，屬估計值 | — |
| 「Debugger CLI 在無模組下仍可用」 | `debugger.py` 的 `mod_list[...]` 是 `(enabled, refactored)` tuple，`if not mod_list['stt']`（:50，同型 :128,:184）對非空 tuple 恆為 False，停用檢查實際無效 | `devtools/debugger.py:50,128,184` |
| 附帶：報告日期 2026-04-06 | HEAD 為 2026-06-11，且之後 `config.yaml` 有再被修改（最近提交 `ece16cf`） | `git log -- configs/config.yaml` |

### 3.2 系統架構深度調查報告.md（2026-04-06）

| 報告內容 | 現碼事實 | 位置 |
|---|---|---|
| §1.1／§4.1「`stt_module.py` 預設值仍是 whisper-large-v3，有降級風險」 | 預設值已是 `openai/whisper-base` | `modules/stt_module/stt_module.py:90–91` |
| §4.1「`models/stt/whisper/` 是否存在待查」 | 本工作目錄無此目錄（`ls` 失敗），config 指向的 `whisper-base` 本地路徑不存在，會落到 HF Hub 下載 | `modules/stt_module/config.yaml:15–16` |
| §2.2 表：StatusManager、SleepManager「前端相關 ❌」 | `status_manager` 被 `state_manager` 模組級 import（:5）並供 TTS 情感向量；`sleep_manager` 被 `state_manager`（:488,1029）使用，其 `SLEEP_ENTERED` 驅動 Framework 卸載模組（`framework.py:898–926`），不只是前端 | `core/states/state_manager.py:5,488,1029` |
| §2.2 表：StateQueue「可選 ?」、WorkflowSession「可選 ?」、WorkingContext「可選 ?」 | 均為必要依賴：`controller.py:264–300` 用 StateQueue 判定 GS 結束；`state_manager.py:9` 模組級 import `WSTaskType`；`general_session.py:30` 模組級 import `working_context_manager` | 同左 |
| §2.2 表：Router「核心必需 ✓」 | SDD 稱已棄用；實際只有輸出閘門一處呼叫 | `module_coordinator.py:619` |
| §2.2 表：ReloadCoordinator「開發工具 ?」 | 無任何引用的死碼 | `core/reload_coordinator.py:496` |
| §3.1 目錄樹：`sys_module/mcp_server.py` 與 `workflows/` | `mcp_server` 是套件；`workflows.py` 與 `workflows/` 同名並存，後者遮蔽前者 | `modules/sys_module/mcp_server/`；`workflows/__init__.py:9–13` |
| §3.2／§5.2 待查「SYS-LLM 的 MCP 連接是否必需」 | 已確認為進程內物件注入，且由 `system_initializer` 以 `get_module` 強制載入兩者 | `system_initializer.py:216–230`；`llm_module/mcp_client.py:87` |
| §4.2「是否需要三種 SessionManager」 | 三者各自單例＋Unified 包裝；CS/WS 結束事件、GS 歷史、Controller 結束判定都已耦合其上，無法用「合併」直接精簡 | `core/sessions/*.py`；`controller.py:329–376` |

### 3.3 依賴清單分析.md（2026-04-06）

| 報告內容 | 現碼事實 | 位置 |
|---|---|---|
| 規劃新增 `engine.mode` 與 `modules.frontend_enabled/ui_enabled…` 巢狀設定 | 實際採 `metadata.engine_mode` 與既有 `modules_enabled`；規劃的巢狀鍵不存在 | `configs/config.yaml:1–16` |
| 規劃 `Entry.py --engine-only`、`system_initializer` 條件初始化 | 兩者皆未實作：`Entry.py` 無該參數，`system_initializer`／`core/` 無 `engine_mode` 引用 | `Entry.py:98–102` |
| 規劃 `requirements-engine.in`、`requirements-frontend.in`、`requirements-dev.in` | 不存在；現有 `requirements.in`（260 行）、`requirements.txt`（856 行），另有未追蹤的 `pyproject.toml`／`uv.lock` | repo 根目錄 |
| 「TTS：edge-tts 為 API 方案（保留）」 | `edge_tts` 只被 `modules/tts_module/deprecated/infer_core.py:2` import，現行 TTS 為本地引擎（CLAUDE 述為 IndexTTS Lite／BigVGAN） | `modules/tts_module/` |
| 開發工具清單含 `pytest==8.2.2` | `requirements.in` 無 pytest 條目（grep）；pytest 實際存在於 `env/` | `requirements.in` |
| 「SleepManager／Qt 相關可移除」「PyQt5 可移除」 | `production_runner.py:178` 在正式路徑以 `PyQt5.QtCore` 處理 Qt 迴圈；config 的 `debug.enable_frontend: true` 時必需 | `core/production_runner.py:174–178`；`config.yaml:36` |
| 預期效益（15–20GB→5–8GB、4–6GB→2–3GB 記憶體、啟動 30–60s→10–20s）、Whisper 3GB→140MB | 均為預估，無量測；僅 STT 模型 ID 設定確已改（`stt_module/config.yaml:15`） | — |
| 未列入：`modules_refactored` 開關 | 現存且無行為 | `config.yaml:17–25` |

---

## 4. 對新 Core 有用的部分（D16 沿用範圍依據）

### 4.1 `event_bus.py`：API 與事件分類

**API（可直接沿用的表面）**

- `event_bus.subscribe(event_type, handler, handler_name=None)`、`unsubscribe(event_type, handler)`、`publish(event_type, data: dict, source="unknown", sync=False)`、`start()`／`stop()`、`get_stats()`、`get_recent_events(count, event_type)`、`clear_history()`；模組級 `publish_event`／`subscribe_event`。
- 行為：單消費者線程（`EventBusProcessor`）依序分發；handler 例外被捕捉並計數，不影響其他 handler；`sync=True` 在發布者線程內直接分發；訂閱表有 `threading.Lock`；歷史保留 100 筆。
- 單元測試 21 項通過（執行證據）。

**沿用前需知的限制（讀碼推論）**

- `SystemEvent` 是封閉 enum（44 項），新增事件要改檔案；`data` 是 `dict`，無 schema，與 `core/schemas.py` 的 Pydantic 原則脫節。
- `Event.event_id = f"evt_{int(time.time()*1000)}"`（:111）毫秒精度，同毫秒事件會同 ID。
- `_stats` 更新不在鎖內；佇列無上限；`stop()` 不排空佇列。
- 單線程串行：任一慢 handler 會阻塞所有事件。
- 沒有關聯鍵：`session_id`／`cycle_index` 是各發布者自行塞進 `data`，缺了就被 Coordinator 當 `unknown:-1`。
- 啟動責任不在 bus：僅 `system_loop` 與 `Entry --debug-gui` 啟動它。

**事件分類（44 項，依發布／訂閱位置歸類）**

| 類別 | 事件 | 備註 |
|---|---|---|
| 核心管線／會話／狀態（15） | INTERACTION_STARTED、INPUT_LAYER_COMPLETE、PROCESSING_LAYER_COMPLETE、OUTPUT_LAYER_COMPLETE、CYCLE_STARTED、CYCLE_COMPLETED、STATE_CHANGED、STATE_ADVANCED、SESSION_STARTED、SESSION_ENDED、GS_ADVANCED、MODULE_INITIALIZED、MODULE_READY、MODULE_ERROR、MODULE_BUSY | 可沿用的骨幹。但 `SESSION_STARTED`、`CYCLE_STARTED`、`MODULE_INITIALIZED`、`MODULE_READY`、`MODULE_ERROR`、`MODULE_BUSY` 在非測試程式中**沒有發布者**（`MODULE_*` 僅 frontend_bridge 訂閱） |
| UI／前端／電源（5） | SLEEP_ENTERED、SLEEP_EXITED、WAKE_READY、ON_CALL_TRIGGERED、ON_CALL_ENDED | SLEEP 由 sleep_manager 以字串發布，Framework 也訂閱；ON_CALL 實際是 FrontendBridge 自發自收，屬 UI 專用 |
| Workflow（11） | WORKFLOW_REQUIRES_INPUT、WORKFLOW_INPUT_COMPLETED、FILE_INPUT_PROVIDED、WORKFLOW_STEP_COMPLETED、WORKFLOW_STEP_APPROVED、WORKFLOW_FAILED、BACKGROUND_WORKFLOW_SUBMITTED/STARTED/COMPLETED/FAILED/CANCELLED | SYS／LLM／Controller 專用；SUBMITTED、STARTED 無任何引用；STEP_APPROVED 只出現在單元測試。新 Core 的 Task 事件應重定義，不應承襲 |
| 自動化（7） | REMINDER_TRIGGERED、CALENDAR_EVENT_STARTING、TODO_OVERDUE、TODO_UPCOMING、FOLDER_CHANGE_DETECTED、MEDIA_CONTROL_EXECUTED、SYSTEM_STARTUP_REPORT | SYS 的 automation_helper 發布、Coordinator 訂閱；FOLDER_CHANGE_DETECTED 無引用 |
| LLM（2） | LLM_RESPONSE_GENERATED、LLM_LEARNING_DATA_RETURNED | 僅 llm_module 與測試使用 |
| MEM（4） | MEMORY_CREATED、MEMORY_RETRIEVED、SNAPSHOT_CREATED、SNAPSHOT_CONSOLIDATED | MEMORY_RETRIEVED、SNAPSHOT_CREATED 無引用；`snapshot_manager.py:1107` 註明以 MEMORY_CREATED 代替 SNAPSHOT_CREATED |

建議沿用邊界：保留 `EventBus` 的訂閱／發布機制與「核心管線／會話／狀態」類別的語意；UI、Workflow、自動化、LLM、MEM 類事件屬舊模組域，在新 Core 內應改為各 Adapter 自行定義，並補上 correlation 欄位。

### 4.2 `sessions/` 對單例的依賴清單（D16 依據）

下列為 `core/sessions/` 對 controller／state_manager／working_context 等單例的**全部**對外依賴（逐檔 grep 與閱讀）：

| 依賴對象 | 位置 | 性質 |
|---|---|---|
| `working_context_manager`、`ContextType` | `general_session.py:30`（**模組級 import**）；使用處 :191（`working_context_ids`）、:230–241（`global_context_data['current_gs_id']`、`set_data(GENERAL_SESSION)`）、:254–266（`create_context`）、:290,297（`set_context_data`）、:384（`get_current_identity`）、:404（`cleanup_expired_contexts`）、:434（`add_to_gs_history`）、:440（`get_gs_history`） | **最強耦合**：GS 的建立、清理、歷史、Identity 都經由它 |
| 同上（延遲 import） | `chatting_session.py:367–370`、`workflow_session.py:470–473`：讀 `global_context_data['current_cycle_index']` 填入 SESSION_ENDED | 以全域 dict 取週期索引 |
| `event_bus`／`SystemEvent` | `general_session.py:439–451`（GS_ADVANCED）、`chatting_session.py:366–383`（SESSION_ENDED，`session_type='chatting'`）、`workflow_session.py:469–490`（SESSION_ENDED，`session_type='workflow'`），皆延遲 import | 乾淨，可保留 |
| `core_framework` → `llm_module._pending_workflow_events`（私有屬性） | `general_session.py:408–415` | 跨模組存取私有欄位，新 Core 必須移除 |
| `status_manager` | `general_session.py:33–35` `_get_status_manager()` | 已定義但全檔無呼叫（死碼） |
| `utils.debug_helper` | 四個檔案皆有 | 日誌工具，可替換 |
| `state_manager`／`state_queue`／`unified_controller`／`user_settings_manager`／`load_config` | **無** | sessions 對狀態機與 Controller 是單向被依賴，沒有反向引用 |

**反向依賴（誰綁住 sessions）**：`from core.sessions…` 出現在 core（引用行數：controller、module_coordinator ×3、production_runner、system_initializer、state_manager ×6、state_queue ×2、system_loop ×4）、devtools/debug_api、Entry，以及 modules（llm ×6、mem ×4、nlp ×5、stt、sys 的 sys_module、step_templates 與 `workflows.py`／`workflows/*` 共 10 檔、ui access_widget）。`state_manager.py:9` 在模組載入時就 import `core.sessions.workflow_session.WSTaskType`。

**生命週期控制權不在 sessions 內**（讀碼）：

- GS 的建立：`controller.py:252,377`、`state_manager.py:139,428`。
- GS 的結束判定：`controller.py:264–328`（依賴 `state_queue`）。
- 超時：Controller 輪詢 `session_manager.check_session_timeouts`（`session_manager.py:624`）。
- 因此 sessions 目前只是「資料物件＋記帳」，語意由 controller／state_manager／state_queue 決定。

### 4.3 對 D16 的判讀

- **可沿用**：`EventBus` 核心機制；`sessions/` 的 GS／CS／WS 資料結構與 `pending_end` 雙條件概念。
- **沿用前必拆**：`general_session.py:30` 對 `working_context_manager` 的模組級依賴與 :408 對 LLM 私有屬性的存取；`SessionRecord` 重複定義；生命週期決策（`check_gs_end_conditions`、`_pending_gs`）需從 controller／state_manager 中取出重寫。
- **不建議沿用**：`SystemEvent` 的 workflow／自動化／UI／LLM／MEM 類別、`router.py`、`reload_coordinator.py`、`registry.get_module` 的無開關載入、`ConditionalStep` 競態（CLAUDE 技術債）。
- **尚未查證**：`test_session_lifecycle.py` 的 4 項 skipped 原因；SDD 03–06 模組層描述；`state_manager`／`state_queue` 內部（各逾千行）是否可獨立抽出狀態機部分。

---

## 5. 未驗證事項

- 未啟動 Entry.py 或任何模組，故「minimal 設定下實際載入了哪些模組」「debug CLI 下事件是否真的不分發」皆為讀碼推論。
- `models/` 目錄在本機缺失，STT 載入行為未驗。
- 兩份新檔（`pyproject.toml`、`uv.lock`）出現於盤點途中，來源不明，本文未讀取其內容。
