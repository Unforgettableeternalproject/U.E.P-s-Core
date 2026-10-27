# U.E.P 新 Core 契約草案

對應任務：T-08（change `core-contract-draft`）。對應提案 §24.1 第 3 項「契約草案」。
上游：`UEP_新架構研究/UEP_新架構技術設計提案_2026-10-25.md`（下稱「提案」）、`Core現況對照表.md`（下稱「現況表」）、`hidden_docs/DECISIONS.md`（下稱「DECISIONS」）。

## 0. 定位與約定

### 0.1 本草案是什麼

- 這是 **草案**：定義新 Core（頂層套件 `uep/`，A7）六份契約（外加 D35 新增的輕量第七契約 CC-7 State Coordinator）的欄位、權威擁有者、不變條件與驗證案例。不含程式碼、不含 DB migration，型別僅為示意。
- 所有欄位與狀態以提案為準；提案沒講清楚、或本草案必須補一個才能成立的地方，欄位表標「草案補充」，並在 §10「待裁決」編號列出，**不自行定案**。
- ADR-001～015（DECISIONS 的 D1–D15）仍為 PROPOSED。本草案以提案主張為預設；每份契約標明所依賴的 ADR，若該 ADR 被調整或否決，該契約需重審。
- A8（艾斯維爾 2026-10-26）已裁決 D22、D23、D30、D31、D32、D33、D34、D35，本草案已落實（見 §10）；A1／A4／A5／A7 同為前提：Core 從頭重寫；Event Bus 程式碼沿用並加 durable journal 封裝；Session 只沿用 GS／CS／WS 語意與 ID；新 Core 在 `uep/`。

### 0.2 欄位表記號

| 記號 | 意義 |
|---|---|
| 型別 | `str`、`int`、`bool`、`ts`（UTC ISO 8601，DB 時鐘為權威）、`enum{...}`、`ref<X>`（指向 X 的穩定 ID）、`obj`、`list<T>`、`?` 表可空 |
| 必填 | `Y` 必填；`N` 可省略；`C` 條件必填（說明欄寫條件） |
| 寫入者 | 欄位由誰決定值。「Gateway」指 Event Gateway；「GW 重建」指不信任外部 payload、由 Gateway 驗證後重寫 |
| 草案補充 | 提案未明列、本草案為使契約成立而新增，必須由人確認（見 §10） |

### 0.3 ID 與命名

- 穩定 ID 一律字串，格式為 `前綴_ULID`（D32，已定案 A8）。前綴：`evt_`、`actv_`（activity）、`task_`、`run_`、`step_`、`act_`（action）、`turn_`、`seg_`（segment）、`conv_`、`lease_`、`xfer_`、`out_`、`dsp_`（dispatch）、`dlv_`、`snap_`、`appr_`、`req_`（state transition request）、`obx_`（outbox）、`blob_`（payload blob）、`trc_`（trace）、`rcpt_`（receipt）、`att_`（attempt）。
- legacy ID（`gs_／cs_／ws_`）**只存 `legacy_ref`**，不作為新主鍵（D23，已定案 A8），但須可解析。§8.3 以此為準，segment 的原生前綴為 `seg_`。
- legacy 的 `turn_{n}`（每個 CS 內自增）與 `step_{n}`（每個 WS 內自增）**非全域唯一**，不得單獨當主鍵；新 Core 的 `turn_id`／`step_id` 為全域唯一值，legacy 值只存 `legacy_ref`，或以複合鍵 `(segment_id, turn_n)`、`(task_id, step_n)` 表示。
- 事件類型採 `領域.動詞過去式` 點分字串（例 `task.completed`），非封閉 enum（現況表 §4.1 指出 `SystemEvent` 封閉 enum 之限制）。
- 時間：持久化欄位用 `ts`；跨主機延遲只比較同端 elapsed time（提案 §19.1），不相減外部時鐘。

### 0.4 契約依賴方向（單向）

依賴方向（A -> B 表示 A 可引用 B 的型別或 ID，反向禁止）：

```
CC-3 Runtime Adapter -> CC-2 ContextSnapshot
CC-5 Output/delivery -> CC-4 Presence/transfer
CC-6 Task/action     -> CC-4 Presence/transfer（僅 presence_bound 查詢）、CC-2（快照）
CC-2、CC-3、CC-4、CC-5、CC-6、CC-7 -> CC-1 Event（事件載體）
CC-1 Event           -> （不依賴任何其他契約）
```

以下為「以 ID 參照」的邊，**不構成型別依賴**（只持有對方的穩定 ID 或唯讀投影，不 import 對方型別）：CC-2 的 `history_entries.delivery_state` 取自 CC-5 的 `deliveries`；CC-5 的 O-7 引用 CC-6 receipt；CC-3 的 `checkpoint` 與 `resume` 引用 CC-6 checkpoint；CC-6 的 `presence_bound` 查詢 CC-4 當前 Lease；CC-4 的 `owner_activity_id` 指向 CC-6 Activity；CC-7 以事件訂閱各 Manager 的狀態事件（不引用其型別）。CC-7 State Coordinator 依賴 CC-1，不依賴 CC-2–CC-6。

規則：CC-1 只以 `payload` 承載其他契約內容，以 ID 互相參照。Runtime（CC-3）只讀 CC-2、只產出 CC-5 的文字增量與 CC-6 的 tool intent，不能寫 CC-4。

### 0.5 INV 速查（提案 §3.2）

| INV | 內容（摘要） |
|---|---|
| INV-01 | 同一 `persona_id` 最多一份可核准輸出的有效 Lease |
| INV-02 | 發話、主動通知、動畫、前景桌面動作均受 Presence／Policy 閘門 |
| INV-03 | 舊 Lease／Core epoch／turn generation 的輸出不可復活 |
| INV-04 | SDK、LLM、Adapter 不可自行宣告 Presence 或改全域權威狀態 |
| INV-05 | 已生成、已合成、已送出、已播放、使用者已知是不同狀態 |
| INV-06 | 任務完成、對話結束、離場、核心關機是不同生命週期事件 |
| INV-07 | 私人記憶不因同一人格或跨平台延續自動進入公開頻道 |
| INV-08 | 無法證明舊端已停止時，嚴格模式暫停新端啟用 |

### 0.6 範圍（scope）對照與偏序

`scope_ref`（CC-1 事件）、`audience_scope`（CC-2 快照、CC-5 輸出）、`memory.scope`（MEM 記憶）三者使用**同一個型別**，避免各自解讀：

`ScopeRef = {level: enum{private, group, public}, owner_id?, group_id?}`；`scope_ref` 欄位是其序列化字串（例 `scope_private_owner`，對應 `level=private, owner_id=owner`）。

| 欄位 | 語意 | 取值來源 |
|---|---|---|
| `scope_ref`（事件） | 該事件內容的最寬可見範圍 | Gateway 重建 |
| `audience_scope`（快照／輸出） | 這一段對話、這一次輸出的實際聽眾範圍 | Core 依當前 Presence／頻道決定 |
| `memory.scope` | 該筆記憶允許被看見的最寬範圍 | MEM Access Layer |

偏序（寬度）：`private < group < public`（rank 0 < 1 < 2）。比較規則：

- **放入規則**：內容（記憶、歷史、摘要、事件）可進入某 audience，當且僅當 `rank(audience) <= rank(內容.scope)`，且 `private` 需 `owner_id` 相同、`group` 需 `group_id` 相同。`scope_admits` **只比同級歸屬**（audience 與內容同 level 時才比對 owner／group）；跨級情形（例如 private audience 讀取 group 內容）的成員資格**不由此函式判斷**，由 MEM ACL 在來源端保證。例：`private` 記憶不能進 `public` audience；`public` 內容可進 `private` audience。
- **衍生規則**：衍生事件或摘要的 `scope_ref` 之 rank 不得大於其所有來源的最小 rank（只縮不放）。
- 「角色自我資料」與「各使用者私人」是否為獨立 level，提案 §15.4 未定，見 D38。

---

## 1. 契約 CC-1：Event（UEPEvent＋journal）

依賴 ADR：ADR-002（單程序＋DB）、ADR-007（State Coordinator 集中寫入）、ADR-008（Task／Run 與 GS／CS 分離）、ADR-010（PostgreSQL 權威；原型可 SQLite）。對應提案 §7、§15。
A4 前提：Event Bus 程式碼沿用，journal 以封裝層加在 Bus 之前，不改動「單消費者線程分發、handler 例外隔離」語意。

### 1.1 UEPEvent 欄位

| 名稱 | 型別 | 必填 | 寫入者 | 說明 |
|---|---|---|---|---|
| `schema_version` | int | Y | 產生者 | 封裝版本，從 1 起 |
| `event_id` | str | Y | Gateway／Core | 全域唯一，格式 `evt_<ULID>`（D32），**不得**用毫秒時間戳（現況表：legacy `evt_{ms}` 同毫秒會撞號） |
| `event_type` | str | Y | 產生者，Gateway 驗證於註冊表 | 點分字串；未註冊類型被拒或進 dead letter |
| `delivery_class` | enum{durable, interaction, control, telemetry} | Y | GW 重建（由 event_type 註冊表查得） | 對應提案 §7.1。ephemeral media 不使用本封裝 |
| `occurred_at` | ts | Y | 產生者 | 外部時鐘，僅供資訊，不用於排序 |
| `received_at` | ts | Y | Gateway | Core 接收時間（DB 時鐘） |
| `journal_seq` | int | Y（落地後） | Journal | 全域單調遞增接收序號；排序依據之一 |
| `source` | str | Y | GW 重建 | 已認證的服務／Adapter 身分，不取自 payload |
| `persona_id` | str | Y | GW 重建 | 首版恆為 `uep` |
| `actor_id` | str | Y | GW 重建 | 已認證 actor；未驗證平台身分不得填成 owner |
| `conversation_id` | str? | N | 產生者，Core 綁定 | 非互動事件為空（提案 §7.2：不為背景維護硬建 GS）。格式須為 `前綴_ULID`（E-20） |
| `segment_id` | str? | N | Core | ConversationSegment（對應 legacy CS）。格式須為 `前綴_ULID`（E-20） |
| `activity_id` | str? | N | Core | 事件所屬 Activity（見 CC-6 §6.1a）；格式須為 `前綴_ULID`（E-20）；互動事件填 interaction_episode 的 Activity，任務事件填該 Task 的 Activity。非互動事件可空 |
| `task_id` / `run_id` | str? | N | Core | 任務關聯。`task_id`、`run_id` 格式須為 `前綴_ULID`（E-20） |
| `turn_id` | str? | N | Core | 正式 `turn_id` 由 Core 綁定一次（提案 §6.2）。格式須為 `前綴_ULID`（E-20） |
| `legacy_ref` | obj? | N | 相容層 | `{gs_id?, cs_id?, ws_id?, cycle_index?, turn_n?, step_n?, memory_token?}`；legacy ID **只存此處**（D23 已定案），僅遷移期使用（見 §8.3） |
| `trace_id` | str | Y | Gateway（缺則建立） | 一條因果鏈共用。外部帶入者限安全字元集（E-20） |
| `causation_id` | str? | N | 產生者 | 直接起因事件的 `event_id`；限安全字元集（E-20） |
| `idempotency_key` | str | C | 產生者 | 外部來源事件（Adapter／平台／worker）必填；Core 內部事件由 Core 產生。長度 ≤ 256、字元集 `[A-Za-z0-9_:.-]`（E-20）；重送命中時的一致性核對見 E-17 |
| `aggregate_type` / `aggregate_id` / `aggregate_version` | str / str / int | C | Core | 屬於某聚合（task、presence、transfer…）的事件必填；版本在同一交易檢查。`aggregate_id` 限安全字元集（E-20）。journal 層只靠唯一約束擋重複版本，**不檢查版本連續**，連續性責任在呼叫端（E-22） |
| `priority` | enum{low, normal, high, urgent} | Y | GW 重建 | 外部 payload 自稱「緊急」不生效（提案 §9.3） |
| `expires_at` | ts? | N | 產生者／Gateway | 過期事件不再觸發主動打擾，仍可留存稽核 |
| `scope_ref` | str | Y | GW 重建 | 資料可見範圍（私人／群組／公開），**只能縮不能放大**（見 E-6） |
| `fence` | obj? | C | Core | 與 CC-4 授權六元組同構：`{persona_id, endpoint_id, lease_id, fencing_epoch, core_epoch, presence_generation}`，互動回合另加 `turn_generation`；承載輸出或位置相關動作的事件必填（CC-4／CC-5） |
| `payload` | obj | Y | 產生者 | 僅限**不含私人原文**的小型結構化內容（ID、狀態、計數、結果碼）；其餘一律走 `payload_ref`（D33） |
| `payload_ref` | str? | C | 產生者 | 指向可刪除 blob 的引用；含私人原文（對話、轉錄、記憶內容）、音檔、長文時必填 |
| `payload_hash` | str? | C | Journal | `payload_ref` 內容的雜湊；blob 被刪除後仍保留，供完整性稽核。格式 `hmac-sha256:<kid>:<hex>`（E-18） |

#### delivery_class × journal × 投遞通道

| delivery_class | 落 journal | 投遞通道 | 說明 |
|---|---|---|---|
| durable | 是（先於投遞，E-1） | 程序內 Bus（journal 成功後）＋Outbox（需重送者） | 領域事件，可重試、可重播 |
| interaction | 是（認證、去重後保存必要內容） | 程序內 Bus | 使用者輸入；原始音訊不在此列 |
| control | 結果與權威狀態落 journal；**訊號本身不排在 journal／Bus 佇列後** | 獨立高優先控制通道，直達 Sink／Adapter | stop／abort／revoke；不得被音訊資料或慢 handler 阻塞（提案 §6.4） |
| telemetry | 否 | 指標／trace 儲存 | 保留政策另定，不承載授權或狀態 |
| （ephemeral media） | 否，不是 UEPEvent | 專用串流與有界緩衝 | 只有其控制與交付結果以事件記錄（E-9） |

### 1.2 Journal、Inbox、Outbox 欄位

| 實體 | 欄位 | 約束 |
|---|---|---|
| `events`（journal） | `event_id` PK、`journal_seq` UNIQUE、`(source, idempotency_key)` UNIQUE（當 key 非空）、`aggregate_id`＋`aggregate_version` UNIQUE、`event_type`、JSONB `payload`（僅無私人原文的小型內容，由 EventSpec 白名單強制）、`payload_ref`、`payload_hash`、`tombstone_at?`、`trace_id`、`scope_ref`、`received_at` | append-only；不更新、不刪除、不 TRUNCATE（列級 `events_append_only`＋語句級 `events_no_truncate` trigger，E-21）。journal **只存元資料＋`payload_ref`＋`payload_hash`**（D33）；刪除請求只刪 blob，journal 留雜湊與墓碑（`tombstone_at`、`tombstone_reason`），保留期限屬政策 |
| `inbox` | `(consumer_id, event_id)` UNIQUE、`claimed_at`（認領時間；**沒有** `completed_at`）、`result` | 認領與 consumer 的 DB 效果同一交易提交（以 savepoint 包住，效果失敗則連認領一併回滾），故「列存在」即表示效果已提交，不需另記完成時間 |
| `outbox` | `outbox_id` PK、`event_id`、`target`、`attempts`、`retry_at`、`acked_at?`、`dead_letter_reason?` | 與觸發它的狀態變更同一交易寫入；重試上限、退避、jitter；毒性事件進 dead letter |

### 1.3 起始事件類型（供其他契約引用）

本表為 §8.1 所用新類型的**完整註冊表**（§8.1 用到的類型必須在此註冊）；legacy 對照見 §8.1。

| 類別 | event_type | delivery_class |
|---|---|---|
| 輸入 | `input.text_final`、`input.utterance_final`、`input.attachment_provided` | interaction |
| 回合 | `turn.started`、`turn.generation_completed`、`turn.completed`、`turn.cancelled` | durable |
| 對話 | `conversation.episode_started`、`conversation.episode_ended`、`conversation.segment_started`、`conversation.segment_ended` | durable |
| 狀態 | `state.transition_requested`、`state.changed` | durable |
| 任務 | `task.created`、`task.completed`、`task.failed`、`task.cancelled`、`task.input_requested`、`task.input_provided`、`run.started`、`run.ended`、`step.completed` | durable；`task.*` 只由 Task Manager 發 |
| 動作 | `action.intended`、`action.approved`、`action.executed`、`action.unknown` | durable |
| 在場 | `presence.arrival_requested`、`presence.departure_requested`、`presence.transfer_requested`、`presence.granted`、`presence.activated`、`presence.quiesced`、`presence.revoked`、`presence.recovering`、`presence.recovered` | durable；其中 stop／revoke 的即時訊號走 control |
| 輸出 | `output.intent_created`、`output.admitted`、`output.rejected`、`delivery.reported` | durable |
| 控制 | `control.stop`、`control.abort`、`control.revoke` | control |
| 記憶 | `memory.record_created`、`memory.consolidated` | durable |
| 主動 | `proactive.trigger_observed`、`proactive.candidate_decided` | durable |
| 生命週期 | `core.started`、`core.ready`、`core.stopping`、`core.component_health_changed` | durable |

P1 註記：Task 的 `paused`／`resumed`／`waiting_approval` 等轉移**不另發 `task.*`**；其可觀測性由 CC-7 `state.changed` 承擔（P2 實作）。P1 的核准 UI 依 `action.intended` 事件驅動。

