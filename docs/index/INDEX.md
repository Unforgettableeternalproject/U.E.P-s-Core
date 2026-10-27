# Index

查「東西在哪」。規則見 [README](README.md)。標 `planned` 的路徑尚未存在（檢查器會略過）。

範圍：新 Core 在頂層 `uep/`（決策 A7）逐項列出；legacy（`core/`、`modules/`）只連到 [Core現況對照表](../架構重構/Core現況對照表.md)，不逐檔列。

## 1. 模組索引

### 1.1 新 Core（`uep/`）

> 套件根與 P1 持久層／CC-1 已落地；其餘子套件隨各週期補列。子套件名稱為暫定，以實際建立為準；契約與擁有者見 §3。

| 模組 | 路徑 | 職責 | 擁有的契約 | 相關 V／D |
|---|---|---|---|---|
| 套件根 | `uep/__init__.py` | 新 Core 根；不得 import legacy 頂層名稱 | — | A7 |
| 邊界測試 | `tests/uep/test_import_boundary.py` | 強制檢查 `uep` 不 import legacy | — | A7、D19 |
| CC-1／持久層測試 | `tests/uep/` | `tests/uep/test_ids_and_config.py`、`tests/uep/test_envelope_and_bus.py`、`tests/uep/test_payload_policy.py` 不需 DB；`tests/uep/test_journal_db.py`、`tests/uep/test_journal_db_hardening.py` 需 `UEP_TEST_DATABASE_URL`，未設時 skip | CC-1 | V20 |
| 設定 | `uep/config.py` | 自環境變數或 repo 根 `.env` 讀 `UEP_DATABASE_URL`、`UEP_TEST_DATABASE_URL` | — | A9、D10 |
| ID | `uep/ids.py` | `前綴_ULID` 產生與驗證 | — | D32 |
| 持久層 | `uep/db/` | `uep/db/models_registry.py` 集中 import 所有 ORM models；SQLAlchemy engine／Base（PostgreSQL＋psycopg3）；alembic 環境與 migration 在 `uep/db/migrations/`，設定檔為根目錄 `alembic.ini` | — | A9、D10 |
| Event | `uep/events/` | `uep/events/envelope.py`（UEPEvent、Gateway 重建）、`uep/events/registry.py`（事件類型註冊表）、`uep/events/bus.py`（自 legacy 複製、訂閱鍵 str）、`uep/events/journal.py`（journal＋可刪 blob）、`uep/events/outbox.py`、`uep/events/inbox.py`、`uep/events/publisher.py`（journal 先於投遞）、`uep/events/tailer.py`（JournalTailer：xmin 安全水位、交易區間游標，D41）、`uep/events/payload_hash.py`（per-install HMAC，D42） | CC-1 | V09、V20／D16、D22、D33 |
| Context | `uep/context/` | `uep/context/snapshot.py`（ContextSnapshot 單一型別＋purpose、不可變、放入規則 X-1）、`uep/context/models.py`（`context_snapshots` 表與 revision 遞增存取） | CC-2 | V28／D24 |
| Runtime（暫名） | `uep/runtime/` planned | Runtime Adapter | CC-3 | V36／D25 |
| Presence | `uep/presence/` | `uep/presence/models.py`（`presence_current`、`presence_transfers`、`presence_leases` 表）、`uep/presence/manager.py`（PresenceManager：原子 acquire／confirm_activated／renew／release／revoke／confirm_quiesced／resolve_recovery、Core 啟動 epoch +1）、`uep/presence/gate.py`（OutputGate）、`uep/presence/epoch.py`（Host 本機 epoch 檔，路徑 `UEP_HOST_EPOCH_FILE` 或使用者資料目錄） | CC-4 | V11–V18／D4–D6、D26–D28 |
| 共用契約型別 | `uep/contracts/` | `uep/contracts/presence.py`（授權六元組、Gate 結果碼 `GateDecision`、acquire／renew／release 結果碼、`TransferPhase`；零依賴，Core 與端點同源） | CC-4 | D27、D28 |
| 控制通道 | `uep/control/` | `uep/control/channel.py`（ControlChannel 協定、程序內實作：端點專屬信箱與執行緒，不經 outbox／Bus）、`uep/control/journal.py`（送出後背景補記 control 事件）、`uep/control/socket_transport.py`（跨行程控制傳輸：只綁 loopback 的 TCP、換行分隔 JSON，與資料通道分離） | CC-5 | E-12、P-8／V06 |
| Output | `uep/output/` | `uep/output/intent.py`（OutputIntent、ControlSignal）、`uep/output/delivery.py`（DeliveryRecord 與 text／audio 狀態機）、`uep/output/models.py`（`output_intents`、`deliveries` 表）、`uep/output/manager.py`（最小 Delivery Manager） | CC-5 | V06–V08／D29 |
| Adapter 協定 | `uep/adapters/` | `uep/adapters/runtime.py`（Runtime、RuntimeRequest、RuntimeEvent）、`uep/adapters/output.py`（OutputAdapter）、`uep/adapters/tool.py`（ToolExecutor） | CC-3、CC-5、CC-6 | V36／D25 |
| Fake | `uep/fakes/` | `uep/fakes/runtime.py`（FakeRuntime）、`uep/fakes/tool.py`（FakeToolExecutor）、`uep/fakes/output.py`（FakeOutputAdapter）、`uep/fakes/crash.py`（SimulatedCrash 注入點）；只供測試與恢復驗證 | CC-3、CC-5、CC-6 | V07、V21 |
| CC-2／CC-5／fake 測試 | `tests/uep/test_context_output_models.py`、`tests/uep/test_fakes.py` 不需 DB；`tests/uep/test_context_output_db.py`、`tests/uep/test_context_output_review.py`、`tests/uep/test_delivery_manager.py` 需 `UEP_TEST_DATABASE_URL`；共用工廠 `tests/uep/factories.py` | CC-2、CC-5 | V06–V08 |
| Task | `uep/tasks/` | `uep/tasks/states.py`（狀態列舉與轉移表）、`uep/tasks/models.py`（`activities`、`tasks`、`runs`、`steps`、`approvals`、`action_intents`、`receipts` 表）、`uep/tasks/manager.py`（Task Manager：唯一寫入者、action 去重／approval 單次消耗／UNKNOWN 查核、重啟恢復、action_guard 全域停止檢查）、`uep/tasks/presence_bridge.py`（消費 presence 事件，使 presence_bound Task 在安全 checkpoint 暫停）；測試 `tests/uep/test_task_model.py`（需 `UEP_TEST_DATABASE_URL`） | CC-6 | V19–V21／D8、D30 |
| State Coordinator | `uep/state/` | `uep/state/models.py`（`state_projection`、`state_requests` 表）、`uep/state/coordinator.py`（CC-7 裁決：Interaction／Behavior 軸請求、Presence／Health 投影、提議者白名單 fail-closed、晚到事件 no-op） | CC-7 | D6、D40 |
| 全域停止 | `uep/stop/` | `uep/stop/models.py`（`global_stop_flags` 表）、`uep/stop/service.py`（GlobalStop：旗標→revoke→廣播、token 常數時間比對、acquire 與 action 的鎖定旗標檢查）、`uep/stop/api.py`（只綁 loopback 的本機 HTTP API，Bearer token）、`uep/stop/__main__.py`（`python -m uep.stop {stop,release,status,resolve-recovery}` CLI，token 只讀環境變數 `UEP_STOP_TOKEN`，至少 16 位元組；`resolve-recovery` 對應 `POST /v1/presence/resolve-recovery`） | CC-4、CC-7 | T-10／D36 |
| 組裝層 | `uep/assembly.py` | `assemble_core`／`CoreAssembly`：Gate、presence_check、transition_guard、action_guard 必填斷言（R1）；啟動順序 presence → bus → stop API → relay／端點 socket | CC-4、CC-6、CC-7 | D36 |
| CLI 端點 | `uep/endpoint/` | 最小本機端點（A12）：`uep/endpoint/endpoint.py`（CliEndpoint：Lease 持有、二次接納、模擬播放、停止證據與 confirm_quiesced）、`uep/endpoint/watchdog.py`（本地單調時鐘 watchdog）、`uep/endpoint/auth.py`（HMAC 身分驗證，密鑰 `UEP_ENDPOINT_SECRET`／`UEP_ENDPOINT_SECRETS`）、`uep/endpoint/protocol.py`／`uep/endpoint/service.py`（端點 RPC 與 Core 端服務）、`uep/endpoint/reconnect.py`（自動重連）、`uep/endpoint/__main__.py`（`python -m uep.endpoint`） | CC-4、CC-5 | V15／A12、D12 |
| CC-4／控制通道測試 | `tests/uep/test_presence_manager.py`、`tests/uep/test_presence_gate.py`、`tests/uep/test_presence_control.py`（共用 `tests/uep/presence_support.py`；需 `UEP_TEST_DATABASE_URL`，control 通道本身的測試不需 DB）；`tests/uep/test_presence_epoch.py`（不需 DB：epoch 檔、契約型別、migration 鏈） | CC-4、CC-5 驗證案例 VC-4-1、VC-4-2、VC-4-4（過期後他端不得啟用）、VC-4-5；P-1～P-6、P-8 | CC-4、CC-5 | V06、V11、V12、V15 |
| CC-7／全域停止／端點測試 | `tests/uep/test_state_coordinator.py`、`tests/uep/test_global_stop.py`、`tests/uep/test_state_presence_bridge.py`、`tests/uep/test_assembly.py`（共用 `tests/uep/state_support.py`）；`tests/uep/test_endpoint_unit.py`、`tests/uep/test_endpoint_auth.py`、`tests/uep/test_endpoint_socket.py`、`tests/uep/test_endpoint_reconnect.py` 多數不需 DB，`tests/uep/test_endpoint_integration.py` 需 `UEP_TEST_DATABASE_URL`（共用 `tests/uep/endpoint_support.py`） | CC-4 VC-4-4、CC-7、T-10 | CC-4、CC-7 | V15 |
| V11／V12／V15 驗收測試 | `tests/uep/test_acceptance_v11.py`、`tests/uep/test_acceptance_v12.py`、`tests/uep/test_acceptance_v15.py`（共用 `tests/uep/acceptance_p2_support.py`；需 `UEP_TEST_DATABASE_URL`）；審查轉入項釘住現況於 `tests/uep/test_p2_review_carryover.py` | CC-4 驗證案例 VC-4-1、VC-4-2、VC-4-4 | CC-4、CC-5 | V11、V12、V15 |
| T-24 稽核回歸測試 | `tests/uep/test_cycle2_audit_a.py`（CC-1：冪等衝突、tailer、欄位格式、Bus、TRUNCATE）、`tests/uep/test_cycle2_audit_b.py`（CC-4／CC-5：fresh_process、resolve-recovery、revoke 放行、hello 證明、dispatch intent 比對）、`tests/uep/test_cycle2_audit_c_tasks.py`（CC-6）、`tests/uep/test_cycle2_audit_c_context.py`（CC-2／CC-3、assembly strict）、`tests/uep/test_cycle2_audit_fix2.py`（第二輪修補：證據資格、payload_hash kid、階段 1.5 重檢、startup grace）；需 `UEP_TEST_DATABASE_URL` 者未設時 skip | CC-1、CC-2、CC-3、CC-4、CC-5、CC-6 | CC-1–CC-6 | V11、V12、V15、V19–V21／D43 |
| V19–V21 驗收測試 | `tests/uep/test_acceptance_v19.py`、`tests/uep/test_acceptance_v20.py`、`tests/uep/test_acceptance_v21.py`（共用 `tests/uep/acceptance_support.py`；需 `UEP_TEST_DATABASE_URL`；fake Adapter、crash 注入） | CC-1、CC-5、CC-6 驗證案例 VC-1-1、VC-1-2、VC-5-3、VC-6-1、VC-6-2 | CC-1、CC-5、CC-6 | V19–V21 |

