# DL-6 草案：ProactiveCandidate 契約與 `proactive.initiate` 決策點（規則版＋shadow）

性質：設計文件（含 DL-6a 實作對照，見第 9 節）。狀態：**定稿**——Q-A～Q-J 全部照建議裁決，並追加房內裁決（第 9.1 節）；DL-6a（契約、規則 engine、shadow 日誌）已實作於 `feature/decision-dl6a`，DL-6b（對接 S-H）尚未開始。
基準：develop c6cd25a。縮寫：提案10-25 = `hidden_docs/架構重構/UEP_新架構研究/UEP_新架構技術設計提案_2026-10-25.md`；決策中樞 = `docs/架構重構/決策中樞架構提案.md`；Agent提案 = `docs/架構重構/Agent架構與工具契約提案.md`。

## 0. 結論（先看這裡）

- `ProactiveCandidate` 放新檔 `uep/decision/proactive.py`，不放 `contracts.py`（理由見 1.2）。
- `proactive.initiate` = async、`shadow=True`、無 native，結果只用既有區段 `loop`／`placement`／`followups`；Phase 1 沒有任何消費者會啟動 Run。
- 規則版仲裁 9 道關卡，硬性條件先於評分；每道有固定 reason_code；通過全部才是 `ready`。
- 不需要 migration：`decision_log` 已有 `candidate_id`／`sub_kind`／`trigger`／`namespace`／`snapshot_version`（0014）。需要兩處小程式修改（見 5）。
- S-H 只做純函式轉換 `(candidate, result, injections) -> LoopTrigger | None`，Phase 1 的輸出只記錄不啟動。
- 房內追加裁決（goal_code 與標籤表分工、`capability_lookup` 注入、import 邊界白名單、冷卻 ledger 持久化風險）見第 9.1 節。

## 1. `ProactiveCandidate`

### 1.1 欄位表

「§9.2」欄位 = 提案10-25 §9.2 的 14 項（`UEP_新架構技術設計提案_2026-10-25.md:345`）。偏離與新增欄位標「新增」並說明理由。

| 欄位 | 型別 | 必填 | 來源 | 對應 §9.2 |
|---|---|---|---|---|
| `candidate_id` | `str`（`new_id("pcand")`） | 必 | 評估階段產生；同時是 `DecisionRequest.subject["candidate_id"]` 與 `LoopTrigger.origin_ref` | candidate_id |
| `trigger_event_ids` | `tuple[str, ...]`（>=1） | 必 | 觸發的 journal 事件 id（如 `proactive.trigger_observed`，`uep/events/registry.py:105`；`uep/memory/sink.py:30`） | trigger_event_ids |
| `rule_version` | `str` | 必 | 產生候選的評估規則版本（不是仲裁 engine 版本；後者在 `DecisionEngineInfo`） | rule_version |
| `goal_ref` | `str`（閉集 `goal_code`，見 4.3） | 必 | 評估階段映射；不是自由文字（Agent提案 §4.2、決策中樞 4.6.1「goal_code 閉集短碼」） | goal_ref |
| `evidence_refs` | `tuple[str, ...]` | 必（可空 tuple） | 事件／記憶 ref，只放 id，不放原文 | evidence_refs |
| `memory_salience` | `float` 0..1 | 必 | MEM 重要性；只用於排序，與打擾價值分開（§9.2 第二段） | memory_salience |
| `urgency` | `Urgency` 列舉：`low／normal／high／critical` | 必 | 見 4.5 Q-H：`critical` 只能來自可信 Policy | urgency |
| `confidence` | `float` 0..1 | 必 | 評估階段對「這件事值得提醒」的信心 | confidence |
| `interruptibility_cost` | `float` 0..1 | 選（預設 None） | 評估當下的估計值；仲裁以即時活動事實重算，不信任候選自帶值 | interruptibility_cost |
| `privacy_scope` | `str`，scope_ref 格式 `scope_<private\|group\|public>[_<id>]` | 必 | 內容的 scope；格式以 `uep.events.envelope.scope_rank`（`uep/events/envelope.py:89`）驗證 | privacy_scope |
| `desired_location` | `PresenceLocation \| None` | 選 | 既有型別 `uep/contracts/presence.py:19`（desktop／discord／website） | desired_location |
| `expires_at` | `datetime`（aware） | 必 | 候選有效期；過期不得啟動 | expires_at |
| `dedupe_key` | `str`（安全字元集） | 必 | 提醒語意＋任務＋對象的組合鍵（§9.2 去重段） | dedupe_key |
| `decision_reason` | `str \| None` | 選 | **偏離**：仲裁輸出本來就是 `DecisionResult.reason_code`；候選只在 REEVALUATED 時攜帶「上一次決策的 reason_code」。不由候選自己宣稱結論 | decision_reason |
| `source_kind` | `SourceKind` 列舉：`schedule／task／memory／platform／assignment` | 必（新增） | 決定 `LoopTrigger.kind`：`assignment`→`external_event`，其餘→`proactive`（Agent提案 `:151-152`）；也決定哪些關卡可略（見 3.2 G1） | 無（新增，§9.1 觀察階段「事件來源之一」） |
| `owner_actor_id` | `str` | 必（新增） | 授權主體；非 owner 來源固定艾斯維爾（D64，`uep/agent/trigger.py:20`） | 無（新增，授權關卡需要） |
| `authorization_ref` | `str \| None` | 選（新增） | 事先授權（例如「到點可選位置提醒」）的 grant id；沒有就走保守路徑。**候選不得自帶授權內容，只帶 ref，由 context 側查證**（§9.2「LLM 敘述不能自己新增外部授權」） | 無（新增，§9.2 末段硬性約束） |
| `created_at` | `datetime`（aware） | 必（新增） | 供排序／過期／退避計算 | 無 |