### 1.4 權威擁有者

| 資源 | 唯一寫入者 | 其他角色 |
|---|---|---|
| `events` journal 的 append | Event Journal（Core 內 Gateway 之後的單一寫入元件） | 任何模組只能「提出事件」，不得直接寫表 |
| 信任欄位（`received_at`、`journal_seq`、`source`、`actor_id`、`priority`、`scope_ref`、`delivery_class`） | Gateway | Adapter／Agent／LLM 只能提供 payload 與 idempotency_key |
| `outbox` 狀態 | Dispatcher | 其他元件不得標記 ACK |
| `inbox` 完成標記 | 各 consumer，與自身 DB 效果同交易 | — |

### 1.5 不變條件

| 編號 | 條件 | 對應 |
|---|---|---|
| E-1 | durable／interaction 事件**先**落 journal，成功後才投遞程序內 Bus；落地失敗則不投遞、不產生副作用 | INV-05、INV-06（可重建依據）；提案 §7.1 |
| E-2 | 投遞至少一次；consumer 以 `(consumer_id, event_id)` 去重；外部事件以 `(source, idempotency_key)` 去重；**不承諾**端到端 exactly-once | 提案 §7.3、V20 |
| E-3 | `priority`、`actor_id`、`scope_ref`、`source` 由 Gateway 重建，payload 內同名欄位一律忽略 | INV-04 |
| E-4 | 生命週期事件各自獨立類型：`task.*`、`conversation.*`、`presence.*`、`core.*` 不得互相取代（`task.completed` 不蘊含 `conversation.episode_ended`，反之亦然） | INV-06 |
| E-10 | `core.stopping`（核心關機）**不**蘊含 `task.cancelled`／`task.failed`，也不蘊含使用者語意的離場；關機時 Presence 授權以 `presence.revoked`（`reason=core_shutdown`）關閉輸出，重啟後依 P-4／T-7 恢復 | INV-06 |
| E-11 | journal 不保存私人原文：含私人原文的內容只存於可刪 blob（`payload_ref`），刪除只動 blob，journal 保留 `payload_hash` 與墓碑（D33） | INV-07 |
| E-16 | **隱私取捨（已知限制）**：(a) `payload_hash` 已改為 per-install 密鑰 HMAC（D42／A16 已實作，格式與驗證見 E-18），低熵短文不再能由字典還原；代價是密鑰遺失即無法驗證 HMAC 雜湊，且沒有有效密鑰時拒絕寫入私人 payload（fail-closed，不降級成 sha256）；(b) blob `content` 設 NULL 後，PostgreSQL 舊 tuple、WAL、備份仍可能存有原文，實際清除需 VACUUM 與備份保留政策，納入 P8 備份演練範圍 | INV-07、V24 |
| E-5 | 帶 `fence` 的事件，consumer 比對當前權威 Lease／epoch／generation，舊值事件記錄後丟棄，不得改變當前狀態 | INV-03 |
| E-6 | 事件衍生新事件時，`scope_ref` 依 §0.6 衍生規則只縮不放（rank 不得大於起因事件） | INV-07 |
| E-7 | 重播（replay）預設只重建投影；任何外部輸出、工具副作用在重播中必須被 suppress 或以 action_id 去重 | 提案 §7.3、V20 |
| E-8 | 排序依 `aggregate_version`（同聚合）與 `journal_seq`（全域接收序），不依 `occurred_at` | 提案 §7.2 |
| E-12 | **P1 publisher 範圍**：只接受 `durable`／`interaction`；`control` 走獨立高優先通道、`telemetry` 不落 journal，兩者於 P2 補實作，P1 送入一律拒絕（`UnsupportedDeliveryClass`） | 提案 §6.4、§7.1 |
| E-13 | **Bus 是 best-effort 程序內通知**（`publish(event, *, sync=False)`，訂閱鍵 `str`）：commit 後崩潰可能漏投，投遞順序不保證等於 `journal_seq`。需不遺漏的消費者**必須**走 outbox target（E-2），不得只靠 Bus 訂閱。handler 例外被隔離且**只記例外型別名稱**（不記訊息與 traceback，與 Outbox 一致）；`stop()` 排空逾時而舊消費者線程仍存活時，`start()` 拋 `RuntimeError` 拒絕啟動（避免雙消費者），舊線程結束後才可重啟 | E-1、E-2 |
| E-14 | `journal_seq` 只保證**唯一且單調分配**，不保證提交可見順序（可有空洞，後號可能先可見）。replay／tailing 需「安全水位」才能推進游標，見 D41；游標 `low_txid` 大於目前水位（xmin 單調不減，只可能是叢集已換）時 tailer 拋 `TailCursorMismatch`，不靜默回空，須重置游標；`watermark_lag(session, cursor)` 回報目前 xmin 與游標 `low_txid` 的差供監控（持續變大代表長交易釘住水位或消費者落後） | E-8、V20 |
| E-15 | **Outbox 執行模型**：目前 handler 在持鎖交易內執行；P2 改 lease 模型（鎖列 → 標記 lease → commit → 執行 → ACK），避免長交易持鎖與 handler 阻塞 | E-2、提案 §7.3 |
| E-9 | 音訊／影格不是 UEPEvent，不逐 frame 落 journal；只有其 *控制與交付結果* 以事件記錄 | 提案 §7.1 |
| E-17 | **冪等重送一致性**：`(source, idempotency_key)` 命中既有事件時，須比對 `event_type`、`scope_ref`、`aggregate`（type／id／version）三者與原事件一致；若重送帶 blob，另比對 blob 雜湊（三態見 E-18）。任一確定不符即拋 `IdempotencyConflict`（訊息只含欄位名與 `event_id`，不含內容）；全部一致為真重送，回傳原事件（`duplicate`）、不新增列、不寫 blob。併發重送輸掉競爭者走同一核對 | E-2、V20 |
| E-18 | **`payload_hash` 格式與三態驗證**：新寫入為 `hmac-sha256:<kid>:<hex>`，`kid` = HMAC(密鑰, `"uep-payload-kid"`) 的前 8 位十六進位（只辨識用哪把密鑰，不可回推密鑰）。可辨識三種格式：無前綴 64 位十六進位（T-23 前的 sha256，只驗證不再新寫）、`hmac-sha256:<hex>`（T-24 前無 kid 的 HMAC，只驗證不再新寫）、新格式。驗證結果三態：`MATCH`；`MISMATCH`（可確定內容不同：舊 sha256 不符，或 kid 相同卻驗不過）；`UNVERIFIABLE`（kid 不同，或舊無 kid HMAC 驗不過——可能只是密鑰已輪替）。冪等重送時只有 `MISMATCH` 算衝突，`UNVERIFIABLE`／缺密鑰記 warning 並視為重送。全空白或短於 16 位元組的密鑰視同缺失。`assemble_core(strict=True)` 在缺少、全空白或過短的 `UEP_PAYLOAD_HASH_KEY` 時拒絕組裝（預設只告警） | E-11、E-16、D42 |
| E-19 | **tailer 與只讀交易**：tailer 取水位的交易為短交易、不持有鎖；但任何長時間未結束的交易（包含只讀、停在 idle in transaction 的連線）都會釘住 xmin 使水位不前進（事件不遺失只延遲），使用方須避免長交易並以 `watermark_lag` 監控 | E-14、D41 |
| E-20 | **EventDraft 欄位格式（外部輸入，驗證一律 `fullmatch`）**：`task_id`、`activity_id`、`run_id`、`conversation_id`、`segment_id`、`turn_id` 須為合法 `前綴_ULID`（`is_valid_id`）；`trace_id`、`causation_id`、`aggregate_id` 限安全字元集 `[A-Za-z0-9_:.-]`、長度 1～128（防止夾帶私人文字）；`idempotency_key` 限 `[A-Za-z0-9_:.-]`、長度 ≤ 256；`scope_ref` 須符合 §0.6 格式。ID 與 scope 的驗證不接受結尾換行等 `match` 漏洞 | INV-07、E-3 |
| E-21 | **journal 防清空**：除列級 `events_append_only`（擋 UPDATE／DELETE）外，migration `0007_journal_truncate_guard` 另加語句級 `events_no_truncate`（BEFORE TRUNCATE），擋一次清空整個 journal。測試清庫在交易內暫時停用此 trigger；正式環境不得停用 | E-11、append-only |
| E-22 | **聚合版本連續性不在 journal 層檢查**：journal 以 `aggregate_id`＋`aggregate_version` 唯一約束擋重複，但**不檢查版本連續**（可跳號）。連續性與樂觀併發由寫入聚合的 Manager 在同一交易自行保證 | E-8 |

### 1.6 驗證案例

**VC-1-1 外部重送去重（V20）**
- 給定：Discord Adapter 以 `idempotency_key=disc:msg_991` 送出 `input.text_final`，Gateway 已落 journal 並回 ACK 給 Adapter 前 Adapter 逾時。
- 當：Adapter 以相同 key 重送同一事件。
- 則：journal 僅 1 筆；第二次請求回傳原 `event_id`；下游 consumer 的 DB 效果（建立 turn）只發生 1 次。

**VC-1-2 Outbox 崩潰重送（V20）**
- 給定：`task.completed` 已與 task 狀態同交易寫入 outbox，Dispatcher 投遞給 consumer A 後、記錄 ACK 前程序崩潰。
- 當：Core 重啟，Dispatcher 重送。
- 則：consumer A 的 inbox 命中 `(A, evt)`，不再執行第二次效果；outbox 最終 `acked_at` 有值；通知僅一次。

**VC-1-3 欺騙欄位被忽略（INV-04）**
- 給定：Website Adapter 送出 `input.text_final`（註冊表預設 `priority=normal`），payload 內夾帶 `priority=urgent`、`actor_id=owner`。
- 當：Gateway 接收。
- 則：journal 內 `priority=normal`（取註冊表／Policy 結果），`actor_id` 為 Adapter 認證的 guest 身分；payload 內同名欄位僅作為不被信任的資料保留。

**VC-1-4 同 key 不同內容（E-17、E-18）**
- 給定：`(disc, disc:msg_991)` 已落 journal，原事件為 `input.text_final`、`scope_private_owner`。
- 當：Adapter 以相同 key 重送，但 `event_type`、`scope_ref`、`aggregate` 任一不同，或帶的 blob 以同一把密鑰（kid 相同）驗不過。
- 則：拋 `IdempotencyConflict`，journal 不新增列；若僅是密鑰已輪替（kid 不同）而無法驗證 blob，則記 warning 並回傳原事件（duplicate），不誤報衝突。

---

## 2. 契約 CC-2：ContextSnapshot

依賴 ADR：ADR-001（Core 掌握狀態）、ADR-013（audience 限制上下文）。對應提案 §6.3、§14、§17.1。

### 2.1 ContextSnapshot 欄位

快照在建立後 **不可變**（實作為深度不可變：dict 欄位轉唯讀映射、list 轉 tuple，建立後無法就地修改；序列化時還原為 dict／list，X-8）；任何變更（新記憶、摘要完成、Persona／Policy 版本切換）產生新 `context_revision`，新 revision 通常下一輪才生效。一個 run／attempt 固定使用一份快照。

| 名稱 | 型別 | 必填 | 寫入者 | 說明 |
|---|---|---|---|---|
| `snapshot_id` | str | Y | Context Adapter | |
| `context_revision` | int | Y | Context Adapter | 單調範圍＝`(conversation_id, segment_id)`；兩者皆 NULL 的背景快照退到 `run_id`，再退到 `(persona_id, purpose)`。以 DB 產生欄位 `scope_key` 實作：有 conversation 或 segment → `c:<conv>:<seg>`；否則有 `run_id` → `r:<run_id>`；否則 `p:<persona_id>:<purpose>`。唯一約束 `(scope_key, context_revision)`；互動快照不因 `run_id` 拆序列 |
| `purpose` | enum{conversation_turn, task_run, proactive_eval, summarize} | Y | Context Adapter | 草案補充：不同用途的預算與切片規則不同（D24） |
| `persona_id` / `persona_version` | str / int | Y | Context Adapter | 當前 run 固定此版本；更新中不改（提案 §17.1） |
| `policy_version` | int | Y | Context Adapter | |
| `conversation_id` / `segment_id` | str / str | Y（互動）／N（背景） | Core | |
| `turn_id` / `run_id` | str? | C | Core | 對應互動回合或任務執行 |
| `audience_scope` | ScopeRef | Y | Core | 本次聽眾範圍（§0.6）；依放入規則決定可進入的記憶與歷史 |
| `actor_ref` | ref<actor> | Y | Core | 本次互動的已認證 actor |
| `history_window` | obj | Y | Context Adapter | `{from_turn, to_turn, summary_ref?, summary_scope?, summary_source_revision?}`；有 `summary_ref` 時 `summary_scope` 必填，且須過放入規則（X-7） |
| `history_entries` | list<obj> | Y | Context Adapter | 每筆含 `turn_id`、`role`、`generated_text_ref`、`scope`、`delivered_range`、`delivery_state`（取值為 CC-5 `DeliveryStatus` 列舉，非自由字串）；使用者可見歷史以交付範圍為準，不把未播出內容寫成已說出（INV-05） |
| `memory_slices` | list<obj> | Y | Context Adapter（資料來自 MEM Access Layer） | 每筆 `{memory_id, scope, source_refs, embedding_version?, retrieved_at, relevance}`；檢索結果須再以當前 ACL／刪除標記過濾 |
| `memory_status` | enum{ok, degraded_no_memory, timeout, unavailable} | Y | Context Adapter | 逾時降級須坦白標示；短提示不計為實質回應（提案 §6.3） |
| `excluded_summary` | obj | Y | Context Adapter | `{count_by_reason}`：因 scope／ACL 被排除的筆數，**不含內容**（供稽核與驗證）。鍵限原因碼（`[a-z][a-z0-9_]{0,31}`），值為非負整數；自由文字鍵一律拒絕（X-9） |
| `task_refs` | list<obj> | N | Context Adapter | 每筆 `{task_id, scope}`，只帶 ID 與 scope，不帶 Python 物件；`scope` 須過放入規則（X-7） |
| `commitments` | list<obj> | N | Context Adapter | 未完成承諾（跨平台 continuation 允許帶入）。鍵白名單 `{commitment_id, kind, task_ref, due_at, status, text_ref, scope}`，值為長度 ≤ 256 的字串（D33 不夾帶原文）；`scope` 必填且須過放入規則（X-7） |
| `capabilities` | obj | Y | Core | 本次允許的工具集合與 Provider 能力（`text_stream`、`tool_calling`、`cancel_semantics` 等）；Capability 是技術可行，Policy 才是允許 |
| `data_egress` | obj | Y | Core | 允許的 Provider／資料出口；無符合者則明確失敗，不擅自 fallback（提案 §8.3） |
| `token_budget` | obj | Y | Context Adapter | `{max, per_section}`；組裝只做一次 |
| `history_owner` | enum{core_projection, sdk_store} | Y | Core | 每個 conversation 只能一個歷史擁有者，避免 SessionControl 與 SDK 重複寫入（提案 §14.4） |
| `sdk_session_ref` | str? | N | Runtime Adapter 回填，Core 記錄 | `sdk_session_ref ↔ segment_id／run_id` 對應 |
| `built_at` / `valid_until` | ts / ts? | Y / N | Context Adapter | |

### 2.2 權威擁有者

| 資源 | 唯一寫入者 | 說明 |
|---|---|---|
| 快照組裝與 `context_revision` | Context Adapter | Runtime、Echo Stream、SDK 不得自行拼接第二份上下文 |
| 記憶內容與 ACL | MEM Access Layer | Context Adapter 只能經它讀取並裁切 |
| 摘要提交 | Context Adapter，於 `summary_source_revision` 與當前 revision 相符時原子提交 | 否則捨棄 |
| Persona／Policy 版本 | Persona／Context Service | 快照只引用版本號 |

### 2.3 不變條件

| 編號 | 條件 | 對應 |
|---|---|---|
| X-1 | `memory_slices`、`history_entries`、摘要皆須滿足 §0.6 放入規則（`rank(audience_scope) <= rank(來源.scope)` 且 owner／group 相符）；跨 audience 的 continuation 只帶允許的摘要、承諾與 task refs | INV-07、V28 |
| X-2 | 快照對 Runtime 唯讀；Runtime 不能修改 revision、記憶或 history | INV-04 |
| X-3 | 同一段訊息只能由一個擁有者寫入歷史（`history_owner`）；MEM 檢索、SessionControl 壓縮、SDK 歷史不得各自重複注入 | 提案 §6.3、§14.4 |
| X-4 | 歷史條目帶 `delivery_state`（`DeliveryStatus` 列舉）；`cancelled`／`unknown`／`rejected`／`suppressed` 四種未完整交付狀態不得寫成「已完整說出」（`delivered_range.complete` 不可為 true） | INV-05、V08 |
| X-5 | 壓縮摘要在背景以 revision 為基準產生；來源 revision 已變則捨棄；禁止先清空歷史再等摘要 | 提案 §14.3 |
| X-6 | `memory_status != ok` 時，涉及個人事實的回答不得斷言記得；敏感操作暫停 | 提案 §17.1（MEM 不可用） |
| X-7 | **所有帶內容的清單項都帶 scope 並過 `scope_admits`**：`commitments[*].scope`、`task_refs[*].scope`、`history_window.summary_scope`（有 `summary_ref` 時）與 `history_entries[*].scope`、`memory_slices[*].scope` 同一規則，建構快照時即檢查，違反拋 `ScopeViolation`（public 快照不得帶 private 承諾、task ref、摘要） | INV-07、V28 |
| X-8 | 快照深度不可變：容器欄位（`capabilities`、`data_egress`、`token_budget`、`excluded_summary`、`commitments`、`delivered_range` 等）建立後不可就地修改，修改一律產生新 `context_revision` | X-2、提案 §14.3 |
| X-9 | `excluded_summary` 的鍵只能是原因碼、值只能是非負筆數；不得以鍵夾帶被排除內容的文字 | INV-07、D33 |

