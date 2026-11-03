# U.E.P 下一週期：Agent 架構與工具契約提案

作者：奈也／奈留（architect）。狀態：**初稿，待敏卡驗收與艾斯維爾裁決**。純文件，不含實作、不含 migration；型別僅為示意。
上游方向（艾斯維爾原話）：「重點是創建 Agent 架構，也就是會有 thought stream 跟自迴圈，並且可以透過工具調用去執行各種工作，不再是單純的 MCP，因此視情況現有 SYS 可能會需要大幅改掉。」「為下一個週期制定一個可擴展的底層工具契約，從 LLM 如何做 tool calling 到我們要怎麼 register 一個工具型式都要特別設計。」
必讀依據：`hidden_docs/架構重構/UEP_新架構研究/UEP_新架構技術設計提案_2026-10-25.md`（下稱「10-25 提案」），與其衝突時以它的方向為主，再對照現有程式實況。
相關文件：[SYS契約草案](SYS契約草案.md)（下稱「SYS 草案」）、[SYS契約增補_平台宣告與遠端執行](SYS契約增補_平台宣告與遠端執行.md)（「SYS 增補」）、[子系統遷入Core計畫](子系統遷入Core計畫.md) §3.5、[Core契約草案](Core契約草案.md)。

**標記**：【奈也】= 實用／體驗觀點；【奈留】= 結構／長期成本觀點；未標者為兩人共識。

---

## 0. 結論先行

1. **SYS 草案不整份推翻，但「Part S 當作 MCP 工具外殼」的定位要改成「Agent 基底」**。保留：ActionIntent 管線（TaskManager 四階段、`params_hash`、approval 綁 `tool_version`、receipt、UNKNOWN 處理）、`worker_epoch`、`ToolSpecResolver` 依賴反轉、D61 結果回饋。大改：(a) 註冊表從「只有 5 個分類欄的 resolver」升為**同時承載分類＋schema＋description 的單一事實來源**；(b) AgentCore 從「單步 `run_step`」升為**有外層 Run 迴圈、預算、心跳、checkpoint 的自迴圈**；(c) LLM tool calling 抽出 **provider 無關的內部表示＋Model Gateway 轉譯層**，`think_llm` 的 `max_tool_rounds` 旁路（D79）整條退場；(d) 核准等待從「回 blocked」改為「**park → resume**」，D61 B 路徑升格為核准等待的常態路徑。
2. **「thought stream」在現架構沒有對應物**（§2.3）。本文把它定義為「attempt 內的 `RuntimeEvent` 串流＋持久化的 Step／ActionIntent／Receipt／checkpoint 軌跡」，並區分「想」與「說」（Q-B）。
3. **能力範圍單向流動、只收窄**：`route 預設表 ∩ Proposal.capability_hint ∩ 快照 capabilities`（決策層）→ 註冊表投影 → `capability_set`（工具層）→ Run 內凍結，之後只能再收窄（不變條件 AL-3）。
4. 待裁決 6 題（§13），其中「自迴圈內『決定』歸誰」為必收題。

---

## 1. 目標與非目標

### 1.1 目標

| # | 目標 |
|---|---|
| G1 | 定義 Agent 自迴圈：觸發、單步、終止、預算、中斷、持久化、單寫者，並與 `Proposal.route=agent_loop`、Proactive（10-25 §9）對接 |
| G2 | 單一註冊表為工具的唯一事實來源（分類＋schema＋description＋能力標籤同源），各 origin 都以同一種 `ToolEntry` 進表 |
| G3 | 能力投影與強制：`capability_set` 是送給 LLM 的 tools 的唯一來源（D60），Gateway 做參數驗證 |
| G4 | LLM tool calling 邊界：provider 無關的內部表示、provider adapter、串流、多輪、平行、錯誤回填 |
| G5 | 副作用與核准在自迴圈下不卡死迴圈 |
| G6 | 從 D79 旁路遷移到新架構的切片順序與驗收條件；明列要廢除的程式與文件 |

### 1.2 非目標

- 不設計 TurnProposer／ProactiveCandidate 內部，也不定 route→能力的對映表內容與 Proposal 新欄位；只定義介面形狀（§10，本節由決策層維護）。
- 不選 SDK（D1 的比較試驗另案）。本文只規定「Runtime 看得到什麼、看不到什麼」，並讓第一個 Runtime 實作可以是自寫薄層（Q-F）。
- 不做 Task Graph（D34）、不做多 Agent handoff（10-25 §8.1：首版單 Agent）。
- 不處理部署、環境設定、憑證配發。
- 不動 Presence／Output Gate／State Coordinator 的契約，只列對接點。

---

## 2. 現況摘要（已重新定位核實；基準：worktree `feature/tool-contract`，自 develop 切出）

### 2.1 Part S 已實作部分

| 項目 | 事實 | 路徑 |
|---|---|---|
| `ToolSpec` | 8 欄 frozen dataclass：`executor`、`side_effect_class`、`idempotency`、`presence_bound`、`tool_version`＋D91 預留 `origin`／`provider`／`targeting`（**皆裸 `str`，無列舉**）。無 description、params_schema、capability、approval_level、scope、interruptible、schema_hash | `uep/adapters/tool.py` |
| `ToolSpecResolver`／`EnumerableToolSpecResolver` | 協定；`resolve` 須為記憶體內查詢（在 FOR UPDATE 交易內呼叫）；`specs()` 供組裝層 TG-5 | `uep/adapters/tool.py` |
| `ToolExecutor` | `lookup_authoritative`、`execute`、`lookup_receipt`；**無 `cancel`**（D69 未實作） | 同上 |
| `StaticToolRegistry` | 凍結映射；只驗名稱非空與 `tool_version>=1` | `uep/tools/registry.py`（40 行） |
| `ToolGateway` | `check_intent` 只檢 `capability_set` 含該工具與 args 為 Mapping；`result_view`／`outcome_view`（D61：items／status／degraded／partial）；`filter_tool_result` 全放行 | `uep/tools/gateway.py` |
| `AgentCore` | `run_step`：`Runtime.start` → 消費 `stream` → `tool_intent` 經 Gateway → `create_action` → `execute_action` → `submit_tool_result`。`ApprovalRequired` 等只回 `blocked`。**核實：`uep/agent/core.py` 完全沒有 `begin_step`／`complete_step`／`heartbeat`／`save_checkpoint` 呼叫，沒有外層 Run 迴圈、預算或取消旗標處理** | `uep/agent/core.py` |
| `Runtime` 協定 | `start`／`resume`／`stream`／`cancel`／`status`／`submit_tool_result`；`RuntimeRequest.capability_set`、`mode∈{direct_chat, agent_loop}`；`RuntimeEvent.kind` 為**封閉列舉**（text_delta／tool_intent／approval_request／checkpoint／result／error）；`tool_intent` 只有 `{tool, args_complete, args, side_effect_hint}`，**無 call id** | `uep/adapters/runtime.py` |
| `TaskManager` | `start_run`、`begin_step`、`complete_step`、`save_checkpoint`、`heartbeat`（回 None）、`record_approval`（只收已驗證 human principal）、`create_action`、`execute_action`、`pause_task`、`resume_run`、`list_resumable_runs`。`ActionStatus` 只有 INTENDED／APPROVED／EXECUTING／SUCCEEDED／FAILED／UNKNOWN（**無否決終態**） | `uep/tasks/manager.py`、`states.py` |
| 組裝 | TG-5 雙向檢查（只對可列舉的靜態註冊表）；`_assert_wired` | `uep/assembly.py` |
| fake | `uep/fakes/runtime.py` 已有 `submit_tool_result`（含 `await_result` 暫停語意） | `uep/fakes/runtime.py` |

**production 實況**：`AgentCore`、`StaticToolRegistry`、`ToolGateway` 只有測試與 `uep/console/rig.py` 在用；沒有任何 `Runtime` 實作，也沒有任何程式產生 `capability_set`。決策層的 `Proposal.route` 有 `ROUTES={direct_chat, agent_loop, task_request, ignore}`（`uep/turn/proposer.py`），但 **`.route` 在 `uep/` 內沒有下游讀取者**（grep 核實：只有驗證用的 `proposal.route not in ROUTES`）。Supervisor、Proactive Engine 在 `uep/` 無實作（`uep/` 內只出現在 `assembly.py`、`events/registry.py` 等註解／事件名）。

### 2.2 實際對話回合的 tool calling（D79 過渡旁路）

```
EchoPipelineHost(host.py) ──_refresh_tools()──► think.tools = access.tool_definitions() ∩ TOOL_CLASSES
LLMThinkStage(think_llm.py)._tokens ──► backends/openai.py (chat / responses) 串流
   └─ _run_tools（max_tool_rounds=2）──► CoreToolExecutor(echo/tools.py) ──► Access Layer(memory/access.py)
                                         └─ 事後補記 action.*（無 ActionIntent／receipt／核准）
```

- 白名單 `TOOL_CLASSES` 只有 `deep_recall`／`member_recall`（internal_read）、`force_remember`（reversible_local）；schema 來自 `tool_definitions()`。**分類（`TOOL_CLASSES`）與 schema（`tool_definitions()`）是兩套事實來源**，且與 Part S 的 `ToolSpec` 無關。
- `openai.py` 已有 chat→Responses 的 tools 格式攤平（`{"type":"function", **t["function"]}`）與「gpt-5.6-luna chat 路徑不接受 function tools + reasoning_effort，降為 none」的暗坑：**provider 差異目前以散落的 if 處理**，沒有能力表。
- 迴圈控制者是 `think_llm.max_tool_rounds`（2 輪）：預算是魔術數字，沒有 Run、Step、取消旗標、持久化。