不放入候選的東西：原文、summary、LLM 敘述、當下 Audience 與 Presence（它們是仲裁時的即時事實，不是候選屬性）。

### 1.2 放置位置與依賴

- 結論：新檔 `uep/decision/proactive.py`（候選、`ProactiveContext`、reason_code 常數、spec、rule engine 同檔；規模與 `memory_dream.py` 相近，278 行）。
- 理由：
  - `contracts.py` 宣告「葉節點，不 import uep 其他部分」（`uep/decision/contracts.py:1`），裡面全是跨決策點共用的形狀（Request／Result／Plan 區段）。候選是 Proactive 擁有的資料結構（決策中樞 `:161`「候選資料結構由 Proactive 擁有」），且需要 `PresenceLocation` 與 scope 驗證，會破壞葉節點。
  - `memory_dream.py` 已有先例：一個決策點一個檔，內含 spec／engine／decider（`uep/decision/memory_dream.py:55`、`:270`）。
- 可 import：`uep.contracts.presence`（CC-4 共用型別，Core 與端點同源）、`uep.events.envelope.scope_rank`、`uep.context.snapshot.scope_admits`（`uep/context/snapshot.py:79`，Audience 放入規則）。不 import `uep.turn`（Audience 在 turn 層就是字串 `audience_scope`，`uep/turn/models.py:26,96`，無獨立型別）、不 import `uep.agent`（轉換函式見 4.2）。
- Audience：沿用 `audience_scope: str` 加 `scope_admits`，不新增型別。Presence：沿用 `PresenceStatus`／`PresenceLocation`，即時快照取自 `PresenceView`（`uep/presence/manager.py:85`），進 facts 時展平成英文欄位。
- 不帶原文：dataclass 內字串欄位全部過安全字元集驗證（比照 `uep/decision/log.py:42` `_SAFE_TOKEN`、`uep/events/envelope.py:19` `_SAFE_REF_PATTERN`）；`__post_init__` 驗證失敗 → 建構時拋 `ValueError`，由評估階段處理，不進仲裁。
- ADR 備註（奈也）：欄位寧可少，等觀察期證明需要再加。（奈留）：`privacy_scope` 與 `audience_scope` 兩者不得混用；前者是內容，後者是此刻聽眾，混用會讓「不公開內容」的情境失效（提案10-25 §9.3 表第二列）。

## 2. 決策點 `proactive.initiate`

### 2.1 Spec

| 項目 | 值 | 依據 |
|---|---|---|
| kind | `proactive.initiate` | 決策中樞 `:335` |
| mode | `async`，由 `hub.submit` 提交 | `uep/decision/hub.py:51`；決策中樞 `:310` |
| owner | `proactive` | — |
| outcomes | `ready`／`deferred`／`rejected`（對應 §9.2 狀態機的 READY／DEFERRED／REJECTED） | 提案10-25 `:350-356` |
| budget_ms | 1000（純規則；只當 engine 保險絲，比照 `memory_dream` 的 `_DEFAULT_BUDGET_MS`） | `uep/decision/memory_dream.py:48` |
| on_timeout／busy／error／invalid reason | `proactive_timeout`／`proactive_busy`／`proactive_error`／`proactive_invalid`；fallback 一律 `deferred`（不誤啟動） | D80 預設；`memory_dream.py:44-47` |
| shadow | `True`；allow_native `False` | 決策中樞 `:335`「強制先 shadow」 |
| plan_sections | 宣告 `("loop", "placement", "followups")`；不得帶 `route`／`reply`／`output`／`policy` | 見 2.3 與 Q-I |

### 2.2 DecisionRequest 組成

| 欄位 | 內容 |
|---|---|
| `request_id` | `<candidate_id>:eval:<seq>`（冪等鍵） |
| `trigger` | 新候選 `event`；退避後重評 `reeval`（帶 `parent_decision_id`）；定時掃描 `tick` |
| `subject` | `{"candidate_id", "namespace"}`；兩者都要，因為日誌欄位由此取（見 5） |
| `facts["candidate"]` | `ProactiveCandidate` 展平（`asdict`，datetime 轉 ISO）；全是 ID／列舉／數值 |
| `facts["context"]` | `ProactiveContext` 展平（注入值，見 4.4） |
| `allowed_outcomes` | `spec.outcomes` |
| `snapshot_versions` | `{"policy": <policy 快照版本>, "presence": <presence generation>}` |

