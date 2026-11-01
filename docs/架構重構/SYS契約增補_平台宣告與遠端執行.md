# U.E.P 新 Core：SYS 契約增補——平台宣告與遠端 executor

對應：[SYS契約草案](SYS契約草案.md)（下稱「SYS 草案」）的增補，不取代它。上游：[Core契約草案](Core契約草案.md)（CC-1～CC-7）、提案 §5.1、§8.4、§11.3、§13、§15.1、§16、§17.1、§18（`hidden_docs/架構重構/UEP_新架構研究/UEP_新架構技術設計提案_2026-10-25.md`）、`hidden_docs/DECISIONS.md`（D45、D46、D53～D62、D77、D79、D85、D87）。
狀態：**草案（第一版）**，純文件；不含程式碼、不含 DB migration。型別僅為示意。
前提（艾斯維爾裁示）：Core 常駐 Mac mini；Desktop／Discord／Website 皆為遠端 Adapter；採 Agent 式架構與通用工具契約，使 P3 起可定義平台與各平台可用工具。
相容約束（切片 0，**只加不改**）：`ToolSpec` 為具名欄位 frozen dataclass，必要欄位 `executor`、`side_effect_class`、`idempotency`、`presence_bound`、`tool_version`，另 `origin="builtin"` 預設；本增補新增欄位一律帶預設值（`provider="core"`、`provider_ref=None`、`targeting="core_local"`、`venues`、`transport_timeout` 等）。`ToolSpecResolver` 只有 `resolve(tool) -> ToolSpec`，解析不到拋 `PolicyViolation`，每次呼叫查當前資料；可見集合（`capability_set` 投影）是 Registry／Gateway 的另一個介面，不進 resolver。`ToolCall` 加 `tool_version: int | None = None`。`TaskManager` 的 `executors` 映射於組裝時凍結。

## 0. 定位與約定

### 0.1 本增補是什麼、改動哪些既有條款

沿用 SYS 草案 §0.1 的做法：每條規則標對應條款；對不到者標「**新增**」。條號前綴：

| 前綴 | 範圍 | 驗收案例 |
|---|---|---|
| `PD-n` | 平台宣告：連線生命週期、宣告訊息、核准清單、可見性、Gateway 通用規則 | `VC-PD-n` |
| `RX-n` | 遠端 executor：派送、逾時、查核、收據來源驗證、憑證邊界 | `VC-RX-n` |
| `DQ-n` | 本增補的待裁決項（§9）；併入 DECISIONS 時改為 D90 起的正式編號 | — |

對既有條款的實際改動提案（需人確認後回寫）：

| 被改動項 | 提案 | 條號 |
|---|---|---|
| SYS 草案 §1.2 非目標、§3.1 `targeting` 欄 | 「Part S 恆為 `core_local`」改為「Part S 內 `core_local` 與 `platform_remote`；多主機裝置解析仍屬 Part H」；enum 擴充 | §2.3 |
| SYS 草案 §3.1「註冊表靜態」 | 維持：註冊表＝Core 側核准清單（靜態設定，人工審核修改）；平台宣告只決定**可用性**，不決定分類 | PD-4 |
| TG-12 可見集合 `origin ∈ {builtin, mcp_static}` | 新增 `platform_approved`；`platform_declared` 等同 `discovered`（不可見） | PD-9 |
| TG-5 組裝斷言 | 遠端 executor 以固定鍵登記進凍結映射；「至少一項引用」對遠端鍵在無平台工具時視為內部用途 | RX-2 |
| TG-4 一 executor 一權威性 | 遠端以**兩個固定鍵**（權威／非權威）服務，不以單鍵 | RX-3 |
| TC-1 憑證保管 | 平台端持有平台憑證時的重述（Core 只持連線金鑰） | RX-12 |
| CC-6 `execute_action` 階段 1／1.5 | 新增選用前置檢查 `executor_ready(tool)`（與 `presence_check`、`action_guard` 同型；建議，DQ-5） | RX-6 |
| CC-1 §1.3 事件註冊表 | 新增「平台」類事件（payload 僅 ID 與雜湊） | PD-2 |
| `ToolCall`（`uep/adapters/tool.py`） | `tool_version`（切片 0 已定）；本增補要求派送訊息再帶 `params_hash` 回聲（不改 `ToolCall`） | RX-5 |
| 契約待辦（TASKS T-24 移出項 6）「hello 每則 MAC／通道綁定」 | 由「Part H 前」**提前為「遠端 executor 啟用前」** | RX-11 |

### 0.2 依賴方向

```
Part S（核准清單／平台宣告目錄／RemoteToolExecutor／Gateway 規則）
   -> CC-4 側的 ConnectionDirectory（唯讀：連線狀態、ledger 資訊）與 ConnectionTransport（送出／收取訊框）
   -> CC-1 事件（訂閱 platform.*）
CC-1～CC-7 -> Part S：禁止
```

- 連線的**權威擁有者**是 Core 側的「連線登記」（Connection Registry，**新增**，與 `uep/endpoint` 同屬 CC-4 端點家族；Part S 不寫）。它發布 `platform.*` 事件；Part S 訂閱後維護「平台宣告目錄」（Part S 自有衍生資料，可由事件與 blob 重建，SA-0 的補充）。
- 事件只做通知與重建；**執行當下的「現在是否在線」必須讀 `ConnectionDirectory`（同步、唯讀）**，不可只信事件快取（事件為 best-effort，E-13）。`ConnectionDirectory`、`ConnectionTransport` 兩個協定由 CC 側定義（建議放 `uep/adapters/platform.py`，新增），Part S 依賴協定，CC 不 import Part S。

### 0.3 現況查證（2026-10-31 讀碼）

| 缺口（盤點） | 查證結果 |
|---|---|
| 1 沒有平台註冊／宣告通道 | **成立**。`assemble_core(executors=…)` 是靜態字串→executor 映射（`uep/assembly.py:284`）；`uep/endpoint`、`uep/presence` 中 `capabilities`／`platform`／`device_id` 皆無匹配；提案 §13.1 的 `hello/capabilities` 與 §15.1 `endpoints.capabilities` 目前只是提案，**程式未實作**。端點現有 hello 只做 HMAC 雙向證明（`uep/endpoint/auth.py`）。 |
| 2 宣告屬不可信輸入 | **成立**。SYS 草案 TG-1／TG-2／TG-12 已把分類權收在 Core，但只涵蓋「MCP 靜態」與「待審目錄 `discovered`」，沒有平台宣告來源；本增補沿用同一模型。 |
| 3 遠端執行協定 | **成立**。`ToolExecutor.execute` 為同步、`ToolResponseLost`／`lookup_receipt` 已具三態語意（`uep/adapters/tool.py`），可承載遠端；缺的是：連線身分綁 executor、receipt 來源驗證規則、ledger 世代、斷線後對帳。 |
| 4 憑證邊界 | **成立**。TC-1 寫「token 只存在 executor 側（Host secrets）」，假設 executor 在 Core Host 內。 |
| 5 場域維度 | **成立**。TG-12 只有 `scope.platforms`／`audience_min`／`capabilities`；`guild_id`／`channel` 只出現在 P3 的 `tool_context`。 |
| 6 版本連動 | **成立**。D55 的 `tool_version` 由 `resolve(tool).version` 提供，未定義「版本由誰決定」；平台自報版本必須排除。 |
| 補充：認證層 | **額外發現**：`auth.py` 檔頭明載 hello 證明「抗冒名、不抗 relay」「跨機器前需每則 MAC 或通道綁定」，且回應不簽章（localhost 信任邊界）。Core 在 Mac mini、Adapter 遠端後，此限制成為遠端 executor 的前置條件（RX-11）。 |

---

## 1. 範圍與非目標

### 1.1 範圍

