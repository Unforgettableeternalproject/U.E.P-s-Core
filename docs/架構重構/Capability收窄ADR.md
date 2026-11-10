# DL-4 ADR：中樞決策 -> capability 收窄

狀態：**定稿**（D4-1～D4-7 全部照建議裁決，並追加房內裁決；base develop 293eddd）。PR1／PR2 已實作於 `feature/decision-dl4`，PR3（組裝）待做，PR4（工具層）待做。作者：奈也／奈留。依附：DL-0（中樞）、工具層 TG-12／PD-9／AL-3／CN-2、Proactive Q-B。
縮寫：決策中樞 = `docs/架構重構/決策中樞架構提案.md`；Agent提案 = `docs/架構重構/Agent架構與工具契約提案.md`。

## 0. 房內裁決（定稿時追加）

- **D4-3 起步取 b，且不等於收窄**：短期的「政策快照標籤」取「已註冊且 enabled」的標籤，語意上**等同不收窄**。`narrow_capability` 的快照輸入目前不提供任何額外限制，真正的快照收窄要等工具層 `enabled_tags()`（c，長期，排在 PR4）。**不得把 b 誤當成已收窄**；ADR 風險段（§7）已明寫。
- **D4-5 是授權邊界**：擴權只能走快照（或表）。任何路徑讓 plan、hint 或 `LoopTrigger` 帶出比表更大的集合，都算阻擋項；PR2 的窮舉屬性測試涵蓋這一點（`tests/uep/test_decision_capability.py`）。
- **「政策快照.capabilities」的型別**：程式中它是 `FrozenMap`（`uep/context/snapshot.py:192`），不是標籤集合；原提案公式的第三項要用的是標籤集合（`snapshot_tags: frozenset[str]`），兩者不可混用。已在 Agent提案 §3 與決策中樞 §4.6.2 的公式旁加註。
- **PR3 這次不做**：組裝（以 `CAPABILITY_CEILING` 驗證表、`host.py` 改讀表、`capability_tags_for` 接線）留待下一輪。

## 實作對照（PR1／PR2，與草案的差異）

| 項目 | 實作 | 與草案差異 |
|---|---|---|
| `DecisionCapability` | `uep/decision/contracts.py`；`__post_init__` 驗證 hint（tuple／list、元素全為 str），無效 -> `ValueError`；`plan_from_dict` 因此對無效區段拋 `ValueError`（目前無 production 呼叫者） | 草案只寫「區段無效」；選擇拋例外而非回 None，因為回 None 等於「不收窄」，違反 fail-closed |
| `Proposal.capability_hint` | `uep/turn/models.py`，預設 None；`validate_proposal` 對非 tuple／list 或含非字串元素回 `capability_hint`（整個 Proposal 無效，回 D80 預設並計 `proposer.invalid`）；表外標籤仍只丟棄 | 草案未規定 Proposal 層驗證；補上以免畸形 hint 進入 plan |
| `plan_sections` | hub 的白名單從只管 `policy` 擴為 `policy`、`capability`；`turn.reply` 宣告 `capability`，`proactive.initiate` 不宣告（D4-2） | 草案寫「補白名單」；未宣告時剝除並回 fallback，與 `policy` 同行為 |
| `narrow_capability` | `uep/decision/capability.py`；簽名 `(base, hint, snapshot_tags, known, *, route, hint_source, previous_tags) -> NarrowResult`；另有 `route_base`、`record_narrow` | 加 `route`（決定 consult 保留與降級）、`hint_source`（D4-6）、`previous_tags`（單調檢查） |
| refine 單調 | `previous_tags` 給定時，超出前版的部分以交集限制並計 `decision.capability_widen_rejected` | 草案寫「保留前版」；改為「新結果 ∩ 前版」，因前版可能已超出當前快照，交集同時保證 ⊆ 快照與 ⊆ 前版 |
| 降級事件 | `NarrowResult.degraded`＋`counters()`；`record_narrow` 套到 metrics 並記一行 log（只含 reason_code、route、ref，不含 hint 內容）。計數鍵 `decision.capability_degraded` 與 `decision.capability_degraded.<reason_code>` | D4-7 b；**未新增決策日誌欄位、不需 migration**（沿用 metrics 計數與 log；落盤欄位若要加留 PR3） |
| 快照與 base 同時失敗 | `capability_snapshot_unavailable` 優先於 `capability_empty` | 草案未定優先序 |
| `ignore`／`task_request` | 表為空集合，reason 為 `capability_empty` 但不算降級事件（非 agent_loop，且 base 並非查不到） | 避免每個 ignore 回合都計降級 |