### 2.4 驗證案例

**VC-2-1 私聊記憶不進公開快照（V28）**
- 給定：Desktop 私聊 segment 產生記憶 M（`scope={private, owner_id=owner}`，rank 0），之後 Presence 轉移到公開 Discord 頻道，新 segment 的 `audience_scope={public}`（rank 2）。
- 當：Context Adapter 為公開 segment 的第一個 turn 建立快照。
- 則：依放入規則 `2 <= 0` 不成立，故 `memory_slices` 不含 M；摘要與 `history_entries` 不含私聊內容；`excluded_summary.count_by_reason` 記錄被排除筆數但不含內容；`task_refs` 僅含 `audience` 允許的任務。

**VC-2-2 摘要競態（提案 §14.3）**
- 給定：背景以 revision 7 產生摘要，產生期間使用者又送出 turn，revision 變為 8。
- 當：摘要完成並嘗試提交。
- 則：提交被拒（來源 revision ≠ 當前）；歷史仍為原始訊息；下一輪快照使用 revision 8 的原始歷史。

**VC-2-3 記憶逾時降級（V23、提案 §6.3）**
- 給定：MEM 檢索超過有界期限。
- 當：建立快照。
- 則：`memory_status=timeout`；`memory_slices` 為空；Runtime 收到此狀態；其回應不得宣稱記得特定事實。

---

## 3. 契約 CC-3：Runtime Adapter

依賴 ADR：ADR-001（SDK 僅執行 Agent Loop）、ADR-009（Model Gateway）、ADR-015（SDK Handoff 不改 Persona／Presence）。對應提案 §8。
Runtime = 可替換的推理／工具迴圈（直達 LLM、OpenAI Agents SDK、Google ADK 等）。

### 3.1 操作

| 操作 | 輸入 | 輸出 | 語意 |
|---|---|---|---|
| `start(request)` | `RuntimeRequest` | 回傳 `attempt_id`，之後以串流送出 `RuntimeEvent` | 開始一次 attempt |
| `resume(request)` | `RuntimeRequest` ＋ `checkpoint_ref` | 同上 | 自 Core 持有的 checkpoint 繼續；**不**接受 Runtime 自存的私有狀態當權威 |
| `cancel(attempt_id, reason)` | | `ack`（`stopped`／`isolated`） | 冪等（含未知 attempt）。無法立即停止（如 GPU kernel）時回 `isolated`，Core 丟棄晚到結果 |
| `stream(attempt_id)` | | `RuntimeEvent` 非同步串流 | 取得該 attempt 的事件流（`start`／`resume` 之後消費）；`start` 只回傳 `attempt_id`，不直接回傳串流 |
| `status(attempt_id)` | | `RuntimeStatus` | 只讀 |

協定型別（`RuntimeRequest`、`RuntimeEvent`、`RuntimeStatus`）以 dataclass 表示。`cancel` 對未知或已結束的 `attempt_id` 同樣冪等（回 `stopped`，不報錯）。

### 3.2 RuntimeRequest 欄位

| 名稱 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `run_id` / `attempt_id` | str / str | Y | `attempt_id` 每次重試／fallback 新建 |
| `snapshot_ref` | ref<ContextSnapshot> | Y | 固定使用此快照（CC-2） |
| `capability_set` | obj | Y | 允許的工具、模型能力、資料出口（來自快照） |
| `cancel_scope` | obj | Y | `{turn_generation, task_run, process}`；用於獨立控制通道 |
| `deadline` | ts | Y | |
| `fence` | obj | C | 互動回合必填：CC-4 授權六元組＋`turn_generation`（只供 Runtime 回帶，Runtime 不解讀） |
| `checkpoint_ref` | ref? | C | `resume` 必填 |
| `mode` | enum{direct_chat, agent_loop} | Y | 一般對話走直達路徑，不強迫多步 Agent（提案 §6.3） |

### 3.3 RuntimeEvent 欄位（Runtime → Core）

| 名稱 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `attempt_id` / `seq` | str / int | Y | 序號單調；Core 用以偵測重複與缺漏 |
| `kind` | enum{text_delta, tool_intent, approval_request, checkpoint, result, error} | Y | 嚴格列舉：不在列舉內的值建構時即拒絕（否則以 kind 取同名欄位會誤取到 `seq`、`attempt_id` 等不相干欄位） |
| `text_delta` | str? | C | `kind=text_delta` |
| `tool_intent` | obj? | C | `{tool, args_complete: bool, args, side_effect_hint}`；`args_complete=false` 的增量**不得**提交（提案 §8.3）。建構時須帶非空字串 `tool` 與布林 `args_complete`，缺任一即拒絕 |
| `approval_request` | obj? | C | `{action_hash, reason}`；僅為請求，不是授權 |
| `checkpoint` | obj? | C | 見 CC-6 checkpoint |
| `result` | obj? | C | `{status, output_ref, usage}` |
| `error` | obj? | C | `{code, retryable, detail}`；`code ∈ {TIMEOUT, CANCELLED, PROVIDER_UNAVAILABLE, EGRESS_DENIED, INVALID_TOOL_ARGS, CAPABILITY_MISSING, RATE_LIMITED}`（草案補充，D25） |

### 3.4 權威擁有者

| 資源 | 唯一寫入者 | Runtime 可做 |
|---|---|---|
| run／attempt 狀態 | Core（Task Manager／Orchestrator） | 回報進度與結果 |
| 對外輸出 | Output Gate（CC-4）＋Delivery（CC-5） | 只能提出 *output intent* 之文字增量，由 Core 轉換 |
| 工具授權與副作用 | Policy＋Tool Gateway＋executor | 只能提出 `tool_intent` |
| Presence、Lease、全域狀態 | Presence Manager／State Coordinator | 無權寫入，也不得持有平台 Bot Token |

### 3.5 不變條件

| 編號 | 條件 | 對應 |
|---|---|---|
| R-1 | Runtime 不得宣告 Presence、修改全域狀態、持有平台憑證、或繞過 Output Gate 發話 | INV-04、INV-02 |
| R-2 | `cancel` 之後晚到的 `text_delta`、`result` 一律丟棄，不得建立 output intent 或改變 turn 狀態；`tool_intent` 不再執行。**已發生的工具副作用 receipt 不丟棄**，仍依 `action_id` 記入 CC-6（副作用不可假裝取消） | INV-03、V07、V21 |
| R-3 | 內部 SDK Handoff 僅為執行者切換：Persona、Presence、對外表達者不變，所有輸出仍由同一 Core 的同一 Persona 版本表達 | ADR-015 |
| R-4 | fallback 在新 `attempt_id` 進行；已有工具副作用者先查 receipt，不重跑整輪 | 提案 §8.3、V21 |
| R-5 | `resume` 只吃 Core 的 checkpoint；SDK 自身 session 僅為歷史投影，非權威 | 提案 §14.4、§17.2 |
| R-6 | 無滿足 `data_egress` 與能力的 Provider 時回 `EGRESS_DENIED`／`CAPABILITY_MISSING`，不得自行改送其他供應商 | 提案 §8.3、V23 |
| R-7 | `RuntimeEvent` 的 `kind` 為嚴格列舉，且恰好帶與 `kind` 同名的那一個 payload 欄位；`kind=tool_intent` 必含非空 `tool` 與布林 `args_complete`（缺漏視為格式錯誤，不猜測補值）；`kind=error` 的 `code` 須在 §3.3 列舉內 | 提案 §8.3、VC-3-2 |

### 3.6 驗證案例

**VC-3-1 取消後晚到結果（V07）**
- 給定：turn T（`turn_generation=5`）的 attempt A 正在串流，使用者插話觸發 `cancel(A)`，Runtime 回 `isolated`（無法立即停止）。
- 當：A 隨後送出 `text_delta` 與 `result`。
- 則：Core 不建立任何 `output_intent`；telemetry 計數器 `runtime.discarded_events{attempt_id=A}` 等於晚到 `text_delta`＋`result` 的件數（此例 2）；若期間有工具 receipt 到達，仍記入 CC-6；新 turn（`generation=6`）的輸出不受影響。

**VC-3-2 不完整工具參數（提案 §8.3）**
- 給定：Runtime 串流送出 `tool_intent` 且 `args_complete=false`，之後 JSON 不合法而 `error(INVALID_TOOL_ARGS)`。
- 當：Core 處理。
- 則：不建立 `action_intent`、不執行工具；不「猜測參數」；attempt 失敗並依 R-4 進行 fallback 或明確失敗。

**VC-3-3 Handoff 不改 Persona（ADR-015）**
- 給定：Runtime 內部由「研究 Agent」handoff 給「寫作 Agent」。
- 當：結果回到 Core。
- 則：輸出 intent 的 `persona_id` 與 `persona_version` 與請求相同；沒有任何 Presence 或 endpoint 欄位改變；journal 無 `presence.*` 事件。

---

## 4. 契約 CC-4：Presence／transfer（Lease＋generation／epoch＋Output Gate）

依賴 ADR：ADR-004（One Identity、One Active Presence）、ADR-005（DB 原子 Lease＋generation／epoch＋Output Gate）、ADR-006（嚴格移轉需 quiescence）、ADR-012（前景桌面工作綁 Presence）。對應提案 §10–§13、§17.3。

### 4.1 PresenceCurrent 欄位

唯一權威 row（每個 `persona_id` 一筆），由 Presence Manager 在 DB 交易內更新。

| 名稱 | 型別 | 必填 | 寫入者 | 說明 |
|---|---|---|---|---|
| `persona_id` | str | Y | — | PK |
| `status` | enum{NONE, ACTIVE, RECOVERING} | Y | Presence Manager | 只留三態（審查 H3；提案 §14.2 的六態列舉待 D27 確認）。`PREPARING／QUIESCING／ACTIVATING` 等移轉階段**只存在於** `presence_transfers.phase`；`ACTIVE` 表示授權與到場確認均成立 |
| `location` | enum{desktop, discord, website} | C | Presence Manager | `status=NONE` 時為空 |
| `endpoint_id` | str | C | Presence Manager | 粒度：Desktop `device_id+client_instance_id`；Discord `guild_id+channel_id+interaction_scope`；Website `room_id+endpoint_instance_id`（提案 §10.2） |
| `owner_activity_id` | str? | N | Presence Manager | 不直接綁短命 CS（提案 §11.1） |
| `lease_id` | str | C | Presence Manager | `ACTIVE` 必填；`NONE` 時可為「已核發、待到場確認」的 Lease（ACTIVATING 期間），此時不得通過 Output Gate（見 §4.5） |
| `core_epoch` | int | Y | Core 啟動程序 | 來源：Host 本機 epoch 檔 +1，且 ≥ DB 內值 +1（兩者取大）；fencing 檔落後 DB → **拒絕啟動**。`presence.start()` 完成前 Output Gate 一律拒絕。啟動順序：`presence.start()` → 才啟動 DeliveryManager／outbox relay |
| `fencing_epoch` | int | Y | 見 D26 | 災難還原後必須大於任何曾核發值，**不可只靠 DB 內遞增整數**（提案 §17.3）。來源與保存位置未定案 |
| `generation` | int | Y | Presence Manager | 只遞增不回退；每次核發 +1 |
| `expires_at` | ts | C | Presence Manager | 以 **Core 單 Host 時鐘**判定（Output Gate 與 Presence Manager 共用同一 clock 物件）。Host 牆鐘回撥列為**殘餘風險**（見 P-13） |
| `row_version` | int | Y | Presence Manager | 樂觀併發版本 |
| `transfer_id` | str? | N | Presence Manager | 進行中的移轉；每個 persona 最多一個未結束移轉 |
| `unquiesced_endpoint_id` | str? | N | Presence Manager | 過期、撤銷或失效後**未證明已停止**的端點；非空時，他端 acquire 回 `NOT_QUIESCED`。標記來自 `revoke` 者維持 `NONE`，直到舊端停止證據被接受或被撤銷 Lease 的 `expires_at` 已過；來自 Lease 過期、啟用逾時、Core 重啟者進 `RECOVERING`，一律須證據或 `resolve_recovery`（P-16） |
| `recovery_reason` | str? | C | Presence Manager | `status=RECOVERING` 時必填（例：`core_restart`、`lease_expired_unquiesced`、`activation_failed`） |

秘密與憑證不放入此物件（提案 §11.1）。

**`presence_leases`（兼歷史）**：每次核發一列 `{lease_id, persona_id, endpoint_id, generation, core_epoch, fencing_epoch, granted_at, expires_at, ended_at?, end_reason?}`，作為稽核與 Gate 查核；有效授權仍只由 `presence_current` 決定（P-1）。

### 4.2 PresenceTransfer 欄位

| 名稱 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `transfer_id` | str | Y | |
| `persona_id` | str | Y | |
| `from_endpoint_id` / `to_endpoint_id` | str? / str | to 必填；from 在「自 NONE 首次 acquire」時為空 | 首次 acquire 也建立 transfer，使階段規則一致 |
| `reason` | obj | Y | 理由、來源（使用者／Planner／Proactive）、預期 `expected_generation` |
| `phase` | enum{PREPARING, QUIESCING, CHECKPOINTED, ACTIVATING, DONE, ABORTED} | Y | 移轉階段唯一存放處。進入恢復時 `presence_current.status=RECOVERING`，`phase` 停在失敗階段並記 `recovery_reason`；`CHECKPOINTED`、`DONE`、`ABORTED` 見 D27 |
| `checkpoint_ref` | ref? | C | `CHECKPOINTED` 後必填 |
| `stop_evidence` | obj | C | 舊端停止證據：`{endpoint_id, old_generation, kind, reported_at}`；綁定 `transfer_id + old_generation`，不是泛用「動畫播完」。`kind` 限白名單 `adapter_stop_report`、`fresh_process`，且須自述已停止（P-17） |
| `deadline` | ts | Y | |
| `row_version` | int | Y | |

### 4.3 OutputAdmission（Output Gate 檢查輸入／結果）

所有可見輸出（文字、TTS、typing indicator、角色進場、動畫、前景工具入口）使用同一規則；admission 發生兩次：Core 接納時與平台發送／播放器接納時。

| 名稱 | 型別 | 必填 | 說明 |
|---|---|---|---|
| **授權六元組** `(persona_id, endpoint_id, lease_id, fencing_epoch, core_epoch, presence_generation)` | 各型別同上 | Y | CC-1 `fence`、CC-5 OutputIntent、CC-3 `fence` 皆與此同構。提案 §11.3 未列 `fencing_epoch`，草案補入（P-5） |
| `activity_id`、`turn_id`、`turn_generation`、`output_id`、`sequence`、`valid_until` | | Y | 其餘欄位（提案 §11.3） |
| `audience_scope` | ScopeRef | Y | 與當前 Presence audience 比對（INV-07） |
| `decision` | enum{ADMIT, REJECT_STALE, REJECT_NO_PRESENCE, REJECT_AUDIENCE, REJECT_EXPIRED, REJECT_REVOKED} | Y（結果） | 結果碼（草案補充，D28） |

### 4.4 操作與結果碼

| 操作 | 發起者 | 結果碼 | 說明 |
|---|---|---|---|
| `acquire(endpoint, expected_version)` | Adapter 經 Core 仲裁 | `GRANTED`、`BUSY`、`VERSION_CONFLICT`、`TRANSFER_PENDING`、`NOT_QUIESCED`、`DENIED` | 同交易鎖定 current row，成功才更新並寫 outbox（`presence.granted`）；`DENIED` 另帶原因碼 `reason`（T-23，見下）。`unquiesced_endpoint_id` 非空且為他端時的處置見 P-16 |
| `renew(lease_id, core_epoch, generation)` | 目前 owner | `RENEWED`、`STALE`、`EXPIRED` | 須精確匹配；過期不可續活，需重新 acquire |
| `revoke(reason)` | Core／全域停止 | `REVOKED` | 全域停止入口不依賴 LLM；`generation` 不 +1；將被撤銷 Lease 的端點記為 `unquiesced_endpoint_id`，狀態回 `NONE`（`RECOVERING` 期間無 Lease，為 no-op，見 P-24），他端放行條件見 P-16（A20／D43） |
| `begin_transfer(...)` | 仲裁後 Core | `xfer_id`／`TRANSFER_PENDING` | 同人格序列化 |
| `confirm_quiesced(endpoint, old_generation, evidence)` | 舊端 Adapter | `ACCEPTED`、`STALE`、`EVIDENCE_REJECTED` | 證據不等於授權；Core 決定是否接受。先判 `STALE`（狀態、端點、generation 不符），再判證據資格：不合格回 `EVIDENCE_REJECTED`，標記維持，端點須補正後重送（P-17、P-18）。接受後清除 `unquiesced_endpoint_id`。P5 的 [B] 移轉流程另帶 `transfer_id` |
| `resolve_recovery(operator, resolution)` | operator（人） | `RESOLVED`、`DENIED` | 需 operator 身分；成功發 `presence.recovered`，`RECOVERING → NONE`（亦清除 `NONE` 下的未證實停止標記）。入口見 P-19（`POST /v1/presence/resolve-recovery`／`python -m uep.stop resolve-recovery`） |
| `confirm_activated(lease_id)` | 新端 Adapter | `ACCEPTED`、`STALE` | |