### 2.3 輸出區段（回答「會不會提醒、通道、理由碼、冷卻、arbitration 結果」）

| 要記錄的事 | 落在哪 | 說明 |
|---|---|---|
| 會不會提醒／啟動 | `DecisionResult.outcome` | `ready`＝會；`deferred`＝稍後再評；`rejected`＝不會 |
| 為什麼 | `DecisionResult.reason_code` | 閉集，見 3.3 |
| 卡在哪一關 | 日誌 `sub_kind` 欄位（既有，`uep/decision/log.py:75-90`） | 值：`validity／stop／authorization／audience／presence／cost／cooldown／budget／confidence／ready`（G8 信心門檻在實作時補上 `confidence`，見 9.2） |
| 通道 | `plan.placement`（既有 `DecisionPlacement`，`contracts.py:70`） | `target_platform`＝PresenceLocation 值；`presence_intent`＝`stay`／`request_move`／`none`；`audience_scope_max`＝可揭露內容的上限 scope |
| 啟動範圍 | `plan.loop`（既有 `DecisionLoop`，`contracts.py:80`） | 只在 `ready` 且 `goal_code` 屬任務類時帶：`start=True`、`trigger_kind`、`goal_code`、`policy_ref`；`reeval_on` 空 |
| 冷卻／下次評估 | `plan.followups`（既有 `DecisionFollowup`，`contracts.py:104`） | `DecisionFollowup(kind="proactive.initiate", after_ms=…)`，先例 `memory_dream.py:196-203` |
| arbitration 結果 | 以上組合 | 不新增 plan 區段 |
| 去重 | `reason_code`＝`duplicate_*` 並以 `dedupe_key` 對 ledger | 不入 plan |

三種 `ready` 的形狀（對應 §9.1 步驟 4「提醒交付、移轉請求或 Task」，`UEP_新架構技術設計提案_2026-10-25.md:339`）：

| initiation | plan 形狀 | 後續 |
|---|---|---|
| 提醒交付 | `placement` 有、`loop` 為 None | 走 Output Gate（Phase 1 不接） |
| 移轉請求 | `placement.presence_intent="request_move"` | 交給 `presence.join_or_move`（獨立 kind，不在本草案） |
| 任務／Run | `loop.start=True` | S-H 轉 `LoopTrigger`（4.2） |

### 2.4 「強制先 shadow」怎麼落地

- Phase 1 沒有任何 `ready` 的消費者：assembly 只掛 `DecisionLogWriter` 觀察者，不掛啟動 Run／投遞提醒的回呼。
- 以測試鎖住（比照 `test_decision_policy_section_lock`，見 `contracts.py:95` 的註記）：assembly 不得出現 `proactive.initiate` 結果的非日誌消費者。
- 不採「空 primary＋規則 engine 當 shadow」的雙軌：目前所有 kind 都只有一個 engine，shadow 是 engine 內自帶 proposer 的機制（`turn_interject.py`），為 proactive 另起雙軌成本高、價值低（見 Q-D）。

## 3. 規則版仲裁

原則：硬性條件（授權、Audience、Presence、停止）先於任何評分；高分不得抵銷（提案10-25 `:347`）。engine 無狀態純函式；冷卻 ledger 由呼叫端注入（Q-E）。

### 3.1 判斷順序

| 序 | 關卡 | 判斷（皆為 context 提供的事實） | 未通過的 outcome |
|---|---|---|---|
| G0 | validity | 候選已過期；`dedupe_key`／`privacy_scope` 格式錯；`trigger_event_ids` 空 | rejected |
| G1 | stop／撤銷 | 全域停止中、授權被撤銷（優先序最高，§9.3 第一項） | rejected（停止中）／rejected（撤銷） |
| G2 | authorization | `authorization_ref` 查證結果：無授權、授權未涵蓋此 `privacy_scope` 或此通道。`source_kind=assignment` 且 `owner_actor_id` 為艾斯維爾視為已授權 | rejected |
| G3 | audience | `scope_admits(audience_scope, candidate.privacy_scope)` 為假（聽眾比內容寬） | deferred；若有 `desired_location` 或私人位置可用 → `placement.presence_intent="request_move"` |
| G4 | presence | 狀態 NONE／RECOVERING／移轉中；`desired_location` 與當前不符 | 見 3.2 |
| G5 | interruption cost | 活動不可中斷（desktop 不可中斷操作）、靜音、承諾中的互動；然後比較價值（salience、urgency、confidence）與即時成本 | deferred（等 checkpoint）／rejected（價值<成本且非 deadline） |
| G6 | cooldown / dedupe | 同 `dedupe_key` 近期已投遞；ACK 未知；同對象最小間隔；同批人 1 小時（D84 `DECISIONS.md:828-840` 的冷卻語意） | deferred（附 `after_ms`）／rejected（已投遞） |
| G7 | budget | 單位時間主動次數上限 | deferred |
| G8 | confidence floor | `confidence` 低於門檻（放最後；它是評分，不是硬性條件） | rejected |
| ready | 全部通過 | | ready |

### 3.2 補充規則