## 1. 背景與現況（證據，基準 develop 293eddd）

單向流：`route 預設表 ∩ hint ∩ 快照` -> 決策層 `capability_tags` -> `registry.project` -> `capability_set` -> Run 凍結（AL-3）。目前**只有後半段存在**。

| 環節 | 現況 | 位置 |
|---|---|---|
| 標籤詞彙（閉集、含副作用上限） | 有，11 個標籤 | `uep/adapters/tool.py:33` `CAPABILITY_CEILING` |
| 投影 | 有；`tags & 閉集`、`ceiling`（凍結）、`prior`（前一步）三重單調；閉集外標籤丟棄 | `uep/tools/registry.py:530-560` |
| 投影脈絡 | 有，不取自模型 | `uep/tools/projection.py:26` |
| 迴圈凍結 | 有；空 tags 或空投影 -> `rejected(capability_empty)`；resume 取 凍結 ∩ 當下 | `uep/agent/loop.py:248-266, 289, 443-449` |
| LoopTrigger | 有；空 tags 拒絕 | `uep/agent/trigger.py:30` |
| 入口 | 有 `LoopEntry`，標籤來源是注入的 `capability_tags_for(ticket, proposal)`；**production 無實作者**；空集合 -> 不啟動 | `uep/echo/agent_wiring.py:46-86`、`uep/turn/orchestrator.py:567-575` |
| direct_chat | **寫死** `{"memory.read"}`，投影失敗 fail-closed（不曝光工具） | `uep/echo/host.py:849-863` |
| consult 再驗 | 有；`CONSULT_TAG` 必須在 凍結 ∩ 快照，缺鍵 fail-closed；advice 目前不引用標籤 | `uep/decision/loop_consult.py:280-337` |
| Proposal | 只有 `route`，**無 `capability_hint`** | `uep/turn/models.py:74-84`；`ROUTES` `uep/turn/proposer.py:30` |
| DecisionPlan | **無 capability 區段**（註解標為待擴充） | `uep/decision/contracts.py:121-132`；`plan_from_dict` 忽略未知鍵 `:147-166` |
| plan_sections | hub 只強制 `policy` 白名單，其餘區段不檢查 | `uep/decision/hub.py:88`、`registry.py:24` |
| Proposal<->Result | P0 只轉 route／reply，capability 無處可帶 | `uep/decision/turn_reply.py:74-110` |
| Proactive | `capability_lookup(goal_code)` 注入，查不到／空／例外 -> `capability_empty` | `uep/decision/proactive.py:733-775` |

兩個要先講清楚的事實：
1. **`route` 無下游**：orchestrator 只用 `route != "agent_loop"` 判斷是否進迴圈，`ignore`、`task_request` 沒有消費者。表目前沒有任何呼叫者，等於從零新增，對 production 零風險。
2. **「政策快照.capabilities」不是標籤集合**：`uep/context/snapshot.py:192` 的 `capabilities` 是 `FrozenMap`（例如 `context_adapter`），不是 `frozenset[str]` 的 CapabilityTag。提案公式的第三項**目前沒有可直接用的資料來源**（見待裁決 D4-3）。

## 2. 決策

### 2.1 DecisionPlan 新增 `capability` 區段（只能收窄）

```python
@dataclass(frozen=True)
class DecisionCapability:
    """DecisionPlan.capability：只是「要求收窄到哪些標籤」的提議，不是最終標籤。"""
    capability_hint: tuple[str, ...] | None = None   # None = 不收窄；() = 明確要求空集合
```

- 區段只放 hint，**不放解出來的 `capability_tags`**。最終標籤是純函式的輸出（2.3），由套用者持有並寫進決策日誌與 `LoopTrigger`。理由（奈留）：plan 是「提議的集合」，放結果會讓人以為 plan 可以直接決定授權。
- `None` 與 `()` 要分：`None` 走預設表；`()` 代表要求空集合，結果走 `capability_empty`。不得把 `()` 當成「不收窄」（否則空 hint 會變成最寬，違反 fail-closed）。
- `Proposal` 增選填 `capability_hint: tuple[str, ...] | None = None`（預設 None，P0 行為不變）；`proposal_to_result`／`result_to_proposal` 雙向帶。`plan_to_dict` 已略過 None，舊輸出不變。
- `plan_from_dict` 需補 `capability` 讀取，且 `capability_hint` 元素強制轉 `str`、非序列型別（含單一字串）-> 區段視為無效。