### 2.3 「thought stream」在現架構有什麼

| 名稱 | 實際是什麼 | 與「自迴圈的 thought stream」的關係 |
|---|---|---|
| `subsystems/echo_thought_core`（舊稱 Session） | LLM 對話上下文與 token 優化：`ContextManager`、壓縮引擎、`session_store`、`llm_client`、backends。遷入計畫 §160 已建議對外改稱 Thought／Context Engine | **是上下文與壓縮零件，不是迴圈**。歷史擁有者問題見 Q-D |
| ES（`uep/stream/`）的 ThinkStage | 一輪對話的 token 串流（push→pull 通道）→ 切句 → TTS | **是單輪對話的「說」的管線**，不是多步思考 |
| `RuntimeEvent` 串流（CC-3） | attempt 的事件序列 | 本文拿它當 thought stream 的即時面 |
| Task／Run／Step／checkpoint | 持久化軌跡 | 本文拿它當 thought stream 的耐久面 |

結論：**「自迴圈」目前只存在於 CC-3／CC-6 的契約與 `AgentCore.run_step` 的單步骨架**；要做的是補外層 Run 迴圈與預算，並新增「想」與「說」的分流。

---

## 3. 架構總覽

```
 觸發來源                    決策層（另案維護）                   Agent 層（本文）                         工具層（本文）
 ───────────                ───────────────────                 ───────────────                        ──────────────
 對話 turn ──► TurnProposer ─► Proposal(route, capability_hint)
 (route=agent_loop/task_request)     │  route 預設表 ∩ hint ∩ 快照capabilities
 外部事件(Chatroom指派…) ─► ProactiveCandidate 評估→仲裁          ▼
 排程/自發(10-25 §9) ───►     (shadow mode 先行)       LoopTrigger(已收窄 capability_tags)
 核准完成/恢復 ─────────────────────────────────────────►│
                                                           ▼
                              Task/Run 建立(TaskManager) ─► AgentLoop (外層 Run 迴圈, 持 worker_epoch)
                                                           │  每 Step：begin_step → Runtime attempt(一段)
                                                           │          → tool_intent* → Gateway 驗證 → create/execute_action
                                                           │          → submit_tool_result → complete_step/checkpoint → heartbeat
                                                           │  預算/取消/deadline/核准park 在 Step 邊界檢查
                       Runtime 實作(Model Gateway 之上) ◄───┤
                          ▲ render_tools(capability_set)    │
                          │                                 ▼
          ToolRegistry ─►projection─► capability_set ──► ToolGateway ──► TaskManager.execute_action ──► ToolExecutor
          (ToolEntry單一來源)                                   (schema驗證)     (分類取自註冊表,TG-1)      (builtin/mcp/platform_remote)
                                                           │
                                  text → 「想」:thought_delta 不進 Output Gate；「說」:result/say → OutputIntent → Output Gate
```

依賴方向（單向）：決策層 → Agent 層 → 工具層 → CC-6 TaskManager；Task Manager 只依賴 `ToolSpecResolver` 協定（SYS 草案 §0.3 的依賴反轉保留）。工具層不 import Agent 層；Agent 層不 import 決策層型別（只收 `LoopTrigger`）。

| 模組 | 職責 | 對外介面 |
|---|---|---|
| `uep/adapters/tool.py`（協定層） | `ToolSpec`（分類，寫入 ActionIntent 的欄位）、`ToolSpecResolver`、`ToolExecutor`、`ToolCall`／`ToolReceipt` | 既有，擴欄只加不改 |
| `uep/tools/registry.py` | `ToolEntry` 載入／驗證／凍結、投影 `project(...)`、lockfile 比對、組裝斷言 | `ToolRegistry`（實作 `EnumerableToolSpecResolver`＋`project`） |
| `uep/tools/gateway.py` | schema 驗證、injected_params、`result_view`、過濾掛點 | `ToolGateway` |
| `uep/tools/render.py`（新） | `render_tools(capability_set, dialect)`：唯一能產生送 provider 的 tools JSON 的地方 | 函式 |
| `uep/models/`（新，對齊 10-25 §8.3、遷入計畫 F5／Q6） | Model Gateway：`ProviderAdapter`、能力表、`ToolCallAssembler` | 協定 |
| `uep/agent/` | `AgentLoop`（外層迴圈）＋ `AgentCore.run_step`（單步，沿用） | `LoopTrigger` → `LoopOutcome` |
| 決策層（另案） | route 預設表、capability_hint、ProactiveCandidate | §10 |

---

## 4. 題 1：Agent 迴圈

### 4.1 與現有 AgentCore／Runtime 協定的關係：**沿用並擴充，不取代**

【奈留】`run_step` 已經把最難的部分做對了（穩定 `action_id`、`StaleWorkerEpoch` 處理、`args_complete` 不建 action、結果只回短碼）。真正缺的是它的外層。取代會丟掉已驗證的不變條件，成本高於擴充。
【奈也】但「迴圈」要有自己的名字與測試面：新增 `AgentLoop` 包住 `AgentCore.run_step`，不要把迴圈邏輯塞進 `run_step`。

| 層 | 沿用 | 擴充／新增 | 捨棄 |
|---|---|---|---|
| `Runtime` 協定 | 全部 6 個操作 | `tool_intent` 加 `call_id`；`RuntimeEvent` 加 `thought_delta`（Q-B）；`RuntimeRequest` 加 `budget_hint`（只讀，供模型自我節制，非強制） | — |
| `AgentCore.run_step` | 全部 | 回傳增加「本步是否因核准 park」等狀態（`StepEnd` 加 `awaiting_approval`、`budget_exhausted`） | — |
| `AgentLoop`（新） | — | 外層 Run 迴圈、預算、心跳、checkpoint、park／resume | — |
| `think_llm._run_tools` + `max_tool_rounds` | — | — | 退為 adapter 內部參數（單一 attempt 內的保險絲），不再是迴圈控制器；`direct_chat` 回合是否保留工具見 Q-E |

### 4.2 觸發來源（全部收斂成同一種輸入 `LoopTrigger`）

| 觸發 | 入口（決策層） | 進迴圈的方式 | 備註 |
|---|---|---|---|
| 使用者回合 | TurnProposer → `Proposal.route=agent_loop` | Orchestrator 在簽 ticket 後以 `LoopTrigger(kind="user_turn")` 啟動 | **`route=agent_loop` 就是這個入口**；`RuntimeRequest.mode="agent_loop"` 與之同名，是對接點 |
| 對話型任務請求 | `route=task_request` | `LoopTrigger(kind="task_request")`，建長壽命 Task | 與 user_turn 差別在 Task 是否跨 turn 存活 |
| 外部事件 | ProactiveCandidate（觀察→評估→仲裁→啟動，10-25 §9）。Chatroom 指派（NT-7）屬此類 | `LoopTrigger(kind="external_event")`；Task `owner_actor_id` 固定艾斯維爾（D64） | 不經 TurnProposer |
| 排程／自發 | 同上，shadow mode 先行（10-25 §9.3） | `LoopTrigger(kind="proactive")` | 首版只做 shadow（只記「會不會啟動、為什麼」） |
| 續行 | 不經決策層（範圍已定） | 核准完成、`resume_run`、Supervisor 重啟恢復 → `LoopTrigger(kind="resume")`，沿用原 Run 的凍結能力 | 恢復不得擴大能力（AL-3） |

迴圈內的「續想」不是新觸發：同一 Run 的下一個 Step。

### 4.3 單步結構（想→決定→工具→觀察→續或停）

一個 Step ＝ `begin_step(label)` ─ Runtime 產生一段輸出（想，`thought_delta`）─ 模型決定 ─ 0..n 個 `tool_intent`（動作）─ 工具結果以 D61 視圖回餵（觀察）─ `complete_step` ＋ `save_checkpoint`。Step 結束＝可 checkpoint 的安全點（沿用 SYS 草案 §2.2）。label 為語意短碼（`think`、`act`、`await_approval`、`finalize`），不得是自由文字。

```
loop:
  (1) 預算檢查(Step 邊界)           ── 耗盡 ──► finalize(無工具)或 FAILED(budget_exhausted)
  (2) heartbeat → 取消旗標/StaleWorkerEpoch ── 命中 ──► Runtime.cancel，收尾
  (3) 重投影 capability_set(只收窄) → 變動則通知 Runtime（下一輪 tools 變少）
  (4) begin_step → run_step：
        thought_delta*  →（不進 Output Gate）
        tool_intent*    → 序列化處理(Q-C)；任一 ApprovalRequired → park（§8）
        result          → 模型宣告完成（可說的內容）
  (5) complete_step + save_checkpoint(含預算計數器、下一步語意、待處理 action ids)
  (6) 停止判斷：result ∣ 無 tool_intent 的純文字收束 ∣ park ∣ 預算 ∣ 取消 ∣ error
```

**「決定」這一步的歸屬**是待裁決 Q-A：我的建議是「啟動與範圍歸決策層；迴圈內步驟（呼叫哪個工具、要不要續想）歸 Agent 迴圈」。