**`acquire` 的 `DENIED` 原因碼（T-23）**：`AcquireOutcome.reason`（`AcquireDenyReason`）只在結果為 `DENIED` 時有值，其他結果碼一律為空；經端點 RPC 回覆的 `reason` 欄位傳到端點（欄位缺漏或未知值視為無原因，結果碼仍照處理）。

| `reason` | 情境 | 端點行為 |
|---|---|---|
| `global_stop_engaged` | 全域停止旗標成立（T-17），直到明確解除前不核發 | **不重試**（`acquire_retryable=False`）；解除後須由人或上層重新啟動端點 |
| `superseded_core` | DB 內的 `core_epoch`／`fencing_epoch` 已被較新的 Core 取代，本 Manager 是殘存的舊行程（P-4、P-5） | 沿用既有邏輯：視為可重試，重連到新 Core 後重新 acquire |
| `guard_rejected` | `acquire_guard` 回了未歸類的拒絕原因 | 可重試 |

Lease TTL 30 秒、心跳 5 秒僅為 **調校起點**（提案 §11.4），不是契約常數；Adapter 用單調時鐘與保守剩餘期限，晚到續約回覆不得延長已失效 Lease。

### 4.5 合法移轉（摘自提案 §12.1，改以 status／phase 兩層表示）

`presence_current.status` 只有 `NONE／ACTIVE／RECOVERING`；階段在 `transfer.phase`。

**實作範圍（P2）**：P2 **只實作 [A] 與 [C]**，不實作 [B] 位置移轉——`ACTIVE` 時他端 `acquire` 一律回 `BUSY`。[B] 延後到多平台（P5），屆時補 `begin_transfer`／`confirm_quiesced` 流程。

**P2 補充規則**：
- `revoke` **不使 `generation` +1**（只在核發時 +1）。`revoke` 將被撤銷 Lease 的端點記為 `unquiesced_endpoint_id`；他端須等舊端停止證據或被撤銷 Lease 的 `expires_at` 已過才放行（P-16，D43 已定案 A20）。VC-4-1 的重複回合因此改為每回合 `revoke` 後由舊端補送停止證據。
- Lease 過期而未證明已停止 → 他端 `acquire` 回 `NOT_QUIESCED` 並進 `RECOVERING`（即使舊 Lease 的 `expires_at` 早已過，過期路徑仍須證據，與 `revoke` 路徑不同，見 P-16）；原端點在狀態仍為 `NONE` 時可重新 `acquire`，狀態已進 `RECOVERING` 後原端點同樣須停止證據或 `resolve_recovery`（D44／A13，暫定）；`confirm_quiesced` 清除 `unquiesced_endpoint_id`。
- Core 重啟時，原 `ACTIVE` → `RECOVERING`（`recovery_reason=core_restart`）。
- `acquire` 對**同一端點**已處於 `ACTIVATING` 的請求冪等，回同一 grant（VC-4-3(b)）。

```
[A] 自 NONE 首次 acquire（無舊端，from_endpoint_id 為空）
    status=NONE, 無 transfer
      -> 建立 transfer, phase=PREPARING      （僅探測連線、載入資源；不進場、不錄音、不發話）
      -> phase=ACTIVATING                    （同交易：核發新 lease_id、generation+1、寫 presence.granted；
                                              status 仍為 NONE，Output Gate 此時一律拒絕）
      -> 新端 confirm_activated              （同交易：status=ACTIVE、phase=DONE）
    失敗：ACTIVATING 啟用失敗 -> status=RECOVERING（phase 停在 ACTIVATING）

[B] ACTIVE 位置移轉（舊 owner 存在）
    status=ACTIVE
      PREPARING   目標未就緒且舊位置仍有效 -> phase=ABORTED，status 維持 ACTIVE（舊）
      PREPARING -> QUIESCING                 （關閉舊 generation 新輸出 admission）
      QUIESCING -> CHECKPOINTED              （舊端停止證據＋進度保存）
      QUIESCING -> status=RECOVERING         （停止或副作用結果未知；phase 停在 QUIESCING）
      CHECKPOINTED -> ACTIVATING             （同交易撤銷舊 Lease、generation+1、核發新 Lease；status=NONE）
      ACTIVATING -> DONE, status=ACTIVE(新)  （新端到場確認）
      ACTIVATING -> status=RECOVERING        （啟用失敗）

[C] 恢復
    status=RECOVERING -> status=NONE         （已確認各端停止；transfer.phase=ABORTED）
    status=NONE -> 重新依 [A] 重試或改選位置
```

任一階段 `status != ACTIVE` 時 Output Gate 一律拒絕該 persona 的輸出（`REJECT_NO_PRESENCE`）。

`PREPARING` 階段可探測連線、載入資源，**不得**角色進場、錄音或發話。

### 4.6 權威擁有者

| 資源 | 唯一寫入者 | 其他角色 |
|---|---|---|
| `presence_current`、`presence_transfers`、`presence_history` | Presence Manager（DB 交易） | Planner 只提議（`PresencePlan`，行程是意向非授權）；Adapter 回報 readiness／stop 證據；LLM／SDK 不得寫 |
| Output Gate 決定 | Output Gate（Core 內）；平台端二次檢查由 Adapter 執行 | Adapter 的檢查依據為 Core 下發的授權，不可自行延長 |
| `core_epoch` | Core 啟動程序（交易） | — |

### 4.7 不變條件

| 編號 | 條件 | 對應 |
|---|---|---|
| P-1 | `presence_current` 以 `persona_id` 為 PK；有效授權由此唯一 row 決定，歷史可多筆但不構成授權 | INV-01 |
| P-2 | 核發／續約／撤銷在同一交易鎖定 current row 並檢查 expected version、owner、status、到期、移轉狀態；不得「先查布林再釋放鎖後寫」 | INV-01 |
| P-3 | `generation` 單調遞增；六元組中任一值落後（舊 `lease_id`／`fencing_epoch`／`core_epoch`／`generation`）的輸出、續約、ACK 一律拒絕；重連須取得全量權威快照與 `state_revision` 後才續接增量 | INV-03、V12 |
| P-4 | Core 重啟新 `core_epoch`；舊 epoch 的 Lease／run 進恢復查核，**輸出預設全關**；不得直接恢復快照中的 ACTIVE Lease | INV-03、V14、V33 |
| P-5 | 還原舊 DB 後，新增 `fencing_epoch` 並撤銷先前憑證／Lease、重新握手後才允許輸出；禁止兩份還原 Host 同時接正式端點 | INV-03、V24 |
| P-6 | 無舊端停止證據時（stop ACK 不明、在途送出 timeout、副作用未知）進 `RECOVERING`，嚴格模式**不得**啟用新端；在途送出 timeout 標 `UNKNOWN`，不得直接開新位置繼續講 | INV-08、V15、V16 |
| P-7 | 外部網路 I/O 不放進長 DB 鎖；移轉以可追蹤 `dispatch_id` 與有序序列推進 | 提案 §11.3 |
| P-13 | **Output Gate 判定順序**：先檢查 `core_epoch`／`persona`（不符 `REJECT_STALE`），再檢查 `status`；`audience` 與 `turn_generation` 由**必填 provider** 提供，要關閉檢查須顯式 `ALLOW_ALL`（不得預設放行）。時間以 Core 單 Host 共用 clock 判定，Host 牆鐘回撥為殘餘風險 | INV-02、INV-03 |
| P-14 | **端點二次接納**是 Gate→send 的 TOCTOU 閉合點：端點於送出／播放前自行檢查六元組與本地剩餘 TTL；控制通道行為見 §5.3 | INV-03、V07 |
| P-15 | 測試與 CI 必須設定 `UEP_HOST_EPOCH_FILE`；禁止兩個 Core 同時對同一 DB 運作（延伸 P-5） | INV-01、INV-03 |
| P-16 | **revoke 後他端放行條件（A20／D43）**：`revoke` 將被撤銷 Lease 的端點記為 `unquiesced_endpoint_id`（`generation` 不 +1）。他端 `acquire` 於下列任一條件成立前回 `NOT_QUIESCED`，且狀態**維持 `NONE`**（不進 `RECOVERING`）：(a) 該端 `confirm_quiesced` 被接受（標記清除）；(b) 被撤銷那張 Lease 的 `expires_at` 已過（`acquire` 時自動清除標記，不需證據）。(b) 只適用標記來自 `revoke` 者（該 Lease 狀態為 `REVOKED` 且 `end_reason ≠ core_restart`，且屬於該標記端點）；標記來自 `lease_expired`、`activation_timeout`、`core_restart` 者，一律進 `RECOVERING` 並須證據或 `resolve_recovery`，不因 `expires_at` 已過而放行。被撤銷的端點自己可立即重新 `acquire` | INV-08、V15、V17、D43 |
| P-17 | **停止證據資格**：`confirm_quiesced` 的證據 `kind` 限白名單 `adapter_stop_report`（端點回報停止）、`fresh_process`（新行程自證，見 P-18），且證據須含 `playing=False` 與 `buffer_flushed=True`（缺欄位視同未證明）。不合格回 `EVIDENCE_REJECTED`，標記與狀態維持。證據不等於授權（見 §4.4） | INV-08、V15 |
| P-18 | **`fresh_process` 自證**：端點行程剛啟動、本行程從未持有 Lease 也從未輸出，且 Core 回報其為未證實停止端點時，可送 `fresh_process` 證據。端點 id 固定（如 `ep_cli`），新行程無法單憑「我是新的」證明同名舊行程已停，故 Core 要求該端點最後一張 Lease 的 `expires_at` + `fresh_process_skew`（預設 30 秒，沿用端點請求驗證的 skew）已過才接受（該端點沒有任何 Lease 紀錄則直接接受）；理由是舊行程受本地 watchdog 約束，保證在本地 deadline（不晚於 Lease TTL）前自行停止。`adapter_stop_report` 不受此延遲限制。端點遇 `EVIDENCE_REJECTED` 時每 2 秒重試、總等待上限為 `max(端點設定的 lease_ttl_seconds, Core 回報的 TTL) + fresh_process skew + 5 秒`（預設約 65 秒，不再固定）；端點的 `lease_ttl_seconds` 設定須不小於 Core 的 Lease TTL，否則上限可能不涵蓋舊 Lease 到期，自證接受後最多重新 `acquire` 2 次；仍被拒則回 `NOT_QUIESCED`，交 operator（P-19） | INV-08、V15、D44 |
| P-19 | **operator 解除入口**：`resolve_recovery` 的對外入口為 `POST /v1/presence/resolve-recovery`（僅綁 loopback、需 `Authorization: Bearer <UEP_STOP_TOKEN>`、記錄 operator）與 `python -m uep.stop resolve-recovery`（token 只讀環境變數）。它**不碰**全域停止旗標：旗標仍成立時端點照樣無法重新 `acquire` | D36、D44、INV-08 |
| P-20 | **Host epoch 檔並行安全**：`read`／`bump`／`raise_to` 的讀改寫由同行程執行緒鎖加跨行程檔案鎖（`<epoch 檔>.lock`；Windows 用 `msvcrt`、其他用 `fcntl`）序列化，等鎖逾時 30 秒拋 `HostEpochError`；寫入先 `fsync` 暫存檔再 `os.replace`，可行時再 `fsync` 目錄（Windows 無法開目錄而略過）。兩個行程併發 `bump` 不會得到相同值 | P-4、P-5、P-15 |
| P-21 | **控制通道 hello 雙向證明**：client 的 hello 帶 nonce 並簽章；Core 驗證後以端點 secret 對該 nonce 簽章（訊息域 `uep-hello-proof`，與請求簽章分離）放進 `hello_ok.proof`；client 驗證通過才信任該連線，否則關線並拋 `EndpointAuthError`。Core 在 `hello_ok` 寫出成功之後才把連線登記為推送目標（之前不會有推送搶先寫入）。**邊界**：只做到抗冒名（冒綁埠者沒有 secret，無法讓端點相信持有 Lease），**不抗 relay**（中間人可原樣轉發雙方的 hello 與證明）；之後的回應與推送不逐則簽章，信任邊界是 loopback 與已驗證連線。Part H（跨機器）前須加每則 MAC 或通道綁定（TLS 憑證綁定），否則不得離開 loopback | INV-03、INV-04、D45 |
| P-22 | **端點請求驗證與重啟窗口**：`RequestVerifier` 拒絕 `ts` 早於「驗證器建立時間 − `startup_grace`」的請求（nonce 表只在記憶體，重啟後清空）。組裝層 `build_request_verifier` 取 `startup_grace = skew = 30 秒`。取捨：grace 讓時鐘落後 δ（< skew）的端點在 Core 重啟後立即送的請求仍被接受（grace 為 0 時會被拒）；代價是重啟前最後 skew 秒內被截取的請求，在該窗口內可重放一次（殘餘風險）。同樣在 Part H 前須每則 MAC／通道綁定 | INV-03、D45 |
| P-23 | **全域停止 token 下限**：`UEP_STOP_TOKEN` 至少 16 位元組（UTF-8）；空值或過短視同未設定——本機停止入口拒絕所有請求（fail-closed）、CLI 結束碼 1，並於 `GlobalStop` 建立時與 API 啟動時各告警一次（不輸出 token 內容） | D36、T-10 |
| P-24 | **RECOVERING 期間無有效 Lease**：進入 `RECOVERING` 時 current row 的 Lease 欄位已清除，故此期間 `revoke` 為 no-op（無 Lease 可撤銷），`unquiesced_endpoint_id` 與 `recovery_reason` 不被覆寫；解除只能經停止證據或 `resolve_recovery`（P-17、P-19）。這取代 §4.5 對 RECOVERING 下 revoke 的舊敘述 | P-6、INV-08 |
| P-25 | **凍結／系統睡眠殘餘風險（已知限制）**：端點本地期限與 watchdog 用單調時鐘，不涵蓋行程凍結或系統睡眠；凍結後舊行程至多到下一次 `renew`（約 TTL/3）才自停，已排隊的 chunk 另受 `intent.valid_until` 牆鐘檢查限制。Part H／P8 跨機器前待補牆鐘輔助檢查 | INV-03、V15 |
| P-8 | 每個輸出於 Core 接納與端點接納皆檢查 P-3 授權六元組、`status=ACTIVE` 與 audience（§0.6）；生成內容自身沒有輸出權；stop／abort／revoke 走獨立控制通道，不排在音訊資料後面 | INV-02、INV-03、V06 |
| P-9 | Adapter 保持連線不等於 Presence；缺席時預設不收集原始音訊，到場請求／留言接收權與錄音權分開 | 提案 §10.3、V18 |
| P-10 | presence-bound 任務於離場時須到安全 checkpoint 暫停；已提交且不可中斷的外部動作先完成或查核，不假裝取消成功 | INV-02、V22 |
| P-11 | 已交接的移轉被使用者取消視為新移轉，不得撿回舊 Lease | 提案 §12.3 |
| P-12 | 降級只減少同一位置的能力，不產生第二個 Presence；服務告警不得冒充她在該平台互動 | 提案 §17.1 |

### 4.8 驗證案例

**VC-4-1 併發 acquire（V11）**
- 給定：`status=NONE`；Desktop、Discord、Website 三個 endpoint 同時送 `acquire`；重複 100 回合（每回合先 `revoke`，並由舊端補送停止證據 `confirm_quiesced` 清除標記回乾淨的 NONE，P-16）。
- 當：三個請求以同步屏障（barrier，三個 worker 於屏障後同時送出）同時到達 Presence Manager。
- 則：每回合恰有 1 個 `GRANTED`，其餘為 `BUSY` 或 `VERSION_CONFLICT`；`presence_current` 只有 1 筆；`generation` 每回合恰 +1；journal 每回合只有 1 筆 `presence.granted`。

**VC-4-2 舊 generation 輸出被拒（V12）**
- 給定：Desktop 持 `generation=41` 的 Lease 並有 3 段待播輸出；Presence 被撤銷後 Discord 取得 `generation=42`。
- 當：Desktop 重連並嘗試送出舊輸出與以舊 `lease_id` 續約。
- 則：兩者皆回 `STALE`／`REJECT_STALE`；Desktop 不得復活播放；journal 有被拒紀錄；Desktop 須取得全量快照後以新 acquire 重新請求。

**VC-4-3 移轉中注入崩潰（V14）**
- 給定：Desktop → Discord 移轉。
- 當：分別在 (a) `phase=QUIESCING`、(b) 核發新 Lease 後事件 ACK 前、(c) `phase=ACTIVATING` 三處強制殺死 Core 並重啟。
- 則：重啟後新 `core_epoch`，輸出全關；DB 的 `phase` 決定恢復路徑。(a) 無停止證據 → `status=RECOVERING`；(b) 重送 grant 冪等，`generation` 僅 +1，不核發第二個 Lease；(c) 判準：重啟後每次探測，`status=ACTIVE` 的 endpoint 數 ≤ 1，且最終 Discord 經查核確認後 `status=ACTIVE`（phase=DONE），或查核不明則 `RECOVERING`→`NONE`；不得出現舊 `generation` 被重新核發。三種情況皆滿足「兩端同時有效 owner = 0」。

