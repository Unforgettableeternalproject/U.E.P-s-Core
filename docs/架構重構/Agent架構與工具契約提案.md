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
4. **裁決後的主要形狀（艾斯維爾，初稿後）**：(a) 迴圈內步驟歸 Agent 迴圈，特殊情況可 `decision.consult` 委派中樞（internal_read 工具、走 ActionIntent、只能在凍結範圍內，§4.9）；(b) 迴圈使用**獨立平行 context**，結束時以 `summary`／`result`／`trail_ref` 回饋原 context（§4.10）；(c) 唯讀與一次性任務工具可**並行**，工具定義帶 `concurrency`／`conflict_keys`／完整 `input_schema`／`output_schema`，多步驟工具為線性 composite，排程規則見 §6.4；legacy 組裝器**類別與引擎不沿用**，只沿用三個概念（§5.7）；(d) 工具有 **scope 維度**（全域／平台／Presence）與平台變體，與 D91 `targeting` 分開（§5.6）；(e) 模型層：迴圈、history、核准、工具執行留在 Core，**不整套沿用任何 SDK**，PydanticAI 只作模型層候選，Spike 1／2 通過才採用（§7.2、§7.4）；(f) 工具註冊表**不執行**載入，只宣告 `requires` 並訂閱 module coordinator 的就緒事件，投影只投 `ready`（§5.8）。
5. 6 題皆已裁決（§13），本文不再有待裁決項目。

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
- 不選 SDK（D1 的比較試驗另案）。本文只規定「Runtime 看得到什麼、看不到什麼」，模型層以 PydanticAI 為候選、依 Spike 1／2 決定，迴圈與狀態不整套沿用任何 SDK（Q-F，§7.2）。
- 不做 Task Graph（D34）、不做多 Agent handoff（10-25 §8.1：首版單 Agent）。
- **module coordinator 與它的 registry**（模組載入、預熱、卸載、patch 機制）不在本提案範圍；本文只定義介面與歸屬（§5.8、§10 I-4），標註「待 module coordinator 設計」。
- 不處理部署、環境設定、憑證配發。
- 不動 Presence／Output Gate／State Coordinator 的契約，只列對接點。

---

## 2. 現況摘要（已重新定位核實；基準：worktree `feature/tool-contract`，自 develop 切出）

### 2.1 Part S 已實作部分

| 項目 | 事實 | 路徑 |
|---|---|---|
| `ToolSpec` | 8 欄 frozen dataclass：`executor`、`side_effect_class`、`idempotency`、`presence_bound`、`tool_version`＋D91 預留 `origin`／`provider`／`targeting`（**皆裸 `str`，無列舉**）。無 description、input_schema、capability、approval_level、scope、interruptible、schema_hash | `uep/adapters/tool.py` |
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
| `uep/models/`（新，對齊 10-25 §8.3、遷入計畫 F5／Q6） | Model Gateway：`ModelPort`（Core 自有埠）、能力表、`ToolCallAssembler`；SDK 型別不得出此套件 | 協定 |
| `uep/agent/` | `AgentLoop`（外層迴圈）＋ `AgentCore.run_step`（單步，沿用） | `LoopTrigger` → `LoopOutcome` |
| 決策層（另案：`決策中樞架構提案.md`） | route 預設表、capability_hint、ProactiveCandidate、DecisionPlan、`decision.consult` 的 kind 與 schema | §10 |
| module coordinator（待設計） | 模組載入／預熱／卸載／patch；發布就緒事件 | 事件 `module.ready／warming／unavailable`（§5.8） |

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

一個 Step ＝ `begin_step(label)` ─ Runtime 產生一段輸出（想，`thought_delta`）─ 模型決定 ─ 0..n 個 `tool_intent`（動作）─ 工具結果以 D61 視圖回餵（觀察）─ `complete_step` ＋ `save_checkpoint`。Step 結束＝可 checkpoint 的安全點（沿用 SYS 草案 §2.2）。label 為語意短碼（`think`、`act`、`await_approval`、`finalize`），不得是自由文字。`thought_delta`（Q-B 已裁決）目前**只輸出到 log／終端**，UI 呈現延後；事件格式先固定，消費端暫時只有 log sink。迴圈在 Step 內的 history 寫入**迴圈 context**（§4.10），不是原 context。

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

**「決定」這一步的歸屬**（Q-A，已裁決）：啟動與範圍歸決策層；迴圈內步驟（呼叫哪個工具、要不要續想）歸 Agent 迴圈；特殊情況迴圈可經 `decision.consult` 委派中樞（§4.9）。

### 4.4 終止與預算

預算是 **Policy 參數**（由 `policy_ref` 解析，數值非契約），計數器存 checkpoint 以跨 resume 保持：

| 維度 | 強制點 | 耗盡處置 |
|---|---|---|
| 步數 `max_steps`、工具次數 `max_tool_calls`、諮詢次數 `max_consults`（`decision.consult` 的子限，**屬於** `max_tool_calls` 的一部分，不另外加總） | AgentLoop，Step 邊界 | 進入 `finalize`：以 `tools=[]` 要求模型收束；仍未收束 → Run `FAILED(budget_exhausted)` |
| 牆鐘 `run_deadline`／`step_timeout` | Supervisor 設旗標＋AgentLoop 於 heartbeat 讀取（SV-2，沿用拆分：step_timeout 只取消卡住的 Step） | 沿用 SV-2 |
| token／成本 `max_tokens`、`max_cost` | Model Gateway 回報 usage → AgentLoop 累計 | 同步數 |
| 無進展 | 同一 `(tool, params_hash)` 連續失敗或重複 N 次、連續 `rejected` N 次；**另：`decision.consult` 同 `kind` 且結果相同（`advice`／`reason_code`／`escalate` 相同）連續 N 次**（`params_hash` 判不出這種情況，需以 kind＋結果摘要比對） | `FAILED(no_progress)`，不再喚醒模型 |
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
| AL-3 | **迴圈執行中不得自行擴大 `capability_set`**。每個 Step 的投影必須是前一 Step 的子集；需要更大能力時，必須產生新的**候選事件**（缺口類別＋`decision_id`，由決策中樞的 `loop.reeval` 處理）並重新經過決策層（範圍只能收窄，由決策層重新裁定）。Run 恢復（resume）沿用 checkpoint 內的凍結集合再與當下快照取交集 |
| AL-4 | 想（`thought_delta`）永不進 Output Gate；說只來自 `result`（或明確的通知 intent） |
| AL-5 | 迴圈不自行建立或消耗 approval（沿用 SA-5、TG-7） |
| AL-6 | 核准等待與長時間等待不佔用 worker（§8） |
| AL-7 | `decision.consult` 的 `advice` 不得超出凍結範圍；超出時只回 `escalate`（`escalate.needed=true`），迴圈結束本 Run，並送出候選事件，由決策中樞 `loop.reeval` 決定是否發出新的 `LoopTrigger`（§4.9 CN-1～3） |
| AL-8 | 迴圈內容只寫入迴圈 context；結果只以 `summary`／`result`／`trail_ref` 一次、冪等地回饋原 context，思考文字與原始工具結果不回饋（§4.10） |

### 4.9 `decision.consult`：迴圈把決定委派給決策中樞（Q-A）

裁決：迴圈內步驟歸 Agent 迴圈（A），但**特殊情況下迴圈可把決定委派給決策中樞**，中樞是**可呼叫的指示器，不是完全獨立**。協議形式為 `decision.consult`。

**註冊表條目**（本文定義條目與強制，**kind 與回傳 schema 歸決策層維護**）：

| 欄位 | 值 | 理由 |
|---|---|---|
| `name` / `capability` | `decision.consult` / `decision.consult`（標籤副作用上限 `internal_read`） | 走標準投影：只有 Run 凍結標籤含 `decision.consult` 才會被投出 |
| `side_effect_class` / `approval_level` | `internal_read` / `none` | 純諮詢，不改任何外部狀態；**走 ActionIntent**（有 `act_`、receipt、稽核） |
| `idempotency` | `native`（同 `(run, step, ordinal)` 重放得同一 `decision_id`） | 崩潰重送不產生第二個決定 |
| `concurrency` | `serial` | 避免多個諮詢同批互相矛盾 |
| `timeout_s` | **0.5**（executor 總時限） | **300 ms 只計中樞 `decide()`**；executor 總時限含派送、序列化與 Gateway 開銷，必須大於 decide 預算（0.5 > 0.3）；逾時處置見 CN-6 |
| `origin` / `executor` | `builtin` / 決策中樞的內部 executor（`internal_executors` 明示，TG-5 豁免對映） | 工具層不內含決策邏輯 |
| `injected_params` | `run_id`、`frozen_capability_tags`、`scope_ref`、`plan_ref` | 由 Gateway 注入，**模型不可提供**；中樞據此知道「凍結範圍」 |

**kind（首版 6 個，歸決策層）**：`speak`、`output_modality`、`placement`、`ask_user`、`loop_end`、`followup`。
**回傳（歸決策層）**：`advice`（DecisionPlan 片段）、`reason_code`、`confidence`、`escalate`（形狀 `{needed: bool, reason_code}`）、`decision_id`。回傳以 D61 `result_view` 的 `items` 承載，`status` 照常。

**強制規則**：

| 編號 | 規則 |
|---|---|
| CN-1 | `advice` **只能落在「凍結範圍 ∩ 當下快照」內**（凍結的能力標籤、scope、audience，再與當下政策快照取交集；凍結範圍內但快照已不允許者也算超出）。超出時中樞**只回 `escalate`**（`escalate.needed=true`），不回超範圍的 advice |
| CN-2 | AgentLoop 在收到結果後**再驗一次**（縱深防禦），範圍同 CN-1（**凍結範圍 ∩ 當下快照**）：`advice` 若引用該交集外的標籤／工具／scope，一律當作 `escalate.needed` 處理並寫稽核。`placement` 類 advice 另須檢查 **`target_platform`**（必須屬交集內允許的平台，且為當下 Presence 可達）與 **`executor_ref`**（必須是註冊表中該平台變體的合法 executor 且 readiness 為 `ready`），任一不符即視為超出 |
| CN-3 | `escalate.needed` ⇒ AgentLoop **結束本 Run**（以 `finalize` 收束，不再續跑工具），並**送出候選事件**（缺口類別＋`decision_id`）；該事件由決策中樞的 **`loop.reeval`** 處理，由它決定是否**發出新的 `LoopTrigger`**（新範圍，只經決策層的收窄／裁定；迴圈自己絕不擴大，AL-3） |
| CN-4 | consult **計入步數**（`max_steps`）與工具次數（`max_tool_calls`），並受 **`max_consults`** 限制（`max_tool_calls` 的子限，見 §4.4） |
| CN-5 | consult 走**快速欄位**：中樞 `decide()` 預算 300 ms（只計 decide，不含 executor 開銷）；條目 `timeout_s=0.5` 為 executor 總時限，必須大於 decide 預算。**不觸發 `plan.refine`**（不重新精煉 DecisionPlan） |
| CN-6 | consult 逾時 ⇒ 回 **`status=ok`＋`advice=∅`**（不是 failed），迴圈改用**決策中樞提案裡的 per-kind 預設表**（預設表歸決策層，類比 D80）。若 **Gateway 層真的失敗**（`failed`／`unknown` 視圖，例如 executor 例外、`tool_not_ready`、總時限 0.5 s 仍逾時），迴圈同樣**把它對映為 `advice=∅`** 並採同一張預設表；失敗仍計入步數與 `max_consults`。**不得因 consult 失敗而放寬任何範圍** |
| CN-7 | `advice` 是**資料，不是授權**：不產生 approval、不繞過 Output Gate／Presence／核准；`speak` 類 advice 只是對 `OutputIntent` 的建議 |
| CN-8 | 呼叫來源有兩種，皆走同一註冊表條目與 ActionIntent：模型自行呼叫（工具被投出時）；或 AgentLoop 在 Policy 規定的點主動呼叫（例如預算將盡、`loop_end` 前、發話前）。「特殊情況」的觸發條件歸 Policy／決策層維護，不寫死在迴圈。**若 `capability_set` 不含 `decision.consult` 標籤，迴圈（含 Policy 主動呼叫路徑）不得呼叫它**（符合 AL-3，不可繞過凍結集合）；決策層會把該標籤放進 `agent_loop` 的預設標籤 |
| CN-9 | `decision_id` 寫入 checkpoint 與 receipt meta，供稽核與重放去重 |

