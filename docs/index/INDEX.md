# Index

查「東西在哪」。規則見 [README](README.md)。標 `planned` 的路徑尚未存在（檢查器會略過）。

範圍：新 Core 在頂層 `uep/`（決策 A7）逐項列出；legacy（`core/`、`modules/`）只連到 [Core現況對照表](../架構重構/Core現況對照表.md)，不逐檔列。

## 1. 模組索引

### 1.1 新 Core（`uep/`）

> 骨架已落地（僅套件根）；子套件隨各週期補列。子套件名稱為暫定，以實際建立為準；契約與擁有者見 §3。

| 模組 | 路徑 | 職責 | 擁有的契約 | 相關 V／D |
|---|---|---|---|---|
| 套件根 | `uep/__init__.py` | 新 Core 根；不得 import legacy 頂層名稱 | — | A7 |
| 邊界測試 | `tests/uep/test_import_boundary.py` | 強制檢查 `uep` 不 import legacy | — | A7、D19 |
| Event（暫名） | `uep/events/` planned | UEPEvent、Event Bus 封裝、journal／inbox／outbox | CC-1 | V09、V20／D16、D22 |
| Context（暫名） | `uep/context/` planned | ContextSnapshot 組裝 | CC-2 | V28／D24 |
| Runtime（暫名） | `uep/runtime/` planned | Runtime Adapter | CC-3 | V36／D25 |
| Presence（暫名） | `uep/presence/` planned | Lease、generation／epoch、Output Gate | CC-4 | V11–V18／D4–D6 |
| Output（暫名） | `uep/output/` planned | OutputIntent、Delivery、ControlSignal | CC-5 | V06–V08／D29 |
| Task（暫名） | `uep/task/` planned | Task／Run、ActionIntent、receipt、恢復 | CC-6 | V19–V21／D8、D30 |

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

## 3. 契約索引

契約內容以 [Core契約草案](../架構重構/Core契約草案.md) 為準（草案未定案，待裁決見其 §10，編號 D22–D40 對應 [TRACKING §1](TRACKING.md)）。依賴方向：CC-3→CC-2；CC-5→CC-4；CC-6→CC-4、CC-2；CC-2–CC-7→CC-1；CC-1 不依賴其他契約（草案 §0.4）。

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

定義以草案 [§1.3](../架構重構/Core契約草案.md) 為準；legacy 對照見草案 §8.1。目前尚無程式實作，定義檔與發布／訂閱者待 `uep/events/`（planned）落地後補列。

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