### 1.2 legacy（範例區段，以 T-01 內容填寫）

| 區域 | 入口 | 一句話現況 | 詳見對照表 |
|---|---|---|---|
| 啟動 | `Entry.py`、`core/production_runner.py`、`core/system_initializer.py` | 預設 `python Entry.py` 走 debug CLI；正式流程需 `--production` | [§1.1](../架構重構/Core現況對照表.md) |
| 模組開關 | `core/framework.py`、`core/registry.py`、`configs/config.yaml` | `engine_mode` 無行為；`registry.get_module` 不檢查 `modules_enabled` | [§1.2](../架構重構/Core現況對照表.md) |
| 事件 | `core/event_bus.py` | `SystemEvent` 44 項、單消費者線程、歷史 100 筆 | [§1.3](../架構重構/Core現況對照表.md)、[§4.1](../架構重構/Core現況對照表.md) |
| 會話 | `core/sessions/`、`core/controller.py`、`core/states/` | sessions 只是資料物件＋記帳，生命週期在 controller／state_manager | [§1.3](../架構重構/Core現況對照表.md)、[§4.2](../架構重構/Core現況對照表.md) |
| 層級協調 | `core/module_coordinator.py`、`core/system_loop.py`、`core/router.py` | Router 仍在輸出路徑上 | [§1.3](../架構重構/Core現況對照表.md) |
| 死碼 | `core/reload_coordinator.py` | 全 repo 無引用 | [§1.3](../架構重構/Core現況對照表.md) |
| 前端橋接 | `core/frontend_bridge.py` | 訂閱十餘類事件轉給 UI／ANI／MOV | [V04](../架構重構/Core現況對照表.md) |
| SYS | `modules/sys_module/workflows.py`、`modules/sys_module/workflows/`、`modules/sys_module/mcp_server/` | 同名檔案與套件衝突，套件勝出；MCP Server 為進程內物件 | [§1.4](../架構重構/Core現況對照表.md) |
| LLM | `modules/llm_module/` | `modules/llm_module/mcp_client.py` 與 `modules/llm_module/mcp_client/` 同名衝突 | [§1.4](../架構重構/Core現況對照表.md) |
| 勘誤 | `docs/架構重構/` 三份舊報告 | 錯誤已列入勘誤表 | [§3](../架構重構/Core現況對照表.md) |

