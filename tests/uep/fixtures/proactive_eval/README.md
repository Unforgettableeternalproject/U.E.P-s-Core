# proactive.initiate 候選 fixture（DL-6a）

`candidates.jsonl`：每行一筆 `{"id", "description", "candidate", "context", "expect"}`，由 `uep.decision.replay_proactive` 重跑。

- `candidate`／`context` 是對預設值的淺層覆寫（預設值與固定時間 `FIXED_NOW` 見 `replay_proactive.py`；`grant`／`policy` 一層合併，`grant: null` 代表沒有授權）。
- `expect` 記錄預期的 `outcome`、`reason_code` 與卡住的關卡（日誌 `sub_kind`）。
- 24 筆涵蓋 24 個非 fallback 的 reason_code：9 關各自至少一筆 rejected／deferred，另有 4 筆 ready。fallback 4 個 reason_code 由 engine 單元測試覆蓋（它們不由資料決定）。
- 這是規則行為的回歸資料，不是人工真值；調整規則或預設門檻時先確認是預期變更，再同步修改 `expect`。