### 2.2 預設能力表

| 表 | 鍵 -> 值 | 歸屬 | 依據 |
|---|---|---|---|
| route 表 `ROUTE_DEFAULT_CAPABILITIES` | `route -> frozenset[tag]` | **決策層資料**（`uep/decision/` 內純資料模組），詞彙由工具層擁有 | 決策中樞提案 Q7 #2、§4.6.2 |
| goal 表 `goal_code -> tags` | `goal_code -> frozenset[tag]` | **工具層**，經 `ProactiveInjections.capability_lookup` 注入 | Proactive Q-B 已裁決，不改 |

- 決策層**不 import 工具層**。組裝層（assembly／`agent_wiring`）啟動時以 `CAPABILITY_CEILING.keys()` 驗證兩張表的每個標籤，**表內有未知標籤 = 啟動失敗**（設定錯誤，不是執行期降級）。
- 預設內容（建議，待工具層確認）：`direct_chat -> {memory.read}`（取代 `host.py:849` 常數，值相同）；`agent_loop -> {memory.read, memory.write, lore.read, chatroom.read, desktop.file_read, web.fetch, decision.consult}`；`task_request`／`ignore -> {}`（`ignore` 永遠不產生 Trigger）。具體集合由工具層切片 B／D 驗收時定案，ADR 只鎖結構與規則。
- 兩張表的不對稱是刻意的：route 是決策層自己的詞彙，goal_code 的能力需求跟著工具走。Proactive 路徑**不經 route 表也不經 hint**，直接 `lookup(goal_code) ∩ 快照`（除非 D4-2 另裁）。

### 2.3 交集公式與 fail-closed

```
narrow_capability(base, hint, snapshot_tags, known) -> NarrowResult(tags, dropped, reason)
  base      = route 表[route]（user_turn／task_request）或 capability_lookup(goal_code)（proactive）
  hint      = plan.capability.capability_hint
  tags = base ∩ known ∩ snapshot_tags
  if hint is not None:
      dropped = hint − known                      # 表外標籤：丟棄並計 decision.hint_dropped
      tags   &= (hint ∩ known) ∪ {decision.consult 若 route == agent_loop}   # consult 恆保留（提案 §4.6.2）
  tags ⊆ base 恆成立（hint 無法新增任何標籤）
```

| 情況 | 處置 | reason_code／計數 |
|---|---|---|
| hint 為 None | 只套表 ∩ 快照 | — |
| hint 含表外標籤 | 丟棄該標籤，其餘照算；**不使 Proposal 無效**（Q18 A） | `decision.hint_dropped` |
| hint 含表內但不在 base 的標籤 | 因交集自然排除，不算擴權；計 `decision.hint_ignored_wider` 供觀察 | 計數 |
| hint 為 `()` 或交集後（扣除 consult）為空 | `agent_loop` -> 降為 `direct_chat`，**不產生 LoopTrigger** | `capability_empty` |
| `base` 查不到（未知 route／goal_code）、表例外 | 視為空集合，同上 | `capability_empty` |
| 快照取不到或形狀錯 | 視為空集合（fail-closed，不退回「全開」） | `capability_snapshot_unavailable` |
| 只剩 `decision.consult`（其餘皆空） | 視為空（consult 單獨存在沒有意義） | `capability_empty` |

- 空交集一律 **fail-closed 為 direct_chat（或不啟動）**，不允許「啟動迴圈但無工具」（Q18 C 否決）。
- 純函式、無 I/O、不 import 工具層；輸入 `known`、`snapshot_tags`、表皆由呼叫端傳入，便於重播與 shadow。
- 兩階段（`plan.refine`）：後版 plan 的 tags 必須 ⊆ 前版（單調，與 AL-3 同構）；違反 -> 保留前版並計 `decision.capability_widen_rejected`。

### 2.4 與 consult（CN-2）、Q26 placement 的關係