### 1.3 外部子系統（不在本 repo）

| 子系統 | 角色 | 進度追蹤 |
|---|---|---|
| Echo-Stream | 語音管線 | T-05 |
| echo_stt／echo_tts／echo_memory／echo_thought_core／Discord Bot | 外派 Test 子系統 | T-06 |

## 2. 檔案索引

只列「找不到會浪費時間」的檔案；一般原始碼從 §1 進入。

| 檔案 | 用途 | 讀者 |
|---|---|---|
| `docs/架構重構/UEP_新架構研究/UEP_新架構技術設計提案_2026-10-25.md` | 提案；ADR 在第 22 章、V01–V36 在第 23 章、工作包在第 24 章 | 設計／驗收 |
| `docs/架構重構/Core現況對照表.md` | T-01 成果：legacy 現況、V01–V04、勘誤、D16 依據 | 所有人 |
| `docs/架構重構/Core契約草案.md` | T-08 成果：CC-1–CC-7 契約欄位、擁有者、不變條件、§8 legacy 映射、§9 用語對照、§10 待裁決 | 實作 |
| `docs/index/TRACKING.md` | 決策／驗收項／任務卡對應矩陣 | 所有人 |
| `hidden_docs/DECISIONS.md`、`hidden_docs/TASKS.md` | 內部決策與任務規劃（gitignored，clone 後可能不存在） | 敏卡 |
| `docs/改進計畫.md` | 舊 Echo Kernel 藍圖；已被提案取代（A5） | 僅查歷史 |
| `pyproject.toml`、`uv.lock` | 新環境依賴（A6）；T-03 負責 | 環境 |
| `alembic.ini`、`uep/db/migrations/versions/` | 資料庫 migration（`0001_events_journal`、`0002_task_model`、`0003_context_output`、`0004_presence`、`0005_state_stop`、`0006_journal_txid`、`0007_journal_truncate_guard`、`0008_receipt_worker_epoch`）；URL 由 `UEP_DATABASE_URL` 提供 | 實作／部署 |