consult 不經 TurnProposer（TurnProposer 只處理對話回合的啟動判斷）；它是迴圈內的第二個決策入口，兩者共用同一個決策中樞與同一組收窄語意。

### 4.10 平行 context：迴圈內容的隔離與回饋（Q-D）

裁決：思考歷史由 **ContextManager 持有**（echo_thought_core）。另：迴圈要開一個**獨立的平行 context** 處理迴圈內容，結束後把結果回饋到原 context。

術語：**原 context**（parent）＝該 conversation 的 ContextManager 工作 context；**迴圈 context**（child）＝Run 專屬的平行 context。

**建立（fork）**——Run 啟動（以及 resume 重建）時由 ContextManager 建立 child，`fork_manifest` 記錄取了什麼：

| 取自 parent | 不取 |
|---|---|
| Persona／系統提示（以 `persona_version` 引用，不複製內容） | 完整原始歷史 |
| 觸發內容：`goal_code`＋（`user_turn` 時）觸發的那句使用者原文 | 其他 conversation／其他 audience 的內容 |
| parent 當前**壓縮摘要**（引用 `parent_revision`）＋有界的**近期視窗**（最近 N 則或 token 預算內） | parent 的待合併 aggregation、未決輸出 |
| audience／scope（`scope_ref`，只縮不放，E-6） | 超出 audience 的私人內容 |
| 凍結工具的 `usage_hint`（由 `capability_set` 渲染，不從 parent 繼承） | parent 先前的工具呼叫與結果訊息 |

child 記錄 `parent_context_id`、`parent_revision`、`fork_manifest`。**迴圈期間 parent 的新訊息不自動進 child**；與任務相關的追加指示須以明確事件送入（`task.input_provided`／`followup`），避免 child 被閒聊污染，也避免 10-25 §6.3 禁止的重複插入。

**迴圈內壓縮**：沿用 ContextManager 的壓縮引擎，對 child 獨立計量與壓縮。優先順序：(1) 先丟棄舊 Step 的 `thought` 文字；(2) 其次把大型工具結果換成「摘要＋`result_ref`」（真相在 receipt／來源，必要時再取）；(3) 最後才壓縮對話訊息。**永不壓掉**：目標、凍結範圍清單、待處理 action 與 approval、最近 K 個 Step 的原文。每次壓縮產生新的 `child_revision` 並記入 checkpoint。

**結束時回饋（merge-back）的形狀**——三部分，**一次、冪等**（鍵 `loop_return:{run_id}`）：

| 部分 | 內容 | 進 parent 的方式 |
|---|---|---|
| `summary` | 迴圈結果的**短摘要**（有 token 上限；標 `scope_ref`；來源標記 `loop:<run_id>`） | parent 的一則系統備註訊息 |
| `result` | 使用者可見的最終結果（`result` 事件內容；**已實際交付**者才寫入 parent，未交付者只記意圖） | 與 Output 交付結果對齊的 assistant 訊息 |
| `trail_ref` | 指向 Run／Step／Action／Receipt 軌跡的**引用**（id，不含內容） | parent 只存引用，需要時經工具取 |

**不回饋**：思考文字、原始工具結果、中間訊息。記憶寫入只經工具（`force_remember` 等），不經 merge-back。失敗／取消／預算耗盡時只回饋一則**狀態註記**（狀態＋`reason_code`，無部分思緒）。park 時 parent 只收到一則冪等的狀態標記（鍵 `loop_status:{run_id}:{state}`，例如「等待核准」），讓對話層知道任務在途。

**park 與崩潰的持久化**：

- child 是 ContextManager 的**子 session**，由其 session store 持久化，鍵 `loop:<run_id>`；Core 的 checkpoint 只存 `child_context_ref`、`child_revision`、`history_hash`（Spike 2 的 S2-1 比對對象）。內容不複製進 Core checkpoint（私人內容走 ref，D33）。
- resume：以 `child_context_ref` 取回，比對 `history_hash`；**不一致或遺失 ⇒ fail-closed**（Run `FAILED(context_lost)`），不以不完整歷史續跑。（由 fork_manifest＋步驟帳本做有損重建是可能的後續選項，首版不做。）
- child 的 `scope_ref` ＝ Task 的 `scope_ref`；Run 終態後依保留政策（TTL，預設短）刪除。
- **假設待核實**：echo_thought_core 現有 `session_store` 的後端與耐久性是否滿足 D10（PostgreSQL 權威）尚未確認；若不滿足，需一層 Core 側的 blob 儲存（類 `params_ref`）。列入 §15。

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
    input_schema: Mapping[str, Any]    # JSON Schema 子集(§6.3、§5.7)，additionalProperties:false；params_hash 依此 canonical
    usage_hint: str = ""                # 使用時機文字(取代 think_llm 內 TOOLS_PROMPT/ABOUT_ME_RULE，C4)
    injected_params: tuple[str, ...] = ()   # Gateway 注入、LLM 看不到的欄位(PD-12)

@dataclass(frozen=True)
class ToolEntry:           # 註冊表的唯一單位
    name: str                           # [a-z][a-z0-9_.]{2,63}
    spec: ToolSpec
    descriptor: ToolDescriptor
    scope: ToolScope                    # 可見性維度(§5.6)：platforms／presence／requires_context／variants…
    result_scope: ScopeRef              # 結果項預設 scope（fail-closed=private）
    # --- 並行與執行(§5.7)；預設全部 fail-closed ---
    concurrency: Literal["parallel_safe", "parallel_one_shot", "serial"] = "serial"
    conflict_keys: tuple[str, ...] = () # 資源鍵範本，如 "file:{path}"；同鍵互斥
    timeout_s: float | None = None      # 非 serial 必填
    cancellable: bool = False           # executor 是否實作 cancel(D69)
    output_schema: Mapping[str, Any] | None = None   # receipt 結果項形狀；internal_read／reversible_local 必填
    composite: CompositeSpec | None = None           # 線性多步驟宣告；子步驟皆為既有 ToolEntry
    # --- 依賴與就緒(§5.8)；實際載入屬 module coordinator，註冊表只宣告 ---
    requires: tuple[str, ...] = ()      # 模組／資源 id，如 "gpu:indextts"
    load_cost: Literal["none", "light", "heavy"] = "none"
    warm_required: bool = False
    # --- 其他 ---
    degradable: bool = False
    schema_hash: str = ""               # 由 Registry 重算並與 lockfile/pin 比對，不信任手填
    provider_ref: str | None = None     # 平台端 local_name；LLM 不可見
    transport_timeout: float | None = None
    disabled: bool = False
    venue_policy: Mapping[str, Any] = field(default_factory=lambda: {"mode": "origin_venue"})