- DL-4 只管**Run 開始前**的標籤集合；consult 管**Run 內**的 advice。兩者共享同一不變量：任何後續決定 ⊆ 凍結集合。
- consult 的 `frozen["capability_tags"]` 就是 DL-4 輸出再經 `project` 還原的標籤集合（`loop.py:474`）；DL-4 不改 CN-1／CN-2 介面與 `_effective_scope`。
- **`decision.consult` 恆保留**會讓 DL-4 與 CN-2 銜接：凍結集合含 consult 標籤，迴圈才能呼叫，`loop_consult.py:298` 的檢查才不會因 hint 收窄而永遠 `scope_exceeded`。快照若移除該標籤，迴圈用 fail-safe 預設，這與現況一致。
- Q26：能力擴張一律 escalate -> 新 Run -> 重新走 DL-4。DL-4 因此**不需要**知道 placement；`placement` 的同平台／跨平台判定仍留在 `loop_consult._placement_in_scope`。新 Run 的 hint 只能收窄不能超出表，所以「先問使用者再擴張」的授權必須落在**表或快照**（例如使用者同意後快照多了標籤），不能靠 hint。這點要在 ADR 明寫，否則會有人試圖用 hint 承載同意（待裁決 D4-5）。
- 不加 consult 新 sub_kind，不改 `ADVICE_SECTION`；advice 目前不引用標籤，無需新增再驗。

### 2.5 `Proposal.capability_hint` 保留，但模型 proposer 的 hint 是「建議」

- 保留（Q7 #1 與工具層已談定）。
- 規則 proposer／特徵 proposer 可填；**LLM／模型 proposer（`plan.refine`、Laya 類）也可填**，因為 hint 只能收窄，最壞結果是模型少給能力，不是多給。
- 但模型提供的 hint 視為不可信輸入：只經 2.3 的純函式；不進 prompt 回饋；不得從 hint 推導任何新標籤。
- 反例保護：hint 內任何字串都不當 pattern／萬用字元（`CAPABILITY_CEILING` 是精確比對，`registry.py:545` 同）。

## 3. 替代方案

| 方案 | 內容 | 優點 | 缺點／取捨 | 結論 |
|---|---|---|---|---|
| A（本案） | plan 區段只帶 hint；最終標籤由純函式算，route 表在決策層、goal 表在工具層 | P0 零變化；符合 Q-B、Q7；權威單一；可 shadow | 兩張表分屬兩層，需啟動期驗證 | 採用 |
| B | Proposal／plan 直接帶 `capability_tags`（最終值） | 少一次計算 | 提議變成授權；模型可帶出任意標籤，需另外驗上限；與「只能收窄」語意衝突 | 否決 |
| C | 不加 hint，route 表直接決定，收窄只靠快照 | 最簡單，零新增面 | 失去「本回合只要唯讀」的細粒度；`proactive.initiate` 無法收窄 | 退而求其次（可作 PR1 的保底） |
| D | 兩張表都放工具層（route 與 goal 統一） | 詞彙與表同處，無跨層驗證 | route 是決策詞彙，工具層要認識 `ignore`／`task_request`；與 Q7 #2 相反；工具層膨脹 | 次選，見 D4-1 |
| E | 兩張表都放決策層 | 單一維護點 | 違反已裁決的 Q-B，goal 表需隨工具增減，決策層被迫跟著工具改版 | 否決 |
| F | 迴圈內允許 hint 動態放寬（有核准時） | 少一次新 Run | 破壞 AL-3 與 Q17；checkpoint 授權邊界不可重放 | 否決 |

## 4. 影響範圍

