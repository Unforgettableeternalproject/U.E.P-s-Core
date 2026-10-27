# Tracking

查「做到哪」。規則見 [README](README.md)；每列第一欄是穩定 ID，檢查器據此確認 V01–V36、D1–D21、A1–A7 沒有漏列。

- V 項原文：提案第 23 章（[提案](../../hidden_docs/架構重構/UEP_新架構研究/UEP_新架構技術設計提案_2026-10-25.md)）。本表「摘要」只是提示。
- 「實作」「測試」填 repo 內路徑（反引號）；未存在的路徑同列須含 `planned`。`—` 表示尚無。
- 狀態詞：`未開始`／`證據已存`（有對應文件，尚待驗收）／`進行中`／`已驗收`。證據類型沿用 T-01：讀碼、執行、實測。
- 「待立卡」：[週期規劃](#3-任務卡)已指定階段與涵蓋的 V 項，但尚未拆成 T-xx。

## 1. 決策（D）

D1–D15 對應提案第 22 章 ADR-001～015，維持 PROPOSED，驗證條件以提案為準。D16 以後為重構期新增決策；D22–D43 來自 [Core契約草案 §10](../架構重構/Core契約草案.md)，草案中 D22、D23、D30–D35 已由 A8 定案，其餘待決。

| ID | 決策摘要 | 狀態 | 最晚定案／負責卡 | 相關 V |
|---|---|---|---|---|
| D1 | 自有 Core 掌握狀態，SDK 僅執行 Agent Loop（ADR-001） | 已定案（A19，暫定） | 週期 2 Part S（待立卡，S1 開工前） | V36 |
| D2 | 首版模組化單程序＋DB，模型服務分離（ADR-002） | PROPOSED | 週期 2 對應卡（待立卡） | V23 |
| D3 | Echo Stream 保持語音管線責任，contracts／core 不 import 平台狀態（ADR-003） | PROPOSED | T-05、週期 2 P3 | V09 |
| D4 | One Identity、One Active Presence，可 NONE（ADR-004） | 已定案（A10） | T-16 | V11、V17 |
| D5 | DB 原子 Lease＋generation／epoch＋Output Gate（ADR-005） | 已定案（A10） | T-16 | V11、V12 |
| D6 | 嚴格移轉需舊端 quiescence，未知則停在恢復（ADR-006） | 已定案（A10） | T-16、T-17 | V13、V14、V15 |
| D7 | State Coordinator 集中寫入，多來源可提議（ADR-007） | PROPOSED | 週期 2（待立卡） | V03 |
| D8 | Task／Run 與 GS／CS 分離（ADR-008） | PROPOSED | 週期 2 P1 實作（待立卡） | V19、V22 |
| D9 | Model Gateway＋專用 STT／TTS Provider（ADR-009） | PROPOSED | 週期 2 P3（待立卡） | V36 |
| D10 | PostgreSQL 作耐久權威，MEM 向量索引先沿用（ADR-010） | PROPOSED | 週期 2 P1 實作（待立卡） | V24 |
| D11 | Proactive 規則先行、模型按需、決策可解釋（ADR-011） | PROPOSED | 週期 4 P7（待立卡） | V25–V27 |
| D12 | 前景桌面工作綁 Presence，明確背景工作可例外（ADR-012） | 已定案（A10） | T-18 | V22 |
| D13 | audience 限制跨平台上下文，交付記憶標證據（ADR-013） | PROPOSED | 週期 3 P5（待立卡） | V28、V08 |
| D14 | Mac mini 作控制 Host，GPU／API 分離（ADR-014） | PROPOSED | 週期 4 P8（待立卡） | V32、V33 |
| D15 | 內部 SDK Handoff 不改 Persona／Presence（ADR-015） | PROPOSED | 週期 4 P6（待立卡） | V36 |
| D16 | 沿用 Event Bus／Session 的範圍：Event Bus 程式碼沿用加 journal 封裝；Session 只沿用語意與 ID | 已定案（A4） | T-08 | V09、V20 |
| D17 | 改進計畫與提案的關係：以提案為主，改進計畫標為已取代 | 已定案（A5） | T-08 | — |
| D18 | 環境目標：Python 3.12＋uv，依賴拆 core／inference／desktop | 已定案（A6） | T-03 | — |
| D19 | legacy 保存形式：原地保留並打 tag，新 Core 開 `uep/` | 已定案（A7） | T-09 | — |
| D20 | STT 預設模型：Breeze 為 Discord 路徑預設（暫定），echo_stt 整體預設待決 | 部分裁決 | T-10（TASKS 尚未展開） | V10 |
| D21 | STT 的 LLM 情境校正放在哪一層：即時對話用原文，校正版背景寫入 | 待決 | T-07 之後、T-08 | V10 |
| D22 | Event Bus 訂閱鍵由封閉 `SystemEvent` enum 改為 `str`，修改沿用的 bus 程式碼（草案 §10；CC-1、§8.4） | 已定案（A8） | — | — |
| D23 | legacy ID 只存 `legacy_ref`；保留 `ON_CALL_ENDED`→`presence.departure_requested`；捨棄 `step.review_passed`、`conversation.episode_advanced`（草案 §10；CC-1、§8） | 已定案（A8） | — | — |
| D24 | `ContextSnapshot.purpose` 拆為多種快照型別或單一型別加欄位（草案 §10；CC-2） | 已定案（A9） | T-14 | — |
| D25 | Runtime `error.code` 列舉是否定案，與 Model Gateway 錯誤碼是否共用（草案 §10；CC-3） | 待決（草案） | 週期 2 對應卡開工前 | — |
| D26 | `fencing_epoch` 的單調來源與保存位置（DB 外），使災難還原後不回退（草案 §10；CC-4） | 已定案（A11） | T-16 | — |
| D27 | `CHECKPOINTED` 視為 transfer phase，並新增 `DONE`／`ABORTED`（草案 §10；CC-4） | 已定案（A11） | T-16 | — |
| D28 | Output Gate 拒絕結果碼列舉是否定案，Core 與端點是否共用碼表（草案 §10；CC-4、CC-5） | 已定案（A11） | T-16 | — |
| D29 | 輸出 `kind` 與交付 `status` 補充值（`cancelled`、`rejected`、`suppressed`）是否納入（草案 §10；CC-5） | 已定案（A9） | T-14 | — |
| D30 | Task／Run 只新增 `WAITING_INPUT`；`READY` 併入 `PENDING`，`BLOCKED` 併入 `PAUSED`＋`pause_reason`（草案 §10；CC-6） | 已定案（A8） | — | — |
| D31 | `interrupt_policy` 三值 interruptible／checkpoint_only／non_interruptible；Policy 依工具決定，建立者只能收緊（草案 §10；CC-6、CC-4） | 已定案（A8） | — | — |
| D32 | 穩定 ID 格式為 `前綴_ULID`（草案 §10；全部） | 已定案（A8） | — | — |
| D33 | journal 只存元資料＋`payload_ref`；私人原文放可刪 blob，journal 留雜湊與墓碑（草案 §10；CC-1） | 已定案（A8） | — | — |
| D34 | 首版不做 Task Graph，Task 只留 `parent_task_id`（草案 §10；CC-6） | 已定案（A8） | — | — |
| D35 | 新增輕量第七契約 CC-7 State Coordinator（軸、轉換請求、合法性裁決、寫入規則）（草案 §10；CC-7、CC-1、CC-6） | 已定案（A8） | — | — |
| D36 | 全域停止／撤銷入口的認證與介面，與 `control.revoke` 的關係（草案 §10；CC-4、CC-5） | 已定案（A11） | T-17 | — |
| D37 | Activity Registry 的元件歸屬；`Activity.kind` 列舉（interaction_episode／task／maintenance）是否定案（草案 §10；CC-6、CC-1） | 待決（草案） | 週期 2 對應卡開工前 | — |
| D38 | scope level 是否需獨立的角色自我（self）與各使用者私人（草案 §10；§0.6、CC-2） | 待決（草案） | 週期 2 對應卡開工前 | — |
| D39 | INV-05「使用者已知」是否建模為交付狀態（如 `user_acknowledged`）及推定證據（草案 §10；CC-5） | 待決（草案） | 週期 2 對應卡開工前 | — |
| D40 | CC-7 `StateDecision.decision` 列舉與 `pause_reason`、Activity 等補充列舉是否定案（草案 §10；CC-7、CC-6） | 待決（草案） | 週期 2 對應卡開工前 | — |
| D41 | journal 安全水位：`journal_seq` 非提交可見順序，replay／tailing 的游標推進；建議以 `xmin` 或提交時間加延遲水位（草案 §10；CC-1） | 已定案（A16） | T-23 | — |
| D42 | `payload_hash` 防字典還原：建議改 per-install 密鑰 HMAC（草案 §10；CC-1） | 已定案（A16） | T-23 | — |
| D43 | 多平台前的 revoke 放行條件：`revoke` 將舊端記為 `unquiesced_endpoint_id`，他端 acquire 須等舊端停止證據或被撤銷 Lease 的 `expires_at` 已過（草案 §10、P-16；CC-4） | 已定案（A20） | T-24 | V17 |
| D44 | 他端 acquire 推進 RECOVERING 後，原端點是否可在停止證據到達前重新 acquire（現況：不可，須證據或 `resolve_recovery`；草案 §4.5 P2 補充僅涵蓋 NONE＋unquiesced；CC-4） | 已定案（A13，暫定） | T-19；第一次完整系統測試時複議 | V15 |
| D45 | 主機節點的連線與身分：連線方向、傳輸、跨網段前提、每裝置金鑰（Part H） | 待決 | 週期 3 Part H（待立卡，H1 開工前） | V17 |
| D46 | 電腦操作的目標裝置解析：前景取 Presence 當前裝置，背景以 `target_device_id` 指定且不隨 Presence 移動（延伸 D12；Part H） | 待決 | 週期 3 Part H（待立卡，H2 開工前） | V17 |
| D47 | Avatar 控制窗口走 CC-5 Output Gate 還是 CC-6 Tool Gateway；採 Output＋Gate，以提案工具補審計（週期 5 P9） | 已定案（A22） | 週期 5 P9（待立卡） | — |
| D48 | Lore Vault（預設 lore space）／Chatroom 接入方式（Part S S0）：Lore Vault 的存在與意義為原生資訊、查詢由 UEP 以工具自決；Chatroom 專屬工具；lore 可寫入但須審核；Chatroom 身分待討論 | 已定案（A18、A22；Chatroom 身分由 D66 定案） | 週期 2 Part S（待立卡，S0） | — |
| D49 | 暫停前狀態保存（Part S；SYS契約草案 §8） | 已定案（A22） | 週期 2 Part S（待立卡），S1 開工前 | V22 |
| D50 | ActionIntent 加 `params_ref`（Part S；SYS契約草案 §8） | 已定案（A22） | 週期 2 Part S（待立卡），S1 開工前 | V21 |
| D51 | `resolve_unknown` 收緊與衝突事件（Part S；SYS契約草案 §8） | 已定案（A22） | 週期 2 Part S（待立卡），S1 開工前 | V21 |
| D52 | UNKNOWN 人工解決 API 與 `retry_of` 擴充（Part S；SYS契約草案 §8） | 已定案（A22） | 週期 2 Part S（待立卡），S1 開工前 | V21 |
| D53 | `params_hash` 細節：`1.0` vs `1`、NFC、大整數（Part S；SYS契約草案 §8） | 已定案（A22） | 週期 2 Part S（待立卡），S2 開工前 | V31 |
| D54 | 註冊表注入 TaskManager（Part S；SYS契約草案 §8） | 已定案（A22） | 週期 2 Part S（待立卡），S2 開工前 | V29、V31 |
| D55 | approval 綁工具 version與 ActionIntent `tool_version`（Part S；SYS契約草案 §8） | 已定案（A22） | 週期 2 Part S（待立卡），S2 開工前 | V31 |
| D56 | 工具結果 audience 過濾與預設 scope（Part S；SYS契約草案 §8） | 已定案（A21） | 週期 2 Part S（待立卡），— | V28 |
| D57 | 常駐說明承載位置（Part S；SYS契約草案 §8） | 已定案（A22） | 週期 2 Part S（待立卡），S0 開工前 | — |
| D58 | Lore 寫入待審機制（Part S；SYS契約草案 §8） | 已定案（A21） | 週期 2 Part S（待立卡），— | — |
| D59 | 工具形式（D48 殘餘）與 MCP 治理（Part S；SYS契約草案 §8） | 已定案（A22） | 週期 2 Part S（待立卡），S0 開工前 | V29 |
| D60 | SDK hosted／built-in 工具禁用與 `capability_set` 對應（Part S；SYS契約草案 §8） | 已定案（A22） | 週期 2 Part S（待立卡），S3 開工前 | V36 |
| D61 | 工具結果回饋 Runtime（Part S；SYS契約草案 §8） | 已定案（A22） | 週期 2 Part S（待立卡），S3 開工前 | V36 |
| D62 | 回合內工具呼叫的 Task 歸屬、生命週期與 action 層強制（Part S；SYS契約草案 §8） | 已定案（A22） | 週期 2 Part S（待立卡），S3 開工前 | V22 |
| D63 | Agent Core 的 CC-7 proposer（Part S；SYS契約草案 §8） | 已定案（A22） | 週期 2 Part S（待立卡），S1 開工前 | — |
| D64 | 非 owner 來源 Task 的 owner／approvers（Part S；SYS契約草案 §8） | 已定案（A22） | 週期 2 Part S（待立卡），S0 開工前 | V30 |
| D65 | 常設授權與 `chatroom.post` 頻率上限（Part S；SYS契約草案 §8） | 已定案（A22） | 週期 2 Part S（待立卡），S0 之後、Part H 前 | V30 |
| D66 | Chatroom 身分與可查核性（Part S；SYS契約草案 §8） | 已定案（A21） | 週期 2 Part S（待立卡），— | V29 |
| D67 | Supervisor 形式、`shutdown` 掛接、單例與逾時（Part S；SYS契約草案 §8） | 已定案（A22） | 週期 2 Part S（待立卡），S1 開工前 | V22 |
| D68 | Avatar 表現指令簡化記帳與 D47 預留位置（Part S；SYS契約草案 §8） | 已定案（A22） | 週期 2 Part S（待立卡），P9 開工前 | — |
| D69 | `ToolExecutor.cancel` 協定（Part S；SYS契約草案 §8） | 已定案（A22） | 週期 2 Part S（待立卡），S3 開工前 | — |

## 2. 已定案事項（A）

| ID | 內容 | 影響的卡 | 對應 D |
|---|---|---|---|
| A1 | Core 從頭重新設計；`core/`、`modules/` 視為 legacy | T-08、T-09 | D16、D19 |
| A2 | 建立 Index 與 Tracking | T-02 | — |
| A3 | 啟用 Lore Vault 任務層管理本次重構 | 全部 | — |
| A4 | Event Bus 程式碼沿用並加 durable journal；Session 只沿用語意與 ID | T-08、T-09 | D16 |
| A5 | 以提案為主，`docs/改進計畫.md` 標為已被取代 | T-08 | D17 |
| A6 | Python 3.12＋uv／pyproject；依賴拆三組 | T-03 | D18 |
| A7 | legacy 原地保留，新 Core 在頂層 `uep/` | T-09 | D19 |
| A8 | 契約草案 8 項採審查建議預設：D22 訂閱鍵改 `str`；D23 legacy ID 只存 `legacy_ref`；D30 只加 `WAITING_INPUT`；D31 `interrupt_policy` 三值；D32 ID 為 `前綴_ULID`；D33 journal 只存元資料＋`payload_ref`；D34 首版不做 Task Graph；D35 新增第七契約 CC-7 | T-08 | D22、D23、D30–D35 |
| A21 | D56 工具結果首版不過濾（技術債，保留過濾掛點）；D58 lore 直寫 vault 加待審標記；D66 Chatroom 以艾斯維爾身分＋主持人模式，僅限自架 Hub 允許清單 | Part S（待立卡） | D56、D58、D66 |
| A22 | Part S 契約草案其餘決策照建議定案；D47 採 Output＋Gate | Part S（待立卡） | D49–D55、D57、D59–D65、D67–D69、D47、D48 |

## 3. 任務卡

狀態以任務層為準（`tasks(action="list")`），此處不重複記錄以免過期。

| 卡 | 標題 | 線／週期 | 任務層 change | 涵蓋 V | 涉及 D／A | 主要產出 |
|---|---|---|---|---|---|---|
| T-01 | Core 現況對照表 | C／1 | `core-status-baseline` | V01–V04 | D16 | [Core現況對照表](../架構重構/Core現況對照表.md) |
| T-02 | Index／Tracking 方案 | C／1 | `index-tracking-scheme` | — | A2 | `docs/index/`、`scripts/check_index.py` |
| T-03 | 環境基線 | C／1 | `environment-baseline` | — | D18、A6 | `pyproject.toml`、`uv.lock` |
| T-04 | Discord 語音辨識異常實驗 | A／1 | `discord-stt-recognition-experiment` | V10（前置） | D20 | 實驗紀錄 planned |
| T-05 | Echo Stream 分支收斂與驗收清單 | A／1 | `echo-stream-acceptance-checklist` | V05、V06、V08–V10（Echo Stream 部分） | D3 | 驗收清單 planned |
| T-06 | 外派 Test 子系統現況與收斂 | B／1 | `subsystem-status-convergence` | — | — | 子系統現況表 planned |
| T-07 | Discord 正式輸入驗收 | A／1 | 尚未轉任務層 | V05、V06、V10 | D20、D21 | trace 與聆聽紀錄 planned |
| T-08 | Core 契約草案 | C／1 | `core-contract-draft` | （契約對應 V09、V20 等） | D16、D17、A4、A5 | [Core契約草案](../架構重構/Core契約草案.md) |
| T-09 | legacy 隔離方案 | C／1 | `legacy-isolation` | — | D19、A7 | 隔離方案 planned |
| T-10 | STT 預設模型決議 | A／1 | 尚未展開 | V10 | D20 | 決議 planned |
| T-11 | 遊戲偵測（hotwords 切換） | A／1 | 尚未展開 | V10 | D21 | 實作 planned |
| T-12 | 持久層＋CC-1 Event／journal | C／2 | `p1-persistence-event-journal` | V20 | D2、D10、D22、D32、D33 | `uep/db/`、`uep/events/` |
| T-13 | CC-6 Task 持久模型 | C／2 | `p1-task-model` | V19、V21 | D8、D30、D31、D34 | `uep/tasks/` |
| T-14 | CC-2／CC-5 schema＋fake Adapters | C／2 | `p1-context-output-fakes` | V20、V21 | D24、D29 | `uep/context/`、`uep/output/`、`uep/fakes/` |
| T-15 | V19–V21 驗證 | C／2 | `p1-v19-v21-verification` | V19、V20、V21 | — | `tests/uep/test_acceptance_v19.py` |
| T-16 | CC-4 Presence／Lease／Output Gate＋控制通道 | C／2 | `p2-presence-gate` | V11、V12 | D4、D5、D26–D28 | `uep/presence/`、`uep/contracts/`、`uep/control/` |
| T-17 | CC-7 State Coordinator＋全域停止 | C／2 | `p2-state-coordinator-stop` | — | D6、D36、D40 | `uep/state/`、`uep/stop/`、`uep/assembly.py` |
| T-18 | 最小本機 CLI 端點＋watchdog | C／2 | `p2-cli-endpoint-watchdog` | V15 | D12、A12 | `uep/endpoint/`、`uep/control/socket_transport.py` |
| T-19 | V11、V12、V15 驗證 | C／2 | `p2-v11-v12-v15-verification` | V11、V12、V15 | D44 | `tests/uep/test_acceptance_v11.py` |
| T-22 | 推理 Host 遷移 Python 3.12 | C／2 | `inference-host-py312-migration` | — | D18、A14 | `pyproject.toml` inference 組、`uv.lock` |
| T-23 | P3 前置修整（Bus §8.4、D41、D42、維護迴圈、DENIED 原因碼） | C／2 | `p3-prereq-bus-journal-hardening` | — | D41、D42、A16 | `uep/events/tailer.py`、`uep/events/payload_hash.py`、`0006_journal_txid` |
| T-24 | 週期 2 Core 追加驗證（P1、P2、T-23 對抗式稽核） | C／2 | `core-cycle2-audit` | — | D43、A20 | `tests/uep/test_cycle2_audit_a.py`、`tests/uep/test_cycle2_audit_b.py`、`tests/uep/test_cycle2_audit_c_tasks.py`、`tests/uep/test_cycle2_audit_c_context.py`、`tests/uep/test_cycle2_audit_fix2.py`；契約同步於 [Core契約草案](../架構重構/Core契約草案.md) E-17～E-22、X-7～X-9、R-7、P-16～P-25、O-13～O-15、§6.7 的 T 系列條號 16～20；`0007_journal_truncate_guard`、`0008_receipt_worker_epoch` |
| Part S | SYS Agent 基底（S0～S3 契約草案；S4、S5 僅邊界） | C／2 | 尚未轉任務層 | V29–V31、V36（延伸 V21、V22、V28） | D49–D69、A21、A22 | [SYS契約草案](../架構重構/SYS契約草案.md) |

T-10、T-11 目前只在 DECISIONS 被引用，TASKS.md 尚無卡片內容；細節待敏卡展開後補。

## 4. 驗收項（V）

「契約覆蓋」摘自 [Core契約草案 §11](../架構重構/Core契約草案.md)：`是`＝草案有對應驗證案例（VC-x-y，尚未執行）；`部分`＝案例不完整；`未涵蓋`＝草案無案例；`契約外`＝屬整合或實測驗收。覆蓋不等於驗收。

| ID | 摘要 | 任務卡 | 實作 | 測試 | 證據 | 狀態 | 契約覆蓋（草案 §11） |
|---|---|---|---|---|---|---|---|
| V01 | 核對本地設計與程式 call path，列出差異 | T-01 | — | — | [對照表 §1、§3](../架構重構/Core現況對照表.md)（讀碼＋執行） | 證據已存 | 契約外 |
| V02 | Echo Stream 與各模組實際接入狀態 | T-01 | — | — | [對照表 §2](../架構重構/Core現況對照表.md)（讀碼；core／modules 無 Echo Stream 引用） | 證據已存 | 契約外 |
| V03 | GS 啟動／雙條件終止、NLP 狀態權限、WS 持久化 | T-01 | — | — | [對照表 §2](../架構重構/Core現況對照表.md)（讀碼＋執行；單元測試不代表端到端） | 證據已存 | 契約外 |
| V04 | Frontend Bridge、ON_CALL、SLEEP、口型、播放器事件來源 | T-01 | — | — | [對照表 §2](../架構重構/Core現況對照表.md)（讀碼） | 證據已存 | 契約外 |
| V05 | 真實語音全鏈路與分段延遲 | T-05（清單）、T-07（實測） | — | — | — | 未開始 | 契約外 |
| V06 | 插話時佇列填滿仍可停止並續說 | T-05、T-07 | — | — | — | 未開始 | 是：VC-5-1、VC-4-5 |
| V07 | 取消／斷線／Lease revoke 競態無晚到輸出 | 週期 2 P3（待立卡） | — | — | — | 未開始 | 是：VC-3-1、VC-4-5 |
| V08 | 播放結束等待與 generated／spoken／unknown 三態 | T-05；週期 2 P3 | — | — | — | 未開始 | 是：VC-5-1、VC-2-3 |
| V09 | 同一輸入只產生一次 LLM／TTS | T-05；週期 2 P3 | — | — | — | 未開始 | 是：VC-7-1 |
| V10 | 長句、多語、多人重疊、回授、連續多輪 | T-05、T-07 | — | — | — | 未開始 | 契約外 |
| V11 | 三端同時 acquire 最多一個 owner | T-19（週期 2 P2） | `uep/presence/`、`uep/endpoint/` | `tests/uep/test_acceptance_v11.py`、`tests/uep/test_cycle2_audit_fix2.py` | 100 回合三端 Barrier 併發 acquire 每回合恰 1 GRANTED；併發 revoke 下取樣 ACTIVE／存活 lease／open transfer 恆 ≤1；HMAC RPC 與真實 loopback socket 各一組。T-24：每回合 revoke 後補送舊端停止證據（`revoke_and_quiesce`，A20）；`tests/uep/test_cycle2_audit_fix2.py` 驗 revoke 後僅舊端可併發 acquire、證據到達後三端併發恰一個 GRANTED | 已驗證（P2，CLI＋模擬端點） | 是：VC-4-1 |
| V12 | 舊 generation／epoch 輸出與 ACK 被拒 | T-19（週期 2 P2） | `uep/presence/gate.py`、`uep/output/manager.py`、`uep/endpoint/` | `tests/uep/test_acceptance_v12.py`、`tests/uep/test_cycle2_audit_b.py` | 舊 generation／舊 core_epoch（重啟 +1 後）輸出經 Gate、DeliveryManager、端點二次接納皆拒；舊 lease renew／release／confirm 皆 STALE；重連不恢復已撤銷權限；六元組逐欄單改皆拒。Core 重啟情境僅 in-process。T-24 補強：端點持新 Lease 時舊輸出逐欄單改皆拒、Manager renew／release／confirm_activated／confirm_quiesced 逐欄單改皆 STALE、claim 後撤銷的輸出不會到達端點；dispatch 比對已持久化 intent（`IntentMismatchError`）、Gate 例外記拒絕 | 已驗證（P2，CLI＋模擬端點） | 是：VC-4-2 |
| V13 | 跨平台移轉先停舊端再啟新端 | 週期 3 P5（待立卡） | — | — | — | 未開始 | 未涵蓋 |
| V14 | 移轉各階段 crash 恢復無雙重在場 | 週期 3 P5（待立卡） | — | — | — | 未開始 | 是：VC-4-3 |
| V15 | 舊端網路隔離的 watchdog 與嚴格模式 | T-19（週期 2 P2） | `uep/endpoint/watchdog.py`、`uep/presence/manager.py` | `tests/uep/test_acceptance_v15.py`、`tests/uep/test_cycle2_audit_b.py`、`tests/uep/test_cycle2_audit_fix2.py` | 隔離下端點依本地單調時鐘於 TTL 內停止並清 buffer，恢復後補送證據；嚴格模式證據到前新端 NOT_QUIESCED、偽造證據不放行、人為確認可放行；證據僅於播放真正停止後送出。缺口記 D44。T-24 補強：端點二次接納自守本地期限與 `valid_until`；停止證據白名單與資格（缺 `playing=False`／`buffer_flushed=True` 為 `EVIDENCE_REJECTED`）；`fresh_process` 須舊 Lease `expires_at`＋30 秒已過，端點有上限（65 秒）等待；revoke 後他端放行條件（A20）；operator `resolve-recovery` API／CLI | 已驗證（P2，CLI＋模擬端點） | 是：VC-4-4 |
| V16 | 離場／加入失敗與 timeout 的 NONE／RECOVERING | 週期 3 P5（待立卡） | — | — | — | 未開始 | 部分：VC-4-4、VC-5-3 |
| V17 | 多分頁／多 Desktop／多頻道的排他粒度 | 週期 3 Part H（多 Desktop）、P4（多頻道）、P5（多分頁）（待立卡） | — | — | — | 未開始 | 未涵蓋 |
| V18 | 缺席的 Discord 不錄音不參與 | 週期 3 P4（待立卡） | — | — | — | 未開始 | 未涵蓋 |
| V19 | Task 跨 GS 結束可恢復；Outbox 重播與 DeliveryManager 去重（production 尚無 task 終局→OutputIntent consumer） | T-15（週期 2 P1） | `uep/tasks/` | `tests/uep/test_acceptance_v19.py`、`tests/uep/test_cycle2_audit_c_tasks.py` | fake 驗證：對話端丟棄＋新 manager 可恢復；6 個 crash 點×3 種 idempotency 已成功 action 恆執行 1 次；完成通知單次；task 終局事件與狀態同交易寫 outbox（target `notification`），Bus 投遞前崩潰可由 outbox 補送。範圍聲明（T-24）：「完成通知單次」只驗證 Outbox 重播與 DeliveryManager 去重（測試自行組出 OutputIntent），production 目前尚無 task 終局事件→OutputIntent 的 consumer；另補 Task／Run／Action 稽核回歸（暫停連動、`resume_run` 暫停原因、`transition_run` 必填 epoch、T-13 全 Run 檢查、`run.ended` 的 `unresolved_actions` 等） | 已驗證（P1，fake） | 是：VC-6-1 |
| V20 | 重送／Outbox 重播不重複 action | T-15（週期 2 P1） | `uep/events/`、`uep/output/manager.py` | `tests/uep/test_acceptance_v20.py`、`tests/uep/test_cycle2_audit_a.py`、`tests/uep/test_cycle2_audit_fix2.py` | fake 驗證：重送／併發重送／ACK 前崩潰重播／雙 dispatcher／delivery 各 crash 點重啟，action execution_count 與 output effect_count 皆 1。已知邊界：權威 lookup 誤報 confirmed_not_sent 且 adapter 非冪等時重送 1 次。T-24 補強：`create_action` 第二層 IntegrityError 仍去重、同 action_id 比對 run／tool／side_effect_class／params_hash；冪等重送不一致拋 `IdempotencyConflict`（含 blob 三態與密鑰輪替不誤報，`tests/uep/test_cycle2_audit_a.py`、`tests/uep/test_cycle2_audit_fix2.py`） | 已驗證（P1，fake） | 是：VC-1-1、VC-1-2 |
| V21 | 工具已做完但 response 遺失可查 receipt | T-15（週期 2 P1） | `uep/tasks/`、`uep/adapters/tool.py` | `tests/uep/test_acceptance_v21.py`、`tests/uep/test_cycle2_audit_c_tasks.py`、`tests/uep/test_cycle2_audit_fix2.py` | fake 驗證：found／undetermined／confirmed_not_executed 各條件、unverified receipt、lookup 例外、輸出端 unknown 皆不盲目重送。已知邊界：查核誤報＋宣告權威＋宣稱可冪等卻不去重的 executor 會重複 1 次。T-24 補強：zombie worker 晚到 receipt 立即入庫，`resolve_unknown` 優先採已存已驗證 receipt、不退回 INTENDED；階段 1.5 呼叫前重檢（被擋 FAILED＋Core 自簽 receipt，executor 零次呼叫）；階段 3 入庫失敗轉 UNKNOWN；receipt 內容限制與 HMAC 脫敏。已知限制：重檢與 executor 呼叫之間仍有殘餘極短窗口 | 已驗證（P1，fake） | 是：VC-6-2 |
| V22 | 離場暫停 presence-bound 工作 | 週期 4 P8（待立卡） | — | — | — | 未開始 | 是：VC-6-5 |
| V23 | DB／GPU／MEM／API 失敗的降級 | 週期 4 P8（待立卡） | — | — | — | 未開始 | 部分：VC-2-3 |
| V24 | 備份還原與 epoch 重建 | 週期 4 P8（待立卡） | — | — | — | 未開始 | 未涵蓋 |
| V25 | 無輸入時授權事件仍產生候選 | 週期 4 P7（待立卡） | — | — | — | 未開始 | 未涵蓋 |
| V26 | 靜音／冷卻／過期不造成連續打擾 | 週期 4 P7（待立卡） | — | — | — | 未開始 | 未涵蓋 |
| V27 | 高 salience 但不適合打擾仍可延後 | 週期 4 P7（待立卡） | — | — | — | 未開始 | 未涵蓋 |
| V28 | 私聊→公開 Discord 不洩漏私人內容 | 週期 3 P5（待立卡） | — | — | — | 未開始 | 是：VC-2-1 |
| V29 | 惡意內容要求擴權被 Policy 拒絕 | 週期 2 Part S（待立卡） | — | — | [SYS契約草案](../架構重構/SYS契約草案.md) | 未開始 | 是：SYS契約草案 VC-S2-3 |
| V30 | LLM 自述核准不能消耗 approval | 週期 2 Part S（待立卡） | — | — | — | 未開始 | 是：VC-6-4；SYS契約草案 TG-7 反例、VC-S2-4 |
| V31 | 參數變更後舊 approval 失效；global stop 不依賴 LLM | 週期 2 Part S（待立卡） | — | — | — | 未開始 | 是：VC-6-3；SYS契約草案 VC-S2-4、TG-11 |
| V32 | 使用者電腦關機 Mac Core 仍運作 | 週期 4 P8（待立卡） | — | — | — | 未開始 | 契約外 |
| V33 | 重開機後只有重新授權的 Presence 到場 | 週期 4 P8（待立卡） | — | — | — | 未開始 | 未涵蓋 |
| V34 | 長時間運行資源有界 | 週期 4 P8（待立卡） | — | — | — | 未開始 | 契約外 |
| V35 | 使用者可知她在哪、為何離開 | 週期 4 P8（待立卡） | — | — | — | 未開始 | 未涵蓋 |
| V36 | SDK 引入前後同案例比較 | 週期 4 P6（待立卡） | — | — | — | 未開始 | 契約外 |

### 填寫範例（有實作後）

```text
| V12 | 舊 generation／epoch 輸出與 ACK 被拒 | T-15 | `uep/presence/lease.py` | `tests/presence/test_lease.py` | `docs/證據/V12-2026-xx-xx.md`（執行） | 進行中 | 是：VC-4-2 |
```
