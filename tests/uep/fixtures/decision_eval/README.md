# 決策中樞行為基線評測集（characterization golden）

放在夾具目錄而非 `docs/`：規格、資料與 golden 同進同出，改資料或重產時一定看得到說明；
上層設計出處為 `hidden_docs/架構重構/決策中樞架構提案.md` §5.2、§7（P1 shadow 一致率門檻、P4 標註與學習模型）。

## 定位

- `golden.jsonl` 是**現況行為快照**（characterization），記錄 `rules.default` 與 `respond.minimal` 對每筆候選目前的
  `outcome`＋`reason_code`。它**不是人工真值**，**不可用於訓練**，也不能當作「正確答案」。
- 用途只有一個：偵測規則行為是否改變（重構、調參、搬遷時的回歸網）。
- 資料皆為手寫合成，不含真實使用者資料。不得放進 `train/`。

## 檔案

| 檔案 | 內容 |
|---|---|
| `candidates.jsonl` | 合成候選（64 筆） |
| `golden.jsonl` | 每筆候選的現況 outcome／reason_code |
| `golden_meta.json` | 產生時的 commit、各 proposer 的 engine version、筆數（`timed` 子物件為時間相關 golden 的對應資訊） |
| `timed_scripts.jsonl` | 時間相關事件腳本（DL-2b，92 行事件，其中 52 筆有結果） |
| `timed_golden.jsonl` | 事件腳本中每個 candidate／tick 事件的現況結果 |
| `tests/uep/decision_eval_runner.py` | 載入與離線重跑（測試與重產腳本共用） |
| `tests/uep/decision_eval_timed_runner.py` | 假 clock 事件回放（DL-2b） |
| `scripts/regen_decision_eval_golden.py` | 重產指令 |
| `tests/uep/test_decision_eval_set.py` | 比對測試 |

## candidates.jsonl 欄位

每行一個 JSON。候選欄位與 `uep.decision.replay_turn.candidate_from_dict` 一致；`ctx` 只認 `_CTX_FIELDS`
（`conversation_id`、`segment_id`、`audience_scope`、`session_ref`、`scope_ref`、`guild_id`、`platform`、`profile_actor_id`）。

| 欄位 | 型別 | 說明 |
|---|---|---|
| `id` | str | 評測集內唯一，格式 `<scenario>-NN` |
| `scenario` | str | 情境組。**同組依檔案順序回放並共用 proposer 狀態，不同組各用全新 proposer** |
| `lang` | `en` / `zh` | 語言批次 |
| `note` | str | 人讀的情境說明（不進比對） |
| `candidate_id` | str | 候選 ID |
| `utterance_refs` | list[str] | 事件 ID |
| `speakers` | list[str] | speaker_ref（可為空） |
| `ctx` | object | SessionContext 欄位 |
| `interaction_state` | str / null | 如 `IDLE`、`LISTENING` |
| `interaction_version` | int / null | |
| `presence_active` | bool | |
| `segments` | int | 合併句數 |
| `participant_count` | int / null | 在場人數；null = 未知 |
| `addressed_hint` | bool | Core 精確喚醒詞比對結果 |
| `text` | str | 原文（合成） |

`deadline` 一律忽略（replay 的既有行為）。

## golden.jsonl 欄位

`{"id": ..., "rules.default": {"outcome", "reason_code"}, "respond.minimal": {"outcome", "reason_code"}}`

## 涵蓋範圍與限制

離線 rerun 只走 `propose`，因此涵蓋：點名（精確／別名／句中）、疑似叫名（tentative）、多人旁聽（observe）、
單人／雙人主動模式（active）、靜默類（過短、語氣詞、感嘆詞 -> not_meaningful）、
人數變動造成的主動／被動切換（`*-hysteresis`）、人數未知、`interaction_state`／`presence_active` 缺漏。

**rerun 無法涵蓋**（需要時間或 Orchestrator 回饋）：`tick` 的插話（`interject_silence`／`interject_context`）、
上下文窗口（`context`／`context_passive`，需要 `on_turn_started`）、插話冷卻（`barge_in_cooldown`）——
這幾項由下方「時間相關情境（timed）」以假 clock 事件回放涵蓋。engagement 的耦合度含真實時間衰減，故測試資料避免依賴邊界值。

**仍不涵蓋**：D80 逾時預設（`core.fallback`）。逾時由 `turn_reply`／hub 直接用 `time.monotonic` 計時，沒有注入 clock 的接點，
涵蓋它必須改產品程式碼，超出本評測集的範疇。

## 時間相關情境（timed，DL-2b）