### 4.4 終止與預算

預算是 **Policy 參數**（由 `policy_ref` 解析，數值非契約），計數器存 checkpoint 以跨 resume 保持：

| 維度 | 強制點 | 耗盡處置 |
|---|---|---|
| 步數 `max_steps`、工具次數 `max_tool_calls` | AgentLoop，Step 邊界 | 進入 `finalize`：以 `tools=[]` 要求模型收束；仍未收束 → Run `FAILED(budget_exhausted)` |
| 牆鐘 `run_deadline`／`step_timeout` | Supervisor 設旗標＋AgentLoop 於 heartbeat 讀取（SV-2，沿用拆分：step_timeout 只取消卡住的 Step） | 沿用 SV-2 |
| token／成本 `max_tokens`、`max_cost` | Model Gateway 回報 usage → AgentLoop 累計 | 同步數 |
| 無進展 | 同一 `(tool, params_hash)` 連續失敗或重複 N 次、連續 `rejected` N 次 | `FAILED(no_progress)`，不再喚醒模型 |
| 並行 Run 數 | AgentLoop 啟動閘門（全域上限） | 排隊或拒絕，由決策層回報原因 |

【奈留】「無進展」必須列為強制項：LLM 自迴圈最常見的失效是重複同一個被拒絕的呼叫燒預算。

### 4.5 中斷與取消（沿用，補迴圈面）

- `control.stop` 或 barge-in：先 `Runtime.cancel`；`EXECUTING` action 依 `interruptible`（D31）：可中斷嘗試 `executor.cancel`（D69，待實作），`checkpoint_only` 到安全點，`non_interruptible` 等完成（受 `shutdown_grace` 約束）。
- **barge-in 取消的是 turn 的「說」，不必然取消 Run**：Run 是否隨之取消看 `interrupt_policy`；預設對 `user_turn`＝取消，對 `task_request`／`external_event`＝暫停語音但 Run 繼續（結果延後通知）。【奈也】這是體驗關鍵：使用者插話不應殺掉一個做到一半的長任務。
- 晚到事件：`cancel` 後晚到的 `thought_delta`／`text`／`result` 丟棄，已發生的 receipt 不丟（R-2，沿用）。

### 4.6 與 Presence／Bus／Orchestrator

- **Orchestrator 仍是 turn 的唯一擁有者**。agent_loop 回合：Orchestrator 簽 ticket、決定要不要先給一句即時回應（direct_chat 風格的 ack）；Run 之後脫離 turn generation 獨立存活，結果以 `OutputIntent` 經 Output Gate 交付，並遵守 Presence（`presence_bound` 工具離場即擋）。Agent 迴圈**不直接發話**（沿用 SYS 草案 §2.1、10-25 §8.1）。
- **Bus**：迴圈只透過 TaskManager 公開方法寫狀態（`task.*`／`run.*`／`step.*`／`action.*` 事件由 TaskManager 發）；迴圈自身不新增事件，唯一新增建議：`tool.drift_detected`（§5.5）、`run.parked`（可用 `run.waiting_approval` 現有事件則免增，待實作時核）。
- **State Coordinator**：維持 D63，`core.agent` 只提議 Interaction 軸。
- **ES 管線隔離**：ES 在自己的專屬 event loop 執行緒（D70(1)）；AgentLoop 不得共用該 loop 做阻塞操作。AgentLoop 在 Core 的 worker 執行緒／asyncio task 上跑，與 ES 只經 `OutputIntent` 與事件銜接。

### 4.7 持久化與單寫者

- **全部沿用 CC-6**：Task／Run／Step／checkpoint／ActionIntent／Receipt／Approval（PostgreSQL，D10）。本文**不新增任何迴圈專屬資料表**。
- checkpoint 內容（沿用 10-25 §17.2）：已完成步驟、輸入／結果引用、`next_step_semantic`、待核准 action ids、已消耗 action id、**預算計數器**、`context_revision`、**凍結的 capability tag 集合與 `tool_version` 快照**、Persona／授權版本。**不存**執行位置或模型原始思考文字。
- **思緒文字預設不持久化**（隱私與體積）：只留 Step label、tool 序列、計數器與 `thought_ref`（可選、預設關閉、有 TTL、可刪，D33 同風格）。要看思緒只能看 live 的 thought stream（Console／觀測）。
- **單寫者**：一個 Task 至多一個未終態 Run；`worker_epoch` 只在 AgentLoop 與 TaskManager 之間（SA-1）；同 Run 同時至多一個 EXECUTING（TC-3）；Supervisor 單例（SV-5）。多個 Run 可並行（不同 Task），各自序列化。

### 4.8 不變條件（迴圈）

| 編號 | 內容 |
|---|---|
| AL-1 | 迴圈只由 `LoopTrigger` 啟動；Runtime、模型輸出、工具結果都不能啟動新 Run 或新 Task |
| AL-2 | 預算由 AgentLoop 強制，不信任模型自述；計數器進 checkpoint |
| AL-3 | **迴圈執行中不得自行擴大 `capability_set`**。每個 Step 的投影必須是前一 Step 的子集；需要更大能力時，必須產生新的觸發事件並重新經過決策層（範圍只能收窄，由決策層重新裁定）。Run 恢復（resume）沿用 checkpoint 內的凍結集合再與當下快照取交集 |
| AL-4 | 想（`thought_delta`）永不進 Output Gate；說只來自 `result`（或明確的通知 intent） |
| AL-5 | 迴圈不自行建立或消耗 approval（沿用 SA-5、TG-7） |
| AL-6 | 核准等待與長時間等待不佔用 worker（§8） |

---

## 5. 題 2：工具註冊形式

### 5.1 單一事實來源：`ToolEntry`

現況缺口：`ToolSpec`（分類）、`access.TOOL_CLASSES`（白名單）、`tool_definitions()`（schema）、`host._refresh_tools`（曝光）各管一段。新設計只有一張表，一個 `ToolEntry` 一次宣告所有面向，其他面向全部由它**衍生**。

```python
# uep/adapters/tool.py（協定層；既有欄位保持不動，新欄位全帶預設，D91 只加不改）
Origin = Literal["builtin", "mcp_static", "platform_approved",   # 可進 LLM 可見集合
                 "discovered", "platform_declared"]               # 候選：永不可見
VISIBLE_ORIGINS: frozenset[Origin] = frozenset({"builtin", "mcp_static", "platform_approved"})
Targeting = Literal["core_local", "platform_remote"]
ApprovalLevel = Literal["none", "per_action", "standing"]        # standing 首版不實作(D65)
InterruptKind = Literal["interruptible", "checkpoint_only", "non_interruptible"]

@dataclass(frozen=True)
class ToolSpec:            # 寫入 ActionIntent 的分類面；resolver 回傳這個
    executor: str
    side_effect_class: SideEffectClass
    idempotency: Idempotency
    presence_bound: bool
    tool_version: int
    origin: Origin = "builtin"
    provider: str = "core"
    targeting: Targeting = "core_local"
    approval_level: ApprovalLevel = "none"      # 新增（帶預設）
    interruptible: InterruptKind = "non_interruptible"  # 新增；fail-closed 預設最嚴
    capability: str = ""                         # 新增；能力標籤，Registry 載入時必填(§5.4)

@dataclass(frozen=True)
class ToolDescriptor:      # LLM 面；不進 resolver、不進 TaskManager
    description: str                    # 給 LLM，Core 撰寫
    params_schema: Mapping[str, Any]    # JSON Schema 子集(§6.3)，additionalProperties:false
    usage_hint: str = ""                # 使用時機文字(取代 think_llm 內 TOOLS_PROMPT/ABOUT_ME_RULE，C4)
    injected_params: tuple[str, ...] = ()   # Gateway 注入、LLM 看不到的欄位(PD-12)

@dataclass(frozen=True)
class ToolEntry:           # 註冊表的唯一單位
    name: str                           # [a-z][a-z0-9_.]{2,63}
    spec: ToolSpec
    descriptor: ToolDescriptor
    scope: ToolScope                    # {data_scope, platforms, audience_min}
    result_scope: ScopeRef              # 結果項預設 scope（fail-closed=private）
    degradable: bool = False
    schema_hash: str = ""               # 由 Registry 重算並與 lockfile/pin 比對，不信任手填
    provider_ref: str | None = None     # 平台端 local_name；LLM 不可見
    transport_timeout: float | None = None
    disabled: bool = False
    venue_policy: Mapping[str, Any] = field(default_factory=lambda: {"mode": "origin_venue"})
```

【奈留】`ToolSpec` 與 `ToolDescriptor` **分開**是刻意的：TaskManager 在 `FOR UPDATE` 交易內只該看到分類面（SYS 草案 §0.3 依賴反轉），description／schema 是 LLM 面，不該因為改文案而動到 CC-6 型別。`ToolEntry` 是 Part S 內部型別，不被 CC 引用。
【奈也】`description` 的調整頻率遠高於分類。因此規定：**改 `description`／`usage_hint` 不需升 `tool_version`**（不影響安全語意），但改 `params_schema` 或任何分類欄必須升版（進 `params_hash` 域，D55）。

`lookup_authoritative` 仍只在 executor 層（TG-4 不變）。

### 5.2 各 origin 如何進表