**VC-4-4 嚴格模式網路隔離（V15）**
- 給定：以調校起點值 Lease TTL=30 秒、心跳=5 秒；Desktop 與 Host 網路隔離並持續 35 秒，其播放器仍有 buffer；Lease 已到期。
- 當：Discord 請求 acquire。
- 則：回 `NOT_QUIESCED`；進 `RECOVERING`；新端保持不可啟用；Desktop 本地 watchdog 於失聯後 TTL（30 秒）內停止 Sink 並隱藏角色（量測超過視為缺陷）；僅在取得停止證據或人為確認後才進 NONE。

**VC-4-5 滿載音訊佇列時撤銷（V06、V07）**
- 給定：Desktop 音訊 queue 已滿，writer 阻塞。
- 當：Core 發出 `control.revoke`。
- 則：控制訊號不經音訊通道且不被擋住；Sink 立即停止、writer 解鎖；之後晚到的舊 generation 音訊被端點二次檢查拒絕。

**VC-4-6 revoke 後他端放行條件（V15、V17，A20）**
- 給定：Desktop 持有效 Lease，Core `revoke`（`generation` 不變，`unquiesced_endpoint_id=Desktop`）。
- 當：Discord 立即 `acquire`；其後 (a) Desktop 送合格停止證據，或 (b) 不送證據但被撤銷 Lease 的 `expires_at` 已過；另一組對照為 Lease 過期（非 revoke）路徑。
- 則：立即 `acquire` 回 `NOT_QUIESCED` 且 `status` 維持 `NONE`；(a)(b) 之後 Discord `GRANTED`；Desktop 在 revoke 後可立即重新 `acquire`；Lease 過期路徑即使 `expires_at` 已過仍須證據或 `resolve_recovery`。三端併發時，證據到達後恰一個 `GRANTED`。

**VC-4-7 新行程自證（V15、D44）**
- 給定：端點 `ep_cli` 的舊行程被殺，Core 標記 `unquiesced_endpoint_id=ep_cli`；同 id 的新行程啟動。
- 當：新行程 `acquire` 得 `NOT_QUIESCED`，送 `fresh_process` 證據。
- 則：舊 Lease 的 `expires_at` + 30 秒未過 → `EVIDENCE_REJECTED`，端點有上限地等待（見 P-18）；已過 → `ACCEPTED`，重新 `acquire` `GRANTED`。證據缺 `playing=False` 或 `buffer_flushed=True`、或 `kind` 不在白名單 → `EVIDENCE_REJECTED`。已持有過 Lease 或輸出過的行程不自證。他端不因 `ep_cli` 的自證而被放行。

---

## 5. 契約 CC-5：Output／delivery

依賴 ADR：ADR-003（Echo Stream 保持語音管線責任）、ADR-005、ADR-013（交付記憶標證據）。對應提案 §6.4–§6.5、§11.3、§17.1。
CC-5 以 CC-4 的授權六元組為前提，定義 *輸出意圖* 與 *交付結果* 兩個分離的紀錄。

### 5.1 OutputIntent 欄位

| 名稱 | 型別 | 必填 | 寫入者 | 說明 |
|---|---|---|---|---|
| `output_id` | str | Y | Core | |
| `kind` | enum{text, audio_stream, typing, avatar_enter_leave, animation, desktop_ui_action, system_notice} | Y | Core | 草案補充列舉（D29）；對應提案 §11.3「文字、TTS、typing indicator、角色進場、動畫及前景工具入口」 |
| `origin` | enum{persona, system} | Y | Core | `system` = 中性服務回條或告警，**不得**以她的口吻（提案 §10.3、§17.1） |
| 授權六元組 `(persona_id, endpoint_id, lease_id, fencing_epoch, core_epoch, presence_generation)` | | Y | Core（取自 CC-4 當前授權） | |
| `activity_id`、`turn_id`、`turn_generation`、`attempt_id` | | C | Core | 互動輸出必填；主動通知用 `activity_id` |
| `sequence` | int | Y | Core | 同 `output_id` 序列內單調 |
| `audience_scope` | ScopeRef | Y | Core | |
| `content_ref` | str | Y | Core | 文字或 artifact 引用；音訊以串流通道傳，這裡存參照與格式 `{sample_rate, channels, codec}` |
| `valid_until` | ts | Y | Core | 短期限播放許可；須 tz-aware（O-15） |
| `generated_text_ref` | str? | C | Core | 完整生成稿（內部紀錄） |

### 5.2 DeliveryRecord 欄位

| 名稱 | 型別 | 必填 | 寫入者 | 說明 |
|---|---|---|---|---|
| `delivery_id` | str | Y | Delivery Manager | |
| `output_id` | ref | Y | | |
| `sequence` | int | Y | Delivery Manager | 對應 OutputIntent 的 `sequence`；`(output_id, sequence, kind)` 對應一筆 intent |
| `kind` | enum | Y | Delivery Manager | 同 OutputIntent `kind` |
| `dispatch_id` | str | Y | Delivery Manager | 外部送出的追蹤 ID；timeout 時不知是否被接受 |
| `status` | enum{generated, synthesized, enqueued, sent, accepted, rendered_played, failed, unknown, cancelled, rejected, suppressed} | Y | Delivery Manager（依證據推進） | 前六為提案 §6.5；`cancelled`、`rejected`（Gate 拒絕）、`suppressed`（重播／shadow 抑制，未真正輸出）為草案補充（D29）。合法路徑見下 |
| `delivery_evidence` | enum{local_playback, remote_render, transport_sent, unknown} | Y | Adapter 回報、Delivery Manager 驗證 | 證據強度（提案 §6.5） |
| `played_range` | obj? | C | Adapter | `{from_sample/chunk, to_sample/chunk}`；Desktop／Website 可回報。未涵蓋完整輸出時見 O-12 |
| `cancel_position` | obj? | C | Core | 被取消時的位置 |
| `spoken_text_ref` / `spoken_text_hash` | str? / str? | C | Delivery Manager | 以 `played_range` 對映的已說出文字，存於可刪 blob（私人原文，同 D33 精神）；供 `ThinkStage.commit(turn_id, spoken_text, generated_text)` 時由 ref 取出 |
| `failure` | obj? | C | | `{code, retryable}` |
| `sent_at` / `reported_at` | ts / ts? | Y / N | | |

### 5.3 ControlSignal 欄位（獨立控制通道）

| 名稱 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `control_id` | str | Y | 冪等鍵 |
| `kind` | enum{stop, abort, revoke} | Y | `stop`＝Sink 立即停止；`abort`＝取消上游；`revoke`＝撤銷授權 |
| `scope` | enum{turn, segment, transfer, process} | Y | |
| `target_fence` | obj | Y | `{turn_generation?, presence_generation?, core_epoch}` |
| `issued_at` | ts | Y | 須 tz-aware（O-15） |

**合法狀態路徑**（只能單向推進，終態不得再改）：

| kind | 合法路徑 | 終態 |
|---|---|---|
| `text`（及其他無合成的 kind） | generated -> enqueued -> sent -> accepted -> rendered_played | rendered_played、failed、cancelled、rejected、suppressed |
| `audio_stream` | generated -> **synthesized** -> enqueued -> sent -> accepted -> rendered_played | 同上 |

- `text` 沒有 `synthesized`；可省略的僅此階段，跳過其他階段不可。
- 任何非終態可轉 `failed`／`unknown`／`cancelled`。`unknown` **不是終態**：僅可被查核解析為 `sent` 或 `failed`。
- `rejected` 只自 `generated` 成立（Output Gate 於接納時拒絕）；`suppressed` 只自**送出前**狀態（`generated`／`synthesized`／`enqueued`）成立，用於重播或 shadow，且不得觸發實際送出。Gate 於 `enqueued` 之後撤銷授權，記為 `cancelled`（不是 `rejected`）。
- `delivery_evidence` 只升不降，順序（草案假設）`unknown < transport_sent < remote_render < local_playback`。同狀態、同證據的重複回報為 no-op，不重寫 `reported_at`。此證據強弱順序仍標為**草案假設**，待確認。
- INV-05 的第五層「使用者已知」**本草案不建模**：交付記錄止於 `rendered_played` 與其證據強度；使用者是否注意到、聽懂無法由 Adapter 證明。是否需要 `user_acknowledged`（例如以使用者後續明確回應推定）見 D39。

`AudioSink.stop()` 同步、立即且冪等；正常完成才 `drain()`，被中斷不可等待舊佇列播完（E02 契約，提案 §6.1）。

**控制通道行為**（落實 E-12 於 P2 補的 control）：
- `control.*` 事件 `scope` 為 public，`idempotency_key=control_id`，**非同步**記入 journal，不阻塞送達；`revoke`／`abort`／`stop` 走優先路徑。
- 端點 handler **不得阻塞**，只設旗標。
- 端點以 generation 判斷晚到的 `revoke`：`signal.generation >= 本地 generation` 才停止。
- 重連須先取權威快照或 `renew`，**不依賴控制通道補送**。
- 端點二次接納（送出／播放前自檢六元組與本地剩餘 TTL）是 Gate→send TOCTOU 的閉合點（P-14）。
- 跨行程 socket 傳輸的連線建立採 hello 雙向證明（抗冒名、不抗 relay），見 P-21；請求重放窗口見 P-22。

### 5.4 權威擁有者

| 資源 | 唯一寫入者 | 其他角色 |
|---|---|---|
| `output_intents` | Core（Orchestrator）；Runtime 只能提出文字增量 | |
| `deliveries` 狀態 | Delivery Manager | Adapter 回報 *證據*，不直接改狀態；Echo Stream Sink 回報 played range |
| 控制訊號 | Core | 任何元件不得自行 `revoke`；Adapter 本地 watchdog 可獨立停止本端 Sink，並事後回報 |
| 使用者可見歷史的交付範圍 | Context Adapter（依 `deliveries` 寫入） | |

### 5.5 不變條件

| 編號 | 條件 | 對應 |
|---|---|---|
| O-1 | 計畫輸出（`output_intents`）與實際交付（`deliveries`）分表；狀態只能依證據單向推進，不得跳級（依 5.2 的 text／audio 各自合法路徑） | INV-05 |
| O-2 | `status ∈ {accepted, rendered_played}` 需 `delivery_evidence ∈ {local_playback, remote_render}`；`transport_sent` 至多到 `sent`；證據 `unknown` 時狀態維持 `unknown`，直到查核解析為 `sent` 或 `failed` | INV-05、V08 |
| O-3 | 取消不 drain；被取消輸出的 `status=cancelled`，歷史只記 `played_range` 內已交付部分；最後播放回報未知時不寫成完整已說出 | INV-05、V08 |
| O-4 | `turn.completed` 分別記錄「生成完成」與「交付結束」兩個時間點；二者不同時成立也合法 | INV-05、INV-06 |
| O-5 | 所有 OutputIntent 必須通過 CC-4 Output Gate；`REJECT_*` 的輸出記為 `rejected`，不重送、不遞補到其他 endpoint | INV-02、INV-03 |
| O-6 | `sent` 後 ACK 遺失或 timeout 標 `unknown`，以 `OutputAdapter.lookup(dispatch_id)` 查核，三態：`found`／`confirmed_not_sent`／`undetermined`（Adapter 另宣告 `lookup_authoritative`）。**只有權威且 `confirmed_not_sent` 才可標 `failed`**（允許以新 dispatch 重試）；其餘維持 `unknown`，避免重複通知。不得無限重送同一提醒，也不得直接開新位置補講 | INV-08、提案 §17.1 |
| O-11 | **單一送出者**：Delivery Manager 以 claim（CAS：`status` 與 `row_version` 條件更新）取得某筆 delivery 的送出權，同一 `(output_id, sequence, kind)` 同時只有一個送出者；claim 失敗者不得送出 | INV-02、INV-05 |
| O-13 | **dispatch 比對已持久化 intent**：`dispatch(intent)` 於 `prepare` 之後載入已持久化的同 `(output_id, sequence)` intent，與呼叫端傳入者**全欄位**比對（`content_ref`、授權六元組、`valid_until` 等）；不一致拋 `IntentMismatchError`，不送出。送出的永遠是已持久化的那份，不信任呼叫端另帶的內容 | INV-03、O-11 |
| O-14 | **Gate 例外 fail-closed**：Output Gate 接納時拋例外，視為拒絕——delivery 目前為 `generated` 記 `rejected`，其後（`synthesized`／`enqueued`）記 `cancelled`，`failure.code=GATE_ERROR`、`retryable=False`，不送出、不卡在 `generated`；例外只記 log，不外洩內容 | INV-02、O-5 |
| O-15 | **時間欄須 tz-aware**：`OutputIntent.valid_until`、`ControlSignal.issued_at`（以及 CC-7 `StateTransitionRequest.expires_at`）建構時即拒絕 naive datetime，避免 naive 與 aware 時鐘比較在 Gate 內才拋 `TypeError` | P-13 |
| O-12 | `played_range` 未涵蓋完整輸出時，即使證據為強證據（`local_playback`／`remote_render`）也**不推到 `rendered_played`**，停在 `accepted`（部分播放以 `played_range` 記錄）。文字類輸出（無合成的 kind）無 `played_range` 時視為完整，可推到 `rendered_played`；`audio_stream` 必須有 `played_range.complete` 才可推到 `rendered_played` | INV-05、V08 |
| O-7 | 工具實際完成以 CC-6 receipt 記帳，與是否已講出無關；不得為補說明而重跑工具 | INV-05、提案 §6.5 |
| O-8 | `origin=system` 的輸出不得使用 Persona 語氣，且仍受 audience 與 Gate 管制 | INV-02、INV-07 |
| O-9 | 歷史中 `generated_text` 與 `spoken_text` 分開保存 | INV-05 |
| O-10 | TTS 失敗時，在當前 Presence 以文字交付並標記語音失敗，不讓別的平台代講 | 提案 §17.1 |

### 5.6 驗證案例

**VC-5-1 插話不 drain、歷史不虛構（V06、V08）**
- 給定：turn 5 的 TTS 句 1–4 已 `enqueued`，句 1–2 `rendered_played`；使用者插話。
- 當：Core 發 `control.stop`（scope=turn）後 `control.abort`。
- 則：Sink 立即停止，不等待舊佇列播完；句 3–4 為 `cancelled`；`spoken_text` 僅含句 1–2；歷史 `delivery_state` 標明取消位置；下一輪仍可說話。

**VC-5-2 Discord 只能證明送出（INV-05）**
- 給定：Discord 語音輸出，Adapter 僅回報封包送出。
- 當：Delivery Manager 更新狀態。
- 則：`status=sent`、`delivery_evidence=transport_sent`；不得寫成 `rendered_played`；歷史標「已送出，聽眾播放未知」。

**VC-5-3 提醒 ACK 遺失（V16、INV-08）**
- 給定：提醒 `dispatch_id=dsp_9` 已送出，平台回應遺失。
- 當：Dispatcher 重試時間到。
- 則：`status=unknown`；先以平台訊息 ID 查核；若查得已送出則更新為 `sent`；若無法查得，進人工／規則處理，**不**無限重送；同一提醒語意只留一筆 `deliveries`。

**VC-5-4 生成完成不等於交付完成（INV-05）**
- 給定：LLM 已完成整段生成，TTS 尚在合成第 3 句。
- 當：觀察 journal。
- 則：已有 `turn.generation_completed`，尚無 `turn.completed`；`deliveries` 中僅部分為 `synthesized`／`enqueued`。

---

## 6. 契約 CC-6：Task／action（Task／Run 與 GS／CS 分離）

依賴 ADR：ADR-007（State Coordinator 集中寫入）、ADR-008（Task／Run 與 GS／CS 分離）、ADR-012（presence_bound 分類）、ADR-007（State Coordinator 集中寫入）。對應提案 §14、§15、§17.2、§18.2。

### 6.1 關聯

```
Persona -> Activities
Activity(kind=interaction_episode)  -> ConversationSegment (對應 CS) -> Turns      （對應 legacy GS）
Activity(kind=task)                 -> Task -> Runs -> Steps -> ActionIntents -> Receipts
```

每個 Task 擁有自己的 `kind=task` Activity（`Task.activity_id` 必填，指向它）。Task 與 GS／CS 的 interaction_episode Activity **無外鍵相依**：只可選擇性經 `origin_ref` 記錄誰建立它。GS 結束（interaction_episode 的 Activity 結束）、CS 結束、平台切換、程序重啟都不終止 Task。

### 6.1a Activity

Activity 是「前景／背景活動的共同索引」（提案 §15.1 `activities`），讓 Presence 的 `owner_activity_id`、事件的 `activity_id`、Task 與互動 episode 有共同掛載點；它**不是**生命週期容器。

| 名稱 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `activity_id` | str | Y | `actv_<ULID>` |
| `persona_id` | str | Y | |
| `kind` | enum{interaction_episode, task, maintenance} | Y | `maintenance` 為記憶整理等內部活動（提案 §10.1）；列舉為草案補充（D37） |
| `presence_bound` | bool | Y | 與 Task 的 `presence_bound` 一致（kind=task 時取自 Task） |
| `status` | enum{ACTIVE, ENDED} | Y | `ENDED` 只表示該 Activity 本身結束 |
| `origin_ref` | obj? | N | 建立來源 |
| `legacy_ref` | obj? | N | `{gs_id?}`（kind=interaction_episode 時） |
| `version` | int | Y | |