- G1：`source_kind=assignment`（Chatroom 指派，NT-7）仍受 G1 約束；G2 以後的豁免只限授權關卡（owner 已知），G3/G4/G5/G6 照走。
- G4 分支：
  - 有效 Presence 且位置相符 → 通過。
  - 位置不符 → `deferred`＋`request_move`（提議，交 `presence.join_or_move`，不在此決定移轉）。
  - 狀態 NONE：有 `authorization_ref` 且 grant 允許選位置 → `ready`＋`placement.target_platform`（提案10-25 §9.3 表第四列）；否則 `deferred`（`presence_none_queued`，列待交付）。
  - RECOVERING／移轉中：`deferred`，`after_ms` 取短值。
- G5：價值與成本比較只在 G1～G4 通過後才算；`urgency=critical` 且來源可信可略過「價值<成本」但不略過不可中斷（等 checkpoint，`ack_pending` 逾時回報服務狀態屬後續）。
- G6：ACK 未知 → `deferred`＋`delivery_unknown`（先查核交付、不重試，§9.3 表第五列）。
- 寫入類欄位：engine 不更新 ledger；投遞後的紀錄由 Phase 2 消費者寫。Phase 1 因為無投遞，冷卻只能由「近期 shadow 為 ready 的紀錄」推算，見 Q-E。

### 3.3 reason_code 清單（安全字元，全小寫）

| 關卡 | outcome | reason_code |
|---|---|---|
| G0 | rejected | `candidate_expired`, `candidate_malformed` |
| G1 | rejected | `global_stop`, `authorization_revoked` |
| G2 | rejected | `no_authorization`, `authorization_scope_insufficient` |
| G3 | deferred | `audience_too_wide_move`（有私人位置可移）, `audience_too_wide_wait` |
| G4 | deferred | `presence_transferring`, `presence_mismatch_move`, `presence_none_queued` |
| G4 | ready | `presence_none_preauthorized` |
| G5 | deferred | `activity_not_interruptible`, `muted`, `in_committed_interaction` |
| G5 | rejected | `cost_exceeds_value` |
| G6 | deferred | `cooldown_active`, `delivery_unknown` |
| G6 | rejected | `duplicate_delivered` |
| G7 | deferred | `budget_exhausted` |
| G8 | rejected | `low_confidence` |
| 通過 | ready | `ready_present`（Presence 相符）, `ready_move`（需要移轉）, `ready_assignment` |
| fallback | deferred | `proactive_timeout`, `proactive_busy`, `proactive_error`, `proactive_invalid` |

reason_code 總數 28（含 4 個 fallback），登記於 spec 以符合決策中樞 `:344`「每個 kind 的 reason_code 集合隨 spec 註冊」。

### 3.4 第一階段的行為

只記錄、不啟動 Run；`ready` 等同「會啟動」，由 S-H 的轉換函式驗證形狀後丟棄（4.2）。決策日誌靜默抑制：engine 實作 `log_quiet`（先例 `memory_dream.py:174`），同一 `candidate_id` 相同 `reason_code` 連續出現只計數，避免重評灌滿日誌。

## 4. 與工具層的交界（切片 S-H）

### 4.1 分工

| 項目 | 決策層（DL-6） | 工具層／Agent 層（S-H） |
|---|---|---|
| 候選、仲裁、reason_code | 提供 | 不判斷觸發 |
| 能不能啟動 | `outcome=ready` 且 `plan.loop.start` | 不重算 |
| `LoopTrigger.kind` | `plan.loop.trigger_kind`（`proactive`／`external_event`） | 驗證 ∈ `TRIGGER_KINDS`（`uep/agent/trigger.py:15`） |
| `goal_code` | `plan.loop.goal_code`（閉集） | 查預設標籤表（4.3） |
| `capability_tags` | **不提供**（`DecisionPlan` 目前沒有 `capability` 區段） | 由 goal_code 查表；非空；不得大於 `placement.audience_scope_max` 所允許 |
| `scope_ref` | `candidate.privacy_scope`（只可收窄，E-6） | 沿用 |
| `owner_actor_id` | `candidate.owner_actor_id`（D64） | 沿用 |
| `policy_ref` | `plan.loop.policy_ref`；尚無 Policy 服務時用固定預設（Agent提案 D1 註記 4） | 解析預算 |
| `snapshot_ref` | context 側產生 `SnapshotPurpose.PROACTIVE_EVAL` 快照（`uep/context/snapshot.py:59`）的 ref | 沿用 |
| `plan_ref` / `origin_ref` | `decision_id` / `candidate_id` | 沿用 |

### 4.2 轉換函式

```python
# 純函式；Phase 1 的回傳值只進日誌（would_start），不呼叫 AgentLoop
def to_loop_trigger(
    candidate: ProactiveCandidate, result: DecisionResult, inj: ProactiveInjections,
) -> LoopTrigger | None:
    # None 條件：outcome != ready、plan.loop 為 None 或 start=False、capability_tags 查表為空
    ...
```