全部都產出同一種 `ToolEntry`，差別只在**誰撰寫、誰審核、何時載入**：

| origin | 例 | 撰寫與審核 | 載入 | executor | 備註 |
|---|---|---|---|---|---|
| `builtin` | 記憶工具（`deep_recall`…，TL0 九個）、`lore.*`、`chatroom.*`、未來 `presentation.*` | 程式碼內宣告（`uep/tools/catalog/*.py`），PR 審核 | 組裝時 `registry.register(entry)` | Core 內 executor（記憶走 `memory_wiring`／Access Layer；Lore／Chatroom 為原生 adapter，憑證只在 executor 側） | **子系統提供的工具＝builtin＋`provider=core`**；子系統（echo_memory）內的 `TOOL_DECLARATIONS` 降為實作細節，不是註冊來源 |
| `mcp_static` | 經 MCP 傳輸的外部 server 的**已審核子集** | 設定檔列名單；Core 保存審核副本（description／params_schema 由人撰寫或審核後釘住） | 啟動時連線 `tools/list`，重算 canonical hash 與 `pinned_schema_hash` 比對 | MCP executor（憑證在 executor 側） | 動態發現的其餘工具進 `discovered`（不可見，D59）。T5（`echo-mcp`）永不註冊（C6） |
| `platform_approved` | Desktop／Discord 平台宣告工具 | 平台只提供候選；Core 核准清單項即 `ToolEntry`（SYS 增補 §2.4，PD-3） | 核准清單（可熱載入，改 schema／分類須升版，PD-8） | 遠端 executor（兩鍵：權威／非權威，D91） | `provider != "core"` ⇒ `targeting=platform_remote` |
| `discovered`／`platform_declared` | 候選目錄 | 待人工審核 | — | — | 永不進 `capability_set` |

**註冊 API 形狀**（示意，不是實作）：

```python
registry = ToolRegistry.build(
    entries=[*memory_entries(memory_wiring), *lore_entries(), *chatroom_entries()],
    mcp_static=load_mcp_static("tools.mcp.toml"),     # 審核副本 + pinned hash
    platform_approved=load_approval_list(...),
    lockfile="uep/tools/tools.lock.json",
)   # 建構即驗證並凍結；任何斷言失敗 → AssemblyError(fail-closed)
```

### 5.3 enum＋註冊表層斷言

「enum 是詞彙，斷言是防線」。載入時（以及 CI）斷言：

| # | 斷言 | 來源／理由 |
|---|---|---|
| RA-1 | `origin`、`side_effect_class`、`idempotency`、`approval_level`、`interruptible`、`targeting` 皆屬 Literal 值域（執行期用 `typing.get_args` 檢查，不信任型別標註） | 現況 D91 三欄為裸 `str` |
| RA-2 | `external_effect`／`high_impact` ⇒ `approval_level≥per_action` | SYS 草案 §3.1 |
| RA-3 | LLM 可見 ⇒ `origin ∈ VISIBLE_ORIGINS`；`discovered`／`platform_declared` 永不投影 | 遷入計畫 §3.5 風險(3) |
| RA-4 | `executor` 名單雙向一致（TG-5）；`provider!="core"` ⇔ `targeting=platform_remote` | 沿用 |
| RA-5 | `interruptible!="non_interruptible"` ⇒ executor 實作 `cancel`（D69） | 沿用 TC-4 |
| RA-6 | `capability` 屬閉集詞彙，且其**副作用上限**不低於該工具的 `side_effect_class`（§5.4） | 新增 |
| RA-7 | `params_schema` 通過 schema linter（§6.3）；`injected_params` 皆不在 LLM 面 schema 內 | 新增 |
| RA-8 | `tool_version` 與 `schema_hash` 對 lockfile 單調：hash 變而 version 不升 ⇒ 建構失敗 | §5.5 |
| RA-9 | 名稱不重複；所有 `name` 與 provider 的名稱限制相容（長度、字元集） | 新增 |
| RA-10 | `mcp_static` 的 executor 不得指向 `echo-mcp`（T5）命名空間 | C6 |

### 5.4 能力標籤（capability）

- **形式**：`<domain>.<verb_class>` 的**閉集**，宣告在程式碼的 `CapabilityTag` 列舉（新增標籤＝改程式碼＋測試＋審核）。例：`memory.read`、`memory.write`、`lore.read`、`lore.write`、`chatroom.read`、`chatroom.post`、`desktop.file_read`、`desktop.file_write`、`web.fetch`、`presentation.propose`。
- **一工具一標籤**（必填）。理由：投影變成簡單過濾，無「多標籤 AND／OR」歧義。route 預設表以標籤**集合**表達（決策層維護）。
- **標籤帶副作用上限**：`memory.read` 的上限是 `internal_read`，`chatroom.post` 的上限是 `external_effect`…（RA-6）。這樣即使決策層的 route 表寫成「給 `memory.read`」，也不可能因為某人把寫入工具誤標而讓寫入工具跟著曝光——**標籤授權範圍的語意被註冊表鎖住**。
- 無萬用字元、無階層繼承（不支援 `memory.*`）。【奈留】萬用字元是隱性擴權的典型入口。
- 標籤與 `scope.platforms`／`audience_min` 是兩回事：標籤決定「這類能力本輪開不開」，scope 決定「這個工具在此場域／受眾能不能用」。

### 5.5 版本與 schema_hash 漂移

- `schema_hash` ＝ canonical JSON（沿用 `params_hash` 的唯一 canonical 函式，TG-6）對 `{name, params_schema, side_effect_class, idempotency, approval_level, targeting}` 的 sha256。description 不入 hash（§5.1）。
- **兩道防線**：
  1. **lockfile（`uep/tools/tools.lock.json`，入版控）**：記 `name → {tool_version, schema_hash}`；CI 測試比對，hash 變而 version 未升 ⇒ 失敗（RA-8）。任何工具變更在 PR diff 上可見、可審。
  2. **執行期**：`mcp_static`／`platform_approved` 啟動與每次重連時重算外部回報的 canonical hash，與 `pinned_schema_hash` 比對；不符 ⇒ 該工具 `disabled`（fail-closed）、發 `tool.drift_detected`（非轉移型事件，payload 僅 `{tool, expected_prefix, actual_prefix}`），保持停用直到人工重審。LLM 面與驗證面一律用 Core 的審核副本，不用外部即時內容。
- 升版後已建立但未執行的舊版 action：`execute_action` 比對 `tool_version` 不符 ⇒ `ActionBlocked(tool_version_changed)`（D55，沿用）。

---

## 6. 題 3：投影與強制

### 6.1 投影鏈（一條公式）

```
route 預設能力表            ┐
∩ Proposal.capability_hint   ├─ 歸決策層 → capability_tags（已收窄）
∩ 政策快照 capabilities      ┘                │
                                               ▼   工具層（本文）
   registry.project(capability_tags, ctx) ──► capability_set
        篩選條件：entry.capability ∈ tags
                  ∧ entry.origin ∈ VISIBLE_ORIGINS ∧ ¬entry.disabled
                  ∧ platform ∈ entry.scope.platforms ∧ audience ≥ entry.scope.audience_min
                  ∧ executor_ready(tool)（D91；遠端 executor 在線且宣告相符）
                  ∧ presence_bound ⇒ Presence ACTIVE
```

`capability_set`（`RuntimeRequest.capability_set`，TG-12 形狀沿用並擴充）：

```python
{"tools": [{"tool": str, "version": int, "description": str, "params_schema": Mapping}],
 "revision": int,            # 投影版本，審計與 resume 比對用
 "tags": ["memory.read", ...]}   # 已收窄的標籤集合（checkpoint 凍結用）
```

不含 executor、憑證、分類細節、`injected_params`。

**介面歸屬**：左半（標籤集合的產生）歸決策層；右半（`project`、Gateway、render）歸工具層。兩者的介面只有「`capability_tags: frozenset[str]` ＋ 快照 ＋ 場域脈絡」。

### 6.2 D60 硬規則落點

> 送給 provider 的 tools **恰等於** `capability_set` 的渲染；除註冊表投影外，任何程式碼不得產生 tools JSON。

- **唯一產生點**：`uep/tools/render.py::render_tools(capability_set, dialect)`；Runtime 實作與 `ProviderAdapter.render_tools` 只呼叫它（或其 dialect 轉譯）。
- **Runtime 啟動斷言**：attempt 開始前，比對「將送出的 tools 名稱集合」＝`capability_set` 名稱集合，否則回 `CAPABILITY_MISSING` 並拒絕啟動（TC-1，沿用 D60）。
- **hosted／built-in 工具禁用**：`render_tools` 只輸出 `type=function`；adapter 若看到任何非 function 型別 ⇒ 拒絕。
- **入口斷言**：`Gateway.check_intent` 對不在**當前投影**內的 `tool_intent` ⇒ `CAPABILITY_MISSING`（沿用）；`think_llm.tools` 屬性刪除，改由 Runtime 每個 attempt 注入。
- **CI 斷言**：靜態測試檢查程式碼中不存在手寫 `{"type": "function"` 的 tools 清單（白名單：`render.py`、測試 fixtures）。
- **能力單調（AL-3）**：`project()` 的結果對同一 Run 只能 ⊆ 前一次；實作上以 checkpoint 內凍結標籤集合為上界。