涵蓋 rerun 回放不到、取決於時間與 Orchestrator 回饋的規則行為。與 `golden.jsonl` 分開存放，既有 59 筆候選與其 golden 不受影響。

- 只針對 `rules.default`（`RuleProposer`，注入假 clock）。`respond.minimal` 沒有時間狀態（無條件 `default_respond`），
  結果不會因時間或回饋而變，故不納入。
- `timed_scripts.jsonl` 每行一個事件，共同欄位 `scenario`、`id`（格式 `<scenario>-NN`）、`lang`、`note`、`type`。
  **同 scenario 依檔案順序回放，共用一個 proposer 與一個假 clock（起點固定 1000.0）；不同 scenario 各用全新的。**

| `type` | 參數 | 對 proposer 的動作 |
|---|---|---|
| `candidate` | 欄位同 `candidates.jsonl` | `propose`；golden 記 `outcome`＋`reason_code` |
| `advance` | `seconds` | 假 clock 前進（不呼叫 proposer） |
| `tick` | 無 | `tick()`；golden 記插話清單（`reason_code`、`reply_mode`、`speaker_ref`、`speakers`、`utterance_refs`） |
| `turn_started` | `turn_id`、`candidate_id`、`speakers` | `on_turn_started`（提議被採用：開 context window、套用冷卻、取走 buffer） |
| `turn_outcome` | `turn_id`、`candidate_id`、`speakers`、`reply_mode`、`reason_code`、`status`、`end_reason`、`generation_ok`、`barge_in`、`started` | `on_turn_outcome`（欄位同 `TurnOutcomeFacts`；`barge_in` 會套用冷卻） |

- `timed_golden.jsonl`：只有 `candidate` 與 `tick` 事件有結果列，格式 `{"id", "type", "outcome", "reason_code"}`
  或 `{"id", "type", "interjections": [...]}`；不含時間戳或亂數 id。
- 情境（各有 `timed-en-*`、`timed-zh-*`）：

| 情境 | 涵蓋的 reason_code | 因 advance 改變的事件 |
|---|---|---|
| `silence` | `interject_silence` | 沉默 15 秒前 tick 無插話，之後 -> `interject_silence` |
| `interject-context` | `interject_context` | 插話冷卻（40 秒）內 tick 無插話，推進 45 秒後 -> `interject_context` |
| `context` | `context`、`active` | 窗口（8 秒）內接話 -> `context`；過期後同一句 -> `active`（單人主動模式） |
| `context-passive` | `context_passive`、`observe` | 窗口內接話 -> `context_passive`；過期後同一句 -> `observe` |
| `barge-cooldown` | `barge_in_cooldown`、`context_passive`、`called` | 冷卻（8 秒）內被擋；冷卻剛過期且窗口仍有效 -> 恢復 `context_passive`；被點名不受冷卻影響 |

- **時間驅動的證明**：`test_result_is_driven_by_advancing_time` 對每個情境重跑「忽略所有 advance（clock 凍結）」的對照序列，
  指定事件的結果必須與有 advance 時不同（例如 `context_passive` 對 `barge_in_cooldown`、插話對無插話）。
- 邊界值刻意留餘量，避開 engagement 衰減區間（10 秒的整數倍）與各 timeout（沉默 10 秒、冷卻 40／8 秒、窗口 8 秒）的精確邊界。
- 新增或修改 `timed_scripts.jsonl` 同樣需要重產（見下）。

## 待 Bernie

以下尚未決定，**本評測集不處理**：

- [ ] `reply_mode` 人工標註欄位（人工真值，P4 訓練與評估用；與本 golden 分開存放）
- [ ] `risky` 人工標註欄位
- [ ] P1 各點 shadow 一致率門檻（§7 P1「約定門檻」）

## 重產流程

測試比對失敗代表**規則行為改變了**。不可直接覆蓋 golden：

1. 看失敗訊息列出的 id，確認每一筆變化都是這次改動**預期**的行為變更（而不是意外迴歸）。
2. 若是意外 -> 修程式，不動 golden。
3. 若是預期 -> 由使用者明確指定後，在 repo 根目錄執行：

```bash
$PY scripts/regen_decision_eval_golden.py                       # 先看差異，不寫檔
$PY scripts/regen_decision_eval_golden.py --confirm-regenerate  # 確認後覆寫 golden.jsonl／timed_golden.jsonl（只寫內容有變的那組）與 golden_meta.json
```

4. 重產後的 diff 與原因寫進 PR 說明。新增或修改 `candidates.jsonl` 同樣需要重產。