- 方向：決策層 → Agent 層（Agent提案 `:116`：決策層可 import `LoopTrigger`，Agent 層不 import 決策層型別）。因此函式放決策側 `uep/decision/proactive.py`，只 import `uep.agent.trigger`。**已驗證（9.1）**：`test_import_boundary.py` 原本沒有限制 `uep/decision/` 與 `uep/agent/` 的互相 import，DL-6a 新增白名單規則：`uep/decision` 對 `uep.agent` 只准 `uep.agent.trigger`；`uep/agent` 對 `uep.decision` 只准 `loop_consult` 與 `contracts`；且 `loop_consult.py`、`contracts.py` 對 `uep.agent` 至多 `trigger`（防循環）。
- 無法產生 Trigger（標籤空）時：日誌記 `capability_empty`，不啟動。符合 Agent提案 `:896`「Agent 層永遠不會收到空 capability_tags」。標籤由注入的 `ProactiveInjections.capability_lookup(goal_code)` 取得，查不到、回空或例外一律視為 `capability_empty`；決策側不 import 工具層的標籤表。
- Chatroom 指派（NT-7）：`source_kind=assignment` → `external_event`；Task owner 固定艾斯維爾。

### 4.3 goal_code 閉集（Q-B 已裁決：goal_code 歸決策層，標籤表歸工具層）

| goal_code | initiation | 說明 |
|---|---|---|
| `remind_due` | 提醒交付（不啟動 Run） | 到期提醒 |
| `report_task_done` | 提醒交付 | 對應 `report.route` 的 Proactive 分支 |
| `request_presence_move` | 移轉請求 | 交 `presence.join_or_move` |
| `assignment_pickup` | Run（`external_event`） | Chatroom 指派 |
| `memory_followup` | Run（`proactive`） | 記憶變更後續 |
| `join_voice` | 預留 | D84，Phase 1 不實作 |

### 4.4 需要注入的值（不得由候選或模型自帶）

`ProactiveContext` 由 assembly 組裝，進 `facts["context"]`：

| 注入值 | 來源 | 用途 |
|---|---|---|
| `current_platform`（PresenceLocation 或 None） | `PresenceView.location`（`uep/presence/manager.py:85`） | G4；與 `loop_consult` 的 `current_platform` 同語意（`loop_consult.py:130`） |
| `presence_status`、`presence_generation`、`transfer_pending` | `PresenceView` | G4 |
| `audience_scope` | 當前 Presence endpoint 的聽眾 scope | G3 |
| `available_platforms`／`private_location_available` | Presence／端點註冊 | G3 移轉選項 |
| `authorization`（grant 查證結果：有效、涵蓋 scope、可選位置） | 授權服務（尚無；Phase 1 用 fixture） | G2 |
| `global_stop`、`mute`、`activity`（可中斷等級） | State／Stop 軸（`uep/state`；待確認欄位名） | G1、G5 |
| `ledger`（近期投遞：dedupe_key、對象、時間、ack 狀態；主動次數） | 呼叫端 | G6、G7 |
| `now`（aware datetime） | 呼叫端（引擎不呼叫 clock，便於重播） | G0、G6 |
| `policy`（門檻、冷卻秒數、靜音時段）＋版本 | `policy.snapshot`（`policy_publish.py`） | G5～G8 |

### 4.5 裁決結果（Q-A～Q-J 全部照建議）

| # | 問題 | 選項 | 裁決（原建議） |
|---|---|---|---|
| Q-A | Phase 1 的候選從哪來 | (a) 手寫 fixture／測試；(b) 重播 journal 的 `proactive.trigger_observed`；(c) 即時訂閱事件 | (a)+(b)：先用 (a) 驗證規則，再以 (b) 對歷史資料跑 shadow；(c) 留到有 producer 切片 |
| Q-B | goal_code 閉集與預設標籤表歸誰 | (a) goal_code 歸決策層、標籤表歸工具層；(b) 決策層一併提供 `plan.capability`；(c) S-H 暫用固定最小標籤集 | (a)；`DecisionPlan` 目前無 `capability` 區段（`contracts.py:121`），為此新增區段超出本卡範圍 |
| Q-C | 轉換函式放哪 | (a) `uep/decision/proactive.py`；(b) assembly；(c) `uep/agent/` | (a)，前提是 import 邊界允許（待驗） |
| Q-D | 如何保證 shadow-only | (a) 無消費者＋鎖定測試；(b) 空 primary＋規則 engine 當 shadow；(c) 加 spec 旗標 `dispatch_enabled` | (a)；(c) 等 Phase 2 開放有限主動行動時再加 |
| Q-E | 冷卻／預算 ledger 放哪 | (a) engine 內記憶體；(b) 呼叫端注入（純函式）；(c) DB 表（需 migration，需要編號） | (b)：重播可重現，避免 engine 持有權威狀態；持久化留 Phase 2 |
| Q-F | D84 關係加權（熟悉度／好感）要不要進 Phase 1 | (a) 進；(b) 只保留 `join_voice` goal_code；(c) 不碰 | (b)：D84 分數定義尚待裁決（`DECISIONS.md:836` 待裁決段） |
| Q-G | 私人內容遇公開 Audience | (a) 一律 reject；(b) deferred＋可移則 `request_move`，不可移則 queue；(c) 降級成不含內容的通知 | (b)：對應 §9.3 表第二列；(c) 需要 Output 側去識別化，Phase 1 不做 |
| Q-H | `urgency=critical` 的來源 | (a) 僅可信 Policy；(b) 事件宣稱即可；(c) 全不信任 | (a)：§9.3「Discord 寫緊急不自動取得最高權限」（`:360`）；候選建構時 `critical` 需附 `policy` 來源標記 |
| Q-I | `plan_sections` 的強制 | (a) 現況：hub 只擋 `policy`（`hub.py:88`），proactive 以引擎單元測試鎖定；(b) hub 泛化，非空即強制；(c) 新增 spec 旗標 | (a)；(b) 會影響 `memory.dream`（宣告 `()` 但實際帶 `loop`／`followups`），需要先清理 |
| Q-J | `ready` 非 Run 類（提醒交付）由誰消費 | (a) Phase 2 的投遞器走 Output Gate；(b) 也包成 LoopTrigger；(c) 暫不處理 | (a)；S-H 範圍只含 Run 類，不要讓提醒走 Agent 迴圈 |