### 6.3 Gateway 參數驗證

現況 Gateway 只驗 args 為 Mapping。新增（`check_intent` 之後、`create_action` 之前，沿用 SA-3 順序）：

1. **JSON Schema 子集驗證**：`type`、`properties`、`required`、`enum`、`minimum`/`maximum`、`minLength`/`maxLength`/`pattern`、`items`、`additionalProperties:false`（強制）。**首版不支援 `$ref`／`oneOf`／`anyOf`／`allOf`**（各 provider 對其支援度不一，是 tool calling 失敗的常見來源；RA-7 linter 在註冊時就擋）。
2. **標註型別**：`x-uep-type ∈ {path, url, actor_ref, scope_ref}`；path 做 canonicalization＋允許目錄，url 做 SSRF 檢查（10-25 §18.3）。
3. **`injected_params`**：LLM 若提供這些欄位 ⇒ `rejected`（`param_not_allowed`）；Gateway 在驗證通過後注入（`venue`、`subject_actor` 等）。注入發生在 `params_hash` 計算**之前**，核准所見＝實際執行。
4. **整數／浮點**：沿用 D53 建議，由 schema 型別擋同值異表示。
5. **失敗回餵**：`status="rejected"`，並帶新增**選填** `reason_code`（固定短碼詞彙：`invalid_tool_args`、`param_not_allowed`、`capability_missing`、`tool_unknown`…）＋ `fields`（違規欄位的 JSON Pointer 路徑，**不含值**），讓模型能自我修正而不洩漏參數內容或例外文字。此為 D61 `result_view` 的**只加不改**增補，需與決策層確認其消費方不受影響（§11）。
6. 驗證失敗不建 action（SA-3），但計入「連續失敗」預算（§4.4）。

---

## 7. 題 4：LLM tool calling 邊界

### 7.1 provider 無關的內部表示

**不新發明**：內部表示就是 `RuntimeEvent.tool_intent`，補兩個欄位以支援多輪與平行：

```python
tool_intent = {
  "tool": str, "args": Mapping, "args_complete": bool,
  "call_id": str,           # 新增：provider 的 call id；缺（如 Ollama）由 adapter 以 (attempt_id, seq, index) 確定性合成
  "index": int,             # 新增：同一模型訊息內的順序
  "side_effect_hint": str | None,   # 沿用：不可信，只記 log（TG-2）
}
```

結果回餵沿用 D61 `submit_tool_result(attempt_id, action_id, result_view)`；`action_id` 與 `call_id` 的對應由 AgentCore 維護（`stable_action_id(run, step, ordinal)` 不變，SA-4）。

### 7.2 Provider adapter（Model Gateway）

位置對齊 10-25 §8.3 與遷入計畫 F5／Q6。`openai.py` 的 chat／Responses 兩種格式攤平已是雛形，但差異現在散落在 if 中；改為顯式能力表：

```python
class ProviderAdapter(Protocol):
    dialect: Literal["openai_chat", "openai_responses", "ollama", "anthropic"]
    caps: ProviderCapabilities     # text_stream, tool_calling, parallel_tool_calls, structured_output,
                                   # tools_with_reasoning(gpt-5.6-luna 的 chat 路徑為 False), cancel_semantics,
                                   # context_limit, data_egress_policy, schema_subset
    def render_tools(self, tools: Sequence[ToolView]) -> list[dict]: ...
    def render_tool_result(self, call_id: str, view: Mapping) -> ModelMessage: ...
    def stream(self, req: ModelRequest) -> AsyncIterator[ModelEvent]: ...
    # ModelEvent: text_delta | tool_call_delta(index, call_id, name, args_fragment)
    #           | tool_call_done(index) | usage | finish(reason: stop|tool_calls|length|error)
```

`ToolCallAssembler`（provider 無關，Core 內）：累積 `tool_call_delta` → `finish` 時解析 JSON → 產生 `tool_intent`。
- 增量期間只在 Agent 緩衝，**不**建 action（TC-2、SA-3）。
- JSON 不合法、或 `finish_reason=length` 截斷在 args 中途 ⇒ `args_complete=false`／回 `invalid_tool_args`，**絕不猜測補值、絕不執行**；以 `rejected` 回餵模型，最多重試 N 次（計入無進展預算）。
- provider 不支援的能力（例如 `tools_with_reasoning=False`）由 adapter **明示降級並記錄**，不再默默改參數。
- 路由（10-25 §8.3）：資料外傳限制 → 必需能力 → 可用性 → 延遲／成本 → 偏好；無滿足者明確失敗，不因 fallback 把私人記憶送往未核准 provider。fallback 發生在**新 attempt**，已有副作用者先查 receipt，不重跑整輪。

| 面向 | OpenAI chat | OpenAI Responses | Ollama | Anthropic（預留，不建） |
|---|---|---|---|---|
| tools 形狀 | `{"type":"function","function":{name,description,parameters}}` | 攤平：`{"type":"function",name,description,parameters}` | 同 chat 風格 | `{name,description,input_schema}` |
| 增量 args | `tool_calls[].function.arguments` 分塊（首塊帶 id/name） | function_call arguments delta 事件 | 通常整塊、無 id | `content_block_delta` 的 `input_json_delta`；`tool_use` block 帶 id |
| 結果回餵 | `role=tool, tool_call_id` | `function_call_output, call_id` | `role=tool` | `tool_result` block（`tool_use_id`） |
| 平行呼叫 | 同一 assistant 訊息多個 tool_calls | 多個 function_call item | 視模型 | 同訊息多個 `tool_use` block |
| 內部對映 | `call_id←id` | `call_id←call_id` | `call_id←合成` | `call_id←id` |

Anthropic 預留成本只有上表一欄＋一個 adapter 檔；內部表示不變。**D60 對「server tools／hosted」的禁用同樣適用**。

### 7.3 串流、多輪、平行、錯誤

- **串流中的 tool_intent**：`text_delta`（說）與 tool call 並行不阻塞（沿用 §4.3）；agent_loop 模式下文字走 `thought_delta`（Q-B）。
- **多輪**：由 AgentLoop 的 Step 迴圈驅動，不再由 adapter 內的 `max_tool_rounds` 驅動。一個 attempt 可跨多個 Step（D61 A）；adapter 內保留 `max_internal_rounds` 僅作保險絲。
- **平行呼叫**：同一模型訊息的多個 `tool_intent` **可同時到達，但預設序列化執行**（TC-3 單一 EXECUTING）。Provider API 要求每個 call id 都有結果才能續輪，因此：被序列化延後的呼叫先得到暫時結果前不續輪；若中途遇到需核准的呼叫 ⇒ 該呼叫 park，**同批後續呼叫以 `blocked(reason_code=deferred_by_approval_wait)` 回餵、不執行**，由模型在 resume 後重新規劃。是否放寬為「internal_read 可並行」見 Q-C。
- **錯誤回填**：`failed`／`rejected`／`blocked`／`unknown` 皆經 D61 視圖回餵，只帶短碼，不帶例外文字或參數值。`UNKNOWN` 不得讓模型以新 action_id 重送同語意操作（SA-11）。
- **args 不完整**：`args_complete=false` 不建 action；組裝完成後重驗（TC-2、SA-3 沿用）；Core 重啟後不補提交（VC-S3-1）。

---

## 8. 題 5：副作用與核准

### 8.1 沿用什麼

TaskManager 四階段、`side_effect_class` 分級（TG 表 §3.4）、`record_approval` 只收已驗證 human principal、`params_hash` 綁 `{tool, v, params}`（D55）、receipt／UNKNOWN／`resolve_unknown`、TG-7 反例、D64（非 owner 來源 Task 的 approvers 固定艾斯維爾）、D62（回合內 Task 懶建立）、D54（分類只由註冊表決定）。

### 8.2 自迴圈下核准等待不卡死：park → resume

**現況**：`AgentCore._handle_intent` 遇 `ApprovalRequired` 只回 `blocked` 並繼續迴圈；Run 實際已被 TaskManager 轉為 `WAITING_APPROVAL`，但 worker 沒有釋放，模型也不知道在等什麼。

**新流程**（D61 B 路徑從「崩潰 fallback」升格為「核准等待的常態路徑」）：

```
execute_action → ApprovalRequired
  1. AgentLoop：save_checkpoint(next_step_semantic="await_approval:<action_id>",
                                 pending_action_ids, context_revision, 預算計數器, 凍結能力標籤)
  2. 同批後續 tool_intent 以 blocked(deferred_by_approval_wait) 回餵(或不續)，Runtime.cancel(attempt, "park")
  3. 通知 intent → approvers 經 Presence 選通道(T-8)；idempotency_key 去重
  4. 釋放 worker（park）：AgentLoop 返回 LoopOutcome("awaiting_approval")，worker 不再占用
  ……（人類在 Approval 通道 record_approval；期間 Supervisor 管 approval_expiry，SV-2(5)）
  5. 核准到達 → LoopTrigger(kind="resume")：resume_run(新 worker_epoch) → Runtime.resume(checkpoint_ref)
  6. 重驗(T-7)：approval 有效、tool_version 未變、能力仍在快照內(只收窄，AL-3)、Presence 條件
  7. execute_action(帶 approval) → submit_tool_result → 續跑下一 Step
```