- **唯一寫入者**：Activity Registry（Core 內由 Orchestrator 持有的單一寫入元件；元件歸屬見 D37）。其他元件只能請求建立／結束，不得直接寫表。`kind=task` 的 Activity 隨 Task 建立同交易建立，由 Task Manager 經 Registry 請求。**P1 過渡**：Task Manager 在同一交易內直接建立／結束 `kind=task` Activity；P2 抽出 Activity Registry（D37）。
- **結束語意**：Activity 結束（尤其 interaction_episode 的 GS 閒置結束）**不終止**其他 Activity、Task、Presence 或人格；kind=task 的 Activity 在 Task 到達終態後才可 `ENDED`；`ENDED` 的 Activity 不得被拿來推定 Task 狀態。

### 6.2 Task 欄位

| 名稱 | 型別 | 必填 | 寫入者 | 說明 |
|---|---|---|---|---|
| `task_id` | str | Y | Task Manager | |
| `activity_id` | ref<Activity(kind=task)> | Y | Task Manager（經 Activity Registry） | 與 GS／CS 生命週期解耦；見 §6.1a |
| `parent_task_id` | ref<task>? | N | Task Manager | 首版只保留父子關聯，**不做 Task Graph**（依賴／觸發條件，D34 已定案 A8） |
| `goal` | str | Y | 建立者 | 耐久目標與承諾 |
| `owner_actor_id` | str | Y | Gateway 認證 | |
| `scope_ref` | str | Y | GW 重建 | |
| `presence_bound` | bool | Y | Policy（依工具分類） | 預設：視窗控制、輸入、角色播音、桌面互動 = true；純檔案分析、遠端運算、內部記憶整理可 false（需任務授權明確標示，提案 §10.4） |
| `status` | enum{PENDING, RUNNING, WAITING_INPUT, WAITING_APPROVAL, PAUSED, SUCCEEDED, FAILED, CANCELLED, UNKNOWN} | Y | Task Manager | 提案 §14.2 八態加 `WAITING_INPUT`（D30 已定案 A8）。舊藍圖 `READY` 併入 `PENDING`；`BLOCKED` 併入 `PAUSED`＋`pause_reason` |
| `pause_reason` | enum{user, cooldown, resource_missing, presence_left, core_shutdown}? | C | Task Manager | `status=PAUSED` 時必填；`resource_missing` 對應舊 `BLOCKED`（列舉為草案補充） |
| `deadline` | ts? | N | 建立者 | |
| `interrupt_policy` | enum{interruptible, checkpoint_only, non_interruptible} | Y | Policy | D31 已定案 A8：由 Policy 依工具決定，建立者只能收緊（不可放寬） |
| `origin_ref` | obj? | N | Task Manager | `{conversation_id?, segment_id?, legacy_gs_id?, legacy_ws_id?}` |
| `version` | int | Y | Task Manager | 樂觀併發；狀態更新帶 expected version |
| `policy_ref` | str | Y | Policy | |

### 6.3 Run／Step 欄位

| 名稱 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `run_id` | str | Y | |
| `task_id` | ref | Y | |
| `attempt` | int | Y | 第幾次執行 |
| `worker_epoch` | int | Y | 隔離過期 worker；worker 的所有回報（轉移、心跳、step、checkpoint、receipt）都必須帶它（T-9、T-16） |
| `status` | enum{PENDING, RUNNING, WAITING_INPUT, WAITING_APPROVAL, PAUSED, SUCCEEDED, FAILED, CANCELLED, UNKNOWN} | Y | 同 Task；`PAUSED` 帶 `pause_reason` |
| `run_deadline` / `step_timeout` / `approval_expiry` / `worker_heartbeat_at` | ts / duration / ts / ts | Y/N | 逾時語意拆開（提案 §14.3）；legacy WS 10 分鐘不得直接取消長任務。**deadline 的強制執行（逾時轉態）尚未實作，待 Part S Supervisor**（T-20） |
| `checkpoint_ref` | ref? | N | 大型 checkpoint 的外部引用 |
| `checkpoint` | JSONB? | N | runs 內嵌 checkpoint（§6.4 內容）；**值層級白名單**，只允許 ID、狀態、計數、雜湊等，不含私人原文（私人內容走 ref） |
| `persona_version` / `policy_version` | int / int | Y | 啟動時固定 |
| `step_id` | str | Y（Step） | 穩定副作用 ID 的來源 |
| `seq` / `label` / `status`（Step） | int / str / enum | Y | steps 的順序、標籤與狀態（status 列舉同 Run 的終態子集：PENDING／RUNNING／SUCCEEDED／FAILED／CANCELLED，草案補充） |

### 6.4 Checkpoint 內容

| 名稱 | 型別 | 說明 |
|---|---|---|
| `completed_steps` | list<step_id> | 已完成步驟 |
| `input_result_refs` | list<ref> | 輸入／結果引用，不含 Python 物件、Qt handle、socket、GPU tensor |
| `auth_snapshot` | obj | 授權、Persona 版本 |
| `pending_approvals` | list<appr_id> | 待核准動作 |
| `consumed_action_ids` | list<act_id> | 已消耗 action |
| `next_step_semantic` | str | 下一步語意（不是「執行到某行」） |
| `tool_receipts` | list<ref> | 工具 receipt |

### 6.5 ActionIntent／Receipt／Approval 欄位

| 實體 | 欄位 | 型別 | 說明 |
|---|---|---|---|
| ActionIntent | `action_id` | str | **同一次執行嘗試的重送**沿用同一 ID；改參數則新 action；`FAILED` 後的重試也用**新** `action_id` |
| | `retry_of` | ref<action>? | 重試時指向被重試的 `FAILED` action |
| | `run_id`／`step_id` | ref | |
| | `tool`、`executor` | str | 目標 executor |
| | `params_hash` | str | canonical 參數雜湊 |
| | `side_effect_class` | enum{internal_read, reversible_local, external_effect, high_impact} | 提案 §18.2 |
| | `idempotency` | enum{native, via_action_id, none} | |
| | `lookup_authoritative` | bool | executor 宣告其 lookup 結果可作為權威（見 T-3） |
| | `approval_ref` | ref? | 外部副作用／高影響必填 |
| | `presence_bound` | bool | |
| | `status` | enum{INTENDED, APPROVED, EXECUTING, SUCCEEDED, FAILED, UNKNOWN} | `UNKNOWN`＝回覆遺失，待查核。**只由 Task Manager 推進**：依 Policy 決定（APPROVED）、依 executor 回傳的 receipt／查核結果推進（EXECUTING 之後）；executor 只寫 receipt，不直接寫此欄 |
| Receipt | `receipt_id`（`rcpt_<ULID>`）、`action_id`、`worker_epoch?`、`result_ref`、`recorded_at`、`source_verified: bool` | | 由 executor 簽回，Core 記錄。`source_verified=False` 的 receipt 只記錄，**不推進** action 狀態。`worker_epoch`（migration `0008_receipt_worker_epoch`）記取得此 receipt 時呼叫端的 epoch，供稽核判斷是否為舊 epoch 的證據；查核（`lookup`）取得者與既有列為 NULL。內容限制見 T-14 |
| Approval | `approval_id`、`actor_id`（人類，已認證）、`action_hash`、`target`、`scope`、`expiry`、`consumed_at?` | | 單次消耗；`target`＝`action.tool`、`scope`＝`task.scope_ref`；受眾與內容由 `action_hash`（全參數 canonical hash）涵蓋。record 只收已認證 principal |

### 6.6 權威擁有者

| 資源 | 唯一寫入者 | 其他角色 |
|---|---|---|
| `tasks`／`runs`／`steps`／checkpoint | Task Manager | Runtime／SDK 只回報進度與結果；Workflow Engine 的 `approve_step` 只是流程內審核，不是授權 |
| `action_intents`／`status` | Task Manager（全部狀態轉換，依 receipt 推進） | executor 只提供 receipt |
| `receipts` | 目標 executor 產生內容，Task Manager 記錄 | |
| `approvals` | 人類經 Approval 通道；Policy 驗證 | LLM 自述「用戶已核准」無效 |
| 全域軸狀態的轉換提議（Interaction／Behavior 等） | NLP、Proactive、Workflow 皆可提出 `state.transition_requested` | State Coordinator（CC-7）裁決並發 `state.changed`；**不代寫**他人實體表（見 §7.1 單一寫入規則）；Task／Run 的狀態由 Task Manager 自行寫入 |

### 6.7 不變條件

| 編號 | 條件 | 對應 |
|---|---|---|
| T-1 | Task 狀態不隨 GS／CS 結束、平台切換、Presence 離場、Core 重啟自動終止；結束只由 `task.completed`／`failed`／`cancelled` 事件 | INV-06、V19 |
| T-2 | 事件重送、Outbox 重播、worker 重啟不重複同一 `action_id` 的副作用 | V20 |
| T-3 | 工具完成但 receipt 遺失 → 以 `action_id` 向 executor 查核（lookup 三態：`found`／`confirmed_not_executed`／`undetermined`）。只有 executor 宣告 `lookup_authoritative` 且 `idempotency ∈ {native, via_action_id}` 時，`confirmed_not_executed` 才可使 action 退回 `INTENDED`；其餘維持 `UNKNOWN` 交規則／人工，不盲目重試。`source_verified=False` 的 receipt 不推進。Action `UNKNOWN` **不連動** Task／Run 狀態。`resolve_unknown` 先查**已存的 `source_verified=True` receipt**：有則直接依其 `succeeded` 推進為 `SUCCEEDED`／`FAILED`，不再向 executor 查核，無論 executor 之後怎麼說都**不退回 `INTENDED`**（已驗證證據強於 executor 回報）；此時 executor 不存在也不報錯 | V21 |
| T-4 | 外部副作用需有效 `approval_ref`：`target`＝`action.tool`、`scope`＝`task.scope_ref`，受眾與內容由 `action_hash` 涵蓋，期限相符，單次消耗；任一變更須新 action 與新批准。消耗時 `approval.actor_id` 須屬 Policy 指定的 approver（P1 為 `task.owner_actor_id`），record 只收已認證 principal | V30、V31 |
| T-5 | 模型輸出、`approve_step`、Runtime 的 `approval_request` 都不能消耗不存在的 approval | INV-04、V30 |
| T-6 | `presence_bound=true` 任務在離場時於安全 checkpoint 暫停（`PAUSED`）；`false` 且已授權者可續跑；不能一邊在別平台聊天、一邊代她操作電腦 | INV-02、V22 |
| T-7 | 恢復前重新驗證權限、目標裝置、資料 scope、檔案是否變更、批准是否過期；已完成副作用不重播；純模型計算可新 attempt 重做 | 提案 §17.2 |
| T-8 | 對使用者承諾但未交付的報告，恢復的是「通知 intent」，不重跑整個任務 | 提案 §17.2 |
| T-9 | 舊 `worker_epoch` 的回報不推進 Run 或 action 狀態：`transition_run` 與 receipt 入庫路徑**只記錄、不推進**（receipt 仍存證據，`worker_epoch` 欄記下舊值）；`heartbeat`、`checkpoint`、`complete_step`、`begin_step` 等不具證據價值的回報改為拋 `StaleWorkerEpoch`，讓被取代的 worker 得知該停手。另見 T-16 | INV-03 |
| T-12 | 接手（resume）一個 Run 的前提：Supervisor 保證舊 worker 已停止，或舊 `worker_heartbeat_at` 已過期；否則不得接手。**Worker 責任**：長時間 executor 呼叫期間 worker 須自行 heartbeat。**PAUSED 的 Run** 若仍有 `EXECUTING` action，同樣須等心跳過期才可接手。`resume_run` 只自動解除 `pause_reason ∈ allowed_reasons` 的 `PAUSED`（預設 `core_shutdown`、`presence_left`；Run 或 Task 任一方的暫停原因不在內即拋 `ResumeNotAllowed`），使用者暫停、`cooldown`、`resource_missing` 須由呼叫端明確傳入 `allowed_reasons`；`list_resumable_runs` 回傳的 Run 帶 `pause_reason` 供呼叫端決定 | INV-03、提案 §17.2 |
| T-14 | **Receipt 內容限制**：executor 對 receipt 只簽 ID／短碼。`receipt_id` 須為 `rcpt_<ULID>`；`result_ref`、`error_code` 為引用，字元集 `[A-Za-z0-9_.:/~-]`、長度 1～256、以 `fullmatch` 判定，**不含 `@ % = +`**（這些能夾帶 email、百分比編碼、base64 類私人內容；executor 的引用格式須避開，否則被脫敏為 `redacted`）；`meta` 須為 ID／數字／短碼，且不得含 Core 保留鍵 `redacted`、`redactions`。不合格時 Core **不拒收**（副作用已發生，證據不能丟），改為脫敏儲存（`receipt_id` 不合格者另發新的 `rcpt_<ULID>`），並保留原內容的摘要與脫敏原因；格式 `meta.redactions[欄位名] = {sha256, reason}`——鍵名沿用 `sha256`，但**值已改為 HMAC**（`hmac-sha256:<kid>:<hex>`，E-18，D42），沒有有效密鑰時 fail-closed 拒絕（與 payload blob 一致）。值層級白名單的非有限 float（NaN／Infinity）一律視為不合格 | INV-07、E-11 |
| T-15 | executor 查核（lookup）拋出例外視為 `undetermined`，action 維持 `UNKNOWN`（T-3） | V21 |
| T-16 | **worker 身分與強制轉移**：`transition_run` 的 `worker_epoch` 為**必填**（舊 epoch 只記錄，T-9）。Supervisor／Core 要不論 epoch 轉移 Run 時改用 `force_transition_run(run_id, to, reason=…)`：`reason` 必須是短碼（`[A-Za-z0-9_.:-]{1,64}`），仍走轉移表與 `transition_guard`，並記 warning log | INV-03、T-9 |
| T-17 | **Task 與 Run 狀態不脫鉤**：`transition_task` 轉到非終態時，未終態 Run 連動轉到同一狀態與 `pause_reason`（終態由 Task 終態收尾負責）；`start_run` 對 `PAUSED` 的 Task 拒絕（須先 `resume`）；`execute_action` 要求 Task 與 Run 皆為 `RUNNING` 或 `WAITING_APPROVAL`（`WAITING_APPROVAL` 放行以便核准後執行），Task 為 `PAUSED`（含使用者暫停）時拒絕，即使 Run 狀態與之脫鉤 | INV-06、T-6 |
| T-18 | **階段 3 入庫失敗保底**：`execute_action` 階段 3（依 receipt 或遺失結果推進）入庫失敗（脫敏金鑰缺失、DB 錯誤、`receipt_id` 衝突等）時，副作用已發生，Core 於**全新交易**把仍為 `EXECUTING` 的 action 轉 `UNKNOWN` 並落地，再上拋原例外；log 只記例外型別與 executor 結果型別，不記訊息全文。保底本身也失敗時只記 log 並照樣上拋 | V21、T-3 |
| T-19 | **action 與 step 身分完整性**：`create_action` 對既有 `action_id` 重呼叫，僅在 `run_id`、`tool`、`side_effect_class` 與 `params_hash` 全相同時冪等回傳既有列；`run`／`tool`／`side_effect_class` 不同拋 `PolicyViolation`，參數不同拋 `ParamsHashMismatch`（併發輸掉競爭的路徑同樣比對）。`begin_step` 對終態 Run 拒絕（`IllegalTransition`） | T-2、V20 |
| T-20 | **待辦（移出 T-24，歸 Part S，尚未實作）**：(a) `side_effect_class`、`idempotency` 目前由呼叫端宣告，未與 Tool 註冊表核對（S2）；`create_action` 只比對同 `action_id` 的前後一致，不驗證宣告值是否真實；(b) T-11 的 `core_shutdown` 暫停程序與 `run_deadline`／`step_timeout`／`approval_expiry` 的強制執行，待 Supervisor | INV-02、V29 |
| T-13 | Task／Run 轉 `SUCCEEDED` 前不得有 `UNKNOWN`／`EXECUTING` 的 action；Task 轉 `SUCCEEDED` 時檢查**該 Task 的所有 Run**（含先前 `FAILED`／`CANCELLED` 者），不只當前 Run。Task 轉終態時同交易終止其未終態 Run；Run 以 `FAILED`／`CANCELLED` 結束而仍遺留 `EXECUTING`／`UNKNOWN` action 時，`run.ended` payload 帶 `reason_code=unresolved_actions`（另帶 `count`、首個 `action_id`）並記 warning，讓待查核項可被查到。Task／Run 的 `UNKNOWN` 狀態 P1 保留未用 | INV-06 |
| T-10 | 全域停止可阻止新工具與主動行動，其控制路徑不依賴 LLM。**階段 1.5 重檢**：`execute_action` 在階段 1（標 `EXECUTING`）提交後、呼叫 executor 前，以新短交易再呼叫一次 `presence_check`（presence_bound 任務）與 `action_guard`；被擋時 executor **確定尚未被呼叫**，action 由 `EXECUTING` 轉 `FAILED`（不是 `UNKNOWN`），Core 自行簽發一筆 `source_verified=True` 的 receipt（`error_code` 為 `stopped_before_call` 或 `presence_lost_before_call`，`meta.origin=core_pre_call`），並上拋 `ActionBlocked`／`PresenceRequired`。重檢交易開頭先驗 epoch 與狀態（不論是否注入 guard 都執行）：`worker_epoch` 已變或 action 不在 `EXECUTING`（Run 已被接手、action 已轉 `UNKNOWN` 等）時拋 `StaleWorkerEpoch`——不呼叫 executor、不寫 receipt、不改狀態，之後交 `resolve_unknown` 查核。`PresenceRequired.retryable` 語意：階段 1 被擋為 `True`（action 維持 `INTENDED`、approval 未消耗，Presence 恢復後可重試）；階段 1.5 被擋為 `False`（action 已是終態 `FAILED`，須建新 action 與新核准）。此窗口縮到極短但**無法完全消除**：重檢通過到 executor 實際收到呼叫之間仍有殘餘極短窗口（已知限制） | 提案 §18.3、V31 |
| T-11 | `core.stopping`（核心關機）不終止 Task：Run 於安全 checkpoint 暫停（`pause_reason=core_shutdown`），不寫 `task.cancelled`／`task.failed`；進行中且結果未知的 action 標 `UNKNOWN` 待重啟查核；重啟後依 T-7 恢復。**待辦（Part S Supervisor，T-20）**：關機時主動暫停 Run 的程序尚未實作；目前只有重啟後 `resume_run` 把 `EXECUTING` action 轉 `UNKNOWN` 的恢復側 | INV-06 |