## 5. `decision_log` 欄位與 migration

結論：**不需要新欄位、不需要 migration。**

| 需求 | 現有欄位 | 缺口 |
|---|---|---|
| 候選 id | `candidate_id` | `primary_row` 只從 `request.native` 取（`uep/decision/log.py:85`）；proactive 不帶 native → 需改為 fallback `request.subject.get("candidate_id")`（1 行，加上安全字元檢查） |
| 卡在哪一關 | `sub_kind`（0014，`log.py:88`） | 無；`facts["sub_kind"]` 填關卡名即可，已過 `_safe_token` |
| 觸發 | `trigger` | 無 |
| 主體 | `namespace` | 無（`subject["namespace"]`） |
| policy 版本 | `snapshot_version` | 無 |
| 通道、冷卻 | 無專屬欄位 | 不記進日誌（`plan_digest` 本就不存在，提案 `:343` 的 plan 摘要尚未實作）；觀察期用 reason_code＋sub_kind 就足以回答「會不會、為什麼」 |
| 掛載 | `DecisionLogWriter.attach` 預設只串三個 kind（`log.py:197`） | 預設清單加入 `proactive.initiate`（或呼叫端明示 `kind=`），才能啟用 `log_quiet` |

若觀察期證明需要記「通道」或「arbitration trace」：另開 migration（接 0014，需要編號 0015）。目前不做。

## 6. 測試計畫

僅離線單元測試，不需 DB／服務。

- DL-6a 實際測試檔為 `tests/uep/test_decision_proactive.py`（離線）；shadow 日誌落盤另有 DB 測試（需 `UEP_TEST_DATABASE_URL`）。以下為原計畫。
- 契約：
  - 候選建構驗證（過期、scope 格式、不安全字元、`trigger_event_ids` 空、urgency 非列舉）→ 拋 `ValueError`。
  - 候選／context 展平後不含原文欄位（欄位白名單測試）。
- 規則 engine：
  - 每個 reason_code 至少一個案例（表 3.3 共 28 個，一對一）。
  - 關卡順序：高 `memory_salience`＋`urgency=high` 仍被 G2/G3 擋下（硬性先於評分）。
  - G1 優先於一切（assignment 也一樣）。
  - 提案10-25 §9.3 的五個情境各一個案例。
  - 同輸入同結果（純函式，固定 `now`），重播一致。
  - `source_kind=assignment` 的 owner 固定為艾斯維爾。
- hub／spec：
  - `submit` 路徑；`decide` 對 async kind 拒絕（`hub.py:41`）；帶 native 被拒。
  - 逾時／busy／例外／invalid → `deferred` 且 `fallback=True`，reason 取自 spec。
  - 輸出區段只含 `loop`／`placement`／`followups`；`policy` 被剝除。
- 日誌：
  - `primary_row` 對 proactive 填 `candidate_id`／`sub_kind`／`trigger`；日誌列不含原文。
  - `log_quiet`：同 candidate 相同 reason 連續出現只計數。
- shadow-only 鎖：
  - assembly 中 `proactive.initiate` 結果只有日誌觀察者；無 Run 被建立（以 FakeTasks 斷言 0 個 Run）。
- S-H（DL-6b）：
  - `ready`＋`loop.start` → `LoopTrigger` 欄位逐一對映（4.1 表）。
  - `deferred`／`rejected`／`loop=None` → `None`。
  - 標籤查表為空 → `None`＋`capability_empty`。
  - `scope_ref` 不寬於 `privacy_scope`。
  - `assignment` → `external_event`；`schedule` → `proactive`。

## 7. 實作切分