1. 平台（Desktop／Discord／Website 等 Adapter 服務）與 Core 之間的**工具通道**：連線生命週期、工具宣告、場域（venue）宣告、名冊（roster）與在場名單資料來源。
2. Core 側**核准清單**（approved list）：平台候選工具進入 LLM 可見集合的唯一途徑；分類由 Core 決定。
3. **遠端 executor 協定**：派送、逾時、斷線、查核、去重、收據來源驗證、取消。
4. 每輪工具篩選新增「場域」與「平台在線且宣告」條件。
5. 平台工具版本與 D55 的連動。
6. **Gateway 通用規則**：把 `uep/echo/tools.py`（`CoreToolExecutor`）現有 Host 規則升格（audience 重驗、LLM 引數不得指定範圍、缺漏 fail-closed）；`member_recall`、`retract_about_me`／`opt_out`／`correct_about_me` 註冊為 Core builtin。

### 1.2 非目標

| 項目 | 處置 |
|---|---|
| 多主機裝置的目標解析（D46）、每裝置金鑰與撤銷（D45 的裝置層）、前景／背景指定 `target_device_id` | Part H。本增補只在訊框預留 `target`（§2.3） |
| 長時間非同步操作（遠端執行超過 `transport_timeout` 上限）的 submit／poll 變體 | 不在內，見 RX-14 與 DQ-11 |
| 平台 SDK、具體 wire 格式（WebSocket／gRPC）的版本化 | 實作前另訂；本文只定義邏輯訊息 |
| Avatar 表現（P9）、Output 交付 | 仍走 CC-5；`presentation.*` 的 `provider="core"`（TG-13 不變） |
| Chatroom／Lore（Core 側 MCP 傳輸） | 仍為 `origin=mcp_static`、`provider="core"`；本增補不改 |
| Task Graph、平台別 Agent、Proactive | 沿用 SYS 草案 §1.2 |

---

## 2. 資料結構

### 2.1 身分層級（三種 ID，不可混用）

| 名稱 | 意義 | 來源 | 用途 |
|---|---|---|---|
| `platform_id` | 平台服務的穩定名稱，`[a-z][a-z0-9_]{2,31}`（例 `discord`、`desktop`、`website`） | Core 設定（艾斯維爾修改） | 核准清單的 `provider`、金鑰綁定、receipt 來源 |
| `connection_id` | 一次連線實例（`conn_<ULID>`，**新增 ID 前綴**，需同步 `uep/ids.py`） | Core 於 hello 通過時核發 | fencing：舊連線的訊框不得影響新連線 |
| `endpoint_id` | Presence 粒度的端點（CC-4 §4.1：Desktop `device_id+client_instance_id` 等） | 既有 | Presence／Lease，**與工具通道獨立** |

**PD-1**（新增）：工具通道金鑰與 Presence 端點金鑰**分開發放**（最小權限）。持工具通道金鑰者不得 `acquire` Lease；持端點金鑰者不得宣告或執行工具。同一個遠端程序可同時持兩把，但 Core 側各自驗證、各自撤銷（DQ-14）。

### 2.2 平台連線登記（Connection Registry，邏輯表）

| 欄位 | 說明 |
|---|---|
| `platform_id` | PK |
| `connection_id` | 當前有效連線；舊連線 fenced |
| `state` | 見 §3.1 |
| `state_revision` | 單調遞增，每次狀態或宣告變更 +1；重連後平台須取得全量快照再接增量（提案 §13.1） |
| `key_id` | 當前連線金鑰識別（不存金鑰） |
| `ledger_id`、`ledger_epoch` | 平台自報的執行紀錄世代（RX-9）；Core 記錄並比對變化 |
| `declared_revision`、`declaration_hash` | 最近一次被接受的宣告 |
| `last_seen` | 以 Core 單 Host 時鐘 |

### 2.3 訊息（邏輯方法；實際 schema 實作前版本化）

**平台 → Core**

| 訊息 | 內容 | 規則 |
|---|---|---|
| `hello`（擴充既有端點 hello） | `platform_id`、`protocol_version`、`tool_protocol`、`client_nonce`、`ledger_id`、`ledger_epoch`、粗粒度 `capabilities`（`text`／`audio_in`／`tools` 等技術能力，提案 §13.1） | 粗粒度 capability 只代表技術上能做（提案 §13.1）；**不含**工具分類 |
| `declare` | `declared_revision`、`tools[]`（候選，見下）、`venues[]`、`data_sources[]` | 整份取代上一份；增量用 `declare_delta`（同樣需 revision 連續） |
| `venues_update` | 增減場域（例：bot 被移出 guild）、`state_revision` | 變更立即影響可見性（PD-10） |
| `roster_update` | `venue_ref`、成員清單（平台使用者 ID、顯示名、別名、`as_of`）、在場名單 | 僅作名稱解析與在場資料，見 PD-14 |
| `heartbeat` | `connection_id`、`state_revision` | 未達則 SUSPECT（§3.1） |
| `ack` | `dispatch_id`、`action_id` | 僅表示「已收到且已寫入平台執行紀錄」；不是 receipt（RX-5） |
| `receipt` | 見 §2.5 | 可晚到、可跨連線（RX-8） |
| `lookup_reply` | 見 §2.5 | |
| `goodbye` | 主動離線（維護／關閉） | 之後視同 DISCONNECTED，不視為撤銷 |

**Core → 平台**

| 訊息 | 內容 |
|---|---|
| `hello_ack` | `connection_id`、`core_epoch`、`server_proof`、`state_revision`；要求平台回報 `declare` |
| `dispatch` | 見 §2.5 |
| `lookup` | `action_id`、`lookup_id` |
| `cancel` | `action_id`（對映 TC-4 的 `executor.cancel`） |
| `revoke` | 連線金鑰撤銷或工具停用通知（盡力送達；Core 不依賴平台確認） |

**候選工具（`declare.tools[]` 的一項；全部視為不可信輸入）**

```python
# 示意
@dataclass(frozen=True)
class DeclaredTool:
    local_name: str            # 平台內名稱；只作為 provider_ref 候選，不是 Core 的 tool 名
    params_schema: dict        # 候選 schema（Core 會自行重算 canonical 雜湊）
    description: str           # 候選說明；只給審核者看，不進 LLM（DQ-7）
    claimed_schema_hash: str   # 平台自報；僅與 Core 重算值比對，不一致整項拒收
    venue_candidates: list[str]  # 能在哪些場域使用（受核准清單 venue_policy 交集限制）
    hints: dict                # claimed side_effect／idempotency／版本等：一律不可信，只記 debug log（比照 TG-2）
```

**PD-2**（新增事件，D81 類型註冊）：Connection Registry 發布 `platform.connection_changed`（`{platform_id, connection_id, state, reason_code}`）與 `platform.declaration_received`（`{platform_id, connection_id, declared_revision, declaration_hash, declaration_ref}`）。宣告內文（schema、說明）屬不可信自由文字，**走可刪 blob（`declaration_ref`）**，不入 payload（同 D78 處理）。`idempotency_key=platform_conn:{platform_id}:{connection_id}:{state_revision}`。註冊於 CC-1 §1.3（durable）。

### 2.4 核准清單項（Core 側，靜態設定；SYS 草案 §3.1 註冊表的擴充列）

核准清單項**就是**註冊表中 `provider != "core"` 的列。除 §3.1 既有欄位外：