### 6.8 驗證案例

**VC-6-1 Task 跨 GS 結束與重啟（V19）**
- 給定：使用者在 GS-1 內建立 Task（`origin_ref.legacy_gs_id=gs_…`），Run 完成 3 個 step，已有 1 個外部副作用且 receipt 已記錄。
- 當：GS-1 閒置結束（`conversation.episode_ended`），隨後 Core 重啟。
- 則：Task 仍為 `RUNNING`／`PAUSED`；重啟後新 `worker_epoch` 的 Run 自 checkpoint 繼續；已完成的副作用 action_id 不重做；完成時通知 intent 送出一次。

**VC-6-2 回覆遺失（V21）**
- 給定：`act_7`（external_effect）執行成功但 executor 回覆遺失。
- 當：Run 逾時、Core 查核。
- 則：向 executor 以 `act_7` 查詢（查核拋出例外視為 `undetermined`），依三態處理：`found`（有可信 receipt）→ `SUCCEEDED`；`confirmed_not_executed` 且 executor 宣告 `lookup_authoritative`、`idempotency ∈ {native, via_action_id}` → 退回 `INTENDED`；其餘（含 `undetermined`、`source_verified=False`）維持 `UNKNOWN` 交規則／人工。絕不以新 action 重送；`UNKNOWN` 不改變 Task／Run 狀態。

**VC-6-3 批准不可重用（V31）**
- 給定：`appr_1` 綁 `params_hash=H1`、`target=A`，已消耗一次。
- 當：Runtime 修改參數為 H2 或再次引用 `appr_1`。
- 則：兩者皆被 Policy 拒絕；需新 `action_id` 與新批准。

**VC-6-4 LLM 自稱已核准（V30）**
- 給定：外部副作用動作等待核准；LLM 輸出「使用者已同意」且 Workflow 觸發 `approve_step`。
- 當：Tool Gateway 檢查。
- 則：無對應 approval row，動作維持 `INTENDED`；Task 進 `WAITING_APPROVAL`。

**VC-6-5 離場暫停（V22）**
- 給定：presence_bound 桌面 Task 進行中；另一個 `presence_bound=false` 分析 Task 進行中並已有授權。
- 當：Presence 由 Desktop 移到 Discord。
- 則：桌面 Task 於安全 checkpoint `PAUSED`；不可中斷的進行中外部動作先完成／查核；分析 Task 繼續。

**VC-6-6 呼叫前被擋（V31、T-10）**
- 給定：`act_8` 已過階段 1（標 `EXECUTING`）；在呼叫 executor 前全域停止成立（或 Presence 失效且任務 `presence_bound`）。
- 當：階段 1.5 重檢。
- 則：executor 零次被呼叫；`act_8` 轉 `FAILED`，Core 自簽 receipt（`stopped_before_call`／`presence_lost_before_call`）；非 `UNKNOWN`，無需查核。重檢通過後到 executor 收到呼叫之間的極短窗口仍屬已知限制。

---

## 6A. 契約 CC-7：State Coordinator

D35 已定案（A8）：新增輕量第七契約。編號為 6A 以保持既有章節交叉參照（§7–§11）不變。
依賴 ADR：ADR-007。對應提案 §14.2、§14.3。僅定義軸、轉換請求、合法性裁決與寫入規則，**不承接**他人實體表的寫入。

### 6A.1 狀態軸與投影

| 軸 | 狀態 | 實體寫入者 | State Coordinator 的角色 |
|---|---|---|---|
| Runtime | STARTING／READY／DEGRADED／STOPPING | Core lifecycle | 投影 |
| Presence | NONE／ACTIVE／RECOVERING（階段見 CC-4） | Presence Manager | 投影（只依 `presence.*` 事件） |
| Interaction | IDLE／LISTENING／THINKING／SPEAKING／WAITING_USER | **State Coordinator** | 唯一寫入者（無其他 Manager 擁有此軸） |
| Task／Run | 見 CC-6 | Task Manager | 投影 |
| Behavior | CHAT／WORK／MISCHIEF／SLEEP 等 | Core Policy 決定 | 裁決合法性後投影 |
| Health | 各服務 ready／degraded／unavailable | Health Registry | 投影 |

### 6A.2 StateTransitionRequest 與 StateDecision 欄位

| 名稱 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `request_id` | str | Y | `req_<ULID>`；冪等鍵 |
| `axis` | enum{Interaction, Behavior} | Y | 只有這兩軸接受轉換請求；其他軸的轉換由實體 Manager 自行發生 |
| `proposer` | str | Y | 已認證的提議者（NLP、Proactive、Workflow 等），Gateway 重建 |
| `expected_version` | int | Y | 該軸投影的版本，樂觀併發 |
| `from_state` / `to_state` | str / str | Y | |
| `reason` | obj | Y | 理由與證據引用 |
| `scope_ref` | str | Y | |
| `expires_at` | ts? | N | 過期請求直接 `REJECTED_EXPIRED`；須 tz-aware（O-15），naive 者建構時拒絕 |
| `decision` | enum{ACCEPTED, REJECTED_ILLEGAL, REJECTED_VERSION, REJECTED_UNAUTHORIZED, REJECTED_EXPIRED, DEFERRED} | Y（回覆） | 列舉為草案補充（D40） |
| `new_version` | int? | C | `ACCEPTED` 時 |

`state.changed` payload：`{axis, from, to, version, cause_event_id}`。合法轉換表以 `policy_version` 版本化。

### 6A.3 權威擁有者

| 資源 | 唯一寫入者 | 其他角色 |
|---|---|---|
| 各軸投影表（`state_projection`） | State Coordinator | 任何元件唯讀 |
| Interaction 軸 | State Coordinator | 提議者只能發請求 |
| 他人實體表（presence／tasks／deliveries 等） | 各 Manager | State Coordinator 不寫 |

### 6A.4 不變條件

| 編號 | 條件 | 對應 |
|---|---|---|
| S-1 | NLP、Proactive、Workflow、Agent 只能提出請求，不得直接寫任何軸（取代 legacy NLP 直寫 StateQueue／state_manager） | INV-04 |
| S-2 | Presence 軸不接受轉換請求；只能由 CC-4 事件驅動投影，避免繞過 Lease | INV-01、INV-04 |
| S-3 | 請求帶 `expected_version`；版本不符 `REJECTED_VERSION`，不得靜默覆寫 | 提案 §15.2 |
| S-4 | 單一服務錯誤（如 TTS）投影到 Health 軸，不得把所有活動塞入全域 `ERROR` | 提案 §14.2 |
| S-5 | `MISCHIEF` 等 Behavior 值不自動擴權（移動視窗、發訊息、處理檔案仍受 Policy／Presence 閘門） | INV-02 |
| S-6 | 投影可由 journal 事件重建；重播不產生外部輸出 | E-7 |

### 6A.5 驗證案例

**VC-7A-1 過期版本被拒（V03 方向）**
- 給定：Interaction 軸版本為 12；NLP 與 Proactive 同時以 `expected_version=12` 提出不同的 `to_state`。
- 當：State Coordinator 依序裁決。
- 則：先到者 `ACCEPTED`（版本 13），後到者 `REJECTED_VERSION`；只有一筆 `state.changed`；後者需重新讀取後重提。

**VC-7A-2 不接受 Presence 請求**
- 給定：Workflow 提出 `axis=Presence, to_state=ACTIVE` 的請求。
- 當：Gateway／State Coordinator 接收。
- 則：被拒（軸不在可請求集合），Presence 投影與 `presence_current` 皆不變；journal 記錄被拒請求。

**VC-7A-3 TTS 錯誤不污染全域狀態**
- 給定：TTS 服務失敗。
- 當：Health Registry 發 `core.component_health_changed`。
- 則：Health 軸該服務為 `unavailable`；Interaction 軸不被寫成 ERROR；CC-5 O-10 的文字降級仍可交付。

---

## 7. 契約之間的交會點（防雙重編排）

### 7.1 單一寫入規則

- **實體表寫入歸各自 Manager**：`presence_*` 歸 Presence Manager；`tasks／runs／steps／action_intents／receipts` 歸 Task Manager；`activities` 歸 Activity Registry（P1 過渡期由 Task Manager 在同一交易代寫 `kind=task`，P2 抽出，D37）；`deliveries` 歸 Delivery Manager；`events／outbox` 歸 Journal／Dispatcher；`state_projection` 與 Interaction 軸歸 State Coordinator（CC-7）。同一欄位不得有兩個寫入者。
- **State Coordinator（CC-7）** 只做各軸投影與轉換請求的合法性裁決，不代寫他人實體表；裁決後由對應 Manager 在自己的交易內寫入。
- 例：`ActionIntent.status` 只由 Task Manager 依 executor receipt 推進；Task `status` 只由 Task Manager 寫；`presence_current.status` 只由 Presence Manager 寫。

### 7.2 交會規則

| 交會 | 規則 | 驗證 |
|---|---|---|
| Echo Stream ↔ Core | Core 決定這次 turn 的工作並綁定 `turn_id`；PipelineRunner 只負責管線內資料流；舊 Coordinator 不得因 `PROCESSING_LAYER_COMPLETE` 再發起第二次 TTS | V09 |
| Runtime ↔ Output | Runtime 只出文字增量；轉為 OutputIntent 的唯一路徑是 Core | VC-3-1 |
| Task ↔ Presence | `presence_bound` 查 CC-4 當前 Lease；離場事件是 CC-4 發、CC-6 消費 | VC-6-5 |
| Context ↔ Delivery | 歷史的 `delivery_state` 取自 CC-5 | VC-5-1、VC-2-1 |

**VC-7-1 單輸入單輸出（V09）**
- 給定：一次文字輸入，相容層仍發出舊 `PROCESSING_LAYER_COMPLETE` 投影。
- 當：Core 處理並完成播放。
- 則：LLM 呼叫恰 1 次；TTS 合成恰 1 次；只有 1 條有效 output／action dispatch 路徑；舊 layer 事件僅為觀測，不觸發任何輸出。

---

## 8. 專節一：legacy 相容映射

依據現況表 §4（SystemEvent 44 項分類、sessions 依賴清單）與 DECISIONS A1／A4／A8。映射僅為 *語意* 對應，新 Core 不承襲舊 enum 與舊 payload 結構。

兩條通則：(1) 相容層**只觀測 legacy 事件、不代發新 Core 的領域事件**；`task.*`、`run.*`、`presence.*` 一律由對應 Manager 依自身狀態發出，legacy 的 `BACKGROUND_WORKFLOW_*`、`WORKFLOW_*`、`SESSION_ENDED(workflow)` 不會觸發任何 `task.*`。(2) 多個 legacy 事件對映同一新類型時，相容層以 `INTERACTION_STARTED`／`OUTPUT_LAYER_COMPLETE` 為準，`CYCLE_STARTED`／`CYCLE_COMPLETED` 只填 `legacy_ref.cycle_index`、不另發事件。

### 8.1 SystemEvent → UEPEvent 類型（值得保留者）

| legacy SystemEvent | 新 event_type | 備註 |
|---|---|---|
| `INTERACTION_STARTED` | `turn.started` | `turn_id` 由 Core 綁定；不再由模組各自決定 |
| `INPUT_LAYER_COMPLETE` | `input.utterance_final`／`input.text_final` | 語意改為「輸入已接受」；文字與內部事件不得偽造成 STT 完成（提案 §6.2） |
| `PROCESSING_LAYER_COMPLETE` | `turn.generation_completed` | 生成完成，**不是**可播放 |
| `OUTPUT_LAYER_COMPLETE` | `delivery.reported`＋`turn.completed` | 拆為交付結果與回合結束；legacy 在 TTS 合成呼叫返回即發布，不代表播完（現況表 V04），新語意必須帶 `delivery_evidence` |
| `CYCLE_STARTED`／`CYCLE_COMPLETED` | （不另發；與 `turn.started`／`turn.completed` 重複對映，見通則 2） | `cycle_index` 降為 `legacy_ref.cycle_index`；`CYCLE_STARTED` 在 legacy 無發布者 |
| `STATE_CHANGED`／`STATE_ADVANCED` | `state.changed` | 由 State Coordinator 發；payload 帶軸（Runtime／Presence／Interaction／Task／Behavior／Health）。`STATE_ADVANCED`（佇列推進）僅為 `reason`，不獨立成型別 |
| `SESSION_STARTED`（legacy 無發布者） | `conversation.episode_started`／`conversation.segment_started` | GS／CS 各一 |
| `SESSION_ENDED`（`session_type=chatting`） | `conversation.segment_ended` | |
| `SESSION_ENDED`（`session_type=workflow`） | 僅對映 `run.ended`（`reason` 帶 WS 結束原因） | `task.*` 由 Task Manager 依原因決定（WS 結束 ≠ Task 必然結束，見 §8.3）；相容層**不得**代發 `task.completed`／`failed`／`cancelled`（尤其不得代發 `task.cancelled`） |
| （legacy GS 結束無事件） | `conversation.episode_ended` | **新增**：補上 legacy 缺的 `GS_ENDED` |
| `MODULE_INITIALIZED`／`READY`／`ERROR`／`BUSY` | `core.component_health_changed` | Health 軸；legacy 皆無非測試發布者 |
| `SLEEP_ENTERED`／`SLEEP_EXITED`／`WAKE_READY` | `state.changed`（axis=Behavior，value=SLEEP／CHAT） | SLEEP 僅為行為模式，不關閉觀察者（提案 §14.3） |
| `ON_CALL_TRIGGERED` | `presence.arrival_requested` | ON_CALL 不再能直接繞過 Presence 啟動角色（提案 §20） |
| `ON_CALL_ENDED` | `presence.departure_requested` | D23 已定案保留 |
| `BACKGROUND_WORKFLOW_SUBMITTED` | `task.created`（語意對應） | legacy 事件不觸發；由 Task Manager 建立 Task 時發 |
| `BACKGROUND_WORKFLOW_STARTED` | `run.started`（語意對應） | 同上 |
| `BACKGROUND_WORKFLOW_COMPLETED`／`FAILED`／`CANCELLED` | `task.completed`／`task.failed`／`task.cancelled`（語意對應） | 同上；legacy 事件僅供觀測比對 |
| `WORKFLOW_REQUIRES_INPUT` | `task.input_requested` | Task／Run 進 `WAITING_INPUT`（D30） |
| `WORKFLOW_INPUT_COMPLETED`／`FILE_INPUT_PROVIDED` | `input.attachment_provided`／`task.input_provided` | |
| `WORKFLOW_STEP_COMPLETED` | `step.completed` | |
| `WORKFLOW_FAILED` | `run.ended`（`result=failed`，由 Task Manager 發） | |
| `REMINDER_TRIGGERED`／`CALENDAR_EVENT_STARTING`／`TODO_OVERDUE`／`TODO_UPCOMING` | `proactive.trigger_observed`（`trigger_kind` 區分） | 進 Proactive 觀察階段，不直接發話（提案 §9.1） |
| `SYSTEM_STARTUP_REPORT` | `core.started`／`core.ready` | |
| `MEMORY_CREATED` | `memory.record_created` | 驅動向量索引重建（提案 §15.3） |
| `SNAPSHOT_CONSOLIDATED` | `memory.consolidated` | |

### 8.2 明確捨棄（不進新 Core 事件集）