- 同一 Run 在等待期間**不占 worker、不跑 heartbeat**；其他 Task 的 Run 照常運行。
- 核准過期：Run 維持 `WAITING_APPROVAL`、Supervisor 重發通知，不自動失敗（SV-2(5)、SA-8）；Task `deadline` 仍會結束它。
- 資料缺口（需實作時補）：(a) `resume_run` 現為「PAUSED 或心跳過期才可接手」，WAITING_APPROVAL 且 worker 已主動釋放時需要明確的 `park` 語意（建議 `TaskManager.park_run(run_id, worker_epoch)`：清 worker 心跳並保留狀態，使 `resume_run` 可立即接手）；(b) **否決路徑**：`ActionStatus` 無 REJECTED／CANCELLED，人類拒絕時 action 無終態——建議否決轉 `FAILED(reason_code=approval_denied)` 並以 `blocked/rejected` 回餵，不新增狀態；(c) 目前沒有人類拒絕的記錄 API（只有 `record_approval`）。以上為待實作決策，不影響本文契約形狀。

### 8.3 其餘類別

| 類別 | 自迴圈下的處置 |
|---|---|
| `internal_read` | Gateway 驗證後同步執行；`UNKNOWN` 重跑無副作用不阻擋（TC-3） |
| `reversible_local` | 需任務授權（Task scope）；執行並留 receipt；`presence_bound` 於 action 層強制（TC-5） |
| `external_effect`／`high_impact` | `per_action`；park → resume；絕不由模型或 Runtime 自述核准（TG-7 反例全部保留） |
| 外部內容誘導擴權 | TG-10 反例保留；AL-3 再加一層：即使被誘導，能力集合也無法在 Run 內擴大 |
| 工作量預算內的授權 | `standing` 授權首版不做（D65）；長任務每次外部副作用都 per_action——【奈也】體驗上摩擦大，這是已知取捨 |

---

## 9. 題 6：遷移與切片

### 9.1 對映 TL0～TL5

按**行為**切，每片可獨立驗收與回退；TL 編號為對映，不再作為排程單位。

| 切片 | 內容 | 對映 | 前置 | 驗收條件 |
|---|---|---|---|---|
| **A 註冊表單一來源** | `ToolSpec` 擴欄＋Literal 列舉；`ToolDescriptor`／`ToolEntry`；`ToolRegistry`（RA-1～10）；lockfile 與 CI 測試；9 個記憶工具以 `builtin` 進表（分類取自 `TOOL_CLASSES`，schema 取自 `tool_definitions()` 的現值），`tool_definitions()`／`TOOL_CLASSES`／`host._refresh_tools` 改為由註冊表**衍生**（行為不變） | TL0＋TL1 | N5 | 衍生出的 tools JSON 與舊 `think.tools` **位元組級相等**（黃金測試）；`access.py` 內不再有獨立 schema 字面；RA 斷言測試全綠；`tests/uep` 與 import-boundary 測試不退步 |
| **B 投影與 D60 斷言** | `registry.project`、`render_tools`、Runtime 啟動斷言；Gateway schema 驗證＋injected_params＋`reason_code` | （新）TG-12 | A | 反例：多給／少給 tool 名稱 ⇒ `CAPABILITY_MISSING`；`$ref` schema 註冊失敗；缺必填／多餘欄位 ⇒ `rejected`＋`fields` 不含值；靜態測試證明無手寫 tools 清單 |
| **C internal_read 走 ActionIntent** | 第一個 `Runtime` 實作（Model Gateway＋ProviderAdapter 薄層，包裝現有 backends）；`direct_chat` 回合的記憶工具改經 AgentCore.run_step → create/execute_action；回合 Task 懶建立（D62） | TL2 | B、D62 實作 | `deep_recall`／`member_recall` 每次呼叫有 `act_`＋receipt；`action.*` 事件與舊版 payload 相容；p50/p95 延遲不劣化超過基準（量測值待定）；`CoreToolExecutor` 白名單路徑關閉 |
| **D 自迴圈** | `AgentLoop`：外層 Run 迴圈、預算、heartbeat、checkpoint；`route=agent_loop` 接 `LoopTrigger`；`thought_delta`（依 Q-B）；D61 A 路徑續跑 | （新） | C、決策層 route 表 | fake Runtime 腳本：多 Step 工具鏈完成；預算耗盡 ⇒ finalize 或 `budget_exhausted`；無進展偵測；AL-3 反例（模型/工具結果要求更大能力 ⇒ 不擴大）；取消後晚到事件丟棄、receipt 保留 |
| **E 核准 park／resume** | `park_run`、否決路徑、同批後續呼叫 deferred；`force_remember`、about-me 寫入類改完整四階段 | TL3 | D、DQ-16、D65 | TG-7 五個反例全過；park 期間 worker 釋放（另一 Run 可使用）；resume 後重驗 `tool_version`／approval／能力；崩潰於 park 前後各一案例無重複副作用 |
| **F Supervisor／deadline／關機暫停** | SV-1～5、`ToolExecutor.cancel`（D69） | （SYS S1 尾） | D | 依 SYS 草案 VC-SV-1／VC-SV-2 |
| **G 原生工具與平台遠端** | `lore.*`、`chatroom.*`（SYS S0）；`mcp_static` 載入與 pin；D91 R0～R3 平台宣告工具；legacy 桌面 workflow 以 `platform_remote` 重寫 | TL4、TL5 | E、F、Part H、每訊框 MAC | 依 SYS 草案 §5.4、增補 §7；drift 偵測案例：外部 schema 改動 ⇒ 工具 disabled＋事件；`echo-mcp` 註冊被 RA-10 擋下 |
| **H Proactive（shadow）** | `ProactiveCandidate`→`LoopTrigger(proactive)`，只 shadow | 10-25 §9 | D、決策層 | 只記錄「會/不會啟動、原因」，無實際 Run 被建立 |

### 9.2 要廢除或降級的現有程式／文件

| 對象 | 處置 | 於切片 |
|---|---|---|
| `uep/memory/access.py::TOOL_CLASSES` 作為權威 | 降為衍生值／廢除（分類以註冊表為準） | A |
| `access.tool_definitions()` 的 schema 字面 | 廢除（schema 以 `ToolDescriptor` 為準；實作細節仍留在 Access Layer） | A |
| `uep/echo/host.py::_refresh_tools` | 改為「取 Runtime 投影」，不再自行篩 | A→C |
| `think_llm.tools`／`tool_executor` 直接注入、`_run_tools`、`max_tool_rounds` 作為迴圈控制 | `_run_tools` 邏輯移入 AgentLoop／Model Gateway；`max_tool_rounds` 降為 adapter 保險絲 | C→D |
| `uep/echo/tools.py::CoreToolExecutor` 白名單旁路與「事後補記 `action.*`」 | 廢除；執行改走 `ToolExecutor` 協定的 builtin executor（Access Layer 呼叫保留） | C |
| `think_llm` 內 `TOOLS_PROMPT`／`ABOUT_ME_RULE` 硬編碼使用時機文字 | 移為 `ToolDescriptor.usage_hint`（C4） | A |
| ES `memory_echo` adapter 的第二份 `member_recall` 實作（Core 路徑） | Core 路徑不再註冊（C2，F2 去重） | A |
| **D79**（過渡旁路決策） | 於 C 完成時結案 | C |
| `StaticToolRegistry` | 由 `ToolRegistry` 取代（同協定；舊類別可保留為測試輔助再移除） | A |
| SYS 草案 §3.1 註冊表欄位表、§3.5 TG-12、§4 S3 | 改寫：以本文 §5、§6、§7 為準 | A～D |
| 遷入計畫 §3.5.4 TL0～TL5 表 | 改寫為本文 §9.1 切片對映 | A |
| `RuntimeEvent`（`call_id`、`thought_delta`）、`result_view`（`reason_code`） | 擴充（只加不改） | B～D |

---

## 10. 決策層介面（**本節由決策層維護**；以下僅為骨架與已協議的介面形狀）

> 本節內容的維護者是決策層 agent。工具層／Agent 層只依賴下列「介面形狀」；形狀以外的欄位、route 對映表內容、proposer 內部一律由決策層決定並在此補寫。

### 10.1 已協議的介面形狀

```python
# Proposal 新增選填欄位（歸決策層定義與維護）
@dataclass(frozen=True)
class Proposal:
    ...                                  # 既有欄位不變
    route: str = "direct_chat"           # ROUTES={direct_chat, agent_loop, task_request, ignore}
    capability_hint: frozenset[str] = frozenset()   # 能力標籤集合；只能收窄；空值＝沿用 route 預設

# Core（決策層維護）：route -> 預設能力標籤 的對映表
ROUTE_DEFAULT_CAPABILITIES: Mapping[str, frozenset[str]]   # ignore / direct_chat 預設不帶工具(空集合)

# 決策層 -> Agent 層 的唯一輸入
@dataclass(frozen=True)
class LoopTrigger:
    kind: Literal["user_turn", "task_request", "external_event", "proactive", "resume"]
    origin_ref: str                 # 來源引用(ticket/候選/指派 id)；外部文字不進 goal
    goal_code: str                  # 語意短碼，非自由文字
    capability_tags: frozenset[str] # = 表 ∩ hint ∩ 快照 capabilities（已收窄）
    scope_ref: str
    owner_actor_id: str             # 非 owner 來源固定為艾斯維爾(D64)
    policy_ref: str                 # 解析出預算與中斷政策
    snapshot_ref: str
```