| 層 | 變更 | 備註 |
|---|---|---|
| 決策層 | `contracts.py` 加 `DecisionCapability`＋`plan_from_dict`／序列化；`uep/turn/models.py` 的 `Proposal` 加選填欄位（預設 None）；`turn_reply.py` 雙向轉換；新模組（純函式 `narrow_capability` 與 route 表）；`hub.py` 補 `capability` 區段白名單與單調檢查；`registry.py` 的 `plan_sections` 加 `"capability"`；決策日誌補 reason／計數 | `contracts.py:123` 註解需更新 |
| 工具層 | 提供 `known` 標籤集（已有 `CAPABILITY_CEILING`，可直接給）；goal 表與 `capability_lookup`（S-H）；`capability_tags_for` 實作者（目前無）；提供 snapshot 標籤來源（D4-3） | `project`／`loop`／`consult` 不改 |
| Agent／組裝 | `agent_wiring` 把 `capability_tags_for` 接到 `narrow_capability`；`host.py:849` 常數改讀 route 表 `direct_chat`（值相同） | 後者可延後 |
| migration | **不需要**。無新表、無 schema 變更；`plan_to_dict` 略過 None，舊 `decision_log` 資料可讀；新欄位僅出現在新紀錄 | 若決策日誌 JSON 有 schema 驗證，需確認允許新增鍵（`plan_from_dict` 已忽略未知鍵，向前相容） |
| production 行為 | **零變化直到有人接上 `capability_tags_for`**。接上後只收窄：`direct_chat` 結果仍為 `{memory.read}`；`agent_loop` 目前無 production 實作，不存在「被收窄」的既有行為 | 風險點是「route 表接上後 agent_loop 開始真的跑」，這是功能啟用，不是回歸 |

## 5. 裁決點（全部照建議）

| # | 題目 | 選項 | 裁決（建議） |
|---|---|---|---|
| D4-1 | route 表與 goal 表的歸屬是否統一 | a) route 表在決策層、goal 表在工具層（本案）；b) 兩者皆工具層；c) 兩者皆決策層 | **a**，並在組裝期以 `CAPABILITY_CEILING` 驗證；Q-B 已裁決 goal 表歸工具層，不重開 |
| D4-2 | Proactive 路徑要不要也吃 `capability_hint` | a) 不吃，只 `lookup ∩ 快照`；b) 吃，`proactive.initiate` 可帶 hint | **a** 起步（S-H 才做，shadow-only）；若日後要收窄再開 b，不影響本案 |
| D4-3 | 「政策快照.capabilities」的來源 | a) 在 `PolicySnapshot` 新增 `capability_tags: frozenset[str]` 欄位；b) 先以「已註冊且 enabled 標籤」代替（等同不收窄）；c) 工具層註冊表提供 `enabled_tags()` | **b 起步、c 為長期**：目前 `snapshot.capabilities` 是 `FrozenMap`（非標籤集合），硬套會 fail-closed 成永遠空；a 要動 snapshot 契約，成本最高 |
| D4-4 | route 表初始內容與 `agent_loop` 的標籤集合 | a) 寫死最小集合（`memory.read`＋`decision.consult`）；b) 完整集合（2.2 的建議）；c) 空，等工具層切片 D 定案 | **a** 起步，工具層切片 D 驗收時擴充；擴充必須是「改程式碼＋測試＋審核」，與標籤閉集同規格 |
| D4-5 | Q26 的「使用者同意後擴張」如何進入下一個 Run 的標籤 | a) 同意寫進快照（工具層 enabled／policy 發布）-> 新 Run 重算；b) 同意以 `plan.capability` 的新欄位 `grant` 承載；c) 新 Run 的 LoopTrigger 由 Orchestrator 直接帶更大標籤集合 | **a**；b、c 都會讓「提議」變成授權，且繞過 DL-4 的單調規則。b／c 若要做，需另開 ADR 並經艾斯維爾核准 |
| D4-6 | 模型 proposer 的 hint 是否額外限制 | a) 與規則 proposer 相同（僅純函式約束）；b) 模型 hint 另加「不得為空集合」；c) 不接受模型 hint | **b**：模型輸出 `()` 多半是失誤，空集合會讓 agent_loop 降級 direct_chat，造成靜默退化；以 `hint_empty_model` 計數丟棄比直接降級安全 |
| D4-7 | 降級事件是否寫入決策日誌／回饋 proposer | a) 只計數；b) 計數＋`reason_code`；c) b＋回饋 proposer 作為負樣本 | **b**；c 等 P4 學習模型階段再議 |

## 6. 切分與測試計畫