## 3. 契約索引

契約內容以 [Core契約草案](../架構重構/Core契約草案.md) 為準（草案未定案，待裁決見其 §10，編號 D22–D44 對應 [TRACKING §1](TRACKING.md)）。依賴方向：CC-3→CC-2；CC-5→CC-4；CC-6→CC-4、CC-2；CC-2–CC-7→CC-1；CC-1 不依賴其他契約（草案 §0.4）。

| 契約 | 草案章節 | 權威擁有者（草案 §x.4／x.6） | 相關 INV | 驗收項（初步） | legacy 對應（草案 §8） |
|---|---|---|---|---|---|
| CC-1 Event（UEPEvent＋journal） | §1 | Event Journal 唯一 append；信任欄位由 Gateway 寫；outbox 由 Dispatcher | INV-03–07 | V09、V20 | `core/event_bus.py` 的 `Event`（[§4.1](../架構重構/Core現況對照表.md)） |
| CC-2 ContextSnapshot | §2 | Context Adapter 組裝與 revision；記憶與 ACL 屬 MEM Access Layer | INV-04、05、07 | V28 | `core/working_context.py` |
| CC-3 Runtime Adapter | §3 | run／attempt 狀態屬 Core；Runtime 只提 intent | INV-02–04 | V36 | `modules/llm_module/` |
| CC-4 Presence／transfer | §4 | Presence Manager（DB 交易）；Output Gate 在 Core | INV-01–03、08 | V11–V18 | 無 |
| CC-5 Output／delivery | §5 | OutputIntent 屬 Core；deliveries 狀態屬 Delivery Manager | INV-02、03、05–08 | V06–V08 | `core/module_coordinator.py` 輸出層 |
| CC-6 Task／action | §6（含 §6.1a Activity） | Task Manager；receipts 屬 executor；approvals 經人類通道 | INV-02–04、06 | V19–V21、V29–V31 | `modules/sys_module/` workflows |
| CC-7 State Coordinator | §6A | State Coordinator 只寫投影與 Interaction 軸；他人實體表歸各 Manager（§7.1） | INV-01、02、04 | V03 | `core/states/state_manager.py`、`core/states/state_queue.py` |