| 欄位 | 型別 | 預設 | 說明 |
|---|---|---|---|
| `provider` | str | `"core"` | `"core"` 或 `platform_id` |
| `provider_ref` | str? | `None` | 平台內 `local_name`；僅派送時使用，LLM 不可見 |
| `targeting` | enum | `core_local` | 擴為 `core_local`／`platform_remote`（只加值）。`provider != "core"` ⇒ 必須 `platform_remote`，反之亦然（組裝斷言） |
| `origin` | enum | `builtin` | 新增值 `platform_approved`（可見）；`platform_declared`＝候選（不可見，等同 `discovered`） |
| `pinned_schema_hash` | str | — | 審核當時 Core 重算的 canonical 雜湊；`platform_approved` 必填 |
| `params_schema`／`description` | — | — | **Core 儲存的審核副本**；驗證與 LLM 所見皆用此副本，不用平台即時回報的內容 |
| `lookup_authority` | enum{authoritative, non_authoritative} | `non_authoritative` | 決定走哪個遠端 executor 鍵（RX-3）；`authoritative` 需審核者確認平台具持久 ledger（RX-9） |
| `venue_policy` | obj | `{mode: "origin_venue"}` | `mode ∈ {origin_venue, listed, any}`；`listed` 帶 `venues[]`；跨場域預設關閉（DQ-6） |
| `injected_params` | list | `[]` | 由 Gateway 注入、LLM 不可提供的欄位（PD-12），例如 `venue`、`subject_actor` |
| `transport_timeout` | float | 設定項 | 單次派送到收據的上限（RX-7）；不得超過 Core 設定的 `transport_timeout_max` |
| `disabled` | bool | `false` | 單一工具停用開關（Core 層，不依賴平台） |

`ToolSpec` 對映：`ToolSpecResolver.resolve(tool)` 只回 `ToolSpec` 的既有五欄＋`origin`＋新增帶預設欄（`provider`、`provider_ref`、`targeting`、`venues`、`transport_timeout`）。

**PD-3**（對 TG-1，D54）：分類欄（`side_effect_class`、`idempotency`、`approval_level`、`result_scope`、`interruptible`、`presence_bound`、`tool_version`）**只由核准清單決定**。平台只能提供候選名稱、schema、說明；平台的任何 hint（含自稱 `internal_read`）不參與判定，也不得影響 `resolve()` 的結果。

**PD-4**：`resolve(tool)` 的結果**不依賴連線狀態**：只要核准清單有此項即回傳，與平台是否在線無關。理由：`resolve_unknown`、核准比對、稽核在平台離線時仍須運作。可用性（在線、宣告相符、場域）是另一個判定（PD-9），由 Registry／Gateway 提供，不進 resolver。核准清單允許執行期修改（熱載入），每次修改若影響 schema 或分類必須遞增 `version`（PD-8）。

### 2.5 遠端 executor 訊框

```python
# 示意；欄位為邏輯欄位
@dataclass(frozen=True)
class DispatchFrame:        # Core -> 平台，整框 MAC（RX-11）
    dispatch_id: str        # 每次傳送唯一；同一 action 重傳（若發生）可不同
    action_id: str          # 去重鍵（RX-4）
    tool: str               # Core 的 tool 名
    provider_ref: str       # 平台內名稱
    tool_version: int
    pinned_schema_hash: str
    params: Mapping         # 含 injected_params（已併入 params_hash）
    params_hash: str        # Core 計算（TG-6）；平台須原樣回聲
    side_effect_class: str
    idempotency: str
    approval_id: str | None
    venue: str | None       # injected；平台須自行比對不超出
    valid_until: float      # Core 時鐘換算的剩餘秒數（比照 CC-4 ttl 作法，不信絕對時間）
    connection_id: str
    target: Mapping | None  # 預留給 Part H（device_id 等）；S2 恆為 None

@dataclass(frozen=True)
class RemoteReceipt:        # 平台 -> Core，整框 MAC
    action_id: str
    params_hash: str        # 回聲，Core 比對
    tool_version: int
    succeeded: bool
    phase: Literal["rejected_before_exec", "executed"]  # 前者＝平台確定未執行
    error_code: str | None  # 短碼，T-14
    result: Mapping | None  # 內容資料（不可信，進 blob，RX-13）
    ledger_id: str
    ledger_epoch: int
    executed_at: float
    platform_id: str
    connection_id: str
    mac: str
```

`lookup_reply`：`{action_id, outcome ∈ {found, never_seen, expired, unavailable}, receipt?, ledger_id, ledger_epoch, mac}`。Core 映射到 `ReceiptLookup`：`found`→`found`；`never_seen`→`confirmed_not_executed`（**僅當** RX-9 全部條件成立）否則→`undetermined`；`expired`、`unavailable`、逾時、MAC 失敗→`undetermined`（T-15）。

---

## 3. 狀態機

### 3.1 連線狀態

```
            hello 通過 + MAC 驗證
DISCONNECTED ───────────────────────► AUTHENTICATED
     ▲                                     │ declare 被接受（revision 連續）
     │ goodbye／heartbeat 逾時 N 次          ▼
     │◄──────────────── SUSPECT ◄──────── ONLINE
     │                (heartbeat 逾 1 次)     │  ▲ heartbeat 恢復
     │                                        └──┘
     └─ 任何狀態 ──金鑰撤銷／allowlist 移除──► REVOKED（終態；須人工重新登記）
```

| 狀態 | 工具可見 | 可派送新 action | 說明 |
|---|---|---|---|
| DISCONNECTED | 否 | 否 | 離線即不在線；不論原因 |
| AUTHENTICATED | 否 | 否 | 已驗證、尚未宣告；此時可做**對帳**（RX-10） |
| ONLINE | 是（受 PD-9 條件） | 是 | |
| SUSPECT | 否（下一輪起不列入） | 否 | 避免把工具給 LLM 後派不出去；已在途 action 照 RX-7 逾時處理 |
| REVOKED | 否 | 否 | 在途 action 一律走 UNKNOWN 路徑；撤銷後金鑰不得再用 |

**PD-5**（連線單一性）：每個 `platform_id` 同時至多一條**有效**工具連線（S2）。第二個通過驗證的 hello 取代舊連線：舊 `connection_id` 被 fenced，舊連線之後的訊框（含 receipt）以**舊連線訊框**處理：派送類與宣告類一律丟棄；`receipt`／`lookup_reply` 若 MAC 有效且 `ledger_id` 相同，仍可入庫（證據不丟，比照 T-9）。多連線（多 Desktop 裝置）由 Part H 擴為 `(platform_id, device_id)`。

**PD-6**（重連=全量重宣告）：重連後平台必須重送完整 `declare`，Core 以 `declared_revision` 與 `declaration_hash` 比對；**不沿用**舊連線的宣告視為有效（比照提案 §13.1「必須取得全量權威快照」）。重連到 ONLINE 之前，該平台工具對 LLM 不可見。

**PD-7**（宣告驗收）：`declare` 的處理為純函式式比對，不產生副作用於核准清單：
1. 對每個候選，Core 以 TG-6 的 canonical 規則重算 `schema_hash`；與 `claimed_schema_hash` 不符 → 該項拒收（`declaration_hash_mismatch`）。
2. 以 `(platform_id, local_name)` 找核准清單項（`provider_ref`）。無對應 → 進**待審目錄**（`platform_declared`，等同 `discovered`，不可見、不可執行），並通知艾斯維爾審核（通知不含說明全文）。
3. 有對應但重算 hash ≠ `pinned_schema_hash` → **該工具自動停用**（`schema_drift`，`core.component_health_changed`，fail-closed），仍保留在待審目錄；不自動採用新 schema。
4. 相符 → 該工具在此連線上標為「已宣告可用」。

**PD-8**（版本連動，D55；缺口 6）：`tool_version` **只由核准清單決定**，平台自報版本不採用。下列任一變更必須遞增 `version`：`pinned_schema_hash`（含審核採用新 schema）、`params_schema`、任一分類欄、`injected_params`、`provider_ref`、`venue_policy`。結果：(a) `params_hash` 域含 `{tool, v}`（TG-6.7），舊 approval 自動 `hash_mismatch`；(b) 建立後升版者在 `execute_action` 以 `resolve(tool).version` 比對失敗 → `ActionBlocked(tool_version_changed)`；(c) 平台升版造成 schema 漂移時，在人工重新審核（version++）之前工具停用，不會出現「平台已換 schema、Core 仍以舊 version 簽核」的窗口；(d) 派送訊框帶 `tool_version` 與 `pinned_schema_hash`，平台若本地版本不符須回 `rejected_before_exec(version_mismatch)`（RX-6）。