| 切片 | 內容 | 前置 | 不含 |
|---|---|---|---|
| DL-6a 契約＋規則 engine＋shadow 日誌（**已實作**，`feature/decision-dl6a`；範圍含 `to_loop_trigger` 純函式與 `replay_proactive` 重播工具，見 9.2） | `uep/decision/proactive.py`（候選、context、reason_code、spec、engine、`log_quiet`）；`log.py` 兩處小修（candidate_id fallback、attach 預設清單）；`__init__` 匯出；測試（第 6 節除 S-H 外） | 無（不依賴工具層） | assembly 組裝、候選 producer（`to_loop_trigger`／`loop_trigger_decision` 已在本切片） |
| DL-6b 對接 S-H（**已實作**，`feature/decision-dl6b`，見 9.3） | goal_code→標籤查表（工具層提供，尚未接，暫回 None）、`ProactiveContext` 的真實注入（Presence／狀態／授權）、assembly 掛載（只掛日誌）、shadow-only 鎖 | DL-6a；Agent提案切片 D（`LoopTrigger` 已存在，`uep/agent/trigger.py`）；Q-A～Q-C 裁決 | 啟動 Run、投遞提醒、`presence.join_or_move`、`report.route` |

## 8. 風險

- 授權服務尚不存在：G2 在 Phase 1 以 fixture 驗證，實際授權模型（grant 結構、可選位置）需另案；Q-H 的 `critical` 可信來源同理。
- Phase 1 無真實投遞，冷卻與 ACK 未知關卡只能以測試資料驗證；上線觀察期的冷卻準確度要等 Phase 2。
- 規則門檻（成本、冷卻秒數、預算）暫以 Policy 快照注入；數值未定，先訂預設並標為暫定。
- `plan_sections` 目前只擋 `policy`（`hub.py:88`），proactive 的區段限制只靠測試，hub 層沒有硬保證（Q-I）。
- 每個重評都會產生日誌列；若 `log_quiet` 抑制不足，需調整策略（例如只記 reason_code 變化）。
- **Phase 2 開放投遞前，冷卻須持久化，否則重啟會重複提醒。** 目前冷卻／預算 ledger 由呼叫端注入（純函式，Q-E），engine 不持有權威狀態；進入 Phase 2 前需要決定 ledger 的持久化位置（DB 表需新 migration 編號，接 0014 之後）與重啟後的冷卻重建。

## 9. 定稿裁決與 DL-6a 實作對照

### 9.1 房內追加裁決（在 Q-A～Q-J 之外）

- **Q-B**：`goal_code` 閉集歸決策層；`goal_code -> capability_tags` 標籤表歸工具層，S-H 才做，DL-6a 不做。`to_loop_trigger` 不直接 import 工具層的表，改由 `ProactiveInjections` 帶 `capability_lookup(goal_code)`；查不到或回空就判 `capability_empty`。
- **`audience_scope_max`**：決策側只傳 scope（`plan.placement.audience_scope_max`＝候選的 `privacy_scope`），不計算標籤子集；最終把關在工具層投影。
- **Q-C**：`uep/decision` 對 `uep.agent` 只准 import `uep.agent.trigger`。
- **import 邊界**（`tests/uep/test_import_boundary.py`）：
  1. `uep/decision` 對 `uep.agent` 只准 import `uep.agent.trigger`。
  2. `uep/agent` 對 `uep.decision` 只准 import `uep.decision.loop_consult` 與 `uep.decision.contracts`（現況 `uep/agent/loop.py` 已 import `loop_consult`，維持通過）。
  3. 被 agent 引用的 `uep/decision/loop_consult.py`、`uep/decision/contracts.py` 對 `uep.agent` 只 import `uep.agent.trigger` 或完全不 import（防循環）。
- **Q-E**：冷卻／預算 ledger 這次由呼叫端注入，是純函式；Phase 2 前須持久化（見第 8 節風險）。

### 9.2 DL-6a 實作對照與偏離