| PR | 內容 | 依賴 | 測試（重點） |
|---|---|---|---|
| PR1 | `DecisionCapability`＋序列化＋`Proposal.capability_hint`＋雙向轉換＋`plan_sections` 白名單 | 無 | 舊 plan／舊 `decision_log` 往返不變；`None` vs `()` 區分；hint 為單一字串、非字串元素 -> 區段無效；P0 outcome／reason_code 與包裝前一致 |
| PR2 | route 表（資料）＋`narrow_capability` 純函式＋決策日誌計數 | PR1 | 屬性測試：`tags ⊆ base`、`tags ⊆ snapshot`、hint 不能新增；表外標籤丟棄並計數；空交集 -> 降級；未知 route／goal；`decision.consult` 恆保留與「只剩 consult 視為空」；單調（refine 不放寬） |
| PR3 | **決策層部分 done**（組裝期 `validate_route_capabilities` 以 `CAPABILITY_CEILING` 驗證表、`host.py` 常數改讀 route 表 `direct_chat`）；`capability_tags_for`／LoopEntry 接線改由工具層 PR4（房內 #113）。原列：組裝：以 `CAPABILITY_CEILING` 驗證表；`capability_tags_for` 接上純函式；`host.py` 常數改讀表 | PR2＋工具層切片 B／D | 表內未知標籤 -> 啟動失敗；`direct_chat` 結果等同 `{memory.read}`；快照缺失 fail-closed；空 tags 不產生 `LoopTrigger`（對照 `agent_wiring.py:81`） |
| PR4（工具層） | **loop 入口部分 done（`feature/tool-pr4-loop-entry`）**：`ToolRegistry.enabled_tags()`（已註冊、未停用、readiness 非 unavailable，限閉集；`warming` 算入；短期等同不收窄）、`capability_tags_for` 的 production 實作（`make_capability_tags_for`，全交給 `narrow_capability`；`Proposal` 無 hint 來源欄位，一律 `rule`——**偏離 D4-6**：模型 hint 的「空集合丟棄並計數」在 wiring 路徑不會生效，要等 `Proposal` 補來源欄位才接得上）、`LoopEntry` 的 production 組裝（`build_loop_entry`／`LoopLauncher`，旗標 `UEP_AGENT_LOOP_ENTRY`，預設關閉，需 `UEP_TOOL_PATH=agent`）；雙重回覆由 `orchestrator.loop_claimed` 鎖住（宿主對已認領的 turn 不送 ES，交付只經 Output Gate，端點依 `content_ref` 渲染）。**已知限制**：agent_loop turn 目前不寫記憶、也沒有過往上下文（記憶寫入另有卡）；child context 暫用行程內暫存並在 Run 終態清理。**未做**：goal 表＋`capability_lookup`（S-H）。原列：goal 表＋`capability_lookup`（S-H）；snapshot 標籤來源（D4-3） | D4-3 裁決 | `lookup` 例外／空／未知 goal_code -> `capability_empty`（既有 `proactive.py:754` 測試擴充） |
| 邊界測試 | import 邊界、`plan_sections` 與 AL-3／CN-2 反例 | PR2、PR3 | 決策層不 import 工具層（沿用 `test_import_boundary.py`）；AL-3 反例：後續投影不比凍結寬；consult 在 hint 收窄後仍可用；shadow 重播：同輸入同輸出 |

驗收條件：全數 `tests/uep` 綠；開啟新區段但不接 `capability_tags_for` 時 production 輸出與 develop 293eddd 位元級相同。

## 7. 風險與技術債

- 兩張表分屬兩層：維護成本在於新增工具時要改兩處；以組裝期驗證與 CI 檢查把漂移擋在啟動前（奈留：麻煩，但是得做）。
- `route` 目前無下游，表一旦被接上，`agent_loop` 才首次在 production 取得工具；啟用順序應與工具層切片 D／E 同步，並先 shadow 記錄「會算出什麼標籤」。
- `snapshot.capabilities` 語意不符（D4-3）；若直接套用會永遠空集合，這是最容易被忽略的實作陷阱。
- **D4-3 b 等同不收窄（務必記住）**：短期快照標籤取「已註冊且 enabled」，因此在 `enabled_tags()`（c，PR4 工具層）到位前，快照這一項**沒有任何收窄效果**，實際的限制只來自預設表與 hint。不得因為公式裡有「∩ 快照」就以為已有快照層的收窄；也不得把這一點當成授權依據。擴權仍只能走快照（D4-5），所以 `enabled_tags()` 落地前，任何「使用者同意後擴張」都沒有合法的落腳處，只能維持表的範圍。
- 本 ADR 未核對決策日誌 JSON 是否有 schema 驗證，及 `agent_wiring.py` 在 production 組裝處是否另有 `capability_tags_for` 實作（grep 只在 `agent_wiring.py` 內找到）；PR3 前需再確認。