```

【奈留】`ToolSpec` 與 `ToolDescriptor` **分開**是刻意的：TaskManager 在 `FOR UPDATE` 交易內只該看到分類面（SYS 草案 §0.3 依賴反轉），description／schema 是 LLM 面，不該因為改文案而動到 CC-6 型別。`ToolEntry` 是 Part S 內部型別，不被 CC 引用。
【奈也】`description` 的調整頻率遠高於分類。因此規定：**改 `description`／`usage_hint` 不需升 `tool_version`**（不影響安全語意），但改 `input_schema` 或任何分類欄必須升版（進 `params_hash` 域，D55）。

`lookup_authoritative` 仍只在 executor 層（TG-4 不變）。

### 5.2 各 origin 如何進表

全部都產出同一種 `ToolEntry`，差別只在**誰撰寫、誰審核、何時載入**：

| origin | 例 | 撰寫與審核 | 載入 | executor | 備註 |
|---|---|---|---|---|---|
| `builtin` | 記憶工具（`deep_recall`…，現行白名單 3 個）、`lore.*`、`chatroom.*`、未來 `presentation.*` | 程式碼內宣告（`uep/tools/catalog/*.py`），PR 審核 | 組裝時 `registry.register(entry)` | Core 內 executor（記憶走 `memory_wiring`／Access Layer；Lore／Chatroom 為原生 adapter，憑證只在 executor 側） | **子系統提供的工具＝builtin＋`provider=core`**；子系統（echo_memory）內的 `TOOL_DECLARATIONS` 降為實作細節，不是註冊來源 |
| `mcp_static` | 經 MCP 傳輸的外部 server 的**已審核子集** | 設定檔列名單；Core 保存審核副本（description／input_schema 由人撰寫或審核後釘住） | 啟動時連線 `tools/list`，重算 canonical hash 與 `pinned_schema_hash` 比對 | MCP executor（憑證在 executor 側） | 動態發現的其餘工具進 `discovered`（不可見，D59）。T5（`echo-mcp`）永不註冊（C6） |
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

**目錄形式**：`builtin` 的 executor 邏輯在程式碼，但**宣告可放 YAML 目錄**（`uep/tools/catalog/*.yaml`）；YAML 只承載宣告（名稱、分類、`input_schema`／`output_schema`、`scope`、`concurrency`、`composite.steps`、`requires`），不含可執行內容，載入後仍走同一組 RA 斷言與 lockfile。這是對 legacy `workflow_definitions.yaml`「宣告式」概念的沿用（§5.7.1）。`decision.consult`（§4.9）亦以 `builtin` 進表，executor 由決策中樞提供。`composite` 條目與 `scope.variants` 的形式見 §5.6、§5.7。

### 5.3 enum＋註冊表層斷言

「enum 是詞彙，斷言是防線」。載入時（以及 CI）斷言：

| # | 斷言 | 來源／理由 |
|---|---|---|
| RA-1 | `origin`、`side_effect_class`、`idempotency`、`approval_level`、`interruptible`、`targeting` 皆屬 Literal 值域（執行期用 `typing.get_args` 檢查，不信任型別標註） | 現況 D91 三欄為裸 `str` |
| RA-2 | `external_effect`／`high_impact` ⇒ `approval_level≥per_action` | SYS 草案 §3.1 |
| RA-3 | LLM 可見 ⇒ `origin ∈ VISIBLE_ORIGINS`；`discovered`／`platform_declared` 永不投影 | 遷入計畫 §3.5 風險(3) |
| RA-4 | `executor` 名單雙向一致（TG-5）；`provider!="core"` ⇔ `targeting=platform_remote` | 沿用；切片 A 刻意分工：註冊表只檢查 provider／targeting 一致，executor 名單雙向一致由組裝層 TG-5 負責 |
| RA-5 | `interruptible!="non_interruptible"` ⇒ `cancellable=True`（executor 實作 `cancel`，D69） | 沿用 TC-4 |
| RA-6 | `capability` 屬閉集詞彙，且其**副作用上限**不低於該工具的 `side_effect_class`（§5.4） | 新增 |
| RA-7 | `input_schema` 通過 schema linter（§6.3）；`injected_params` 皆不在 LLM 面 schema 內 | 新增；切片 A 刻意延後：linter 暫不強制 `additionalProperties:false`（延到切片 B；A 要求衍生的 tools JSON 與舊 schema 位元組相等，現行記憶工具 schema 未宣告） |
| RA-8 | `tool_version` 與 `schema_hash` 對 lockfile 單調：hash 變而 version 不升 ⇒ 建構失敗 | §5.5 |
| RA-9 | 名稱不重複；所有 `name` 與 provider 的名稱限制相容（長度、字元集） | 新增 |
| RA-10 | `mcp_static` 的 executor 不得指向 `echo-mcp`（T5）命名空間 | C6 |
| RA-11 | `concurrency` 資格（§5.7.3）：`parallel_safe` ⇒ `internal_read`∧`approval_level=none`∧設有 `timeout_s`；`parallel_one_shot` ⇒ `side_effect_class∈{internal_read, reversible_local}`∧`approval_level=none`∧`idempotency∈{native, via_action_id}`∧`cancellable`∧設有 `timeout_s`∧非 composite；需核准者恆為 `serial` | Q-C，不靠名稱 |
| RA-12 | `conflict_keys` 範本的 `{param}` 皆存在於 `input_schema` 且為純量型別（path 類先 canonical）；`parallel_one_shot` 且非 `internal_read` ⇒ `conflict_keys` 非空（至少 `tool:{name}`） | Q-C，fail-closed |
| RA-13 | `scope.variants` 只覆寫 `description`／收窄後的 `input_schema`／executor 路由／`transport_timeout`；任何分類欄與基底不同 ⇒ 註冊失敗（§5.6） | Q-E |
| RA-14 | composite：子步驟皆為已註冊的非 composite 條目；線性；composite 自身 `serial`；宣告的 `side_effect_class`／`approval_level` ≥ 子步驟最大值 | Q-C |
| RA-15 | `requires` 的 id 若 module coordinator 未知 ⇒ 該工具 readiness 恆為 `unavailable`（fail-closed，啟動時警告）；`warm_required=True` ⇒ `requires` 非空 | §5.8 |
| RA-16 | `output_schema` 必填者（見 §5.7.2）缺失 ⇒ 註冊失敗；`input_schema`／`output_schema` 皆通過 linter | Q-C |

### 5.4 能力標籤（capability）

- **形式**：`<domain>.<verb_class>` 的**閉集**，宣告在程式碼的 `CapabilityTag` 列舉（新增標籤＝改程式碼＋測試＋審核）。例：`memory.read`、`memory.write`、`lore.read`、`lore.write`、`chatroom.read`、`chatroom.post`、`desktop.file_read`、`desktop.file_write`、`web.fetch`、`presentation.propose`、`decision.consult`。
- **一工具一標籤**（必填）。理由：投影變成簡單過濾，無「多標籤 AND／OR」歧義。route 預設表以標籤**集合**表達（決策層維護）。
- **標籤帶副作用上限**：`memory.read` 的上限是 `internal_read`，`chatroom.post` 的上限是 `external_effect`…（RA-6）。這樣即使決策層的 route 表寫成「給 `memory.read`」，也不可能因為某人把寫入工具誤標而讓寫入工具跟著曝光——**標籤授權範圍的語意被註冊表鎖住**。
- 無萬用字元、無階層繼承（不支援 `memory.*`）。【奈留】萬用字元是隱性擴權的典型入口。
- 標籤與 `scope.platforms`／`audience_min` 是兩回事：標籤決定「這類能力本輪開不開」，scope 決定「這個工具在此場域／受眾能不能用」。

### 5.5 版本與 schema_hash 漂移

- `schema_hash` ＝ canonical JSON（沿用 `params_hash` 的唯一 canonical 函式，TG-6）對 `{name, input_schema, side_effect_class, idempotency, approval_level, targeting}` 的 sha256。description 不入 hash（§5.1）。
- **兩道防線**：
  1. **lockfile（`uep/tools/tools.lock.json`，入版控）**：記 `name → {tool_version, schema_hash}`；CI 測試比對，hash 變而 version 未升 ⇒ 失敗（RA-8）。任何工具變更在 PR diff 上可見、可審。
  2. **執行期**：`mcp_static`／`platform_approved` 啟動與每次重連時重算外部回報的 canonical hash，與 `pinned_schema_hash` 比對；不符 ⇒ 該工具 `disabled`（fail-closed）、發 `tool.drift_detected`（非轉移型事件，payload 僅 `{tool, expected_prefix, actual_prefix}`），保持停用直到人工重審。LLM 面與驗證面一律用 Core 的審核副本，不用外部即時內容。
- 升版後已建立但未執行的舊版 action：`execute_action` 比對 `tool_version` 不符 ⇒ `ActionBlocked(tool_version_changed)`（D55，沿用）。

### 5.6 scope 維度：全域工具與平台適配（Q-E）

記憶工具之後要依本契約重構為**全域工具**；同一工具會依平台適配。scope 因此是 `ToolEntry` 的一等維度，且必須與 D91 的 `targeting` 分開：

| 維度 | 回答的問題 | 在哪個階段使用 | 欄位 |
|---|---|---|---|
| **scope（可見性）** | 這個工具**在什麼場域／平台／Presence 條件下被提供**給模型？ | **投影期**（`registry.project`） | `ToolScope` |
| **targeting（執行位置）** | 這個工具**由誰、在哪裡執行**？ | **執行期**（`execute_action` 派送） | `ToolSpec.targeting`（`core_local`／`platform_remote`）＋ `provider` |

兩者獨立，可任意組合：

| 例 | scope | targeting | 說明 |
|---|---|---|---|
| `deep_recall`（記憶） | `global` | `core_local` | 所有平台都提供；Core 內執行 |
| `member_recall` | `global`＋`requires_context={"guild_roster"}` | `core_local` | 取代 `host._refresh_tools` 內「有 guild 才曝光」的硬編碼 |
| `desktop.file_read` | `platform:desktop` | `platform_remote` | 只在 Desktop 場域提供，由 Desktop 端 executor 執行 |
| `notify.user`（假設） | `global`（以 variants 適配） | `platform_remote`（依 variant 選平台 executor） | 同名工具，Desktop 彈通知、Discord 送訊息 |
| `presentation.propose` | `global`＋`presence_bound` | `core_local` | 離場即不提供 |

```python
@dataclass(frozen=True)
class ToolScope:
    platforms: frozenset[str] = frozenset()        # 空集合＝global；否則 {"desktop","discord","website"} 的子集（"platform:<name>"）
    presence: Literal["any", "bound"] = "any"      # bound ⇒ 僅 Presence ACTIVE 且在該場域時提供（與 ToolSpec.presence_bound 同源，不可矛盾）
    requires_context: frozenset[str] = frozenset() # 投影所需的脈絡條件，例如 {"guild_roster"}、{"private_audience"}
    data_scope: ScopeRef = "private"               # 沿用 SYS 草案 §3.1
    audience_min: AudienceLevel = "private"        # 沿用
    variants: Mapping[str, ToolVariant] = field(default_factory=dict)  # key＝平台名；缺 key ⇒ 使用基底定義
```

**投影篩選**（接 §6.1）：`entry.scope.platforms == ∅ ∨ ctx.platform ∈ entry.scope.platforms`，且 `requires_context ⊆ ctx.available_context`，且 `presence=="bound" ⇒ ctx.presence.active_at(ctx.platform)`，且 `audience ≥ audience_min`。`ctx.platform` 取自**當下 Presence 所在場域**（政策快照），不取自模型輸入。

**同一工具在不同平台的適配（`ToolVariant`）**——規則：

1. 同名工具只有**一份分類**。`side_effect_class`、`idempotency`、`approval_level`、`interruptible`、`presence_bound`、`tool_version`、`capability` 只在基底定義（`resolve(tool)` 與連線／平台無關，PD-4）。**變體不得改分類**；分類不同＝不同工具，另取名稱（例如 `desktop.file_write` 與全域的 `file.write` 不是變體關係）。
2. 變體**只能**覆寫四類欄位：`description`（平台用語）、`input_schema`（只能收窄：收窄 enum、增加 `injected_params`，不得新增必填欄位或放寬型別）、`provider_ref`／executor 路由（對應平台端 executor）、`transport_timeout`。
3. 變體被選中時，`capability_set` 的條目帶 `variant="<platform>"`；`tool_intent` 回到 Gateway 時，**以 `capability_set` 查回變體，不信任模型自帶**；Gateway 以變體的 schema 驗證並注入 `injected_params`（例如 `venue`）。
4. 每個變體各自計算 `schema_hash` 並進 lockfile（鍵為 `name@variant`）；基底 `tool_version` 不變的前提下，變體 schema 改動仍須更新 lockfile（因為它改變 LLM 所見與驗證面），但不影響既有 approval（approval 綁基底 `{tool, v, params}`）。
5. 缺少當前平台變體、且基底定義不適用於該平台（`platforms` 非空且不含當前平台）⇒ 不投出（fail-closed），不靜默降級到別的平台變體。

**與 D91 的區分**：D91 的 `origin=platform_approved`／`provider`／`targeting=platform_remote` 描述「工具由平台端執行」；scope 描述「何時把工具給模型看」。`platform_approved` 的工具其 scope 通常是 `platform:<該平台>`，但這是慣例而非約束，RA 不強制。

### 5.7 並行資格、衝突鍵、schema 嚴格化與 composite（Q-C）

**5.7.1 legacy 組裝器評估**（`legacy-final-v0.9.4` 的 `sys_module/workflows.py`、`step_templates.py`）：**類別與引擎不沿用**。理由：狀態在記憶體（崩潰即失）、資料靠字串 key 傳遞（無 schema）、核准繞過 ActionIntent、沒有任何並行／冪等／衝突欄位、`ConditionalStep` 依 SYS 草案 §7.2 已定案不承襲。**沿用三個概念**：(1) YAML 宣告名稱／分類／參數 schema；(2) `initial_params` 預填、使互動步驟可跳過；(3) 必要／可選輸入。這三者在新契約中分別落為：目錄檔案格式（§5.2）、composite 的 `initial_params`（§5.7.4）、`input_schema.required`。

**5.7.2 新欄位**（`ToolEntry`）：

| 欄位 | 型別 | 預設 | 說明 |
|---|---|---|---|
| `concurrency` | `Literal["parallel_safe","parallel_one_shot","serial"]` | **`serial`**（fail-closed） | 並行資格；**由註冊表驗證，不靠名稱或模型自述** |
| `conflict_keys` | `tuple[str, ...]` | `()` | 資源鍵**範本**，以 `{param}` 引用 `input_schema` 欄位，例如 `file:{path}`、`lore:note:{id}`、`tool:{name}`。Gateway 在驗證與 canonical 之後代入，得到實際資源鍵；**同鍵互斥** |
| `timeout_s` | `float` | 設定項預設 | 單次執行上限；非 `serial` 必填 |
| `cancellable` | `bool` | `False` | executor 是否實作 `cancel`（D69）；與 `interruptible` 關係見 RA-5 |
| `input_schema` | JSON Schema 子集 | 必填 | 即初稿與 SYS 草案 §3.1 的 `params_schema`（更名）。**嚴格化**：`additionalProperties:false`、所有欄位有明確型別、整數與浮點不混用、`x-uep-type` 標註 path／url／actor_ref／scope_ref |
| `output_schema` | JSON Schema 子集 | 必填（`internal_read`／`reversible_local`）；其餘建議 | 描述 receipt `result_ref` 內容的**項目**形狀；Gateway 在 `filter_tool_result` 前驗證，不符 ⇒ `failed`＋`degraded`，不交給模型 |

**`params_hash` 依 `input_schema` canonical**：Gateway 在驗證通過後、計算 `params_hash` 前，依 schema 做**型別導向正規化**——補上 schema 的 `default`（使「省略」與「明寫預設值」同雜湊，核准所見＝實際執行）、`x-uep-type: path` 做 canonicalization 與 NFC、注入 `injected_params`、拒絕整數欄位的浮點形式與超過 2^53 的整數（D53 建議）。雜湊函式仍是 `uep/tasks/manager.py` 的唯一 `params_hash`（TG-6），Gateway 只負責餵入已正規化的 params。

**5.7.3 並行資格定義**

- **`one_shot` 的定義**：**單一 ActionIntent；沒有 `WAITING_INPUT`；只執行一次（無工具內部重試迴圈，重試必須是新 action）；結果全在 receipt（無需後續互動才能取得）**。
- 資格由 `concurrency` 與分類**共同**驗證（RA-11），不靠名稱：

| `concurrency` | 要求 | 典型 |
|---|---|---|
| `parallel_safe` | `side_effect_class == internal_read` ∧ `approval_level == none` ∧ 設有 `timeout_s` ∧ `output_schema` | `deep_recall`、`lore.recall`、`chatroom.read` |
| `parallel_one_shot` | `side_effect_class ∈ {internal_read, reversible_local}` ∧ `approval_level == none` ∧ `idempotency ∈ {native, via_action_id}` ∧ `cancellable` ∧ 設有 `timeout_s` ∧ 非 composite ∧ 非空 `conflict_keys`（寫入者）；`input_schema`／`output_schema` 皆嚴格 | 單檔 Desktop 動作、`force_remember` |
| `serial`（預設） | 無 | 其餘全部，包含所有 `external_effect`／`high_impact`、所有需核准者、所有 composite |

- **需核准者永遠 `serial`**：核准會讓 Run park，不能與並行批次共存。
- TC-3 修訂（見 §12）：由「同 Run 至多一個 EXECUTING」改為「EXECUTING 集合要麼恰好一個 `serial` 動作，要麼全部為通過資格的並行動作且資源鍵兩兩不交」。

**5.7.4 多步驟工具：composite**

- 由註冊表**宣告**為 composite：`ToolEntry.composite: CompositeSpec`，其 `steps` 的每一步都**引用一個既有 ToolEntry**（子步驟本身是普通工具，各有分類、schema、核准）。
- composite 是**宣告式巨集，不是引擎**：模型呼叫 composite 時，Agent 層將其展開成**依序**的子步驟，**每個子步驟各自走完整 ActionIntent**（各有 `act_`、receipt、核准、`params_hash`；子 action id ＝ `stable_action_id(run, step, ordinal, sub_index)`）。composite 本身不另建 ActionIntent（避免雙重記帳）。
- **composite 本身 `serial`**，且不得巢狀。composite 的 `side_effect_class`／`approval_level` **取子步驟最大值**；宣告值不得低於計算值（RA-14），僅用於投影與稽核。
- **首版只做線性**：步驟 k 的輸入可引用 composite 輸入或前序步驟 receipt 的輸出欄位（受 `output_schema` 約束的受限路徑語法，不是任意表達式）；**分支、迴圈、條件一律交給 Agent 迴圈**（即 `ConditionalStep` 不承襲）。
- 失敗語意：步驟 k 失敗或 `UNKNOWN` ⇒ 後續步驟不執行，composite 結果以逐步狀態列為 `items`，整體 `status` 取最差者；**已成功的步驟不重播**；崩潰或 park 後從 checkpoint 的 `composite_cursor` 繼續。
- `initial_params`（沿用概念）：composite 可宣告預填值，使本來需要互動詢問的欄位不必再問；缺少必要輸入 ⇒ `rejected(invalid_tool_args)`＋`fields`，由 Agent 迴圈決定是否向使用者詢問（`decision.consult(kind=ask_user)`，§4.9），**工具本身不進 `WAITING_INPUT`**。

```yaml
# uep/tools/catalog/desktop_prepare_notes.yaml（示意；YAML 只承載宣告，執行仍由程式碼 executor）
name: desktop.prepare_notes
kind: composite
capability: desktop.file_write
scope: { platforms: [desktop], presence: bound }
input_schema:
  type: object
  additionalProperties: false
  required: [source_path]
  properties:
    source_path: { type: string, x-uep-type: path }
    title: { type: string, maxLength: 120, default: "筆記" }
steps:
  - tool: desktop.file_read
    params: { path: "$.input.source_path" }
  - tool: desktop.file_write
    params: { path: "$.input.source_path", content: "$.steps[0].output.text" }
```

### 5.8 requires、readiness 與 module coordinator 介面

**歸屬**：模組的實際載入／預熱／卸載、以及日後可能加入的補釘（patch）機制，由獨立的 **module coordinator＋其 registry** 負責（決策層 Q5＝A，艾斯維爾裁決）；決策中樞只**提議**預載。**module coordinator 與它的 registry 本身不在本提案範圍，只定義介面與歸屬——標註「待 module coordinator 設計」**。**工具註冊表不執行 load／warm／unload。**

**`ToolEntry` 的宣告**：

| 欄位 | 型別 | 預設 | 說明 |
|---|---|---|---|
| `requires` | `tuple[str, ...]` | `()` | 依賴的**模組／資源 id**（由 module coordinator 命名空間定義，例如 `gpu:indextts`、`subsystem:echo_memory`、`platform:desktop`） |
| `load_cost` | `Literal["none","light","heavy"]` | `"none"` | 載入成本分級，供決策層預載提議與投影的 warming 處置參考 |
| `warm_required` | `bool` | `False` | 是否需預熱才能首次呼叫（`True` ⇒ 模組需達 ready 之前不可用） |

**事件介面**（module coordinator → 註冊表；名稱與 payload 為本文擬定，**待 module coordinator 設計時確認**）：`module.ready`／`module.warming`／`module.unavailable`，payload `{module_id, revision, reason_code?}`；`revision` 單調遞增，註冊表以**最新 revision** 為準（亂序、重送皆可容忍）。

**readiness 推導**（註冊表維護，記憶體內，不做 I/O）：對每個工具，`readiness = AND over requires`：全部 `ready` ⇒ `ready`；任一 `unavailable` ⇒ `unavailable`；否則（有 `warming`、無 `unavailable`）⇒ `warming`。`requires=()` 恆為 `ready`。`requires` 中出現 module coordinator 未知的 id ⇒ `unavailable`（fail-closed）並於啟動時記警告。D91 的 `executor_ready(tool)`（遠端 executor 在線）可視為同一機制的特例（`platform:<id>` 模組），是否合併待 D91 實作時確認。

**投影與 warming 處置**：
1. **只投出 `ready` 的工具**（§6.1）。
2. `warming` 的處置：**預設不投出**，模型不知其存在；但該工具若屬 Run 凍結的能力標籤之內（AL-3 允許它之後出現，因為不擴大範圍），`capability_set` 附帶 `pending: [{tool, reason: "warming"}]` 供 AgentLoop 讀取。AgentLoop 可選擇：(a) 繼續不用；(b) `park(await_ready:<tool>)`，受 `warm_wait_budget` 限制，逾時視為不可用繼續。**不允許**把 warming 工具投出給模型並讓呼叫端阻塞等待（延遲不可預期，且與 `step_timeout` 衝突）。
3. `unavailable`：不投出；不報 pending。

**執行前再檢查**：`ToolGateway.check_intent` 在 capability 檢查之後、`create_action` 之前，**再查一次 readiness**。已被投出的工具在執行前轉為非 `ready` ⇒ 回 `blocked`＋`reason_code=tool_not_ready`，**不建 action**，並計入無進展預算。（`execute_action` 階段 1 的 `resolve(tool)` 仍與 readiness 無關，PD-4 不變。）

**patch 與版本**：module coordinator 的 patch 若改變工具的 `input_schema`／分類欄，**必須依 §5.5 升版**（`tool_version`、`schema_hash`、lockfile）；註冊表收到的條目更新若 `schema_hash` 變而 `tool_version` 未升 ⇒ 拒絕並將該工具 `disabled`（RA-8 延伸到熱載入路徑）。純實作層 patch（schema 與分類不變）不需升版，但 readiness 事件應反映重載期間的 `warming`。

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
                  ∧ scope 篩選（§5.6）：platforms==∅ ∨ ctx.platform ∈ platforms
                      ∧ requires_context ⊆ ctx.available_context
                      ∧ presence=="bound" ⇒ Presence 在 ctx.platform ACTIVE
                      ∧ audience ≥ audience_min
                  ∧ readiness(tool)=="ready"（§5.8；含 D91 executor_ready 的特例）
        選中時帶 variant（若 scope.variants 有該平台）
```

`capability_set`（`RuntimeRequest.capability_set`，TG-12 形狀沿用並擴充）：

```python
{"tools": [{"tool": str, "version": int, "description": str, "input_schema": Mapping,
            "variant": str | None}],        # variant＝平台適配名；回到 Gateway 時以此查回，不信任模型自帶
 "pending": [{"tool": str, "reason": "warming"}],   # 凍結範圍內、尚未 ready 的工具(§5.8)；不給 LLM
 "revision": int,            # 投影版本，審計與 resume 比對用
 "tags": ["memory.read", ...]}   # 已收窄的標籤集合（checkpoint 凍結用）
```

不含 executor、憑證、分類細節、`injected_params`。`pending` 只給 AgentLoop，不渲染給模型。

**介面歸屬**：左半（標籤集合的產生）歸決策層；右半（`project`、Gateway、render）歸工具層。兩者的介面只有「`capability_tags: frozenset[str]` ＋ 快照 ＋ 場域脈絡」。

### 6.2 D60 硬規則落點

> 送給 provider 的 tools **恰等於** `capability_set` 的渲染；除註冊表投影外，任何程式碼不得產生 tools JSON。

- **唯一產生點**：`uep/tools/render.py::render_tools(capability_set, dialect)`；Runtime 實作與 `ModelPort.render_tools` 只呼叫它（或其 dialect 轉譯）。
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

### 6.4 平行排程規則（Q-C）

排程器是 `AgentLoop` 內的**純函式** `plan_waves(batch, registry, policy) -> list[Wave]`（輸入相同則輸出相同，可單元測試）；實際執行仍逐個走 `create_action`／`execute_action`。

**步驟**：

1. **批次**＝同一則模型訊息內、`args_complete=true` 的全部 `tool_intent`，依 `index` 排序。批次大小受 `max_batch`（Policy）限制；超出者以 `blocked(reason_code=deferred_batch_limit)` 回餵，不執行。
2. **逐項前置**（每項獨立）：`capability_set` 檢查 → readiness（`tool_not_ready`）→ schema 驗證 → 正規化與 `params_hash`。任一失敗 ⇒ 該項立即 `rejected`／`blocked` 回餵，**不進入分組**，也**不影響其他項**。
3. **資格判定**：該項「可並行」⇔ `concurrency ∈ {parallel_safe, parallel_one_shot}` ∧ `approval_level == none` ∧ Presence 條件已滿足。其餘一律 `serial`。**資格由註冊表欄位與分類決定，不看工具名稱，也不看模型的 `side_effect_hint`**（TG-2）。
4. **代入資源鍵**：以驗證後的 params 代入 `conflict_keys` 範本，得到該項的實際資源鍵集合 `K(i)`（例：`file:/home/x/a.txt`）。
5. **貪婪分組（保序）**：依 `index` 掃描，維護「當前波（wave）」：
   - `serial` 項（含所有需核准者、composite）⇒ **關閉**當前波，自己成為單獨一波（屏障），其後另開新波。
   - 可並行項：若 `K(i)` 與當前波內任何成員的資源鍵相交（**同鍵互斥**）⇒ 關閉當前波並以它開新波；否則加入當前波。
   - 結果：波與波**依序**執行；同波成員**並行**執行；有衝突的項依 `index` 先後**序列化**，不會被重排到更前面。
6. **執行**：波內先對所有成員 `create_action`（`action_id` 由 `(run, step, ordinal)` 決定，SA-4），再並行 `execute_action`，並行度受 `max_parallel`（Policy）限制；每項受自己的 `timeout_s` 約束。
7. **回餵順序**：結果一律**依 `index` 順序**寫入 history 並交給模型（確定性，利於 Spike 2 的 hash 比對）；完成較早者先暫存，波結束後一併交付（D61 的 `submit_tool_result` 仍是逐筆，由 Runtime 暫停到全部到齊再續）。

**單一失敗如何回填與影響**：

| 情況 | 該項回填 | 對同批其他項 |
|---|---|---|
| 驗證失敗／`CAPABILITY_MISSING`／`tool_not_ready` | `rejected`／`blocked`＋`reason_code`（`fields` 不含值） | 無影響 |
| 執行失敗（`FAILED`） | `failed` | **不取消**同波兄弟（彼此獨立）；後續波照常 |
| 逾時 | 先 `executor.cancel`：`stopped` ⇒ `failed(reason_code=timeout)`；`isolated`／無回應 ⇒ action 轉 `UNKNOWN`，`unknown` | 同波兄弟照常；見下一列 |
| 出現非 `internal_read` 的 `UNKNOWN` | `unknown` | **同批尚未開始的項**一律 `blocked(reason_code=unknown_unresolved)`（TC-3：未決副作用先解決）；已在執行者讓其完成 |
| 需核准的項（屏障） | 先完成其之前所有波；該項 park（§8.2） | 其後同批項 `blocked(reason_code=deferred_by_approval_wait)`，不執行；resume 後由模型重新規劃 |
| `StaleWorkerEpoch`／取消／deadline | — | 對所有在途項 `executor.cancel`；已發生的 receipt 照常入庫（R-2）；未開始者不再執行 |
| 預算將盡 | 依剩餘 `max_tool_calls` 截斷 | 超出者 `blocked(reason_code=deferred_budget)` |

**驗證要點**（切片 D 的測試依據）：同一輸入批次重複排程得相同的波；衝突鍵相交者必定落在不同波且保持 `index` 先後；任一 `serial` 或需核准項前後各為屏障；單一失敗不改變兄弟項的結果；回餵順序與 `index` 一致。

### 6.5 並行與 TC-3 的 DB 強制（設計要點，待實作定案）

- 現行規則「同 Run 至多一個 EXECUTING」＋ DB 部分唯一索引 `UNIQUE(run_id) WHERE status='EXECUTING'`（D62）與並行**衝突**。修訂為：(a) 應用層，在 `execute_action` 階段 1 已鎖定 Run 列的前提下檢查——EXECUTING 集合要麼**恰好一個 `serial` 動作**，要麼**全部為通過資格的並行動作且資源鍵兩兩不交**；(b) 因 Run 列已 `FOR UPDATE`，同 Run 內的檢查不會競態；(c) **跨 Run 的資源鍵**（兩個 Run 同時寫同一檔案）需要資源鎖表（`resource_key` 唯一、隨 action 終態釋放）——鍵前綴標明作用域（`run:`／`global:`）；此表的 schema 與 migration 待實作設計。原部分唯一索引移除。這是對 D62 的實質修改，列於 §12。

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

### 7.2 模型層：`ModelPort` 與 SDK 研究結論（Q-F，**已裁決**）

艾斯維爾：「可以的話直接上 SDK，但 UEP 有它的特性，可能無法整套沿用，要多做研究比較。」

**判準**：UEP 的特性是 Core 持有唯一權威狀態（D1、INV-04）：Run／Step／checkpoint／ActionIntent／Approval 在 PostgreSQL，由 TaskManager 單寫者（`worker_epoch`）管理；工具執行與核准走 ActionIntent；結果依 audience 過濾後才回模型。凡是「自帶一份執行狀態或歷史、並自行決定何時執行工具」的框架，都會形成**第二份權威狀態**。

| 候選 | 能提供什麼 | 與 UEP 的衝突 | 結論 |
|---|---|---|---|
| OpenAI Agents SDK（暫定 openai-agents 0.24.0） | Agent loop、session、tracing、handoff | `RunState`／session 是第二份 history 與執行狀態；工具由 SDK 執行；hosted tools；trace 外送需另管；與 10-25 §14.4「單一歷史擁有者」衝突 | **不整套沿用**；可作 10-25 §8.2 的比較基準組 |
| LangGraph | 圖式 checkpoint | checkpointer 是第二份 checkpoint 權威，與 TaskManager `save_checkpoint` 重複（10-25 §8.2 已警告） | **不沿用** |
| Claude Agent SDK | Claude 的 agent harness | 只支援 Anthropic；綁 CLI 行程；不符合多 provider 與 Ollama 需求 | **不沿用** |
| **PydanticAI**（暫定 pydantic-ai-slim 2.65.0） | 多 provider 的 Model 抽象；串流 part 事件；外部工具集與延遲工具請求 | 若用其 `Agent` 預設行為會自行執行工具、自管 message history | **只用其模型層**：Model／provider＋串流 part 事件，取代自寫 `ProviderAdapter` 的實作（下述） |

**採用形狀**（以下對 PydanticAI 的具體 API 名稱取自艾斯維爾裁決與房內研究結論，**本文作者未獨立核對當前官方文件**，列入 §15；Spike 1／2 的目的之一就是驗證）：

1. **Core 自有埠 `ModelPort`**（即 §7.2 初稿的 `ProviderAdapter` Protocol，保留為 Core 內部介面，`ProviderCapabilities` 能力表亦保留）：`render_tools`／`render_tool_result`／`stream`。Agent 層、Gateway、TaskManager **只依賴 `ModelPort`**，不見任何 SDK 型別。
2. **`uep/models/` 內的實作二選一**：(a) PydanticAI 之上的適配器（首選，Spike 1／2 通過為採用門檻）；(b) 自寫 per-provider adapter（OpenAI chat／Responses／Ollama，沿用現有 backends；Anthropic 預留）。SDK 只允許出現在這個套件（import-boundary 測試強制；邊界已由艾斯維爾確認）。
3. **工具全部走外部工具集**（PydanticAI 的 `ExternalToolset`）：SDK 不持有可執行函式，只知道名稱與 schema（來自 `capability_set` 渲染）。
4. **遇到工具呼叫即結束 run**（以 `DeferredToolRequests` 回報待處理的工具呼叫）：對應 `tool_intent` 事件與 park／resume——SDK 的 run 在每個工具呼叫處結束，Core 執行 ActionIntent（或 park 等核准）後，以**新的 run** 帶 Core 持有的 history＋工具結果繼續。這正是 D61 的 B 路徑；A 路徑（同 attempt 續跑）在此實作下由「連續的短 run」模擬，對 `Runtime` 協定而言仍是 `submit_tool_result`。
5. **串流事件對映**：`ThinkingPartDelta` → `thought_delta`；`TextPartDelta` → `text_delta`（`direct_chat`）／`result` 的文字（`agent_loop`）；工具呼叫 part → `ToolCallAssembler` → `tool_intent`。
6. **D60 斷言自寫**（不信任 SDK）：送往 SDK 的工具名稱集合 ＝ `capability_set` 名稱集合；SDK 的 `builtin_tools` 必須為空；任何非 function 工具 ⇒ 拒絕啟動 attempt（`CAPABILITY_MISSING`）。
7. **history 由 Core 組裝**（Q-D：ContextManager 持有，迴圈內走平行 context，§4.10）；SDK 不保存跨 run 的 message history。

`ProviderCapabilities` 與 `ToolCallAssembler`（初稿設計）保留：

```python
class ModelPort(Protocol):
    dialect: Literal["openai_chat", "openai_responses", "ollama", "anthropic"]
    caps: ProviderCapabilities     # text_stream, tool_calling, parallel_tool_calls, structured_output,
                                   # tools_with_reasoning(gpt-5.6-luna 的 chat 路徑為 False), cancel_semantics,
                                   # context_limit, data_egress_policy, schema_subset
    def render_tools(self, tools: Sequence[ToolView]) -> list[dict]: ...
    def render_tool_result(self, call_id: str, view: Mapping) -> ModelMessage: ...
    def stream(self, req: ModelRequest) -> AsyncIterator[ModelEvent]: ...
    # ModelEvent: thought_delta | text_delta | tool_call_delta(index, call_id, name, args_fragment)
    #           | tool_call_done(index) | usage | finish(reason: stop|tool_calls|length|error)
```

`ToolCallAssembler`（provider 無關，Core 內）：累積 `tool_call_delta` → `finish` 時解析 JSON → 產生 `tool_intent`。增量期間只在 Agent 緩衝，**不**建 action（TC-2、SA-3）；JSON 不合法、或 `finish_reason=length` 截斷在 args 中途 ⇒ 回 `invalid_tool_args`，**絕不猜測補值、絕不執行**，以 `rejected` 回餵模型，最多重試 N 次（計入無進展預算）。provider 不支援的能力由 adapter **明示降級並記錄**。路由（10-25 §8.3）：資料外傳限制 → 必需能力 → 可用性 → 延遲／成本 → 偏好；無滿足者明確失敗；fallback 發生在**新 attempt**，已有副作用者先查 receipt。

| 面向 | OpenAI chat | OpenAI Responses | Ollama | Anthropic（預留，不建） |
|---|---|---|---|---|
| tools 形狀 | `{"type":"function","function":{name,description,parameters}}` | 攤平：`{"type":"function",name,description,parameters}` | 同 chat 風格 | `{name,description,input_schema}` |
| 增量 args | `tool_calls[].function.arguments` 分塊（首塊帶 id/name） | function_call arguments delta 事件 | 通常整塊、無 id | `input_json_delta`；`tool_use` block 帶 id |
| 結果回餵 | `role=tool, tool_call_id` | `function_call_output, call_id` | `role=tool` | `tool_result` block（`tool_use_id`） |
| 平行呼叫 | 同一訊息多個 tool_calls | 多個 function_call item | 視模型 | 同訊息多個 `tool_use` block |
| 內部對映 | `call_id←id` | `call_id←call_id` | `call_id←合成` | `call_id←id` |

### 7.3 串流、多輪、平行、錯誤

- **串流中的 tool_intent**：agent_loop 模式下推理文字走 `thought_delta`（Q-B，已裁決；目前只輸出 log／終端，UI 延後），說只來自 `result`／通知 intent；`direct_chat` 的 `text_delta` 照舊進句子流。
- **多輪**：由 AgentLoop 的 Step 迴圈驅動，不再由 adapter 內的 `max_tool_rounds` 驅動；adapter 內保留 `max_internal_rounds` 僅作保險絲。
- **平行呼叫**：同一模型訊息的多個 `tool_intent` 由 **§6.4 排程規則**分組處理（Q-C 已裁決：唯讀與一次性任務工具可並行，其餘序列化）。
- **錯誤回填**：`failed`／`rejected`／`blocked`／`unknown` 皆經 D61 視圖回餵，只帶短碼，不帶例外文字或參數值。`UNKNOWN` 不得讓模型以新 action_id 重送同語意操作（SA-11）。
- **args 不完整**：`args_complete=false` 不建 action；組裝完成後重驗（TC-2、SA-3 沿用）；Core 重啟後不補提交（VC-S3-1）。

### 7.4 Spike 1／2（採用 SDK 模型層的門檻）

**Spike 1（切片 C 之前；模型層）**——在 OpenAI chat、OpenAI Responses、Ollama 三種 provider 下全數通過才採用，任一項失敗 ⇒ 回到自寫 adapter：

| # | 驗收 |
|---|---|
| S1-1 | SDK **執行工具次數 ＝ 0**（所有工具僅以 tool_intent 回報，由 Core 執行） |
| S1-2 | **想／說零混入**：`thought_delta` 內容不出現在 `text_delta`／`result`，反向亦然（含串流邊界測試） |
| S1-3 | 首 token 額外延遲 ≤ 上限（**50 ms，艾斯維爾已裁決**）；基準為現有直達 backend |
| S1-4 | 取消後 **500 ms 內**停止產生事件 |
| S1-5 | 內建工具被斷言擋下（`builtin_tools` 非空、非 function 工具、工具名稱集合 ≠ `capability_set` 皆拒絕啟動） |

**Spike 1 結果（2026-11-03，pydantic-ai-slim 2.65.0；`feature/spike1-pydantic-ai`；量測腳本 `scripts/spike1_model_layer.py`，離線測試 `tests/uep/test_models_port.py`／`test_models_boundary.py`）**

| # | 結果 | 數據 |
|---|---|---|
| S1-1 | 通過（三種 provider） | SDK 執行工具 0 次；Core mock executor 各 1 次；第二輪帶工具結果回餵（新 run）三者皆正常收尾 |
| S1-2 | 通過 | Responses（`reasoning_summary=auto`）與 Ollama gemma4（`thinking=True`）都有 thought_delta，事件序上 thought 全在 text 前，thought 開頭未出現在 text；OpenAI chat 不產生 thinking（照實記錄）。Ollama 的 thought 內會草擬最終答案（內容重複，非串流混入） |
| S1-3 | 通過但餘裕小 | 中位數差（新－現行 openai.py，N=10 交錯）：chat +4.3 ms（828.2 vs 823.9，p90 1236.7 vs 1341.6）、Responses +40.3 ms（906.2 vs 865.9，p90 998.6 vs 950.2）。逐對差的中位數 chat +58.5／Responses +30.4 ms；網路抖動（約 ±300 ms）遠大於 SDK 層。無網路時 SDK 層到首個 text_delta 約 2.6 ms（median，FunctionModel） |
| S1-4 | 通過 | cancel() 到串流結束：chat median 3.9／max 5.0 ms、Responses 1.8／8.7 ms、Ollama 0.8／1.0 ms（各 N=3）；取消後內容事件 0 |
| S1-5 | 通過 | 內建工具、少給、多給、畸形 capability_set 皆在送出前被擋（`CAPABILITY_MISSING`／`ValueError`），不送請求 |

判定：**採用 PydanticAI 作模型層**（`uep/models/`）。限制見 §15。

**Spike 2（切片 D 之前；park／resume）**：

| # | 驗收 |
|---|---|
| S2-1 | park／resume **崩潰重建後 history 的 hash 一致**（Core 重組 history 與崩潰前逐訊息相等） |
| S2-2 | **重複核准只產生一次副作用**（重送核准／重放 resume 不重複執行 action） |
| S2-3 | resume 後的**工具集合 ⊆ 凍結集合**（AL-3） |

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

按**行為**切，每片可獨立驗收與回退；TL 編號為對映，不再作為排程單位。**Spike 1 在 C 之前、Spike 2 在 D 之前**（兩者決定模型層走 SDK 還是自寫 adapter，見 §7.4）。

| 切片 | 內容 | 對映 | 前置 | 驗收條件 |
|---|---|---|---|---|
| **A 註冊表單一來源** | `ToolSpec` 擴欄＋Literal 列舉；`ToolDescriptor`／`ToolEntry`（含 §5.1 全欄：`scope`、`concurrency`、`conflict_keys`、`timeout_s`、`cancellable`、`input_schema`／`output_schema`、`requires`／`load_cost`／`warm_required`）；`ToolRegistry`（RA-1～14，欄位先「宣告並驗證」，排程與 readiness 訂閱尚不啟用）；lockfile 與 CI 測試；3 個記憶工具（現行 `TOOL_CLASSES` 白名單：`deep_recall`／`force_remember`／`member_recall`）以 `builtin`、`scope=global` 進表（分類取自 `TOOL_CLASSES`，schema 取自 `tool_definitions()` 現值；about-me 三工具、`adjust_importance`、`trigger_dream` 等不在白名單內，進表留待後續切片），`tool_definitions()`／`TOOL_CLASSES`／`host._refresh_tools` 改由註冊表**衍生**（行為不變） | TL0＋TL1 | N5 | 衍生的 tools JSON 與舊 `think.tools` **位元組級相等**（黃金測試）；`access.py` 無獨立 schema 字面；RA 斷言測試全綠；未宣告 `concurrency` 的條目預設 `serial`（fail-closed 測試）；`tests/uep` 與 import-boundary 不退步 |
| **B 投影與 D60 斷言** | `registry.project`（含 scope／平台／Presence／ready 篩選，§6.1）、`render_tools`、Runtime 啟動斷言；Gateway schema 驗證＋`injected_params`＋`reason_code` | （新）TG-12 | A | 反例：多給／少給 tool 名稱 ⇒ `CAPABILITY_MISSING`；`$ref` schema 註冊失敗；缺必填／多餘欄位 ⇒ `rejected`＋`fields` 不含值；同一工具在不同 `(platform, presence)` 下投影結果符合 §5.6 表；readiness 非 `ready` 的工具不被投出、`warming` 依 §5.8 處置（以 fake module coordinator 事件驅動）；靜態測試證明無手寫 tools 清單。**延後（已落地範圍之外）**：`render_tools` 的 Responses／Ollama 方言 → Spike 1／C；`ToolVariant.injected_params` → G；`x-uep-type` 的 path 正規化與 url SSRF 檢查 → G；Gateway 執行前再查 readiness（`tool_not_ready`）→ F；`host._refresh_tools` 改用 `project` 與刪除 `think_llm.tools` → C；Runtime／host 尚未接線（`check_tools_match_capability`、`render_tools`、`project` 目前無 production 呼叫端）→ C |
| **Spike 1 模型層** | PydanticAI 的 Model／provider＋串流 part 事件，包在 `uep/models/` 內（邊界已裁決：SDK 只能出現在 `uep/models/`）。三種 provider：OpenAI chat、OpenAI Responses、Ollama | Q-F | B | 見 §7.4：SDK 執行工具次數＝0；想／說零混入；首 token 額外延遲 ≤ 50 ms（已裁決）；取消後 500 ms 內停止；內建工具被斷言擋下。**任一項失敗 ⇒ 退回自寫 `ProviderAdapter`** **Spike 1 已通過（2026-11-03，見 §7.4 結果），採 PydanticAI** |
| **C internal_read 走 ActionIntent** | 第一個 `Runtime` 實作（依 Spike 1 結果：PydanticAI 模型層，或自寫薄層）；`direct_chat` 回合的記憶工具改經 `AgentCore.run_step` → create/execute_action；回合 Task 懶建立（D62） | TL2 | Spike 1、D62 實作 | `deep_recall`／`member_recall` 每次呼叫有 `act_`＋receipt；`action.*` 事件與舊版 payload 相容；p50/p95 延遲不劣化超過基準；`CoreToolExecutor` 白名單路徑關閉；`direct_chat` 的投影僅含唯讀工具（Q-E） |
| **Spike 2 park／resume** | 以 fake executor 驗證 DeferredToolRequests 式「遇工具呼叫即結束 run」→ checkpoint → 崩潰重建 → resume | Q-F、D61 B | C | 見 §7.4：崩潰重建後 history 的 hash 一致；重複核准只產生一次副作用；resume 後工具集合 ⊆ 凍結集合 |
| **D 自迴圈** | `AgentLoop`：外層 Run 迴圈、預算、heartbeat、checkpoint；`route=agent_loop` 接 `LoopTrigger`；`thought_delta`（只輸出到 log／終端）；D61 A 路徑續跑；**平行 context**（§4.10）；**`decision.consult`**（§4.9，依決策層 schema）；**平行排程器**（§6.4，首批只有 `parallel_safe` 工具可並行） | （新） | Spike 2、決策層 route 表與 consult schema | fake Runtime 腳本：多 Step 工具鏈完成；預算耗盡 ⇒ finalize 或 `budget_exhausted`；無進展偵測；AL-3 反例（模型／工具結果／consult 要求更大能力 ⇒ 不擴大，consult 只回 escalate）；consult 計入步數且不觸發 plan.refine；平行批次衝突 ⇒ 序列化、單一失敗回填符合 §6.4；平行 context 的建立／回饋／park 持久化案例（§4.10） |
| **E 核准 park／resume** | `park_run`、否決路徑、同批後續呼叫 deferred；`force_remember`、about-me 寫入類改完整四階段 | TL3 | D、DQ-16、D65 | TG-7 五個反例全過；park 期間 worker 釋放（另一 Run 可使用）；resume 後重驗 `tool_version`／approval／能力；崩潰於 park 前後各一案例無重複副作用；park 時平行 context 可重建 |
| **F Supervisor、cancel、readiness 整合** | SV-1～5、`ToolExecutor.cancel`（D69）；註冊表訂閱 module coordinator 就緒事件並維護 readiness、Gateway 執行前再檢查（`tool_not_ready`，§5.8）；module coordinator 本體**不在本提案範圍（待 module coordinator 設計）**；`concurrency=parallel_one_shot` 啟用條件（需 `cancellable`＋`timeout_s`） | （SYS S1 尾） | D | 依 SYS 草案 VC-SV-1／VC-SV-2；readiness：事件 `unavailable` ⇒ 工具不被投出，已投出而執行前轉為非 ready ⇒ Gateway 回 `tool_not_ready` 且不建 action；事件遺失／亂序以最新 revision 為準；one_shot 條目缺 `cancellable` 註冊失敗 |
| **G 原生工具、composite 與平台遠端** | `lore.*`、`chatroom.*`（SYS S0）；`decision.consult` 條目（schema 由決策層供）；`mcp_static` 載入與 pin；composite 線性工具（§5.7）；D91 R0～R3 平台宣告工具；legacy 桌面 workflow 以 `platform_remote`＋composite 重寫（只沿用 YAML 宣告、`initial_params`、必要／可選輸入三個概念） | TL4、TL5 | E、F、Part H、每訊框 MAC | 依 SYS 草案 §5.4、增補 §7；drift 偵測案例；`echo-mcp` 被 RA-10 擋下；composite 各子步驟各有 `act_`／receipt，中途失敗不重播已成功步驟 |
| **H Proactive（shadow）** | `ProactiveCandidate`→`LoopTrigger(proactive)`，只 shadow | 10-25 §9 | D、決策層 | 只記錄「會／不會啟動、原因」，無實際 Run 被建立 |

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
| `RuntimeEvent`（`call_id`、`thought_delta`）、`result_view`（`reason_code`／`fields`） | 擴充（只加不改） | B～D |
| D62／TC-3 的 DB 部分唯一索引 `UNIQUE(run_id) WHERE status='EXECUTING'` | 移除，改為應用層資格檢查＋資源鎖表（§6.5） | D |
| `think_llm` 的 tool 輪次迴圈與 history 內嵌（工具訊息直接寫進 parent history） | 改寫入迴圈 context；結果以 merge-back 回饋（§4.10） | C→D |
| legacy `sys_module/workflows.py`、`step_templates.py` 的類別與引擎（`legacy-final-v0.9.4`） | **不沿用**（只沿用 YAML 宣告、`initial_params`、必要／可選輸入三個概念） | G |
| SYS 草案 §7.2 的 workflow 遷移項 | 以 composite（線性）＋ Agent 迴圈分支取代；`ConditionalStep` 不承襲維持 | G |

---

## 10. 決策層介面（內容以 `決策中樞架構提案.md` 為準）

決策層（決策中樞）的設計由另一份文件維護：`docs/架構重構/決策中樞架構提案.md`（決策層 agent 在 `feature/decision-hub` 撰寫；本文撰寫時該文件尚未合入，引用其內容時以該文件為準，不在此重複）。route→能力對映表、`capability_hint` 的產生與驗證、`ProactiveCandidate` 欄位、`DecisionPlan` 內容、`consult` 的 kind 與回傳 schema、延遲預算，全部歸決策層。本節只保留**工具層／Agent 層依賴的介面摘要**。

### 10.1 四個介面點

| # | 介面 | 方向 | 本文依賴的形狀 | 歸屬 |
|---|---|---|---|---|
| I-1 | **DecisionPlan → 能力收窄 → `render_tools`** | 決策層 → 工具層 | 決策層輸出已收窄的 `capability_tags: frozenset[str]`（= route 預設表 ∩ `capability_hint` ∩ 政策快照 `capabilities`，只能收窄）。工具層 `registry.project(capability_tags, ctx)` → `capability_set` → `render_tools`（D60 唯一產生點，§6.2） | 標籤集合歸決策層；投影／渲染歸工具層 |
| I-2 | **`LoopTrigger`（迴圈上游）** | 決策層 → Agent 層 | 對話走 TurnProposer（TurnCandidate→Proposal），事件走 `ProactiveCandidate`（評估→仲裁）；兩者共用 I-1 後產生 `LoopTrigger`（下方形狀）。迴圈不得自行啟動 Run（AL-1） | 觸發判斷歸決策層；`LoopTrigger` 的消費歸 Agent 層 |
| I-3 | **`decision.consult`** | Agent 層 → 決策層（經工具層） | 註冊表中的 `internal_read` 工具，走 ActionIntent（§4.9）。kind 首版 6 個：`speak`、`output_modality`、`placement`、`ask_user`、`loop_end`、`followup`。回傳 `advice`（DecisionPlan 片段）、`reason_code`、`confidence`、`escalate`（`{needed, reason_code}`）、`decision_id` | **kind 與回傳 schema 歸決策層維護**；工具條目、計步、凍結範圍檢查歸本文 |
| I-4 | **模組預載提議與就緒狀態** | 決策層 → module coordinator；module coordinator → 註冊表 | DecisionPlan 含模組／工具預載的**提議**（中樞只提議，不執行）。實際載入（與日後可能的 patch 機制）由獨立的 **module coordinator＋其 registry** 負責（決策層 Q5＝A，艾斯維爾裁決）。工具註冊表**不執行** load／warm／unload，只在 `ToolEntry.requires` 宣告依賴，並訂閱 module coordinator 發布的就緒狀態事件以維護每個工具的 readiness（§5.8） | 提議歸決策層；載入與狀態歸 module coordinator（**待 module coordinator 設計**）；readiness 與投影歸工具層 |

### 10.2 `LoopTrigger` 形狀（摘要；欄位對映細節見決策層文件）

```python
@dataclass(frozen=True)
class LoopTrigger:
    kind: Literal["user_turn", "task_request", "external_event", "proactive", "resume"]
    origin_ref: str                 # 來源引用(ticket／候選／指派 id)；外部文字不進 goal
    goal_code: str                  # 語意短碼，非自由文字
    capability_tags: frozenset[str] # I-1 的結果，Run 內凍結(AL-3)
    scope_ref: str
    owner_actor_id: str             # 非 owner 來源固定為艾斯維爾(D64)
    policy_ref: str                 # 解析出預算與中斷政策
    snapshot_ref: str
    plan_ref: str | None = None     # 對應的 DecisionPlan 引用(審計用)；resume 沿用原值
```

**降級規則**：決策中樞在 `capability_hint` 被整個丟棄（`hint_dropped`）或收窄後能力集合為空（`capability_empty`）時，**將 route 降為 `direct_chat`**。**降級後不產生 `LoopTrigger`**——Agent 層不被啟動，回合照 `direct_chat` 路徑處理（Q-E：僅唯讀工具）。Agent 層因此永遠不會收到 `capability_tags` 為空的 `LoopTrigger`（收到即為違反介面，拒絕啟動並記稽核）。

### 10.3 決策層若變更下列契約，需同步修訂本文

`capability_tags` 的產生語意（必須維持「只能收窄」）、`consult` 回傳中 `advice` 的承載範圍（必須落在凍結範圍內）、module coordinator 就緒事件的格式（§5.8）。`result_view` 的選填 `reason_code`／`fields` 增補（§6.3）需決策層確認其消費方不受影響。

---

## 11. 與 10-25 提案的逐條對應

| 10-25 章節 | 主張 | 本文處置 | 理由 |
|---|---|---|---|
| §6.3 ThinkStage | 一般對話走直達路徑；需工具或多步時 ThinkStage 透過同一 Runtime Adapter 呼叫 Agent，再把可表達結果轉句子流 | **沿用**；補「想／說」分流（AL-4、Q-B）：agent_loop 下只有 `result`／通知 intent 進句子流 | 與 §6.3 的「可表達結果」一致，把它落成事件種類 |
| §6.3 | MEM 檢索、SessionControl 壓縮、SDK 歷史不得重複插入同一段訊息；`context_revision` | **沿用**；`context_revision` 進 checkpoint；歷史擁有者見 Q-D | agent_loop 多 Step 時重複插入風險放大 |
| §6.3 | 晚到資料需取消／重開，使用 `attempt_id` | **沿用**；park／resume 與 fallback 同用新 attempt | |
| §8.1 | SDK 在 Core 管理的執行邊界內；Runtime Adapter 提供 start／resume／cancel／status；輸入為 ContextSnapshot＋能力集合；不持 Bot Token、不繞過 Output Gate | **沿用**，現有 `Runtime` 協定已符合；D1「SDK」措辭**修改**為「任何 Runtime 實作」，迴圈、history、核准、工具執行留在 Core，SDK 只可能出現在模型層（PydanticAI 候選，Spike 1／2 決定，Q-F） | production 目前沒有任何 Runtime 實作；先有迴圈與契約，SDK 是可替換的實作選項 |
| §8.1 | Handoff 不是人格分裂；首版單 Agent | **沿用** | 非目標 |
| §8.2 | SDK 選型試驗（現有直達＋SYS 為基準組） | **沿用**；Spike 1／2（§7.4）即此比較試驗：PydanticAI 模型層對照自寫 `ModelPort` 實作作為基準組 | |
| §8.3 | Model Gateway：能力表、路由順序、工具呼叫增量須完整組裝驗證後才提交、fallback 於新 attempt | **沿用並具體化**為 `ModelPort`／`ProviderCapabilities`／`ToolCallAssembler`（§7.2） | |
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
| D1 | 自有 Core 掌握狀態，SDK 僅執行 Agent Loop | **修改** | 「SDK 執行 Agent Loop」→「**Agent Loop 在 Core**，SDK 至多提供模型層（`uep/models/`）」；Core 管狀態的原則不變且更嚴格 |
| D54／TG-1～5 | 註冊表注入 TaskManager；分類只由註冊表 | **保留** | `ToolSpec` 擴欄只加不改 |
| D55／TG-6 | approval 綁 `{tool, version}` | **保留** | 補：改 `input_schema` 或分類必升版；description 不必 |
| D56／TG-9 | 結果過濾掛點首版全放行 | **保留** | |
| D57（NT-2） | 常駐說明承載位置 | **保留**；`usage_hint` 為工具級使用時機文字，與常駐說明分開 | |
| D59 | MCP 當傳輸、靜態註冊、動態發現入待審目錄、schema 釘住 | **保留並具體化** | `origin=mcp_static`＋lockfile／pin（§5.5） |
| D60 | 禁用 hosted 工具；清單恰等於 `capability_set` | **保留並加強** | 落點明確化（`render_tools` 唯一產生點、CI 斷言）（§6.2） |
| D61 | A 為常態、B 為 fallback | **修改（擴充 B 的適用情境）** | B 升格為核准等待的常態路徑；`result_view` 增選填 `reason_code`／`fields`（只加不改，需與決策層確認） |
| D62／TC-5 | 回合內懶建立 Task、action 層強制 presence | **保留**；agent_loop 的 Task 為長壽命 | 兩種 Task 生命週期並存 |
| D62／TC-3 | 同 Run 至多一個 EXECUTING；部分唯一索引 | **修改**（Q-C） | EXECUTING 集合＝恰一個 `serial` 或全為通過資格且資源鍵不交的並行動作；索引移除，改應用層＋資源鎖表（§6.5）。UNKNOWN 阻擋語意保留 |
| 艾斯維爾 Q-A 裁決 | 迴圈可委派決定給中樞 | **新增** | `decision.consult`（§4.9）、AL-7 |
| 艾斯維爾 Q-D 裁決 | 平行 context | **新增** | §4.10、AL-8；10-25 §14.4 的落地方式（歷史擁有者仍是 ContextManager） |
| D91 `executor_ready(tool)` | 遠端 executor 在線的前置檢查 | **保留，視為 readiness 特例** | 併入或對映 §5.8 的 readiness，待 D91 實作時確認 |
| 決策層 Q5＝A | module coordinator＋registry 負責載入；中樞只提議 | **採用** | 工具註冊表不執行 lifecycle，只宣告 `requires` 並訂閱就緒事件（§5.8） |
| SYS 草案 §7.2（workflow 遷移邊界） | ConditionalStep 不承襲 | **保留** | legacy 組裝器類別／引擎不沿用（§5.7.1） |
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

## 13. 裁決紀錄

初稿的 6 題已全部由艾斯維爾裁決（以下附裁決要點，契約已依此修訂；本節只記結論與其落點）。**本節不再有待裁決或待確認項目**；決策層自身的待決項見 `決策中樞架構提案.md`，module coordinator 的設計另案。

### 13.1 Q-A 自迴圈內「決定」歸誰：**已裁決，採 A（加委派機制）**

- 裁決要點：採 A（決策層管「要不要啟動、以什麼範圍啟動」，迴圈管迴圈內步驟）。另：特殊情況下迴圈可把決定**委派給決策中樞**，中樞當作可呼叫的指示器、不是完全獨立。協議形式 `decision.consult`：註冊表中的 `internal_read` 工具、走 ActionIntent。
- 落點：§4.9（`decision.consult` 介面與歸屬）、§4.8 AL-3／AL-7、§10。
- 被否決的選項（供日後回溯）：B 迴圈自行決定（可自我擴權、無統一稽核點）；C 決策層給上限、迴圈可在上限內升級（破壞 AL-3 的簡單性）；D 每步都過決策層（延遲與成本不可接受，且 consult 只在「特殊情況」才用，正是 D 的有界版本）。

### 13.2 Q-B 想／說分流：**已裁決，採 A（`thought_delta`）**

- 裁決要點：採 A。思考流目前只輸出到 log／終端，UI 呈現延後。
- 落點：§4.3、§7.1、AL-4。UI 訂閱 thought stream 的契約本輪**不定**，僅保證事件格式存在（`RuntimeEvent.kind="thought_delta"`），消費端目前只有 log sink。

### 13.3 Q-C 平行 tool call：**已裁決，採 B 並擴大**

- 裁決要點：唯讀工具與「一次性任務工具」都可並行，因此工具定義須有完整 schema 與步驟定義。legacy 組裝器（`legacy-final-v0.9.4` 的 `sys_module/workflows.py`、`step_templates.py`）評估結論：**類別與引擎不沿用**（狀態在記憶體、資料靠字串 key、核准繞過 ActionIntent、無並行／冪等／衝突欄位、`ConditionalStep` 依 SYS 草案 §7.2 已定案不承襲）；**沿用三個概念**：YAML 宣告名稱／分類／參數 schema、`initial_params` 預填跳過互動、必要／可選輸入。`ToolEntry` 新增 `concurrency`、`conflict_keys`、`timeout_s`、`cancellable`、`input_schema`／`output_schema`；多步驟工具由註冊表宣告為 composite。
- 落點：§5.1（欄位）、§5.7（composite 與 one_shot）、§6.4（排程規則）、RA-11～RA-14、TC-3 修訂（§12）。

### 13.4 Q-D 思考歷史：**已裁決，採 A（ContextManager 持有）＋獨立平行 context**

- 裁決要點：採 A。另：迴圈要開一個**獨立的平行 context** 處理迴圈內容，結束後把結果回饋到原 context。
- 落點：§4.10（建立、壓縮、回饋形狀、park 持久化）。

### 13.5 Q-E direct_chat 工具：**已裁決，採 A，並加 scope 維度**

- 裁決要點：採 A（`direct_chat` 只保留唯讀工具，走 ActionIntent）。另：記憶工具之後依工具層契約重構為「全域工具」；工具依 scope 適配不同平台，契約須處理。
- 落點：§5.6（scope 維度，與 D91 `targeting` 區分）、§6.1（投影依 Presence／平台篩選）、§9.1 切片 A／C。

### 13.6 Q-F Runtime／模型層：**已裁決**

- 裁決要點：艾斯維爾——「可以的話直接上 SDK，但 UEP 有它的特性，可能無法整套沿用，要多做研究比較。」研究結論：**迴圈、history、核准、工具執行都留在 Core，不整套沿用任何 SDK**；模型層候選 PydanticAI，只用其 Model／provider 與串流 part 事件，取代自寫 `ProviderAdapter`。細節與 Spike 1／2 見 §7.2、§7.4。
- 邊界確認（艾斯維爾）：「迴圈自寫＋模型層以 PydanticAI 為候選（依 Spike 1 結果決定）」可以；Spike 1 的首 token 額外延遲上限定為 **50 ms**。PydanticAI 只允許出現在 `uep/models/`（import-boundary 測試強制），Spike 1／2 任一項失敗即退回自寫 `ModelPort` 實作。

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
| 並行與 DB 強制 | 移除 EXECUTING 部分唯一索引後，跨 Run 的資源衝突需要資源鎖表；schema、鎖釋放（含崩潰後孤兒鎖）與 migration 未設計（§6.5） |
| 平行 context 持久化 | 子 session 的耐久性取決於 echo_thought_core `session_store` 後端，是否滿足 D10 未核實；`context_lost` 為 fail-closed，Run 會失敗而非降級 |
| `decision.consult` 延遲與依賴 | `decide()` 300 ms（executor 總時限 0.5 s）計入每次迴圈委派；中樞不可用時各 kind 的預設行為歸決策層，若預設過寬會被視為擴權，需決策層審 |
| module coordinator 未設計 | readiness 事件格式、亂序語意與 patch 機制皆為擬定；在其設計前，切片 B／F 以 fake module coordinator 驗收 |
| SDK 採用風險 | PydanticAI 的版本（pydantic-ai-slim 2.65.0）與 openai-agents 0.24.0 為暫定值；API 名稱未獨立核對（§15）；Spike 失敗則退回自寫 `ModelPort` 實作，成本約為一組 per-provider adapter |
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
- **PydanticAI 的具體 API**（`ExternalToolset`、`DeferredToolRequests`、`ThinkingPartDelta`／`TextPartDelta`、`builtin_tools`）與其版本號、openai-agents 0.24.0，皆取自艾斯維爾裁決與房內研究結論，**本文作者未獨立核對官方文件**；Spike 1／2 負責驗證。OpenAI Agents SDK／LangGraph／Claude Agent SDK 的「第二份權威狀態」「只支援 Anthropic 且綁 CLI」等判斷同為轉述。
- **Spike 1 的已驗證與未驗證（pydantic-ai-slim 2.65.0）**：API 已對照安裝版原始碼與實測（`run_stream_events` 在 2.x 是 async context manager；取消用 `CancellationToken`；內建工具即 `native_tools`）。D60 斷言比對的是「交給 SDK 的 ToolDefinition 經 `render_tools` 還原的 chat 形狀」，不是最終 HTTP body（Responses／Ollama 的線上形狀由 SDK 產生，未攔截比對）。S1-3 的 N=10 抖動大於 SDK 層本身，結論僅為「未超過 50 ms」，非精確差值；Ollama 無現行 backend 可比，對照為直打 `/v1` chat。gpt-5.6／gpt-6-luna「chat 帶 tools 不接受 reasoning_effort」仍沿用現行降為 none 的做法，未單獨實測被拒。未涵蓋：平行 tool call 的實測、`finish_reason=length` 截斷在 args 中途、Ollama 無 call id 的合成路徑（實測 Ollama 有回 id）、並發多 attempt、SDK 內建重試對延遲的影響。
- legacy 組裝器（`legacy-final-v0.9.4` 的 `sys_module/workflows.py`、`step_templates.py`）的評估結論（狀態在記憶體、字串 key、核准繞過、無並行／冪等／衝突欄位）取自艾斯維爾裁決敘述，本文作者**未重新讀 tag 內程式**。
- `echo_thought_core` 的 ContextManager 是否支援「子 session／fork」「獨立壓縮」「以鍵持久化並取回」，以及其 `session_store` 後端與耐久性，僅讀過 `engine_base.py` 的介面說明，**未驗證**；§4.10 是對它的需求，不是對現況的描述。
- 決策中樞文件（`決策中樞架構提案.md`，`feature/decision-hub`）撰寫時尚未合入，§10 與 §4.9 對 `consult`／DecisionPlan／lifecycle 提議的描述僅依房內協議；其最終形狀以該文件為準。
- module coordinator 的就緒事件名稱與 payload（`module.ready／warming／unavailable`、`revision`）為本文擬定。
- §6.4 的貪婪分組是本文設計，未對照實際工具批次分布評估並行度損失；`max_batch`、`max_parallel`、`warm_wait_budget` 數值未定。
- 資源鎖表、`park_run`、否決路徑（§6.5、§8.2）皆為提案，未讀 `execute_action` 全文確認與現有階段 1 的交互。
- `ToolVariant` 以「變體不得改分類」換取 `resolve(tool)` 與平台無關（PD-4）；變體 id 如何隨 action 保存供 `execute_action` 路由，只定到「由 `capability_set` 查回」，細節待實作。