| 項目 | 草案 | 實作 | 理由 |
|---|---|---|---|
| 檔案 | `uep/decision/proactive.py` | 另新增 `uep/decision/replay_proactive.py`（Q-A 的重播工具，照 `replay_turn.py`）與 `tests/uep/fixtures/proactive_eval/`（24 筆） | Q-A 要求 fixture 加重播工具 |
| `candidate_id` 產生 | `new_id("pcand")` | 不新增 ID 前綴；候選建構只驗證安全字元集 | 候選由評估階段產生，DL-6a 沒有 producer；避免為此改 `uep/ids.py` |
| `urgency=critical` 來源（Q-H） | 「需附 policy 來源標記」 | 候選新增欄位 `urgency_source`（`event`／`policy`）；`critical` 必須是 `policy`，否則建構拋 `ValueError` | 讓 Q-H 在型別層就能強制，不靠約定 |
| G8 關卡名 | sub_kind 未列 | 補 `confidence` | 信心門檻是獨立的一關，日誌需區分 |
| G4 `presence_none_preauthorized` | 表列為 G4 ready | 通過 G4 後仍走 G5～G8，最後才 ready（reason_code 保留此值） | 硬性條件不得被早退略過；僅 Presence 為 NONE 時自選位置 |
| `ready_move` | 「需要移轉」 | 位置不符且授權 `may_move` 為真時 ready（`presence_intent=request_move`）；否則 `presence_mismatch_move`（deferred） | 草案未定義何時直接放行；以授權明示為準 |
| 授權通道 | 「授權涵蓋此通道」 | `GrantFact.locations` 必須列出目標通道，空 tuple＝沒有授權任何通道 | 保守預設 |
| `proactive_busy`／`proactive_timeout` | fallback 一律 deferred | engine 用不阻塞的鎖判 busy；超過 `spec.budget_ms` 的結果丟棄並回 timeout | 規則 engine 沒有執行緒池，用最小機制讓兩個 reason_code 可達 |
| 日誌 `sub_kind` | 「facts 填關卡名」 | engine 在回傳前把關卡名寫入 `request.facts["sub_kind"]`（觀察者拿到同一個 request） | 關卡要仲裁後才知道，呼叫端無法事先填 |
| `candidate_id` fallback | 加安全字元檢查 | 用 `[A-Za-z0-9_.:-]{1,128}`（比照 `events.envelope` 的安全參考字元集），不沿用 `_SAFE_TOKEN` | ULID 含大寫，`_SAFE_TOKEN` 只收小寫 |
| `to_loop_trigger` | 回傳 `LoopTrigger \| None` | 同；另提供 `loop_trigger_decision` 回傳 `(trigger, 原因)`，原因含 `capability_empty`、`would_start` | 日誌需要記 would_start／不啟動原因 |
| `plan_sections` | Q-I(a) | 宣告 `loop／placement／followups`；hub 仍只強制 `policy`，其餘由 `test_decision_proactive` 鎖 | 同 Q-I |
| shadow-only 鎖 | assembly 不掛消費者 | AST 鎖：`uep/` 內除 `proactive.py`、`replay_proactive.py` 外不得 import `uep.decision.proactive`、使用其受保護符號；`"proactive.initiate"` 字串只准出現在 `proactive.py` 與 `log.py` | 照 policy 區段 AST 鎖的做法；assembly 組裝屬 DL-6b |
| 註冊 | 照 `memory_dream` 模式 | `register_proactive(hub)`；DL-6a 不接進 `assembly.py` | assembly 掛載（只掛日誌）屬 DL-6b |

reason_code 共 28（24 個判斷結果＋4 個 fallback），fixture 覆蓋 24 個判斷結果，4 個 fallback 由 engine 單元測試覆蓋。

### 9.3 DL-6b 實作對照與偏離

| 項目 | 草案 | 實作 | 理由 |
|---|---|---|---|
| 接線檔位置 | 未指定 | `uep/echo/proactive_wiring.py`（`attach_proactive_shadow`、`ProactiveShadow`） | 沿用 `agent_wiring`／`*_wiring` 慣例；`uep/decision` 保持不依賴 Presence／State／Stop；AST 鎖白名單只加此檔，assembly 只 import 接線檔，不直接碰受保護符號 |
| 掛載條件與順序 | 只掛日誌 | `assembly.py` 在 `echo` 存在且決策日誌啟用的區塊內，於 `DecisionLogWriter.attach` 之前 `register_proactive`；停用時零成本；`CoreAssembly.proactive_shadow` 暴露入口 | 比照 `register_policy_publish`；預設名單已含 `proactive.initiate` |
| 評估入口 | 候選 producer 未定 | `ProactiveShadow.evaluate(candidate)`：`build_request -> hub.submit -> loop_trigger_decision`；只把 `would_start` 與 `LT_*` 寫進 logger（決策本身由決策日誌落盤）；`LoopTrigger` 產生後即丟棄 | Phase 1 無 producer、無計時器；不建 Run、不投遞、不呼叫 `presence.join_or_move`／`report.route` |
| `ProactiveContext` 注入 | 4.4 表 | `presence.peek()`（唯讀）、`state.current(INTERACTION／BEHAVIOR)`、`stop.is_engaged()`、assembly 的 `presence_audience`、`clock`；讀取失敗一律 fail-closed（Presence=RECOVERING、global_stop=True、muted=True） | `peek` 不觸發過期轉換，適合 shadow |
| 暫定欄位 | `authorization`、`mute`、`activity` 待確認 | `grant=None`（授權服務未建）、`private_locations=()`、`ledger`／`budget_used` 由呼叫端傳入；`muted`＝Behavior 為 SLEEP；`activity_interruptible`＝Interaction 為 IDLE；`in_committed_interaction`＝SPEAKING／WAITING_USER；`activity_cost=0.0`；聽眾 scope 缺值取 `scope_public` | 保守預設（寧可不提醒），待 State 軸正式欄位與授權服務 |
| `capability_lookup`／`snapshot_ref` | S-H 提供 | 預設回 None（-> `capability_empty`）；`snapshot_ref` 為 placeholder `snapshot_placeholder_dl6b` | 工具層標籤表與 `PROACTIVE_EVAL` 快照尚未接上；之後只需換掉 `ProactiveShadow` 的注入值 |
| shadow-only 測試 | assembly 層 0 個 Run | `tests/uep/test_assembly_proactive_shadow.py`：評估前後 `session_factory` 零呼叫、`tasks`／`delivery` 零呼叫、`ready`＋lookup None -> `capability_empty`、決策日誌有紀錄；另有接線檔 import／符號鎖 | 以 DB session 零開啟證明沒有 Run／output_intents |