### 3.2 可見性條件（每輪篩選；缺口 5）

**PD-9**（擴充 TG-12）：一個工具出現在本輪 `capability_set` 當且僅當以下全成立：
1. 既有（TG-12）：`origin ∈ {builtin, mcp_static, platform_approved}`；通過 `scope.platforms`、`audience_min`、快照 `capabilities`。
2. `disabled=false`。
3. 若 `provider != "core"`：Connection Registry 中該 `platform_id` 為 `ONLINE`，且最近一次宣告包含此工具並通過 PD-7 第 4 點（hash 相符）。
4. 場域：依 `venue_policy`：`origin_venue` ⇒ 本輪 session 的場域（`guild_id`／`channel_id` 等，**取自 Adapter 認證後的 session context**，不取自 LLM 或平台宣告的字串）屬於該平台目前 `venues`（`venues_update` 後的有效集合）且與核准清單設定相交；`listed` ⇒ 在列表內；`any` ⇒ 不檢查（僅限不涉場域資料的工具）。
5. `provider_ref` 平台確實宣告「此能力」（缺口 5 的「宣告了此能力」）：以步驟 3 的宣告為準，不另信粗粒度 `capabilities`。

`capability_set` 在 attempt 開始時計算並**投影**（不含 `provider`、`provider_ref`、`venue_policy`、金鑰等）。**可見不等於可執行**：屬於本輪的投影可能在執行前失效（平台離線、場域撤銷），因此執行當下必須重驗（PD-10、RX-6）。

**PD-10**（執行時重驗）：`execute_action` 階段 1 與 1.5 重驗 PD-9 第 2～5 點（經 `executor_ready(tool)`，DQ-5）。階段 1 失敗：`PlatformUnavailable`（retryable，action 維持 `INTENDED`，approval 未消耗）；階段 1.5 失敗：沿用 T-10——executor 確定未被呼叫，action `FAILED`，Core 簽發 `source_verified=True`、`meta.origin=core_pre_call` 的 receipt（`error_code=platform_offline_before_call`／`venue_revoked_before_call`）。`venues_update` 撤銷場域時，該場域下**已建立未執行**的 action 同理被擋。

**PD-11**（跨場域）：預設 `venue_policy.mode=origin_venue`，即工具只在**同平台同場域**的 session 可見。「在 Desktop 對話中要求貼到某 Discord 頻道」屬跨場域，預設不可見；若日後開放，必須同時滿足：核准清單明列目標場域、內容 scope 通過 `scope_admits(目標場域 audience, 內容 scope)`（比照 NT-8）、per_action 核准畫面顯示完整場域與內容（DQ-6）。

### 3.3 action 與連線的交互（D62／TC-3／T-3）

| 時點 | 連線事件 | action 結果 | 依據 |
|---|---|---|---|
| 階段 1 前 | 平台已非 ONLINE | `PlatformUnavailable`；`INTENDED`；approval 未消耗 | PD-10 |
| 階段 1.5 | 轉為非 ONLINE | `FAILED`（Core 簽 pre_call receipt） | T-10 |
| 已寫入連線、未見 `ack` | 斷線／逾時 | `ToolResponseLost` → `UNKNOWN`（**不得**推定未執行） | RX-7 |
| 已 `ack`、未見 receipt | 斷線／逾時 | `ToolResponseLost` → `UNKNOWN` | RX-7 |
| 平台回 `rejected_before_exec` | 正常 | `FAILED`（`source_verified=True`） | RX-5 |
| 重連後 | 對帳 | 對每個該平台的 `UNKNOWN`／`EXECUTING` 殘留 action 呼叫 `resolve_unknown`（三態） | RX-10 |
| `UNKNOWN` 且非 `internal_read` | — | 同 Run 後續 action 阻擋（TC-3 應用層） | D62 |
| Run 因 deadline 轉 FAILED 時仍有 UNKNOWN | — | `run.ended(reason_code=unresolved_actions)` | T-13 |

`UNKNOWN` 的 Task／Run 不連動（T-3）。平台長時間離線時，非 `internal_read` 的 UNKNOWN 會讓該 Run 無法再發動新副作用；這是 D62 的預期行為，出口是 `resolve_unknown`（重連後自動）或 `resolve_unknown_manual`（SA-11，人類）。

---

## 4. 安全條文：核准清單與可見性

**PD-12**（Gateway 通用規則 1：LLM 不得指定範圍；升格自 `CoreToolExecutor`「`args` 不能指定範圍」，對應 TG-1、SA-3）：每個工具的 `injected_params` 清單內的欄位（`venue`、`guild_id`、`present_uids`、`subject_actor`、`audience`、`actor_id` 等範圍／身分類）：(a) 不得出現在 `capability_set` 投影的 `params_schema` 內（LLM 看不到）；(b) 若 `tool_intent.args` 仍帶此類欄位 → 整個 `tool_intent` 以 `INVALID_TOOL_ARGS` 拒絕，**不是**靜默覆寫；(c) 由 Gateway 依 Core 每輪新建的 `tool_context`（不可跨輪共用）注入，並**併入 `params_hash` 域**，使 approval 綁定實際場域與主體。

**PD-13**（Gateway 通用規則 2：audience 重驗；升格自 `audience_ok`，對應 TG-9、X-1）：凡 `scope.audience_min` 非 public 或 `result_scope` 非 public 的工具，Gateway 在 **create_action 前、階段 1、階段 1.5、結果交付前**各以「**當下** Output Gate／Presence 的 audience」重驗；不使用 `tool_context` 建立時的值。任一不符 → 拒絕（`denied`），不回報原因給 LLM。

**PD-14**（Gateway 通用規則 3：缺漏 fail-closed；對應 SA-3、NT-8）：`tool_context` 的必要欄位（`actor_id`、`audience`、`segment_id`、`ticket_ref`；場域類工具另需 `venue`）缺漏、型別不符、或工具不在核准清單，一律在 `create_action` 之前拒絕，**不建立 `act_`**，只計數、不補記 `action.*`（沒有任何動作發生）；回給 Runtime 的是通用拒絕碼，不區分原因。

**PD-15**（平台資料不授權；對 TG-10、D77）：平台經 `roster_update`、`declare`、`receipt.result` 提供的內容都是資料：(a) 不得改變 `actor_id`／owner／approvers／scope；(b) **身分命名空間綁定**：平台連線只能斷言自己 `platform_id` 命名空間的外部 ID（`discord` 連線只能斷言 `discord:<id>`），Core 依 D77 映射表重建 `actor_id`，映射缺失視為訪客；Desktop owner 身分不得由 Discord 連線取得；(c) roster 僅用於名稱→uid 解析與在場判定，解析結果必須屬該場域名冊，名稱不唯一不猜（沿用 `GuildRoster.resolve_member`）；(d) roster 的 `as_of` 超過 `roster_max_age`（設定項）→ fail-closed（DQ-9）。

**PD-16**（builtin 成員工具；缺口之外的既定事項）：`member_recall`（`internal_read`）、`retract_about_me`、`opt_out`（別談我）、`correct_about_me`（皆建議 `reversible_local`，因 D87 為遮蔽而非刪除；分類待記憶線確認）註冊為 `provider="core"`、`origin=builtin`、`targeting=core_local`，資料與邏輯在 Core 的 MEM Access Layer。規則：
1. 平台只宣告「成員查詢」能力（data_source：roster／在場名單），**不提供工具實作**，也不接觸記憶內容。
2. `member_recall` 只在有場域且該場域有有效 roster 的 session 可見（對映 P3 現況「只在有 guild 的 session 曝光」）。
3. `member` 名稱參數是業務欄位（LLM 可給）；`guild_id`／`venue`、`present_uids`、`viewer_uid` 為 `injected_params`（PD-12）；執行當下重取在場名單並重驗 audience（PD-13）。
4. `retract_about_me`／`opt_out`／`correct_about_me` 的**主體不是參數**：`subject` 為注入欄位，等於 Gateway 重建的呼叫者 `actor`；「只有當事人可呼叫」由此結構性成立，LLM 無法指定他人為主體（D87）。
5. 名稱解析不唯一或解析不到：回通用提示請模型向使用者確認，不碰 engine（沿用現行 `MEMBER_NOT_FOUND` 行為）。
6. 與 D79 的關係：D79 的 Host 白名單過渡偏離，於本契約落地（Part S S3）後由 ActionIntent 管線取代；`action.*` 事後補記改為正規事件。每輪工具可能多一組 `task.*`／`action.*` 事件（D62 已登記的事件量取捨），`internal_read` 簡化記帳路徑維持 D62 待量測項。