## 4. 事件索引

### 4.1 新 Core 事件（UEPEvent 類型）

定義以草案 [§1.3](../架構重構/Core契約草案.md) 為準；legacy 對照見草案 §8.1。註冊表實作於 `uep/events/registry.py`（`DEFAULT_EVENT_SPECS`）；發布路徑為 `uep/events/publisher.py`，P1 只處理 durable／interaction（control、telemetry 待後續）。各事件的發布／訂閱者隨對應契約落地補列。

| 類別 | event_type | delivery_class | 定義位置 | 契約 | 驗收項（初步） |
|---|---|---|---|---|---|
| 輸入 | `input.text_final`、`input.utterance_final`、`input.attachment_provided` | interaction | 草案 §1.3 | CC-1 | V05、V10 |
| 回合 | `turn.started`、`turn.generation_completed`、`turn.completed`、`turn.cancelled` | durable | 草案 §1.3 | CC-1 | V08、V09 |
| 對話 | `conversation.episode_started`、`conversation.episode_ended`、`conversation.segment_started`、`conversation.segment_ended` | durable | 草案 §1.3 | CC-1 | — |
| 狀態 | `state.transition_requested`、`state.changed` | durable | 草案 §1.3 | CC-1、CC-7 | V03 |
| 任務 | `task.*`、`run.started`、`run.ended`、`step.completed` | durable | 草案 §1.3 | CC-6 | V19 |
| 動作 | `action.intended`、`action.approved`、`action.executed`、`action.unknown` | durable | 草案 §1.3 | CC-6 | V21、V30 |
| 在場 | `presence.*`（arrival_requested、transfer_requested、granted、activated、quiesced、revoked、recovering） | durable；stop／revoke 即時訊號走 control | 草案 §1.3 | CC-4 | V11–V16 |
| 輸出 | `output.intent_created`、`output.admitted`、`output.rejected`、`delivery.reported` | durable | 草案 §1.3 | CC-5 | V07、V08 |
| 控制 | `control.stop`、`control.abort`、`control.revoke` | control | 草案 §1.3 | CC-4、CC-5 | V06、V07 |
| 主動 | `proactive.trigger_observed`、`proactive.candidate_decided` | durable | 草案 §1.3 | CC-1 | V25–V27 |
| 生命週期 | `core.started`、`core.ready`、`core.stopping`、`core.component_health_changed` | durable | 草案 §1.3 | CC-1 | V34 |

事件新增或更名時，同一個 PR 更新此表與草案 §1.3；檢查器不解析事件，僅檢查本檔的路徑。

### 4.2 legacy 事件

`SystemEvent` 44 項依發布／訂閱位置分為六類（核心管線／會話／狀態、UI／電源、Workflow、自動化、LLM、MEM），分類表與 file:line 見 [Core現況對照表 §4.1](../架構重構/Core現況對照表.md)。建議沿用邊界：僅「核心管線／會話／狀態」類別的語意（D16）。查單一舊事件：

```bash
grep -rn "EVENT_NAME" core/ modules/
```