| 項目 | 原因 |
|---|---|
| `SystemEvent` 封閉 enum 與 `Event.data: dict` 無 schema | 以註冊表＋UEPEvent 封裝取代（現況表 §4.1） |
| `Event.event_id = evt_{ms}` | 同毫秒撞號 |
| `WORKFLOW_STEP_APPROVED`、`GS_ADVANCED` | 前者語意是流程內審核，不得被誤當授權（V30）；後者低價值。D23 已定案：兩者皆**捨棄**，不保留相容事件（`step.review_passed`、`conversation.episode_advanced` 不存在） |
| `FOLDER_CHANGE_DETECTED`、`MEDIA_CONTROL_EXECUTED` | legacy 無引用／純 SYS 域事件；改為 Adapter 或 executor 自有，必要時以 `action.executed` receipt 表達 |
| `LLM_RESPONSE_GENERATED`、`LLM_LEARNING_DATA_RETURNED` | Runtime 內部細節；對外僅 `turn.generation_completed` 與 telemetry |
| `MEMORY_RETRIEVED`、`SNAPSHOT_CREATED` | 無引用；`snapshot_manager.py:1107` 以 `MEMORY_CREATED` 代替 `SNAPSHOT_CREATED`；改為 telemetry |
| `layer` 去重鍵 `session_id:cycle_index:layer` | 不能替代副作用去重；以 event／turn／action ID 取代（提案 §4.1） |
| `Module Coordinator` 的 `unknown:-1` 預設去重鍵 | 缺關聯鍵時應拒絕而非吃掉不同事件 |
| `router.py`、`reload_coordinator.py`、`registry.get_module` 無開關載入 | 現況表 §4.3「不建議沿用」 |
| `WorkingContext` 全域可變共享字典（`global_context_data['current_cycle_index']` 等） | 改為 CC-2 型別化快照 |
| `core_framework → llm_module._pending_workflow_events` 私有屬性存取 | 跨模組私有存取，新 Core 禁止 |
| `ConditionalStep` 順序執行無等待 | 由 CC-6 的 Run／Step 與 checkpoint 取代（CLAUDE.md 技術債） |

### 8.3 GS／CS／WS ID 與生命週期 → 新 Session／Task／Run

legacy ID 格式（讀自 `core/sessions`）：`gs_{unix秒}_{8hex}`、`cs_{8hex}`、`ws_{8hex}`、`turn_{n}`、`step_{n}`。A4：沿用 **語意與 ID**。

| legacy | 新 Core 對應 | ID 規則 | 生命週期語意 |
|---|---|---|---|
| GS（`gs_…`） | Activity（kind=interaction_episode）的相容外殼（§6.1a）；`conversation_id` 可不同 | `legacy_ref.gs_id` 保留原格式，新 Core 內以 `activity_id`（`actv_<ULID>`）為權威主鍵（D23 已定案：legacy ID 只存 `legacy_ref`）。legacy 備援格式 `gs_{int(time)}`（`session_manager.py:736`，無 8hex 後綴，同秒可能重複）也須可解析，但不得當唯一鍵 | GS 僅為一段互動 episode；閒置可結束；**結束不結束人格、Presence 或獨立 Task**。取代 legacy 由 `check_gs_end_conditions` 輪詢＋`_pending_gs` 的三處分散判定，改為 Core 單點判定，並發出 `conversation.episode_ended` |
| GS 類型 `GSType`（voice／text／system_event／scheduled／continuation） | `episode_started` payload 的 `trigger_kind` | | `system_event`／`scheduled` 事件不再偽裝成輸入 |
| CS（`cs_…`） | ConversationSegment，帶 `audience_scope`（CC-2） | 原生 `segment_id` 為 `seg_<ULID>`；`cs_{8hex}` 存 `legacy_ref.cs_id`。legacy `turn_{n}` 只在該 CS 內唯一，存 `legacy_ref.turn_n` 或複合鍵 `(segment_id, turn_n)`；`memory_token` 格式 `cs_{session_id}_{int(created_at.timestamp())}`（`chatting_session.py:139`）存 `legacy_ref.memory_token`，不當 ID | 進 WORK 可 `PAUSED` 保留脈絡，不強制清空；`pending_end` 雙條件概念保留為「結束請求＋Core 確認」 |
| WS（`ws_…`） | Task 的相容觀測視圖（`ws_id` ↔ `task_id` 對應表）；每次執行為 Run。WS 結束只對映 `run.ended`，`task.*` 由 Task Manager 決定 | `legacy_ref.ws_id` 對 `task_id` 一對一；`step_{n}` 只在該 WS 內唯一，存 `legacy_ref.step_n` 或複合鍵 `(task_id, step_n)`，**不**直接當 `step_id`（穩定副作用 ID 須全域唯一）；`ws_{session_id}_{ts}` 工作流標識（`workflow_session.py:178`）存 `legacy_ref.memory_token`，不當 ID | WS 可存在於 GS 之下，但 Task 可超越 GS、平台與程序（提案 §14.1）。legacy WS 10 分鐘逾時**不得**取消 Task，只可轉為 `run_deadline`／`approval_expiry`（提案 §14.3） |
| GS 5 分鐘閒置 | `conversation idle timeout` | | 只管對話，不影響 Task／Presence |
| `GSStatus`、`CSStatus`、`WSStatus` | 分別投影到 Interaction 軸、Segment 狀態、Task／Run status | | `WSStatus.EXECUTING/WAITING/PAUSED/…` → `RUNNING/WAITING_INPUT 或 WAITING_APPROVAL/PAUSED` |
| `SessionRecord`（重複定義於 `general_session.py:54`、`session_manager.py:57`） | 不沿用；以 journal 事件重建 | | |
| `SessionRecordType` GS／CS／WS | 保留為 `legacy_ref` 類別標籤 | | |

A4 沿用前提（現況表 §4.2／§4.3）：新 Session 不得對 `working_context_manager`、`controller`、`state_manager` 單例有模組級依賴；生命週期判定不留在 sessions 內，改由 State Coordinator／Core 單點寫入。

### 8.4 Event Bus 程式碼沿用條件（A4，僅列契約層需求）

保留：`subscribe／unsubscribe／publish(sync=)`、單消費者線程分發、handler 例外隔離、`get_stats`。
包裝層需滿足（P1／T-23／T-24 已實作，行為見 E-13）：journal-first（E-1）、訂閱鍵為 `str`（取代封閉 enum，D22 已定案 A8）、事件 ID 改用 CC-1 的 `event_id`、佇列有上限與 `stop()` 排空（逾時後舊線程存活則拒絕重啟）、stats 在鎖內更新、handler 例外只記型別、慢 handler 不阻塞控制通道（控制訊號走獨立通道，不經此 Bus）。

---

## 9. 專節二：改進計畫用語對照（A5）

`docs/改進計畫.md`（Echo Kernel 藍圖）標為「已被取代」。其用語併入提案與本草案如下。

| 舊藍圖概念 | 提案章節 | 新契約位置／說明 |
|---|---|---|
| Echo Kernel（系統主語） | §1、§5（UEP Core Runtime） | 「Core」；唯一權威寫入者。不再命名為 Kernel |
| 雙迴圈：Foreground loop | §6.2、§14.2 Interaction 軸 | 互動 turn 路徑：CC-3 direct_chat、CC-5 輸出；由 State Coordinator 追蹤 |
| 雙迴圈：Background loop | §7、§9（Proactive Engine）、§10.4 | 事件驅動背景活動：CC-6 Task（`presence_bound=false`）＋ `proactive.*`；不是輪詢迴圈 |
| Event Bus 降級為通訊骨幹 | §7.1、§20 | CC-1；A4 沿用程式碼加 journal |
| Module Coordinator 降級為管線協調器 | §6.2「禁止重複編排」、§20 | 第 7 節交會規則；舊 Coordinator 不得二次 dispatch |
| Shared Memory：Story KB／Plan KB／Preferences | §15.1、§15.3–§15.4 | `memory_records`／`index_refs` 與 scope；CC-2 `memory_slices`。KB 分類為 MEM 內容分類，非獨立契約 |
| 三層存放：Raw／Structured／Index | §15.3 | 原始 metadata 與授權為事實來源；FAISS 向量索引是可重建投影 |
| Task Engine／Task Graph | §14.1、§15.1 `tasks`、`runs`、`steps` | CC-6。首版不做 Task Graph，只留 `parent_task_id`（D34 已定案 A8） |
| Task 狀態機（Pending／Ready／Running／WaitingUser／Blocked／Paused／Done／Failed） | §14.2 Task／Run 軸 | CC-6 `status`：`WaitingUser`→`WAITING_INPUT`；`Ready`→`PENDING`；`Blocked`→`PAUSED`＋`pause_reason`（D30 已定案 A8） |
| Skill Registry | §8.4（工具註冊）、§18.2 | Tool Gateway 的工具註冊（能力、executor、scope、副作用分類、冪等、核准、可中斷、是否綁定 Presence）。CC-6 ActionIntent 引用 |
| Safe vs Risky Skills | §18.2 工具與核准分類 | `side_effect_class`：internal_read／reversible_local 約為 Safe；external_effect／high_impact 約為 Risky。細分為四級，不再二分 |
| Subagents（Nightly Curator、Stuck Breaker） | §8.1、ADR-015、§9 | 內部 Runtime 執行者／Proactive 觸發的 Task；SDK Handoff 不改 Persona／Presence（R-3） |
| 離線優先 | §8.3、§16、§24.3 | `data_egress` 與 Model Gateway 路由（資料外傳限制為最先條件）；首版預設僅允許已核准供應商 |
| 離線分級 L0／L1／L2 | §8.3（Provider 能力與路由）、§16.2、ADR-014 | 不作為契約欄位；對應 Provider 能力表與 `data_egress`。L2＝外部 API，一律走資料外傳政策 |
| 備份：每日增量／每週快照／每月歸檔 | §17.3 | 提案改為 DB 一致備份＋還原演練；頻率由 RPO（待使用者決定）定 |
| JSON 範例 `task.create` | §7.2、§15.1 | 以 CC-1 `task.created` 事件＋CC-6 Task 欄位取代 |
| 階段一～四開發計畫 | §21 路線圖 P0–P8 | 以提案路線圖為準 |

---

## 10. 待裁決

沿用 DECISIONS 的編號，自 D22 起。D22、D23、D30、D31、D32、D33、D34、D35 已由艾斯維爾裁決（A8，2026-10-26）並落實於本草案，標「已定案（A8）」；D41、D42（A16）、D43（A20）亦已定案並實作；其餘（D24–D29、D36–D40）仍待決，草案未定案（TRACKING 另記 D24、D29 為 A9、D26–D28 與 D36 為 A11 已定案，以 TRACKING 為準）。

| 編號 | 問題 | 影響契約 |
|---|---|---|
| D22 | **已定案（A8）**：Event Bus 訂閱鍵由封閉 `SystemEvent` enum 改為 `str`，修改沿用的 bus 程式碼 | CC-1、§8.4 |
| D23 | **已定案（A8）**：legacy ID（`gs_／cs_／ws_`）只存 `legacy_ref`；保留 `ON_CALL_ENDED`→`presence.departure_requested`；捨棄 `step.review_passed`、`conversation.episode_advanced` | CC-1、§8 |
| D24 | `ContextSnapshot.purpose` 是否拆為多種快照型別（conversation_turn／task_run／proactive_eval／summarize），或單一型別加欄位 | CC-2 |
| D25 | Runtime `error.code` 列舉是否定案，與 Model Gateway 錯誤碼是否共用 | CC-3 |
| D26 | `fencing_epoch` 的單調來源與保存位置（DB 外，如 Host 本機檔／金鑰服務），使災難還原後不回退；提案 §17.3 只要求「新的 deployment／Core fencing epoch」，未指定來源 | CC-4 |
| D27 | 提案 §12.1 狀態圖含 `CHECKPOINTED`，§14.2 Presence 軸列舉未含；草案將其視為 *transfer phase*，並新增 `DONE`／`ABORTED`；需確認 | CC-4 |
| D28 | Output Gate 拒絕結果碼列舉是否定案（`REJECT_STALE` 等為草案補充），以及 Core 端與端點端是否共用同一份碼表 | CC-4、CC-5 |
| D29 | 輸出 `kind` 與交付 `status` 的補充值（`cancelled`、`rejected`、`suppressed`）是否納入；提案 §6.5 的狀態鏈未含取消／拒絕 | CC-5 |
| D30 | **已定案（A8）**：Task／Run 只新增 `WAITING_INPUT`；舊 `READY` 併入 `PENDING`，`BLOCKED` 併入 `PAUSED`＋`pause_reason` | CC-6、§8、§9 |
| D31 | **已定案（A8）**：`interrupt_policy` 三值 interruptible／checkpoint_only／non_interruptible；Policy 依工具決定，建立者只能收緊 | CC-6、CC-4 |
| D32 | **已定案（A8）**：穩定 ID 格式為 `前綴_ULID`（可按時間排序） | 全部 |
| D33 | **已定案（A8）**：journal 只存元資料＋`payload_ref`；私人原文放可刪 blob，journal 留 `payload_hash` 與墓碑 | CC-1 |
| D34 | **已定案（A8）**：首版不做 Task Graph，Task 只留 `parent_task_id` | CC-6、§9 |
| D35 | **已定案（A8）**：新增輕量第七契約 CC-7 State Coordinator（軸、轉換請求、合法性裁決、寫入規則），見 §6A | CC-7、CC-1、CC-6 |
| D36 | 全域停止／撤銷入口（提案 §18.3）的認證與介面：由誰、經哪個通道觸發，與 `control.revoke` 的關係 | CC-4、CC-5 |
| D37 | Activity Registry 的具體元件歸屬（Orchestrator 內單一寫入元件，或併入 Task Manager／Conversation Manager）；`Activity.kind` 列舉（interaction_episode／task／maintenance）是否定案 | CC-6、CC-1 |
| D38 | scope 的 level 是否需獨立的「角色自我（self）」與「各使用者私人」（提案 §15.4 列四類範圍，草案 §0.6 只有 private／group／public） | §0.6、CC-2 |
| D39 | INV-05「使用者已知」是否建模為交付狀態（如 `user_acknowledged`）及其推定證據；草案目前不建模 | CC-5 |
| D40 | CC-7 `StateDecision.decision` 列舉與 `pause_reason`、Activity 等草案補充列舉是否定案 | CC-7、CC-6 |
| D41 | **已定案（A16）**：journal 讀取的「安全水位」：`journal_seq` 非提交可見順序，replay／tailing 如何推進游標。建議：以 `xmin`（或提交時間）加延遲水位，僅處理低於水位的 `journal_seq` | CC-1 |
| D42 | **已定案（A16）**：`payload_hash` 防字典還原，已由 sha256 改為 per-install 密鑰 HMAC（`hmac-sha256:<kid>:<hex>`，見 E-18；密鑰存放與輪替另定） | CC-1 |
| D43 | **已定案（A20，T-24）**：`revoke` 將舊端記為 `unquiesced_endpoint_id`（`generation` 不 +1）；他端 acquire 須等舊端停止證據或被撤銷 Lease 的 `expires_at` 已過，才放行（維持 `NONE`，不進 `RECOVERING`）；過期、啟用逾時、Core 重啟路徑仍須證據。見 P-16 | CC-4 |

---

## 11. V01–V36 覆蓋表

「覆蓋」指本草案有對應驗證案例（尚未執行，驗收項皆維持未勾選）。

| V | 覆蓋 | 對應案例／說明 |
|---|---|---|
| V01–V04 | 契約外 | 現況盤點，由 `Core現況對照表.md`（T-01）承擔 |
| V05 | 契約外 | 真實語音全程實測，屬整合驗收 |
| V06 | 是 | VC-5-1、VC-4-5 |
| V07 | 是 | VC-3-1、VC-4-5 |
| V08 | 是 | VC-5-1、VC-2-3（歷史標示） |
| V09 | 是 | VC-7-1（§7.2 案例） |
| V10 | 契約外 | 長句、多語、回授等語音品質實測 |
| V11 | 是 | VC-4-1 |
| V12 | 是 | VC-4-2 |
| V13 | **未涵蓋** | Desktop→Discord→Website→Desktop 的人工順序驗收；本草案只有單次移轉案例 |
| V14 | 是 | VC-4-3 |
| V15 | 是 | VC-4-4 |
| V16 | 部分 | VC-4-4、VC-5-3；動畫離場失敗、Discord 加入失敗各自的案例未寫 |
| V17 | **未涵蓋** | 多分頁／多裝置／多頻道粒度；P-1 與 §4.1 只定義粒度 |
| V18 | **未涵蓋** | 缺席不錄音；僅有 P-9 條件，無案例 |
| V19 | 是 | VC-6-1 |
| V20 | 是 | VC-1-1、VC-1-2 |
| V21 | 是 | VC-6-2 |
| V22 | 是 | VC-6-5 |
| V23 | 部分 | VC-2-3（MEM）、R-6（資料出口）；DB／GPU／外部 API 各自失敗的案例未寫 |
| V24 | **未涵蓋** | 備份還原；僅有 P-5 條件與 D26 |
| V25、V26、V27 | **未涵蓋** | Proactive 候選、抑制與延後，不在 CC-1～CC-7 範圍 |
| V28 | 是 | VC-2-1 |
| V29 | **未涵蓋** | 惡意內容要求擴權；僅有 R-1／T-5 條件 |
| V30 | 是 | VC-6-4 |
| V31 | 是 | VC-6-3 |
| V32 | 契約外 | Host 層級實測 |
| V33 | **未涵蓋** | Mac 重開機後只有重新授權的 Presence 到場；僅有 P-4 條件 |
| V34 | 契約外 | 資源有界，屬運維實測 |
| V35 | **未涵蓋** | 使用者可見性（她在哪、為何離開）；UX 驗收 |
| V36 | 契約外 | SDK 引入前後比較，屬選型試驗 |