**PD-17**（核准清單的修改權）：只有艾斯維爾（設定檔或經人類認證的管理介面）可新增、變更、停用核准清單項或調整 `pinned_schema_hash`；沒有任何註冊工具可讀寫此清單；平台宣告不能自動升格為核准（比照 NT-9 第 6 點對 Hub 允許清單的處置）。清單變更記入 journal（僅 ID、版本、短碼）。

---

## 5. 遠端 executor 協定

### 5.1 單鍵路由是否足夠（回應補充約束）

**結論：路由足夠，權威性不足；S2 不需要非同步變體，Part H 才需要。**

| 面向 | 單一 `RemoteToolExecutor`（固定鍵，內部依 `provider_ref` 路由） | 評估 |
|---|---|---|
| 路由 | 以 `ToolCall.tool` 查核准清單取得 `platform_id`＋`provider_ref`，再經 `ConnectionTransport` 送出 | **足夠**。executors 映射凍結不受影響，新增平台不需改組裝 |
| TG-4 一 executor 一權威性 | `lookup_authoritative` 是 executor 層級的單一布林（`uep/adapters/tool.py`），`resolve_unknown` 只讀它。單鍵只能全權威或全非權威 | **不足**。全設 False 會讓具持久 ledger 的平台也永不能自動重送（保守但浪費）；全設 True 則讓無持久 ledger 的平台冒充權威（危險）。**建議兩個固定鍵**（RX-3） |
| 同步 `execute` | 在 worker 執行緒內阻塞至收據或逾時；逾時拋 `ToolResponseLost` | **S2 足夠**：網路 RTT 級工具（發訊息、查名冊）都在 `transport_timeout_max`（建議起點 30 秒，設定項）內；SV-2 已承認同步呼叫期間 worker 無法 heartbeat，`deadline_grace` 強制路徑為預期路徑 |
| 長時操作（GUI 自動化、大檔處理、Part H） | 逾時 → UNKNOWN → 查核輪詢 | **不足**。可勉強以「逾時當 UNKNOWN、`resolve_unknown` 輪詢」模擬，但 UNKNOWN 會阻擋同 Run 後續非 `internal_read` action（TC-3），且把正常等待誤標成異常。非同步變體需要 CC-6 新子狀態（`DISPATCHED`／`PENDING_RECEIPT`），屬 Part H（DQ-11） |

**RX-1**（新增）：遠端 executor 實作現有同步 `ToolExecutor` 協定，**不改協定**；`RemoteToolExecutor` 內部可有非同步連線層（橋接為「提交到連線 loop、帶逾時等待」），但對 Task Manager 呈現同步語意。

**RX-2**（對 TG-5）：`executors` 映射於組裝時凍結，遠端以固定鍵登記，不隨平台增減。沒有任何 `platform_approved` 項時，遠端鍵視為內部用途，不違反「每個 executor 至少被引用」。核准清單的 `executor` 欄對遠端項只能是下列兩鍵之一（組裝斷言：`provider!="core"` ⇒ `executor ∈ 遠端鍵`）。

**RX-3**（對 TG-4，缺口 3）：遠端鍵為 `endpoint_auth`（`lookup_authoritative=True`）與 `endpoint_best_effort`（`False`）——共用同一個連線層（`ConnectionTransport`），兩個 executor 實例。核准清單的 `lookup_authority` 決定使用哪一個。這滿足「同一 executor 實例所服務的工具權威性相同」而不改協定。（鍵名為示意；coordinator 若已定固定鍵 `"endpoint"`，則需擇一：加 `lookup_authoritative_for(tool)` 選用方法並改 `resolve_unknown`，或接受此兩鍵——建議後者，影響較小，DQ-4。）

**RX-4**（去重，缺口 3）：以 `action_id` 為唯一去重鍵。平台**必須**在回 `ack` 前把 `action_id` 寫入持久 ledger，並對重複的 `dispatch`（同 `action_id`）回傳**既有 receipt**，不重複執行——不論工具 `idempotency` 為何（`idempotency=none` 的工具，重複派送由平台層擋，工具本身不重複執行；Core 仍不主動重送）。Ledger 保留期不得短於 `unknown_resolve_horizon`（設定項，建議起點 7 天）；超過者 `lookup` 回 `expired`（→ `undetermined`）。

**RX-5**（receipt 與 ack 的分界）：`ack` ≠ receipt。receipt 需含 `params_hash` 回聲與 `tool_version`；Core 比對 `params_hash`／`tool_version`/`action_id` 皆等於自身記錄才可能 `source_verified=True`（RX-8）。`phase=rejected_before_exec` 表示平台確定未執行（版本不符、場域不符、過期、未知工具），Core 轉 `FAILED`，與「回覆遺失」嚴格區分。

**RX-6**（前置與平台端複驗）：Core 側 `executor_ready(tool)`（PD-10）；平台側須複驗 `tool_version`、`pinned_schema_hash`、`venue`、`valid_until`、`connection_id` 為當前，任一不符回 `rejected_before_exec`（對應提案 §18.2「Policy 判斷保存在 Core 及 executor 兩端」）。平台**無法**驗證 `approval_id` 真偽（無 Core 密鑰）；approval 的真偽仍只由 Core 保證，平台信任 Core 簽章的訊框（RX-11）。

**RX-7**（逾時與斷線語意）：
1. 三個計時：`ack_timeout`（寫入連線後等 `ack`）、`transport_timeout`（寫入後等 receipt，上限 `transport_timeout_max`）、`lookup_timeout`。
2. 只有「訊框**確定未寫入**連線」（送出前連線已非 ONLINE，或寫入前拋錯）才視為未執行，回 `FAILED`（`dispatch_not_sent`，Core 簽 `core_pre_call`）。其餘任何失敗（寫入後逾時、斷線、MAC 失敗、無法解析的回覆）一律拋 `ToolResponseLost`。
3. **Core 傳輸層不得在連線遺失後透明重送 dispatch**（at-most-once 由 Core 保證；平台的 RX-4 去重是第二道防線）。
4. 逾時後晚到的 receipt 照常入庫（T-9），不因 action 已 `UNKNOWN` 而丟棄。

**RX-8**（receipt 來源驗證，缺口 3）：`source_verified=True` 當且僅當全成立：(a) 訊框 MAC 以該 `platform_id` 的連線金鑰驗證通過（RX-11）；(b) `receipt.platform_id` 等於該 action 工具的 `provider`（**連線身分綁 executor 鍵**：平台 B 的連線不能為平台 A 的 action 簽 receipt）；(c) `params_hash`、`tool_version`、`action_id` 與 Core 記錄相符；(d) `connection_id` 為當前，**或**為同 `platform_id` 的先前連線且 `ledger_id` 相同（晚到 receipt）；(e) `executed_at` 不早於 action 的 `EXECUTING` 起點（Core 時鐘換算後容許時差內）。不符者以 `source_verified=False` 記錄，不推進狀態（CC-6 既定語意）。receipt 內容受 T-14 脫敏（platform 提供的字串全屬不可信）。