收窄公式：`capability_tags = ROUTE_DEFAULT_CAPABILITIES[route] ∩ (hint 若非空) ∩ 政策快照.capabilities`。
工具層接收 `capability_tags` 後的行為見 §6.1（投影）、AL-3（Run 內不擴大）。

### 10.2 待決策層填寫（骨架）

| 項目 | 內容 | 狀態 |
|---|---|---|
| route→能力標籤對映表 | 各 route 的預設標籤集合 | 待決策層填寫 |
| `capability_hint` 的產生規則與驗證（hint 含表外標籤時的處置：丟棄／整個 Proposal 無效） | | 待決策層填寫 |
| 事件入口：`ProactiveCandidate` → `LoopTrigger` 的欄位對映、仲裁與冷卻 | 參照 10-25 §9.2 | 待決策層填寫 |
| 「要不要啟動、以什麼範圍啟動」的判斷邏輯與延遲預算（D80 的 300ms 是否適用 agent_loop） | | 待決策層填寫 |
| 觸發被拒絕／降級時對使用者的回應方式 | | 待決策層填寫 |

---

## 11. 與 10-25 提案的逐條對應

| 10-25 章節 | 主張 | 本文處置 | 理由 |
|---|---|---|---|
| §6.3 ThinkStage | 一般對話走直達路徑；需工具或多步時 ThinkStage 透過同一 Runtime Adapter 呼叫 Agent，再把可表達結果轉句子流 | **沿用**；補「想／說」分流（AL-4、Q-B）：agent_loop 下只有 `result`／通知 intent 進句子流 | 與 §6.3 的「可表達結果」一致，把它落成事件種類 |
| §6.3 | MEM 檢索、SessionControl 壓縮、SDK 歷史不得重複插入同一段訊息；`context_revision` | **沿用**；`context_revision` 進 checkpoint；歷史擁有者見 Q-D | agent_loop 多 Step 時重複插入風險放大 |
| §6.3 | 晚到資料需取消／重開，使用 `attempt_id` | **沿用**；park／resume 與 fallback 同用新 attempt | |
| §8.1 | SDK 在 Core 管理的執行邊界內；Runtime Adapter 提供 start／resume／cancel／status；輸入為 ContextSnapshot＋能力集合；不持 Bot Token、不繞過 Output Gate | **沿用**，現有 `Runtime` 協定已符合；D1「SDK」措辭**修改**為「任何 Runtime 實作」，首個實作為 Model Gateway 之上的薄層（Q-F） | production 目前沒有任何 Runtime 實作；先有迴圈與契約，SDK 是可替換的實作選項 |
| §8.1 | Handoff 不是人格分裂；首版單 Agent | **沿用** | 非目標 |
| §8.2 | SDK 選型試驗（現有直達＋SYS 為基準組） | **沿用**；本文切片 C 的薄 Runtime 即「基準組」，使後續 SDK 比較有對照 | |
| §8.3 | Model Gateway：能力表、路由順序、工具呼叫增量須完整組裝驗證後才提交、fallback 於新 attempt | **沿用並具體化**為 `ProviderAdapter`／`ProviderCapabilities`／`ToolCallAssembler`（§7.2） | |
| §8.4 | 沿用 MCP 標準，外加 Policy／Tool Gateway；註冊含能力、executor、scope、副作用、冪等、核准、可中斷、presence | **沿用並修正**：把「沿用 MCP」收窄為「MCP 只是 `mcp_static` 一種 origin 與傳輸」；註冊以 `ToolEntry` 為單位且加入 schema／description／capability（§5） | 艾斯維爾「不再是單純的 MCP」；10-25 已預留此方向 |
| §9 Proactive | 四階段；shadow mode 先行；硬性條件先於評分；LLM 敘述不能新增外部授權 | **沿用**；接為 `LoopTrigger(proactive/external_event)`，並加 AL-3（迴圈內不擴權） | 事件入口歸決策層 |
| §14.4 | SDK session 只管模型歷史；每個 conversation 一個歷史擁有者 | **沿用**，並指出 agent_loop 讓此問題變成真問題 ⇒ Q-D | 多 Step 的思考歷史由誰持有 |
| §17.2 | checkpoint 不存程式位置；恢復前重驗權限／scope／批准；已完成副作用不重播 | **沿用**；補上預算計數器、凍結標籤、`tool_version` 快照 | |
| §18.2 | 四類工具與核准分級；核准是系統行為設計 | **沿用**；park→resume 讓 per_action 在自迴圈下可行 | |
| §24 | 研究工作包：六類同題比較（一般對話、MEM、唯讀工具、需核准、取消、恢復） | **沿用**為切片 C／D／E 的驗收題材 | |

**與 SYS 草案是否衝突**：10-25 提案與 SYS 草案**無硬衝突，差異在粒度**：10-25 只給方向（Runtime Adapter、Tool Gateway、分類），SYS 草案給了細節但把 Agent 層寫成「單步＋Supervisor」。本文的修正集中在兩處：(1) 把 10-25 §6.3／§8.1 的「需要多步推理時由 Agent 處理」補成具體的外層迴圈；(2) 把 §8.4 的「註冊表」補成含 schema 的單一事實來源。

---

## 12. 與現行決策（D 編號）的衝突與處置

| D／條款 | 內容 | 處置 | 說明 |
|---|---|---|---|
| D1 | 自有 Core 掌握狀態，SDK 僅執行 Agent Loop | **修改措辭** | 「SDK」→「任何 Runtime 實作」；Core 管狀態的原則不變 |
| D54／TG-1～5 | 註冊表注入 TaskManager；分類只由註冊表 | **保留** | `ToolSpec` 擴欄只加不改 |
| D55／TG-6 | approval 綁 `{tool, version}` | **保留** | 補：改 `params_schema` 或分類必升版；description 不必 |
| D56／TG-9 | 結果過濾掛點首版全放行 | **保留** | |
| D57（NT-2） | 常駐說明承載位置 | **保留**；`usage_hint` 為工具級使用時機文字，與常駐說明分開 | |
| D59 | MCP 當傳輸、靜態註冊、動態發現入待審目錄、schema 釘住 | **保留並具體化** | `origin=mcp_static`＋lockfile／pin（§5.5） |
| D60 | 禁用 hosted 工具；清單恰等於 `capability_set` | **保留並加強** | 落點明確化（`render_tools` 唯一產生點、CI 斷言）（§6.2） |
| D61 | A 為常態、B 為 fallback | **修改（擴充 B 的適用情境）** | B 升格為核准等待的常態路徑；`result_view` 增選填 `reason_code`／`fields`（只加不改，需與決策層確認） |
| D62／TC-5 | 回合內懶建立 Task、action 層強制 presence | **保留**；agent_loop 的 Task 為長壽命 | 兩種 Task 生命週期並存 |
| D63 | `core.agent` 只提議 Interaction 軸 | **保留** | |
| D64 | 非 owner 來源 Task 的 owner／approvers 固定艾斯維爾 | **保留** | `LoopTrigger.owner_actor_id` 承接 |
| D65 | 首版不做常設授權 | **保留**，但列為自迴圈體驗風險 | |
| D67 | Supervisor 形式 | **保留** | 切片 F |
| D69 | `ToolExecutor.cancel` | **保留**（未實作） | 切片 F／RA-5 |
| D79 | ES 內建記憶工具走 Host 白名單，事後補記 | **廢除（切片 C 結案）** | 旁路由 ActionIntent 取代；已知偏離結清 |
| D91／SYS 增補 | 平台宣告、遠端 executor、核准清單項＝註冊表列 | **保留**；`origin`／`provider`／`targeting` 由裸 str 改 Literal | 增補 §2.4 欄位併入 `ToolEntry` |
| D34 | 首版不做 Task Graph | **保留** | |
| D10／D2 | PostgreSQL 權威；單程序 | **保留** | 迴圈無新表 |
| SYS 草案 §2.2 | agent loop 一輪＝一個 Step 內 0..n 個 ActionIntent | **保留**，補外層迴圈、預算、無進展 | |
| SYS 草案 §3.1 欄位表 | 18 欄註冊表 | **修改**：拆為 `ToolSpec`＋`ToolDescriptor`＋`ToolEntry`（§5.1） | 欄位語意不變 |
| SYS 草案 TG-12 | `capability_set` 為註冊表投影 | **修改**：加 `revision`／`tags`、標籤閉集與副作用上限、AL-3 單調 | |
| SYS 草案 TC-1 | SDK 代理函式只產生 tool_intent | **保留** | |
| SYS 草案 SA-5 | Agent Core 等待 Approval，不自建 | **保留**；補 park 機制 | |
| 遷入計畫 §3.5.4 TL0～TL5 | 工具層收斂順序 | **修改**：改以切片 A～H 行為切，TL 作對映（§9.1） | TL2 之後依賴 Part S 實作進度的風險仍在，不列為 M 系列完成條件 |

---

## 13. 待裁決問題（只收會改變契約的題；共 6 題）

### Q-A（必收）自迴圈內「決定」這一步歸誰

背景：非對話事件觸發的自迴圈不經 TurnProposer，須定義「要不要啟動／以什麼範圍啟動」與「迴圈內每一步決定」各歸誰。