**RX-9**（`confirmed_not_executed` 的權威條件，對 T-3）：`lookup_reply=never_seen` 要映射為 `confirmed_not_executed`，必須同時成立：(a) 該工具走 `endpoint_auth`；(b) `idempotency ∈ {native, via_action_id}`；(c) 回覆 MAC 有效；(d) 回覆的 `ledger_id` 與 `ledger_epoch` 與 action 派送當時 Core 記錄的**相同**（ledger 世代未變：平台資料庫被重建／回滾／換機後，「從未見過」不再代表「未執行」）；(e) action 的派送時間在 ledger 保留期內。任一不成立 → `undetermined`。ledger 世代變化時，Core 發出 `core.component_health_changed(ledger_changed)` 並把該平台所有 `endpoint_auth` 工具暫降為非權威，直到艾斯維爾確認。

**RX-10**（重連對帳）：平台進入 AUTHENTICATED 後、發布 ONLINE 之前，Core 對該平台所有 `UNKNOWN`／（重啟後殘留的）`EXECUTING` action 呼叫 `resolve_unknown`（沿用 T-3 三態；`undetermined` 者留在人工佇列，不重試）。對帳的 `lookup` 與正常派送共用同一 MAC 與 `connection_id` 規則。對帳期間該平台新派送被擋（`PlatformUnavailable`）。

**RX-11**（連線認證，缺口 3／4；提前 TASKS T-24 移出項 6）：遠端 executor 啟用前，工具通道必須滿足：(1) TLS 或同等通道加密；(2) hello 後每個訊框（雙向）帶 `connection_id`、遞增 `seq` 與 MAC（以連線金鑰；Core→平台的 `dispatch`/`lookup`/`cancel` 也要簽，修正 `auth.py` 檔頭「回應不簽章」的 localhost 假設）；(3) 防重放：`seq` 單調、舊 `connection_id` fenced；(4) 金鑰不入 prompt、payload、錯誤輸出（提案 §18.3）。本地同機部署（Discord 與 Core 同在 Mac mini）也走相同規則，避免「同機視為可信」的例外路徑。

**RX-12**（憑證邊界 TC-1 重述，缺口 4）：
1. **平台憑證**（Discord bot token、桌面使用者權限）只存在平台端；Core、Runtime、SDK 不持有、不見、不轉交。TC-1「憑證只存在 executor 側」在遠端情境的對應是：**executor 側＝平台端**。
2. Core 側只持**連線金鑰**（Host secrets，Runtime 不可讀）。連線金鑰只能讓持有者以該 `platform_id` 身分通訊，不能核准、不能改核准清單、不能取得 Core 的 Lore／Chatroom 憑證。
3. `dispatch` 內容（`params`、`result`）不得含 Core 側任何密鑰；`params_schema` 禁止 `secret` 型標註欄位。
4. 因為平台以自己的權限行事，Core **無法**限制平台對其平台憑證的使用；能做的是：核准清單限定工具、`per_action` 核准（外部副作用）、場域限制、平台端複驗（RX-6）、Core 層停用開關。這是**有意識接受的信任邊界**（提案 §18.1：被攻陷的 Adapter 無法單靠 schema 防止越權），須登記為風險。
5. 撤銷：(a) 單一工具 `disabled`；(b) 單一平台金鑰撤銷→REVOKED；(c) 全域停止（D36）阻止所有新 action。三者都在 Core 層，不依賴平台配合；`revoke` 訊息盡力送達。

**RX-13**（結果資料）：`receipt.result` 為不可信資料（TG-10）：存為 Core 持有的 blob，`result_ref` 由 Core 產生（不採平台字串）；單筆大小有上限；項目拆分後逐項 scope 由**核准清單 `result_scope` 與 Adapter 映射**決定，**平台自帶的 scope 只能收窄、不能放寬**（`private > group > public`）；經 TG-9 過濾掛點（首版全放行，D56）。

**RX-14**（長時操作，非目標）：核准清單項的 `transport_timeout` 不得超過 `transport_timeout_max`；預期超過者不得進清單。Part H 若需要，另提非同步變體：`dispatch` 回 `ack` 後 action 進新子狀態，receipt 以推送或輪詢取得，並需要 CC-6 狀態機與 TC-3 的修訂（DQ-11）。

**RX-15**（取消，對 TC-4／D69）：`interruptible` 的遠端工具須實作 `cancel`；未回應或平台不支援者視為 `unsupported`，該工具不得宣告 `interruptible`。取消後仍可能有 receipt 晚到（`isolated`），照 R-2 入庫。

---

## 6. 與 Part H（D45／D46）的邊界

| 項目 | 本增補（S2 範圍） | Part H |
|---|---|---|
| 連線方向 | 採 D45 建議：**平台主動連 Core 的長連線**（Core 不開對平台的入站連線；離線即不在線）。這是對 D45 的**部分提前裁決**（DQ-1） | 其餘：節點在使用者電腦的部署、NAT／VPN 前提、每裝置金鑰發放與撤銷 |
| 身分單位 | `platform_id`（一平台一把連線金鑰） | `(platform_id, device_id)`，每裝置一把（D45） |
| 宣告 | `hello`／`declare`／`venues_update`／`roster_update`、核准清單 | 裝置能力（OS、是否有互動桌面、可用 executor）作為 `declare` 的 `data_sources` 與 `venue` 的擴充 |
| 目標解析 | 場域（guild／channel）取自 session context；`target` 訊框欄位預留且恆為 `None` | D46 不變：前景取 Presence 當前 `device_id`；背景明確 `target_device_id`；移轉時續跑與預設值 |
| 遠端 executor | RX-1～RX-15 | 沿用 RX；增加：裝置離線語意、非同步變體（RX-14）、多連線（PD-5）、GUI 類 receipt |
| 前景桌面工具與 Presence | `presence_bound` 仍為 action 層強制（TC-5）；「平台在線」與「Lease 持有」是兩件事，在線不代表可前景操作 | D12／D46 |

**是否重開 D46：否。** 理由：(1) D46 管的是「**哪台裝置**」的解析，本增補只處理「**哪個平台連線、哪個場域**」，兩者在訊框上以 `target` 欄分離；(2) 把遠端執行拉進 Part S 並不改變 D12「前景桌面工作綁 Presence」與 D46「背景明確帶 `target_device_id`」的任何前提；(3) 唯一壓力是 D45：連線方向與傳輸若不先定，S2 的遠端 executor 無法實作，故建議**拆分 D45**——平台層（連線方向、通道加密與每訊框 MAC、金鑰以 `platform_id` 為單位）併入本增補提前裁決；裝置層（每裝置金鑰、NAT／VPN）仍留在 H1 之前（DQ-1）。Part H 因此縮小：H1 的「登記節點、能力、在線狀態」大半由本增補的宣告通道承擔，H1 變成「在已有通道上加 device 層」；H2 不變；H3 變成「沿用 RX 並補長時操作」。TASKS Part H 的描述需同步微調（見 §8）。

---

## 7. 驗收案例

**VC-PD-1 平台自報分類無效（PD-3）**
- 給定：`discord` 宣告候選 `post_message`，`hints` 自稱 `internal_read`、`version=1`；核准清單該項為 `external_effect`／`per_action`／`version=3`。
- 則：`resolve("discord.post_message")` 回核准清單值；`tool_intent.side_effect_hint=internal_read` 無影響；action 進 `WAITING_APPROVAL`；平台離線時 `resolve` 仍回同一結果（PD-4）。

**VC-PD-2 宣告漂移自動停用（PD-7、PD-8）**
- 給定：平台重連後宣告同名工具但 schema 多一欄。
- 則：Core 重算 hash ≠ `pinned_schema_hash` → 工具自動停用、進待審目錄、`core.component_health_changed(schema_drift)`；本輪 `capability_set` 不含它；已存在的 v3 `INTENDED` action 在 `execute_action` 被擋（`PlatformUnavailable` 或 `tool_version_changed`，依審核是否已升版）；人工審核採用後 `version=4`，舊 `appr_` `hash_mismatch`。

**VC-PD-3 未知工具進待審（PD-7.2）**：平台宣告核准清單沒有的 `delete_channel` → 進待審目錄、不可見、LLM 提出 `tool_intent` 被 `CAPABILITY_MISSING` 拒絕、不建 `act_`。

**VC-PD-4 場域維度（PD-9、PD-11）**
- 給定：`discord.post_message` 為 `origin_venue`；平台目前場域含 `guild_A/ch_1`；本輪 session 在 `guild_B/ch_9`。
- 則：本輪工具集不含它；同一 session 在 `guild_A/ch_1` 則含；`venues_update` 移除 `guild_A` 後，`guild_A` 下已建立未執行的 action 在階段 1 被 `PlatformUnavailable` 擋；Desktop session 對 Discord 場域不可見（跨場域預設關閉）。

**VC-PD-5 在線條件（PD-9.3、§3.1）**：平台 SUSPECT／DISCONNECTED 時下一輪不列入其工具；重連但未重新 `declare` 時仍不可見（PD-6）。

**VC-PD-6 LLM 不得指定範圍（PD-12）**：Runtime 的 `member_recall` `tool_intent.args` 帶 `guild_id` 或 `present_uids`；`correct_about_me` 帶 `subject=他人 uid` → 皆 `INVALID_TOOL_ARGS`，不建 action；合法呼叫的 `subject` 恆等於 Gateway 重建的 `actor`。

**VC-PD-7 audience 重驗（PD-13）**：呼叫發出後、執行前 Gate 的 presence audience 收窄 → 階段 1 `denied`，無 action 事件；與 `tool_context` 內舊值不一致時以 Gate 當下值為準。

**VC-PD-8 缺漏 fail-closed（PD-14）**：`tool_context` 缺 `audience`／`venue`、或 `member_recall` 在無 roster 的 session → 通用拒絕碼、不建 `act_`、僅計數。

**VC-PD-9 平台資料不授權、命名空間綁定（PD-15）**：`discord` 連線的 `roster_update` 含 `desktop:owner` 身分斷言 → 忽略並計數，`actor_id` 不變；roster `as_of` 過期 → `member_recall` fail-closed；兩位成員同名 → 不猜、請使用者確認。

**VC-RX-1 ack 遺失 → UNKNOWN → 不重送（RX-7）**：`external_effect`／`via_action_id` 工具，dispatch 已寫入、連線中斷 → `ToolResponseLost`、action `UNKNOWN`；Core 傳輸層未重送（fake 平台記錄收到 dispatch 恰 1 次）；同 Run 後續非 `internal_read` action 被 `ActionBlocked`（TC-3）。

**VC-RX-2 重連對帳（RX-10、RX-9）**：續上；平台重連（同 `ledger_id`、同 `ledger_epoch`）回 `found` → action 依 receipt 推進；改測 `never_seen` 且 `endpoint_auth`＋`via_action_id` → `confirmed_not_executed`，action 可回 `INTENDED`（無任何已存 receipt 時，SA-9）；改測 `endpoint_best_effort` → `undetermined`，維持 `UNKNOWN`。

**VC-RX-3 ledger 世代變化（RX-9）**：平台以新 `ledger_epoch` 重連並對舊 action 回 `never_seen` → 映射為 `undetermined`；`core.component_health_changed(ledger_changed)`；`endpoint_auth` 工具暫降為非權威。

**VC-RX-4 receipt 來源驗證（RX-8）**：(a) 平台 B 對平台 A 的 `action_id` 送 receipt（MAC 有效）→ `source_verified=False`、不推進；(b) `params_hash` 回聲不符 → 同上；(c) MAC 錯 → 丟棄；(d) 舊連線但同 `ledger_id` 的晚到 receipt → 入庫並推進；(e) `UNKNOWN` action 的 `source_verified=False` receipt 存在時，`confirmed_not_executed` 不得退回 `INTENDED`（SA-9）。

**VC-RX-5 去重（RX-4）**：同 `action_id` 的 dispatch 被平台收到兩次（例如測試刻意重送）→ 只執行一次，第二次回既有 receipt。

**VC-RX-6 平台拒絕（RX-5、RX-6）**：核准清單升版後平台仍執行舊版 → 回 `rejected_before_exec(version_mismatch)` → action `FAILED`（`source_verified=True`）而非 `UNKNOWN`。

**VC-RX-7 憑證邊界（RX-12）**：Runtime 行程環境與 `capability_set` 找不到連線金鑰與任何平台憑證；`dispatch` 的 `params` 無 Core 密鑰；單一工具 `disabled`、金鑰撤銷、全域停止三種開關皆在不依賴平台回應下生效。

**VC-RX-8 通道認證（RX-11）**：重放舊 `seq` 的 `receipt`、以舊 `connection_id` 的 `dispatch` 回應、未簽章的 `dispatch` → 皆被拒；`dispatch` 被中間人竄改 `params` → 平台 MAC 驗證失敗而不執行。

**VC-RX-9 同步逾時不阻塞全系統（RX-1、RX-7）**：平台無回應 → 在 `transport_timeout` 後 `ToolResponseLost`，不超過 `transport_timeout_max`；其他 Run 不受影響。

**VC-RX-10 結果 scope（RX-13）**：平台回傳 receipt 的項目自帶 `scope=public`、核准清單 `result_scope=private` → 以 `private` 計；平台標 `private` 而清單為 `group` → 收窄為 `private`。

---

## 8. 切片排入建議

### 8.1 與 SYS 草案 §1.3 切片的關係

切片 0（已決定）：`ToolSpec` 最小欄位＋預留 `provider`／`targeting`、`ToolSpecResolver`、`create_action` 改介面、`params_hash` 強化。本增補**不要求改動切片 0**；以下切片都在其後，且可在 fake 上完成。

| 切片 | 內容 | 屬於 | 前置 |
|---|---|---|---|
| R0 | `ToolSpec` 新增帶預設欄（`provider_ref`、`venues`、`transport_timeout`、`pinned_schema_hash` 等）；`ToolCall.tool_version`；`ConnectionDirectory`／`ConnectionTransport` 協定；PD-1～PD-4、PD-8 的純資料與測試（fake 連線） | S2 | 切片 0 |
| R1 | Connection Registry（狀態機 §3.1、`platform.*` 事件）、`declare` 驗收（PD-7）、平台宣告目錄、核准清單載入與組裝斷言（RX-2）、PD-9 可見性（含場域）、`uep/fakes/platform.py`（in-process fake 平台）| S2 | R0；CC-1 註冊 `platform.*` |
| R2 | `RemoteToolExecutor`（兩鍵，RX-1～RX-10、RX-13、RX-15）、對帳、`executor_ready` 前置檢查（DQ-5）、ledger 世代 | S2 | R1；SA-9、D62 已實作 |
| G1 | Gateway 通用規則 PD-12～PD-15；`member_recall` 與 `retract/opt_out/correct_about_me` 以 builtin 註冊（PD-16），取代 P3 的 `CoreToolExecutor` 白名單（D79 過渡偏離清償） | S2／S3 | 切片 0～1；與 R 系列互相獨立，可平行 |
| R3 | 真實傳輸：TLS、每訊框 MAC、`seq`/`connection_id`（RX-11）；Desktop／Discord 平台端參考實作（含持久 ledger）；`roster_update` 真實來源 | **跨 P5（Discord Adapter 實測）與 Part H 之前**；啟用任何真實遠端工具的**前置條件** | R2；DQ-1、DQ-3 |
| H | 裝置層：`(platform_id, device_id)`、D45 裝置層、D46、非同步變體（RX-14）、GUI 類 receipt | **Part H** | R3 |

### 8.2 哪些屬 S2、哪些屬 Part H