| 選項 | 內容 | 利 | 弊 |
|---|---|---|---|
| A. 決策層管啟動與範圍，Agent 迴圈管迴圈內步驟（**建議**，決策層提出、敏卡同意） | 對話走 TurnProposer（TurnCandidate→Proposal），事件走 ProactiveCandidate（評估→仲裁）；兩者共用「route→能力收窄∩快照」；進同一個迴圈。迴圈只決定呼叫哪個工具、要不要續想。加不變條件 AL-3：迴圈內不得擴大 `capability_set`，需更大能力須產生新候選重新經決策層 | 權限範圍單一出口；迴圈無法自我擴權；與 10-25 §9 一致 | 兩個入口要維持一致的收窄邏輯；迴圈內遇到「需要別類能力」要多一次往返 |
| B. 迴圈自行決定（含是否啟動子任務、是否擴能力） | 決策層只在對話入口；事件入口由 AgentLoop 內建規則決定 | 事件路徑延遲最短、實作最少 | 分散「要不要做」的判斷，無統一稽核點；與「決策層決定範圍」的房內共識衝突；迴圈可自我擴權，是最大風險 |
| C. 兩者分工但迴圈可在預先授權的「能力上限」內自行升級 | 決策層給一個上限集合與一個初始集合，迴圈可在上限內擴大 | 少一次往返 | 破壞 AL-3 的簡單性：上限與初始的差異需另行審計；上限一旦過寬等同選項 B |
| D. 全部由決策層逐步裁決（每個工具呼叫都過決策層） | 決策層每步介入 | 控制最嚴 | 延遲與成本不可接受；決策層變成迴圈本體 |

**建議：A**。【奈留】範圍只能收窄、不可迴圈內放大，才能讓「一個 Run 的授權邊界」在 checkpoint 中可驗證、可重放。【奈也】多一次往返的成本由「把常見的相鄰能力放進同一 route 預設表」緩解。

### Q-B 「想」與「說」如何分流（`thought_delta`）

背景：`RuntimeEvent.kind` 是封閉列舉；agent_loop 的中間文字不該進 Output Gate。

| 選項 | 內容 | 利 | 弊 |
|---|---|---|---|
| A. 新增 `thought_delta` kind（**建議**） | `agent_loop` 的推理文字走 `thought_delta`；`text_delta` 保留給 `direct_chat`；說只來自 `result`／通知 intent | 明確、fail-closed（未知 kind 永不進 Output Gate）；測試面清楚 | CC-3 變更（封閉列舉加值）、fake 與消費者要更新 |
| B. 依 `mode` 改變 `text_delta` 語意 | `mode=agent_loop` 時 `text_delta` 視為思緒 | 協定零變更 | 隱含語意；mode 設錯就洩漏思緒到語音；難審計 |
| C. 思緒完全不外送 | Runtime 不輸出中間文字，只輸出 tool_intent 與 result | 最簡、最私密 | 失去觀測與除錯面；Console 看不到思考過程 |

建議 **A**。

### Q-C 平行 tool call 與 TC-3（同 Run 單一 EXECUTING）

| 選項 | 內容 | 利 | 弊 |
|---|---|---|---|
| A. 一律序列化執行（**建議首版**） | 平行意圖到達，逐一執行 | 與 TC-3、單一 EXECUTING 索引零衝突；實作最簡 | 多個 `internal_read` 時延遲較高 |
| B. 僅 `internal_read` 可並行 | 其餘序列化 | 查詢類延遲低 | 要改 TC-3 的 DB 索引語意（目前 `UNIQUE(run_id) WHERE EXECUTING`）；receipt／順序更複雜 |
| C. 全部可並行 | | 最快 | 與 TC-3 衝突，UNKNOWN 阻擋語意失效；不建議 |

建議 **A**，量測後再議 B。

### Q-D agent_loop 的思考歷史由誰持有（10-25 §14.4 的「單一歷史擁有者」）

| 選項 | 內容 | 利 | 弊 |
|---|---|---|---|
| A. `echo_thought_core` 的 ContextManager 持有（**建議**） | 沿用其壓縮與 token 優化；Core checkpoint 只存 `context_revision`＋引用 | 重用已有壓縮／session store；與 direct_chat 一致 | resume 時要能用 revision 重建；跨 Step 的 tool 呼叫／結果訊息須進同一歷史 |
| B. Core checkpoint 持有完整 thought 歷史 | Core 自存 | 恢復自足 | 與壓縮引擎重複；私人內容落 DB 的範圍變大 |
| C. 兩者分工：對話歷史歸 A，工具軌跡歸 Core（Step／receipt） | | 職責清楚 | 組裝上下文時要合併兩源，易重複插入（10-25 §6.3 禁止） |

建議 **A**，並規定工具呼叫／結果以 D61 視圖（已過濾）寫入歷史。

### Q-E `direct_chat` 回合是否保留輕量工具（D79 退場後）

| 選項 | 內容 | 利 | 弊 |
|---|---|---|---|
| A. 保留 `internal_read`（記憶查詢）走 ActionIntent；其餘需 `agent_loop`（**建議**） | 即 ROUTE 表 `direct_chat` 只開 `memory.read` | 對話延遲與現況近似；記憶查詢是對話的核心體驗 | 需承擔每回合的 `act_`／事件量（D62 已預見） |
| B. `direct_chat` 完全無工具 | 需記憶時由決策層升為 `agent_loop` | 路徑最乾淨 | 一般閒聊也要升級才能回憶，延遲與複雜度上升 |
| C. 保留所有 internal_read 與 reversible_local | | 體驗最好 | 擴大對話路徑的寫入面，與「一般對話不啟動多步」相悖 |

建議 **A**；route→標籤表的內容仍歸決策層，此題只決定契約是否允許 `direct_chat` 帶工具。

### Q-F 第一個 Runtime 實作的形式

| 選項 | 內容 | 利 | 弊 |
|---|---|---|---|
| A. Model Gateway 之上的自寫薄 Runtime（**建議**） | 包裝現有 backends＋`ToolCallAssembler`；SDK 比較（D1 試驗）以它為基準組 | 契約先落地、無新依賴；hosted 工具自然禁用；10-25 §8.2 的「基準組」 | 自維護 tool loop 細節（串流、取消） |
| B. 直接採 OpenAI Agents SDK | | 省迴圈樣板 | 需先驗證可禁用 hosted 工具、取消語意、trace 隱私（10-25 §8.2 列的驗證項尚未做）；SDK 內建 session 與 §14.4 衝突 |
| C. 並行兩者 | | 快速比較 | 兩套都要維護 |

建議 **A**，B 作為切片 D 之後的比較試驗。

---

## 14. 風險與技術債

| 項目 | 說明 |
|---|---|
| 工具層與 Part S 實作綁定 | 切片 C 以後都依賴 D62 的 TaskManager 變更（`execute_action` action 層 presence、EXECUTING 索引）；若 D62 未實作，C 只能退回「事後補記」 |
| lockfile 維護成本 | 每次改 schema 要跑腳本更新；以 CI 失敗訊息指向指令降低成本 |
| schema 子集過窄 | 不支援 `$ref`／`oneOf` 可能讓部分外部 MCP 工具無法註冊；這是有意的 fail-closed，必要時逐案擴充 linter 並測 provider 相容 |
| 自迴圈成本與失控 | 預算與無進展偵測為強制項；數值為調校起點，需量測 |
| per_action 摩擦 | 長任務每次外部副作用都要核准；`standing` 授權（D65）延後 |
| thought 可觀測性 vs 隱私 | 預設不持久化思緒；除錯需要時以 `thought_ref`＋TTL（預設關） |
| 否決路徑缺口 | `ActionStatus` 無否決終態；需實作時定案（建議 FAILED＋`approval_denied`） |
| 遺留：TG-9 全放行 | 自迴圈讓工具結果（lore、Chatroom）進入更長鏈路，公開 audience 洩漏風險放大；清償順序建議在 G 之前 |
| 與決策層的耦合 | `result_view` 增補欄位、`capability_hint` 驗證規則、D80 延遲預算是否適用 agent_loop，皆需決策層同意（§10） |

## 15. 未核實的假設（交付時一併回報）

- Chatroom 工具能否以 `action_id` 去重／查核仍未驗證（SYS 草案 NT-9 前置），本文未改動其保守預設（`idempotency=none`、非權威）。
- `.route` 無下游讀取者是以 grep 核實 `uep/` 內 Python 檔，未檢查測試與外部 repo。
- Supervisor／Proactive 在 `uep/` 無實作，是依 grep 與 SYS 草案現況表推定；未逐檔確認是否有分支上的未合併實作。
- `run.waiting_approval` 類事件是否已存在於事件註冊表、park 是否需新事件，未逐項核對。
- `resume_run` 對「worker 主動釋放的 WAITING_APPROVAL」的行為，只讀了函式說明與簽章，未讀完整實作；`park_run` 為提案，非已查證缺口的唯一解。
- OpenAI／Ollama／Anthropic 各 provider 串流事件名稱與欄位（§7.2 表）依既有 `openai.py` 讀碼與一般 API 知識整理，未對照當前官方文件逐一核對；Anthropic 欄僅供預留。
- 「gpt-5.6-luna chat 不接受 tools+reasoning_effort」依 `openai.py` 註解，未實測。
- 延遲、事件量的影響（切片 C）未量測。