- **S2（Part S）**：PD-1～PD-17、RX-1～RX-13、RX-15、R0～R2、G1，以及全部以 fake 平台驗證的案例（VC-PD-*、VC-RX-1～7、9、10）。
- **R3（跨週期）**：RX-11 與 VC-RX-8 需真實傳輸才有鑑別力（比照 VC-S3-3 的部署前提）。
- **Part H**：RX-14 非同步變體、多連線、裝置層金鑰與目標解析。
- **需連動修改的上位文件**（本增補不代改，列為待辦）：SYS 草案 §1.2、§3.1（`targeting`、新欄、`origin`）、TG-5、TG-12、TC-1；Core 草案 §1.3 事件註冊表與 CC-6 `execute_action`（若採 DQ-5）；TASKS：Part H H1／H3 描述（改述為「建立在平台宣告通道與 RX 之上」）與 T-24 移出項 6 的排程（由「Part H 前」提前為「R3」）；DECISIONS：D45 拆分註記。

---

## 9. 待裁決清單（附建議）

編號暫為 DQ，併入 DECISIONS 時換 D90 起。

| 編號 | 題目 | 建議 | 取捨 | 最晚定案 |
|---|---|---|---|---|
| DQ-1 | D45 是否拆分，平台層連線方向／通道／金鑰單位提前裁決 | **拆分**：平台層採 D45 建議（平台主動連 Core 長連線、金鑰以 `platform_id` 為單位、先只支援同機／同 LAN／VPN）併入本增補；裝置層留 H1 | 不拆則 R2 之後無法定傳輸；拆分增加一次裁決 | R1 開工前 |
| DQ-2 | 是否重開 D46 | **否**（§6） | — | — |
| DQ-3 | 每訊框 MAC／通道綁定由「Part H 前」提前到 R3（遠端 executor 啟用前） | **提前** | 增加 R3 工作量；換得遠端工具不依賴 localhost 信任假設 | R2 完成前 |
| DQ-4 | 遠端 executor 鍵：單鍵或兩鍵（權威／非權威） | **兩鍵**（`endpoint_auth`、`endpoint_best_effort`），共用連線層；否則須擴 `ToolExecutor` 協定加 `lookup_authoritative_for(tool)` 並改 `resolve_unknown` | 兩鍵：映射多一個固定鍵；單鍵加方法：改 CC-6 讀取點 | R2 開工前 |
| DQ-5 | `execute_action` 階段 1／1.5 新增 `executor_ready(tool)` 選用前置檢查 | **採用**，與 `presence_check`／`action_guard` 同型 | 改 CC-6；否則離線只能在 executor 內以 pre_call 失敗收尾，approval 已消耗而須重核准 | R2 開工前 |
| DQ-6 | 跨場域工具（Desktop 說「貼到 Discord」）是否開放 | **首版不開**，`venue_policy=origin_venue` 為預設；日後需 `listed`＋`scope_admits`＋核准畫面顯示場域 | 功能受限；換得不洩漏 private 內容到公開場域（INV-07） | P5 前 |
| DQ-7 | 平台提供的 `description` 是否進 LLM | **不進**；LLM 只見 Core 撰寫並儲存的說明；平台說明只給審核者 | 新工具需人工寫說明；排除 injection 面 | R1 開工前 |
| DQ-8 | 核准清單的儲存與審核流程（設定檔／DB、熱載入、誰能核可、審核 UI） | 設定檔為真相，經人類認證介面產生變更；僅艾斯維爾；熱載入須遞增 `version` 並寫 journal | 設定檔較簡單但缺 UI；DB 較彈性但新增權威資料 | R1 開工前 |
| DQ-9 | roster／在場名單來源與新鮮度：平台推送快取加 TTL，或執行時同步查詢 | **推送快取＋`roster_max_age`＋在場名單於執行當下以 `roster_query` 補查（可選）**；過期 fail-closed | 現行 P3 為「呼叫當下重取在場名單」，純快取會弱化；補查增加一次往返 | G1 開工前 |
| DQ-10 | 平台端持久 ledger 的最低要求，無法滿足者的處置 | 要求 `action_id` 去重表持久化且保留期 ≥ `unknown_resolve_horizon`；無法滿足者只能走 `endpoint_best_effort` | Discord Adapter 需有本地持久儲存 | R3 前 |
| DQ-11 | 長時操作的非同步變體 | **S2 不做**（RX-14）；Part H 評估新增 CC-6 子狀態 | 避免 S2 動 CC-6 狀態機 | H2 開工前 |
| DQ-12 | 晚到 receipt 的入庫公開方法 | 確認 `TaskManager` 現有路徑是否可由連線層直接呼叫（T-9 已規定「入庫」，但是否有公開 API 未驗證）；沒有則 R2 補一個，僅接受 `source_verified` 判定後的資料 | 需讀 `uep/tasks/manager.py` 確認 | R2 開工前 |
| DQ-13 | CC-1 §1.3 註冊 `platform.*` 事件 | 採用 PD-2 的兩種事件；payload 僅 ID／雜湊／短碼，宣告走 blob | 沿 D81 流程；新增事件類型 | R1 開工前 |
| DQ-14 | 工具通道金鑰是否與 Presence 端點金鑰分開 | **分開**（PD-1） | 金鑰管理量加倍；換得最小權限 | R1 開工前 |
| DQ-15 | 同一 `platform_id` 多連線 | S2 單一有效連線，新連線取代舊連線（PD-5）；多連線留 Part H | 多 Desktop 前不能同時在線兩台 | R1 開工前 |
| DQ-16 | `retract_about_me`／`opt_out`／`correct_about_me` 的 `side_effect_class` 與 approval | 建議皆 `reversible_local`、`approval=none`（主體恆為呼叫者本人，PD-16.4）；需記憶線確認遮蔽可逆性 | 若記憶線認定不可逆，須升為 `external_effect` 加核准。**記憶線結論（2026-10-31，開發房間 #651）**：opt_out 可逆（`enabled=False` 還原）；retract 資料面可逆但無還原 API（僅遮蔽 actor 讀取，投影不刪）；correct **不可逆**（另寫 actor episode，會進 Dream 與投影）。暫定 opt_out／retract 為 `reversible_local`、無核准；correct 不得歸 `reversible_local`，分類於 G1 定案。**已知例外（#682）**：目前 correct 經 scope engine 的 `engine_resolver` 直接寫 actor engine，繞過 actor 的 adapter（storage_lock 保證不損毀，但不經 salience 評估與隱私意圖掃描）。G1 改法：engine 的 correct 拆兩步，記憶端只做遮蔽，episode 由 Gateway 經 actor 的 adapter 寫入；拆完後 correct 的不可逆部分成為一般 store | G1 開工前 | G1 開工前 |
| DQ-17 | `transport_timeout_max` 與 `unknown_resolve_horizon` 的初值 | 起點 30 秒／7 天（設定項，調校） | — | R2 開工前 |

---

## 10. 風險與已知偏離

1. **平台以自身權限行事**（RX-12.4）：被攻陷的 Adapter 可在其核准工具的範圍內濫用平台憑證；Core 只能靠核准清單、per_action 核准、場域限制、停用開關縮小影響。此為提案 §18.1 已承認的信任邊界。
2. **`UNKNOWN` 阻擋同 Run**（§3.3）：平台長時間離線時，Run 無法再發動非 `internal_read` 的新 action；需靠重連對帳或人工出口。
3. **對 D79 過渡偏離的清償時機**：P3 的 `CoreToolExecutor` 在 G1 之前維持原狀；G1 完成前，PD-12～PD-14 只在 Host 層以現有形式存在。
4. **roster 為平台提供資料**：名稱解析正確性依賴平台名冊品質；解析錯誤不會擴權（PD-15），但可能讓 `member_recall` 查錯人；以「不唯一不猜」與 STT 容錯規則（D87）緩解。
5. **TG-9 首版不過濾（D56）**：平台工具結果同樣不過濾；RX-13 只保證逐項 scope 與收窄規則存在。
